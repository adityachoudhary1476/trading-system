#!/usr/bin/env python3
"""Round 6: Refined high-frequency strategy search.

Builds on Round 5 findings, focusing on finer-grained parameter sweeps
around top-performing strategies (EMA crosses, momentum, trend filters)
and combined multi-signal strategies.

Round 5 winners:
  - EMA 3-5 cross:       Sharpe=0.36, Return=78.7%,  AvgBars=13.5
  - Momentum 20 thr1%:   Sharpe=0.38, Return=79.4%,  AvgBars=19.4
  - SMA 20 trend filter: Sharpe=0.36, Return=79.9%,  AvgBars=17.4
  - EMA 5-13 cross:      Sharpe=0.43, Return=97.9%,  AvgBars=24.2 (borderline freq)
"""
from __future__ import annotations

import warnings
import numpy as np
import pandas as pd
from trading_system.research.backtester import BacktestConfig, run_backtest
from trading_system.research.costs import IndiaTransactionCostModel, Segment as CostSegment
from trading_system.research.dataset import HistoricalDataset
from trading_system.research.performance import compute_performance
from trading_system.research.strategies import (
    Strategy, Signal, StrategyMeta,
    EMATrendStrategy, MomentumStrategy, BreakoutStrategy,
)
from trading_system.research.strategy_lab.spec import (
    StrategySpec, const_operand, field_operand,
    indicator_operand, logic, make_condition,
)
from trading_system.research.strategy_lab.interpreter import build_strategy
from trading_system.research.strategy_lab.engine import merged_backtest_config
from trading_system.research.risk import RiskConfig
from tabulate import tabulate

warnings.filterwarnings("ignore", category=FutureWarning)

CSV_PATH = "nifty-daily-moves-and-gaps/data/nifty50.csv"
INITIAL_CAPITAL = 100_000.0


def load_nifty_dataset():
    df = pd.read_csv(CSV_PATH, parse_dates=["Date"])
    df.index = pd.DatetimeIndex(df["Date"])
    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"
    })
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    df = df.tz_localize("UTC")
    print(f"  Loaded {len(df)} rows, {df.index[0].date()} to {df.index[-1].date()}")
    return HistoricalDataset(symbol="NSE:NIFTY", timeframe="1d", data=df, contract_id="NSE:NIFTY")


def make_risk_config(stop_loss_pct=None, take_profit_pct=None, max_alloc=0.95):
    return RiskConfig(
        max_allocation_pct=max_alloc,
        allow_short=False,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
    )


def run_builtin(dataset, strat, wb, stop=None, take=None):
    cfg = BacktestConfig(
        initial_capital=INITIAL_CAPITAL,
        slippage_pct=0.001,
        cost_model=IndiaTransactionCostModel(),
        cost_segment=CostSegment.EQUITY_FUTURE.value,
        warmup_bars=wb,
        risk=make_risk_config(stop, take),
    )
    result = run_backtest(dataset, strat, cfg)
    perf = compute_performance(result)
    return perf, result, cfg


def run_spec(dataset, spec, wb):
    base = BacktestConfig(
        initial_capital=INITIAL_CAPITAL,
        slippage_pct=0.001,
        cost_model=IndiaTransactionCostModel(),
        cost_segment=CostSegment.EQUITY_FUTURE.value,
        warmup_bars=wb,
    )
    cfg = merged_backtest_config(spec, base)
    strategy = build_strategy(spec)
    result = run_backtest(dataset, strategy, cfg)
    perf = compute_performance(result)
    return perf, result, cfg


# === Spec builders ===

def ema_cross_spec(fast, slow, stop=None, take=None, max_alloc=0.95):
    fk, sk = f"ema_{fast}", f"ema_{slow}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
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
        "position_sizing": {"max_allocation_pct": max_alloc},
        "risk": risk,
    }, model="round6-script")


def ema_trend_momentum_spec(ema_window, mom_window, mom_threshold, stop=None, take=None):
    """Combined: LONG when EMA trend is up AND momentum exceeds threshold;
    exit when EMA trend reverses OR momentum drops below 0."""
    ema_k = f"ema_{ema_window}"
    mom_k = f"momentum_{mom_window}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"EMA {ema_window} trend and Mom{mom_window} thr{int(mom_threshold*100)}pct",
        "description": f"LONG while EMA({ema_window}) > close and momentum({mom_window}) > {mom_threshold*100:.1f}%. Exit both conditions break.",        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [
            {"name": "ema", "params": {"window": ema_window}},
            {"name": "momentum", "params": {"window": mom_window}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand(ema_k)),
            make_condition(indicator_operand(mom_k), ">", const_operand(mom_threshold)),
        ),
        "exit": logic("OR",
            make_condition(field_operand("close"), "<", indicator_operand(ema_k)),
            make_condition(indicator_operand(mom_k), "<", const_operand(0.0)),
        ),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round6-script")


def ema_trend_only_spec(ema_window, stop=None, take=None):
    ema_k = f"ema_{ema_window}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"EMA {ema_window} trend filter",
        "description": f"LONG while close > EMA({ema_window}); exit below.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [{"name": "ema", "params": {"window": ema_window}}],
        "entry": make_condition(field_operand("close"), ">", indicator_operand(ema_k)),
        "exit": make_condition(field_operand("close"), "<", indicator_operand(ema_k)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round6-script")


def sma_trend_spec(window, stop=None, take=None):
    sk = f"sma_{window}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"SMA {window} trend filter",
        "description": f"LONG while close > SMA({window}); exit below.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [{"name": "sma", "params": {"window": window}}],
        "entry": make_condition(field_operand("close"), ">", indicator_operand(sk)),
        "exit": make_condition(field_operand("close"), "<", indicator_operand(sk)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round6-script")


def run_all_backtests(dataset):
    configs = []

    # === 1. Finer EMA crosses (refine around 3-5, 5-13, 8-21 winners) ===
    for fast, slow in [(3, 5), (3, 7), (4, 7), (5, 7), (4, 6), (5, 8),
                       (6, 11), (7, 13), (3, 6), (5, 9), (8, 13), (3, 4)]:
        wb = max(slow, fast) + 5
        if slow - fast <= 2:
            # Use wider SL/TP for ultra-tight crosses to survive costs
            configs.append((
                f"EMA {fast}-{slow} cross (SL5 TP10) freq",
                EMATrendStrategy(fast=fast, slow=slow),
                False, wb,
                {"stop_loss_pct": 0.05, "take_profit_pct": 0.10},
            ))
        configs.append((
            f"EMA {fast}-{slow} Spec (SL5 TP10) freq",
            ema_cross_spec(fast, slow, stop=0.05, take=0.10),
            True, wb,
            None,
        ))

    # === 2. EMA cross with varied risk params on known winners ===
    for fast, slow in [(3, 5), (5, 13), (8, 21)]:
        wb = max(slow, fast) + 5
        for sl, tp in [(0.03, 0.06), (0.05, 0.10), (0.08, 0.15), (0.10, 0.20), (None, None)]:
            tag = f"SL{int(sl*100) if sl else 0}TP{int(tp*100) if tp else 0}" if sl else "no-risk"
            name = f"EMA {fast}-{slow} Spec ({tag}) freq"
            configs.append((
                name,
                ema_cross_spec(fast, slow, stop=sl, take=tp),
                True, wb,
                None,
            ))

    # === 3. Momentum with finer thresholds ===
    for window in [15, 20, 25]:
        for thr in [0.005, 0.01, 0.015, 0.02, 0.03]:
            exit_thr = -thr  # symmetric exit
            configs.append((
                f"Momentum {window} thr{thr*100:.1f}% exit0pct (SL5 TP10) freq",
                MomentumStrategy(window=window, entry_thr=thr, exit_thr=0.0),
                False, window + 5,
                {"stop_loss_pct": 0.05, "take_profit_pct": 0.10},
            ))

    # === 4. EMA trend filters with finer periods ===
    for w in [10, 12, 15, 18, 20, 22, 25, 30, 35, 40]:
        configs.append((
            f"EMA {w} trend filter (SL5 TP10) freq",
            ema_trend_only_spec(w, stop=0.05, take=0.10),
            True, w + 5,
            None,
        ))

    # === 5. SMA trend filters with finer periods ===
    for w in [12, 15, 18, 20, 22, 25, 30, 35, 40]:
        configs.append((
            f"SMA {w} trend filter (SL5 TP10) freq",
            sma_trend_spec(w, stop=0.05, take=0.10),
            True, w + 5,
            None,
        ))

    # === 6. Combined EMA trend + momentum (multi-signal confirmation) ===
    for ema_w in [15, 20, 25, 30]:
        for mom_w in [10, 15, 20]:
            for thr in [0.01, 0.02, 0.03]:
                configs.append((
                    f"EMA {ema_w} + Mom({mom_w}) thr{thr*100:.0f}% (SL5 TP10) freq",
                    ema_trend_momentum_spec(ema_w, mom_w, thr, stop=0.05, take=0.10),
                    True, max(ema_w, mom_w) + 5,
                    None,
                ))

    # === 7. EMA crosses with no risk params (let EMA exit handle it) ===
    for fast, slow in [(3, 5), (4, 7), (5, 8), (5, 13), (8, 21)]:
        wb = max(slow, fast) + 5
        configs.append((
            f"EMA {fast}-{slow} no-risk freq",
            ema_cross_spec(fast, slow, stop=None, take=None),
            True, wb,
            None,
        ))

    # === 8. MACD short variants ===
    for fast, slow, signal_sp in [(3, 8, 3), (5, 13, 3), (3, 5, 3),
                                   (4, 7, 2), (5, 10, 3), (6, 13, 3)]:
        configs.append((
            f"MACD {fast}-{slow}-{signal_sp} (SL5 TP10) freq",
            macd_spec(fast, slow, signal_sp, stop=0.05, take=0.10),
            True, slow + signal_sp + 5,
            None,
        ))

    results = []
    for name, strat, is_spec, wb, risk_dict in configs:
        print(f"--- {name} (warmup={wb}) ---", end=" ")
        if is_spec:
            perf, result, cfg = run_spec(dataset, strat, wb)
        else:
            perf, result, cfg = run_builtin(
                dataset, strat, wb,
                risk_dict.get("stop_loss_pct") if risk_dict else None,
                risk_dict.get("take_profit_pct") if risk_dict else None,
            )
        results.append((name, perf, result, cfg))
        if perf is not None:
            avg_bars = len(result.equity_curve) / max(perf.n_trades, 1) if perf.n_trades > 0 else float('inf')
            print(f"Return={perf.total_return*100:.1f}% Sharpe={perf.sharpe:.2f} "
                  f"Trades={perf.n_trades} AvgBars={avg_bars:.1f} "
                  f"Win%={perf.win_rate*100:.0f}% PF={perf.profit_factor or 0:.2f}")
        else:
            print("ERR")

    return results


# Reuse macd spec from round 5 (defined inline since it's in the same file)
def macd_spec(fast, slow, signal_sp, stop=None, take=None):
    mk = f"macd_{fast}_{slow}_{signal_sp}"
    sk = f"macd_signal_{fast}_{slow}_{signal_sp}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"MACD {fast}-{slow}-{signal_sp} cross",
        "description": f"LONG when MACD line crosses above signal line; exit on cross below.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [
            {"name": "macd", "params": {"fast": fast, "slow": slow, "signal": signal_sp}},
            {"name": "macd_signal", "params": {"fast": fast, "slow": slow, "signal": signal_sp}},
        ],
        "entry": make_condition(indicator_operand(mk), "crosses_above", indicator_operand(sk)),
        "exit": make_condition(indicator_operand(mk), "crosses_below", indicator_operand(sk)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round6-script")


def print_summary(results):
    rows = []
    for name, perf, result, cfg in results:
        if perf is None:
            rows.append([name, "ERR", "ERR", "ERR", "ERR", "ERR", "ERR", "ERR", "ERR"])
        else:
            avg_bars = len(result.equity_curve) / max(perf.n_trades, 1) if perf.n_trades > 0 else float('inf')
            rows.append([
                name,
                f"{perf.total_return*100:.1f}%",
                f"{perf.sharpe:.2f}" if np.isfinite(perf.sharpe) else "N/A",
                f"{perf.sortino:.2f}" if np.isfinite(perf.sortino) else "N/A",
                f"{avg_bars:.1f}",
                str(perf.n_trades),
                f"{perf.win_rate*100:.0f}%",
                f"{perf.profit_factor:.2f}" if perf.profit_factor and np.isfinite(perf.profit_factor) else "N/A",
                f"{perf.max_drawdown*100:.1f}%",
            ])

    headers = ["Strategy", "Return", "Sharpe", "Sortino", "AvgBars", "Trades", "Win%", "PF", "MaxDD"]
    print("\n" + "=" * 160)
    print("NIFTY 50 Round 6 - Refined High-Frequency Strategies")
    print(f"Capital: Rs.{INITIAL_CAPITAL:,.0f}  Data: {CSV_PATH}  Strategies: {len(results)}")
    print("AvgBars = average bars held per trade (5=weekly, 10=2-weekly, 20=monthly)")
    print("=" * 160)
    print(tabulate(rows, headers=headers, tablefmt="grid"))
    print("=" * 160)

    valid = [(n, p, r, c) for n, p, r, c in results if p is not None]
    if valid:
        freq_profitable = [(n, p, r) for n, p, r, _ in valid
                          if p.n_trades > 0 and len(r.equity_curve) / p.n_trades <= 20 and p.total_return > 0]
        freq_profitable.sort(key=lambda x: x[1].n_trades, reverse=True)
        print(f"\nHigh-frequency AND profitable (avg bars <= 20, return > 0): {len(freq_profitable)}/{len(valid)}")

        print("\n--- High-frequency + profitable: Ranking by Sharpe ---")
        for i, (n, p, r) in enumerate(sorted(freq_profitable, key=lambda x: x[1].sharpe, reverse=True)[:25], 1):
            avg_bars = len(r.equity_curve) / max(p.n_trades, 1)
            print(f"  {i}. {n}: Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
                  f"AvgBars={avg_bars:.1f} Trades={p.n_trades} PF={p.profit_factor or 0:.2f} MaxDD={p.max_drawdown*100:.1f}%")

        print("\n--- High-frequency + profitable: Ranking by Total Return ---")
        for i, (n, p, r) in enumerate(sorted(freq_profitable, key=lambda x: x[1].total_return, reverse=True)[:25], 1):
            avg_bars = len(r.equity_curve) / max(p.n_trades, 1)
            print(f"  {i}. {n}: Return={p.total_return*100:.1f}% "
                  f"AvgBars={avg_bars:.1f} Trades={p.n_trades} Sharpe={p.sharpe:.2f} PF={p.profit_factor or 0:.2f}")

        print(f"\nTotal: {len(valid)} valid, {len(freq_profitable)} high-freq+profitable "
              f"({len(freq_profitable)/len(valid)*100:.0f}%)")


if __name__ == "__main__":
    print("Loading Nifty 50 dataset...")
    ds = load_nifty_dataset()
    print(f"\nDataset: {ds.symbol} {ds.timeframe}, {len(ds.data)} rows")
    print(f"Date range: {ds.data.index[0].date()} to {ds.data.index[-1].date()}")
    print("\nRunning Round 6 backtests (refined high-frequency strategies)...\n")
    results = run_all_backtests(ds)
    print_summary(results)
