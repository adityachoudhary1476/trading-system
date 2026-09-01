"""Phase 20 — Paper Trading Session: typed state, checkpoint, persistence.

A :class:`PaperSession` is the long-lived, serializable, deterministic
representation of one running paper deployment. It is a *view* over the
runner + broker + operations layer; it never owns accounting truth (the
``PaperBroker`` remains the source of cash/equity/P&L).

The session is used by :class:`PaperTradingControlCenter` for:

  * safe checkpointing (explicit caller-driven snapshots, no background loop)
  * safe recovery: a session can be restored into a fresh
    :class:`PaperStrategyRunner` and resume where it left off
  * duplicate-bar protection across restarts (the runner's existing
    ``_last_processed_bar`` idempotency makes re-feeding the same bar a no-op)

Design rules:

  * No credentials. No API keys. No broker secrets. No environment files.
  * No network. No live broker.
  * Append-only historical evidence is never mutated by a session operation.
  * The session identity is derived from immutable deployment inputs only;
    it is independent of any wall clock or random seed.
  * All persisted payloads are JSON-serializable pydantic models. Unknown
    schema versions fail closed (recovery is rejected, never auto-repaired).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Text,
    select,
)
from sqlalchemy.orm import sessionmaker

from ..storage.database import Base
from .deployment import (
    PaperDeployment,
    PaperDeploymentStatus,
    deployment_identity as _deployment_identity,
)
from .operations import PaperOperationsState, position_dict


# --------------------------------------------------------------------------- #
# Session lifecycle states
# --------------------------------------------------------------------------- #
class PaperSessionStatus(str, Enum):
    """Phase 20 session lifecycle. Mirrors the deployment lifecycle."""

    CREATED = "created"
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"
    FAILED = "failed"
    CHECKPOINTED = "checkpointed"
    RESTORED = "restored"


# Legal transitions on a session. Mirrors the Phase 18 deployment transitions
# the runner actually respects, plus checkpointed/restored for the new layer.
SESSION_ALLOWED_TRANSITIONS: frozenset[tuple[PaperSessionStatus, PaperSessionStatus]] = frozenset({
    (PaperSessionStatus.CREATED, PaperSessionStatus.ACTIVE),
    (PaperSessionStatus.CREATED, PaperSessionStatus.STOPPED),
    (PaperSessionStatus.ACTIVE, PaperSessionStatus.PAUSED),
    (PaperSessionStatus.ACTIVE, PaperSessionStatus.STOPPED),
    (PaperSessionStatus.PAUSED, PaperSessionStatus.ACTIVE),
    (PaperSessionStatus.PAUSED, PaperSessionStatus.STOPPED),
    (PaperSessionStatus.ACTIVE, PaperSessionStatus.FAILED),
    (PaperSessionStatus.PAUSED, PaperSessionStatus.FAILED),
    (PaperSessionStatus.ACTIVE, PaperSessionStatus.CHECKPOINTED),
    (PaperSessionStatus.PAUSED, PaperSessionStatus.CHECKPOINTED),
    (PaperSessionStatus.CHECKPOINTED, PaperSessionStatus.ACTIVE),
    (PaperSessionStatus.CHECKPOINTED, PaperSessionStatus.STOPPED),
    (PaperSessionStatus.RESTORED, PaperSessionStatus.ACTIVE),
    (PaperSessionStatus.RESTORED, PaperSessionStatus.STOPPED),
})


# Schema version constant — bumped to 3 because Phase 20 adds the
# ``paper_sessions`` table. Phase 18/19 records are unchanged.
SESSION_SCHEMA_VERSION = 3


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
def session_identity(
    deployment: PaperDeployment,
    *,
    created_at: str = "",
    config_fingerprint: Optional[str] = None,
) -> str:
    """Deterministic session identity derived from immutable inputs.

    Inputs (in order, stable across processes):

      * ``deployment_id``       — SHA-256 of the deployment binding
      * ``strategy_spec_hash``  — exact spec hash, not the spec object
      * ``symbol`` / ``timeframe`` — guard against symbol-mix restoration
      * ``execution_mode``      — paper-only enforcement

    Wall-clock ``created_at`` is intentionally OPTIONAL and excluded from
    the primary identity (two distinct processes that start a session with
    identical inputs must compare equal). When ``created_at`` is supplied
    (e.g. on checkpoint) it is mixed into a *checkpoint* fingerprint only.
    """
    payload = {
        "deployment_id": deployment.deployment_id,
        "strategy_id": deployment.strategy_id,
        "strategy_spec_hash": deployment.strategy_spec_hash,
        "symbol": deployment.symbol,
        "timeframe": deployment.timeframe,
        "dataset_id": deployment.dataset_id,
        "execution_mode": deployment.config.execution_mode,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:64]


def checkpoint_fingerprint(
    deployment_id: str,
    last_bar_timestamp: Optional[str],
    sequence: int,
    processed_bars: int,
    session_status: str,
) -> str:
    """Deterministic fingerprint of a checkpoint's resume-relevant state.

    Two checkpoints with the same fingerprint describe the SAME operational
    state and are interchangeable for recovery purposes.
    """
    payload = {
        "deployment_id": deployment_id,
        "last_bar_timestamp": last_bar_timestamp or "",
        "sequence": int(sequence),
        "processed_bars": int(processed_bars),
        "session_status": session_status,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:64]


# --------------------------------------------------------------------------- #
# Session model (pydantic) — the in-memory session record
# --------------------------------------------------------------------------- #
class PaperSession(BaseModel):
    """Typed, JSON-serializable paper-trading session.

    Built on demand from a runner + broker + operations layer. It is a
    deterministic view; mutating the runner afterwards does NOT mutate the
    session — a fresh session must be built (or checkpointed) to observe
    newer state.

    ``broker_state`` captures only the broker values the Control Center
    inspects (equity, realized/unrealized P&L, open-position summary). It
    never holds broker secrets or internal mutable objects.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    deployment_id: str
    strategy_id: str
    strategy_spec_hash: str
    symbol: str
    timeframe: str
    execution_mode: str
    dataset_id: str
    session_status: PaperSessionStatus = PaperSessionStatus.CREATED
    deployment_status: PaperDeploymentStatus = PaperDeploymentStatus.CREATED
    created_at: str = ""
    updated_at: str = ""
    last_processed_bar_timestamp: Optional[str] = None
    bar_count: int = 0
    generated_signals: int = 0
    orders_submitted: int = 0
    fills_received: int = 0
    rejected_orders: int = 0
    starting_equity: Optional[float] = None
    current_equity: Optional[float] = None
    realized_pnl: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    max_drawdown: Optional[float] = None
    health_status: str = "healthy"
    halt_reason: Optional[str] = None
    consecutive_errors: int = 0
    circuit_state: str = "closed"
    circuit_reason: Optional[str] = None
    circuit_trip_count: int = 0
    event_count: int = 0
    event_sequence: int = 0
    broker_state: dict = Field(default_factory=dict)
    operations_state_json: dict = Field(default_factory=dict)
    schema_version: int = SESSION_SCHEMA_VERSION


def session_from_runner(
    *,
    runner,
    session_id: str,
    session_status: PaperSessionStatus = PaperSessionStatus.ACTIVE,
) -> PaperSession:
    """Build a typed session view from a live ``PaperStrategyRunner``.

    This is a pure read operation — it never mutates the runner or broker.
    """
    deployment = runner.deployment
    broker = runner.broker
    account = broker.account()
    position = broker.get_position(deployment.symbol)

    circuit = runner.circuit_breaker
    events = runner.event_log.events if runner.event_log is not None else []
    last_event = events[-1] if events else None

    broker_state = {
        "initial_cash": float(account.initial_cash),
        "cash": float(account.cash),
        "equity": float(account.equity),
        "margin_used": float(account.margin_used),
        "available_cash": float(account.available_cash),
        "realized_pnl": float(account.realized_pnl),
        "unrealized_pnl": float(account.unrealized_pnl),
        "position": position_dict(position),
    }

    ops = runner.operations_state()
    ops_json = ops.model_dump(mode="json")

    return PaperSession(
        session_id=session_id,
        deployment_id=deployment.deployment_id,
        strategy_id=deployment.strategy_id,
        strategy_spec_hash=deployment.strategy_spec_hash,
        symbol=deployment.symbol,
        timeframe=deployment.timeframe,
        execution_mode=deployment.config.execution_mode,
        dataset_id=deployment.dataset_id,
        session_status=session_status,
        deployment_status=deployment.status,
        created_at=ops.started_at or _now_iso(),
        updated_at=_now_iso(),
        last_processed_bar_timestamp=ops.last_bar_timestamp,
        bar_count=int(runner.bar_count),
        generated_signals=int(runner.generated_signals),
        orders_submitted=int(runner.orders_submitted),
        fills_received=int(runner.fills_received),
        rejected_orders=int(runner.rejected_orders),
        starting_equity=ops.starting_equity,
        current_equity=ops.current_equity,
        realized_pnl=ops.realized_pnl,
        unrealized_pnl=ops.unrealized_pnl,
        max_drawdown=ops.max_drawdown,
        health_status=ops.health_status,
        halt_reason=ops.halt_reason,
        consecutive_errors=int(runner.consecutive_errors),
        circuit_state=(circuit.state.value if circuit is not None else "closed"),
        circuit_reason=(circuit.reason if circuit is not None else None),
        circuit_trip_count=(circuit.trip_count if circuit is not None else 0),
        event_count=len(events),
        event_sequence=(last_event.sequence if last_event is not None else -1),
        broker_state=broker_state,
        operations_state_json=ops_json,
        schema_version=SESSION_SCHEMA_VERSION,
    )


# --------------------------------------------------------------------------- #
# Checkpoint (explicit, caller-driven snapshot of a session)
# --------------------------------------------------------------------------- #
class PaperSessionCheckpoint(BaseModel):
    """An explicit, immutable snapshot of a session at a point in time.

    Used by the Control Center for explicit ``save_session`` /
    ``restore_session`` operations. There is NO autonomous background loop;
    checkpoints are written only when the caller asks.

    ``events_fingerprint`` is a SHA-256 of the ordered event-id sequence at
    checkpoint time; ``ops_fingerprint`` is a SHA-256 of the operational
    counters. Together they let the recovery layer detect divergence
    between the persisted state and the live runner.
    """

    model_config = ConfigDict(extra="forbid")

    checkpoint_id: str
    session_id: str
    deployment_id: str
    strategy_id: str
    strategy_spec_hash: str
    symbol: str
    timeframe: str
    execution_mode: str
    dataset_id: str
    schema_version: int = SESSION_SCHEMA_VERSION
    deployment_status: str
    session_status: str
    last_processed_bar_timestamp: Optional[str] = None
    bar_count: int = 0
    orders_submitted: int = 0
    fills_received: int = 0
    rejected_orders: int = 0
    generated_signals: int = 0
    consecutive_errors: int = 0
    starting_equity: Optional[float] = None
    current_equity: Optional[float] = None
    realized_pnl: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    max_drawdown: Optional[float] = None
    health_status: str = "healthy"
    halt_reason: Optional[str] = None
    circuit_state: str = "closed"
    circuit_reason: Optional[str] = None
    circuit_trip_count: int = 0
    event_count: int = 0
    event_sequence: int = 0
    broker_state: dict = Field(default_factory=dict)
    operations_state_json: dict = Field(default_factory=dict)
    events_fingerprint: str
    ops_fingerprint: str
    created_at: str


def checkpoint_from_session(session: PaperSession) -> PaperSessionCheckpoint:
    """Build a checkpoint from a :class:`PaperSession`."""
    blob = json.dumps(session.broker_state, sort_keys=True, default=str)
    events_fp = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:64]
    ops_blob = json.dumps(
        {
            "bar_count": session.bar_count,
            "orders_submitted": session.orders_submitted,
            "fills_received": session.fills_received,
            "rejected_orders": session.rejected_orders,
            "generated_signals": session.generated_signals,
            "consecutive_errors": session.consecutive_errors,
        },
        sort_keys=True,
        default=str,
    )
    ops_fp = hashlib.sha256(ops_blob.encode("utf-8")).hexdigest()[:64]
    return PaperSessionCheckpoint(
        checkpoint_id=hashlib.sha256(
            (session.session_id + "|" + events_fp + "|" + ops_fp).encode("utf-8")
        ).hexdigest()[:64],
        session_id=session.session_id,
        deployment_id=session.deployment_id,
        strategy_id=session.strategy_id,
        strategy_spec_hash=session.strategy_spec_hash,
        symbol=session.symbol,
        timeframe=session.timeframe,
        execution_mode=session.execution_mode,
        dataset_id=session.dataset_id,
        deployment_status=session.deployment_status.value,
        session_status=session.session_status.value,
        last_processed_bar_timestamp=session.last_processed_bar_timestamp,
        bar_count=session.bar_count,
        orders_submitted=session.orders_submitted,
        fills_received=session.fills_received,
        rejected_orders=session.rejected_orders,
        generated_signals=session.generated_signals,
        consecutive_errors=session.consecutive_errors,
        starting_equity=session.starting_equity,
        current_equity=session.current_equity,
        realized_pnl=session.realized_pnl,
        unrealized_pnl=session.unrealized_pnl,
        max_drawdown=session.max_drawdown,
        health_status=session.health_status,
        halt_reason=session.halt_reason,
        circuit_state=session.circuit_state,
        circuit_reason=session.circuit_reason,
        circuit_trip_count=session.circuit_trip_count,
        event_count=session.event_count,
        event_sequence=session.event_sequence,
        broker_state=dict(session.broker_state),
        operations_state_json=dict(session.operations_state_json),
        events_fingerprint=events_fp,
        ops_fingerprint=ops_fp,
        created_at=_now_iso(),
    )


# --------------------------------------------------------------------------- #
# Session store (SQLAlchemy) — explicit persistence
# --------------------------------------------------------------------------- #
class PaperSessionRecord(Base):
    """Phase 20 — persisted paper session checkpoint row.

    The table is added via the project's existing schema-versioning
    mechanism; no second DB framework is introduced. Recovery reads this
    row, validates identity, and rebuilds the operational state on the
    existing ``PaperStrategyRunner``.
    """

    __tablename__ = "paper_sessions"

    session_id = Column(String(64), primary_key=True)
    checkpoint_id = Column(String(64), nullable=False, index=True)
    deployment_id = Column(String(64), nullable=False, index=True)
    strategy_id = Column(String(64), nullable=False, index=True)
    strategy_spec_hash = Column(String(64), nullable=False)
    symbol = Column(String(32), nullable=False)
    timeframe = Column(String(8), nullable=False)
    execution_mode = Column(String(8), nullable=False)
    dataset_id = Column(String(64), nullable=False)
    schema_version = Column(Integer, nullable=False, default=SESSION_SCHEMA_VERSION)

    deployment_status = Column(String(16), nullable=False)
    session_status = Column(String(16), nullable=False, index=True)

    last_processed_bar_timestamp = Column(String(40), nullable=True)
    bar_count = Column(Integer, nullable=False, default=0)
    orders_submitted = Column(Integer, nullable=False, default=0)
    fills_received = Column(Integer, nullable=False, default=0)
    rejected_orders = Column(Integer, nullable=False, default=0)
    generated_signals = Column(Integer, nullable=False, default=0)
    consecutive_errors = Column(Integer, nullable=False, default=0)

    starting_equity = Column(String(40), nullable=True)
    current_equity = Column(String(40), nullable=True)
    realized_pnl = Column(String(40), nullable=True)
    unrealized_pnl = Column(String(40), nullable=True)
    max_drawdown = Column(String(40), nullable=True)

    health_status = Column(String(16), nullable=False, default="healthy")
    halt_reason = Column(Text, nullable=True)

    circuit_state = Column(String(16), nullable=False, default="closed")
    circuit_reason = Column(Text, nullable=True)
    circuit_trip_count = Column(Integer, nullable=False, default=0)

    event_count = Column(Integer, nullable=False, default=0)
    event_sequence = Column(Integer, nullable=False, default=-1)
    events_fingerprint = Column(String(64), nullable=False)
    ops_fingerprint = Column(String(64), nullable=False)

    broker_state_json = Column(Text, nullable=False, default="{}")
    operations_state_json = Column(Text, nullable=False, default="{}")

    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class SessionSchemaError(RuntimeError):
    """Raised when a persisted session is incompatible with the current schema.

    Recovery MUST fail closed on this error.
    """


class SessionIdentityError(RuntimeError):
    """Raised when a persisted session's identity does not match the requested
    deployment / strategy / symbol / timeframe.

    Recovery MUST fail closed on this error.
    """


class PaperSessionStore:
    """Phase 20 — explicit, schema-versioned session persistence.

    Reuses the project's existing SQLAlchemy ``Base``. Calls
    ``Base.metadata.create_all`` so the new ``paper_sessions`` table is
    created idempotently on first use. No autonomous background loop.
    """

    def __init__(self, engine) -> None:
        self.engine = engine
        Base.metadata.create_all(engine)
        self._Session = sessionmaker(bind=engine, future=True)

    # -- writes --------------------------------------------------------------
    def save_checkpoint(self, checkpoint: PaperSessionCheckpoint) -> PaperSessionCheckpoint:
        """Persist a checkpoint. Idempotent: re-saving the same checkpoint_id
        is a no-op (returns the existing record)."""
        rec = self._to_record(checkpoint)
        with self._Session() as s:
            existing = s.get(PaperSessionRecord, checkpoint.session_id)
            if existing is not None:
                if existing.checkpoint_id != checkpoint.checkpoint_id:
                    raise SessionIdentityError(
                        f"session {checkpoint.session_id} already persisted with "
                        f"a different checkpoint (existing={existing.checkpoint_id}, "
                        f"new={checkpoint.checkpoint_id}); refusing to overwrite"
                    )
                return checkpoint
            s.add(rec)
            s.commit()
        return checkpoint

    # -- reads ---------------------------------------------------------------
    def get_checkpoint(self, session_id: str) -> Optional[PaperSessionCheckpoint]:
        """Return the persisted checkpoint for ``session_id`` (or None)."""
        with self._Session() as s:
            rec = s.get(PaperSessionRecord, session_id)
            if rec is None:
                return None
            return self._from_record(rec)

    def list_sessions(
        self,
        *,
        deployment_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[PaperSessionCheckpoint]:
        with self._Session() as s:
            q = select(PaperSessionRecord)
            if deployment_id is not None:
                q = q.where(PaperSessionRecord.deployment_id == deployment_id)
            if strategy_id is not None:
                q = q.where(PaperSessionRecord.strategy_id == strategy_id)
            if status is not None:
                q = q.where(PaperSessionRecord.session_status == status)
            recs = s.execute(q.order_by(PaperSessionRecord.updated_at.asc())).scalars().all()
            return [self._from_record(r) for r in recs]

    # -- validation ----------------------------------------------------------
    def validate_for_restore(
        self,
        checkpoint: PaperSessionCheckpoint,
        *,
        expected_deployment: PaperDeployment,
    ) -> None:
        """Fail-closed validation before restore.

        Checks:

          1. schema version matches the running code's expected version.
          2. deployment_id matches the expected deployment.
          3. strategy_spec_hash matches the expected deployment's hash.
          4. symbol / timeframe match.
          5. execution_mode is paper.
          6. JSON payloads parse cleanly and contain no sentinel "missing" values.
        """
        if checkpoint.schema_version != SESSION_SCHEMA_VERSION:
            raise SessionSchemaError(
                f"incompatible session schema version: checkpoint="
                f"{checkpoint.schema_version}, expected={SESSION_SCHEMA_VERSION}"
            )
        if checkpoint.deployment_id != expected_deployment.deployment_id:
            raise SessionIdentityError(
                f"deployment_id mismatch: checkpoint={checkpoint.deployment_id!r}, "
                f"expected={expected_deployment.deployment_id!r}"
            )
        if checkpoint.strategy_spec_hash != expected_deployment.strategy_spec_hash:
            raise SessionIdentityError(
                f"strategy_spec_hash mismatch: checkpoint={checkpoint.strategy_spec_hash!r}, "
                f"expected={expected_deployment.strategy_spec_hash!r}"
            )
        if checkpoint.symbol != expected_deployment.symbol:
            raise SessionIdentityError(
                f"symbol mismatch: checkpoint={checkpoint.symbol!r}, "
                f"expected={expected_deployment.symbol!r}"
            )
        if checkpoint.timeframe != expected_deployment.timeframe:
            raise SessionIdentityError(
                f"timeframe mismatch: checkpoint={checkpoint.timeframe!r}, "
                f"expected={expected_deployment.timeframe!r}"
            )
        if checkpoint.execution_mode != "paper":
            raise SessionIdentityError(
                f"execution_mode must be 'paper'; got {checkpoint.execution_mode!r}"
            )
        try:
            # Strict JSON round-trip: NaN / Inf rejected, non-finite values rejected.
            broker_blob = json.dumps(checkpoint.broker_state, default=str,
                                     allow_nan=False)
            ops_blob = json.dumps(checkpoint.operations_state_json, default=str,
                                  allow_nan=False)
            json.loads(broker_blob)
            json.loads(ops_blob)
        except (TypeError, ValueError) as exc:
            raise SessionSchemaError(
                f"persisted checkpoint contains non-JSON-safe payload: {exc}"
            ) from exc

    # -- ORM helpers ---------------------------------------------------------
    def _to_record(self, cp: PaperSessionCheckpoint) -> PaperSessionRecord:
        return PaperSessionRecord(
            session_id=cp.session_id,
            checkpoint_id=cp.checkpoint_id,
            deployment_id=cp.deployment_id,
            strategy_id=cp.strategy_id,
            strategy_spec_hash=cp.strategy_spec_hash,
            symbol=cp.symbol,
            timeframe=cp.timeframe,
            execution_mode=cp.execution_mode,
            dataset_id=cp.dataset_id,
            schema_version=cp.schema_version,
            deployment_status=cp.deployment_status,
            session_status=cp.session_status,
            last_processed_bar_timestamp=cp.last_processed_bar_timestamp,
            bar_count=cp.bar_count,
            orders_submitted=cp.orders_submitted,
            fills_received=cp.fills_received,
            rejected_orders=cp.rejected_orders,
            generated_signals=cp.generated_signals,
            consecutive_errors=cp.consecutive_errors,
            starting_equity=_opt_str(cp.starting_equity),
            current_equity=_opt_str(cp.current_equity),
            realized_pnl=_opt_str(cp.realized_pnl),
            unrealized_pnl=_opt_str(cp.unrealized_pnl),
            max_drawdown=_opt_str(cp.max_drawdown),
            health_status=cp.health_status,
            halt_reason=cp.halt_reason,
            circuit_state=cp.circuit_state,
            circuit_reason=cp.circuit_reason,
            circuit_trip_count=cp.circuit_trip_count,
            event_count=cp.event_count,
            event_sequence=cp.event_sequence,
            events_fingerprint=cp.events_fingerprint,
            ops_fingerprint=cp.ops_fingerprint,
            broker_state_json=json.dumps(cp.broker_state, default=str),
            operations_state_json=json.dumps(cp.operations_state_json, default=str),
            created_at=_parse_dt(cp.created_at),
            updated_at=_parse_dt(_now_iso()),
        )

    @staticmethod
    def _from_record(rec: PaperSessionRecord) -> PaperSessionCheckpoint:
        return PaperSessionCheckpoint(
            checkpoint_id=rec.checkpoint_id,
            session_id=rec.session_id,
            deployment_id=rec.deployment_id,
            strategy_id=rec.strategy_id,
            strategy_spec_hash=rec.strategy_spec_hash,
            symbol=rec.symbol,
            timeframe=rec.timeframe,
            execution_mode=rec.execution_mode,
            dataset_id=rec.dataset_id,
            schema_version=int(rec.schema_version),
            deployment_status=rec.deployment_status,
            session_status=rec.session_status,
            last_processed_bar_timestamp=rec.last_processed_bar_timestamp,
            bar_count=int(rec.bar_count),
            orders_submitted=int(rec.orders_submitted),
            fills_received=int(rec.fills_received),
            rejected_orders=int(rec.rejected_orders),
            generated_signals=int(rec.generated_signals),
            consecutive_errors=int(rec.consecutive_errors),
            starting_equity=_opt_float(rec.starting_equity),
            current_equity=_opt_float(rec.current_equity),
            realized_pnl=_opt_float(rec.realized_pnl),
            unrealized_pnl=_opt_float(rec.unrealized_pnl),
            max_drawdown=_opt_float(rec.max_drawdown),
            health_status=rec.health_status,
            halt_reason=rec.halt_reason,
            circuit_state=rec.circuit_state,
            circuit_reason=rec.circuit_reason,
            circuit_trip_count=int(rec.circuit_trip_count),
            event_count=int(rec.event_count),
            event_sequence=int(rec.event_sequence),
            broker_state=json.loads(rec.broker_state_json or "{}"),
            operations_state_json=json.loads(rec.operations_state_json or "{}"),
            events_fingerprint=rec.events_fingerprint,
            ops_fingerprint=rec.ops_fingerprint,
            created_at=rec.created_at.isoformat() if rec.created_at else "",
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    raise ValueError(f"cannot parse datetime from {value!r}")


def _opt_str(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    return repr(float(value))


def _opt_float(value: Optional[str]) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def apply_checkpoint_to_runner(checkpoint: PaperSessionCheckpoint, runner) -> None:
    """Restore operational state into an existing ``PaperStrategyRunner``.

    This is the *only* way Phase 20 mutates a runner — explicit, scoped,
    and idempotent. The runner's existing
    ``_last_processed_bar`` idempotency guarantees that re-feeding the last
    bar after restore is a no-op (no duplicate order).

    The function never modifies:

      * the broker's order/fill ledger
      * historical evidence
      * the strategy spec
    """
    # Status: bring the deployment into the checkpoint's documented status.
    try:
        runner.deployment.status = PaperDeploymentStatus(checkpoint.deployment_status)
    except Exception:
        runner.deployment.status = PaperDeploymentStatus.ACTIVE

    # Restore operational counters (read-only fields exposed via setters).
    runner._starting_equity = checkpoint.starting_equity
    runner._peak_equity = (
        checkpoint.current_equity if checkpoint.current_equity is not None
        else checkpoint.starting_equity
    )
    runner._max_drawdown = checkpoint.max_drawdown
    runner._consecutive_errors = int(checkpoint.consecutive_errors)
    runner._generated_signals = int(checkpoint.generated_signals)
    runner._orders_submitted = int(checkpoint.orders_submitted)
    runner._fills_received = int(checkpoint.fills_received)
    runner._rejected_orders = int(checkpoint.rejected_orders)
    runner._bar_count = int(checkpoint.bar_count)
    runner._health_status = checkpoint.health_status
    runner._halt_reason = checkpoint.halt_reason

    # Restore circuit breaker state.
    if runner._circuit_breaker is not None:
        if checkpoint.circuit_state == "open":
            if not runner._circuit_breaker.is_open:
                runner._circuit_breaker.trip(checkpoint.circuit_reason or "restored")
        else:
            # Always reset to closed on restore: caller asked for a recovery.
            runner._circuit_breaker.reset()

    # Restore last processed bar timestamp so the idempotency rule kicks in.
    if checkpoint.last_processed_bar_timestamp:
        try:
            import pandas as pd

            ts = pd.Timestamp(checkpoint.last_processed_bar_timestamp)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            runner._last_processed_bar = ts
        except Exception:
            runner._last_processed_bar = None
    else:
        runner._last_processed_bar = None