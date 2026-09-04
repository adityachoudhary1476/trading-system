"""Phase 21 — typed API request / response models.

These are thin pydantic envelopes over the existing Phase 20 dashboard
models. The API layer never invents parallel representations of paper
state; the dashboard snapshot is the single source of truth for read
responses, and lifecycle / checkpoint operations only carry the minimum
metadata needed to identify a target.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..paper.dashboard import (
    DashboardAccountBlock,
    DashboardCircuitBreakerBlock,
    DashboardDeploymentSummary,
    DashboardEventSummary,
    DashboardEvidenceSummary,
    DashboardHealthBlock,
    DashboardPerformanceBlock,
    DashboardPositionsBlock,
    DashboardRiskBlock,
    DashboardStrategySummary,
)
from ..paper.session import PaperSession, PaperSessionCheckpoint
from .errors import APIError, ErrorResponse


# --------------------------------------------------------------------------- #
# Deployment responses
# --------------------------------------------------------------------------- #
class DeploymentResponse(BaseModel):
    """Response shape for a single deployment detail / summary endpoint."""

    model_config = ConfigDict(extra="forbid")

    deployment: DashboardDeploymentSummary
    schema_version: int = 1


class DeploymentListResponse(BaseModel):
    """Response shape for the deployments list endpoint."""

    model_config = ConfigDict(extra="forbid")

    deployments: list[DashboardDeploymentSummary]
    count: int
    schema_version: int = 1


# --------------------------------------------------------------------------- #
# Creation request / response
# --------------------------------------------------------------------------- #
class DeploymentCreateRequest(BaseModel):
    """Request body for ``POST /deployments``.

    Accepts either a full ``StrategySpec`` dict (``spec``) or a
    ``strategy_id`` referencing an already-registered strategy. A
    ``DatasetId`` defaults to ``"market_data"``. ``config`` accepts an
    optional partial :class:`PaperDeploymentConfig` override (all fields
    paper-only; live execution is never permitted).
    """

    model_config = ConfigDict(extra="forbid")

    spec: Optional[dict[str, Any]] = Field(
        default=None,
        description="Full StrategySpec JSON dict. Required if strategy_id is not provided.",
    )
    strategy_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Reference to an already-registered strategy. Ignored if spec is provided.",
    )
    symbol: Optional[str] = Field(default=None, min_length=1)
    timeframe: Optional[str] = Field(default=None, min_length=1)
    dataset_id: Optional[str] = Field(default=None, min_length=1)
    config: Optional[dict[str, Any]] = Field(default=None, description="PaperDeploymentConfig override fields")


class DeploymentCreateResponse(BaseModel):
    """Response body for ``POST /deployments``.

    Returns the created deployment summary together with the live session id
    the frontend can navigate to.
    """

    model_config = ConfigDict(extra="forbid")

    deployment: DashboardDeploymentSummary
    session_id: str
    schema_version: int = 1


# --------------------------------------------------------------------------- #
# Lifecycle / session request models
# --------------------------------------------------------------------------- #
class LifecycleRequest(BaseModel):
    """Optional payload for lifecycle endpoints. Body is reserved for
    future use (e.g. a transition reason); Phase 21 leaves it empty so
    that transitions remain explicit caller-driven actions.
    """

    model_config = ConfigDict(extra="forbid")

    reason: Optional[str] = None


class CheckpointRequest(BaseModel):
    """Body for ``POST /deployments/{id}/checkpoint``.

    Empty for now; future fields can be added (e.g. ``label``). Checkpoints
    remain explicit and caller-driven.
    """

    model_config = ConfigDict(extra="forbid")

    label: Optional[str] = None


class RestoreRequest(BaseModel):
    """Body for ``POST /deployments/{id}/restore``.

    The caller may optionally pre-attach a runner. The default behaviour is
    to rebuild operational state into the control center's existing live
    runner for this deployment, if any.
    """

    model_config = ConfigDict(extra="forbid")

    attach_runner: bool = False


# --------------------------------------------------------------------------- #
# Session / checkpoint responses
# --------------------------------------------------------------------------- #
class SessionResponse(BaseModel):
    """Response shape for session / checkpoint endpoints."""

    model_config = ConfigDict(extra="forbid")

    session: PaperSession
    checkpoint: Optional[PaperSessionCheckpoint] = None
    schema_version: int = 1


# --------------------------------------------------------------------------- #
# Inspection responses — one per inspection block
# --------------------------------------------------------------------------- #
class AccountResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: DashboardAccountBlock
    schema_version: int = 1


class PositionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    positions: DashboardPositionsBlock
    schema_version: int = 1


class PerformanceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    performance: DashboardPerformanceBlock
    schema_version: int = 1


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    health: DashboardHealthBlock
    schema_version: int = 1


class RiskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk: DashboardRiskBlock
    schema_version: int = 1


class CircuitBreakerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    circuit_breaker: DashboardCircuitBreakerBlock
    schema_version: int = 1


class EventsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: DashboardEventSummary
    schema_version: int = 1


class EvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence: DashboardEvidenceSummary
    schema_version: int = 1


# --------------------------------------------------------------------------- #
# Health / dashboard
# --------------------------------------------------------------------------- #
class HealthEndpointResponse(BaseModel):
    """``GET /health`` response. Always returns 200 unless the API itself
    cannot run. The control center's per-deployment health lives in
    :class:`HealthResponse`."""

    model_config = ConfigDict(extra="forbid")

    status: str = "ok"
    phase: str = "phase-21-control-center-api"
    paper_only: bool = True
    schema_version: int = 1


# Re-export the error models for convenience so callers only need one import.
__all__ = [
    "APIError",
    "ErrorResponse",
    "HealthEndpointResponse",
    "DeploymentListResponse",
    "DeploymentResponse",
    "DeploymentCreateRequest",
    "DeploymentCreateResponse",
    "LifecycleRequest",
    "CheckpointRequest",
    "RestoreRequest",
    "SessionResponse",
    "AccountResponse",
    "PositionsResponse",
    "PerformanceResponse",
    "HealthResponse",
    "RiskResponse",
    "CircuitBreakerResponse",
    "EventsResponse",
    "EvidenceResponse",
    "OrderIntentRequest",
    "OrderIntentResponse",
]


# --------------------------------------------------------------------------- #
# External order-intent (Day-13 autonomous-agent boundary)
# --------------------------------------------------------------------------- #
class OrderIntentRequest(BaseModel):
    """Request body for ``POST /deployments/{id}/orders``.

    This is the ONLY way an external caller can submit a paper-trading order.
    Every field maps 1:1 to the existing ``OrderIntent`` type accepted by
    ``PaperTradingControlCenter.submit_order_intent``.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., min_length=1, description="Trading symbol (e.g. 'AAPL')")
    side: str = Field(..., description="Either 'BUY' or 'SELL'")
    quantity: float = Field(..., gt=0, description="Positive number of shares/contracts")
    order_type: str = Field(default="MARKET", description="MARKET or LIMIT")
    limit_price: Optional[float] = Field(
        default=None, description="Required for LIMIT orders; ignored for MARKET"
    )
    client_order_id: Optional[str] = Field(
        default=None, min_length=1, max_length=128,
        description="Caller-supplied idempotency key. If set, retries with the "
                    "same key return the original result without creating a "
                    "duplicate order/fill.",
    )
    current_price: Optional[float] = Field(
        default=None, gt=0,
        description="Reference market price for MARKET fills. Required when the "
                    "deployment has no live price feed.",
    )


class OrderIntentResponse(BaseModel):
    """Response body for ``POST /deployments/{id}/orders``.

    Always carries the order status so agents can distinguish accepted,
    partially-filled, rejected, and idempotent-retry responses.
    """

    model_config = ConfigDict(extra="forbid")

    order_id: str = ""
    client_order_id: Optional[str] = None
    symbol: str
    side: str
    quantity: float
    order_type: str
    limit_price: Optional[float]
    status: str
    filled_quantity: float
    avg_fill_price: float
    fills: list[dict]
    cash_after: Optional[float]
    equity_after: Optional[float]
    realized_pnl_after: Optional[float]
    unrealized_pnl_after: Optional[float]
    position_qty_after: Optional[float]
    reject_reason: str = ""
    idempotent: bool = False
    schema_version: int = 1