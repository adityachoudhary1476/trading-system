#!/usr/bin/env python3
"""Round 16: 30-min intra-day data — ultra-fast strategies targeting 3-4 trades/week.

On 30-min data (~13 bars/day, ~65 bars/week), 3-4 trades/week needs AvgBars ~18-22.
Tests ultra-fast EMA crosses, Donchian channels, and RSI reversals.
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
WEEKS_PER_YEAR = 252 / 5


def load_30min_dataset():
    df = pd.read_csv("nifty-daily-moves-and-gaps/data/NIFTY 50_5minute.csv", parse_dates=["date"])
    df.index = pd.DatetimeIndex(df["date"])
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    df = df.tz_localize("UTC")
    # Aggregate to 30-minute bars
    df = df.resample("30min").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()
    # Use last 5 years for cleaner signal
    cutoff = df.index.max() - pd.Timedelta(days=365 * 5)
    df = df[df.index >= cutoff]
    print(f"  30MIN: {len(df)} rows, {df.index[0]} to {df.index[-1]}")
    print(f"  Bars/week: ~{len(df) / (len(df)/260/WEEKS_PER_YEAR)/WEEKS_PER_YEAR:.1f}")  # placeholder
    return HistoricalDataset(symbol="NSE:NIFTY", timeframe="30min", data=df, contract_id="NSE:NIFTY-30min")


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


def run_builtin(dataset, strat, wb, stop=None, take=None):
    cfg = BacktestConfig(
        initial_capital=INITIAL_CAPITAL, slippage_pct=0.001,
        cost_model=IndiaTransactionCostModel(), cost_segment=CostSegment.EQUITY_FUTURE.value,
        warmup_bars=wb, risk=make_risk_config(stop, take),
    )
    result = run_backtest(dataset, strat, cfg)
    perf = compute_performance(result)
    return perf, result, cfg


def ema_cross_spec(name, fast, slow, stop=None, take=None):
    fk, sk = f"ema_{fast}", f"ema_{slow}"
    risk = {}
    if stop: risk["stop_loss_pct"] = stop
    if take: risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": name, "symbol": "NSE:NIFTY", "timeframe": "30m",
        "indicators": [
            {"name": "ema", "params": {"window": fast}},
            {"name": "ema", "params": {"window": slow}},
        ],
        "entry": make_condition(indicator_operand(fk), "crosses_above", indicator_operand(sk)),
        "exit": make_condition(indicator_operand(fk), "crosses_below", indicator_operand(sk)),
        "position_sizing": {"max_allocation_pct": 0.95}, "risk": risk,
    }, model="round16-script")


def ema_cross_trend_filter_spec(name, fast, slow, long_ema, stop=None, take=None):
    ema_k = f"ema_{long_ema}"
    risk = {}
    if stop: risk["stop_loss_pct"] = stop
    if take: risk["take_profit_pct"] = take
    inds = [
        {"name": "ema", "params": {"window": fast}},
        {"name": "ema", "params": {"window": slow}},
        {"name": "ema", "params": {"window": long_ema}},
    ]
    entry = logic("AND",
        make_condition(indicator_operand(f"ema_{fast}"), "crosses_above", indicator_operand(f"ema_{slow}")),
        make_condition(field_operand("close"), ">", indicator_operand(ema_k)),
    )
    exit_cond = make_condition(indicator_operand(f"ema_{fast}"), "crosses_below", indicator_operand(f"ema_{slow}"))
    return StrategySpec.from_model_json({
        "name": name, "symbol": "NSE:NIFTY", "timeframe": "30m",
        "indicators": inds, "entry": entry, "exit": exit_cond,
        "position_sizing": {"max_allocation_pct": 0.95}, "risk": risk,
    }, model="round16-script")


def donchian_spec(name, window, stop=None, take=None):
    uk, lk = f"donchian_upper_{window}", f"donchian_lower_{window}"
    risk = {}
    if stop: risk["stop_loss_pct"] = stop
    if take: risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": name, "symbol": "NSE:NIFTY", "timeframe": "30m",
        "indicators": [
            {"name": "donchian_upper", "params": {"window": window}},
            {"name": "donchian_lower", "params": {"window": window}},
        ],
        "entry": make_condition(field_operand("close"), ">", indicator_operand(uk)),
        "exit": make_condition(field_operand("close"), "<", indicator_operand(lk)),
        "position_sizing": {"max_allocation_pct": 0.95}, "risk": risk,
    }, model="round16-script")


def rsi_reversal_spec(name, rsi_window, lo, hi, stop=None, take=None):
    rk = f"rsi_{rsi_window}"
    risk = {}
    if stop: risk["stop_loss_pct"] = stop
    if take: risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": name, "symbol": "NSE:NIFTY", "timeframe": "30m",
        "indicators": [{"name": "rsi", "params": {"window": rsi_window}}],
        "entry": make_condition(indicator_operand(rk), "<", const_operand(lo)),
        "exit": make_condition(indicator_operand(rk), ">", const_operand(hi)),
        "position_sizing": {"max_allocation_pct": 0.95}, "risk": risk,
    }, model="round16-script")


def run_all(dataset):
    results = []
    configs = []

    # Bar stats
    total_bars = len(dataset.data)
    days = total_bars / 13  # ~13 30-min bars per trading day
    weeks = days / 5
    print(f"  Total bars: {total_bars}, ~{days:.0f} trading days, ~{weeks:.0f} weeks")
    print(f"  Target 3-4 trades/week = {weeks*3:.0f}-{weeks*4:.0f} trades total")

    # === Part 1: Ultra-fast EMA crosses ===
    for fast, slow in [(2, 3), (2, 4), (2, 5), (3, 4), (3, 5), (3, 6), (3, 8),
                        (4, 5), (4, 6), (4, 8), (5, 6), (5, 8), (5, 10), (6, 8), (6, 10),
                        (8, 10), (8, 13), (10, 13), (10, 20), (13, 20)]:
        wb = slow + 5
        for sl, tp in [(0.01, 0.015), (0.015, 0.02), (0.02, 0.03), (0.02, 0.04), (0.01, 0.03)]:
            configs.append((
                f"EMA {fast}-{slow} SL{int(sl*100)}TP{int(tp*100)}",
                ema_cross_spec(f"EMA {fast}-{slow}", fast, slow, stop=sl, take=tp),
                True, wb, None,
            ))

    # === Part 2: EMA crosses with trend filter (price > EMA(50)) ===
    for fast, slow in [(3, 5), (3, 8), (5, 10), (5, 13), (8, 13), (10, 20)]:
        wb = max(slow, 50) + 5
        for sl, tp in [(0.01, 0.02), (0.015, 0.025), (0.02, 0.03), (0.02, 0.04), (0.015, 0.035)]:
            configs.append((
                f"EMA {fast}-{slow} Trend50 SL{int(sl*100)}TP{int(tp*100)}",
                ema_cross_trend_filter_spec(f"EMA {fast}-{slow} Trend50", fast, slow, 50, stop=sl, take=tp),
                True, wb, None,
            ))

    # === Part 3: Donchian channels (very short windows) ===
    for w in [3, 5, 8, 10, 13, 20, 30, 50]:
        wb = w + 5
        for sl, tp in [(0.01, 0.015), (0.01, 0.02), (0.015, 0.025), (0.02, 0.03), (0.015, 0.03), (0.02, 0.04)]:
            configs.append((
                f"Donchian {w} SL{int(sl*100)}TP{int(tp*100)}",
                donchian_spec(f"Donchian {w}", w, stop=sl, take=tp),
                True, wb, None,
            ))

    # === Part 4: RSI reversal strategies ===
    for rw in [2, 3, 5, 7, 10, 14]:
        for lo, hi in [(20, 50), (25, 55), (30, 60), (30, 70), (25, 65), (20, 55)]:
            wb = rw + 5
            for sl, tp in [(0.01, 0.02), (0.015, 0.025), (0.02, 0.03)]:
                configs.append((
                    f"RSI {rw} lo{lo} hi{hi} SL{int(sl*100)}TP{int(tp*100)}",
                    rsi_reversal_spec(f"RSI {rw}", rw, lo, hi, stop=sl, take=tp),
                    True, wb, None,
                ))

    # === Part 5: Built-in strategies ===
    for fast, slow in [(2, 5), (3, 5), (3, 8), (5, 10), (5, 13)]:
        for sl, tp in [(0.01, 0.02), (0.015, 0.025), (0.02, 0.03), (0.02, 0.04)]:
            configs.append((
                f"EMATrend {fast}-{slow} SL{int(sl*100)}TP{int(tp*100)}",
                EMATrendStrategy(fast=fast, slow=slow, allow_short=False),
                False, slow + 5, {"stop_loss_pct": sl, "take_profit_pct": tp},
            ))

    for w in [3, 5, 8, 10, 13, 20]:
        for sl, tp in [(0.01, 0.02), (0.015, 0.025), (0.02, 0.03)]:
            configs.append((
                f"Breakout {w} SL{int(sl*100)}TP{int(tp*100)}",
                BreakoutStrategy(lookback=w),
                False, w + 5, {"stop_loss_pct": sl, "take_profit_pct": tp},
            ))

    for w in [3, 5, 8, 10, 13, 20]:
        for entry_thr, exit_thr in [(0.005, 0.0), (0.01, 0.0), (0.015, 0.0), (0.01, -0.005), (0.005, -0.005)]:
            configs.append((
                f"Momentum {w} entr{entry_thr*100:.1f} exit{exit_thr*100:.1f}",
                MomentumStrategy(window=w, entry_thr=entry_thr, exit_thr=exit_thr),
                False, w + 5, {"stop_loss_pct": 0.02, "take_profit_pct": 0.03},
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
            ab = len(result.equity_curve) / max(perf.n_trades, 1)
            days_actual = len(result.equity_curve) / 13
            weeks_actual = days_actual / 5
            tw = perf.n_trades / weeks_actual if weeks_actual > 0 else 0
            print(f"Return={perf.total_return*100:.1f}% Sharpe={perf.sharpe:.2f} "
                  f"Trades={perf.n_trades} AvgBars={ab:.1f} Trades/Wk={tw:.1f} "
                  f"Win%={perf.win_rate*100:.0f}% PF={perf.profit_factor or 0:.2f}")
            results.append((name, perf, result))
        else:
            print("ERR/Trades=0")
    return results


def print_summary(results):
    total_bars = sum(len(r.equity_curve) for _, _, r in results) / len(results) if results else 0
    bars_per_week = total_bars / (total_bars/13/5) if total_bars > 0 else 365

    print(f"\n{'='*140}")
    print("ROUND 16 SUMMARY: 30-min strategies targeting 3-4 trades/week")
    print(f"{'='*140}")
    rows = []
    for name, perf, result in results:
        ab = len(result.equity_curve) / max(perf.n_trades, 1)
        days = len(result.equity_curve) / 13
        weeks = days / 5
        tw = perf.n_trades / weeks if weeks > 0 else 0
        rows.append([name, f"{perf.total_return*100:.1f}%", f"{perf.sharpe:.2f}",
                     f"{ab:.1f}", str(perf.n_trades), f"{tw:.1f}",
                     f"{perf.win_rate*100:.0f}%", f"{perf.profit_factor:.2f}" if perf.profit_factor and np.isfinite(perf.profit_factor) else "N/A",
                     f"{perf.max_drawdown*100:.1f}%"])
    headers = ["Strategy", "Return", "Sharpe", "AvgBars", "Trades", "Trades/Wk", "Win%", "PF", "MaxDD"]
    print(tabulate(rows, headers=headers, tablefmt="grid"))
    print(f"{'='*140}")

    profitable = [(n, p, r) for n, p, r in results if p.total_return > 0 and np.isfinite(p.sharpe)]
    target = [(n, p, r) for n, p, r in profitable if 2.5 <= (p.n_trades / (len(r.equity_curve)/13/5) if len(r.equity_curve)/13/5 > 0 else 0) <= 5.0]
    target.sort(key=lambda x: x[1].sharpe, reverse=True)

    print(f"\n--- Profitable in 3-4 trades/week range (2.5-5.0) ---")
    if target:
        for n, p, r in target[:20]:
            ab = len(r.equity_curve) / max(p.n_trades, 1)
            tw = p.n_trades / (len(r.equity_curve)/13/5) if len(r.equity_curve)/13/5 > 0 else 0
            print(f"  {n}: Trades/Wk={tw:.1f} Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
                  f"AvgBars={ab:.1f} PF={p.profit_factor or 0:.2f} MaxDD={p.max_drawdown*100:.1f}%")
    else:
        print("  None in exact range.")

    print(f"\n--- Top 20 profitable by Sharpe ---")
    profitable.sort(key=lambda x: x[1].sharpe, reverse=True)
    for n, p, r in profitable[:20]:
        ab = len(r.equity_curve) / max(p.n_trades, 1)
        tw = p.n_trades / (len(r.equity_curve)/13/5) if len(r.equity_curve)/13/5 > 0 else 0
        print(f"  {n}: Trades/Wk={tw:.1f} Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
              f"AvgBars={ab:.1f} PF={p.profit_factor or 0:.2f}")

    print(f"\n--- Most frequent profitable (by Trades/Wk) ---")
    if profitable:
        freq = sorted(profitable, key=lambda x: x[1].n_trades / (len(x[2].equity_curve)/13/5) if len(x[2].equity_curve)/13/5 > 0 else 0, reverse=True)
        for n, p, r in freq[:15]:
            ab = len(r.equity_curve) / max(p.n_trades, 1)
            tw = p.n_trades / (len(r.equity_curve)/13/5) if len(r.equity_curve)/13/5 > 0 else 0
            print(f"  {n}: Trades/Wk={tw:.1f} Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
                  f"AvgBars={ab:.1f} PF={p.profit_factor or 0:.2f}")
    else:
        print("  None profitable on this timeframe.")

    print(f"\nTotal: {len(results)} valid, {len(profitable)} profitable, {len(target)} in 3-4/wk range")


if __name__ == "__main__":
    print("Loading 30-min dataset (5 years)...\n")
    ds = load_30min_dataset()
    print(f"\nRunning Round 16 backtests...\n")
    results = run_all(ds)
    print_summary(results)
