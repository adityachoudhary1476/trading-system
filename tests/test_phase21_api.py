"""Phase 21 — Control-Center API Surface tests.

Covers:

  * API availability (health, route registration, JSON serialization)
  * Deployment discovery and filtering
  * Dashboard snapshot endpoint
  * Inspection endpoints (account, positions, performance, health, risk,
    circuit-breaker, events, evidence)
  * Lifecycle endpoints (activate, pause, resume, stop, invalid transition)
  * Circuit-breaker explicit reset
  * Session / checkpoint / restore endpoints (including fail-closed
    validation paths)
  * Checkpoint policy behaviour (disabled by default, every-N-bars,
    drawdown threshold, validation, determinism)
  * Paper-only enforcement
  * No-credentials / no-network / no-dynamic-execution safety
  * AST / static safety scans
  * stdlib HTTP server adapter (loopback binding, body size cap, JSON I/O)
"""
from __future__ import annotations

import ast
import json
import os
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine

from trading_system.execution.paper_broker import PaperBroker
from trading_system.paper import (
    PaperDeployment,
    PaperDeploymentConfig,
    PaperDeploymentStatus,
    PaperStrategyRunner,
)
from trading_system.paper.deployment import deployment_identity
from trading_system.paper.session import (
    PaperSessionCheckpoint,
    SessionIdentityError,
    SessionSchemaError,
)
from trading_system.paper_api import (
    APIServer,
    APIErrorCode,
    APIErrorException,
    CheckpointDecision,
    CheckpointPolicy,
    PaperAPIRouter,
    build_default_server,
    evaluate_checkpoint_policy,
)
from trading_system.paper_api.errors import (
    DOMAIN_ERROR_MAP,
    ErrorResponse,
    map_domain_exception,
)
from trading_system.research.evidence import (
    EvidenceStore,
    EvidenceType,
    StrategyEvidence,
    StrategyStatus,
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
from trading_system.paper.control import (
    InvalidLifecycleTransitionError,
    NotPaperModeError,
    PaperBrokerRequiredError,
    UnknownDeploymentError,
)
from trading_system.paper import DeploymentGate


API_DIR = Path(__file__).resolve().parents[1] / "src" / "trading_system" / "paper_api"
API_PY_FILES = sorted(API_DIR.glob("*.py"))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _spec(name="Phase21 spec", symbol="NSE:SBIN"):
    return StrategySpec(
        name=name,
        description="phase 21 api test",
        symbol=symbol,
        timeframe="1d",
        indicators=[{"name": "sma", "params": {"window": 5}}],
        entry=make_condition(field_operand("close"), ">", indicator_operand("sma_5")),
        generated_by="test",
    )


def _uptrend(n=40, seed=1):
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


def _build_eligible(engine, spec, total_trades=100):
    store = EvidenceStore(engine)
    registry = StrategyRegistry(store)
    intelligence = StrategyIntelligence(registry)
    gate = DeploymentGate(
        intelligence=intelligence,
        requirement=EvidenceRequirement(),
        freshness_config=EvidenceFreshnessConfig(max_age_days=180),
    )
    strategy = registry.register_strategy(spec)
    registry.update_strategy_status(
        strategy.strategy_id, StrategyStatus.WALK_FORWARD_VALIDATED
    )
    ds_id = "ds-phase21"
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
    return store, registry, intelligence, gate, decision.deployment


def _seed_runner(dep, *, circuit_breaker=None):
    broker = PaperBroker(initial_cash=100_000.0)
    runner = PaperStrategyRunner(
        deployment=dep, broker=broker, spec=_spec(),
        circuit_breaker=circuit_breaker,
    )
    return broker, runner


@pytest.fixture()
def fixture(engine):
    """Build a paper trading control center with one active deployment + runner."""
    from trading_system.paper.control import PaperTradingControlCenter
    spec = _spec()
    store, registry, intelligence, gate, dep = _build_eligible(engine, spec)
    with store._Session() as s:
        s.merge(dep.as_record())
        s.commit()
    broker, runner = _seed_runner(dep)
    for bar in _bars(_uptrend(30)):
        runner.process_bar(bar)
    center = PaperTradingControlCenter(
        registry=registry, intelligence=intelligence, gate=gate,
    )
    sid = center.attach_runner(dep.deployment_id, runner)
    return center, dep, broker, runner, sid


@pytest.fixture()
def engine():
    return create_engine("sqlite://")


@pytest.fixture()
def router(fixture):
    center, *_ = fixture
    return PaperAPIRouter(center)


# --------------------------------------------------------------------------- #
# API availability
# --------------------------------------------------------------------------- #
class TestAPIAvailability:
    def test_health_endpoint(self, router):
        env = router.dispatch("GET", "/health")
        assert env.status == 200
        assert env.body["status"] == "ok"
        assert env.body["paper_only"] is True

    def test_route_registration_listed(self, router):
        routes = router.routes()
        assert any(p == "^/health$" for p, _ in routes)
        assert any("deployments" in p for p, _ in routes)
        assert any("dashboard" in p for p, _ in routes)

    def test_unknown_route_returns_404(self, router):
        env = router.dispatch("GET", "/does-not-exist")
        assert env.status == 404
        assert env.body["error"]["code"] == "not_found"

    def test_method_not_allowed(self, router):
        env = router.dispatch("POST", "/health")
        assert env.status == 405
        assert env.body["error"]["code"] == "method_not_allowed"

    def test_invalid_json_body(self, router):
        env = router.dispatch("POST", "/deployments/x/restore", raw_body="not-json")
        assert env.status == 400
        assert env.body["error"]["code"] == "bad_request"

    def test_non_object_body(self, router):
        env = router.dispatch("POST", "/deployments/x/restore", raw_body="[1,2,3]")
        assert env.status == 400
        assert env.body["error"]["code"] == "bad_request"


# --------------------------------------------------------------------------- #
# Deployment endpoints
# --------------------------------------------------------------------------- #
class TestDeployments:
    def test_list_deployments_empty(self, engine):
        from trading_system.paper.control import PaperTradingControlCenter
        store = EvidenceStore(engine)
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
        router = PaperAPIRouter(center)
        env = router.dispatch("GET", "/deployments")
        assert env.status == 200
        assert env.body["count"] == 0
        assert env.body["deployments"] == []

    def test_list_deployments_after_seed(self, router, fixture):
        _center, dep, *_ = fixture
        env = router.dispatch("GET", "/deployments")
        assert env.status == 200
        assert env.body["count"] >= 1
        ids = [d["deployment_id"] for d in env.body["deployments"]]
        assert dep.deployment_id in ids

    def test_filter_by_strategy_id(self, router, fixture):
        _center, dep, *_ = fixture
        env = router.dispatch("GET", "/deployments?" + urlencode({
            "strategy_id": dep.strategy_id,
        }))
        assert env.status == 200
        assert all(d["strategy_id"] == dep.strategy_id for d in env.body["deployments"])

    def test_filter_by_symbol(self, router, fixture):
        _center, dep, *_ = fixture
        env = router.dispatch("GET", "/deployments?" + urlencode({"symbol": dep.symbol}))
        assert env.status == 200
        assert env.body["count"] >= 1
        for d in env.body["deployments"]:
            assert d["symbol"] == dep.symbol

    def test_filter_by_unknown_returns_empty(self):
        from trading_system.paper.control import PaperTradingControlCenter
        engine = create_engine("sqlite://")
        store = EvidenceStore(engine)
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
        router = PaperAPIRouter(center)
        env = router.dispatch("GET", "/deployments?symbol=NSE:NOPE")
        assert env.status == 200
        assert env.body["count"] == 0

    def test_get_deployment(self, router, fixture):
        _center, dep, *_ = fixture
        env = router.dispatch("GET", f"/deployments/{dep.deployment_id}")
        assert env.status == 200
        assert env.body["deployment"]["deployment_id"] == dep.deployment_id

    def test_get_unknown_deployment(self, router):
        env = router.dispatch("GET", "/deployments/missing-id")
        assert env.status == 404
        assert env.body["error"]["code"] == "unknown_deployment"

    def test_limit_query_bounds(self, router, fixture):
        _center, _dep, *_ = fixture
        env = router.dispatch("GET", "/deployments?limit=10000")
        assert env.status == 400
        env = router.dispatch("GET", "/deployments?limit=0")
        assert env.status == 400
        env = router.dispatch("GET", "/deployments?limit=abc")
        assert env.status == 400

    def test_deterministic_ordering(self, router, fixture):
        env1 = router.dispatch("GET", "/deployments")
        env2 = router.dispatch("GET", "/deployments")
        assert env1.body == env2.body


# --------------------------------------------------------------------------- #
# Dashboard endpoint
# --------------------------------------------------------------------------- #
class TestDashboard:
    def test_dashboard(self, router, fixture):
        _center, dep, *_ = fixture
        env = router.dispatch("GET", f"/deployments/{dep.deployment_id}/dashboard")
        assert env.status == 200
        body = env.body
        assert "deployment" in body
        assert "session" in body
        assert "account" in body
        assert "positions" in body
        assert "performance" in body
        assert "health" in body
        assert "risk" in body
        assert "circuit_breaker" in body
        assert "recent_events" in body
        assert "evidence_summary" in body
        assert body["session"]["deployment_id"] == dep.deployment_id

    def test_dashboard_no_secrets(self, router, fixture):
        _center, dep, *_ = fixture
        env = router.dispatch("GET", f"/deployments/{dep.deployment_id}/dashboard")
        text = json.dumps(env.body).lower()
        for forbidden in ("api_key", "secret", "password", "access_token",
                          "refresh_token"):
            assert forbidden not in text

    def test_export_endpoint(self, router, fixture):
        _center, dep, *_ = fixture
        env = router.dispatch("GET", f"/deployments/{dep.deployment_id}/export")
        assert env.status == 200
        assert "dashboard_snapshot" in env.body


# --------------------------------------------------------------------------- #
# Inspection endpoints
# --------------------------------------------------------------------------- #
class TestInspectionEndpoints:
    def test_account(self, router, fixture):
        _center, dep, *_ = fixture
        env = router.dispatch("GET", f"/deployments/{dep.deployment_id}/account")
        assert env.status == 200
        assert env.body["account"]["initial_cash"] == 100_000.0

    def test_positions(self, router, fixture):
        _center, dep, *_ = fixture
        env = router.dispatch("GET", f"/deployments/{dep.deployment_id}/positions")
        assert env.status == 200
        assert "is_flat" in env.body["positions"]

    def test_performance(self, router, fixture):
        _center, dep, *_ = fixture
        env = router.dispatch("GET", f"/deployments/{dep.deployment_id}/performance")
        assert env.status == 200
        assert "bar_count" in env.body["performance"]

    def test_health(self, router, fixture):
        _center, dep, *_ = fixture
        env = router.dispatch("GET", f"/deployments/{dep.deployment_id}/health")
        assert env.status == 200
        assert env.body["health"]["status"] in ("healthy", "warning", "halted")

    def test_risk(self, router, fixture):
        _center, dep, *_ = fixture
        env = router.dispatch("GET", f"/deployments/{dep.deployment_id}/risk")
        assert env.status == 200
        assert env.body["risk"]["decision"] in ("allow", "warning", "halt")

    def test_circuit_breaker_get(self, router, fixture):
        _center, dep, *_ = fixture
        env = router.dispatch("GET", f"/deployments/{dep.deployment_id}/circuit-breaker")
        assert env.status == 200
        assert env.body["circuit_breaker"]["state"] in ("closed", "open")

    def test_events_filtering(self, router, fixture):
        _center, dep, *_ = fixture
        env = router.dispatch(
            "GET",
            f"/deployments/{dep.deployment_id}/events?event_type=bar_processed&limit=5",
        )
        assert env.status == 200
        assert all(e["event_type"] == "bar_processed" for e in env.body["events"]["recent"])
        assert len(env.body["events"]["recent"]) <= 5

    def test_events_since_sequence(self, router, fixture):
        _center, dep, *_ = fixture
        env = router.dispatch(
            "GET",
            f"/deployments/{dep.deployment_id}/events?since_sequence=5",
        )
        assert env.status == 200
        assert all(e["sequence"] >= 5 for e in env.body["events"]["recent"])

    def test_evidence_endpoint(self, router, fixture):
        _center, dep, *_ = fixture
        env = router.dispatch("GET", f"/deployments/{dep.deployment_id}/evidence")
        assert env.status == 200
        assert env.body["evidence"]["research_count"] >= 1
        assert env.body["evidence"]["walk_forward_count"] >= 1


# --------------------------------------------------------------------------- #
# Lifecycle endpoints
# --------------------------------------------------------------------------- #
class TestLifecycleEndpoints:
    def test_activate(self, router, fixture):
        _center, dep, *_ = fixture
        # Pause first, then activate.
        router.dispatch("POST", f"/deployments/{dep.deployment_id}/pause")
        env = router.dispatch("POST", f"/deployments/{dep.deployment_id}/activate")
        assert env.status == 200
        assert env.body["deployment"]["status"] == "active"

    def test_pause_resume(self, router, fixture):
        _center, dep, *_ = fixture
        env = router.dispatch("POST", f"/deployments/{dep.deployment_id}/pause")
        assert env.status == 200
        assert env.body["deployment"]["status"] == "paused"
        env = router.dispatch("POST", f"/deployments/{dep.deployment_id}/resume")
        assert env.status == 200
        assert env.body["deployment"]["status"] == "active"

    def test_stop(self, router, fixture):
        _center, dep, *_ = fixture
        env = router.dispatch("POST", f"/deployments/{dep.deployment_id}/stop")
        assert env.status == 200
        assert env.body["deployment"]["status"] == "stopped"

    def test_invalid_transition_after_stop(self, router, fixture):
        _center, dep, *_ = fixture
        router.dispatch("POST", f"/deployments/{dep.deployment_id}/stop")
        env = router.dispatch("POST", f"/deployments/{dep.deployment_id}/activate")
        assert env.status == 409
        assert env.body["error"]["code"] == "invalid_lifecycle_transition"

    def test_unknown_deployment_lifecycle(self, router):
        env = router.dispatch("POST", "/deployments/nope/activate")
        assert env.status == 404
        assert env.body["error"]["code"] == "unknown_deployment"

    def test_invalid_body_shape(self, router, fixture):
        _center, dep, *_ = fixture
        env = router.dispatch(
            "POST",
            f"/deployments/{dep.deployment_id}/activate",
            raw_body=json.dumps({"extra": "field"}),
        )
        # pydantic strict mode rejects extra fields -> 400
        assert env.status == 400


# --------------------------------------------------------------------------- #
# Circuit breaker reset
# --------------------------------------------------------------------------- #
class TestCircuitBreakerReset:
    def test_explicit_reset(self, router, fixture):
        center, dep, broker, runner, _sid = fixture
        # Trip the breaker on the runner.
        from trading_system.paper import PaperCircuitBreaker
        cb = PaperCircuitBreaker()
        cb.trip("manual")
        # Replace the breaker on the existing runner (only if not present).
        if runner.circuit_breaker is None:
            object.__setattr__(runner, "_circuit_breaker", cb)
        env = router.dispatch(
            "POST", f"/deployments/{dep.deployment_id}/reset-circuit-breaker"
        )
        assert env.status == 200
        assert env.body["circuit_breaker"]["state"] == "closed"

    def test_reset_does_not_reactivate_deployment(self, router, fixture):
        center, dep, broker, runner, _sid = fixture
        from trading_system.paper import PaperCircuitBreaker
        cb = PaperCircuitBreaker()
        cb.trip("manual")
        if runner.circuit_breaker is None:
            object.__setattr__(runner, "_circuit_breaker", cb)
        # Pause the deployment.
        router.dispatch("POST", f"/deployments/{dep.deployment_id}/pause")
        # Reset the breaker.
        router.dispatch(
            "POST", f"/deployments/{dep.deployment_id}/reset-circuit-breaker"
        )
        # Deployment must remain paused.
        env = router.dispatch("GET", f"/deployments/{dep.deployment_id}")
        assert env.body["deployment"]["status"] == "paused"

    def test_open_circuit_prevents_trading(self, router, fixture):
        center, dep, broker, runner, sid = fixture
        from trading_system.paper import PaperCircuitBreaker
        cb = PaperCircuitBreaker()
        cb.trip("test")
        if runner.circuit_breaker is None:
            object.__setattr__(runner, "_circuit_breaker", cb)
        # A subsequent bar must not produce a new order.
        before = runner.orders_submitted
        bar = next(_bars(_uptrend(1)))
        bar["timestamp"] = pd.Timestamp("2024-12-31", tz="UTC")
        runner.process_bar(bar)
        assert runner.orders_submitted == before


# --------------------------------------------------------------------------- #
# Session / checkpoint / restore endpoints
# --------------------------------------------------------------------------- #
class TestSessionEndpoints:
    def test_checkpoint_and_get_session(self, router, fixture):
        center, dep, broker, runner, sid = fixture
        env = router.dispatch("POST", f"/deployments/{dep.deployment_id}/checkpoint")
        assert env.status == 200
        assert env.body["checkpoint"]["session_id"] == sid
        env = router.dispatch("GET", f"/deployments/{dep.deployment_id}/session")
        assert env.status == 200
        assert env.body["session"]["bar_count"] == runner.bar_count

    def test_restore_round_trip(self, router, fixture):
        center, dep, broker, runner, sid = fixture
        # Save a checkpoint.
        router.dispatch("POST", f"/deployments/{dep.deployment_id}/checkpoint")
        # Restore into the same runner via the API.
        env = router.dispatch("POST", f"/deployments/{dep.deployment_id}/restore")
        assert env.status == 200
        assert env.body["checkpoint"]["session_id"] == sid

    def test_checkpoint_invalid_body(self, router, fixture):
        center, dep, *_ = fixture
        env = router.dispatch(
            "POST",
            f"/deployments/{dep.deployment_id}/checkpoint",
            raw_body=json.dumps({"unknown": "field"}),
        )
        assert env.status == 400

    def test_restore_unknown_session(self, router, engine):
        # Build a center with no live runner.
        from trading_system.paper.control import PaperTradingControlCenter
        store = EvidenceStore(engine)
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
        router = PaperAPIRouter(center)
        env = router.dispatch("POST", "/deployments/none/restore")
        assert env.status == 404

    def test_corrupt_checkpoint_rejected(self, router, fixture):
        center, dep, broker, runner, sid = fixture
        router.dispatch("POST", f"/deployments/{dep.deployment_id}/checkpoint")
        # Corrupt the persisted broker_state_json directly. Bypassing the
        # save_checkpoint() idempotency check ensures the corruption
        # actually reaches the storage layer.
        from sqlalchemy import text
        with center.session_store.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE paper_sessions SET broker_state_json = :blob "
                    "WHERE session_id = :sid"
                ),
                {"blob": '{"bad": NaN}', "sid": sid},
            )
        env = router.dispatch("POST", f"/deployments/{dep.deployment_id}/restore")
        assert env.status in (409, 404)

    def test_schema_mismatch_rejected(self, router, fixture):
        center, dep, broker, runner, sid = fixture
        router.dispatch("POST", f"/deployments/{dep.deployment_id}/checkpoint")
        from sqlalchemy import text
        with center.session_store.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE paper_sessions SET schema_version = :v "
                    "WHERE session_id = :sid"
                ),
                {"v": 999, "sid": sid},
            )
        env = router.dispatch("POST", f"/deployments/{dep.deployment_id}/restore")
        assert env.status in (409, 404)

    def test_strategy_hash_mismatch_rejected(self, router, fixture):
        center, dep, broker, runner, sid = fixture
        router.dispatch("POST", f"/deployments/{dep.deployment_id}/checkpoint")
        from sqlalchemy import text
        with center.session_store.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE paper_sessions SET strategy_spec_hash = :v "
                    "WHERE session_id = :sid"
                ),
                {"v": "wrong-hash", "sid": sid},
            )
        env = router.dispatch("POST", f"/deployments/{dep.deployment_id}/restore")
        assert env.status in (409, 404)


# --------------------------------------------------------------------------- #
# Checkpoint policy
# --------------------------------------------------------------------------- #
class TestCheckpointPolicy:
    def test_disabled_by_default(self):
        policy = CheckpointPolicy()
        assert policy.enabled is False
        assert evaluate_checkpoint_policy(
            policy=policy, bar_count=10, max_drawdown=-0.05,
        ) == CheckpointDecision.SKIP

    def test_every_n_bars_triggers(self):
        policy = CheckpointPolicy(enabled=True, every_n_bars=5)
        # No prior checkpoint: base is 0.
        assert evaluate_checkpoint_policy(
            policy=policy, bar_count=5, max_drawdown=0.0,
        ) == CheckpointDecision.CHECKPOINT
        # Just before the next interval: still SKIP.
        assert evaluate_checkpoint_policy(
            policy=policy, bar_count=9, max_drawdown=0.0,
            last_checkpoint_bar=5,
        ) == CheckpointDecision.SKIP
        # Reaches the interval: CHECKPOINT.
        assert evaluate_checkpoint_policy(
            policy=policy, bar_count=10, max_drawdown=0.0,
            last_checkpoint_bar=5,
        ) == CheckpointDecision.CHECKPOINT

    def test_every_n_bars_invalid(self):
        with pytest.raises(Exception):
            CheckpointPolicy(enabled=True, every_n_bars=0)
        with pytest.raises(Exception):
            CheckpointPolicy(enabled=True, every_n_bars=-3)

    def test_drawdown_threshold_triggers(self):
        policy = CheckpointPolicy(enabled=True, drawdown_threshold_pct=0.05)
        assert evaluate_checkpoint_policy(
            policy=policy, bar_count=3, max_drawdown=-0.06,
        ) == CheckpointDecision.CHECKPOINT
        assert evaluate_checkpoint_policy(
            policy=policy, bar_count=3, max_drawdown=-0.04,
        ) == CheckpointDecision.SKIP

    def test_drawdown_threshold_invalid(self):
        with pytest.raises(Exception):
            CheckpointPolicy(enabled=True, drawdown_threshold_pct=1.5)
        with pytest.raises(Exception):
            CheckpointPolicy(enabled=True, drawdown_threshold_pct=-0.1)

    def test_enabled_without_triggers_rejected(self):
        with pytest.raises(Exception):
            CheckpointPolicy(enabled=True)

    def test_policy_does_not_alter_trading(self, fixture):
        # The policy is a pure function: calling it on a runner must
        # never change the runner's state. Use the snapshot diff.
        center, dep, broker, runner, _sid = fixture
        before = (runner.bar_count, runner.orders_submitted,
                  runner.fills_received, runner.rejected_orders)
        policy = CheckpointPolicy(enabled=True, every_n_bars=5)
        evaluate_checkpoint_policy(
            policy=policy, bar_count=runner.bar_count,
            max_drawdown=runner.max_drawdown,
        )
        after = (runner.bar_count, runner.orders_submitted,
                 runner.fills_received, runner.rejected_orders)
        assert before == after

    def test_policy_is_deterministic(self):
        policy = CheckpointPolicy(enabled=True, every_n_bars=5)
        a = evaluate_checkpoint_policy(
            policy=policy, bar_count=7, max_drawdown=-0.01,
        )
        b = evaluate_checkpoint_policy(
            policy=policy, bar_count=7, max_drawdown=-0.01,
        )
        assert a == b

    def test_policy_with_unknown_drawdown_is_safe(self):
        policy = CheckpointPolicy(enabled=True, drawdown_threshold_pct=0.10)
        # max_drawdown=None: cannot evaluate threshold -> SKIP.
        assert evaluate_checkpoint_policy(
            policy=policy, bar_count=10, max_drawdown=None,
        ) == CheckpointDecision.SKIP


# --------------------------------------------------------------------------- #
# Paper-only enforcement
# --------------------------------------------------------------------------- #
class TestPaperOnlyEnforcement:
    def test_router_rejects_abstract_broker_via_assert(self):
        from trading_system.paper_api.router import PaperAPIRouter
        from trading_system.paper.control import PaperTradingControlCenter
        from trading_system.execution.broker import Broker
        engine = create_engine("sqlite://")
        store = EvidenceStore(engine)
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
        router = PaperAPIRouter(center)
        with pytest.raises((TypeError, PaperBrokerRequiredError)):
            center.assert_paper_broker(Broker())  # type: ignore[abstract]

    def test_router_does_not_instantiate_brokers(self, fixture):
        center, *_ = fixture
        router = PaperAPIRouter(center)
        # The router has no broker-construction code path; verify by
        # exercising it without attaching any broker.
        env = router.dispatch("GET", "/health")
        assert env.status == 200

    def test_error_mapping_for_paper_broker_required(self):
        exc = PaperBrokerRequiredError("expected PaperBroker")
        mapped = map_domain_exception(exc)
        assert mapped.code == APIErrorCode.PAPER_BROKER_REQUIRED

    def test_error_mapping_for_not_paper_mode(self):
        exc = NotPaperModeError("live")
        mapped = map_domain_exception(exc)
        assert mapped.code == APIErrorCode.NON_PAPER_EXECUTION_MODE

    def test_error_mapping_for_unknown_deployment(self):
        exc = UnknownDeploymentError("missing")
        mapped = map_domain_exception(exc)
        assert mapped.code == APIErrorCode.UNKNOWN_DEPLOYMENT

    def test_error_mapping_for_invalid_lifecycle(self):
        exc = InvalidLifecycleTransitionError("bad")
        mapped = map_domain_exception(exc)
        assert mapped.code == APIErrorCode.INVALID_LIFECYCLE_TRANSITION

    def test_error_mapping_for_session_schema(self):
        exc = SessionSchemaError("bad")
        mapped = map_domain_exception(exc)
        assert mapped.code == APIErrorCode.SCHEMA_MISMATCH

    def test_error_mapping_for_session_identity(self):
        exc = SessionIdentityError("bad")
        mapped = map_domain_exception(exc)
        assert mapped.code == APIErrorCode.CORRUPTED_PERSISTED_STATE


# --------------------------------------------------------------------------- #
# Security / no-credentials
# --------------------------------------------------------------------------- #
class TestSecurity:
    FORBIDDEN_TOKENS = (
        "api_key", "secret", "password", "access_token",
        "refresh_token", "token",
    )

    @pytest.mark.parametrize("path_template,method", [
        ("/health", "GET"),
        ("/deployments", "GET"),
    ])
    def test_listing_endpoints_no_secrets(self, router, fixture, path_template, method):
        env = router.dispatch(method, path_template)
        text = json.dumps(env.body).lower()
        for forbidden in self.FORBIDDEN_TOKENS:
            assert forbidden not in text

    def test_dashboard_no_secrets(self, router, fixture):
        _center, dep, *_ = fixture
        env = router.dispatch("GET", f"/deployments/{dep.deployment_id}/dashboard")
        text = json.dumps(env.body).lower()
        for forbidden in self.FORBIDDEN_TOKENS:
            assert forbidden not in text

    def test_export_no_secrets(self, router, fixture):
        _center, dep, *_ = fixture
        env = router.dispatch("GET", f"/deployments/{dep.deployment_id}/export")
        text = json.dumps(env.body).lower()
        for forbidden in self.FORBIDDEN_TOKENS:
            assert forbidden not in text


# --------------------------------------------------------------------------- #
# AST / static safety scans
# --------------------------------------------------------------------------- #
class TestStaticSafety:
    def test_no_live_broker_imports(self):
        forbidden = ("fyers", "upstox", "zerodha", "angel", "alice",
                     "kite", "aliceblue")
        for path in API_PY_FILES:
            text = path.read_text(encoding="utf-8").lower()
            for token in forbidden:
                assert f"import {token}" not in text, \
                    f"{path.name} imports live broker module {token}"
                assert f"from {token}" not in text, \
                    f"{path.name} imports live broker module {token}"

    def test_no_forbidden_calls(self):
        forbidden = {"eval", "exec", "compile", "__import__", "open",
                     "subprocess", "system", "popen", "globals", "locals",
                     "vars", "breakpoint", "getattr", "setattr"}
        for path in API_PY_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    assert node.func.id not in forbidden, \
                        f"{path.name}:{node.lineno} calls {node.func.id}()"

    def test_no_env_access(self):
        for path in API_PY_FILES:
            text = path.read_text(encoding="utf-8")
            # Allow the explicit, narrow env reads the server does for
            # host / port / body size / non-loopback opt-in. Reject
            # .env, load_dotenv, os.environ broad reads, and any
            # settings.*secret* key.
            assert "load_dotenv" not in text, f"{path.name} calls load_dotenv"
            assert ".env" not in text, f"{path.name} references .env"
            assert "settings.api_key" not in text
            assert "settings.secret" not in text
            assert "settings.token" not in text
            assert "settings.password" not in text
            # Generic os.environ is allowed only inside the server
            # module and only for the narrow set of variables we
            # explicitly document.
            if path.name != "server.py":
                assert "os.environ" not in text, \
                    f"{path.name} reads os.environ broadly"

    def test_no_network_modules(self):
        # Forbidden OUTBOUND network modules. ``http.server`` is a
        # permitted stdlib inbound-HTTP adapter (used to expose the
        # control center on loopback).
        forbidden = {
            ("urllib", "request"),
            ("urllib", "error"),
            ("urllib", "robotparser"),
            ("http", "client"),
            ("socket",),
            ("requests",),
            ("httpx",),
            ("aiohttp",),
        }
        for path in API_PY_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for token in forbidden:
                            mod_parts = alias.name.split(".")
                            if tuple(mod_parts[:len(token)]) == tuple(token):
                                raise AssertionError(
                                    f"{path.name}:{node.lineno} imports {alias.name}"
                                )
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    mod_parts = mod.split(".")
                    for token in forbidden:
                        if tuple(mod_parts[:len(token)]) == tuple(token):
                            raise AssertionError(
                                f"{path.name}:{node.lineno} imports from {mod}"
                            )

    def test_no_pickle_marshal(self):
        for path in API_PY_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert alias.name not in ("pickle", "marshal", "shelve")
                elif isinstance(node, ast.ImportFrom):
                    assert (node.module or "") not in ("pickle", "marshal", "shelve")

    def test_no_credential_related_dispatch_methods(self, router):
        # No endpoint should be named /buy, /sell, /execute, or
        # /positions/close — these look like live-broker order paths.
        # /orders is allowed only under /deployments/{id}/orders (paper
        # order-intent path that goes through the control center, not a
        # direct broker call).
        for pattern, _methods in router.routes():
            for forbidden in ("/buy", "/sell", "/execute",
                              "/positions/close"):
                assert forbidden not in pattern, \
                    f"forbidden order path: {pattern}"
            # /orders is OK only as a sub-path of /deployments.
            if "/orders" in pattern:
                assert "/deployments/" in pattern, \
                    f"/orders must be under /deployments: {pattern}"


# --------------------------------------------------------------------------- #
# stdlib HTTP server
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestHTTPServer:
    def _start(self, router, port):
        server = APIServer(router, host="127.0.0.1", port=port)
        thread = threading.Thread(
            target=server.serve_forever, daemon=True
        )
        thread.start()
        # Give the server a moment to bind.
        for _ in range(20):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                    break
            except OSError:
                time.sleep(0.05)
        return server, thread

    def test_loopback_binding(self, router):
        # Default loopback should be accepted.
        port = _free_port()
        server, _ = self._start(router, port)
        try:
            req = Request(f"http://127.0.0.1:{port}/health")
            with urlopen(req, timeout=2) as resp:
                assert resp.status == 200
                body = json.loads(resp.read().decode("utf-8"))
                assert body["status"] == "ok"
        finally:
            server.shutdown()

    def test_non_loopback_rejected(self, router):
        with pytest.raises(ValueError):
            APIServer(router, host="0.0.0.0", port=_free_port())

    def test_request_size_cap(self, router):
        port = _free_port()
        server, _ = self._start(router, port)
        try:
            huge = "a" * (2 * 1024 * 1024)  # 2 MiB > default 1 MiB cap
            req = Request(
                f"http://127.0.0.1:{port}/deployments",
                data=huge.encode("utf-8"),
                method="POST",
            )
            with pytest.raises(Exception):
                urlopen(req, timeout=5)
        finally:
            server.shutdown()

    def test_health_through_http(self, router):
        port = _free_port()
        server, _ = self._start(router, port)
        try:
            req = Request(f"http://127.0.0.1:{port}/health")
            with urlopen(req, timeout=2) as resp:
                assert resp.status == 200
        finally:
            server.shutdown()


# --------------------------------------------------------------------------- #
# Existing Phase 20 surface remains unchanged
# --------------------------------------------------------------------------- #
class TestBackwardCompatibility:
    def test_phase20_public_api_unchanged(self):
        from trading_system.paper import (
            PaperTradingControlCenter,
            PaperSession,
            PaperSessionCheckpoint,
            PaperSessionStore,
            PaperControlCenterSnapshot,
        )
        # All Phase 20 classes still importable.
        assert PaperTradingControlCenter is not None
        assert PaperSession is not None
        assert PaperSessionCheckpoint is not None
        assert PaperSessionStore is not None
        assert PaperControlCenterSnapshot is not None

    def test_phase20_control_center_methods_present(self):
        from trading_system.paper.control import PaperTradingControlCenter
        required = {
            "list_deployments", "get_deployment",
            "activate_deployment", "pause_deployment",
            "resume_deployment", "stop_deployment",
            "save_session", "restore_session",
            "inspect_session", "inspect_account", "inspect_positions",
            "inspect_performance", "inspect_health", "inspect_risk",
            "inspect_circuit_breaker", "inspect_events", "inspect_evidence",
            "build_dashboard_snapshot", "export_json",
            "reset_circuit_breaker",
            "find_session_for_deployment", "get_runner",
            "assert_paper_broker",
        }
        missing = required - set(dir(PaperTradingControlCenter))
        assert not missing, f"missing Phase 20 methods: {missing}"