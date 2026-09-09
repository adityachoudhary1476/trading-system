"""Phase 23 — Autonomous bot state persistence.

Stores the authoritative bot state (lifecycle, enabled flag, kill switch)
so that a backend restart does not silently reactivate a previously killed
bot.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Column, DateTime, String

from ..research.evidence import Base, _now


class AutonomousBotRecord(Base):
    __tablename__ = "autonomous_bots"

    bot_id = Column(String(64), primary_key=True)
    state = Column(String(16), nullable=False, default="created")
    enabled = Column(String(5), nullable=False, default="true")
    kill_switch_state = Column(String(16), nullable=False, default="active")
    kill_switch_reason = Column(String(64), nullable=True)
    kill_switch_halted_at = Column(String(64), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False)


def _to_bool(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


def _from_bool(value: bool) -> str:
    return "true" if value else "false"


def _now_iso() -> Optional[str]:
    now = _now()
    if now is None:
        return None
    if isinstance(now, datetime):
        return now.isoformat()
    return str(now)


class AutonomousBotStateStore:
    """Persistent store for the authoritative autonomous bot state."""

    def __init__(self, engine) -> None:
        self.engine = engine
        self._Session = __import__("sqlalchemy.orm.session", fromlist=["sessionmaker"]).sessionmaker(bind=engine, future=True)

    def ensure_schema(self) -> None:
        """Create the autonomous_bots table if it does not exist."""
        Base.metadata.create_all(self.engine, tables=[AutonomousBotRecord.__table__])

    def load_state(self, bot_id: str) -> dict[str, Any]:
        with self._Session() as s:
            rec = s.get(AutonomousBotRecord, bot_id)
            if rec is None:
                return {}
            return {
                "bot_id": rec.bot_id,
                "state": rec.state,
                "enabled": _to_bool(rec.enabled),
                "kill_switch_state": rec.kill_switch_state,
                "kill_switch_reason": rec.kill_switch_reason,
                "kill_switch_halted_at": rec.kill_switch_halted_at,
                "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
            }

    def save_state(
        self,
        bot_id: str,
        state: str,
        enabled: bool,
        kill_switch_state: str,
        kill_switch_reason: Optional[str] = None,
        kill_switch_halted_at: Optional[str] = None,
    ) -> None:
        with self._Session() as s:
            rec = s.get(AutonomousBotRecord, bot_id)
            now = datetime.now(timezone.utc)
            if rec is None:
                rec = AutonomousBotRecord(bot_id=bot_id)
                s.add(rec)
            rec.state = state
            rec.enabled = _from_bool(enabled)
            rec.kill_switch_state = kill_switch_state
            rec.kill_switch_reason = kill_switch_reason
            rec.kill_switch_halted_at = kill_switch_halted_at
            rec.updated_at = now
            s.commit()
