"""Inactive AI-analyst interface (Day 7 — future consumption only).

Day 7 builds a clean *contract* for where an AI analyst could later consume research
outputs. This module is intentionally INACTIVE: no LLM is invoked, no trading rules
are generated, no metrics are computed by a model. It only structures the data an
analyst would read. The backtester and strategies never import or call into here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .backtester import BacktestResult
from .performance import PerformanceReport


@dataclass
class AnalysisSnapshot:
    """Read-only bundle an AI analyst could later consume (not used in Day 7)."""

    symbol: str
    timeframe: str
    contract_id: str
    strategy_name: str
    latest_signal: str = "FLAT"
    feature_snapshot: dict = field(default_factory=dict)
    performance: Optional[PerformanceReport] = None
    risk_state: dict = field(default_factory=dict)
    regime: str = "unknown"

    @classmethod
    def from_result(cls, result: BacktestResult, perf: PerformanceReport) -> "AnalysisSnapshot":
        return cls(
            symbol=result.dataset.symbol,
            timeframe=result.dataset.timeframe,
            contract_id=result.dataset.contract_id,
            strategy_name=result.strategy.meta.name,
            performance=perf,
        )
