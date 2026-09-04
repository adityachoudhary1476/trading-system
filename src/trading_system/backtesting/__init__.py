"""Backtesting engine.

Re-exports the deterministic, provider-independent backtester from
``research/backtester.py``. The implementation supports:
  * Causal signal replay (enter at next-bar open, no look-ahead).
  * Transaction costs + slippage (generic pct or India-specific cost model).
  * Walk-forward / out-of-sample train-test splits.
  * Max drawdown, Sharpe/Sortino, profit factor, win rate, exposure.

Usage (analysis / paper only — no live orders):

    from trading_system.backtesting import run_backtest, BacktestConfig, BacktestResult
    result = run_backtest(dataset, strategy, BacktestConfig(initial_capital=1_00_000))
    perf = compute_performance(result)
"""
from __future__ import annotations

from ..research.backtester import (
    BacktestConfig,
    BacktestResult,
    Trade,
    run_backtest,
)
from ..research.performance import (
    PerformanceReport,
    compute_performance,
)
from ..research.risk import RiskConfig

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "Trade",
    "RiskConfig",
    "run_backtest",
    "PerformanceReport",
    "compute_performance",
]
