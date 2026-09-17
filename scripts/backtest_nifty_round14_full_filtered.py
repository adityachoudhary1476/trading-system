#!/usr/bin/env python3
"""Round 14: Filtered backtests on FULL dataset (2015-2026 for 5-min, 2007-2026 for daily).

Testing EMA crosses + volatility/trend/momentum filters on longer history where
the strategies may have performed better. Also tries 30-min aggregation.
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
WEEKS_PER_YEAR = 252 / 5


def load_5min_dataset_full():
    df = pd.read_csv("nifty-daily-moves-and-gaps/data/NIFTY 50_5minute.csv", parse_dates=["date"])
    df.index = pd.DatetimeIndex(df["date"])
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    df = df.tz_localize("UTC")
    print(f"  5MIN FULL: {len(df)} rows, {df.index[0]} to {df.index[-1]}")
    return HistoricalDataset(symbol="NSE:NIFTY", timeframe="5m", data=df, contract_id="NSE:NIFTY-5min")


def load_daily_dataset():
    df = pd.read_csv("nifty-daily-moves-and-gaps/data/nifty50.csv", parse_dates=["Date"])
    df.index = pd.DatetimeIndex(df["Date"])
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    df = df.tz_localize("UTC")
    print(f"  DAILY FULL: {len(df)} rows, {df.index[0].date()} to {df.index[-1].date()}")
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


def ema_cross_filtered_spec(name, fast, slow, indicators, entry_cond, exit_cond,
                            stop=None, take=None, timeframe="5m"):
    risk = {}
    if stop: risk["stop_loss_pct"] = stop
    if take: risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": name,
        "symbol": "NSE:NIFTY", "timeframe": timeframe,
        "indicators": indicators,
        "entry": entry_cond,
        "exit": exit_cond,
        "position_sizing": {"max_allocation_pct": 0.95}, "risk": risk,
    }, model="round14-script")


def ema_cross_with_trend_filter(fast, slow, long_ema, timeframe="5m"):
    ema_k = f"ema_{long_ema}"
    return [
        {"name": "ema", "params": {"window": fast}},
        {"name": "ema", "params": {"window": slow}},
        {"name": "ema", "params": {"window": long_ema}},
    ], logic("AND",
        make_condition(indicator_operand(f"ema_{fast}"), "crosses_above", indicator_operand(f"ema_{slow}")),
        make_condition(field_operand("close"), ">", indicator_operand(ema_k)),
    ), make_condition(indicator_operand(f"ema_{fast}"), "crosses_below", indicator_operand(f"ema_{slow}"))


def ema_cross_with_vol_filter(fast, slow, atr_short, atr_long, timeframe="5m"):
    return [
        {"name": "ema", "params": {"window": fast}},
        {"name": "ema", "params": {"window": slow}},
        {"name": "atr", "params": {"window": atr_short}},
        {"name": "atr", "params": {"window": atr_long}},
    ], logic("AND",
        make_condition(indicator_operand(f"ema_{fast}"), "crosses_above", indicator_operand(f"ema_{slow}")),
        make_condition(indicator_operand(f"atr_{atr_short}"), ">", indicator_operand(f"atr_{atr_long}")),
    ), make_condition(indicator_operand(f"ema_{fast}"), "crosses_below", indicator_operand(f"ema_{slow}"))


def ema_cross_with_all_filters(fast, slow, long_ema, atr_short, atr_long, rsi_window, rsi_thresh, timeframe="5m"):
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


def ema_cross_with_momentum_conf(fast, slow, mom_window, mom_thresh, timeframe="5m"):
    mk = f"momentum_{mom_window}"
    return [
        {"name": "ema", "params": {"window": fast}},
        {"name": "ema", "params": {"window": slow}},
        {"name": "momentum", "params": {"window": mom_window}},
    ], logic("AND",
        make_condition(indicator_operand(f"ema_{fast}"), "crosses_above", indicator_operand(f"ema_{slow}")),
        make_condition(indicator_operand(mk), ">", const_operand(mom_thresh)),
    ), make_condition(indicator_operand(f"ema_{fast}"), "crosses_below", indicator_operand(f"ema_{slow}"))


def run_all_5min_full(dataset):
    results = []
    configs = []

    # 5-min EMA crosses with trend filter (longer-term)
    for fast, slow in [(30, 60), (50, 100), (80, 120), (100, 150)]:
        wb = max(slow, 200) + 20
        for sl, tp in [(0.02, 0.04), (0.02, 0.05), (0.03, 0.05)]:
            inds, entry, exit = ema_cross_with_trend_filter(fast, slow, 200, "5m")
            configs.append((
                f"5m EMA {fast}-{slow} Trend200 SL{int(sl*100)}TP{int(tp*100)}",
                ema_cross_filtered_spec(
                    f"EMA {fast}-{slow} 5m Trend200", fast, slow,
                    inds, entry, exit, stop=sl, take=tp, timeframe="5m",
                ),
                True, wb,
            ))

    # 5-min EMA crosses with volatility filter
    for fast, slow in [(30, 60), (50, 100), (80, 120), (100, 150)]:
        wb = max(slow, 55) + 20
        for sl, tp in [(0.02, 0.04), (0.02, 0.05), (0.03, 0.05)]:
            inds, entry, exit = ema_cross_with_vol_filter(fast, slow, 14, 55, "5m")
            configs.append((
                f"5m EMA {fast}-{slow} VolATR SL{int(sl*100)}TP{int(tp*100)}",
                ema_cross_filtered_spec(
                    f"EMA {fast}-{slow} 5m VolATR", fast, slow,
                    inds, entry, exit, stop=sl, take=tp, timeframe="5m",
                ),
                True, wb,
            ))

    # 5-min EMA crosses with all filters
    for fast, slow in [(30, 60), (50, 100), (80, 120)]:
        wb = max(slow, 200, 55, 14) + 20
        for sl, tp in [(0.02, 0.04), (0.02, 0.05), (0.03, 0.05)]:
            inds, entry, exit = ema_cross_with_all_filters(fast, slow, 200, 14, 55, 14, 40, "5m")
            configs.append((
                f"5m EMA {fast}-{slow} AllFilt SL{int(sl*100)}TP{int(tp*100)}",
                ema_cross_filtered_spec(
                    f"EMA {fast}-{slow} 5m AllFilt", fast, slow,
                    inds, entry, exit, stop=sl, take=tp, timeframe="5m",
                ),
                True, wb,
            ))

    # 5-min EMA crosses with momentum confirmation
    for fast, slow in [(30, 60), (50, 100), (80, 120)]:
        wb = max(slow, 14) + 20
        for mom_w in [5, 10, 20]:
            for mom_t in [0.005, 0.01, 0.015]:
                for sl, tp in [(0.02, 0.04), (0.02, 0.05), (0.03, 0.05)]:
                    inds, entry, exit = ema_cross_with_momentum_conf(fast, slow, mom_w, mom_t, "5m")
                    configs.append((
                        f"5m EMA {fast}-{slow} Mom{mom_w}_{mom_t*100:.1f} SL{int(sl*100)}TP{int(tp*100)}",
                        ema_cross_filtered_spec(
                            f"EMA {fast}-{slow} 5m Mom{mom_w}", fast, slow,
                            inds, entry, exit, stop=sl, take=tp, timeframe="5m",
                        ),
                        True, max(slow, mom_w) + 20,
                    ))

    for name, strat, is_spec, wb in configs:
        print(f"--- {name} (wb={wb}) ---", end=" ")
        perf, result, cfg = run_spec(dataset, strat, wb)
        if perf is not None and perf.n_trades > 0:
            ab = len(result.equity_curve) / max(perf.n_trades, 1)
            weeks = len(result.equity_curve) / (70 * 5)
            tw = perf.n_trades / weeks if weeks > 0 else 0
            print(f"Return={perf.total_return*100:.1f}% Sharpe={perf.sharpe:.2f} "
                  f"Trades={perf.n_trades} AvgBars={ab:.1f} Trades/Wk={tw:.1f} "
                  f"Win%={perf.win_rate*100:.0f}% PF={perf.profit_factor or 0:.2f}")
            results.append((name, perf, result))
        else:
            print("ERR/Trades=0")
    return results


def run_all_daily(dataset):
    results = []
    configs = []

    # Daily EMA crosses with trend filter (longer-term)
    for fast, slow in [(3, 5), (5, 10), (5, 13), (3, 8), (5, 8), (3, 13)]:
        wb = max(slow, 200) + 5
        for sl, tp in [(0.03, 0.05), (0.03, 0.07), (0.04, 0.06), (0.04, 0.08)]:
            inds, entry, exit = ema_cross_with_trend_filter(fast, slow, 200, "1d")
            configs.append((
                f"1d EMA {fast}-{slow} Trend200 SL{int(sl*100)}TP{int(tp*100)}",
                ema_cross_filtered_spec(
                    f"EMA {fast}-{slow} Trend200", fast, slow,
                    inds, entry, exit, stop=sl, take=tp, timeframe="1d",
                ),
                True, wb,
            ))

    # Daily EMA crosses with volatility filter
    for fast, slow in [(3, 5), (5, 10), (5, 13), (3, 8), (5, 8)]:
        wb = max(slow, 55) + 5
        for sl, tp in [(0.03, 0.05), (0.03, 0.07), (0.04, 0.06)]:
            inds, entry, exit = ema_cross_with_vol_filter(fast, slow, 14, 55, "1d")
            configs.append((
                f"1d EMA {fast}-{slow} VolATR SL{int(sl*100)}TP{int(tp*100)}",
                ema_cross_filtered_spec(
                    f"EMA {fast}-{slow} VolATR", fast, slow,
                    inds, entry, exit, stop=sl, take=tp, timeframe="1d",
                ),
                True, wb,
            ))

    # Daily EMA crosses with all filters
    for fast, slow in [(3, 5), (5, 10), (5, 13), (3, 8)]:
        wb = max(slow, 200, 55, 14) + 5
        for sl, tp in [(0.03, 0.05), (0.03, 0.07), (0.04, 0.06)]:
            inds, entry, exit = ema_cross_with_all_filters(fast, slow, 200, 14, 55, 14, 40, "1d")
            configs.append((
                f"1d EMA {fast}-{slow} AllFilt SL{int(sl*100)}TP{int(tp*100)}",
                ema_cross_filtered_spec(
                    f"EMA {fast}-{slow} AllFilt", fast, slow,
                    inds, entry, exit, stop=sl, take=tp, timeframe="1d",
                ),
                True, wb,
            ))

    # Daily EMA crosses with momentum confirmation
    for fast, slow in [(3, 5), (5, 10), (5, 13), (3, 8)]:
        wb = max(slow, 14) + 5
        for mom_w in [10, 20, 30]:
            for mom_t in [0.01, 0.02, 0.03]:
                for sl, tp in [(0.03, 0.05), (0.03, 0.07), (0.04, 0.06)]:
                    inds, entry, exit = ema_cross_with_momentum_conf(fast, slow, mom_w, mom_t, "1d")
                    configs.append((
                        f"1d EMA {fast}-{slow} Mom{mom_w}_{mom_t*100:.0f} SL{int(sl*100)}TP{int(tp*100)}",
                        ema_cross_filtered_spec(
                            f"EMA {fast}-{slow} Mom{mom_w}", fast, slow,
                            inds, entry, exit, stop=sl, take=tp, timeframe="1d",
                        ),
                        True, max(slow, mom_w) + 5,
                    ))

    for name, strat, is_spec, wb in configs:
        print(f"--- {name} (wb={wb}) ---", end=" ")
        perf, result, cfg = run_spec(dataset, strat, wb)
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


def print_summary(results, title, bars_per_week):
    print(f"\n{'='*130}")
    print(title)
    print(f"{'='*130}")
    rows = []
    for name, perf, result in results:
        ab = len(result.equity_curve) / max(perf.n_trades, 1)
        weeks = len(result.equity_curve) / bars_per_week
        tw = perf.n_trades / weeks if weeks > 0 else 0
        rows.append([name, f"{perf.total_return*100:.1f}%", f"{perf.sharpe:.2f}",
                     f"{ab:.1f}", str(perf.n_trades), f"{tw:.1f}",
                     f"{perf.win_rate*100:.0f}%", f"{perf.profit_factor:.2f}" if perf.profit_factor and np.isfinite(perf.profit_factor) else "N/A",
                     f"{perf.max_drawdown*100:.1f}%"])
    headers = ["Strategy", "Return", "Sharpe", "AvgBars", "Trades", "Trades/Wk", "Win%", "PF", "MaxDD"]
    print(tabulate(rows, headers=headers, tablefmt="grid"))
    print(f"{'='*130}")

    profitable = [(n, p, r) for n, p, r in results if p.total_return > 0 and np.isfinite(p.sharpe)]
    target = [(n, p, r) for n, p, r in profitable if 2.5 <= (p.n_trades / (len(r.equity_curve) / bars_per_week) if len(r.equity_curve) / bars_per_week > 0 else 0) <= 5.0]
    target.sort(key=lambda x: x[1].sharpe, reverse=True)

    print(f"\n--- Profitable in 3-4 trades/week range (2.5-5.0) ---")
    if target:
        for n, p, r in target[:20]:
            ab = len(r.equity_curve) / max(p.n_trades, 1)
            tw = p.n_trades / (len(r.equity_curve) / bars_per_week) if len(r.equity_curve) / bars_per_week > 0 else 0
            print(f"  {n}: Trades/Wk={tw:.1f} Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
                  f"AvgBars={ab:.1f} PF={p.profit_factor or 0:.2f} MaxDD={p.max_drawdown*100:.1f}%")
    else:
        print("  None in exact range.")

    print(f"\n--- Top 15 profitable by Sharpe ---")
    profitable.sort(key=lambda x: x[1].sharpe, reverse=True)
    for n, p, r in profitable[:15]:
        ab = len(r.equity_curve) / max(p.n_trades, 1)
        tw = p.n_trades / (len(r.equity_curve) / bars_per_week) if len(r.equity_curve) / bars_per_week > 0 else 0
        print(f"  {n}: Trades/Wk={tw:.1f} Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
              f"AvgBars={ab:.1f} PF={p.profit_factor or 0:.2f}")

    print(f"\n--- Most frequent profitable (by Trades/Wk) ---")
    if profitable:
        freq = sorted(profitable, key=lambda x: x[1].n_trades / (len(x[2].equity_curve) / bars_per_week) if len(x[2].equity_curve) / bars_per_week > 0 else 0, reverse=True)
        for n, p, r in freq[:10]:
            ab = len(r.equity_curve) / max(p.n_trades, 1)
            tw = p.n_trades / (len(r.equity_curve) / bars_per_week) if len(r.equity_curve) / bars_per_week > 0 else 0
            print(f"  {n}: Trades/Wk={tw:.1f} Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
                  f"AvgBars={ab:.1f} PF={p.profit_factor or 0:.2f}")
    else:
        print("  None profitable on this timeframe.")

    print(f"\nTotal: {len(results)} valid, {len(profitable)} profitable, {len(target)} in 3-4/wk range")


if __name__ == "__main__":
    print("Loading datasets...\n")
    ds_5min = load_5min_dataset_full()
    print()
    ds_daily = load_daily_dataset()

    print(f"\n{'='*130}")
    print("ROUND 14A: Full 5-min data (2015-2026) — filtered EMA crosses")
    print(f"{'='*130}\n")
    results_5min = run_all_5min_full(ds_5min)
    print_summary(results_5min, "ROUND 14A SUMMARY: Filtered EMA Crosses on 5-min (2015-2026)", 70 * 5)

    print(f"\n{'='*130}")
    print("ROUND 14B: Full daily data (2007-2026) — filtered EMA crosses")
    print(f"{'='*130}\n")
    results_daily = run_all_daily(ds_daily)
    print_summary(results_daily, "ROUND 14B SUMMARY: Filtered EMA Crosses on Daily (2007-2026)", 252)
