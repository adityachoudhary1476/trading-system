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
]