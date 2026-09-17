#!/usr/bin/env python3
"""Backtest single-leg Nifty strategies using the existing deterministic backtester.

Tests strategies researched from the internet against Nifty 50 daily data
using the project's own backtesting pipeline (backtester.py + performance.py).
No new strategy code is added to the codebase.
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


# --------------------------------------------------------------------------- #
# Custom Strategy: Supertrend (implemented here, NOT added to codebase indicators)
# --------------------------------------------------------------------------- #
class SupertrendStrategy(Strategy):
    meta = StrategyMeta(
        "supertrend", "Supertrend(ATR multiplier) trend-following: LONG above, FLAT below."
    )

    def __init__(self, atr_period=10, multiplier=3.0, allow_short=False):
        self.atr_period = atr_period
        self.multiplier = multiplier
        self.allow_short = allow_short

    @property
    def params(self):
        return {"atr_period": self.atr_period, "multiplier": self.multiplier,
                "allow_short": self.allow_short}

    def generate(self, df):
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low).rename("hl"),
            (high - prev_close).abs().rename("hc"),
            (low - prev_close).abs().rename("lc"),
        ], axis=1).max(axis=1)
        atr_val = tr.ewm(alpha=1 / self.atr_period, adjust=False,
                         min_periods=self.atr_period).mean()
        hl2 = (high + low) / 2.0
        basic_upper = hl2 + self.multiplier * atr_val
        basic_lower = hl2 - self.multiplier * atr_val

        n = len(df)
        final_upper = pd.Series(np.nan, index=df.index, dtype=float)
        final_lower = pd.Series(np.nan, index=df.index, dtype=float)
        direction = pd.Series(0, index=df.index, dtype=int)

        for i in range(n):
            if i == 0 or np.isnan(atr_val.iloc[i]):
                continue
            # Initialize final bands on first valid bar
            if i == 1 or (np.isnan(final_upper.iloc[i - 1]) and np.isnan(final_lower.iloc[i - 1])):
                final_upper.iloc[i] = basic_upper.iloc[i]
                final_lower.iloc[i] = basic_lower.iloc[i]
            else:
                fu_prev = final_upper.iloc[i - 1]
                fl_prev = final_lower.iloc[i - 1]
                pc = close.iloc[i - 1]
                # Sticky upper band
                if (basic_upper.iloc[i] < fu_prev) or (pc > fu_prev):
                    final_upper.iloc[i] = basic_upper.iloc[i]
                else:
                    final_upper.iloc[i] = fu_prev
                # Sticky lower band
                if (basic_lower.iloc[i] > fl_prev) or (pc < fl_prev):
                    final_lower.iloc[i] = basic_lower.iloc[i]
                else:
                    final_lower.iloc[i] = fl_prev

            # Determine direction using final bands
            prev_dir = direction.iloc[i - 1] if i > 0 else 0
            if prev_dir == -1:
                # Was bearish, check for bullish flip
                if close.iloc[i] > final_upper.iloc[i]:
                    direction.iloc[i] = 1
                else:
                    direction.iloc[i] = -1
            elif prev_dir == 1:
                # Was bullish, check for bearish flip
                if close.iloc[i] < final_lower.iloc[i]:
                    direction.iloc[i] = -1
                else:
                    direction.iloc[i] = 1
            else:
                # Uninitialized, default to bullish if close > final_upper
                if close.iloc[i] > final_upper.iloc[i]:
                    direction.iloc[i] = 1
                else:
                    direction.iloc[i] = -1

        # Build supertrend line from final bands
        supertrend = pd.Series(np.nan, index=df.index, dtype=float)
        for i in range(n):
            if direction.iloc[i] == 1:
                supertrend.iloc[i] = final_lower.iloc[i]
            elif direction.iloc[i] == -1:
                supertrend.iloc[i] = final_upper.iloc[i]

        target = pd.Series(0, index=df.index, dtype=int)
        target[close > supertrend] = Signal.LONG
        if self.allow_short:
            target[close < supertrend] = Signal.SHORT
        return target
def sma_cross_spec(fast, slow):
    fk, sk = f"sma_{fast}", f"sma_{slow}"
    return StrategySpec.from_model_json({
        "name": f"SMA {fast}-{slow} crossover",
        "description": f"LONG when SMA({fast}) crosses above SMA({slow}); exit on cross below.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [
            {"name": "sma", "params": {"window": fast}},
            {"name": "sma", "params": {"window": slow}},
        ],
        "entry": make_condition(indicator_operand(fk), "crosses_above", indicator_operand(sk)),
        "exit": make_condition(indicator_operand(fk), "crosses_below", indicator_operand(sk)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": {"stop_loss_pct": 0.10, "take_profit_pct": 0.20},
    }, model="backtest-script")


def rsi_mr_spec(window, oversold, overbought):
    rk = f"rsi_{window}"
    return StrategySpec.from_model_json({
        "name": f"RSI {window} mean reversion",
        "description": (f"LONG when RSI {window} crosses above {oversold}; "
                        f"exit when RSI {window} > {overbought}."),
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [{"name": "rsi", "params": {"window": window}}],
        "entry": make_condition(indicator_operand(rk), "crosses_above",
                                const_operand(float(oversold))),
        "exit": make_condition(indicator_operand(rk), ">",
                               const_operand(float(overbought))),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": {"stop_loss_pct": 0.04, "take_profit_pct": 0.08},
    }, model="backtest-script")


def bb_mr_spec(window, num_std, rsi_window, rsi_oversold):
    ns = int(num_std) if float(num_std).is_integer() else num_std
    bb_ck = f"bb_lower_{window}_{ns}"
    bb_mk = f"bb_middle_{window}_{ns}"
    rk = f"rsi_{rsi_window}"
    return StrategySpec.from_model_json({
        "name": f"BB {window}-{ns} RSI {rsi_window} MR",
        "description": (f"LONG when close < lower BB AND RSI<{rsi_oversold}; "
                        f"exit when close > middle BB."),
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [
            {"name": "bb_lower", "params": {"window": window, "num_std": num_std}},
            {"name": "bb_middle", "params": {"window": window, "num_std": num_std}},
            {"name": "rsi", "params": {"window": rsi_window}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), "<", indicator_operand(bb_ck)),
            make_condition(indicator_operand(rk), "<", const_operand(float(rsi_oversold))),
        ),
        "exit": make_condition(field_operand("close"), ">", indicator_operand(bb_mk)),
        "position_sizing": {"max_allocation_pct": 0.5},
        "risk": {"stop_loss_pct": 0.05, "take_profit_pct": 0.10},
    }, model="backtest-script")


def macd_cross_spec(fast, slow, signal_sp):
    mk = f"macd_{fast}_{slow}_{signal_sp}"
    sk = f"macd_signal_{fast}_{slow}_{signal_sp}"
    return StrategySpec.from_model_json({
        "name": f"MACD {fast}-{slow}-{signal_sp} crossover",
        "description": "LONG when MACD line crosses above signal line; exit on cross below.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [
            {"name": "macd", "params": {"fast": fast, "slow": slow, "signal": signal_sp}},
            {"name": "macd_signal", "params": {"fast": fast, "slow": slow, "signal": signal_sp}},
        ],
        "entry": make_condition(indicator_operand(mk), "crosses_above",
                                indicator_operand(sk)),
        "exit": make_condition(indicator_operand(mk), "crosses_below",
                               indicator_operand(sk)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": {"stop_loss_pct": 0.05},
    }, model="backtest-script")


def sma_trend_spec(window):
    sk = f"sma_{window}"
    return StrategySpec.from_model_json({
        "name": f"SMA {window} trend filter",
        "description": f"LONG while close > SMA {window}; exit when close < SMA {window}.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [{"name": "sma", "params": {"window": window}}],
        "entry": make_condition(field_operand("close"), ">", indicator_operand(sk)),
        "exit": make_condition(field_operand("close"), "<", indicator_operand(sk)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": {"stop_loss_pct": 0.07},
    }, model="backtest-script")


def warmup(name, **kw):
    w = {
        "sma_cross": max(kw.get("fast", 50), kw.get("slow", 200)) + 5,
        "ema": max(kw.get("fast", 12), kw.get("slow", 26)) + 5,
        "rsi": kw.get("window", 14) * 2 + 5,
        "momentum": kw.get("window", 10) + 5,
        "breakout": kw.get("lookback", 20) + 5,
        "supertrend": kw.get("atr_period", 10) + 5,
        "sma_trend": kw.get("window", 20) + 5,
        "bb": kw.get("window", 20) * 2 + 5,
        "macd": kw.get("slow", 26) + kw.get("signal", 9) + 5,
    }
    return w.get(name, 50)


def run_all_backtests(dataset):
    cost_model = IndiaTransactionCostModel()
    seg = CostSegment.EQUITY_FUTURE

    configs = [
        ("SMA 50/200 Golden Cross", sma_cross_spec(50, 200), "sma_cross", {"fast": 50, "slow": 200}),
        ("EMA 12/26 Cross", EMATrendStrategy(fast=12, slow=26), "ema", {"fast": 12, "slow": 26}),
        ("RSI(14) Mean Reversion", rsi_mr_spec(14, 30, 55), "rsi", {"window": 14}),
        ("RSI(2) Mean Reversion", rsi_mr_spec(2, 10, 65), "rsi", {"window": 2}),
        ("10-bar Momentum (+2%)", MomentumStrategy(window=10, entry_thr=0.02, exit_thr=0.0), "momentum", {"window": 10}),
        ("Momentum 12-1", MomentumStrategy(window=12, entry_thr=0.0, exit_thr=-0.0), "momentum", {"window": 12}),
        ("BB(20,2) + RSI(14) MR", bb_mr_spec(20, 2.0, 14, 40), "bb", {"window": 20}),
        ("MACD(12,26,9) Crossover", macd_cross_spec(12, 26, 9), "macd", {"slow": 26, "signal": 9}),
        ("Supertrend(10,3)", SupertrendStrategy(atr_period=10, multiplier=3.0), "supertrend", {"atr_period": 10}),
        ("20-bar Breakout", BreakoutStrategy(lookback=20), "breakout", {"lookback": 20}),
        ("SMA20 Trend Filter", sma_trend_spec(20), "sma_trend", {"window": 20}),
    ]

    results = []
    for name, strat, wname, wparams in configs:
        w = warmup(wname, **wparams)
        print(f"--- {name} (warmup={w}) ---", end=" ")

        if isinstance(strat, StrategySpec):
            base = BacktestConfig(
                initial_capital=INITIAL_CAPITAL, slippage_pct=0.001,
                cost_model=cost_model, cost_segment=seg.value, warmup_bars=w,
            )
            cfg = merged_backtest_config(strat, base)
            strategy = build_strategy(strat)
        else:
            cfg = BacktestConfig(
                initial_capital=INITIAL_CAPITAL, slippage_pct=0.001,
                cost_model=cost_model, cost_segment=seg.value, warmup_bars=w,
            )
            strategy = strat

        try:
            result = run_backtest(dataset, strategy, cfg)
            perf = compute_performance(result)
            results.append((name, perf, result, cfg))
            print(f"Return={perf.total_return*100:.1f}% Sharpe={perf.sharpe:.2f} "
                  f"MaxDD={perf.max_drawdown*100:.1f}% Trades={perf.n_trades} "
                  f"Win%={perf.win_rate*100:.0f}% PF={perf.profit_factor or 0:.2f}")
        except Exception as e:
            print(f"ERROR: {e}")
            results.append((name, None, None, cfg))

    return results


def print_summary(results):
    rows = []
    for name, perf, result, cfg in results:
        if perf is None:
            rows.append([name, "ERR", "ERR", "ERR", "ERR", "ERR", "ERR", "ERR"])
        else:
            rows.append([
                name,
                f"{perf.total_return*100:.1f}%",
                f"{perf.sharpe:.2f}" if np.isfinite(perf.sharpe) else "N/A",
                f"{perf.sortino:.2f}" if np.isfinite(perf.sortino) else "N/A",
                f"{perf.max_drawdown*100:.1f}%",
                str(perf.n_trades),
                f"{perf.win_rate*100:.0f}%",
                f"{perf.profit_factor:.2f}" if perf.profit_factor and np.isfinite(perf.profit_factor) else "N/A",
            ])

    headers = ["Strategy", "Return", "Sharpe", "Sortino", "MaxDD", "Trades", "Win%", "PF"]
    print("\n" + "=" * 105)
    print("NIFTY 50 Single-Leg Strategy Backtest Results")
    print(f"Capital: Rs.{INITIAL_CAPITAL:,.0f}  Data: {CSV_PATH}")
    print("=" * 105)
    print(tabulate(rows, headers=headers, tablefmt="grid"))
    print("=" * 105)

    valid = [(n, p) for n, p, _, _ in results if p is not None]
    if valid:
        print("\nRanking by Sharpe Ratio (best risk-adjusted):")
        for i, (n, p) in enumerate(sorted(valid, key=lambda x: x[1].sharpe, reverse=True), 1):
            print(f"  {i}. {n}: Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
                  f"MaxDD={p.max_drawdown*100:.1f}% Trades={p.n_trades}")

        print("\nRanking by Total Return:")
        for i, (n, p) in enumerate(sorted(valid, key=lambda x: x[1].total_return, reverse=True), 1):
            print(f"  {i}. {n}: Return={p.total_return*100:.1f}% "
                  f"MaxDD={p.max_drawdown*100:.1f}% Trades={p.n_trades}")


if __name__ == "__main__":
    print("Loading Nifty 50 dataset...")
    ds = load_nifty_dataset()
    print(f"\nDataset: {ds.symbol} {ds.timeframe}, {len(ds.data)} rows")
    print(f"Date range: {ds.data.index[0].date()} to {ds.data.index[-1].date()}")
    print("\nRunning backtests...\n")
    results = run_all_backtests(ds)
    print_summary(results)
