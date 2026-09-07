"""Autonomous Events — Phase 1.

Typed event model for auditable autonomous decisions and behavior.
Future events may include: BOT_STARTED, BOT_STOPPED, SCAN_STARTED,
CANDIDATES_FOUND, DECISION_CREATED, DECISION_REJECTED, DEPLOYMENT_CREATED,
DEPLOYMENT_STOPPED, POLICY_VIOLATION, ERROR.

Phase 1 provides a clean typed event interface. Persistence is not yet
justified — events are in-memory and can be serialized for later storage.
No LLM-generated prose is used in event messages.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel
from datetime import datetime, timezone


class AutonomousEventType(str, Enum):
    """Typed autonomous event types — Phase 1 foundation."""
    BOT_STARTED = "bot_started"
    BOT_STOPPED = "bot_stopped"
    BOT_PAUSED = "bot_paused"
    BOT_RESUMED = "bot_resumed"
    BOT_HALTED = "bot_halted"
    BOT_ERROR = "bot_error"
    SCAN_STARTED = "scan_started"
    SCAN_COMPLETED = "scan_completed"
    CANDIDATES_FOUND = "candidates_found"
    DECISION_CREATED = "decision_created"
    DECISION_REJECTED = "decision_rejected"
    DEPLOYMENT_CREATED = "deployment_created"
    DEPLOYMENT_STOPPED = "deployment_stopped"
    POLICY_VIOLATION = "policy_violation"
    ERROR = "error"


def make_autonomous_event_id(
    deployment_id: str,
    sequence: int,
    event_type: AutonomousEventType,
    timestamp: str,
    payload: dict,
) -> str:
    """Deterministic event identity from immutable event content.

    Same pattern as the existing PaperOperationEvent.make_event_id in
    src/trading_system/paper/events.py, so the identity system is consistent.
    """
    payload = {
        "deployment_id": deployment_id,
        "sequence": int(sequence),
        "event_type": event_type.value,
        "timestamp": str(timestamp),
        "payload": json.loads(json.dumps(payload, sort_keys=True, default=str)),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:64]


class AutonomousEvent(BaseModel):
    """One auditable autonomous event.

    Design follows the existing PaperOperationEvent pattern from
    src/trading_system/paper/events.py:
      - Append-only identity.
      - Deterministic event_id from immutable content.
      - JSON-safe payload (no broker internals, no credentials).
      - No randomness or wall-clock time in identity.
    """

    model_config = {"extra": "forbid"}

    event_id: str
    deployment_id: str
    bot_id: str
    event_type: AutonomousEventType
    timestamp: str
    symbol: str
    timeframe: str
    message: str = ""
    payload: dict = {}
    sequence: int = 0


class AutonomousEventLog:
    """Append-only, deterministic log of autonomous events.

    Bound to a single bot. Sequence numbers are assigned monotonically
    at recording time, so ordering is always deterministic for a given
    stream of ``record`` calls.
    """

    def __init__(self, bot_id: str) -> None:
        self.bot_id = bot_id
        self._events: list[AutonomousEvent] = []
        self._seq = 0

    def record(
        self,
        event_type: AutonomousEventType,
        *,
        deployment_id: str = "",
        bot_id: str = "",
        symbol: str = "",
        timeframe: str = "",
        message: str = "",
        payload: Optional[dict] = None,
    ) -> AutonomousEvent:
        """Append one event with the next sequence number. Deterministic."""
        payload = payload if payload is not None else {}
        seq = self._seq
        self._seq += 1

        ev = AutonomousEvent(
            event_id=make_autonomous_event_id(
                deployment_id=deployment_id,
                sequence=seq,
                event_type=event_type,
                timestamp=_now_iso(),
                payload=payload,
            ),
            deployment_id=deployment_id,
            bot_id=bot_id,
            event_type=event_type,
            timestamp=_now_iso(),
            symbol=symbol,
            timeframe=timeframe,
            message=message,
            payload=_stable(payload),
            sequence=seq,
        )
        self._events.append(ev)
        return ev

    @property
    def events(self) -> list[AutonomousEvent]:
        return list(self._events)

    def count_type(self, event_type: AutonomousEventType) -> int:
        return sum(1 for e in self._events if e.event_type == event_type)

    def last_of_type(
        self, event_type: AutonomousEventType
    ) -> Optional[AutonomousEvent]:
        for e in reversed(self._events):
            if e.event_type == event_type:
                return e
        return None


def _stable(obj: Any) -> Any:
    """JSON-stable representation (sorted keys, str fallback)."""
    return json.loads(json.dumps(obj, sort_keys=True, default=str))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()