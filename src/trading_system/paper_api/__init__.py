"""Phase 21 — Control-Center API Surface.

This package is a thin, paper-only HTTP/RPC adapter over the existing
``PaperTradingControlCenter`` (Phase 20). It is intentionally narrow:

  * It does NOT implement order placement.
  * It does NOT introduce a new execution engine.
  * It does NOT bypass ``DeploymentGate`` or ``PaperBroker`` enforcement.
  * It does NOT accept or persist credentials.
  * It does NOT dynamically import or instantiate live brokers.

Public surface:

  * :class:`PaperAPIRouter`        — pure dispatch (testable without sockets)
  * :class:`CheckpointPolicy`      — explicit, caller-driven checkpoint policy
  * :class:`APIError`              — typed error envelope
  * :class:`APIServer`             — stdlib ``http.server`` adapter
  * typed request/response models (:mod:`paper_api.models`)

Phase 21 does not implement live trading.
"""
from __future__ import annotations

from .checkpoint_policy import (
    CheckpointDecision,
    CheckpointPolicy,
    evaluate_checkpoint_policy,
)
from .errors import APIError, APIErrorCode, APIErrorException
from .models import (
    CheckpointRequest,
    DeploymentListResponse,
    DeploymentResponse,
    ErrorResponse,
    HealthResponse,
    LifecycleRequest,
    RestoreRequest,
    SessionResponse,
)
from .router import PaperAPIRouter, RouteHandler
from .server import APIServer, build_default_server

__all__ = [
    "APIError",
    "APIErrorCode",
    "APIErrorException",
    "CheckpointDecision",
    "CheckpointPolicy",
    "CheckpointRequest",
    "DeploymentListResponse",
    "DeploymentResponse",
    "ErrorResponse",
    "HealthResponse",
    "LifecycleRequest",
    "PaperAPIRouter",
    "RestoreRequest",
    "RouteHandler",
    "SessionResponse",
    "APIServer",
    "build_default_server",
    "evaluate_checkpoint_policy",
]