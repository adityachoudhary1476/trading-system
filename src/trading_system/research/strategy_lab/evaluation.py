"""Deterministic strategy evaluation (Phase 13, Step 7).

Turns a BacktestResult into a StrategyEvaluation — a flat, serializable summary
of the metrics the EXISTING data actually supports:

  net P&L, total return, trade count, winners/losers, win rate, average trade,
  max drawdown, transaction costs, slippage (deterministic recomputation of the
  component already embedded in fills), exposure.

Sharpe/Sortino/profit-factor are passed through from the existing
``compute_performance`` (Day 7) — they are reported as None with an explicit
``unavailable_metrics`` entry when the sample cannot defensibly support them
(e.g. zero trades). No metric is fabricated.

An evaluation is a DESCRIPTION of a historical simulation. It is not a forecast.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from ..backtester import BacktestResult
from ..performance import compute_performance
from ..strategies import Strategy
from .spec import StrategySpec

__all__ = ["StrategyEvaluation", "evaluate_result", "evaluate_spec"]


@dataclass
class StrategyEvaluation:
    spec_name: str
    symbol: str
    timeframe: str
    generated_by: str = ""

    initial_capital: float = 0.0
    final_capital: float = 0.0
    net_pnl: float = 0.0
    total_return: float = 0.0

    n_trades: int = 0
    winning: int = 0
    losing: int = 0
    win_rate: Optional[float] = None
    avg_trade: Optional[float] = None
    avg_trade_return: Optional[float] = None

    max_drawdown: float = 0.0
    transaction_costs: float = 0.0
    slippage_estimate: float = 0.0
    exposure_pct: float = 0.0

    profit_factor: Optional[float] = None
    sharpe: Optional[float] = None
    sortino: Optional[float] = None

    reliable: bool = False
    notes: list[str] = field(default_factory=list)
    unavailable_metrics: list[str] = field(default_factory=list)


def _slippage_costs(result: BacktestResult) -> float:
    """Deterministically recompute the slippage component embedded in fills.

    The backtester fills entries at raw*(1±slip) and exits at raw*(1∓slip).
    Inverting that algebra gives the exact cost slippage contributed per leg:
      a leg paying the adverse side of slip has cost = qty*fill*slip/(1±slip).
    With slippage_pct == 0 this is exactly 0.0.
    """
    slip = float(result.config.slippage_pct)
    if slip <= 0.0:
        return 0.0
    total = 0.0
    for t in result.trades:
        if t.direction == 1:  # long: bought above raw, sold below raw
            total += t.quantity * t.entry_price * slip / (1.0 + slip)
            total += t.quantity * t.exit_price * slip / (1.0 - slip)
        else:  # short: sold below raw, bought back above raw
            total += t.quantity * t.entry_price * slip / (1.0 - slip)
            total += t.quantity * t.exit_price * slip / (1.0 + slip)
    return total


def evaluate_result(result: BacktestResult, strategy_name: str = "") -> StrategyEvaluation:
    """Evaluate a BacktestResult deterministically (no fabrication)."""
    perf = compute_performance(result)
    n = perf.n_trades

    unavailable: list[str] = []
    win_rate = perf.win_rate if n else None
    avg_trade = (sum(t.net_pnl for t in result.trades) / n) if n else None
    avg_trade_ret = perf.avg_trade_return if n else None
    if n == 0:
        unavailable += ["win_rate", "avg_trade", "avg_trade_return",
                        "profit_factor", "sharpe", "sortino"]

    # Profit factor: undefined (None) when there are no losers or no trades —
    # an infinite ratio is not serializable and not a number we rank on blindly.
    profit_factor = perf.profit_factor
    pf_note = None
    if n == 0 or profit_factor is None or not math.isfinite(profit_factor):
        profit_factor = None
        pf_note = "profit factor undefined (no losing trades or no trades)"
        if "profit_factor" not in unavailable:
            unavailable.append("profit_factor")

    notes = list(perf.notes)
    if pf_note:
        notes.append(pf_note)

    strategy = result.strategy
    return StrategyEvaluation(
        spec_name=strategy_name or getattr(getattr(strategy, "meta", None), "name", ""),
        symbol=result.dataset.symbol,
        timeframe=result.dataset.timeframe,
        generated_by="",  # provenance is attached by evaluate_spec
        initial_capital=result.initial_capital,
        final_capital=result.final_capital,
        net_pnl=result.net_pnl,
        total_return=result.total_return,
        n_trades=n,
        winning=perf.winning,
        losing=perf.losing,
        win_rate=win_rate,
        avg_trade=avg_trade,
        avg_trade_return=avg_trade_ret,
        max_drawdown=perf.max_drawdown,
        transaction_costs=float(sum(t.costs for t in result.trades)),
        slippage_estimate=_slippage_costs(result),
        exposure_pct=perf.exposure_pct,
        profit_factor=profit_factor,
        sharpe=perf.sharpe if n else None,
        sortino=perf.sortino if n else None,
        reliable=perf.reliable,
        notes=notes,
        unavailable_metrics=unavailable,
    )


def evaluate_spec(spec: StrategySpec, result: BacktestResult) -> StrategyEvaluation:
    """Evaluate a backtest that was run for a specific StrategySpec."""
    evaluation = evaluate_result(result, strategy_name=spec.name)
    evaluation.generated_by = spec.generated_by
    return evaluation


def evaluate_strategy(strategy: Strategy, result: BacktestResult) -> StrategyEvaluation:
    """Evaluate a backtest for any existing Strategy (e.g. EMATrendStrategy)."""
    return evaluate_result(result, strategy_name=strategy.meta.name)

