#!/usr/bin/env python3
"""Round 15: Daily filtered strategies targeting 3-4 trades/week.

Focus: faster EMA crosses (2-5, 3-5, 3-8, 5-8) with trend/volatility filters,
plus mean-reversion and momentum strategies that may naturally trade 3-4x/week.
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
from trading_system.research.strategies import EMATrendStrategy, MomentumStrategy, BreakoutStrategy
from tabulate import tabulate

warnings.filterwarnings("ignore", category=FutureWarning)

INITIAL_CAPITAL = 100_000.0
TRADING_DAYS_PER_YEAR = 252
WEEKS_PER_YEAR = TRADING_DAYS_PER_YEAR / 5


def load_daily_dataset():
    df = pd.read_csv("nifty-daily-moves-and-gaps/data/nifty50.csv", parse_dates=["Date"])
    df.index = pd.DatetimeIndex(df["Date"])
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    df = df.tz_localize("UTC")
    print(f"  DAILY: {len(df)} rows, {df.index[0].date()} to {df.index[-1].date()}")
    return HistoricalDataset(symbol="NSE:NIFTY", timeframe="1d", data=df, contract_id="NSE:NIFTY")


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
        "name": name,
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [
            {"name": "ema", "params": {"window": fast}},
            {"name": "ema", "params": {"window": slow}},
        ],
        "entry": make_condition(indicator_operand(fk), "crosses_above", indicator_operand(sk)),
        "exit": make_condition(indicator_operand(fk), "crosses_below", indicator_operand(sk)),
        "position_sizing": {"max_allocation_pct": 0.95}, "risk": risk,
    }, model="round15-script")


def ema_cross_filtered_spec(name, fast, slow, inds, entry, exit, stop=None, take=None):
    risk = {}
    if stop: risk["stop_loss_pct"] = stop
    if take: risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": name,
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": inds,
        "entry": entry,
        "exit": exit,
        "position_sizing": {"max_allocation_pct": 0.95}, "risk": risk,
    }, model="round15-script")


def ema_cross_trend_filter(fast, slow, long_ema):
    ema_k = f"ema_{long_ema}"
    return [
        {"name": "ema", "params": {"window": fast}},
        {"name": "ema", "params": {"window": slow}},
        {"name": "ema", "params": {"window": long_ema}},
    ], logic("AND",
        make_condition(indicator_operand(f"ema_{fast}"), "crosses_above", indicator_operand(f"ema_{slow}")),
        make_condition(field_operand("close"), ">", indicator_operand(ema_k)),
    ), make_condition(indicator_operand(f"ema_{fast}"), "crosses_below", indicator_operand(f"ema_{slow}"))


def ema_cross_vol_filter(fast, slow, atr_short, atr_long):
    return [
        {"name": "ema", "params": {"window": fast}},
        {"name": "ema", "params": {"window": slow}},
        {"name": "atr", "params": {"window": atr_short}},
        {"name": "atr", "params": {"window": atr_long}},
    ], logic("AND",
        make_condition(indicator_operand(f"ema_{fast}"), "crosses_above", indicator_operand(f"ema_{slow}")),
        make_condition(indicator_operand(f"atr_{atr_short}"), ">", indicator_operand(f"atr_{atr_long}")),
    ), make_condition(indicator_operand(f"ema_{fast}"), "crosses_below", indicator_operand(f"ema_{slow}"))


def donchian_filtered_spec(name, window, stop=None, take=None):
    uk, lk = f"donchian_upper_{window}", f"donchian_lower_{window}"
    risk = {}
    if stop: risk["stop_loss_pct"] = stop
    if take: risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": name, "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [
            {"name": "donchian_upper", "params": {"window": window}},
            {"name": "donchian_lower", "params": {"window": window}},
        ],
        "entry": make_condition(field_operand("close"), ">", indicator_operand(uk)),
        "exit": make_condition(field_operand("close"), "<", indicator_operand(lk)),
        "position_sizing": {"max_allocation_pct": 0.95}, "risk": risk,
    }, model="round15-script")


def run_all(dataset):
    results = []
    configs = []

    # === Part 1: Fast EMA crosses (baseline + filters) ===
    # Target: 3-4 trades/week = ~150-200 trades over 18 years
    for fast, slow in [(2, 3), (2, 4), (2, 5), (3, 4), (3, 5), (3, 6), (3, 8), (4, 6), (4, 8), (5, 8), (5, 10), (5, 13)]:
        wb = slow + 5
        for sl, tp in [(0.03, 0.05), (0.03, 0.06), (0.04, 0.06), (0.04, 0.08), (0.05, 0.08)]:
            configs.append((
                f"EMA {fast}-{slow} SL{int(sl*100)}TP{int(tp*100)}",
                ema_cross_spec(f"EMA {fast}-{slow}", fast, slow, stop=sl, take=tp),
                True, wb, None,
            ))

    # === Part 2: EMA crosses with trend filter (price > EMA(200)) ===
    for fast, slow in [(2, 3), (2, 5), (3, 5), (3, 8), (5, 10), (5, 13)]:
        wb = max(slow, 200) + 5
        for sl, tp in [(0.03, 0.05), (0.03, 0.07), (0.04, 0.06), (0.04, 0.08)]:
            inds, entry, exit = ema_cross_trend_filter(fast, slow, 200)
            configs.append((
                f"EMA {fast}-{slow} Trend200 SL{int(sl*100)}TP{int(tp*100)}",
                ema_cross_filtered_spec(f"EMA {fast}-{slow} Trend200", fast, slow, inds, entry, exit, stop=sl, take=tp),
                True, wb, None,
            ))

    # === Part 3: EMA crosses with vol filter (ATR 14 > ATR 55) ===
    for fast, slow in [(2, 3), (2, 5), (3, 5), (3, 8), (5, 10), (5, 13)]:
        wb = max(slow, 55) + 5
        for sl, tp in [(0.03, 0.05), (0.03, 0.07), (0.04, 0.06)]:
            inds, entry, exit = ema_cross_vol_filter(fast, slow, 14, 55)
            configs.append((
                f"EMA {fast}-{slow} VolATR SL{int(sl*100)}TP{int(tp*100)}",
                ema_cross_filtered_spec(f"EMA {fast}-{slow} VolATR", fast, slow, inds, entry, exit, stop=sl, take=tp),
                True, wb, None,
            ))

    # === Part 4: EMA crosses with trend + vol filter ===
    for fast, slow in [(2, 5), (3, 5), (3, 8), (5, 10), (5, 13)]:
        wb = max(slow, 200, 55) + 5
        for sl, tp in [(0.03, 0.05), (0.03, 0.07), (0.04, 0.06)]:
            ema_k = f"ema_200"
            inds = [
                {"name": "ema", "params": {"window": fast}},
                {"name": "ema", "params": {"window": slow}},
                {"name": "ema", "params": {"window": 200}},
                {"name": "atr", "params": {"window": 14}},
                {"name": "atr", "params": {"window": 55}},
            ]
            entry = logic("AND",
                make_condition(indicator_operand(f"ema_{fast}"), "crosses_above", indicator_operand(f"ema_{slow}")),
                make_condition(field_operand("close"), ">", indicator_operand(ema_k)),
                make_condition(indicator_operand("atr_14"), ">", indicator_operand("atr_55")),
            )
            exit = make_condition(indicator_operand(f"ema_{fast}"), "crosses_below", indicator_operand(f"ema_{slow}"))
            configs.append((
                f"EMA {fast}-{slow} TrendVol SL{int(sl*100)}TP{int(tp*100)}",
                ema_cross_filtered_spec(f"EMA {fast}-{slow} TrendVol", fast, slow, inds, entry, exit, stop=sl, take=tp),
                True, wb, None,
            ))

    # === Part 5: Donchian breakouts (various windows) ===
    for w in [3, 5, 8, 10, 13, 20, 30, 50]:
        wb = w + 5
        for sl, tp in [(0.03, 0.05), (0.03, 0.07), (0.04, 0.06), (0.04, 0.08), (0.05, 0.08)]:
            configs.append((
                f"Donchian {w} SL{int(sl*100)}TP{int(tp*100)}",
                donchian_filtered_spec(f"Donchian {w}", w, stop=sl, take=tp),
                True, wb, None,
            ))

    # === Part 6: Built-in strategies ===
    for w in [5, 8, 10, 13, 20, 30]:
        for sl, tp in [(0.03, 0.05), (0.03, 0.07), (0.04, 0.06)]:
            configs.append((
                f"EMATrend {w} SL{int(sl*100)}TP{int(tp*100)}",
                EMATrendStrategy(fast=5, slow=w),
                False, w + 5, {"stop_loss_pct": sl, "take_profit_pct": tp},
            ))

    for fast, slow, signal in [(12, 26, 9), (8, 17, 9), (5, 13, 5), (3, 10, 3)]:
        for sl, tp in [(0.03, 0.05), (0.03, 0.07), (0.04, 0.06)]:
            configs.append((
                f"EMATrend {fast}-{slow} SL{int(sl*100)}TP{int(tp*100)}",
                EMATrendStrategy(fast=fast, slow=slow),
                False, slow + 5, {"stop_loss_pct": sl, "take_profit_pct": tp},
            ))

    for w in [5, 8, 10, 13, 20]:
        for entry_thr, exit_thr in [(0.02, 0.0), (0.03, 0.0), (0.05, 0.0), (0.02, -0.01), (0.03, -0.01)]:
            configs.append((
                f"Momentum {w} entr{entry_thr*100:.0f} exit{exit_thr*100:.0f}",
                MomentumStrategy(window=w, entry_thr=entry_thr, exit_thr=exit_thr),
                False, w + 5, {"stop_loss_pct": 0.04, "take_profit_pct": 0.06},
            ))

    for w in [5, 8, 10, 13, 20]:
        for sl, tp in [(0.03, 0.05), (0.04, 0.06)]:
            configs.append((
                f"Breakout {w} SL{int(sl*100)}TP{int(tp*100)}",
                BreakoutStrategy(lookback=w),
                False, w + 5, {"stop_loss_pct": sl, "take_profit_pct": tp},
            ))

    # === Part 7: EMATrendStrategy with very short windows (position reversal) ===
    for fast, slow in [(2, 3), (2, 4), (2, 5), (3, 4), (3, 5)]:
        for sl, tp in [(0.03, 0.05), (0.03, 0.06), (0.04, 0.06)]:
            configs.append((
                f"EMATrendRev {fast}-{slow} SL{int(sl*100)}TP{int(tp*100)}",
                EMATrendStrategy(fast=fast, slow=slow, allow_short=False),
                False, slow + 5, {"stop_loss_pct": sl, "take_profit_pct": tp},
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
            years = len(result.equity_curve) / 252
            tw = perf.n_trades / years / WEEKS_PER_YEAR if years > 0 else 0
            print(f"Return={perf.total_return*100:.1f}% Sharpe={perf.sharpe:.2f} "
                  f"Trades={perf.n_trades} AvgBars={ab:.1f} Trades/Wk={tw:.1f} "
                  f"Win%={perf.win_rate*100:.0f}% PF={perf.profit_factor or 0:.2f}")
            results.append((name, perf, result))
        else:
            print("ERR/Trades=0")
    return results


def print_summary(results):
    print(f"\n{'='*140}")
    print("ROUND 15 SUMMARY: Daily strategies targeting 3-4 trades/week")
    print(f"{'='*140}")
    rows = []
    for name, perf, result in results:
        ab = len(result.equity_curve) / max(perf.n_trades, 1)
        years = len(result.equity_curve) / 252
        tw = perf.n_trades / years / WEEKS_PER_YEAR if years > 0 else 0
        rows.append([name, f"{perf.total_return*100:.1f}%", f"{perf.sharpe:.2f}",
                     f"{ab:.1f}", str(perf.n_trades), f"{tw:.1f}",
                     f"{perf.win_rate*100:.0f}%", f"{perf.profit_factor:.2f}" if perf.profit_factor and np.isfinite(perf.profit_factor) else "N/A",
                     f"{perf.max_drawdown*100:.1f}%"])
    headers = ["Strategy", "Return", "Sharpe", "AvgBars", "Trades", "Trades/Wk", "Win%", "PF", "MaxDD"]
    print(tabulate(rows, headers=headers, tablefmt="grid"))
    print(f"{'='*140}")

    profitable = [(n, p, r) for n, p, r in results if p.total_return > 0 and np.isfinite(p.sharpe)]
    target = [(n, p, r) for n, p, r in profitable if 2.5 <= (p.n_trades / (len(r.equity_curve)/252) / WEEKS_PER_YEAR if len(r.equity_curve)/252 > 0 else 0) <= 5.0]
    target.sort(key=lambda x: x[1].sharpe, reverse=True)

    print(f"\n--- Profitable in 3-4 trades/week range (2.5-5.0) ---")
    if target:
        for n, p, r in target[:20]:
            ab = len(r.equity_curve) / max(p.n_trades, 1)
            tw = p.n_trades / (len(r.equity_curve)/252) / WEEKS_PER_YEAR if len(r.equity_curve)/252 > 0 else 0
            print(f"  {n}: Trades/Wk={tw:.1f} Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
                  f"AvgBars={ab:.1f} PF={p.profit_factor or 0:.2f} MaxDD={p.max_drawdown*100:.1f}%")
    else:
        print("  None in exact range.")

    print(f"\n--- Top 20 profitable by Sharpe ---")
    profitable.sort(key=lambda x: x[1].sharpe, reverse=True)
    for n, p, r in profitable[:20]:
        ab = len(r.equity_curve) / max(p.n_trades, 1)
        tw = p.n_trades / (len(r.equity_curve)/252) / WEEKS_PER_YEAR if len(r.equity_curve)/252 > 0 else 0
        print(f"  {n}: Trades/Wk={tw:.1f} Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
              f"AvgBars={ab:.1f} PF={p.profit_factor or 0:.2f}")

    print(f"\n--- All profitable sorted by Trades/Wk ---")
    freq = sorted(profitable, key=lambda x: x[1].n_trades / (len(x[2].equity_curve)/252) / WEEKS_PER_YEAR if len(x[2].equity_curve)/252 > 0 else 0, reverse=True)
    for n, p, r in freq[:15]:
        ab = len(r.equity_curve) / max(p.n_trades, 1)
        tw = p.n_trades / (len(r.equity_curve)/252) / WEEKS_PER_YEAR if len(r.equity_curve)/252 > 0 else 0
        print(f"  {n}: Trades/Wk={tw:.1f} Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
              f"AvgBars={ab:.1f} PF={p.profit_factor or 0:.2f}")

    print(f"\nTotal: {len(results)} valid, {len(profitable)} profitable, {len(target)} in 3-4/wk range")


if __name__ == "__main__":
    print("Loading daily dataset...\n")
    ds = load_daily_dataset()
    print(f"\nRunning Round 15 backtests...\n")
    results = run_all(ds)
    print_summary(results)
