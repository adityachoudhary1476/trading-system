#!/usr/bin/env python3
"""Round 10: Targeted 3-4 trades/week search on 30-min + daily data.

3-4 trades/week means:
  On daily data: ~2.5 round-trips/week → ~2.5 trade events/week → need many trades, hard to be profitable
  On 30-min data: ~3 trades/week → 35400 bars / (544 weeks * 3) = ~21.5 AvgBars

Focus: strategies on 30-min data with longer-period indicators that hit ~3-4 trades/week.
Also test daily data with time-based exits (force exit after N days) to boost frequency.
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
BARS_PER_DAY_30MIN = 13  # ~13 30-min bars per trading day
WEEKS_PER_YEAR = 50
TRADING_DAYS_PER_YEAR = 252


def load_30min_dataset():
    df = pd.read_csv("nifty-daily-moves-and-gaps/data/NIFTY 50_5minute.csv", parse_dates=["date"])
    df.index = pd.DatetimeIndex(df["date"])
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    df = df.tz_localize("UTC")
    # NOTE: 5-min data has zero volume, so do NOT filter by volume
    df = df.resample("30min").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    print(f"  30MIN: {len(df)} rows, {df.index[0]} to {df.index[-1]}")
    return HistoricalDataset(symbol="NSE:NIFTY", timeframe="30m", data=df, contract_id="NSE:NIFTY-30min")


def load_daily_dataset():
    df = pd.read_csv("nifty-daily-moves-and-gaps/data/nifty50.csv", parse_dates=["Date"])
    df.index = pd.DatetimeIndex(df["Date"])
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    df = df.tz_localize("UTC")
    return HistoricalDataset(symbol="NSE:NIFTY", timeframe="1d", data=df, contract_id="NSE:NIFTY")


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


def ema_cross_spec(fast, slow, stop=None, take=None, timeframe="30m"):
    fk, sk = f"ema_{fast}", f"ema_{slow}"
    risk = {}
    if stop: risk["stop_loss_pct"] = stop
    if take: risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"EMA {fast}-{slow} cross",
        "description": f"LONG when EMA({fast}) crosses above EMA({slow}); exit on cross below.",
        "symbol": "NSE:NIFTY", "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": fast}},
            {"name": "ema", "params": {"window": slow}},
        ],
        "entry": make_condition(indicator_operand(fk), "crosses_above", indicator_operand(sk)),
        "exit": make_condition(indicator_operand(fk), "crosses_below", indicator_operand(sk)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round10-script")


def ema_trend_spec(window, stop=None, take=None, timeframe="30m"):
    ema_k = f"ema_{window}"
    risk = {}
    if stop: risk["stop_loss_pct"] = stop
    if take: risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"EMA {window} trend filter",
        "description": f"LONG while close > EMA({window}); exit below.",
        "symbol": "NSE:NIFTY", "timeframe": timeframe,
        "indicators": [{"name": "ema", "params": {"window": window}}],
        "entry": make_condition(field_operand("close"), ">", indicator_operand(ema_k)),
        "exit": make_condition(field_operand("close"), "<", indicator_operand(ema_k)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round10-script")


def donchian_spec(window, stop=None, take=None):
    uk = f"donchian_upper_{window}"
    lk = f"donchian_lower_{window}"
    risk = {}
    if stop: risk["stop_loss_pct"] = stop
    if take: risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"Donchian {window} breakout",
        "description": f"LONG when close > upper Donchian({window}); exit when close < lower.",
        "symbol": "NSE:NIFTY", "timeframe": "30m",
        "indicators": [
            {"name": "donchian_upper", "params": {"window": window}},
            {"name": "donchian_lower", "params": {"window": window}},
        ],
        "entry": make_condition(field_operand("close"), ">", indicator_operand(uk)),
        "exit": make_condition(field_operand("close"), "<", indicator_operand(lk)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round10-script")


def calc_trades_per_week(result, perf, bars_per_day):
    """Calculate trades per week correctly."""
    total_bars = len(result.equity_curve)
    trading_days = total_bars / bars_per_day
    weeks = trading_days / 5
    return perf.n_trades / weeks if weeks > 0 else 0


def run_30min_backtests(dataset):
    results = []
    configs = []

    # === EMA crosses on 30-min targeting 3-4 trades/week ===
    # On 30-min: ~544 weeks, 3-4 trades/week = 1632-2176 trades
    # AvgBars = 35400 / 1900 ≈ 18.6
    # We need strategies with AvgBars ≈ 15-25 that are profitable
    for fast, slow in [(10, 20), (10, 30), (15, 30), (15, 40), (20, 40),
                       (20, 50), (20, 60), (30, 60), (15, 50), (10, 40)]:
        wb = slow + 5
        for sl, tp in [(0.02, 0.03), (0.02, 0.04), (0.02, 0.05), (0.03, 0.05), (0.03, 0.06)]:
            configs.append((
                f"EMA {fast}-{slow} cross 30min SL{int(sl*100)}TP{int(tp*100)}",
                ema_cross_spec(fast, slow, stop=sl, take=tp, timeframe="30m"),
                True, wb, None, "30min",
            ))

    # === Donchian breakouts on 30-min ===
    for w in [10, 15, 20, 25, 30, 40, 50]:
        wb = w + 5
        for sl, tp in [(0.02, 0.03), (0.02, 0.04), (0.02, 0.05), (0.03, 0.05)]:
            configs.append((
                f"Donchian {w} 30min SL{int(sl*100)}TP{int(tp*100)}",
                donchian_spec(w, stop=sl, take=tp),
                True, wb, None, "30min",
            ))

    # === EMA trend filters on 30-min with moderate periods ===
    for w in [30, 40, 50, 60, 80, 100]:
        wb = w + 5
        for sl, tp in [(0.02, 0.03), (0.02, 0.04), (0.02, 0.05)]:
            configs.append((
                f"EMA {w} trend 30min SL{int(sl*100)}TP{int(tp*100)}",
                ema_trend_spec(w, stop=sl, take=tp, timeframe="30m"),
                True, wb, None, "30min",
            ))

    for name, strat, is_spec, wb, risk_dict, tf in configs:
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
            bpd = BARS_PER_DAY_30MIN
            tw = calc_trades_per_week(result, perf, bpd)
            print(f"Return={perf.total_return*100:.1f}% Sharpe={perf.sharpe:.2f} "
                  f"Trades={perf.n_trades} AvgBars={avg_bars:.1f} "
                  f"Trades/Wk={tw:.1f} Win%={perf.win_rate*100:.0f}% PF={perf.profit_factor or 0:.2f}")
            results.append((name, perf, result, tf, bpd))
        else:
            print(f"ERR/Trades=0")

    return results


def print_summary(results):
    print(f"\n{'='*140}")
    print("ROUND 10 SUMMARY: Strategies targeting 3-4 trades/week (30-min data)")
    print(f"{'='*140}")

    rows = []
    for name, perf, result, tf, bpd in results:
        avg_bars = len(result.equity_curve) / max(perf.n_trades, 1) if perf.n_trades > 0 else float('inf')
        tw = calc_trades_per_week(result, perf, bpd)
        rows.append([
            name,
            f"{perf.total_return*100:.1f}%",
            f"{perf.sharpe:.2f}" if np.isfinite(perf.sharpe) else "N/A",
            f"{avg_bars:.1f}",
            str(perf.n_trades),
            f"{tw:.1f}",
            f"{perf.win_rate*100:.0f}%",
            f"{perf.profit_factor:.2f}" if perf.profit_factor and np.isfinite(perf.profit_factor) else "N/A",
            f"{perf.max_drawdown*100:.1f}%",
        ])
    headers = ["Strategy", "Return", "Sharpe", "AvgBars", "Trades", "Trades/Wk", "Win%", "PF", "MaxDD"]
    print(tabulate(rows, headers=headers, tablefmt="grid"))
    print(f"{'='*140}")

    profitable = [(n, p, r, tf, bpd) for n, p, r, tf, bpd in results if p.total_return > 0 and np.isfinite(p.sharpe)]

    # Target range: 2.5-5.0 trades/week
    target = [(n, p, r, tf, bpd) for n, p, r, tf, bpd in profitable
              if 2.5 <= calc_trades_per_week(r, p, bpd) <= 5.0]
    target.sort(key=lambda x: x[1].sharpe, reverse=True)

    print(f"\n--- Profitable strategies in 3-4 trades/week range (Trades/Wk 2.5-5.0) ---")
    if target:
        for n, p, r, tf, bpd in target[:25]:
            ab = len(r.equity_curve) / max(p.n_trades, 1)
            tw = calc_trades_per_week(r, p, bpd)
            print(f"  {n}: Trades/Wk={tw:.1f} Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
                  f"AvgBars={ab:.1f} Trades={p.n_trades} PF={p.profit_factor or 0:.2f} MaxDD={p.max_drawdown*100:.1f}%")
    else:
        print("  None found in exact 3-4 range. Closest by Sharpe (all profitable):")
        profitable.sort(key=lambda x: x[1].sharpe, reverse=True)
        for n, p, r, tf, bpd in profitable[:20]:
            ab = len(r.equity_curve) / max(p.n_trades, 1)
            tw = calc_trades_per_week(r, p, bpd)
            print(f"  {n}: Trades/Wk={tw:.1f} Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
                  f"AvgBars={ab:.1f} Trades={p.n_trades} PF={p.profit_factor or 0:.2f}")

    # Also show most frequent profitable
    freq_profitable = sorted(profitable, key=lambda x: calc_trades_per_week(x[2], x[1], x[4]), reverse=True)
    print(f"\n--- Most frequent profitable strategies ---")
    for n, p, r, tf, bpd in freq_profitable[:10]:
        ab = len(r.equity_curve) / max(p.n_trades, 1)
        tw = calc_trades_per_week(r, p, bpd)
        print(f"  {n}: Trades/Wk={tw:.1f} Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
              f"AvgBars={ab:.1f} PF={p.profit_factor or 0:.2f}")

    print(f"\nTotal: {len(results)} valid, {len(profitable)} profitable, {len(target)} in 3-4/wk range")


if __name__ == "__main__":
    print("Loading 30-minute aggregated dataset...\n")
    ds = load_30min_dataset()
    print(f"\nDataset: {ds.symbol} {ds.timeframe}, {len(ds.data)} rows")
    print(f"Date range: {ds.data.index[0]} to {ds.data.index[-1]}")
    print(f"~{len(ds.data)/(BARS_PER_DAY_30MIN*TRADING_DAYS_PER_YEAR):.1f} years, ~{len(ds.data)/(BARS_PER_DAY_30MIN*5):.0f} weeks")
    print(f"Target: 3-4 trades/week = {len(ds.data)/(BARS_PER_DAY_30MIN*5)*3:.0f}-{len(ds.data)/(BARS_PER_DAY_30MIN*5)*4:.0f} total trades")
    print("\nRunning Round 10 backtests...\n")
    results = run_30min_backtests(ds)
    print_summary(results)
