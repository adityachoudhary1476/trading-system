#!/usr/bin/env python3
"""Round 7: Ultra-high-frequency strategies — 3-4 trades per week.

Goal: Find strategies that generate 3-4 trades per week on daily data
(~252 trading days/year => 75-100 trades/year minimum, target 100+ trades).

On 4536 bars of ~19 years of data, 3-4 trades/week means:
  - ~900 weeks × 3-4 = 2700-3600 trades total
  - AvgBars ≈ 4536 / 3000 ≈ 1.5 (holding period of 1-2 days)

Strategy focus:
  - Ultra-short EMA crosses (2-3, 2-4, 3-4) with tight SL/TP
  - EMA trend filters (2, 3, 4, 5) with tight SL/TP
  - Short momentum (2, 3, 5) with tight thresholds
  - RSI 2/3 mean-reversion with tight exits
  - Bollinger Band mean-reversion with tight exits
  - Very short breakouts (2, 3, 5)
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
TRADING_DAYS_PER_YEAR = 252
WEEKS_PER_YEAR = TRADING_DAYS_PER_YEAR / 5  # ~50


def load_nifty_dataset():
    df = pd.read_csv(CSV_PATH, parse_dates=["Date"])
    df.index = pd.DatetimeIndex(df["Date"])
    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"
    })
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    df = df.tz_localize("UTC")
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
    }, model="round7-script")


def ema_trend_spec(window, stop=None, take=None, max_alloc=0.95):
    ema_k = f"ema_{window}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"EMA {window} trend filter",
        "description": f"LONG while close > EMA({window}); exit below.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [{"name": "ema", "params": {"window": window}}],
        "entry": make_condition(field_operand("close"), ">", indicator_operand(ema_k)),
        "exit": make_condition(field_operand("close"), "<", indicator_operand(ema_k)),
        "position_sizing": {"max_allocation_pct": max_alloc},
        "risk": risk,
    }, model="round7-script")


def rsi_mr_spec(window, oversold, overbought, stop=None, take=None):
    rk = f"rsi_{window}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"RSI {window} MR {oversold}-{overbought}",
        "description": f"LONG when RSI {window} crosses above {oversold}; exit above {overbought}.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [{"name": "rsi", "params": {"window": window}}],
        "entry": make_condition(indicator_operand(rk), "crosses_above", const_operand(float(oversold))),
        "exit": make_condition(indicator_operand(rk), ">", const_operand(float(overbought))),
        "position_sizing": {"max_allocation_pct": 0.5},
        "risk": risk,
    }, model="round7-script")


def bb_mr_spec(window, num_std, rsi_window, rsi_os, stop=None, take=None):
    ns = int(num_std) if float(num_std).is_integer() else num_std
    ns_str = str(ns)
    bb_lk = f"bb_lower_{window}_{ns_str}"
    bb_mk = f"bb_middle_{window}_{ns_str}"
    rk = f"rsi_{rsi_window}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"BB {window}-{ns} lower MR RSI{rsi_window}",
        "description": f"LONG when close < lower BB({window},{ns}) AND RSI<{rsi_os}; exit when close > middle BB.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [
            {"name": "bb_lower", "params": {"window": window, "num_std": num_std}},
            {"name": "bb_middle", "params": {"window": window, "num_std": num_std}},
            {"name": "rsi", "params": {"window": rsi_window}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), "<", indicator_operand(bb_lk)),
            make_condition(indicator_operand(rk), "<", const_operand(float(rsi_os))),
        ),
        "exit": make_condition(field_operand("close"), ">", indicator_operand(bb_mk)),
        "position_sizing": {"max_allocation_pct": 0.5},
        "risk": risk,
    }, model="round7-script")


def run_all_backtests(dataset):
    configs = []

    # === 1. Ultra-short EMA crosses with tight SL/TP ===
    for fast, slow in [(2, 3), (2, 4), (2, 5), (3, 4), (3, 5), (4, 5), (4, 6), (3, 6)]:
        wb = slow + 3
        for sl_pct, tp_pct in [(0.02, 0.03), (0.02, 0.05), (0.03, 0.05), (0.02, 0.04), (0.03, 0.04)]:
            tag = f"SL{int(sl_pct*100)}TP{int(tp_pct*100)}"
            configs.append((
                f"EMA {fast}-{slow} ({tag}) uhf",
                EMATrendStrategy(fast=fast, slow=slow),
                False, wb,
                {"stop_loss_pct": sl_pct, "take_profit_pct": tp_pct},
            ))

    # === 2. EMA cross via Spec with tight SL/TP ===
    for fast, slow in [(2, 3), (2, 4), (3, 4), (3, 5), (4, 5)]:
        wb = slow + 3
        for sl_pct, tp_pct in [(0.02, 0.03), (0.02, 0.05), (0.03, 0.05)]:
            tag = f"SL{int(sl_pct*100)}TP{int(tp_pct*100)}"
            configs.append((
                f"EMA {fast}-{slow} Spec ({tag}) uhf",
                ema_cross_spec(fast, slow, stop=sl_pct, take=tp_pct),
                True, wb,
                None,
            ))

    # === 3. Ultra-short EMA trend filters with tight SL/TP ===
    for w in [2, 3, 4, 5, 6]:
        wb = w + 3
        for sl_pct, tp_pct in [(0.02, 0.03), (0.02, 0.05), (0.03, 0.05), (0.02, 0.04)]:
            tag = f"SL{int(sl_pct*100)}TP{int(tp_pct*100)}"
            configs.append((
                f"EMA {w} trend ({tag}) uhf",
                ema_trend_spec(w, stop=sl_pct, take=tp_pct),
                True, wb,
                None,
            ))

    # === 4. Momentum with very short lookback and tight thresholds ===
    for window in [2, 3, 5, 7]:
        for entry_thr, exit_thr in [(0.01, 0.0), (0.015, -0.005),
                                     (0.02, 0.0), (0.02, -0.005),
                                     (0.01, -0.01), (0.015, -0.01),
                                     (0.005, 0.0), (0.005, -0.005)]:
            configs.append((
                f"Momentum {window} thr{entry_thr*100:.0f}% exit{exit_thr*100:.0f}% SL5TP10 uhf",
                MomentumStrategy(window=window, entry_thr=entry_thr, exit_thr=exit_thr),
                False, window + 3,
                {"stop_loss_pct": 0.05, "take_profit_pct": 0.10},
            ))

    # === 5. RSI mean-reversion with very short windows ===
    for window in [2, 3, 5, 7]:
        for os_v, ob_v in [(5, 85), (10, 80), (5, 75), (10, 75), (15, 80),
                           (5, 90), (3, 85), (7, 80), (5, 80), (10, 90)]:
            for sl_pct, tp_pct in [(0.02, 0.03), (0.02, 0.04), (0.03, 0.05)]:
                tag = f"SL{int(sl_pct*100)}TP{int(tp_pct*100)}"
                configs.append((
                    f"RSI {window} MR {os_v}-{ob_v} ({tag}) uhf",
                    rsi_mr_spec(window, os_v, ob_v, stop=sl_pct, take=tp_pct),
                    True, window + 3,
                    None,
                ))

    # === 6. BB mean-reversion with tight parameters ===
    for window in [5, 10, 15]:
        for ns in [2.0]:
            for rsi_w in [5, 10]:
                for rsi_os in [20, 30]:
                    for sl_pct, tp_pct in [(0.02, 0.03), (0.02, 0.04)]:
                        tag = f"SL{int(sl_pct*100)}TP{int(tp_pct*100)}"
                        configs.append((
                            f"BB {window}-{int(ns)} lower MR RSI{rsi_w} {rsi_os} ({tag}) uhf",
                            bb_mr_spec(window, ns, rsi_w, rsi_os, stop=sl_pct, take=tp_pct),
                            True, window * 2 + 3,
                            None,
                        ))

    # === 7. Breakout with very short lookback + tight SL ===
    for lb in [2, 3, 5]:
        for sl_pct, tp_pct in [(0.02, 0.03), (0.02, 0.04), (0.02, 0.05)]:
            tag = f"SL{int(sl_pct*100)}TP{int(tp_pct*100)}"
            configs.append((
                f"Breakout {lb} ({tag}) uhf",
                BreakoutStrategy(lookback=lb),
                False, lb + 3,
                {"stop_loss_pct": sl_pct, "take_profit_pct": tp_pct},
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
        results.append((name, perf, result, cfg))
        if perf is not None:
            avg_bars = len(result.equity_curve) / max(perf.n_trades, 1) if perf.n_trades > 0 else float('inf')
            trades_per_year = perf.n_trades / (len(result.equity_curve) / TRADING_DAYS_PER_YEAR) if perf.n_trades > 0 else 0
            trades_per_week = trades_per_year / WEEKS_PER_YEAR
            print(f"Return={perf.total_return*100:.1f}% Sharpe={perf.sharpe:.2f} "
                  f"Trades={perf.n_trades} AvgBars={avg_bars:.1f} "
                  f"Trades/yr={trades_per_year:.0f} Trades/wk={trades_per_week:.1f} "
                  f"Win%={perf.win_rate*100:.0f}% PF={perf.profit_factor or 0:.2f}")
        else:
            print("ERR")

    return results


def print_summary(results):
    rows = []
    for name, perf, result, cfg in results:
        if perf is None:
            rows.append([name, "ERR", "ERR", "ERR", "ERR", "ERR", "ERR", "ERR", "ERR", "ERR"])
        else:
            avg_bars = len(result.equity_curve) / max(perf.n_trades, 1) if perf.n_trades > 0 else float('inf')
            ty = len(result.equity_curve) / TRADING_DAYS_PER_YEAR
            trades_per_year = perf.n_trades / ty if ty > 0 else 0
            trades_per_week = trades_per_year / WEEKS_PER_YEAR
            rows.append([
                name,
                f"{perf.total_return*100:.1f}%",
                f"{perf.sharpe:.2f}" if np.isfinite(perf.sharpe) else "N/A",
                f"{avg_bars:.1f}",
                str(perf.n_trades),
                f"{trades_per_week:.1f}",
                f"{perf.win_rate*100:.0f}%",
                f"{perf.profit_factor:.2f}" if perf.profit_factor and np.isfinite(perf.profit_factor) else "N/A",
                f"{perf.max_drawdown*100:.1f}%",
            ])

    headers = ["Strategy", "Return", "Sharpe", "AvgBars", "Trades", "Trades/Wk", "Win%", "PF", "MaxDD"]
    print("\n" + "=" * 170)
    print("NIFTY 50 Round 7 - Ultra-High-Frequency Strategies (3-4 trades/week)")
    print(f"Capital: Rs.{INITIAL_CAPITAL:,.0f}  Data: {CSV_PATH}  Strategies: {len(results)}")
    print("Target: 3-4 trades per week (AvgBars ~1-3)")
    print("=" * 170)
    print(tabulate(rows, headers=headers, tablefmt="grid"))
    print("=" * 170)

    valid = [(n, p, r, c) for n, p, r, c in results if p is not None and p.n_trades > 0]

    # Categorize by trade frequency
    high_freq = [(n, p, r) for n, p, r, _ in valid
                 if (len(r.equity_curve) / p.n_trades) <= 5 and p.total_return > 0]
    high_freq.sort(key=lambda x: x[1].sharpe, reverse=True)

    very_high_freq = [(n, p, r) for n, p, r, _ in valid
                      if (p.n_trades / (len(r.equity_curve) / TRADING_DAYS_PER_YEAR) / WEEKS_PER_YEAR) >= 2.5
                      and p.total_return > 0]
    very_high_freq.sort(key=lambda x: x[1].sharpe, reverse=True)

    print(f"\n--- High-frequency (AvgBars <= 5, return > 0): {len(high_freq)} ---")
    for i, (n, p, r) in enumerate(high_freq[:20], 1):
        avg_bars = len(r.equity_curve) / max(p.n_trades, 1)
        tw = p.n_trades / (len(r.equity_curve) / TRADING_DAYS_PER_YEAR) / WEEKS_PER_YEAR
        print(f"  {i}. {n}: Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
              f"AvgBars={avg_bars:.1f} Trades={p.n_trades} Trades/Wk={tw:.1f} PF={p.profits_factor if hasattr(p, 'profits_factor') else (p.profit_factor or 0):.2f}")

    print(f"\n--- Very high-frequency (Trades/Wk >= 2.5, return > 0): {len(very_high_freq)} ---")
    for i, (n, p, r) in enumerate(very_high_freq[:30], 1):
        avg_bars = len(r.equity_curve) / max(p.n_trades, 1)
        tw = p.n_trades / (len(r.equity_curve) / TRADING_DAYS_PER_YEAR) / WEEKS_PER_YEAR
        print(f"  {i}. {n}: Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
              f"AvgBars={avg_bars:.1f} Trades={p.n_trades} Trades/Wk={tw:.1f} PF={p.profit_factor or 0:.2f} MaxDD={p.max_drawdown*100:.1f}%")


if __name__ == "__main__":
    print("Loading Nifty 50 dataset...")
    ds = load_nifty_dataset()
    print(f"\nDataset: {ds.symbol} {ds.timeframe}, {len(ds.data)} rows")
    print(f"Date range: {ds.data.index[0].date()} to {ds.data.index[-1].date()}")
    print(f"~{len(ds.data)/TRADING_DAYS_PER_YEAR:.1f} years, ~{len(ds.data)/5:.0f} weeks")
    print(f"Target: 3-4 trades/week = {len(ds.data)/5*3:.0f}-{len(ds.data)/5*4:.0f} total trades")
    print("\nRunning Round 7 backtests (ultra-high-frequency strategies)...\n")
    results = run_all_backtests(ds)
    print_summary(results)
