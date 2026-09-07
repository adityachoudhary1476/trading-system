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
    "AutonomousBotResponse",
    "AutonomousLifecycleResponse",
    "AutonomousScanResponse",
    "AutonomousDecideResponse",
    "AutonomousDeploymentsResponse",
    "AutonomousEventsResponse",
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

    # --- Phase 8: Options contract metadata (optional) ---
    options_contract_id: Optional[str] = Field(
        default=None, description="Resolved options contract identifier (e.g. instrument.contract_id)"
    )
    strike: Optional[float] = Field(default=None, gt=0, description="Strike price of the option")
    expiry: Optional[str] = Field(default=None, description="ISO date expiry YYYY-MM-DD")
    option_type: Optional[str] = Field(default=None, description="Option right: 'CE' or 'PE'")


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
    is_idempotent_replay: bool = False

    # --- Phase 8: Options contract metadata (optional) ---
    options_contract_id: Optional[str] = None
    strike: Optional[float] = None
    expiry: Optional[str] = None
    option_type: Optional[str] = None
    idempotent: bool = False
    schema_version: int = 1


# --------------------------------------------------------------------------- #
# Phase 6 — Autonomous Trading Operations Center response models
# --------------------------------------------------------------------------- #
class AutonomousBotResponse(BaseModel):
    """Response for ``GET /autonomous/bot``."""

    model_config = ConfigDict(extra="forbid")

    bot: dict[str, Any]
    schema_version: int = 1


class AutonomousLifecycleResponse(BaseModel):
    """Response for ``POST /autonomous/bot/{action}``."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    message: str
    bot: dict[str, Any]
    schema_version: int = 1


class AutonomousScanResponse(BaseModel):
    """Response for ``GET /autonomous/scan``."""

    model_config = ConfigDict(extra="forbid")

    scan: dict[str, Any]
    ranking: Optional[dict[str, Any]] = None
    schema_version: int = 1


class AutonomousDecideResponse(BaseModel):
    """Response for ``POST /autonomous/decide``."""

    model_config = ConfigDict(extra="forbid")

    result_id: str
    scan_id: Optional[str] = None
    ranking_id: Optional[str] = None
    evaluated_at: str
    decisions: list[dict]
    valid_count: int
    rejected_count: int
    schema_version: int = 1


class AutonomousDeploymentsResponse(BaseModel):
    """Response for ``GET /autonomous/deployments``."""

    model_config = ConfigDict(extra="forbid")

    deployments: list[DashboardDeploymentSummary]
    count: int
    schema_version: int = 1


class AutonomousEventsResponse(BaseModel):
    """Response for ``GET /autonomous/events``."""

    model_config = ConfigDict(extra="forbid")

    events: list[dict]
    count: int
    schema_version: int = 1


# --------------------------------------------------------------------------- #
# Phase 8 — Options capability surface (Phase A, observational only)
# --------------------------------------------------------------------------- #
# Possible provider states. The string values are part of the public API
# contract — clients may branch on them.
PROVIDER_STATUS_AVAILABLE = "available"
PROVIDER_STATUS_UNAVAILABLE = "unavailable"
PROVIDER_STATUS_NOT_CONFIGURED = "not_configured"
PROVIDER_STATUS_DISABLED = "disabled"

# Single-leg capability phase is supported in the controller's option
# execution path; multi-leg requires additional phases and is reported
# separately.
OPTIONS_EXECUTION_PHASE = "single_leg_capability_surface"


class OptionsProviderStatus(BaseModel):
    """Status of a single option-data provider dependency.

    ``status`` is one of the ``PROVIDER_STATUS_*`` constants. ``detail``
    is a short, safe, human-readable explanation that never includes
    credentials, tokens, internal paths, or stack traces.
    """

    model_config = ConfigDict(extra="forbid")

    status: str
    detail: str = ""


class OptionsCapabilityResponse(BaseModel):
    """Capability surface for the deployment's options stack.

    This endpoint is strictly observational in Phase A. It does NOT
    place orders, does NOT enable autonomous execution, and does NOT
    register providers. It inspects the existing
    ``AutonomousController`` + ``PaperDeploymentConfig`` to report:

    * ``enabled`` — whether the deployment's configuration permits
      options (``PaperDeploymentConfig.options_enabled``).
    * ``allowed_option_types`` — the deployment's configured rights
      (verbatim from ``PaperDeploymentConfig.allowed_option_types``).
    * ``max_contracts_per_trade`` — the deployment's configured cap
      (verbatim from
      ``PaperDeploymentConfig.max_options_contracts_per_trade``).
    * ``providers`` — per-dependency status (``discoverer``,
      ``quote``, ``chain``). The synthetic
      ``InMemoryOptionsChainProvider`` is NEVER reported as production
      capability.
    * ``capable`` — whether the production option components required
      for single-leg capability reporting are present. This is a
      capability probe, NOT an indication that autonomous option
      execution is active.
    * ``last_error`` — last observed safe error string, if any.

    Wording matters: ``capable`` means *the backend has the components
    required to expose options data*; it does NOT mean *autonomous
    options trading is enabled*. The UI must keep this distinction.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    allowed_option_types: list[str] = Field(default_factory=list)
    max_contracts_per_trade: Optional[int] = None
    providers: dict[str, "OptionsProviderStatus"] = Field(default_factory=dict)
    capable: bool
    execution_phase: str = OPTIONS_EXECUTION_PHASE
    autonomous_execution_active: bool = False
    last_error: Optional[str] = None
    schema_version: int = 1