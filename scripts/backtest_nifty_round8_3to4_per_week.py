#!/usr/bin/env python3
"""Round 8: 3-4 trades per week — exploring approaches to reach target frequency.

Two approaches:
  A) Daily data with position-reversal (reverse on EMA flip) — maximizes trade count
  B) 5-minute data aggregated to 30-min bars — gives more data points for higher frequency

On 5-min data (~70 bars/day), 3-4 trades/week means ~87-116 bars/trade.
On daily data, we need strategies that turn over quickly.
"""
from __future__ import annotations

import warnings
import numpy as np
import pandas as pd
from trading_system.research.backtester import BacktestConfig, run_backtest
from trading_system.research.costs import IndiaTransactionCostModel, Segment as CostSegment
from trading_system.research.dataset import HistoricalDataset
from trading_system.research.performance import compute_performance
from trading_system.research.strategies import EMATrendStrategy, MomentumStrategy, BreakoutStrategy
from trading_system.research.strategy_lab.spec import (
    StrategySpec, const_operand, field_operand,
    indicator_operand, logic, make_condition,
)
from trading_system.research.strategy_lab.interpreter import build_strategy
from trading_system.research.strategy_lab.engine import merged_backtest_config
from trading_system.research.risk import RiskConfig
from tabulate import tabulate

warnings.filterwarnings("ignore", category=FutureWarning)

INITIAL_CAPITAL = 100_000.0
TRADING_DAYS_PER_YEAR = 252
WEEKS_PER_YEAR = TRADING_DAYS_PER_YEAR / 5  # ~50


def load_daily_dataset():
    df = pd.read_csv("nifty-daily-moves-and-gaps/data/nifty50.csv", parse_dates=["Date"])
    df.index = pd.DatetimeIndex(df["Date"])
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    df = df.tz_localize("UTC")
    print(f"  DAILY: {len(df)} rows, {df.index[0].date()} to {df.index[-1].date()}")
    return HistoricalDataset(symbol="NSE:NIFTY", timeframe="1d", data=df, contract_id="NSE:NIFTY")


def load_30min_dataset():
    """Load 5-min data and aggregate to 30-min bars."""
    df = pd.read_csv("nifty-daily-moves-and-gaps/data/NIFTY 50_5minute.csv", parse_dates=["date"])
    df.index = pd.DatetimeIndex(df["date"])
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    df = df.tz_localize("UTC")

    # Aggregate to 30-minute bars
    df = df.resample("30min").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()

    print(f"  30MIN: {len(df)} rows, {df.index[0]} to {df.index[-1]}")
    return HistoricalDataset(symbol="NSE:NIFTY", timeframe="30min", data=df, contract_id="NSE:NIFTY-30min")


def make_risk_config(stop_loss_pct=None, take_profit_pct=None, max_alloc=0.95):
    return RiskConfig(
        max_allocation_pct=max_alloc, allow_short=False,
        stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct,
    )


def run_builtin(dataset, strat, wb, stop=None, take=None):
    cfg = BacktestConfig(
        initial_capital=INITIAL_CAPITAL, slippage_pct=0.001,
        cost_model=IndiaTransactionCostModel(), cost_segment=CostSegment.EQUITY_FUTURE.value,
        warmup_bars=wb, risk=make_risk_config(stop, take),
    )
    result = run_backtest(dataset, strat, cfg)
    perf = compute_performance(result)
    return perf, result, cfg


def run_spec(dataset, spec, wb):
    base = BacktestConfig(
        initial_capital=INITIAL_CAPITAL, slippage_pct=0.001,
        cost_model=IndiaTransactionCostModel(), cost_segment=CostSegment.EQUITY_FUTURE.value,
        warmup_bars=wb,
    )
    cfg = merged_backtest_config(spec, base)
    strategy = build_strategy(spec)
    result = run_backtest(dataset, strategy, cfg)
    perf = compute_performance(result)
    return perf, result, cfg


# === EMA cross spec (standard, no reversal) ===
def ema_cross_spec(fast, slow, stop=None, take=None):
    fk, sk = f"ema_{fast}", f"ema_{slow}"
    risk = {}
    if stop: risk["stop_loss_pct"] = stop
    if take: risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"EMA {fast}-{slow} cross",
        "description": f"LONG when EMA({fast}) crosses above EMA({slow}); exit on cross below.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [
            {"name": "ema", "params": {"window": fast}},
            {"name": "ema", "params": {"window": slow}},
        ],
        "entry": make_condition(indicator_operand(fk), "crosses_above", indicator_operand(sk)),
        "exit": make_condition(indicator_operand(fk), "crosses_below", indicator_operand(sk)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round8-script")


# === EMA trend filter spec ===
def ema_trend_spec(window, stop=None, take=None):
    ema_k = f"ema_{window}"
    risk = {}
    if stop: risk["stop_loss_pct"] = stop
    if take: risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"EMA {window} trend filter",
        "description": f"LONG while close > EMA({window}); exit below.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [{"name": "ema", "params": {"window": window}}],
        "entry": make_condition(field_operand("close"), ">", indicator_operand(ema_k)),
        "exit": make_condition(field_operand("close"), "<", indicator_operand(ema_k)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round8-script")


# === Momentum with tight exit (exit at breakeven or small profit) ===
def momentum_tight_spec(window, entry_thr, exit_thr):
    mk = f"mom_{window}"
    return StrategySpec.from_model_json({
        "name": f"Mom {window} thr{entry_thr*100:.0f} exit{exit_thr*100:.0f} tight",
        "description": f"LONG when momentum({window}) > {entry_thr*100:.1f}%; exit at {exit_thr*100:.1f}%.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [{"name": "momentum", "params": {"window": window}}],
        "entry": make_condition(indicator_operand(mk), ">", const_operand(entry_thr)),
        "exit": make_condition(indicator_operand(mk), "<", const_operand(exit_thr)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": {},
    }, model="round8-script")


# === Donchian Channel breakout ===
def donchian_spec(window, stop=None, take=None):
    uk = f"donchian_upper_{window}"
    lk = f"donchian_lower_{window}"
    risk = {}
    if stop: risk["stop_loss_pct"] = stop
    if take: risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"Donchian {window} breakout",
        "description": f"LONG when close > upper Donchian({window}); exit when close < lower.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [
            {"name": "donchian_upper", "params": {"window": window}},
            {"name": "donchian_lower", "params": {"window": window}},
        ],
        "entry": make_condition(field_operand("close"), ">", indicator_operand(uk)),
        "exit": make_condition(field_operand("close"), "<", indicator_operand(lk)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round8-script")


def run_30min_backtests(dataset):
    results = []
    configs = []

    # On 30-min data, each day has ~13 bars. 3-4 trades/week = ~17-22 trades per 3-day period
    # = ~7-8 days per trade on 30-min data
    # With 5558 bars over 10 years, 3-4 trades/week = ~1500-2000 trades total
    # AvgBars = 5558 / 1750 ≈ 3.2

    # Ultra-short EMA crosses on 30-min
    for fast, slow in [(5, 10), (10, 20), (8, 13), (5, 15), (3, 10), (10, 15), (15, 25), (20, 30), (15, 30), (25, 50)]:
        wb = slow + 5
        for sl, tp in [(0.02, 0.03), (0.02, 0.04), (0.03, 0.05), (0.02, 0.05)]:
            configs.append((
                f"EMA {fast}-{slow} 30min SL{int(sl*100)}TP{int(tp*100)}",
                ema_cross_spec(fast, slow, stop=sl, take=tp),
                True, wb, None,
            ))

    # EMA trend filters on 30-min
    for w in [10, 15, 20, 25, 30, 40, 50]:
        wb = w + 5
        for sl, tp in [(0.02, 0.03), (0.02, 0.04), (0.03, 0.05)]:
            configs.append((
                f"EMA {w} trend 30min SL{int(sl*100)}TP{int(tp*100)}",
                ema_trend_spec(w, stop=sl, take=tp),
                True, wb, None,
            ))

    # Donchian breakout on 30-min
    for w in [10, 15, 20, 30, 50]:
        wb = w + 5
        for sl, tp in [(0.02, 0.03), (0.02, 0.04), (0.02, 0.05)]:
            configs.append((
                f"Donchian {w} 30min SL{int(sl*100)}TP{int(tp*100)}",
                donchian_spec(w, stop=sl, take=tp),
                True, wb, None,
            ))

    # Momentum on 30-min
    for window in [5, 10, 15, 20, 30]:
        for entry_thr, exit_thr in [(0.01, 0.0), (0.015, -0.005), (0.02, 0.0),
                                     (0.01, -0.01), (0.015, -0.01)]:
            configs.append((
                f"Mom {window} thr{entry_thr*100:.0f} exit{exit_thr*100:.0f} 30min",
                MomentumStrategy(window=window, entry_thr=entry_thr, exit_thr=exit_thr),
                False, window + 5, {"stop_loss_pct": 0.03, "take_profit_pct": 0.05},
            ))

    for name, strat, is_spec, wb, risk_dict in configs:
        print(f"--- {name} (wb={wb}) ---", end=" ")
        if is_spec:
            perf, result, cfg = run_spec(dataset, strat, wb)
        else:
            perf, result, cfg = run_builtin(
                dataset, strat, wb,
                risk_dict.get("stop_loss_pct") if risk_dict else None,
                risk_dict.get("take_profit_pct") if risk_dict else None,
            )
        if perf is not None and perf.n_trades > 0:
            avg_bars = len(result.equity_curve) / max(perf.n_trades, 1)
            trading_days = len(result.equity_curve) / 13  # ~13 bars per day in 30-min
            tw = perf.n_trades / (trading_days / WEEKS_PER_YEAR)
            print(f"Return={perf.total_return*100:.1f}% Sharpe={perf.sharpe:.2f} "
                  f"Trades={perf.n_trades} AvgBars={avg_bars:.1f} "
                  f"Trades/Wk={tw:.1f} Win%={perf.win_rate*100:.0f}% PF={perf.profit_factor or 0:.2f}")
            results.append((name, perf, result))
        else:
            print(f"ERR/Trades=0")

    return results


def run_daily_position_reversal(dataset):
    """Test strategies with position reversal — reverse direction on every signal flip."""
    results = []
    configs = []

    # These use the built-in EMATrendStrategy which already reverses position
    # Try very short EMAs that flip frequently
    for fast, slow in [(2, 3), (2, 4), (2, 5), (3, 4), (3, 5)]:
        wb = slow + 2
        for sl, tp in [(0.02, 0.03), (0.02, 0.04), (0.02, 0.05), (0.03, 0.05)]:
            configs.append((
                f"EMA {fast}-{slow} reversal SL{int(sl*100)}TP{int(tp*100)}",
                EMATrendStrategy(fast=fast, slow=slow),
                False, wb, {"stop_loss_pct": sl, "take_profit_pct": tp},
                True,  # is_spec = False for built-in
            ))

    # Trend filters with reversal (built-in strategy already reverses)
    for w in [2, 3, 4, 5]:
        wb = w + 2
        for sl, tp in [(0.02, 0.03), (0.02, 0.04), (0.02, 0.05)]:
            configs.append((
                f"EMA {w} trend reversal SL{int(sl*100)}TP{int(tp*100)}",
                ema_trend_spec(w, stop=sl, take=tp),
                True, wb, None, True,
            ))

    for name, strat, is_spec, wb, risk_dict, _ in configs:
        print(f"--- {name} (wb={wb}) ---", end=" ")
        if is_spec:
            perf, result, cfg = run_spec(dataset, strat, wb)
        else:
            perf, result, cfg = run_builtin(
                dataset, strat, wb,
                risk_dict.get("stop_loss_pct") if risk_dict else None,
                risk_dict.get("take_profit_pct") if risk_dict else None,
            )
        if perf is not None and perf.n_trades > 0:
            avg_bars = len(result.equity_curve) / max(perf.n_trades, 1)
            tw = perf.n_trades / (len(result.equity_curve) / TRADING_DAYS_PER_YEAR) / WEEKS_PER_YEAR
            print(f"Return={perf.total_return*100:.1f}% Sharpe={perf.sharpe:.2f} "
                  f"Trades={perf.n_trades} AvgBars={avg_bars:.1f} "
                  f"Trades/Wk={tw:.1f} Win%={perf.win_rate*100:.0f}% PF={perf.profit_factor or 0:.2f}")
            results.append((name, perf, result))
        else:
            print(f"ERR/Trades=0")

    return results


if __name__ == "__main__":
    print("=== Approach A: 30-min data strategies ===")
    ds_30min = load_30min_dataset()
    print(f"\nRunning 30-min backtests...\n")
    results_30 = run_30min_backtests(ds_30min)

    print(f"\n=== Approach B: Daily position reversal ===")
    ds_daily = load_daily_dataset()
    print(f"\nRunning daily reversal backtests...\n")
    results_daily = run_daily_position_reversal(ds_daily)

    # === Summary ===
    print(f"\n{'='*120}")
    print("ROUND 8 SUMMARY: Best strategies targeting 3-4 trades/week")
    print(f"{'='*120}")

    all_results = [("30min", n, p, r) for n, p, r in results_30] + [("daily", n, p, r) for n, p, r in results_daily]

    # Sort by Sharpe (descending), only profitable
    profitable = [(tf, n, p, r) for tf, n, p, r in all_results if p.total_return > 0 and np.isfinite(p.sharpe)]
    profitable.sort(key=lambda x: x[2].sharpe, reverse=True)

    print(f"\n--- All profitable strategies sorted by Sharpe ---")
    for tf, n, p, r in profitable[:25]:
        avg_bars = len(r.equity_curve) / max(p.n_trades, 1) if p.n_trades > 0 else float('inf')
        # Calculate trades/week
        if tf == "30min":
            trading_periods = len(r.equity_curve) / 13
        else:
            trading_periods = len(r.equity_curve) / TRADING_DAYS_PER_YEAR
        tw = p.n_trades / trading_periods / WEEKS_PER_YEAR if trading_periods > 0 else 0
        print(f"  [{tf}] {n}: Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
              f"AvgBars={avg_bars:.1f} Trades={p.n_trades} Trades/Wk={tw:.1f} PF={p.profit_factor or 0:.2f}")

    # Specifically find strategies closest to 3-4 trades/week
    target_freq = profitable
    target_freq.sort(key=lambda x: abs(x[2].n_trades / (len(x[3].equity_curve) / (13 if x[0] == "30min" else TRADING_DAYS_PER_YEAR)) / WEEKS_PER_YEAR - 3.5))
    print(f"\n--- Strategies closest to 3-4 trades/week (profitable) ---")
    for tf, n, p, r in target_freq[:15]:
        avg_bars = len(r.equity_curve) / max(p.n_trades, 1) if p.n_trades > 0 else float('inf')
        if tf == "30min":
            trading_periods = len(r.equity_curve) / 13
        else:
            trading_periods = len(r.equity_curve) / TRADING_DAYS_PER_YEAR
        tw = p.n_trades / trading_periods / WEEKS_PER_YEAR if trading_periods > 0 else 0
        print(f"  [{tf}] {n}: Trades/Wk={tw:.1f} Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
              f"AvgBars={avg_bars:.1f} PF={p.profit_factor or 0:.2f} MaxDD={p.max_drawdown*100:.1f}%")

    print(f"\nTotal strategies tested: {len(all_results)}")
    print(f"Profitable: {len(profitable)}")
