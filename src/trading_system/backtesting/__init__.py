"""Backtesting engine. NOT implemented on Day 1.

Reserved for replaying signals/strategies over stored historical data once a
signal generator and risk manager exist. Placeholder only.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BacktestResult:
    strategy: str = ""
    total_return: float = 0.0
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    trades: int = 0


def run_backtest(*args, **kwargs) -> BacktestResult:
    raise NotImplementedError("Backtesting is a Day 2+ component.")
