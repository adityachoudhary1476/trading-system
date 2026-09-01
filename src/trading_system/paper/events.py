"""Phase 19 — Paper operations event model + append-only event log.

An audit trail for the paper-operations layer. Every operational state change,
signal, order, fill, rejection, risk warning, health warning, pause/resume/halt
is recorded exactly once, in order, with a deterministic identity.

Design:
  * Append-only. Events are never mutated or deleted after being recorded.
  * Deterministic identity: ``event_id`` is a SHA-256 of the immutable event
    content (deployment_id, sequence, event_type, timestamp, payload), so the
    same logical event always hashes to the same id. No wall-clock time or
    randomness is used anywhere in the identity or ordering.
  * Timestamps come from the bar stream (``pd.Timestamp`` -> ISO), never from
    the wall clock, so identical replays produce identical event logs.
  * No credentials, secrets, or arbitrary Python objects are ever stored.
"""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from .deployment import PaperDeployment


class PaperOperationEventType(str, Enum):
    """Stable, documented paper-operations event types."""

    DEPLOYMENT_CREATED = "deployment_created"
    DEPLOYMENT_ACTIVATED = "deployment_activated"
    BAR_PROCESSED = "bar_processed"
    SIGNAL_GENERATED = "signal_generated"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_FILLED = "order_filled"
    ORDER_REJECTED = "order_rejected"
    RISK_WARNING = "risk_warning"
    HEALTH_WARNING = "health_warning"
    PAUSED = "paused"
    RESUMED = "resumed"
    HALTED = "halted"
    DEPLOYMENT_STOPPED = "deployment_stopped"
    ERROR = "error"


class PaperOperationEvent(BaseModel):
    """One auditable paper-operations event.

    ``payload`` holds structured, JSON-safe event-specific data (signal name,
    order id, fill price, reason text, counters, etc). It never holds broker
    internals, credentials, or non-serializable objects.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str
    deployment_id: str
    strategy_id: str
    event_type: PaperOperationEventType
    timestamp: str
    symbol: str
    timeframe: str
    message: str = ""
    payload: dict = Field(default_factory=dict)
    sequence: int = 0


def _stable(obj: Any) -> Any:
    """JSON-stable representation (sorted keys, str fallback)."""
    return json.loads(json.dumps(obj, sort_keys=True, default=str))


def make_event_id(
    deployment_id: str,
    sequence: int,
    event_type: PaperOperationEventType,
    timestamp: str,
    payload: dict,
) -> str:
    """Deterministic event identity from immutable event content."""
    payload = {
        "deployment_id": deployment_id,
        "sequence": int(sequence),
        "event_type": event_type.value,
        "timestamp": str(timestamp),
        "payload": _stable(payload),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:64]


class PaperOperationsEventLog:
    """Append-only, deterministic paper-operations event log.

    Bound to a single deployment. Sequence numbers are assigned monotonically
    at recording time, so ordering is always deterministic for a given stream
    of ``record`` calls.
    """

    def __init__(self, deployment: PaperDeployment) -> None:
        self.deployment_id = deployment.deployment_id
        self.strategy_id = deployment.strategy_id
        self.symbol = deployment.symbol
        self.timeframe = deployment.timeframe
        self._events: list[PaperOperationEvent] = []
        self._seq = 0

    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #
    def record(
        self,
        event_type: PaperOperationEventType,
        timestamp: str,
        message: str = "",
        payload: Optional[dict] = None,
    ) -> PaperOperationEvent:
        """Append one event with the next sequence number. Deterministic."""
        payload = payload if payload is not None else {}
        seq = self._seq
        self._seq += 1
        ev = PaperOperationEvent(
            event_id=make_event_id(
                self.deployment_id, seq, event_type, timestamp, payload
            ),
            deployment_id=self.deployment_id,
            strategy_id=self.strategy_id,
            event_type=event_type,
            timestamp=str(timestamp),
            symbol=self.symbol,
            timeframe=self.timeframe,
            message=message,
            payload=_stable(payload),
            sequence=seq,
        )
        self._events.append(ev)
        return ev

    # ------------------------------------------------------------------ #
    # Views
    # ------------------------------------------------------------------ #
    @property
    def events(self) -> list[PaperOperationEvent]:
        return list(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self):
        return iter(self._events)

    def count_type(self, event_type: PaperOperationEventType) -> int:
        return sum(1 for e in self._events if e.event_type == event_type)

    def last_of_type(
        self, event_type: PaperOperationEventType
    ) -> Optional[PaperOperationEvent]:
        for e in reversed(self._events):
            if e.event_type == event_type:
                return e
        return None
