#!/usr/bin/env python3
"""Round 11: 3-4 trades/week on 5-minute data.

On 5-min data: ~70 bars/day, 350 bars/week.
3-4 trades/week => AvgBars ~87-116.

Strategy focus:
  - EMA crosses at 50-100, 100-200 period (≈1-4 hour holding period)
  - Donchian breakouts at 50-100 period
  - EMA trend filters at 80-200 period
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
BARS_PER_DAY_5MIN = 70
WEEKS_PER_YEAR = 50


def load_5min_dataset():
    df = pd.read_csv("nifty-daily-moves-and-gaps/data/NIFTY 50_5minute.csv", parse_dates=["date"])
    df.index = pd.DatetimeIndex(df["date"])
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    df = df.tz_localize("UTC")
    print(f"  5MIN: {len(df)} rows, {df.index[0]} to {df.index[-1]}")
    return HistoricalDataset(symbol="NSE:NIFTY", timeframe="5m", data=df, contract_id="NSE:NIFTY-5min")


def make_risk_config(stop_loss_pct=None, take_profit_pct=None, max_alloc=0.95):
    return RiskConfig(max_allocation_pct=max_alloc, allow_short=False,
                       stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct)


def run_builtin(dataset, strat, wb, stop=None, take=None):
    cfg = BacktestConfig(initial_capital=INITIAL_CAPITAL, slippage_pct=0.001,
                         cost_model=IndiaTransactionCostModel(), cost_segment=CostSegment.EQUITY_FUTURE.value,
                         warmup_bars=wb, risk=make_risk_config(stop, take))
    result = run_backtest(dataset, strat, cfg)
    perf = compute_performance(result)
    return perf, result, cfg


def run_spec(dataset, spec, wb):
    base = BacktestConfig(initial_capital=INITIAL_CAPITAL, slippage_pct=0.001,
                          cost_model=IndiaTransactionCostModel(), cost_segment=CostSegment.EQUITY_FUTURE.value,
                          warmup_bars=wb)
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
        "symbol": "NSE:NIFTY", "timeframe": "5m",
        "indicators": [{"name": "ema", "params": {"window": fast}}, {"name": "ema", "params": {"window": slow}}],
        "entry": make_condition(indicator_operand(fk), "crosses_above", indicator_operand(sk)),
        "exit": make_condition(indicator_operand(fk), "crosses_below", indicator_operand(sk)),
        "position_sizing": {"max_allocation_pct": 0.95}, "risk": risk,
    }, model="round11-script")


def ema_trend_spec(window, stop=None, take=None):
    ema_k = f"ema_{window}"
    risk = {}
    if stop: risk["stop_loss_pct"] = stop
    if take: risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"EMA {window} trend filter",
        "symbol": "NSE:NIFTY", "timeframe": "5m",
        "indicators": [{"name": "ema", "params": {"window": window}}],
        "entry": make_condition(field_operand("close"), ">", indicator_operand(ema_k)),
        "exit": make_condition(field_operand("close"), "<", indicator_operand(ema_k)),
        "position_sizing": {"max_allocation_pct": 0.95}, "risk": risk,
    }, model="round11-script")


def donchian_spec(window, stop=None, take=None):
    uk, lk = f"donchian_upper_{window}", f"donchian_lower_{window}"
    risk = {}
    if stop: risk["stop_loss_pct"] = stop
    if take: risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"Donchian {window} breakout",
        "symbol": "NSE:NIFTY", "timeframe": "5m",
        "indicators": [{"name": "donchian_upper", "params": {"window": window}},
                        {"name": "donchian_lower", "params": {"window": window}}],
        "entry": make_condition(field_operand("close"), ">", indicator_operand(uk)),
        "exit": make_condition(field_operand("close"), "<", indicator_operand(lk)),
        "position_sizing": {"max_allocation_pct": 0.95}, "risk": risk,
    }, model="round11-script")


def calc_trades_per_week(result, perf):
    total_bars = len(result.equity_curve)
    trading_days = total_bars / BARS_PER_DAY_5MIN
    weeks = trading_days / 5
    return perf.n_trades / weeks if weeks > 0 else 0


def run_all_backtests(dataset):
    results = []
    configs = []

    # EMA crosses targeting AvgBars ~87-116 (3-4 trades/week on 5-min)
    for fast, slow in [(50, 100), (50, 150), (50, 200), (60, 100), (60, 150),
                       (80, 120), (80, 150), (100, 150), (100, 200), (120, 200),
                       (100, 250), (150, 250), (150, 300)]:
        wb = slow + 10
        for sl, tp in [(0.02, 0.03), (0.02, 0.04), (0.02, 0.05), (0.03, 0.05)]:
            configs.append((
                f"EMA {fast}-{slow} cross 5m SL{int(sl*100)}TP{int(tp*100)}",
                ema_cross_spec(fast, slow, stop=sl, take=tp),
                True, wb, None,
            ))

    # EMA trend filters
    for w in [80, 100, 120, 150, 200, 250, 300]:
        wb = w + 10
        for sl, tp in [(0.02, 0.03), (0.02, 0.04), (0.02, 0.05)]:
            configs.append((
                f"EMA {w} trend 5m SL{int(sl*100)}TP{int(tp*100)}",
                ema_trend_spec(w, stop=sl, take=tp),
                True, wb, None,
            ))

    # Donchian breakouts
    for w in [50, 80, 100, 150, 200, 300]:
        wb = w + 10
        for sl, tp in [(0.02, 0.03), (0.02, 0.04), (0.02, 0.05), (0.03, 0.05)]:
            configs.append((
                f"Donchian {w} 5m SL{int(sl*100)}TP{int(tp*100)}",
                donchian_spec(w, stop=sl, take=tp),
                True, wb, None,
            ))

    for name, strat, is_spec, wb, risk_dict in configs:
        print(f"--- {name} (wb={wb}) ---", end=" ")
        if is_spec:
            perf, result, cfg = run_spec(dataset, strat, wb)
        else:
            perf, result, cfg = run_builtin(dataset, strat, wb,
                risk_dict.get("stop_loss_pct"), risk_dict.get("take_profit_pct"))
        if perf is not None and perf.n_trades > 0:
            ab = len(result.equity_curve) / max(perf.n_trades, 1)
            tw = calc_trades_per_week(result, perf)
            print(f"Return={perf.total_return*100:.1f}% Sharpe={perf.sharpe:.2f} "
                  f"Trades={perf.n_trades} AvgBars={ab:.1f} Trades/Wk={tw:.1f} PF={perf.profit_factor or 0:.2f}")
            results.append((name, perf, result))
        else:
            print("ERR/Trades=0")

    return results


def print_summary(results):
    print(f"\n{'='*120}")
    print("ROUND 11 SUMMARY: 5-Minute Data — Targeting 3-4 trades/week")
    print(f"{'='*120}")

    rows = []
    for name, perf, result in results:
        ab = len(result.equity_curve) / max(perf.n_trades, 1)
        tw = calc_trades_per_week(result, perf)
        rows.append([name, f"{perf.total_return*100:.1f}%", f"{perf.sharpe:.2f}",
                     f"{ab:.1f}", str(perf.n_trades), f"{tw:.1f}",
                     f"{perf.win_rate*100:.0f}%", f"{perf.profit_factor:.2f}" if perf.profit_factor and np.isfinite(perf.profit_factor) else "N/A",
                     f"{perf.max_drawdown*100:.1f}%"])
    headers = ["Strategy", "Return", "Sharpe", "AvgBars", "Trades", "Trades/Wk", "Win%", "PF", "MaxDD"]
    print(tabulate(rows, headers=headers, tablefmt="grid"))
    print(f"{'='*120}")

    profitable = [(n, p, r) for n, p, r in results if p.total_return > 0 and np.isfinite(p.sharpe)]

    target = [(n, p, r) for n, p, r in profitable if 2.5 <= calc_trades_per_week(r, p) <= 5.0]
    target.sort(key=lambda x: x[1].sharpe, reverse=True)

    print(f"\n--- Profitable in 3-4 trades/week range (2.5-5.0) ---")
    if target:
        for n, p, r in target[:25]:
            ab = len(r.equity_curve) / max(p.n_trades, 1)
            tw = calc_trades_per_week(r, p)
            print(f"  {n}: Trades/Wk={tw:.1f} Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
                  f"AvgBars={ab:.1f} PF={p.profit_factor or 0:.2f} MaxDD={p.max_drawdown*100:.1f}%")
    else:
        print("  None in exact range. Closest by Sharpe (profitable):")
        profitable.sort(key=lambda x: x[1].sharpe, reverse=True)
        for n, p, r in profitable[:20]:
            ab = len(r.equity_curve) / max(p.n_trades, 1)
            tw = calc_trades_per_week(r, p)
            print(f"  {n}: Trades/Wk={tw:.1f} Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
                  f"AvgBars={ab:.1f} PF={p.profit_factor or 0:.2f}")

    print(f"\n--- Most frequent profitable ---")
    freq_prof = sorted(profitable, key=lambda x: calc_trades_per_week(x[2], x[1]), reverse=True)
    for n, p, r in freq_prof[:10]:
        ab = len(r.equity_curve) / max(p.n_trades, 1)
        tw = calc_trades_per_week(r, p)
        print(f"  {n}: Trades/Wk={tw:.1f} Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
              f"AvgBars={ab:.1f} PF={p.profit_factor or 0:.2f}")

    print(f"\nTotal: {len(results)} valid, {len(profitable)} profitable, {len(target)} in 3-4/wk range")


if __name__ == "__main__":
    print("Loading 5-minute dataset...\n")
    ds = load_5min_dataset()
    print(f"\nRunning Round 11 backtests (5-min data)...\n")
    results = run_all_backtests(ds)
    print_summary(results)
