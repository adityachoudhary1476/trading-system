"""Phase 21 — typed API errors.

All API errors flow through the :class:`APIError` envelope so clients see
deterministic, well-formed error payloads. Python tracebacks and internal
implementation details (filesystem paths, SQL, etc.) are NEVER exposed.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class APIErrorCode(str, Enum):
    """Stable, documented API error codes.

    Codes are part of the public contract: clients can branch on them
    without parsing free-form messages.
    """

    # Generic
    BAD_REQUEST = "bad_request"
    NOT_FOUND = "not_found"
    INTERNAL_ERROR = "internal_error"
    METHOD_NOT_ALLOWED = "method_not_allowed"

    # Domain
    UNKNOWN_DEPLOYMENT = "unknown_deployment"
    UNKNOWN_STRATEGY = "unknown_strategy"
    UNKNOWN_SESSION = "unknown_session"
    INVALID_LIFECYCLE_TRANSITION = "invalid_lifecycle_transition"
    RETIRED_STRATEGY = "retired_strategy"
    REJECTED_STRATEGY = "rejected_strategy"
    INVALID_STRATEGY_SPEC = "invalid_strategy_spec"
    STRATEGY_HASH_MISMATCH = "strategy_hash_mismatch"
    SYMBOL_MISMATCH = "symbol_mismatch"
    TIMEFRAME_MISMATCH = "timeframe_mismatch"
    NON_PAPER_EXECUTION_MODE = "non_paper_execution_mode"
    CIRCUIT_BREAKER_OPEN = "circuit_breaker_open"
    INVALID_CHECKPOINT = "invalid_checkpoint"
    SCHEMA_MISMATCH = "schema_mismatch"
    CORRUPTED_PERSISTED_STATE = "corrupted_persisted_state"
    PAPER_BROKER_REQUIRED = "paper_broker_required"


# Map domain exceptions to API error codes / HTTP status codes.
# The router uses this table to translate domain-level exceptions into
# deterministic API errors.
from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:
    from ..paper.control import (  # noqa: F401
        ControlCenterError,
        InvalidLifecycleTransitionError,
        NotPaperModeError,
        PaperBrokerRequiredError,
        UnknownDeploymentError,
    )
    from ..paper.session import (  # noqa: F401
        SessionIdentityError,
        SessionSchemaError,
    )


# Tuple of (exception type, error code, http status).
DOMAIN_ERROR_MAP: list[tuple[type, APIErrorCode, int]] = [
    # Control center
    ("UnknownDeploymentError", APIErrorCode.UNKNOWN_DEPLOYMENT, 404),
    ("InvalidLifecycleTransitionError", APIErrorCode.INVALID_LIFECYCLE_TRANSITION, 409),
    ("PaperBrokerRequiredError", APIErrorCode.PAPER_BROKER_REQUIRED, 400),
    ("NotPaperModeError", APIErrorCode.NON_PAPER_EXECUTION_MODE, 400),
    ("ControlCenterError", APIErrorCode.BAD_REQUEST, 400),
    # Session
    ("SessionSchemaError", APIErrorCode.SCHEMA_MISMATCH, 409),
    ("SessionIdentityError", APIErrorCode.CORRUPTED_PERSISTED_STATE, 409),
]


def _resolve_class(name: str):
    """Resolve an exception class by name (avoids runtime import cycles)."""
    from ..paper import control as _ctl
    from ..paper import session as _sess

    table = {
        "UnknownDeploymentError": _ctl.UnknownDeploymentError,
        "InvalidLifecycleTransitionError": _ctl.InvalidLifecycleTransitionError,
        "PaperBrokerRequiredError": _ctl.PaperBrokerRequiredError,
        "NotPaperModeError": _ctl.NotPaperModeError,
        "ControlCenterError": _ctl.ControlCenterError,
        "SessionSchemaError": _sess.SessionSchemaError,
        "SessionIdentityError": _sess.SessionIdentityError,
    }
    return table.get(name)


for i, (name, code, status) in enumerate(list(DOMAIN_ERROR_MAP)):
    cls = _resolve_class(name)
    if cls is not None:
        DOMAIN_ERROR_MAP[i] = (cls, code, status)


class APIError(BaseModel):
    """Stable, JSON-serializable API error envelope."""

    model_config = ConfigDict(extra="forbid")

    code: APIErrorCode
    message: str
    details: Optional[dict] = None


class ErrorResponse(BaseModel):
    """Standard error response shape."""

    model_config = ConfigDict(extra="forbid")

    error: APIError
    request_id: Optional[str] = None
    timestamp: str = Field(default="")
    schema_version: int = 1


def make_error(
    code: APIErrorCode,
    message: str,
    *,
    details: Optional[dict[str, Any]] = None,
    status: int = 400,
) -> "APIErrorException":
    """Build a domain error exception carrying both the code and HTTP status."""
    return APIErrorException(code=code, message=message, details=details, status=status)


class APIErrorException(RuntimeError):
    """Exception type the router catches and translates into JSON responses.

    Carries the structured error payload so no internal details leak. Always
    include a stable ``code``; ``message`` is human-readable but is NOT
    parsed by clients.
    """

    def __init__(
        self,
        *,
        code: APIErrorCode,
        message: str,
        details: Optional[dict[str, Any]] = None,
        status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.status = status
        self.payload = APIError(code=code, message=message, details=details)


def map_domain_exception(exc: BaseException) -> APIErrorException:
    """Translate a domain exception into an :class:`APIErrorException`.

    If the exception is not registered, returns an INTERNAL_ERROR with a
    safe message (no traceback, no path, no SQL).
    """
    for cls, code, status in DOMAIN_ERROR_MAP:
        if isinstance(exc, cls):
            return APIErrorException(
                code=code,
                message=str(exc) or code.value,
                status=status,
            )
    return APIErrorException(
        code=APIErrorCode.INTERNAL_ERROR,
        message="internal error",
        status=500,
    )