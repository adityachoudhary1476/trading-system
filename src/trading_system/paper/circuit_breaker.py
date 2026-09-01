"""Phase 19 — Paper circuit breaker.

A deployment-specific, deterministic circuit breaker for the paper-operations
layer. It does NOT replace the health monitor or risk guard — it is the
mechanism those components (or the runner) can use to force ``NO_ACTION``.

States:
    CLOSED  — normal operation; the runner may process bars.
    OPEN    — tripped; the runner must emit NO_ACTION and record the reason.

There is NO automatic recovery. Resetting to CLOSED requires explicit caller
action (``reset``), so a halted deployment can never silently resume.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional


class CircuitState(str, Enum):
    """Circuit-breaker states."""

    CLOSED = "closed"
    OPEN = "open"


class PaperCircuitBreaker:
    """Deployment-specific paper-only circuit breaker.

    Pure in-memory state. Never touches the broker, never places orders.
    """

    def __init__(self) -> None:
        self._state: CircuitState = CircuitState.CLOSED
        self._reason: Optional[str] = None
        self._trips: int = 0

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state == CircuitState.OPEN

    @property
    def reason(self) -> Optional[str]:
        return self._reason

    @property
    def trip_count(self) -> int:
        return self._trips

    def allowed_to_trade(self) -> bool:
        return self._state == CircuitState.CLOSED

    # ------------------------------------------------------------------ #
    # Transitions
    # ------------------------------------------------------------------ #
    def trip(self, reason: str) -> None:
        """Open the circuit. Idempotent: re-tripping keeps it OPEN.

        The first trip reason is preserved so the root cause is never hidden by
        later trips.
        """
        if self._state == CircuitState.CLOSED:
            self._state = CircuitState.OPEN
            self._reason = str(reason)
        self._trips += 1

    def reset(self) -> None:
        """Explicitly close the circuit. Requires caller intent."""
        self._state = CircuitState.CLOSED
        self._reason = None
