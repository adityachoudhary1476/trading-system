"""Phase 20 — Paper Trading Control Center.

The :class:`PaperTradingControlCenter` is the deterministic orchestration
and observability layer around the existing paper-trading system. It does
NOT introduce a second execution engine; it never bypasses the deployment
gate, never weakens the broker-only safety, never mutates historical
research evidence, and never touches a live broker.

What this layer does:

  * Discovers persisted paper deployments (registry / persistence layer).
  * Controls deployment lifecycle (CREATED / ACTIVE / PAUSED / STOPPED /
    FAILED / CHECKPOINTED / RESTORED) through explicit, type-checked
    transitions.
  * Builds typed, JSON-serializable sessions and checkpoints from the
    existing ``PaperStrategyRunner`` / ``PaperBroker``.
  * Persists and restores checkpoints through an explicit
    ``PaperSessionStore`` that reuses the project's SQLAlchemy ``Base``.
  * Exposes read-only inspection APIs for deployment, session, account,
    positions, performance, health, risk, circuit breaker, events, and
    evidence.
  * Assembles a single :class:`PaperControlCenterSnapshot` dashboard
    payload suitable for a future UI/API.
  * Provides a deterministic, JSON-safe report export.

Safety boundary
---------------

This module is **strictly paper trading**. There is:

  * NO live broker integration
  * NO live order placement
  * NO automatic paper→live promotion
  * NO live credentials
  * NO autonomous trading loop
  * NO background persistence loop (checkpoints are explicit)
  * NO network / socket / subprocess usage

Phase 20 does not implement live trading.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import create_engine

from ..execution.paper_broker import PaperBroker
from ..execution.broker import BrokerError
from ..execution.orders import (
    Order,
    OrderIntent,
    OrderResult,
    OrderStatus,
    Side,
)
from ..research.evidence import (
    EvidenceStore,
    EvidenceType,
    StrategyEvidence,
    strategy_identity,
)
from ..research.strategy_intelligence import (
    EvidenceFreshnessConfig,
    Eligibility,
    EvidenceRequirement,
    StrategyIntelligence,
)
from ..research.strategy_lab.spec import StrategySpec
from ..research.strategy_registry import StrategyRegistry
from .circuit_breaker import PaperCircuitBreaker
from .dashboard import (
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
    PaperControlCenterSnapshot,
    build_deployment_summary,
    build_strategy_summary,
)
from .deployment import (
    PaperDeployment,
    PaperDeploymentConfig,
    PaperDeploymentRecord,
    PaperDeploymentStatus,
    _rec_to_deployment,
)
from .events import PaperOperationEvent, PaperOperationEventType
from .gate import (
    PAPER_TRADING_GATE_REASONS,
    DeploymentGate,
    GateDecision,
)
from .health import PaperHealthMonitor
from .operations import position_dict
from .report import (
    PaperOperationsReport,
    PaperTradingReport,
    build_operations_report,
)
from .risk import PaperRiskConfig, PaperRiskGuard, RiskDecision
from .runner import PaperStrategyRunner, SignalType
from .session import (
    PaperSession,
    PaperSessionCheckpoint,
    PaperSessionStatus,
    PaperSessionStore,
    SESSION_ALLOWED_TRANSITIONS,
    SESSION_SCHEMA_VERSION,
    apply_checkpoint_to_runner,
    checkpoint_from_session,
    session_from_runner,
    session_identity,
)
from .snapshot import PaperPerformanceSnapshot


# --------------------------------------------------------------------------- #
# Errors (deterministic, typed)
# --------------------------------------------------------------------------- #
class ControlCenterError(RuntimeError):
    """Base error for the Phase 20 control layer."""


class UnknownDeploymentError(ControlCenterError):
    """Raised when a deployment id does not exist."""


class InvalidLifecycleTransitionError(ControlCenterError):
    """Raised when a requested deployment or session transition is not allowed."""


class PaperBrokerRequiredError(ControlCenterError):
    """Raised when a non-PaperBroker is supplied."""


class NotPaperModeError(ControlCenterError):
    """Raised when the underlying deployment's execution_mode is not paper."""


# Deployment statuses in which the control center accepts external
# (agent-driven) order intents. Only ACTIVE deployments are tradable.
STATUS_ACCEPTS_ORDERS: frozenset = frozenset({PaperDeploymentStatus.ACTIVE})


# --------------------------------------------------------------------------- #
# Control Center
# --------------------------------------------------------------------------- #
class PaperTradingControlCenter:
    """Deterministic, paper-only orchestration layer.

    Holds references to:

      * the ``StrategyRegistry`` + ``EvidenceStore`` (persistence)
      * the ``StrategyIntelligence`` facade (eligibility + lifecycle)
      * the ``DeploymentGate`` (Phase 18 safety)
      * an in-memory map of live :class:`PaperStrategyRunner` sessions,
        keyed by ``session_id``

    The control center NEVER spawns autonomous loops. Every checkpoint is
    written only when the caller asks via :meth:`save_session`. Recovery
    is explicit via :meth:`restore_session`.
    """

    def __init__(
        self,
        *,
        registry: StrategyRegistry,
        intelligence: StrategyIntelligence,
        gate: Optional[DeploymentGate] = None,
        session_store: Optional[PaperSessionStore] = None,
        requirement: Optional[EvidenceRequirement] = None,
        freshness_config: Optional[EvidenceFreshnessConfig] = None,
    ) -> None:
        self.registry = registry
        self.intelligence = intelligence
        self.gate = gate or DeploymentGate(
            intelligence=intelligence,
            requirement=requirement or EvidenceRequirement(),
            freshness_config=freshness_config or EvidenceFreshnessConfig(max_age_days=180),
        )
        self.session_store = session_store or PaperSessionStore(registry.store.engine)
        self.requirement = self.gate.requirement
        self.freshness_config = self.gate.freshness_config
        # In-memory map of live sessions.
        self._runners: dict[str, PaperStrategyRunner] = {}
        self._deployments: dict[str, PaperDeployment] = {}

    # ------------------------------------------------------------------ #
    # Construction helpers
    # ------------------------------------------------------------------ #
    @classmethod
    def from_engine(
        cls,
        engine,
        *,
        requirement: Optional[EvidenceRequirement] = None,
        freshness_config: Optional[EvidenceFreshnessConfig] = None,
    ) -> "PaperTradingControlCenter":
        """Build a control center over the project's existing SQLAlchemy engine."""
        store = EvidenceStore(engine)
        registry = StrategyRegistry(store)
        intelligence = StrategyIntelligence(registry)
        gate = DeploymentGate(
            intelligence=intelligence,
            requirement=requirement,
            freshness_config=freshness_config,
        )
        return cls(
            registry=registry,
            intelligence=intelligence,
            gate=gate,
            session_store=PaperSessionStore(engine),
            requirement=requirement,
            freshness_config=freshness_config,
        )

    # ------------------------------------------------------------------ #
    # Deployment discovery
    # ------------------------------------------------------------------ #
    def list_deployments(
        self,
        *,
        deployment_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[PaperDeployment]:
        """Deterministic discovery of persisted paper deployments.

        Filtering matches against the ``paper_deployments`` table (Phase 18
        schema) populated by ``PaperDeployment.as_record()``. Live
        (non-paper) deployments are NOT exposed because Phase 20 is
        paper-only.
        """
        with self.registry.store._Session() as s:
            q = s.query(PaperDeploymentRecord)
            if deployment_id is not None:
                q = q.filter(PaperDeploymentRecord.deployment_id == deployment_id)
            if strategy_id is not None:
                q = q.filter(PaperDeploymentRecord.strategy_id == strategy_id)
            if symbol is not None:
                q = q.filter(PaperDeploymentRecord.symbol == symbol)
            if timeframe is not None:
                q = q.filter(PaperDeploymentRecord.timeframe == timeframe)
            if status is not None:
                q = q.filter(PaperDeploymentRecord.status == status)
            recs = q.order_by(PaperDeploymentRecord.created_at.asc()).all()
            return [_rec_to_deployment(r) for r in recs]

    def get_deployment(self, deployment_id: str) -> Optional[PaperDeployment]:
        with self.registry.store._Session() as s:
            rec = s.get(PaperDeploymentRecord, deployment_id)
            return _rec_to_deployment(rec) if rec else None

    def has_deployment(self, deployment_id: str) -> bool:
        return self.get_deployment(deployment_id) is not None

    # ------------------------------------------------------------------ #
    # Deployment lifecycle
    # ------------------------------------------------------------------ #
    def create_deployment(
        self,
        *,
        spec: StrategySpec,
        dataset_id: str,
        config: Optional[PaperDeploymentConfig] = None,
    ) -> tuple[PaperDeployment, StrategySpec, GateDecision]:
        """Run the deployment gate; on success, persist the deployment.

        Returns ``(deployment, spec, decision)``. The deployment is added
        to the in-memory cache so subsequent lifecycle / inspection calls
        work without re-loading from the DB.
        """
        cfg = config or PaperDeploymentConfig()
        spec_hash = strategy_identity(spec)
        strategy = self.registry.get_strategy(spec_hash)
        if strategy is None:
            decision = GateDecision(
                passed=False, reasons=["unknown_strategy"]
            )
            return (None, spec, decision)  # type: ignore[return-value]

        decision = self.gate.evaluate(
            strategy_id=strategy.strategy_id,
            spec=spec,
            symbol=spec.symbol,
            timeframe=spec.timeframe,
            dataset_id=dataset_id,
            config=cfg,
        )
        if not decision.passed or decision.deployment is None:
            return (None, spec, decision)  # type: ignore[return-value]

        deployment = decision.deployment
        # Persist (idempotent — same inputs produce same deployment_id).
        rec = deployment.as_record()
        with self.registry.store._Session() as s:
            existing = s.get(PaperDeploymentRecord, deployment.deployment_id)
            if existing is None:
                s.add(rec)
                s.commit()
            else:
                # Verify identity (refuse if same id with different spec/config).
                if existing.strategy_spec_hash != deployment.strategy_spec_hash:
                    raise ControlCenterError(
                        "deployment_id collision with a different spec hash"
                    )
        self._deployments[deployment.deployment_id] = deployment
        return deployment, spec, decision

    def _transition_deployment(
        self,
        deployment_id: str,
        new_status: PaperDeploymentStatus,
        *,
        reason: str = "",
    ) -> PaperDeployment:
        deployment = self._load_or_cache_deployment(deployment_id)
        current = deployment.status
        _assert_deployment_transition(current, new_status)
        deployment.status = new_status
        deployment.updated_at = _now_iso()
        if new_status == PaperDeploymentStatus.ACTIVE and not deployment.activated_at:
            deployment.activated_at = _now_iso()
        with self.registry.store._Session() as s:
            rec = s.get(PaperDeploymentRecord, deployment_id)
            if rec is None:
                # Persist if absent (lifecycle called on a freshly created dep).
                s.add(deployment.as_record())
            else:
                rec.status = new_status.value
                rec.updated_at = _parse_dt(deployment.updated_at)
                if new_status == PaperDeploymentStatus.ACTIVE and not rec.activated_at:
                    rec.activated_at = _parse_dt(deployment.activated_at)
            s.commit()
        return deployment

    def activate_deployment(self, deployment_id: str) -> PaperDeployment:
        return self._transition_deployment(deployment_id, PaperDeploymentStatus.ACTIVE)

    def pause_deployment(self, deployment_id: str) -> PaperDeployment:
        return self._transition_deployment(deployment_id, PaperDeploymentStatus.PAUSED)

    def resume_deployment(self, deployment_id: str) -> PaperDeployment:
        return self._transition_deployment(deployment_id, PaperDeploymentStatus.ACTIVE)

    def stop_deployment(self, deployment_id: str) -> PaperDeployment:
        return self._transition_deployment(deployment_id, PaperDeploymentStatus.STOPPED)

    def fail_deployment(self, deployment_id: str) -> PaperDeployment:
        return self._transition_deployment(deployment_id, PaperDeploymentStatus.FAILED)

    def _load_or_cache_deployment(self, deployment_id: str) -> PaperDeployment:
        if deployment_id in self._deployments:
            return self._deployments[deployment_id]
        deployment = self.get_deployment(deployment_id)
        if deployment is None:
            raise UnknownDeploymentError(deployment_id)
        self._deployments[deployment_id] = deployment
        return deployment

    # ------------------------------------------------------------------ #
    # Broker enforcement
    # ------------------------------------------------------------------ #
    def assert_paper_broker(self, broker) -> PaperBroker:
        """Hard paper-only boundary check.

        Re-uses the project's existing ``DeploymentGate.assert_paper_broker``
        guard so there is exactly one paper-only validation mechanism.
        """
        DeploymentGate.assert_paper_broker(broker)
        if not isinstance(broker, PaperBroker):
            raise PaperBrokerRequiredError(
                f"expected PaperBroker; got {type(broker).__module__}."
                f"{type(broker).__name__}"
            )
        return broker

    # ------------------------------------------------------------------ #
    # Session lifecycle (control-center-side)
    # ------------------------------------------------------------------ #
    def attach_runner(self, deployment_id: str, runner: PaperStrategyRunner) -> str:
        """Attach a live runner to the control center and return a session_id."""
        self.assert_paper_broker(runner.broker)
        deployment = self._load_or_cache_deployment(deployment_id)
        if deployment.strategy_spec_hash != runner.deployment.strategy_spec_hash:
            raise ControlCenterError(
                "runner spec hash does not match the attached deployment"
            )
        sid = session_identity(deployment)
        self._runners[sid] = runner
        return sid

    def detach_runner(self, session_id: str) -> None:
        self._runners.pop(session_id, None)

    def find_session_for_deployment(self, deployment_id: str) -> Optional[str]:
        """Return the live session id attached to ``deployment_id`` if any.

        This is the only public way to map a deployment id to a live
        session; it is read-only and never mutates state.
        """
        for sid, runner in self._runners.items():
            if runner.deployment.deployment_id == deployment_id:
                return sid
        return None

    def get_runner(self, session_id: str):
        """Return the live :class:`PaperStrategyRunner` for ``session_id``.

        Returns ``None`` when no live runner is attached. Read-only.
        """
        return self._runners.get(session_id)

    def list_sessions(
        self,
        *,
        deployment_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[PaperSessionCheckpoint]:
        return self.session_store.list_sessions(
            deployment_id=deployment_id,
            strategy_id=strategy_id,
            status=status,
        )

    def get_session(self, session_id: str) -> Optional[PaperSessionCheckpoint]:
        return self.session_store.get_checkpoint(session_id)

    # ------------------------------------------------------------------ #
    # Checkpoint + restore
    # ------------------------------------------------------------------ #
    def save_session(self, session_id: str) -> PaperSessionCheckpoint:
        """Capture and persist the live session for ``session_id``.

        Explicit — there is no background loop. Raises ``UnknownDeploymentError``
        when the session is not attached.
        """
        runner = self._runners.get(session_id)
        if runner is None:
            raise UnknownDeploymentError(session_id)
        DeploymentGate.assert_paper_broker(runner.broker)
        deployment = runner.deployment
        session = session_from_runner(
            runner=runner,
            session_id=session_id,
            session_status=PaperSessionStatus.CHECKPOINTED,
        )
        checkpoint = checkpoint_from_session(session)
        self.session_store.save_checkpoint(checkpoint)
        return checkpoint

    def restore_session(
        self,
        *,
        session_id: str,
        runner: PaperStrategyRunner,
    ) -> PaperSessionCheckpoint:
        """Restore a persisted checkpoint into a provided runner.

        Validates identity, schema version, execution mode, and JSON
        payloads. Fail-closed on any mismatch. After restore, the runner's
        existing ``_last_processed_bar`` idempotency ensures re-feeding the
        same bar is a no-op — no duplicate orders.

        The provided ``runner`` must already be bound to the matching
        deployment (same deployment_id, same spec hash, same broker type).
        """
        DeploymentGate.assert_paper_broker(runner.broker)
        if runner.deployment.config.execution_mode != "paper":
            raise NotPaperModeError(runner.deployment.config.execution_mode)
        checkpoint = self.session_store.get_checkpoint(session_id)
        if checkpoint is None:
            raise UnknownDeploymentError(session_id)
        self.session_store.validate_for_restore(
            checkpoint, expected_deployment=runner.deployment
        )
        apply_checkpoint_to_runner(checkpoint, runner)
        sid = session_identity(runner.deployment)
        self._runners[sid] = runner
        return checkpoint

    # ------------------------------------------------------------------ #
    # Inspection (read-only, JSON-serializable)
    # ------------------------------------------------------------------ #
    def inspect_deployment(self, deployment_id: str) -> DashboardDeploymentSummary:
        deployment = self._load_or_cache_deployment(deployment_id)
        return build_deployment_summary(deployment)

    def inspect_session(self, session_id: str) -> PaperSession:
        runner = self._runners.get(session_id)
        if runner is None:
            cp = self.session_store.get_checkpoint(session_id)
            if cp is None:
                raise UnknownDeploymentError(session_id)
            return PaperSession(
                session_id=cp.session_id,
                deployment_id=cp.deployment_id,
                strategy_id=cp.strategy_id,
                strategy_spec_hash=cp.strategy_spec_hash,
                symbol=cp.symbol,
                timeframe=cp.timeframe,
                execution_mode=cp.execution_mode,
                dataset_id=cp.dataset_id,
                deployment_status=PaperDeploymentStatus(cp.deployment_status),
                session_status=PaperSessionStatus(cp.session_status),
                last_processed_bar_timestamp=cp.last_processed_bar_timestamp,
                bar_count=cp.bar_count,
                generated_signals=cp.generated_signals,
                orders_submitted=cp.orders_submitted,
                fills_received=cp.fills_received,
                rejected_orders=cp.rejected_orders,
                starting_equity=cp.starting_equity,
                current_equity=cp.current_equity,
                realized_pnl=cp.realized_pnl,
                unrealized_pnl=cp.unrealized_pnl,
                max_drawdown=cp.max_drawdown,
                health_status=cp.health_status,
                halt_reason=cp.halt_reason,
                consecutive_errors=cp.consecutive_errors,
                circuit_state=cp.circuit_state,
                circuit_reason=cp.circuit_reason,
                circuit_trip_count=cp.circuit_trip_count,
                event_count=cp.event_count,
                event_sequence=cp.event_sequence,
                broker_state=dict(cp.broker_state),
                operations_state_json=dict(cp.operations_state_json),
                schema_version=cp.schema_version,
            )
        return session_from_runner(
            runner=runner, session_id=session_id,
            session_status=PaperSessionStatus(session_id_running_status(runner)),
        )

    def inspect_positions(self, session_id: str) -> DashboardPositionsBlock:
        session = self.inspect_session(session_id)
        pos = session.broker_state.get("position") if session.broker_state else None
        return DashboardPositionsBlock(
            open_position=pos, is_flat=(pos is None)
        )

    def inspect_account(self, session_id: str) -> DashboardAccountBlock:
        session = self.inspect_session(session_id)
        bs = session.broker_state or {}
        starting = session.starting_equity
        total_return: Optional[float] = None
        if starting is not None and starting > 0:
            total_return = (
                (bs.get("equity", 0.0) - starting) / starting
            )
        return DashboardAccountBlock(
            initial_cash=float(bs.get("initial_cash", 0.0)),
            cash=float(bs.get("cash", 0.0)),
            equity=float(bs.get("equity", 0.0)),
            margin_used=float(bs.get("margin_used", 0.0)),
            available_cash=float(bs.get("available_cash", 0.0)),
            realized_pnl=float(bs.get("realized_pnl", 0.0)),
            unrealized_pnl=float(bs.get("unrealized_pnl", 0.0)),
            starting_equity=starting,
            total_return=total_return,
        )

    def inspect_performance(self, session_id: str) -> DashboardPerformanceBlock:
        session = self.inspect_session(session_id)
        bs = session.broker_state or {}
        equity = float(bs.get("equity", 0.0))
        starting = session.starting_equity
        total_return: Optional[float] = None
        if starting is not None and starting > 0:
            total_return = (equity - starting) / starting
        exposure: Optional[float] = None
        pos = bs.get("position") if bs else None
        if pos and equity > 0:
            mv = abs(pos.get("market_value", 0.0))
            if mv:
                exposure = mv / equity
        # Live-runner path: compute trade stats directly from the broker fill
        # ledger so we never report fabricated metrics. When no runner is
        # attached (checkpointed-only state) we use the persisted counters.
        trade_count = 0
        win_rate: Optional[float] = None
        profit_factor: Optional[float] = None
        runner = self._runners.get(session_id)
        if runner is not None:
            tc, wr, pf = _ops_trade_stats(runner.broker, runner.deployment.symbol)
            trade_count, win_rate, profit_factor = tc, wr, pf
        else:
            trade_count = _count_trades(session)
        return DashboardPerformanceBlock(
            realized_pnl=session.realized_pnl,
            unrealized_pnl=session.unrealized_pnl,
            total_pnl=(equity - starting) if starting is not None else None,
            return_=total_return,
            drawdown=session.max_drawdown,
            trade_count=trade_count,
            win_rate=win_rate,
            profit_factor=profit_factor,
            exposure=exposure,
            health_status=session.health_status,
            orders_submitted=session.orders_submitted,
            fills_received=session.fills_received,
            rejected_orders=session.rejected_orders,
            generated_signals=session.generated_signals,
            bar_count=session.bar_count,
        )

    def inspect_health(self, session_id: str) -> DashboardHealthBlock:
        session = self.inspect_session(session_id)
        return DashboardHealthBlock(
            status=session.health_status,
            halt_reason=session.halt_reason,
            warnings=list(_ops_warnings(session)),
        )

    def inspect_risk(self, session_id: str) -> DashboardRiskBlock:
        """Phase 20 risk view.

        Re-evaluates the operations state against the runner's risk guard
        if available; otherwise reports an ALLOW view derived from the
        existing session counters.
        """
        runner = self._runners.get(session_id)
        if runner is not None and runner._risk_guard is not None:
            account = runner.broker.account()
            position = runner.broker.get_position(runner.deployment.symbol)
            decision, reason = runner._risk_guard.check(
                max_drawdown=runner._max_drawdown,
                equity=account.equity,
                position=position,
                rejected_orders=runner._rejected_orders,
                consecutive_errors=runner._consecutive_errors,
            )
            return DashboardRiskBlock(decision=decision.value, reason=reason)
        session = self.inspect_session(session_id)
        return DashboardRiskBlock(decision="allow", reason=None)

    def inspect_circuit_breaker(self, session_id: str) -> DashboardCircuitBreakerBlock:
        session = self.inspect_session(session_id)
        return DashboardCircuitBreakerBlock(
            state=session.circuit_state,
            reason=session.circuit_reason,
            trip_count=session.circuit_trip_count,
        )

    def inspect_events(
        self,
        *,
        session_id: Optional[str] = None,
        deployment_id: Optional[str] = None,
        event_type: Optional[str] = None,
        since_sequence: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """Read-only event log inspection.

        ``since_sequence`` filters by the operation-event sequence number
        (>=). ``limit`` truncates to the most recent N events.
        ``event_type`` accepts the string value (e.g. ``"bar_processed"``).
        """
        runner = self._runners.get(session_id) if session_id else None
        events: list[PaperOperationEvent] = []
        if runner is not None and runner.event_log is not None:
            events = runner.event_log.events
        else:
            # Fall back to checkpointed state: at least the count + sequence.
            return []
        if deployment_id is not None and events:
            events = [e for e in events if e.deployment_id == deployment_id]
        if event_type is not None:
            events = [e for e in events if e.event_type.value == event_type]
        if since_sequence is not None:
            events = [e for e in events if e.sequence >= since_sequence]
        if limit is not None and len(events) > limit:
            events = events[-limit:]
        return [e.model_dump(mode="json") for e in events]

    def inspect_evidence(
        self, *, deployment_id: Optional[str] = None, strategy_id: Optional[str] = None
    ) -> DashboardEvidenceSummary:
        sid = strategy_id
        if sid is None and deployment_id is not None:
            deployment = self.get_deployment(deployment_id)
            if deployment is not None:
                sid = deployment.strategy_id
        if sid is None:
            return DashboardEvidenceSummary()
        research = self.registry.list_evidence(
            strategy_id=sid, evidence_type=EvidenceType.RESEARCH.value
        )
        walk = self.registry.list_evidence(
            strategy_id=sid, evidence_type=EvidenceType.WALK_FORWARD.value
        )
        paper = self.registry.list_evidence(
            strategy_id=sid, evidence_type=EvidenceType.PAPER_TRADING.value
        )
        return _summarize_evidence(research, walk, paper)

    # ------------------------------------------------------------------ #
    # Dashboard snapshot
    # ------------------------------------------------------------------ #
    def build_dashboard_snapshot(self, session_id: str) -> PaperControlCenterSnapshot:
        """Build a single aggregate dashboard payload for ``session_id``."""
        deployment = self._resolve_deployment_for_session(session_id)
        deployment_summary = build_deployment_summary(deployment)
        strategy = self.registry.get_strategy(deployment.strategy_id)
        if strategy is None:
            strategy_summary: Optional[DashboardStrategySummary] = None
        else:
            research = self.registry.list_evidence(
                strategy_id=strategy.strategy_id, evidence_type=EvidenceType.RESEARCH.value
            )
            walk = self.registry.list_evidence(
                strategy_id=strategy.strategy_id, evidence_type=EvidenceType.WALK_FORWARD.value
            )
            paper = self.registry.list_evidence(
                strategy_id=strategy.strategy_id, evidence_type=EvidenceType.PAPER_TRADING.value
            )
            strategy_summary = build_strategy_summary(
                strategy=strategy,
                research_evidences=research,
                walk_forward_evidences=walk,
                paper_evidences=paper,
            )
        session = self.inspect_session(session_id)
        account = self.inspect_account(session_id)
        positions = self.inspect_positions(session_id)
        performance = self.inspect_performance(session_id)
        health = self.inspect_health(session_id)
        risk = self.inspect_risk(session_id)
        circuit = self.inspect_circuit_breaker(session_id)
        recent = self.inspect_events(session_id=session_id, limit=20)
        last = recent[-1] if recent else None
        recent_summary = DashboardEventSummary(
            total_events=session.event_count,
            last_event_sequence=session.event_sequence,
            last_event_type=(last["event_type"] if last else None),
            last_event_timestamp=(last["timestamp"] if last else None),
            recent=recent,
        )
        evidence = self.inspect_evidence(strategy_id=deployment.strategy_id)
        return PaperControlCenterSnapshot(
            generated_at=_now_iso(),
            deployment=deployment_summary,
            strategy=strategy_summary,
            session=session,
            account=account,
            positions=positions,
            performance=performance,
            health=health,
            risk=risk,
            circuit_breaker=circuit,
            recent_events=recent_summary,
            evidence_summary=evidence,
            schema_version=1,
            session_schema_version=SESSION_SCHEMA_VERSION,
        )

    # ------------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------------ #
    def export_report(self, session_id: str) -> PaperTradingReport:
        """Export the Phase 18 ``PaperTradingReport`` for an active session."""
        runner = self._runners.get(session_id)
        if runner is None:
            raise UnknownDeploymentError(session_id)
        from .report import build_report

        broker = runner.broker
        deployment = runner.deployment
        ops = runner.operations_state()
        # Reuse the runner's measurements; build_report reads the broker.
        return build_report(
            deployment=deployment,
            runner=runner,
            broker=broker,
            dataset_id=deployment.dataset_id,
            start=pd_timestamp(ops.started_at),
            end=pd_timestamp(ops.last_bar_timestamp),
        )

    def export_operations_report(self, session_id: str) -> PaperOperationsReport:
        runner = self._runners.get(session_id)
        if runner is None:
            raise UnknownDeploymentError(session_id)
        return build_operations_report(runner.deployment, runner)

    def export_json(self, session_id: str) -> dict:
        """JSON-serializable snapshot + Phase 18/19 reports.

        Contains no credentials, no secrets, no broker internals.
        """
        snapshot = self.build_dashboard_snapshot(session_id)
        try:
            ops_report = self.export_operations_report(session_id)
        except UnknownDeploymentError:
            ops_report = None
        try:
            trading_report = self.export_report(session_id)
        except UnknownDeploymentError:
            trading_report = None
        return {
            "dashboard_snapshot": snapshot.model_dump(mode="json", by_alias=True),
            "operations_report": (
                ops_report.model_dump(mode="json") if ops_report is not None else None
            ),
            "trading_report": (
                trading_report.model_dump(mode="json") if trading_report is not None else None
            ),
            "exported_at": _now_iso(),
            "schema_version": 1,
        }

    def export_json_text(self, session_id: str) -> str:
        return json.dumps(self.export_json(session_id), default=str)

    # ------------------------------------------------------------------ #
    # Circuit breaker helpers
    # ------------------------------------------------------------------ #
    def reset_circuit_breaker(self, session_id: str) -> None:
        """Explicit, caller-driven reset of the circuit breaker.

        The control center NEVER resets a breaker implicitly — a tripped
        breaker can only be closed by the operator calling this method.
        """
        runner = self._runners.get(session_id)
        if runner is None or runner.circuit_breaker is None:
            raise ControlCenterError("no live runner or circuit breaker attached")
        runner.circuit_breaker.reset()

    # ------------------------------------------------------------------ #
    # External order-intent (Day-13 autonomous-agent boundary)
    # ------------------------------------------------------------------ #\n
    def submit_order_intent(
        self,
        session_id: str,
        intent: OrderIntent,
    ) -> OrderResult:
        """Submit a single external order intent through the full safety stack.

        Flow:
            lifecycle check → circuit breaker → risk guard → short-selling
            check → idempotency → broker.submit_order → fill → accounting
            → event log.

        The caller must supply a ``session_id`` that maps to a live, ACTIVE
        runner attached to this control center. PaperBroker is never
        instantiated by external callers.

        ``client_order_id`` provides idempotency: when it is set, a retry with
        the same key returns the previously persisted result instead of
        creating a duplicate order/fill.
        """
        runner = self._runners.get(session_id)
        if runner is None:
            raise UnknownDeploymentError(session_id)

        deployment = runner.deployment

        # --- 1. Lifecycle: only ACTIVE deployments accept external orders ---
        if deployment.status not in STATUS_ACCEPTS_ORDERS:
            self._emit_external_event(
                runner, "order_intent_rejected",
                symbol=intent.symbol, client_order_id=intent.client_order_id,
                reason=f"deployment_status_{deployment.status.value}",
            )
            raise ControlCenterError(
                f"deployment {deployment.deployment_id} is not active "
                f"(status={deployment.status.value}); orders are not accepted"
            )

        # --- 2. Circuit breaker: OPEN means halt ---
        cb = runner.circuit_breaker
        if cb is not None and cb.is_open:
            self._emit_external_event(
                runner, "order_intent_rejected",
                symbol=intent.symbol, client_order_id=intent.client_order_id,
                reason=f"circuit_breaker_open:{cb.reason}",
            )
            raise ControlCenterError(
                f"circuit breaker is open for deployment {deployment.deployment_id}; "
                f"reason={cb.reason}"
            )

        # --- 3. Risk guard ---
        risk = runner._risk_guard
        if risk is not None:
            account = runner.broker.account()
            position = runner.broker.get_position(intent.symbol)
            decision, reason = risk.check(
                max_drawdown=runner._max_drawdown,
                equity=account.equity,
                position=position,
                rejected_orders=runner._rejected_orders,
                consecutive_errors=runner._consecutive_errors,
            )
            if decision == RiskDecision.HALT:
                self._emit_external_event(
                    runner, "order_intent_rejected",
                    symbol=intent.symbol, client_order_id=intent.client_order_id,
                    reason=f"risk_halt:{reason}",
                )
                raise ControlCenterError(
                    f"risk guard halted; reason={reason}"
                )

        # --- 4. Short-selling policy ---
        if intent.side == Side.SELL and not deployment.config.allow_short:
            pos = runner.broker.get_position(intent.symbol)
            pos_qty = pos.qty if pos is not None else 0.0
            # A SELL that would create a short position is rejected.
            if intent.quantity > pos_qty:
                self._emit_external_event(
                    runner, "order_intent_rejected",
                    symbol=intent.symbol, client_order_id=intent.client_order_id,
                    side=intent.side.value, quantity=intent.quantity,
                    position_qty=pos_qty, reason="shorting_disabled",
                )
                raise ControlCenterError(
                    f"short selling is disabled for deployment {deployment.deployment_id}; "
                    f"SELL {intent.quantity} exceeds long position {pos_qty}"
                )

        # --- 5. Idempotency: client_order_id → persisted result ---
        if intent.client_order_id is not None:
            persisted = self.session_store.get_order(
                session_id=session_id,
                client_order_id=intent.client_order_id,
            )
            # If we previously recorded a result, return it (retry-safe).
            # We re-read from the broker to surface current state, but the
            # order/fill was already executed — we must NOT re-execute.
            if persisted is not None and persisted.result_json:
                import json
                cached = json.loads(persisted.result_json)
                return OrderResult(
                    order_id=cached["order_id"],
                    client_order_id=cached.get("client_order_id"),
                    symbol=cached["symbol"],
                    side=cached["side"],
                    quantity=cached["quantity"],
                    order_type=cached["order_type"],
                    limit_price=cached.get("limit_price"),
                    status=cached["status"],
                    filled_quantity=cached["filled_quantity"],
                    avg_fill_price=cached["avg_fill_price"],
                    fills=cached["fills"],
                    cash_after=cached.get("cash_after"),
                    equity_after=cached.get("equity_after"),
                    realized_pnl_after=cached.get("realized_pnl_after"),
                    unrealized_pnl_after=cached.get("unrealized_pnl_after"),
                    position_qty_after=cached.get("position_qty_after"),
                    reject_reason=cached.get("reject_reason", ""),
                    is_idempotent_replay=True,
                )

        # --- 6. Validate inputs against broker rules ---
        broker = runner.broker
        # Pre-validate: broker.submit_order raises BrokerError on bad input.
        try:
            order = broker.submit_order(
                symbol=intent.symbol,
                side=intent.side,
                quantity=intent.quantity,
                order_type=intent.order_type,
                limit_price=intent.limit_price,
                current_price=intent.current_price,
            )
        except BrokerError as exc:
            result = OrderResult(
                order_id="",
                client_order_id=intent.client_order_id,
                symbol=intent.symbol,
                side=intent.side.value,
                quantity=intent.quantity,
                order_type=intent.order_type.value,
                limit_price=intent.limit_price,
                status=OrderStatus.REJECTED.value,
                filled_quantity=0.0,
                avg_fill_price=0.0,
                fills=[],
                cash_after=None,
                equity_after=None,
                realized_pnl_after=None,
                unrealized_pnl_after=None,
                position_qty_after=None,
                reject_reason=str(exc),
            )
            self._emit_external_event(
                runner, "order_intent_rejected",
                symbol=intent.symbol, client_order_id=intent.client_order_id,
                reason=str(exc),
            )
            self._persist_order(
                session_id=session_id, intent=intent,
                order=order, result=result,
            )
            return result

        # --- 7. Build result from the fill ---
        account = broker.account()
        pos = broker.get_position(intent.symbol)
        fills_json = [
            {
                "fill_id": f.fill_id,
                "symbol": f.symbol,
                "side": f.side.value,
                "quantity": float(f.quantity),
                "price": float(f.price),
                "fee": float(f.fee),
            }
            for f in order.fills
        ]
        result = OrderResult(
            order_id=order.order_id,
            client_order_id=intent.client_order_id,
            symbol=intent.symbol,
            side=intent.side.value,
            quantity=order.quantity,
            order_type=order.order_type.value,
            limit_price=order.limit_price,
            status=order.status.value,
            filled_quantity=order.filled_quantity,
            avg_fill_price=order.avg_fill_price,
            fills=fills_json,
            cash_after=float(account.cash),
            equity_after=float(account.equity),
            realized_pnl_after=float(account.realized_pnl),
            unrealized_pnl_after=float(account.unrealized_pnl),
            position_qty_after=(pos.qty if pos is not None else 0.0),
        )

        # --- 8. Event log ---
        self._emit_external_event(
            runner, "order_intent_executed",
            symbol=intent.symbol, client_order_id=intent.client_order_id,
            order_id=order.order_id, side=intent.side.value,
            quantity=intent.quantity, status=order.status.value,
            fill_count=len(order.fills),
        )

        # --- 9. Persist idempotency record ---
        self._persist_order(session_id=session_id, intent=intent,
                            order=order, result=result)
        return result

    def _persist_order(self, session_id: str, intent: OrderIntent, order: Order, result: OrderResult) -> None:
        """Persist the idempotency mapping for this order intent."""
        if intent.client_order_id is None:
            return
        result_dict = {
            "order_id": result.order_id,
            "client_order_id": result.client_order_id,
            "symbol": result.symbol,
            "side": result.side,
            "quantity": result.quantity,
            "order_type": result.order_type,
            "limit_price": result.limit_price,
            "status": result.status,
            "filled_quantity": result.filled_quantity,
            "avg_fill_price": result.avg_fill_price,
            "fills": result.fills,
            "cash_after": result.cash_after,
            "equity_after": result.equity_after,
            "realized_pnl_after": result.realized_pnl_after,
            "unrealized_pnl_after": result.unrealized_pnl_after,
            "position_qty_after": result.position_qty_after,
            "reject_reason": result.reject_reason,
        }
        self.session_store.record_order(
            session_id=session_id,
            client_order_id=intent.client_order_id,
            order_id=result.order_id or order.order_id,
            status=result.status,
            result_json=result_dict,
        )

    def _emit_external_event(self, runner, event_type: str, **payload) -> None:
        """Record an external-order-event into the runner's event log if present."""
        if runner.event_log is None:
            return
        from .events import PaperOperationEventType
        # Map our descriptive event type to the closest existing event type.
        type_map = {
            "order_intent_rejected": PaperOperationEventType.ORDER_REJECTED,
            "order_intent_executed": PaperOperationEventType.ORDER_FILLED,
        }
        op_type = type_map.get(event_type)
        if op_type is None:
            return
        runner.event_log.record(
            op_type,
            _now_iso(),
            event_type,
            payload,
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _resolve_deployment_for_session(self, session_id: str) -> PaperDeployment:
        runner = self._runners.get(session_id)
        if runner is not None:
            return runner.deployment
        cp = self.session_store.get_checkpoint(session_id)
        if cp is None:
            raise UnknownDeploymentError(session_id)
        deployment = self.get_deployment(cp.deployment_id)
        if deployment is None:
            raise UnknownDeploymentError(cp.deployment_id)
        return deployment


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def pd_timestamp(value):
    if value is None:
        return None
    try:
        import pandas as pd

        return pd.Timestamp(value)
    except Exception:
        return None


def _assert_deployment_transition(
    current: PaperDeploymentStatus, target: PaperDeploymentStatus
) -> None:
    """Validate a deployment lifecycle transition against Phase 18 rules."""
    # Map of allowed transitions (mirrors the existing Phase 18 expectations).
    allowed = {
        PaperDeploymentStatus.CREATED: {PaperDeploymentStatus.ACTIVE,
                                        PaperDeploymentStatus.PAUSED,
                                        PaperDeploymentStatus.STOPPED,
                                        PaperDeploymentStatus.FAILED},
        PaperDeploymentStatus.ACTIVE: {PaperDeploymentStatus.PAUSED,
                                       PaperDeploymentStatus.STOPPED,
                                       PaperDeploymentStatus.FAILED},
        PaperDeploymentStatus.PAUSED: {PaperDeploymentStatus.ACTIVE,
                                       PaperDeploymentStatus.STOPPED,
                                       PaperDeploymentStatus.FAILED},
        PaperDeploymentStatus.STOPPED: set(),
        PaperDeploymentStatus.FAILED: set(),
    }
    if target not in allowed.get(current, set()):
        raise InvalidLifecycleTransitionError(
            f"deployment transition {current.value} -> {target.value} is not allowed"
        )


def _summarize_evidence(
    research: list[StrategyEvidence],
    walk: list[StrategyEvidence],
    paper: list[StrategyEvidence],
) -> DashboardEvidenceSummary:
    def _newest(items: list[StrategyEvidence]) -> Optional[str]:
        if not items:
            return None
        return sorted(items, key=lambda e: (e.created_at or "", e.evidence_id),
                      reverse=True)[0].evidence_id
    def _newest_at(items: list[StrategyEvidence]) -> Optional[str]:
        if not items:
            return None
        return sorted(items, key=lambda e: (e.created_at or "", e.evidence_id),
                      reverse=True)[0].created_at or None
    return DashboardEvidenceSummary(
        research_count=len(research),
        walk_forward_count=len(walk),
        paper_trading_count=len(paper),
        latest_research_evidence_id=_newest(research),
        latest_walk_forward_evidence_id=_newest(walk),
        latest_paper_trading_evidence_id=_newest(paper),
        latest_research_at=_newest_at(research),
        latest_walk_forward_at=_newest_at(walk),
        latest_paper_trading_at=_newest_at(paper),
    )


def _count_trades(session: PaperSession) -> int:
    """Round-trip trade count derived from the operational state if present.

    Trade counting requires broker fill-ledger reconstruction which lives in
    the runner. We never fabricate; when the live runner is attached, the
    dashboard performance block is recomputed from the broker.
    """
    js = session.operations_state_json or {}
    if "trade_count" in js and isinstance(js["trade_count"], int):
        return int(js["trade_count"])
    return 0


def _ops_trade_stats(broker: PaperBroker, symbol: str) -> tuple[int, Optional[float], Optional[float]]:
    """Reconstruct round-trip trade stats from the broker's fill ledger.

    Mirrors the Phase 19 helper of the same name in ``paper.report`` to keep
    the Control Center self-contained without re-importing internal helpers.
    """
    fills = []
    for order in broker._orders.values():
        fills.extend(order.fills)
    trades: list[dict] = []
    open_legs: list[dict] = []
    for f in fills:
        if f.symbol != symbol:
            continue
        if f.side.value == "BUY":
            open_legs.append({
                "price": float(f.price),
                "qty": float(f.quantity),
                "fee": float(f.fee),
            })
        else:
            remaining = float(f.quantity)
            close_price = float(f.price)
            close_fee = float(f.fee)
            while remaining > 0 and open_legs:
                leg = open_legs[0]
                leg_qty = min(remaining, leg["qty"])
                gross = (close_price - leg["price"]) * leg_qty
                leg_cost = leg["fee"] * (leg_qty / leg["qty"])
                trades.append({
                    "net": gross - leg_cost - close_fee * (leg_qty / f.quantity),
                })
                leg["qty"] -= leg_qty
                remaining -= leg_qty
                if leg["qty"] <= 1e-12:
                    open_legs.pop(0)
    n = len(trades)
    if n == 0:
        return 0, None, None
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    win_rate = len(wins) / n
    gross_win = sum(t["net"] for t in wins)
    gross_loss = -sum(t["net"] for t in losses)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None
    return n, win_rate, profit_factor


def _ops_warnings(session: PaperSession) -> Iterable[str]:
    js = session.operations_state_json or {}
    out: list[str] = []
    for k in ("warnings", "health_warnings"):
        v = js.get(k)
        if isinstance(v, list):
            out.extend(str(x) for x in v)
    return out


def session_id_running_status(runner: PaperStrategyRunner) -> str:
    """Best-effort session status derived from the live runner."""
    status = runner.deployment.status
    mapping = {
        PaperDeploymentStatus.ACTIVE: PaperSessionStatus.ACTIVE,
        PaperDeploymentStatus.PAUSED: PaperSessionStatus.PAUSED,
        PaperDeploymentStatus.STOPPED: PaperSessionStatus.STOPPED,
        PaperDeploymentStatus.FAILED: PaperSessionStatus.FAILED,
        PaperDeploymentStatus.CREATED: PaperSessionStatus.CREATED,
    }
    return mapping.get(status, PaperSessionStatus.ACTIVE).value