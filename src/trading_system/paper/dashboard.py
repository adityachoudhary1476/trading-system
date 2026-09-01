"""Phase 20 — Paper Control Center dashboard snapshot.

A single, typed, JSON-serializable aggregate object describing the live
state of one paper deployment through the Control Center. This is the
backend foundation for a future UI/API; no frontend is built in Phase 20.

The snapshot is intentionally read-only. Every nested model is a pydantic
v2 ``BaseModel`` with ``extra="forbid"`` and is built only from public
project abstractions (runner, broker, operations layer, evidence store,
deployment registry). It must never expose:

  * broker internals (``_orders``, mutable state)
  * credentials, API keys, or secrets (no environment file content)
  * live broker objects (only ``PaperBroker`` is permitted by Phase 20)
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..research.evidence import StrategyEvidence
from .circuit_breaker import CircuitState
from .deployment import PaperDeployment, PaperDeploymentStatus
from .operations import HealthStatus
from .session import PaperSession, SESSION_SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# Sub-blocks
# --------------------------------------------------------------------------- #
class DashboardDeploymentSummary(BaseModel):
    """Deployment identity block — required for any dashboard rendering."""

    model_config = ConfigDict(extra="forbid")

    deployment_id: str
    strategy_id: str
    strategy_spec_hash: str
    symbol: str
    timeframe: str
    execution_mode: str
    dataset_id: str
    status: str
    created_at: str
    activated_at: Optional[str] = None
    updated_at: str
    notes: str = ""
    schema_version: int = 1


class DashboardStrategySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    spec_hash: str
    symbol: str
    timeframe: str
    lifecycle_status: str
    generated_by: str
    name: str
    description: str = ""
    parent_strategy_id: str = ""
    latest_evidence_at: Optional[str] = None
    latest_research_evidence_id: Optional[str] = None
    latest_walk_forward_evidence_id: Optional[str] = None
    latest_paper_evidence_id: Optional[str] = None


class DashboardAccountBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initial_cash: float
    cash: float
    equity: float
    margin_used: float
    available_cash: float
    realized_pnl: float
    unrealized_pnl: float
    starting_equity: Optional[float] = None
    total_return: Optional[float] = None


class DashboardPositionsBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    open_position: Optional[dict] = None
    is_flat: bool = True


class DashboardPerformanceBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    realized_pnl: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    total_pnl: Optional[float] = None
    return_: Optional[float] = Field(default=None, alias="return")
    drawdown: Optional[float] = None
    trade_count: int = 0
    win_rate: Optional[float] = None
    profit_factor: Optional[float] = None
    exposure: Optional[float] = None
    health_status: str = HealthStatus.HEALTHY.value
    orders_submitted: int = 0
    fills_received: int = 0
    rejected_orders: int = 0
    generated_signals: int = 0
    bar_count: int = 0


class DashboardHealthBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = HealthStatus.HEALTHY.value
    halt_reason: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)


class DashboardRiskBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = "allow"
    reason: Optional[str] = None


class DashboardCircuitBreakerBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str = CircuitState.CLOSED.value
    reason: Optional[str] = None
    trip_count: int = 0


class DashboardEventSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_events: int = 0
    last_event_sequence: int = -1
    last_event_type: Optional[str] = None
    last_event_timestamp: Optional[str] = None
    recent: list[dict] = Field(default_factory=list)


class DashboardEvidenceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_count: int = 0
    walk_forward_count: int = 0
    paper_trading_count: int = 0
    latest_research_evidence_id: Optional[str] = None
    latest_walk_forward_evidence_id: Optional[str] = None
    latest_paper_trading_evidence_id: Optional[str] = None
    latest_research_at: Optional[str] = None
    latest_walk_forward_at: Optional[str] = None
    latest_paper_trading_at: Optional[str] = None


# --------------------------------------------------------------------------- #
# Top-level snapshot
# --------------------------------------------------------------------------- #
class PaperControlCenterSnapshot(BaseModel):
    """Phase 20 — single, typed, JSON-serializable dashboard snapshot.

    ``schema_version`` is the Phase 20 schema (1). ``session_schema_version``
    mirrors the running session schema so consumers can detect cross-version
    payloads.
    """

    model_config = ConfigDict(extra="forbid")

    generated_at: str
    deployment: DashboardDeploymentSummary
    strategy: Optional[DashboardStrategySummary] = None
    session: PaperSession
    account: DashboardAccountBlock
    positions: DashboardPositionsBlock
    performance: DashboardPerformanceBlock
    health: DashboardHealthBlock
    risk: DashboardRiskBlock
    circuit_breaker: DashboardCircuitBreakerBlock
    recent_events: DashboardEventSummary
    evidence_summary: DashboardEvidenceSummary
    schema_version: int = 1
    session_schema_version: int = SESSION_SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def build_deployment_summary(deployment: PaperDeployment) -> DashboardDeploymentSummary:
    return DashboardDeploymentSummary(
        deployment_id=deployment.deployment_id,
        strategy_id=deployment.strategy_id,
        strategy_spec_hash=deployment.strategy_spec_hash,
        symbol=deployment.symbol,
        timeframe=deployment.timeframe,
        execution_mode=deployment.config.execution_mode,
        dataset_id=deployment.dataset_id,
        status=deployment.status.value,
        created_at=deployment.created_at or "",
        activated_at=deployment.activated_at,
        updated_at=deployment.updated_at or "",
        notes=deployment.notes or "",
    )


def build_strategy_summary(
    *,
    strategy,
    research_evidences: list[StrategyEvidence],
    walk_forward_evidences: list[StrategyEvidence],
    paper_evidences: list[StrategyEvidence],
) -> DashboardStrategySummary:
    latest_paper = _latest_of(paper_evidences)
    latest_research = _latest_of(research_evidences)
    latest_wf = _latest_of(walk_forward_evidences)
    latest_at = _latest_at(research_evidences, walk_forward_evidences, paper_evidences)
    # Use a benign attribute lookup that never falls back to live-broker internals.
    parent = ""
    if hasattr(strategy, "parent_strategy_id"):
        try:
            parent = strategy.parent_strategy_id or ""
        except Exception:
            parent = ""
    return DashboardStrategySummary(
        strategy_id=strategy.strategy_id,
        spec_hash=strategy.spec_hash,
        symbol=strategy.symbol,
        timeframe=strategy.timeframe,
        lifecycle_status=strategy.status.value,
        generated_by=strategy.generated_by,
        name=strategy.name,
        description=strategy.description or "",
        parent_strategy_id=parent,
        latest_evidence_at=latest_at,
        latest_research_evidence_id=(latest_research.evidence_id if latest_research else None),
        latest_walk_forward_evidence_id=(latest_wf.evidence_id if latest_wf else None),
        latest_paper_evidence_id=(latest_paper.evidence_id if latest_paper else None),
    )


def _latest_of(evidences: list[StrategyEvidence]) -> Optional[StrategyEvidence]:
    if not evidences:
        return None
    return sorted(
        evidences,
        key=lambda e: (e.created_at or "", e.evidence_id),
        reverse=True,
    )[0]


def _latest_at(*groups: list[StrategyEvidence]) -> Optional[str]:
    best: Optional[str] = None
    for g in groups:
        for e in g:
            if e.created_at and (best is None or e.created_at > best):
                best = e.created_at
    return best