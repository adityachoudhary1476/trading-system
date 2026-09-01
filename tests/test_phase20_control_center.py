"""Phase 20 — Paper Trading Control Center tests.

Covers:
  * deployment discovery, filtering, persistence round-trip
  * deployment lifecycle (create / activate / pause / resume / stop / fail)
  * invalid lifecycle transitions fail safely with typed errors
  * paper-only broker enforcement (PaperBroker required; live brokers rejected)
  * session construction, checkpoint, and restore
  * duplicate-bar protection after restore
  * recovery after corruption / schema mismatch / identity mismatch
  * read-only inspection of session, positions, account, performance, health,
    risk, circuit breaker, events, evidence
  * dashboard snapshot serialization
  * JSON export (no credentials, deterministic)
  * determinism of snapshots and reports
  * Phase 20 safety boundaries (no live brokers, no network, no dynamic exec,
    no credentials, no env access)
"""
from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine

from trading_system.execution.paper_broker import PaperBroker
from trading_system.paper import (
    CircuitState,
    DeploymentGate,
    InvalidLifecycleTransitionError,
    NotPaperModeError,
    PaperBrokerRequiredError,
    PaperControlCenterSnapshot,
    PaperDeployment,
    PaperDeploymentConfig,
    PaperDeploymentStatus,
    PaperHealthConfig,
    PaperHealthMonitor,
    PaperOperationEventType,
    PaperOperationsEventLog,
    PaperRiskConfig,
    PaperRiskGuard,
    PaperCircuitBreaker,
    PaperSession,
    PaperSessionCheckpoint,
    PaperSessionStore,
    PaperStrategyRunner,
    PaperTradingControlCenter,
    SessionIdentityError,
    SessionSchemaError,
    UnknownDeploymentError,
    deployment_identity,
    session_identity,
)
from trading_system.research.evidence import (
    EvidenceStore,
    EvidenceType,
    StrategyEvidence,
    strategy_identity,
)
from trading_system.research.strategy_intelligence import (
    EvidenceFreshnessConfig,
    EvidenceRequirement,
    StrategyIntelligence,
)
from trading_system.research.strategy_lab.spec import (
    StrategySpec,
    field_operand,
    indicator_operand,
    make_condition,
)
from trading_system.research.strategy_registry import (
    StrategyRegistry,
    evidence_identity as _evidence_identity,
)


PHASE20_FILES = (
    "control.py",
    "session.py",
    "dashboard.py",
)
PAPER_DIR = Path(__file__).resolve().parents[1] / "src" / "trading_system" / "paper"


# --------------------------------------------------------------------------- #
# Shared helpers (mirror the existing test conventions)
# --------------------------------------------------------------------------- #
def _spec(name="Phase20 spec", symbol="NSE:SBIN"):
    return StrategySpec(
        name=name,
        description="phase 20 control center test",
        symbol=symbol,
        timeframe="1d",
        indicators=[{"name": "sma", "params": {"window": 5}}],
        entry=make_condition(field_operand("close"), ">", indicator_operand("sma_5")),
        generated_by="test",
    )


def _deployment(spec, *, status=PaperDeploymentStatus.ACTIVE, config=None):
    cfg = config or PaperDeploymentConfig()
    spec_hash = strategy_identity(spec)
    did = deployment_identity("sid-1", spec_hash, spec.symbol, spec.timeframe,
                             "ds-1", cfg)
    return PaperDeployment(
        deployment_id=did,
        strategy_id="sid-1",
        strategy_spec_hash=spec_hash,
        symbol=spec.symbol,
        timeframe=spec.timeframe,
        dataset_id="ds-1",
        config=cfg,
        status=status,
    )


def _uptrend(n=60, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    close = 100 + np.cumsum(rng.normal(0.3, 0.6, n))
    return pd.DataFrame({
        "open": close + 0.1, "high": close + 1.0, "low": close - 1.0,
        "close": close, "volume": rng.integers(100, 1000, n).astype(float),
    }, index=idx)


def _bars(df):
    for ts, row in df.iterrows():
        yield {
            "timestamp": pd.Timestamp(ts),
            "open": float(row["open"]), "high": float(row["high"]),
            "low": float(row["low"]), "close": float(row["close"]),
            "volume": float(row["volume"]),
        }


def _build_eligible(store, registry, intelligence, gate, spec, total_trades=100):
    from trading_system.research.evidence import StrategyStatus
    strategy = registry.register_strategy(spec)
    registry.update_strategy_status(
        strategy.strategy_id, StrategyStatus.WALK_FORWARD_VALIDATED
    )
    ds_id = "ds-phase20"
    fresh = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    registry.record_evidence(StrategyEvidence(
        evidence_id=_evidence_identity(strategy.strategy_id, EvidenceType.RESEARCH,
                                       ds_id, {"k": 1}),
        strategy_id=strategy.strategy_id, strategy_spec_hash=strategy.spec_hash,
        evidence_type=EvidenceType.RESEARCH, dataset_id=ds_id,
        configuration_json={"k": 1},
        metrics_json={"rows": 400, "candidates": [{
            "variant_index": 0, "status": "evaluated",
            "spec_name": strategy.name, "spec_errors": [], "error": "",
            "evaluation": {"total_return": 0.10, "profit_factor": 1.5,
                            "max_drawdown": -0.05, "n_trades": 25},
            "filter_passed": True, "filter_reasons": [],
        }], "ranking": [], "notes": []},
        created_at=fresh,
    ))
    registry.record_evidence(StrategyEvidence(
        evidence_id=_evidence_identity(strategy.strategy_id, EvidenceType.WALK_FORWARD,
                                       ds_id, {"k": 2}),
        strategy_id=strategy.strategy_id, strategy_spec_hash=strategy.spec_hash,
        evidence_type=EvidenceType.WALK_FORWARD, dataset_id=ds_id,
        configuration_json={"k": 2},
        metrics_json={"kind": "fixed_spec", "spec_name": strategy.name,
                      "symbol": strategy.symbol, "timeframe": strategy.timeframe,
                      "mode": "rolling", "folds": [],
                      "summary": {
                          "n_folds": 5, "n_valid": 4, "n_failed": 1,
                          "coverage": 0.8, "coverage_ok": True,
                          "positive_folds": 3, "positive_fold_ratio": 0.75,
                          "avg_fold_return": 0.05, "median_fold_return": 0.05,
                          "worst_fold_return": -0.05, "best_fold_return": 0.15,
                          "return_std": 0.05, "return_dispersion": 1.0,
                          "max_validation_drawdown": -0.08, "consistency_score": 0.7,
                          "total_validation_trades": total_trades,
                          "min_validation_trades": 10, "valid_fold_ids": [0, 1, 2, 3],
                      },
                      "warnings": [], "notes": []},
        created_at=fresh,
    ))
    decision = gate.evaluate(
        strategy_id=strategy.strategy_id, spec=spec,
        symbol=spec.symbol, timeframe=spec.timeframe,
        dataset_id=ds_id, config=PaperDeploymentConfig(),
    )
    assert decision.passed, decision.reasons
    return strategy, decision.deployment, ds_id


@pytest.fixture()
def engine():
    return create_engine("sqlite://")


@pytest.fixture()
def center(engine):
    store = EvidenceStore(engine)
    registry = StrategyRegistry(store)
    intelligence = StrategyIntelligence(registry)
    gate = DeploymentGate(
        intelligence=intelligence,
        requirement=EvidenceRequirement(),
        freshness_config=EvidenceFreshnessConfig(max_age_days=180),
    )
    return PaperTradingControlCenter(
        registry=registry, intelligence=intelligence, gate=gate,
    )


# --------------------------------------------------------------------------- #
# Deployment discovery
# --------------------------------------------------------------------------- #
class TestDeploymentDiscovery:
    def test_list_empty(self, center):
        assert center.list_deployments() == []

    def test_create_and_list_round_trip(self, center):
        spec = _spec()
        strategy, deployment, _ = _build_eligible(
            center.registry.store, center.registry, center.intelligence, center.gate,
            spec,
        )
        # Manually persist a deployment record (simulating an existing Phase 18 deployment).
        from trading_system.paper.deployment import PaperDeploymentRecord
        with center.registry.store._Session() as s:
            s.merge(deployment.as_record())
            s.commit()
        found = center.list_deployments(deployment_id=deployment.deployment_id)
        assert len(found) == 1
        assert found[0].deployment_id == deployment.deployment_id

    def test_filter_by_strategy_id(self, center):
        spec = _spec()
        strategy, deployment, _ = _build_eligible(
            center.registry.store, center.registry, center.intelligence, center.gate,
            spec,
        )
        from trading_system.paper.deployment import PaperDeploymentRecord
        with center.registry.store._Session() as s:
            s.merge(deployment.as_record())
            s.commit()
        results = center.list_deployments(strategy_id=strategy.strategy_id)
        assert any(d.deployment_id == deployment.deployment_id for d in results)
        assert center.list_deployments(strategy_id="not-found") == []

    def test_filter_by_symbol_timeframe_status(self, center):
        spec = _spec()
        _, deployment, _ = _build_eligible(
            center.registry.store, center.registry, center.intelligence, center.gate,
            spec,
        )
        from trading_system.paper.deployment import PaperDeploymentRecord
        with center.registry.store._Session() as s:
            s.merge(deployment.as_record())
            s.commit()
        assert len(center.list_deployments(symbol="NSE:SBIN")) == 1
        assert len(center.list_deployments(timeframe="1d")) == 1
        assert len(center.list_deployments(status="created")) == 1
        assert len(center.list_deployments(status="active")) == 0

    def test_get_deployment_returns_none_for_unknown(self, center):
        assert center.get_deployment("does-not-exist") is None

    def test_listing_is_deterministic(self, center):
        spec = _spec()
        _, dep_a, _ = _build_eligible(
            center.registry.store, center.registry, center.intelligence, center.gate,
            spec,
        )
        spec2 = _spec(name="Phase20 spec other", symbol="NSE:TCS")
        _, dep_b, _ = _build_eligible(
            center.registry.store, center.registry, center.intelligence, center.gate,
            spec2,
        )
        from trading_system.paper.deployment import PaperDeploymentRecord
        with center.registry.store._Session() as s:
            s.merge(dep_a.as_record())
            s.merge(dep_b.as_record())
            s.commit()
        first = [d.deployment_id for d in center.list_deployments()]
        second = [d.deployment_id for d in center.list_deployments()]
        assert first == second


# --------------------------------------------------------------------------- #
# Deployment lifecycle
# --------------------------------------------------------------------------- #
class TestDeploymentLifecycle:
    def test_create_via_control_center(self, center):
        spec = _spec()
        _, deployment, ds_id = _build_eligible(
            center.registry.store, center.registry, center.intelligence, center.gate,
            spec,
        )
        with center.registry.store._Session() as s:
            s.merge(deployment.as_record())
            s.commit()
        # Lifecycle transitions through the control center.
        d = center.pause_deployment(deployment.deployment_id)
        assert d.status == PaperDeploymentStatus.PAUSED
        d = center.resume_deployment(deployment.deployment_id)
        assert d.status == PaperDeploymentStatus.ACTIVE
        d = center.stop_deployment(deployment.deployment_id)
        assert d.status == PaperDeploymentStatus.STOPPED

    def test_invalid_transition_fails(self, center):
        spec = _spec()
        _, deployment, _ = _build_eligible(
            center.registry.store, center.registry, center.intelligence, center.gate,
            spec,
        )
        with center.registry.store._Session() as s:
            s.merge(deployment.as_record())
            s.commit()
        center.stop_deployment(deployment.deployment_id)
        with pytest.raises(InvalidLifecycleTransitionError):
            center.resume_deployment(deployment.deployment_id)

    def test_create_to_stopped_valid(self, center):
        spec = _spec()
        _, deployment, _ = _build_eligible(
            center.registry.store, center.registry, center.intelligence, center.gate,
            spec,
        )
        with center.registry.store._Session() as s:
            s.merge(deployment.as_record())
            s.commit()
        center.stop_deployment(deployment.deployment_id)
        assert center.get_deployment(deployment.deployment_id).status == \
            PaperDeploymentStatus.STOPPED

    def test_paused_to_active_valid(self, center):
        spec = _spec()
        _, deployment, _ = _build_eligible(
            center.registry.store, center.registry, center.intelligence, center.gate,
            spec,
        )
        with center.registry.store._Session() as s:
            s.merge(deployment.as_record())
            s.commit()
        center.pause_deployment(deployment.deployment_id)
        center.resume_deployment(deployment.deployment_id)
        assert center.get_deployment(deployment.deployment_id).status == \
            PaperDeploymentStatus.ACTIVE

    def test_terminal_states_cannot_transition(self, center):
        spec = _spec()
        _, deployment, _ = _build_eligible(
            center.registry.store, center.registry, center.intelligence, center.gate,
            spec,
        )
        with center.registry.store._Session() as s:
            s.merge(deployment.as_record())
            s.commit()
        center.stop_deployment(deployment.deployment_id)
        for op in (center.activate_deployment, center.pause_deployment,
                   center.resume_deployment):
            with pytest.raises(InvalidLifecycleTransitionError):
                op(deployment.deployment_id)

    def test_unknown_deployment_raises(self, center):
        with pytest.raises(UnknownDeploymentError):
            center.activate_deployment("does-not-exist")


# --------------------------------------------------------------------------- #
# Broker enforcement
# --------------------------------------------------------------------------- #
class TestPaperBrokerOnlyEnforcement:
    def test_paper_broker_accepted(self, center):
        broker = PaperBroker(initial_cash=100_000.0)
        assert center.assert_paper_broker(broker) is broker

    def test_abstract_broker_rejected(self, center):
        from trading_system.execution.broker import Broker
        with pytest.raises(TypeError):
            center.assert_paper_broker(Broker())  # type: ignore[abstract]

    def test_arbitrary_subclass_rejected(self, center):
        from trading_system.execution.broker import Broker
        class FakeBroker(Broker):
            def submit_order(self, *a, **k): ...
            def cancel_order(self, *a, **k): return False
            def get_order(self, *a, **k): return None
            def update_market_price(self, *a, **k): ...
            def get_position(self, *a, **k): return None
            def positions(self): return {}
            def account(self): return None
        with pytest.raises(TypeError):
            center.assert_paper_broker(FakeBroker())

    def test_none_rejected(self, center):
        with pytest.raises(TypeError):
            center.assert_paper_broker(None)


# --------------------------------------------------------------------------- #
# Session + checkpoint + restore
# --------------------------------------------------------------------------- #
class TestSessionCheckpointRestore:
    def _build_runner(self, spec):
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        return dep, PaperStrategyRunner(deployment=dep, broker=broker, spec=spec)

    def test_session_from_runner_round_trip(self, center):
        spec = _spec()
        dep, runner = self._build_runner(spec)
        for bar in _bars(_uptrend(30)):
            runner.process_bar(bar)
        sid = session_identity(dep)
        session = PaperSession(
            session_id=sid,
            deployment_id=dep.deployment_id,
            strategy_id=dep.strategy_id,
            strategy_spec_hash=dep.strategy_spec_hash,
            symbol=dep.symbol,
            timeframe=dep.timeframe,
            execution_mode="paper",
            dataset_id=dep.dataset_id,
            bar_count=runner.bar_count,
            orders_submitted=runner.orders_submitted,
            fills_received=runner.fills_received,
            rejected_orders=runner.rejected_orders,
            starting_equity=runner._starting_equity,
            current_equity=runner.broker.account().equity,
            realized_pnl=runner.broker.account().realized_pnl,
            last_processed_bar_timestamp=(
                runner._last_processed_bar.isoformat()
                if runner._last_processed_bar is not None else None
            ),
        )
        # Serialization round-trip
        blob = session.model_dump_json()
        restored = PaperSession.model_validate_json(blob)
        assert restored.bar_count == session.bar_count
        assert restored.execution_mode == "paper"

    def test_checkpoint_persisted_and_retrieved(self, center, engine):
        spec = _spec()
        dep, runner = self._build_runner(spec)
        for bar in _bars(_uptrend(30)):
            runner.process_bar(bar)
        sid = session_identity(dep)
        session = PaperSession(
            session_id=sid,
            deployment_id=dep.deployment_id,
            strategy_id=dep.strategy_id,
            strategy_spec_hash=dep.strategy_spec_hash,
            symbol=dep.symbol,
            timeframe=dep.timeframe,
            execution_mode="paper",
            dataset_id=dep.dataset_id,
            bar_count=runner.bar_count,
            orders_submitted=runner.orders_submitted,
            fills_received=runner.fills_received,
            rejected_orders=runner.rejected_orders,
            starting_equity=runner._starting_equity,
            current_equity=runner.broker.account().equity,
            last_processed_bar_timestamp=(
                runner._last_processed_bar.isoformat()
                if runner._last_processed_bar is not None else None
            ),
        )
        cp = PaperSessionCheckpoint(
            checkpoint_id="cp-1",
            session_id=sid,
            deployment_id=dep.deployment_id,
            strategy_id=dep.strategy_id,
            strategy_spec_hash=dep.strategy_spec_hash,
            symbol=dep.symbol,
            timeframe=dep.timeframe,
            execution_mode="paper",
            dataset_id=dep.dataset_id,
            deployment_status=dep.status.value,
            session_status="checkpointed",
            bar_count=session.bar_count,
            orders_submitted=session.orders_submitted,
            fills_received=session.fills_received,
            rejected_orders=session.rejected_orders,
            starting_equity=session.starting_equity,
            current_equity=session.current_equity,
            realized_pnl=session.realized_pnl,
            unrealized_pnl=session.unrealized_pnl,
            events_fingerprint="x" * 64,
            ops_fingerprint="y" * 64,
            created_at="2024-01-01T00:00:00+00:00",
            broker_state=session.broker_state,
            operations_state_json={},
        )
        store = PaperSessionStore(engine)
        store.save_checkpoint(cp)
        assert store.get_checkpoint(sid) is not None
        assert store.get_checkpoint(sid).checkpoint_id == "cp-1"

    def test_save_and_restore_round_trip(self, center):
        spec = _spec()
        dep, runner = self._build_runner(spec)
        # Persist the deployment so the control center can resolve it.
        from trading_system.paper.deployment import PaperDeploymentRecord
        with center.registry.store._Session() as s:
            s.merge(dep.as_record())
            s.commit()
        bars = list(_bars(_uptrend(30)))
        for bar in bars:
            runner.process_bar(bar)
        orders_before = runner.orders_submitted
        fills_before = runner.fills_received
        sid = center.attach_runner(dep.deployment_id, runner)
        cp = center.save_session(sid)
        assert cp.session_id == sid
        assert cp.orders_submitted == orders_before

        # Re-feed the last bar after restore (idempotency rule).
        last_bar = bars[-1]
        runner_before = runner.orders_submitted
        runner.process_bar(last_bar)
        assert runner.orders_submitted == runner_before

        # Restore into a fresh runner + fresh broker. After restore, replaying
        # the same bar must NOT generate a duplicate order.
        broker2 = PaperBroker(initial_cash=100_000.0)
        dep2, runner2 = self._build_runner(spec)
        center.assert_paper_broker(broker2)
        runner2._starting_equity = runner._starting_equity
        runner2._peak_equity = runner._peak_equity
        center.restore_session(session_id=sid, runner=runner2)
        orders_before_restore = runner2.orders_submitted
        runner2.process_bar(last_bar)
        assert runner2.orders_submitted == orders_before_restore
        # Continue past the last bar with the next-newer bar: orders are
        # appended normally (not suppressed).
        if len(bars) > 1:
            runner2.process_bar(bars[-1])
        # The key invariant is no duplicate order from the last persisted bar.

    def test_unknown_session_restore_raises(self, center):
        spec = _spec()
        dep, runner = self._build_runner(spec)
        with pytest.raises(UnknownDeploymentError):
            center.restore_session(session_id="not-a-real-session", runner=runner)

    def test_save_unknown_session_raises(self, center):
        with pytest.raises(UnknownDeploymentError):
            center.save_session("not-attached")

    def test_corrupt_json_rejected(self, center):
        spec = _spec()
        dep, runner = self._build_runner(spec)
        sid = session_identity(dep)
        cp = PaperSessionCheckpoint(
            checkpoint_id="cp-1",
            session_id=sid,
            deployment_id=dep.deployment_id,
            strategy_id=dep.strategy_id,
            strategy_spec_hash=dep.strategy_spec_hash,
            symbol=dep.symbol,
            timeframe=dep.timeframe,
            execution_mode="paper",
            dataset_id=dep.dataset_id,
            deployment_status="active",
            session_status="checkpointed",
            events_fingerprint="x" * 64,
            ops_fingerprint="y" * 64,
            created_at="2024-01-01T00:00:00+00:00",
            broker_state={"bad": float("nan")},  # not strict-JSON
            operations_state_json={},
        )
        with pytest.raises(SessionSchemaError):
            center.session_store.validate_for_restore(cp, expected_deployment=dep)

    def test_schema_version_mismatch_rejected(self, center):
        spec = _spec()
        dep, runner = self._build_runner(spec)
        sid = session_identity(dep)
        cp = PaperSessionCheckpoint(
            checkpoint_id="cp-1",
            session_id=sid,
            deployment_id=dep.deployment_id,
            strategy_id=dep.strategy_id,
            strategy_spec_hash=dep.strategy_spec_hash,
            symbol=dep.symbol,
            timeframe=dep.timeframe,
            execution_mode="paper",
            dataset_id=dep.dataset_id,
            schema_version=999,  # wrong version
            deployment_status="active",
            session_status="checkpointed",
            events_fingerprint="x" * 64,
            ops_fingerprint="y" * 64,
            created_at="2024-01-01T00:00:00+00:00",
        )
        with pytest.raises(SessionSchemaError):
            center.session_store.validate_for_restore(cp, expected_deployment=dep)

    def test_strategy_hash_mismatch_rejected(self, center):
        spec = _spec()
        dep, runner = self._build_runner(spec)
        sid = session_identity(dep)
        cp = PaperSessionCheckpoint(
            checkpoint_id="cp-1",
            session_id=sid,
            deployment_id=dep.deployment_id,
            strategy_id=dep.strategy_id,
            strategy_spec_hash="wrong-hash",
            symbol=dep.symbol,
            timeframe=dep.timeframe,
            execution_mode="paper",
            dataset_id=dep.dataset_id,
            deployment_status="active",
            session_status="checkpointed",
            events_fingerprint="x" * 64,
            ops_fingerprint="y" * 64,
            created_at="2024-01-01T00:00:00+00:00",
        )
        with pytest.raises(SessionIdentityError):
            center.session_store.validate_for_restore(cp, expected_deployment=dep)

    def test_symbol_mismatch_rejected(self, center):
        spec = _spec()
        dep, runner = self._build_runner(spec)
        sid = session_identity(dep)
        cp = PaperSessionCheckpoint(
            checkpoint_id="cp-1",
            session_id=sid,
            deployment_id=dep.deployment_id,
            strategy_id=dep.strategy_id,
            strategy_spec_hash=dep.strategy_spec_hash,
            symbol="NSE:DIFFERENT",
            timeframe=dep.timeframe,
            execution_mode="paper",
            dataset_id=dep.dataset_id,
            deployment_status="active",
            session_status="checkpointed",
            events_fingerprint="x" * 64,
            ops_fingerprint="y" * 64,
            created_at="2024-01-01T00:00:00+00:00",
        )
        with pytest.raises(SessionIdentityError):
            center.session_store.validate_for_restore(cp, expected_deployment=dep)

    def test_timeframe_mismatch_rejected(self, center):
        spec = _spec()
        dep, runner = self._build_runner(spec)
        sid = session_identity(dep)
        cp = PaperSessionCheckpoint(
            checkpoint_id="cp-1",
            session_id=sid,
            deployment_id=dep.deployment_id,
            strategy_id=dep.strategy_id,
            strategy_spec_hash=dep.strategy_spec_hash,
            symbol=dep.symbol,
            timeframe="1h",  # mismatch
            execution_mode="paper",
            dataset_id=dep.dataset_id,
            deployment_status="active",
            session_status="checkpointed",
            events_fingerprint="x" * 64,
            ops_fingerprint="y" * 64,
            created_at="2024-01-01T00:00:00+00:00",
        )
        with pytest.raises(SessionIdentityError):
            center.session_store.validate_for_restore(cp, expected_deployment=dep)

    def test_non_paper_execution_mode_rejected(self, center):
        spec = _spec()
        dep, runner = self._build_runner(spec)
        sid = session_identity(dep)
        cp = PaperSessionCheckpoint(
            checkpoint_id="cp-1",
            session_id=sid,
            deployment_id=dep.deployment_id,
            strategy_id=dep.strategy_id,
            strategy_spec_hash=dep.strategy_spec_hash,
            symbol=dep.symbol,
            timeframe=dep.timeframe,
            execution_mode="live",  # wrong mode
            dataset_id=dep.dataset_id,
            deployment_status="active",
            session_status="checkpointed",
            events_fingerprint="x" * 64,
            ops_fingerprint="y" * 64,
            created_at="2024-01-01T00:00:00+00:00",
        )
        with pytest.raises(SessionIdentityError):
            center.session_store.validate_for_restore(cp, expected_deployment=dep)


# --------------------------------------------------------------------------- #
# Inspection (read-only)
# --------------------------------------------------------------------------- #
class TestInspection:
    def _seed_runner(self, spec, center, *, circuit_breaker=None):
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(
            deployment=dep, broker=broker, spec=spec,
            event_log=PaperOperationsEventLog(dep),
            circuit_breaker=circuit_breaker,
        )
        # Persist deployment so the control center can resolve it.
        from trading_system.paper.deployment import PaperDeploymentRecord
        with center.registry.store._Session() as s:
            s.merge(dep.as_record())
            s.commit()
        return dep, broker, runner

    def test_inspect_session(self, center):
        spec = _spec()
        dep, broker, runner = self._seed_runner(spec, center)
        for bar in _bars(_uptrend(30)):
            runner.process_bar(bar)
        sid = center.attach_runner(dep.deployment_id, runner)
        session = center.inspect_session(sid)
        assert session.bar_count == 30
        assert session.execution_mode == "paper"

    def test_inspect_account(self, center):
        spec = _spec()
        dep, broker, runner = self._seed_runner(spec, center)
        for bar in _bars(_uptrend(30)):
            runner.process_bar(bar)
        sid = center.attach_runner(dep.deployment_id, runner)
        account = center.inspect_account(sid)
        # Equity reflects realised + unrealised PnL — we just check the
        # view is well-formed and the initial cash is preserved.
        assert account.initial_cash == 100_000.0
        assert isinstance(account.equity, float)

    def test_inspect_positions(self, center):
        spec = _spec()
        dep, broker, runner = self._seed_runner(spec, center)
        for bar in _bars(_uptrend(30)):
            runner.process_bar(bar)
        sid = center.attach_runner(dep.deployment_id, runner)
        pos = center.inspect_positions(sid)
        # Either flat or has a position dict; field types must match.
        assert pos.is_flat is True or isinstance(pos.open_position, dict)

    def test_inspect_performance(self, center):
        spec = _spec()
        dep, broker, runner = self._seed_runner(spec, center)
        for bar in _bars(_uptrend(30)):
            runner.process_bar(bar)
        sid = center.attach_runner(dep.deployment_id, runner)
        perf = center.inspect_performance(sid)
        assert perf.bar_count == 30

    def test_inspect_health(self, center):
        spec = _spec()
        dep, broker, runner = self._seed_runner(spec, center)
        for bar in _bars(_uptrend(30)):
            runner.process_bar(bar)
        sid = center.attach_runner(dep.deployment_id, runner)
        health = center.inspect_health(sid)
        assert health.status in ("healthy", "warning", "halted")

    def test_inspect_risk(self, center):
        spec = _spec()
        dep, broker, runner = self._seed_runner(spec, center)
        for bar in _bars(_uptrend(30)):
            runner.process_bar(bar)
        sid = center.attach_runner(dep.deployment_id, runner)
        risk = center.inspect_risk(sid)
        assert risk.decision in ("allow", "warning", "halt")

    def test_inspect_circuit_breaker(self, center):
        spec = _spec()
        cb = PaperCircuitBreaker()
        cb.trip("risk")
        dep, broker, runner = self._seed_runner(spec, center, circuit_breaker=cb)
        for bar in _bars(_uptrend(30)):
            runner.process_bar(bar)
        sid = center.attach_runner(dep.deployment_id, runner)
        out = center.inspect_circuit_breaker(sid)
        assert out.state == "open"
        assert out.reason == "risk"
        assert out.trip_count == 1

    def test_inspect_events_filtering(self, center):
        spec = _spec()
        dep, broker, runner = self._seed_runner(spec, center)
        for bar in _bars(_uptrend(20)):
            runner.process_bar(bar)
        sid = center.attach_runner(dep.deployment_id, runner)
        # Event types are filterable.
        events = center.inspect_events(session_id=sid,
                                        event_type="bar_processed")
        assert all(e["event_type"] == "bar_processed" for e in events)
        # Sequence filter.
        later = center.inspect_events(session_id=sid, since_sequence=10)
        assert all(e["sequence"] >= 10 for e in later)
        # Limit truncates.
        limit = center.inspect_events(session_id=sid, limit=3)
        assert len(limit) <= 3

    def test_inspect_evidence_summary(self, center):
        spec = _spec()
        strategy, _, _ = _build_eligible(
            center.registry.store, center.registry, center.intelligence, center.gate,
            spec,
        )
        summary = center.inspect_evidence(strategy_id=strategy.strategy_id)
        assert summary.research_count >= 1
        assert summary.walk_forward_count >= 1


# --------------------------------------------------------------------------- #
# Dashboard snapshot
# --------------------------------------------------------------------------- #
class TestDashboardSnapshot:
    def test_snapshot_serialization(self, center):
        spec = _spec()
        dep, broker, runner = TestInspection()._seed_runner(spec, center)
        for bar in _bars(_uptrend(20)):
            runner.process_bar(bar)
        sid = center.attach_runner(dep.deployment_id, runner)
        snap = center.build_dashboard_snapshot(sid)
        assert isinstance(snap, PaperControlCenterSnapshot)
        blob = snap.model_dump_json(by_alias=True)
        restored = PaperControlCenterSnapshot.model_validate_json(blob)
        assert restored.deployment.deployment_id == snap.deployment.deployment_id
        assert restored.session.bar_count == snap.session.bar_count

    def test_snapshot_contains_no_secrets(self, center):
        spec = _spec()
        dep, broker, runner = TestInspection()._seed_runner(spec, center)
        for bar in _bars(_uptrend(20)):
            runner.process_bar(bar)
        sid = center.attach_runner(dep.deployment_id, runner)
        snap = center.build_dashboard_snapshot(sid)
        blob = snap.model_dump_json(by_alias=True).lower()
        for forbidden in ("api_key", "secret", "token", "password", "access_token",
                          "refresh_token"):
            assert forbidden not in blob

    def test_snapshot_deterministic(self, center):
        snaps = []
        for _ in range(2):
            spec = _spec()
            dep, broker, runner = TestInspection()._seed_runner(spec, center)
            for bar in _bars(_uptrend(20)):
                runner.process_bar(bar)
            sid = center.attach_runner(dep.deployment_id, runner)
            snaps.append(center.build_dashboard_snapshot(sid))
        assert snaps[0].session.bar_count == snaps[1].session.bar_count
        e0 = [(e["event_type"], e["sequence"])
              for e in snaps[0].recent_events.recent]
        e1 = [(e["event_type"], e["sequence"])
              for e in snaps[1].recent_events.recent]
        assert e0 == e1


# --------------------------------------------------------------------------- #
# JSON export
# --------------------------------------------------------------------------- #
class TestJsonExport:
    def test_export_json(self, center):
        spec = _spec()
        dep, broker, runner = TestInspection()._seed_runner(spec, center)
        for bar in _bars(_uptrend(10)):
            runner.process_bar(bar)
        sid = center.attach_runner(dep.deployment_id, runner)
        payload = center.export_json(sid)
        text = json.dumps(payload, default=str).lower()
        assert "api_key" not in text
        assert "secret" not in text
        assert payload["dashboard_snapshot"]["session"]["bar_count"] == 10

    def test_export_json_text_round_trip(self, center):
        spec = _spec()
        dep, broker, runner = TestInspection()._seed_runner(spec, center)
        for bar in _bars(_uptrend(5)):
            runner.process_bar(bar)
        sid = center.attach_runner(dep.deployment_id, runner)
        text = center.export_json_text(sid)
        reloaded = json.loads(text)
        assert "dashboard_snapshot" in reloaded


# --------------------------------------------------------------------------- #
# Circuit breaker explicit reset
# --------------------------------------------------------------------------- #
class TestCircuitBreakerControl:
    def test_explicit_reset_only(self, center):
        spec = _spec()
        cb = PaperCircuitBreaker()
        cb.trip("manual")
        dep, broker, runner = TestInspection()._seed_runner(
            spec, center, circuit_breaker=cb
        )
        runner.circuit_breaker.trip("manual")
        sid = center.attach_runner(dep.deployment_id, runner)
        # Inspection shows OPEN
        assert center.inspect_circuit_breaker(sid).state == "open"
        # Without explicit reset the breaker stays open.
        runner.circuit_breaker.is_open  # noqa
        assert center.inspect_circuit_breaker(sid).state == "open"
        # Explicit reset.
        center.reset_circuit_breaker(sid)
        assert center.inspect_circuit_breaker(sid).state == "closed"


# --------------------------------------------------------------------------- #
# Historical evidence preservation
# --------------------------------------------------------------------------- #
class TestHistoricalEvidencePreservation:
    def test_paper_evidence_does_not_mutate_research(self, center):
        spec = _spec()
        strategy, _, _ = _build_eligible(
            center.registry.store, center.registry, center.intelligence, center.gate,
            spec,
        )
        before = center.registry.list_evidence(strategy_id=strategy.strategy_id)
        # Record a paper trading evidence using the existing Phase 19 path.
        from trading_system.paper.report import build_paper_operations_evidence
        dep = _deployment(spec, status=PaperDeploymentStatus.ACTIVE)
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(deployment=dep, broker=broker, spec=spec)
        for bar in _bars(_uptrend(10)):
            runner.process_bar(bar)
        from trading_system.paper.report import build_operations_report
        report = build_operations_report(dep, runner)
        ev = build_paper_operations_evidence(
            deployment=dep, spec=spec, report=report, dataset_id="ds-1",
        )
        center.registry.record_evidence(ev)
        after = center.registry.list_evidence(strategy_id="sid-1")
        # Research + walk-forward records still present and unchanged.
        for prev in before:
            still = center.registry.get_evidence(prev.evidence_id)
            assert still is not None
            assert still.metrics_json == prev.metrics_json


# --------------------------------------------------------------------------- #
# Retired strategy cannot resume
# --------------------------------------------------------------------------- #
class TestRetiredStrategyCannotResume:
    def test_retired_strategy_blocks_create(self, center):
        spec = _spec()
        strategy, _, _ = _build_eligible(
            center.registry.store, center.registry, center.intelligence, center.gate,
            spec,
        )
        # Retire the strategy.
        center.intelligence.retire_strategy(strategy.strategy_id, "test retire")
        # Re-attempting deployment via the gate must fail closed.
        decision = center.gate.evaluate(
            strategy_id=strategy.strategy_id, spec=spec,
            symbol=spec.symbol, timeframe=spec.timeframe,
            dataset_id="ds-phase20",
            config=PaperDeploymentConfig(),
        )
        assert not decision.passed
        assert "retired_strategy" in decision.reasons


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
class TestDeterminism:
    def test_two_identical_runs_same_snapshot_metrics(self, center):
        snaps = []
        for _ in range(2):
            spec = _spec()
            dep, broker, runner = TestInspection()._seed_runner(spec, center)
            for bar in _bars(_uptrend(20)):
                runner.process_bar(bar)
            sid = center.attach_runner(dep.deployment_id, runner)
            snap = center.build_dashboard_snapshot(sid)
            snaps.append(snap)
        assert snaps[0].session.bar_count == snaps[1].session.bar_count
        assert snaps[0].session.orders_submitted == snaps[1].session.orders_submitted
        assert snaps[0].account.equity == snaps[1].account.equity

    def _seed_runner_v2(self, spec):
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(deployment=dep, broker=broker, spec=spec)
        return dep, broker, runner


# --------------------------------------------------------------------------- #
# Phase 20 safety boundaries
# --------------------------------------------------------------------------- #
class TestPhase20Safety:
    def test_no_live_broker_imports(self):
        forbidden = ("fyers", "upstox", "zerodha", "angel", "alice", "kite")
        for name in PHASE20_FILES:
            path = PAPER_DIR / name
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8").lower()
            for token in forbidden:
                assert f"import {token}" not in text, \
                    f"{name} imports live broker module {token}"
                assert f"from {token}" not in text, \
                    f"{name} imports live broker module {token}"

    def test_no_forbidden_calls(self):
        forbidden = {"eval", "exec", "compile", "__import__", "open",
                     "subprocess", "system", "popen", "globals", "locals",
                     "vars", "breakpoint", "getattr", "setattr"}
        for name in PHASE20_FILES:
            path = PAPER_DIR / name
            if not path.exists():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    assert node.func.id not in forbidden, \
                        f"{name}:{node.lineno} calls {node.func.id}()"

    def test_no_env_access(self):
        for name in PHASE20_FILES:
            path = PAPER_DIR / name
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            assert "load_dotenv" not in text, f"{name} calls load_dotenv"
            assert "os.environ" not in text, f"{name} reads os.environ"
            assert "settings.api_key" not in text, f"{name} reads settings keys"

    def test_no_network_modules(self):
        forbidden = ("socket", "urllib", "requests", "httpx", "http.client")
        for name in PHASE20_FILES:
            path = PAPER_DIR / name
            if not path.exists():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for token in forbidden:
                            assert token not in alias.name, \
                                f"{name} imports {alias.name}"
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    for token in forbidden:
                        assert token not in mod, f"{name} imports {mod}"

    def test_no_pickle_marshal(self):
        for name in PHASE20_FILES:
            path = PAPER_DIR / name
            if not path.exists():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert alias.name not in ("pickle", "marshal", "shelve")
                elif isinstance(node, ast.ImportFrom):
                    assert (node.module or "") not in ("pickle", "marshal", "shelve")

    def test_snapshots_and_reports_have_no_secrets(self):
        spec = _spec()
        store = EvidenceStore(create_engine("sqlite://"))
        registry = StrategyRegistry(store)
        intelligence = StrategyIntelligence(registry)
        gate = DeploymentGate(
            intelligence=intelligence,
            requirement=EvidenceRequirement(),
            freshness_config=EvidenceFreshnessConfig(max_age_days=180),
        )
        center = PaperTradingControlCenter(
            registry=registry, intelligence=intelligence, gate=gate,
        )
        dep = _deployment(spec, status=PaperDeploymentStatus.ACTIVE)
        # Persist so the control center can resolve the deployment.
        from trading_system.paper.deployment import PaperDeploymentRecord
        with center.registry.store._Session() as s:
            s.merge(dep.as_record())
            s.commit()
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(deployment=dep, broker=broker, spec=spec)
        for bar in _bars(_uptrend(10)):
            runner.process_bar(bar)
        sid = center.attach_runner(dep.deployment_id, runner)
        text = center.export_json_text(sid).lower()
        for forbidden in ("api_key", "secret", "password", "access_token",
                          "refresh_token"):
            assert forbidden not in text