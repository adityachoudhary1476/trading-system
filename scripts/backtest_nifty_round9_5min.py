#!/usr/bin/env python3
"""Round 9: Finding strategies that trade 3-4 times per week on 5-minute data.

On 5-min data (~70 bars/day), 3-4 trades/week means:
  - ~350 bars/week, 3-4 trades = ~87-116 bars between trades
  - Holding for ~1-2 trading days

Strategy focus:
  - EMA crosses with longer periods (20-50, 50-100) on 5-min data
  - These should produce ~3-4 signals per week with meaningful trends
  - Tight SL/TP to capture intraday moves
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
BARS_PER_DAY = 70  # approx 5-min bars per trading day
WEEKS_PER_YEAR = 50  # approx


def load_5min_dataset():
    df = pd.read_csv("nifty-daily-moves-and-gaps/data/NIFTY 50_5minute.csv", parse_dates=["date"])
    df.index = pd.DatetimeIndex(df["date"])
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    df = df.tz_localize("UTC")
    # Remove zero-volume bars (non-trading hours)
    nonzero = df[df["volume"] > 0].copy()
    if len(nonzero) > 0:
        df = nonzero
    print(f"  5MIN: {len(df)} rows, {df.index[0]} to {df.index[-1]}")
    return HistoricalDataset(symbol="NSE:NIFTY", timeframe="5min", data=df, contract_id="NSE:NIFTY-5min")


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


def ema_cross_spec(fast, slow, stop=None, take=None):
    fk, sk = f"ema_{fast}", f"ema_{slow}"
    risk = {}
    if stop: risk["stop_loss_pct"] = stop
    if take: risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"EMA {fast}-{slow} cross",
        "description": f"LONG when EMA({fast}) crosses above EMA({slow}); exit on cross below.",
        "symbol": "NSE:NIFTY", "timeframe": "5m",
        "indicators": [
            {"name": "ema", "params": {"window": fast}},
            {"name": "ema", "params": {"window": slow}},
        ],
        "entry": make_condition(indicator_operand(fk), "crosses_above", indicator_operand(sk)),
        "exit": make_condition(indicator_operand(fk), "crosses_below", indicator_operand(sk)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round9-script")


def ema_trend_spec(window, stop=None, take=None):
    ema_k = f"ema_{window}"
    risk = {}
    if stop: risk["stop_loss_pct"] = stop
    if take: risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"EMA {window} trend filter",
        "description": f"LONG while close > EMA({window}); exit below.",
        "symbol": "NSE:NIFTY", "timeframe": "5m",
        "indicators": [{"name": "ema", "params": {"window": window}}],
        "entry": make_condition(field_operand("close"), ">", indicator_operand(ema_k)),
        "exit": make_condition(field_operand("close"), "<", indicator_operand(ema_k)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round9-script")


def run_all_backtests(dataset):
    configs = []

    # === EMA crosses on 5-min with periods that target 3-4 trades/week ===
    # On 5-min data: 350 bars/week, 3-4 trades = ~87-116 bars/trade
    for fast, slow in [(10, 20), (15, 30), (20, 50), (20, 60), (30, 60),
                       (30, 90), (50, 100), (50, 200), (20, 100), (15, 50)]:
        wb = slow + 5
        for sl, tp in [(0.02, 0.03), (0.02, 0.04), (0.02, 0.05), (0.03, 0.05), (0.03, 0.06)]:
            configs.append((
                f"EMA {fast}-{slow} cross (SL{int(sl*100)}TP{int(tp*100)}) 5min",
                ema_cross_spec(fast, slow, stop=sl, take=tp),
                True, wb, None,
            ))

    # === EMA trend filters on 5-min ===
    for w in [15, 20, 30, 50, 60, 90, 100, 200]:
        wb = w + 5
        for sl, tp in [(0.02, 0.03), (0.02, 0.04), (0.02, 0.05), (0.03, 0.05)]:
            configs.append((
                f"EMA {w} trend (SL{int(sl*100)}TP{int(tp*100)}) 5min",
                ema_trend_spec(w, stop=sl, take=tp),
                True, wb, None,
            ))

    # === Breakout on 5-min ===
    for lb in [20, 30, 50, 60, 90, 100]:
        wb = lb + 5
        for sl, tp in [(0.02, 0.03), (0.02, 0.04), (0.02, 0.05)]:
            configs.append((
                f"Breakout {lb} (SL{int(sl*100)}TP{int(tp*100)}) 5min",
                BreakoutStrategy(lookback=lb),
                False, wb, {"stop_loss_pct": sl, "take_profit_pct": tp},
            ))

    # === Momentum on 5-min ===
    for window in [10, 20, 30, 50, 60]:
        for entry_thr, exit_thr in [(0.01, 0.0), (0.015, -0.005), (0.02, 0.0),
                                     (0.01, -0.01), (0.015, -0.01)]:
            configs.append((
                f"Mom {window} thr{entry_thr*100:.0f} exit{exit_thr*100:.0f} 5min SL5TP10",
                MomentumStrategy(window=window, entry_thr=entry_thr, exit_thr=exit_thr),
                False, window + 5, {"stop_loss_pct": 0.05, "take_profit_pct": 0.10},
            ))

    results = []
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
            years = len(result.equity_curve) / (BARS_PER_DAY * 252)
            tw = perf.n_trades / years / WEEKS_PER_YEAR
            print(f"Return={perf.total_return*100:.1f}% Sharpe={perf.sharpe:.2f} "
                  f"Trades={perf.n_trades} AvgBars={avg_bars:.1f} "
                  f"Trades/Wk={tw:.1f} Win%={perf.win_rate*100:.0f}% PF={perf.profit_factor or 0:.2f}")
            results.append((name, perf, result, "5min"))
        else:
            print(f"ERR/Trades=0" if perf is None else "Trades=0")

    return results


if __name__ == "__main__":
    print("Loading 5-minute dataset...")
    ds = load_5min_dataset()
    print(f"\nDataset: {ds.symbol} {ds.timeframe}, {len(ds.data)} rows")
    print(f"Date range: {ds.data.index[0]} to {ds.data.index[-1]}")
    print(f"~{len(ds.data)/(BARS_PER_DAY*252):.1f} years")
    print(f"Target: 3-4 trades/week")
    print("\nRunning Round 9 backtests (5-min data)...\n")
    results = run_all_backtests(ds)

    print(f"\n{'='*130}")
    print("ROUND 9 SUMMARY: 5-Minute Data Strategies")
    print(f"{'='*130}")

    rows = []
    for name, perf, result, tf in results:
        avg_bars = len(result.equity_curve) / max(perf.n_trades, 1) if perf.n_trades > 0 else float('inf')
        years = len(result.equity_curve) / (BARS_PER_DAY * 252)
        tw = perf.n_trades / years / WEEKS_PER_YEAR if years > 0 else 0
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
    print(f"{'='*130}")

    profitable = [(n, p, r) for n, p, r in results if p.total_return > 0 and np.isfinite(p.sharpe)]
    profitable.sort(key=lambda x: x[1].sharpe, reverse=True)

    print(f"\n--- Profitable strategies sorted by Sharpe ---")
    for n, p, r in profitable[:30]:
        avg_bars = len(r.equity_curve) / max(p.n_trades, 1) if p.n_trades > 0 else float('inf')
        years = len(r.equity_curve) / (BARS_PER_DAY * 252)
        tw = p.n_trades / years / WEEKS_PER_YEAR if years > 0 else 0
        print(f"  {n}: Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
              f"AvgBars={avg_bars:.1f} Trades={p.n_trades} Trades/Wk={tw:.1f} PF={p.profit_factor or 0:.2f}")

    # Find strategies closest to 3-4 trades/week that are profitable
    near_target = [(n, p, r) for n, p, r in profitable
                   if 2.5 <= (p.n_trades / (len(r.equity_curve) / (BARS_PER_DAY * 252)) / WEEKS_PER_YEAR) <= 5.0]
    near_target.sort(key=lambda x: abs(
        (x[1].n_trades / (len(x[2].equity_curve) / (BARS_PER_DAY * 252)) / WEEKS_PER_YEAR) - 3.5))

    print(f"\n--- Strategies targeting 3-4 trades/week (profitable, Trades/Wk 2.5-5.0) ---")
    if near_target:
        for n, p, r in near_target[:20]:
            avg_bars = len(r.equity_curve) / max(p.n_trades, 1)
            tw = p.n_trades / (len(r.equity_curve) / (BARS_PER_DAY * 252)) / WEEKS_PER_YEAR
            print(f"  {n}: Trades/Wk={tw:.1f} Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
                  f"AvgBars={avg_bars:.1f} PF={p.profit_factor or 0:.2f} MaxDD={p.max_drawdown*100:.1f}%")
    else:
        print("  None found in 3-4 trades/week range while profitable.")
        print("  Showing closest profitable strategies by trade frequency:")
        freq_sorted = sorted(profitable, key=lambda x:
            (x[1].n_trades / (len(x[2].equity_curve) / (BARS_PER_DAY * 252)) / WEEKS_PER_YEAR), reverse=True)
        for n, p, r in freq_sorted[:10]:
            avg_bars = len(r.equity_curve) / max(p.n_trades, 1)
            tw = p.n_trades / (len(r.equity_curve) / (BARS_PER_DAY * 252)) / WEEKS_PER_YEAR
            print(f"  {n}: Trades/Wk={tw:.1f} Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
                  f"AvgBars={avg_bars:.1f} PF={p.profit_factor or 0:.2f}")

    print(f"\nTotal: {len(results)} valid, {len(profitable)} profitable")
