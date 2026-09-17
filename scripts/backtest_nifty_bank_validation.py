#!/usr/bin/env python3
"""Round 6B: Cross-market validation — test top high-frequency strategies
on NIFTY 50 (^NSEI) and BANK NIFTY (^NSEBANK) over the last ~19 years
(2007-09-17 to 2026-03-12).

Tests the top strategies identified in Round 6:
  1. SMA 25 trend filter
  2. Momentum 15 (1.5% threshold)
  3. EMA 3-5 cross (no-risk)
  4. EMA 3-6 cross
  5. EMA 15 + Momentum(15) thr1% combined
"""
from __future__ import annotations

import warnings
import numpy as np
import pandas as pd
import yfinance as yf
from trading_system.research.backtester import BacktestConfig, run_backtest
from trading_system.research.costs import IndiaTransactionCostModel, Segment as CostSegment
from trading_system.research.dataset import HistoricalDataset
from trading_system.research.performance import compute_performance
from trading_system.research.strategies import (
    EMATrendStrategy, MomentumStrategy,
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

INITIAL_CAPITAL = 100_000.0


def download_dataset(symbol: str, yf_symbol: str) -> HistoricalDataset:
    print(f"  Downloading {symbol} ({yf_symbol})...")
    df = yf.download(yf_symbol, start="2007-01-01", end="2026-03-15", interval="1d", progress=False)
    df.index = pd.DatetimeIndex(df.index).tz_localize("UTC")
    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"
    })
    # Flatten MultiIndex columns (yfinance returns (col, ticker) tuples)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    print(f"  Loaded {len(df)} rows, {df.index[0].date()} to {df.index[-1].date()}")
    return HistoricalDataset(symbol=symbol, timeframe="1d", data=df, contract_id=symbol)


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


# === Spec builders (reuse from Round 6) ===

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
    }, model="round6b-script")


def ema_trend_momentum_spec(ema_window, mom_window, mom_threshold, stop=None, take=None):
    ema_k = f"ema_{ema_window}"
    mom_k = f"momentum_{mom_window}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"EMA {ema_window} trend and Mom{mom_window} thr{int(mom_threshold*100)}pct",
        "description": f"LONG while EMA({ema_window}) > close and momentum({mom_window}) > {mom_threshold*100:.1f}%. Exit both conditions break.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
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
    }, model="round6b-script")


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
    }, model="round6b-script")


# === Top strategies from Round 6 ===

def get_strategy_set(dataset):
    """Return list of (name, callable, is_spec, warmup) for top Round 6 strategies."""
    return [
        # 1. SMA 25 trend filter (Round 6 top: Sharpe=0.43, Return=133.4%)
        ("SMA 25 trend filter", sma_trend_spec(25, stop=0.05, take=0.10), True, 30, None),

        # 2. Momentum 15, 1.5% threshold (Round 6: Sharpe=0.40, Return=86.1%)
        ("Momentum 15 thr1.5pct", MomentumStrategy(window=15, entry_thr=0.015, exit_thr=-0.015),
         False, 20, {"stop_loss_pct": 0.05, "take_profit_pct": 0.10}),

        # 3. EMA 3-5 no-risk cross (Round 6: Sharpe=0.40, Return=95.3%)
        ("EMA 3-5 no-risk", ema_cross_spec(3, 5, stop=None, take=None), True, 10, None),

        # 4. EMA 3-6 cross (Round 6: Sharpe=0.40, Return=101.2%)
        ("EMA 3-6 SL5TP10", ema_cross_spec(3, 6, stop=0.05, take=0.10), True, 11, None),

        # 5. EMA 15 + Mom(15) thr1% combined (Round 6: Sharpe=0.38, Return=76.2%)
        ("EMA 15 and Mom15 thr1pct", ema_trend_momentum_spec(15, 15, 0.01, stop=0.05, take=0.10),
         True, 20, None),
    ]


def run_validation(dataset, label):
    print(f"\n{'='*80}")
    print(f"  VALIDATING on {label} ({dataset.symbol})")
    print(f"{'='*80}")
    results = []
    for name, strat, is_spec, wb, risk_dict in get_strategy_set(dataset):
        print(f"  {name} (wb={wb})...", end=" ")
        if is_spec:
            perf, result, cfg = run_spec(dataset, strat, wb)
        else:
            perf, result, cfg = run_builtin(
                dataset, strat, wb,
                risk_dict.get("stop_loss_pct") if risk_dict else None,
                risk_dict.get("take_profit_pct") if risk_dict else None,
            )
        if perf is not None:
            avg_bars = len(result.equity_curve) / max(perf.n_trades, 1) if perf.n_trades > 0 else float('inf')
            print(f"OK  Return={perf.total_return*100:.1f}% Sharpe={perf.sharpe:.2f} "
                  f"Trades={perf.n_trades} AvgBars={avg_bars:.1f} PF={perf.profit_factor or 0:.2f}")
            results.append((name, perf, result))
        else:
            print("ERR")
    return results


def print_cross_market_summary(nifty_results, bank_results):
    print(f"\n{'='*100}")
    print(f"  CROSS-MARKET VALIDATION: NIFTY 50 vs BANK NIFTY (~19 years, 2007-2026)")
    print(f"{'='*100}")

    headers = ["Strategy", "NIFTY Return", "NIFTY Sharpe", "NIFTY AvgBars", "BANK Return", "BANK Sharpe", "BANK AvgBars"]
    rows = []
    for nr, br in zip(nifty_results, bank_results):
        n_name, n_perf, n_res = nr
        b_name, b_perf, b_res = br
        n_ab = len(n_res.equity_curve) / max(n_perf.n_trades, 1) if n_perf.n_trades > 0 else float('inf')
        b_ab = len(b_res.equity_curve) / max(b_perf.n_trades, 1) if b_perf.n_trades > 0 else float('inf')
        rows.append([
            n_name,
            f"{n_perf.total_return*100:.1f}%",
            f"{n_perf.sharpe:.2f}" if np.isfinite(n_perf.sharpe) else "N/A",
            f"{n_ab:.1f}",
            f"{b_perf.total_return*100:.1f}%",
            f"{b_perf.sharpe:.2f}" if np.isfinite(b_perf.sharpe) else "N/A",
            f"{b_ab:.1f}",
        ])

    print(tabulate(rows, headers=headers, tablefmt="grid"))
    print(f"{'='*100}")

    # Summary insights
    print("\n--- Insights ---")
    for i, (nr, br) in enumerate(zip(nifty_results, bank_results)):
        n_name, n_perf, n_res = nr
        b_name, b_perf, b_res = br
        n_sh = n_perf.sharpe if np.isfinite(n_perf.sharpe) else 0
        b_sh = b_perf.sharpe if np.isfinite(b_perf.sharpe) else 0
        n_ret = n_perf.total_return * 100
        b_ret = b_perf.total_return * 100

        if n_sh > 0.3 and b_sh > 0.3:
            verdict = "ROBUST: Both markets profitable + Sharpe > 0.3"
        elif n_sh > 0.3 or b_sh > 0.3:
            verdict = "PARTIAL: Profitable on one market only"
        else:
            verdict = "WEAK: Neither market shows strong Sharpe"

        print(f"  {n_name}: NIFTY Sharpe={n_sh:.2f}/{n_ret:.0f}% | BANK Sharpe={b_sh:.2f}/{b_ret:.0f}% -> {verdict}")


if __name__ == "__main__":
    print("Loading datasets via yfinance...\n")
    nifty_ds = download_dataset("NSE:NIFTY", "^NSEI")
    bank_ds = download_dataset("NSE:BANKNIFTY", "^NSEBANK")

    ny_results = run_validation(nifty_ds, "NIFTY 50 (^NSEI)")
    bk_results = run_validation(bank_ds, "BANK NIFTY (^NSEBANK)")

    print_cross_market_summary(ny_results, bk_results)
