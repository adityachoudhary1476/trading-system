#!/usr/bin/env python3
"""Round 5: High-frequency strategy search.

Finds strategies that generate trading signals on a weekly to 2-weekly basis
(average holding period of 1-20 bars on daily data).

Focus: short-period EMAs, SMAs, momentum, RSI, Bollinger bands, MACD variants.
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


# Spec builders
def ema_cross_spec(fast, slow, stop=None, take=None):
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
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round5-script")


def sma_cross_spec(fast, slow, stop=None, take=None):
    fk, sk = f"sma_{fast}", f"sma_{slow}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"SMA {fast}-{slow} cross",
        "description": f"LONG when SMA({fast}) crosses above SMA({slow}); exit on cross below.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [
            {"name": "sma", "params": {"window": fast}},
            {"name": "sma", "params": {"window": slow}},
        ],
        "entry": make_condition(indicator_operand(fk), "crosses_above", indicator_operand(sk)),
        "exit": make_condition(indicator_operand(fk), "crosses_below", indicator_operand(sk)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round5-script")


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
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round5-script")


def bb_mr_spec(window, num_std, rsi_window, rsi_os, stop=None, take=None):
    ns = int(num_std) if float(num_std).is_integer() else num_std
    bb_lk = f"bb_lower_{window}_{ns}"
    bb_mk = f"bb_middle_{window}_{ns}"
    rk = f"rsi_{rsi_window}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"BB {window}-{ns} and RSI {rsi_window} MR",
        "description": f"LONG when close < lower BB AND RSI<{rsi_os}; exit when close > middle BB.",
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
    }, model="round5-script")


def bb_upper_spec(window, num_std, stop=None, take=None):
    ns = int(num_std) if float(num_std).is_integer() else num_std
    bb_uk = f"bb_upper_{window}_{ns}"
    bb_mk = f"bb_middle_{window}_{ns}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"BB {window}-{ns} upper breakout",
        "description": f"LONG when close > upper BB({window},{ns}); exit when close < middle BB.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [
            {"name": "bb_upper", "params": {"window": window, "num_std": num_std}},
            {"name": "bb_middle", "params": {"window": window, "num_std": num_std}},
        ],
        "entry": make_condition(field_operand("close"), ">", indicator_operand(bb_uk)),
        "exit": make_condition(field_operand("close"), "<", indicator_operand(bb_mk)),
        "position_sizing": {"max_allocation_pct": 0.5},
        "risk": risk,
    }, model="round5-script")


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
    }, model="round5-script")


def sma_trend_spec(window, stop=None, take=None):
    sk = f"sma_{window}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"SMA {window} trend filter",
        "description": f"LONG while close > SMA {window}; exit below.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [{"name": "sma", "params": {"window": window}}],
        "entry": make_condition(field_operand("close"), ">", indicator_operand(sk)),
        "exit": make_condition(field_operand("close"), "<", indicator_operand(sk)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round5-script")


def run_all_backtests(dataset):
    """High-frequency strategies: weekly to 2-weekly trading."""
    configs = []

    # === Ultra-short EMA crosses ===
    for fast, slow in [(3, 5), (3, 8), (3, 10), (3, 13), (5, 8), (5, 10), (5, 13),
                       (5, 20), (8, 13), (8, 21), (8, 34), (10, 20), (10, 21),
                       (13, 21), (13, 34), (15, 26), (20, 34), (20, 55)]:
        configs.append((
            f"EMA {fast}-{slow} built-in (SL5 TP10) freq",
            EMATrendStrategy(fast=fast, slow=slow),
            False, max(slow, fast) + 5,
            {"stop_loss_pct": 0.05, "take_profit_pct": 0.10},
        ))

    # === Ultra-short EMA crosses with wider SL ===
    for fast, slow in [(3, 5), (3, 8), (5, 8), (5, 10), (8, 13), (8, 21), (10, 21)]:
        configs.append((
            f"EMA {fast}-{slow} built-in (SL8 TP12) freq",
            EMATrendStrategy(fast=fast, slow=slow),
            False, max(slow, fast) + 5,
            {"stop_loss_pct": 0.08, "take_profit_pct": 0.12},
        ))

    # === EMA crosses via StrategySpec (for risk param tracking) ===
    for fast, slow in [(3, 8), (3, 13), (5, 13), (8, 21), (10, 21)]:
        configs.append((
            f"EMA {fast}-{slow} Spec (SL5 TP10) freq",
            ema_cross_spec(fast, slow, stop=0.05, take=0.10),
            True, max(slow, fast) + 5,
            None,
        ))

    # === Ultra-short SMA crosses ===
    for fast, slow in [(3, 5), (3, 8), (3, 10), (3, 15), (5, 8), (5, 10), (5, 15),
                       (5, 20), (8, 13), (8, 21), (10, 15), (10, 20)]:
        configs.append((
            f"SMA {fast}-{slow} cross (SL5 TP10) freq",
            sma_cross_spec(fast, slow, stop=0.05, take=0.10),
            True, max(slow, fast) + 5,
            None,
        ))

    # === Short SMA trend filters ===
    for w in [5, 8, 10, 15, 20, 30]:
        configs.append((
            f"SMA {w} trend filter (SL5 TP10) freq",
            sma_trend_spec(w, stop=0.05, take=0.10),
            True, w + 5,
            None,
        ))

    # === Short momentum strategies ===
    for window in [3, 5, 7, 10, 12, 15, 20]:
        for thr, exit_thr in [(0.0, 0.0), (0.01, 0.0), (0.02, 0.0), (0.0, -0.01), (0.01, -0.01)]:
            configs.append((
                f"Momentum {window} thr{thr*100:.0f} exit{exit_thr*100:.0f}pct (SL5 TP10) freq",
                MomentumStrategy(window=window, entry_thr=thr, exit_thr=exit_thr),
                False, window + 5,
                {"stop_loss_pct": 0.05, "take_profit_pct": 0.10},
            ))

    # === Short RSI strategies ===
    for window in [2, 3, 5, 7, 10]:
        for os, ob in [(5, 70), (10, 65), (15, 70), (10, 80), (20, 70), (5, 60),
                       (10, 50), (15, 50), (3, 80), (10, 60)]:
            configs.append((
                f"RSI {window} MR {os}-{ob} (SL4 TP8) freq",
                rsi_mr_spec(window, os, ob, stop=0.04, take=0.08),
                True, window * 2 + 5,
                None,
            ))

    # === Short Bollinger band strategies ===
    for window in [10, 15, 20]:
        for ns in [2.0, 3.0]:
            # Upper breakout
            configs.append((
                f"BB {window}-{int(ns)} upper breakout (SL5 TP10) freq",
                bb_upper_spec(window, ns, stop=0.05, take=0.10),
                True, window * 2 + 5,
                None,
            ))
            # Lower MR with RSI
            configs.append((
                f"BB {window}-{int(ns)} lower MR RSI14 (SL5 TP10) freq",
                bb_mr_spec(window, ns, 14, 35, stop=0.05, take=0.10),
                True, window * 2 + 5,
                None,
            ))

    # === Short MACD variants ===
    for fast, slow, signal_sp in [(5, 13, 3), (6, 13, 3), (8, 17, 5),
                                   (8, 21, 5), (10, 26, 5), (12, 26, 5),
                                   (3, 10, 3), (5, 13, 2), (8, 13, 3)]:
        configs.append((
            f"MACD {fast}-{slow}-{signal_sp} cross (SL5 TP10) freq",
            macd_spec(fast, slow, signal_sp, stop=0.05, take=0.10),
            True, slow + signal_sp + 5,
            None,
        ))

    # === Breakout strategies with short lookback ===
    for lb in [3, 5, 8, 10, 15, 20]:
        configs.append((
            f"Breakout {lb} (SL5 TP10) freq",
            BreakoutStrategy(lookback=lb),
            False, lb + 5,
            {"stop_loss_pct": 0.05, "take_profit_pct": 0.10},
        ))

    # === Combined: EMA cross + RSI filter for more signal frequency ===
    for fast, slow in [(5, 13), (8, 21), (10, 21), (5, 20)]:
        configs.append((
            f"EMA {fast}-{slow} spec no-risk freq",
            ema_cross_spec(fast, slow, stop=None, take=None),
            True, max(slow, fast) + 5,
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
        print(f"Return={perf.total_return*100:.1f}% Sharpe={perf.sharpe:.2f} "
              f"Trades={perf.n_trades} AvgBars={perf.n_trades and (len(result.equity_curve)/max(perf.n_trades,1)):.1f} "
              f"Win%={perf.win_rate*100:.0f}% PF={perf.profit_factor or 0:.2f}")

    return results


def print_summary(results):
    n_bars = 10000  # approximate bars in evaluation window

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
    print("\n" + "=" * 155)
    print("NIFTY 50 Round 5 - High-Frequency Strategies (weekly to 2-weekly)")
    print(f"Capital: Rs.{INITIAL_CAPITAL:,.0f}  Data: {CSV_PATH}  Strategies: {len(results)}")
    print("AvgBars = average bars held per trade (5=weekly, 10=2-weekly, 20=monthly)")
    print("=" * 155)
    print(tabulate(rows, headers=headers, tablefmt="grid"))
    print("=" * 155)

    valid = [(n, p, r, c) for n, p, r, c in results if p is not None]
    if valid:
        # Filter for high-frequency + profitable
        freq_profitable = [(n, p, r) for n, p, r, _ in valid
                          if p.n_trades > 0 and len(r.equity_curve) / p.n_trades <= 20 and p.total_return > 0]
        freq_profitable.sort(key=lambda x: x[1].n_trades, reverse=True)
        print(f"\nHigh-frequency AND profitable (avg bars <= 20, return > 0): {len(freq_profitable)}/{len(valid)}")
        print("\nTop 30 high-frequency profitable strategies (by trade count):")
        for i, (n, p, r) in enumerate(freq_profitable[:30], 1):
            avg_bars = len(r.equity_curve) / max(p.n_trades, 1)
            print(f"  {i}. {n}: Return={p.total_return*100:.1f}% Sharpe={p.sharpe:.2f} "
                  f"AvgBars={avg_bars:.1f} Trades={p.n_trades} "
                  f"Win%={p.win_rate*100:.0f}% PF={p.profit_factor or 0:.2f} MaxDD={p.max_drawdown*100:.1f}%")

        print("\nRanking by Sharpe (all high-freq + profitable):")
        for i, (n, p, r) in enumerate(sorted(freq_profitable, key=lambda x: x[1].sharpe, reverse=True)[:20], 1):
            avg_bars = len(r.equity_curve) / max(p.n_trades, 1)
            print(f"  {i}. {n}: Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
                  f"AvgBars={avg_bars:.1f} Trades={p.n_trades} PF={p.profit_factor or 0:.2f}")

        print("\nRanking by Total Return (high-freq + profitable):")
        for i, (n, p, r) in enumerate(sorted(freq_profitable, key=lambda x: x[1].total_return, reverse=True)[:20], 1):
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
    print("\nRunning Round 5 backtests (high-frequency strategies)...\n")
    results = run_all_backtests(ds)
    print_summary(results)
