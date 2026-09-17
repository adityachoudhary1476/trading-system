#!/usr/bin/env python3
"""Round 13: Filtered 5-min backtest — EMA crosses + volatility/trend/momentum filters.

Hypothesis: EMA crosses on 5-min NIFTY are too noisy. Adding filters (trend confirmation,
volatility filter, momentum filter) should reduce false breakouts and make faster
EMA crosses profitable while achieving higher trade frequency.
"""
from __future__ import annotations

import warnings
import numpy as np
import pandas as pd
from trading_system.research.backtester import BacktestConfig, run_backtest
from trading_system.research.costs import IndiaTransactionCostModel, Segment as CostSegment
from trading_system.research.dataset import HistoricalDataset
from trading_system.research.performance import compute_performance
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


def load_5min_dataset():
    df = pd.read_csv("nifty-daily-moves-and-gaps/data/NIFTY 50_5minute.csv", parse_dates=["date"])
    df.index = pd.DatetimeIndex(df["date"])
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    df = df.tz_localize("UTC")
    # Use last 2 years only
    cutoff = df.index.max() - pd.Timedelta(days=730)
    df = df[df.index >= cutoff]
    print(f"  5MIN: {len(df)} rows, {df.index[0]} to {df.index[-1]}")
    print(f"  Bars/day: ~{BARS_PER_DAY_5MIN}, Weeks: ~{len(df)/(BARS_PER_DAY_5MIN*5):.0f}")
    return HistoricalDataset(symbol="NSE:NIFTY", timeframe="5m", data=df, contract_id="NSE:NIFTY-5min")


def make_risk_config(stop_loss_pct=None, take_profit_pct=None, max_alloc=0.95):
    return RiskConfig(max_allocation_pct=max_alloc, allow_short=False,
                       stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct)


def run_spec(dataset, spec, wb):
    base = BacktestConfig(initial_capital=INITIAL_CAPITAL, slippage_pct=0.001,
                          cost_model=IndiaTransactionCostModel(), cost_segment=CostSegment.EQUITY_FUTURE.value,
                          warmup_bars=wb)
    cfg = merged_backtest_config(spec, base)
    strategy = build_strategy(spec)
    result = run_backtest(dataset, strategy, cfg)
    perf = compute_performance(result)
    return perf, result, cfg


def ema_cross_spec(name, fast, slow, indicators, entry_cond, exit_cond, stop=None, take=None):
    risk = {}
    if stop: risk["stop_loss_pct"] = stop
    if take: risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": name,
        "symbol": "NSE:NIFTY", "timeframe": "5m",
        "indicators": indicators,
        "entry": entry_cond,
        "exit": exit_cond,
        "position_sizing": {"max_allocation_pct": 0.95}, "risk": risk,
    }, model="round13-filtered")


def calc_trades_per_week(result, perf):
    total_bars = len(result.equity_curve)
    weeks = total_bars / (BARS_PER_DAY_5MIN * 5)
    return perf.n_trades / weeks if weeks > 0 else 0


def ema_cross_base(fast, slow):
    fk, sk = f"ema_{fast}", f"ema_{slow}"
    return [(
        f"EMA {fast}-{slow} 5m",
        [
            {"name": "ema", "params": {"window": fast}},
            {"name": "ema", "params": {"window": slow}},
        ],
        make_condition(indicator_operand(fk), "crosses_above", indicator_operand(sk)),
        make_condition(indicator_operand(fk), "crosses_below", indicator_operand(sk)),
    )]


def ema_cross_with_trend_filter(fast, slow, long_ema):
    """EMA cross + long-term trend filter: only take longs when price > EMA(long)."""
    ema_k = f"ema_{long_ema}"
    return [
        {"name": "ema", "params": {"window": fast}},
        {"name": "ema", "params": {"window": slow}},
        {"name": "ema", "params": {"window": long_ema}},
    ], logic("AND",
        make_condition(indicator_operand(f"ema_{fast}"), "crosses_above", indicator_operand(f"ema_{slow}")),
        make_condition(field_operand("close"), ">", indicator_operand(ema_k)),
    ), make_condition(indicator_operand(f"ema_{fast}"), "crosses_below", indicator_operand(f"ema_{slow}"))


def ema_cross_with_vol_filter(fast, slow, atr_short, atr_long):
    """EMA cross + volatility filter: only trade when ATR(short) > ATR(long)."""
    return [
        {"name": "ema", "params": {"window": fast}},
        {"name": "ema", "params": {"window": slow}},
        {"name": "atr", "params": {"window": atr_short}},
        {"name": "atr", "params": {"window": atr_long}},
    ], logic("AND",
        make_condition(indicator_operand(f"ema_{fast}"), "crosses_above", indicator_operand(f"ema_{slow}")),
        make_condition(indicator_operand(f"atr_{atr_short}"), ">", indicator_operand(f"atr_{atr_long}")),
    ), make_condition(indicator_operand(f"ema_{fast}"), "crosses_below", indicator_operand(f"ema_{slow}"))


def ema_cross_with_momentum_filter(fast, slow, rsi_window, rsi_thresh):
    """EMA cross + momentum filter: only take longs when RSI > threshold."""
    return [
        {"name": "ema", "params": {"window": fast}},
        {"name": "ema", "params": {"window": slow}},
        {"name": "rsi", "params": {"window": rsi_window}},
    ], logic("AND",
        make_condition(indicator_operand(f"ema_{fast}"), "crosses_above", indicator_operand(f"ema_{slow}")),
        make_condition(indicator_operand(f"rsi_{rsi_window}"), ">", const_operand(rsi_thresh)),
    ), make_condition(indicator_operand(f"ema_{fast}"), "crosses_below", indicator_operand(f"ema_{slow}"))


def ema_cross_with_all_filters(fast, slow, long_ema, atr_short, atr_long, rsi_window, rsi_thresh):
    """EMA cross + trend + volatility + momentum filter."""
    ema_k = f"ema_{long_ema}"
    return [
        {"name": "ema", "params": {"window": fast}},
        {"name": "ema", "params": {"window": slow}},
        {"name": "ema", "params": {"window": long_ema}},
        {"name": "atr", "params": {"window": atr_short}},
        {"name": "atr", "params": {"window": atr_long}},
        {"name": "rsi", "params": {"window": rsi_window}},
    ], logic("AND",
        make_condition(indicator_operand(f"ema_{fast}"), "crosses_above", indicator_operand(f"ema_{slow}")),
        make_condition(field_operand("close"), ">", indicator_operand(ema_k)),
        make_condition(indicator_operand(f"atr_{atr_short}"), ">", indicator_operand(f"atr_{atr_long}")),
        make_condition(indicator_operand(f"rsi_{rsi_window}"), ">", const_operand(rsi_thresh)),
    ), make_condition(indicator_operand(f"ema_{fast}"), "crosses_below", indicator_operand(f"ema_{slow}"))


def run_all(dataset):
    results = []
    configs = []

    # --- Baseline: pure EMA crosses (faster ones that were too noisy) ---
    for fast, slow in [(30, 60), (50, 100), (80, 120), (100, 150)]:
        wb = slow + 20
        for sl, tp in [(0.02, 0.04), (0.02, 0.05), (0.03, 0.05)]:
            fk, sk = f"ema_{fast}", f"ema_{slow}"
            inds = [{"name": "ema", "params": {"window": fast}},
                    {"name": "ema", "params": {"window": slow}}]
            entry = make_condition(indicator_operand(fk), "crosses_above", indicator_operand(sk))
            exit = make_condition(indicator_operand(fk), "crosses_below", indicator_operand(sk))
            configs.append((
                f"EMA {fast}-{slow} 5m SL{int(sl*100)}TP{int(tp*100)} (baseline)",
                ema_cross_spec(
                    f"EMA {fast}-{slow} 5m SL{int(sl*100)}TP{int(tp*100)}",
                    fast, slow,
                    inds, entry, exit, stop=sl, take=tp,
                ),
                True, wb,
            ))

    # --- Trend filter: EMA cross + price > EMA(200) ---
    for fast, slow in [(30, 60), (50, 100), (80, 120), (100, 150)]:
        wb = max(slow, 200) + 20
        for sl, tp in [(0.02, 0.04), (0.02, 0.05), (0.03, 0.05)]:
            inds, entry, exit = ema_cross_with_trend_filter(fast, slow, 200)
            configs.append((
                f"EMA {fast}-{slow} Trend200 SL{int(sl*100)}TP{int(tp*100)}",
                ema_cross_spec(
                    f"EMA {fast}-{slow} Trend200",
                    fast, slow,
                    inds, entry, exit, stop=sl, take=tp,
                ),
                True, wb,
            ))

    # --- Volatility filter: EMA cross + ATR(14) > ATR(55) ---
    for fast, slow in [(30, 60), (50, 100), (80, 120), (100, 150)]:
        wb = max(slow, 55) + 20
        for sl, tp in [(0.02, 0.04), (0.02, 0.05), (0.03, 0.05)]:
            inds, entry, exit = ema_cross_with_vol_filter(fast, slow, 14, 55)
            configs.append((
                f"EMA {fast}-{slow} VolATR SL{int(sl*100)}TP{int(tp*100)}",
                ema_cross_spec(
                    f"EMA {fast}-{slow} VolATR",
                    fast, slow,
                    inds, entry, exit, stop=sl, take=tp,
                ),
                True, wb,
            ))

    # --- Momentum filter: EMA cross + RSI(14) > 40 ---
    for fast, slow in [(30, 60), (50, 100), (80, 120), (100, 150)]:
        wb = max(slow, 14) + 20
        for sl, tp in [(0.02, 0.04), (0.02, 0.05), (0.03, 0.05)]:
            inds, entry, exit = ema_cross_with_momentum_filter(fast, slow, 14, 40)
            configs.append((
                f"EMA {fast}-{slow} MomRSI40 SL{int(sl*100)}TP{int(tp*100)}",
                ema_cross_spec(
                    f"EMA {fast}-{slow} MomRSI40",
                    fast, slow,
                    inds, entry, exit, stop=sl, take=tp,
                ),
                True, wb,
            ))

    # --- All filters combined ---
    for fast, slow in [(30, 60), (50, 100), (80, 120)]:
        wb = max(slow, 200, 55, 14) + 20
        for sl, tp in [(0.02, 0.04), (0.02, 0.05), (0.03, 0.05)]:
            inds, entry, exit = ema_cross_with_all_filters(fast, slow, 200, 14, 55, 14, 40)
            configs.append((
                f"EMA {fast}-{slow} AllFilt SL{int(sl*100)}TP{int(tp*100)}",
                ema_cross_spec(
                    f"EMA {fast}-{slow} AllFilt",
                    fast, slow,
                    inds, entry, exit, stop=sl, take=tp,
                ),
                True, wb,
            ))

    for name, strat, is_spec, wb in configs:
        print(f"--- {name} (wb={wb}) ---", end=" ")
        perf, result, cfg = run_spec(dataset, strat, wb)
        if perf is not None and perf.n_trades > 0:
            ab = len(result.equity_curve) / max(perf.n_trades, 1)
            tw = calc_trades_per_week(result, perf)
            print(f"Return={perf.total_return*100:.1f}% Sharpe={perf.sharpe:.2f} "
                  f"Trades={perf.n_trades} AvgBars={ab:.1f} Trades/Wk={tw:.1f} "
                  f"Win%={perf.win_rate*100:.0f}% PF={perf.profit_factor or 0:.2f}")
            results.append((name, perf, result))
        else:
            print("ERR/Trades=0")
    return results


def print_summary(results):
    print(f"\n{'='*130}")
    print("ROUND 13 SUMMARY: Filtered 5-Minute EMA Crosses")
    print(f"{'='*130}")
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
    print(f"{'='*130}")

    profitable = [(n, p, r) for n, p, r in results if p.total_return > 0 and np.isfinite(p.sharpe)]
    target = [(n, p, r) for n, p, r in profitable if 2.5 <= calc_trades_per_week(r, p) <= 5.0]
    target.sort(key=lambda x: x[1].sharpe, reverse=True)

    print(f"\n--- Profitable in 3-4 trades/week range (2.5-5.0) ---")
    if target:
        for n, p, r in target[:20]:
            ab = len(r.equity_curve) / max(p.n_trades, 1)
            tw = calc_trades_per_week(r, p)
            print(f"  {n}: Trades/Wk={tw:.1f} Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
                  f"AvgBars={ab:.1f} PF={p.profit_factor or 0:.2f} MaxDD={p.max_drawdown*100:.1f}%")
    else:
        print("  None in exact range.")

    print(f"\n--- Top 15 by Sharpe (profitable) ---")
    profitable.sort(key=lambda x: x[1].sharpe, reverse=True)
    for n, p, r in profitable[:15]:
        ab = len(r.equity_curve) / max(p.n_trades, 1)
        tw = calc_trades_per_week(r, p)
        print(f"  {n}: Trades/Wk={tw:.1f} Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
              f"AvgBars={ab:.1f} PF={p.profit_factor or 0:.2f}")

    print(f"\n--- Most frequent profitable (by Trades/Wk) ---")
    freq = sorted(profitable, key=lambda x: calc_trades_per_week(x[2], x[1]), reverse=True)
    for n, p, r in freq[:10]:
        ab = len(r.equity_curve) / max(p.n_trades, 1)
        tw = calc_trades_per_week(r, p)
        print(f"  {n}: Trades/Wk={tw:.1f} Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
              f"AvgBars={ab:.1f} PF={p.profit_factor or 0:.2f}")

    print(f"\nTotal: {len(results)} valid, {len(profitable)} profitable, {len(target)} in 3-4/wk range")


if __name__ == "__main__":
    print("Loading 5-min dataset (last 2 years)...\n")
    ds = load_5min_dataset()
    print(f"\nRunning Round 13 backtests (filtered)...\n")
    results = run_all(ds)
    print_summary(results)
