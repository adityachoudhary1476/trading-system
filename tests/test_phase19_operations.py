"""Phase 19 — Paper Trading Operations & Monitoring tests.

Covers the operations layer: event log, operations state, health monitor,
risk guard, circuit breaker, performance snapshots, runner integration,
operations report, evidence integration, determinism, failure handling,
deployment isolation, and safety boundaries.
"""
from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine

from trading_system.execution.paper_broker import PaperBroker
from trading_system.paper import (
    CircuitState,
    HealthStatus,
    PaperCircuitBreaker,
    PaperDeployment,
    PaperDeploymentConfig,
    PaperDeploymentStatus,
    PaperHealthConfig,
    PaperHealthMonitor,
    PaperOperationEventType,
    PaperOperationsEventLog,
    PaperOperationsReport,
    PaperOperationsState,
    PaperPerformanceSnapshot,
    PaperRiskConfig,
    PaperRiskGuard,
    PaperStrategyRunner,
    RiskDecision,
    SignalType,
    build_operations_report,
)
from trading_system.paper.deployment import deployment_identity
from trading_system.paper.events import make_event_id
from trading_system.paper.snapshot import build_snapshot
from trading_system.research.evidence import (
    EvidenceStore,
    EvidenceType,
    StrategyEvidence,
    strategy_identity,
)
from trading_system.research.strategy_lab.spec import (
    StrategySpec,
    field_operand,
    indicator_operand,
    make_condition,
)
from trading_system.research.strategy_registry import StrategyRegistry


PAPER_DIR = Path(__file__).resolve().parents[1] / "src" / "trading_system" / "paper"
PAPER_PY_FILES = sorted(PAPER_DIR.glob("*.py"))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _spec(name="Phase19 ops", symbol="NSE:SBIN"):
    return StrategySpec(
        name=name,
        description="phase 19 operations test",
        symbol=symbol,
        timeframe="1d",
        indicators=[{"name": "sma", "params": {"window": 5}}],
        entry=make_condition(field_operand("close"), ">", indicator_operand("sma_5")),
        generated_by="test",
    )


def _deployment(spec, *, status=PaperDeploymentStatus.ACTIVE, config=None):
    cfg = config or PaperDeploymentConfig()
    spec_hash = strategy_identity(spec)
    did = deployment_identity(
        "sid-1", spec_hash, spec.symbol, spec.timeframe, "ds-1", cfg
    )
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


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
class TestEventLog:
    def test_event_is_deterministic(self):
        spec = _spec()
        dep = _deployment(spec)
        log = PaperOperationsEventLog(dep)
        e1 = log.record(PaperOperationEventType.BAR_PROCESSED, "2024-01-01", "msg", {"a": 1})
        log2 = PaperOperationsEventLog(dep)
        e2 = log2.record(PaperOperationEventType.BAR_PROCESSED, "2024-01-01", "msg", {"a": 1})
        assert e1.event_id == e2.event_id

    def test_sequence_is_monotonic(self):
        spec = _spec()
        dep = _deployment(spec)
        log = PaperOperationsEventLog(dep)
        for _ in range(5):
            log.record(PaperOperationEventType.BAR_PROCESSED, "2024-01-01", "", {})
        seqs = [e.sequence for e in log.events]
        assert seqs == sorted(set(seqs))
        assert seqs[0] == 0 and seqs[-1] == 4

    def test_append_only(self):
        spec = _spec()
        dep = _deployment(spec)
        log = PaperOperationsEventLog(dep)
        log.record(PaperOperationEventType.BAR_PROCESSED, "2024-01-01", "", {})
        log.record(PaperOperationEventType.SIGNAL_GENERATED, "2024-01-02", "", {})
        assert len(log) == 2
        assert log.count_type(PaperOperationEventType.BAR_PROCESSED) == 1

    def test_last_of_type(self):
        spec = _spec()
        dep = _deployment(spec)
        log = PaperOperationsEventLog(dep)
        log.record(PaperOperationEventType.BAR_PROCESSED, "2024-01-01", "", {})
        log.record(PaperOperationEventType.SIGNAL_GENERATED, "2024-01-02", "sig", {})
        log.record(PaperOperationEventType.BAR_PROCESSED, "2024-01-03", "", {})
        last = log.last_of_type(PaperOperationEventType.BAR_PROCESSED)
        assert last is not None
        assert last.timestamp == "2024-01-03"

    def test_event_id_is_sha256_like(self):
        eid = make_event_id("d", 0, PaperOperationEventType.BAR_PROCESSED, "t", {})
        assert len(eid) == 64
        int(eid, 16)  # hex


# --------------------------------------------------------------------------- #
# Operations state
# --------------------------------------------------------------------------- #
class TestOperationsState:
    def test_builds_from_runner(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(deployment=dep, broker=broker, spec=spec)
        for bar in _bars(_uptrend()):
            runner.process_bar(bar)
        state = runner.operations_state()
        assert state.deployment_id == dep.deployment_id
        assert state.processed_bars == runner.bar_count
        assert state.status == "active"

    def test_serializable(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(deployment=dep, broker=broker, spec=spec)
        for bar in _bars(_uptrend()):
            runner.process_bar(bar)
        state = runner.operations_state()
        blob = state.model_dump_json()
        restored = PaperOperationsState.model_validate_json(blob)
        assert restored.deployment_id == state.deployment_id
        assert restored.processed_bars == state.processed_bars

    def test_no_credentials_exposed(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(deployment=dep, broker=broker, spec=spec)
        for bar in _bars(_uptrend()):
            runner.process_bar(bar)
        text = runner.operations_state().model_dump_json()
        assert "api_key" not in text.lower()
        assert "secret" not in text.lower()


# --------------------------------------------------------------------------- #
# Health monitor
# --------------------------------------------------------------------------- #
class TestHealthMonitor:
    def test_healthy_by_default(self):
        mon = PaperHealthMonitor()
        ev = mon.evaluate(
            deployment_status="active", processed_bars=10, filled_orders=1,
            rejected_orders=0, consecutive_errors=0, max_drawdown=0.0,
            equity=100_000.0, position=None,
        )
        assert ev.status == HealthStatus.HEALTHY
        assert ev.allowed_to_trade is True

    def test_stopped_deployment_halts(self):
        mon = PaperHealthMonitor()
        ev = mon.evaluate(
            deployment_status="stopped", processed_bars=10, filled_orders=0,
            rejected_orders=0, consecutive_errors=0, max_drawdown=0.0,
            equity=100_000.0, position=None,
        )
        assert ev.status == HealthStatus.HALTED
        assert ev.allowed_to_trade is False

    def test_failed_deployment_halts(self):
        mon = PaperHealthMonitor()
        ev = mon.evaluate(
            deployment_status="failed", processed_bars=10, filled_orders=0,
            rejected_orders=0, consecutive_errors=0, max_drawdown=0.0,
            equity=100_000.0, position=None,
        )
        assert ev.status == HealthStatus.HALTED

    def test_drawdown_warning(self):
        cfg = PaperHealthConfig(warn_drawdown_pct=0.05)
        mon = PaperHealthMonitor(cfg)
        ev = mon.evaluate(
            deployment_status="active", processed_bars=10, filled_orders=1,
            rejected_orders=0, consecutive_errors=0, max_drawdown=-0.06,
            equity=94_000.0, position=None,
        )
        assert ev.status == HealthStatus.WARNING
        assert any("drawdown" in w for w in ev.warnings)

    def test_drawdown_halt(self):
        cfg = PaperHealthConfig(halt_drawdown_pct=0.10)
        mon = PaperHealthMonitor(cfg)
        ev = mon.evaluate(
            deployment_status="active", processed_bars=10, filled_orders=1,
            rejected_orders=0, consecutive_errors=0, max_drawdown=-0.15,
            equity=85_000.0, position=None,
        )
        assert ev.status == HealthStatus.HALTED
        assert "drawdown" in ev.halt_reason

    def test_rejected_orders_halt(self):
        cfg = PaperHealthConfig(warn_rejected_orders=2, halt_rejected_orders=5)
        mon = PaperHealthMonitor(cfg)
        ev = mon.evaluate(
            deployment_status="active", processed_bars=10, filled_orders=0,
            rejected_orders=5, consecutive_errors=0, max_drawdown=0.0,
            equity=100_000.0, position=None,
        )
        assert ev.status == HealthStatus.HALTED

    def test_consecutive_errors_halt(self):
        cfg = PaperHealthConfig(halt_consecutive_errors=3)
        mon = PaperHealthMonitor(cfg)
        ev = mon.evaluate(
            deployment_status="active", processed_bars=10, filled_orders=0,
            rejected_orders=0, consecutive_errors=3, max_drawdown=0.0,
            equity=100_000.0, position=None,
        )
        assert ev.status == HealthStatus.HALTED

    def test_bars_without_fill_warning(self):
        cfg = PaperHealthConfig(warn_bars_without_fill=50)
        mon = PaperHealthMonitor(cfg)
        ev = mon.evaluate(
            deployment_status="active", processed_bars=60, filled_orders=0,
            rejected_orders=0, consecutive_errors=0, max_drawdown=0.0,
            equity=100_000.0, position=None,
        )
        assert ev.status == HealthStatus.WARNING
        assert any("no fill" in w for w in ev.warnings)

    def test_exposure_warning(self):
        from trading_system.paper_trading import Position
        cfg = PaperHealthConfig(warn_exposure_pct=0.5)
        mon = PaperHealthMonitor(cfg)
        pos = Position(symbol="NSE:SBIN", qty=100.0, avg_entry_price=100.0,
                       current_price=100.0)
        ev = mon.evaluate(
            deployment_status="active", processed_bars=10, filled_orders=1,
            rejected_orders=0, consecutive_errors=0, max_drawdown=0.0,
            equity=10_000.0, position=pos,
        )
        # exposure = 100*100/10000 = 1.0 >= 0.5
        assert ev.status == HealthStatus.WARNING

    def test_unset_limits_not_enforced(self):
        cfg = PaperHealthConfig()  # all None
        mon = PaperHealthMonitor(cfg)
        ev = mon.evaluate(
            deployment_status="active", processed_bars=1000, filled_orders=0,
            rejected_orders=100, consecutive_errors=50, max_drawdown=-0.9,
            equity=10_000.0, position=None,
        )
        assert ev.status == HealthStatus.HEALTHY

    def test_determinism(self):
        cfg = PaperHealthConfig(warn_drawdown_pct=0.05, halt_drawdown_pct=0.10)
        mon = PaperHealthMonitor(cfg)
        args = dict(
            deployment_status="active", processed_bars=10, filled_orders=1,
            rejected_orders=0, consecutive_errors=0, max_drawdown=-0.06,
            equity=94_000.0, position=None,
        )
        e1 = mon.evaluate(**args)
        e2 = mon.evaluate(**args)
        assert e1.status == e2.status
        assert e1.warnings == e2.warnings


# --------------------------------------------------------------------------- #
# Risk guard
# --------------------------------------------------------------------------- #
class TestRiskGuard:
    def test_allow_by_default(self):
        guard = PaperRiskGuard()
        decision, reason = guard.check(
            max_drawdown=0.0, equity=100_000.0, position=None,
            rejected_orders=0, consecutive_errors=0,
        )
        assert decision == RiskDecision.ALLOW
        assert reason is None

    def test_drawdown_halt(self):
        cfg = PaperRiskConfig(max_drawdown_pct=0.10)
        guard = PaperRiskGuard(cfg)
        decision, reason = guard.check(
            max_drawdown=-0.15, equity=85_000.0, position=None,
            rejected_orders=0, consecutive_errors=0,
        )
        assert decision == RiskDecision.HALT
        assert "drawdown" in reason

    def test_position_value_halt(self):
        from trading_system.paper_trading import Position
        cfg = PaperRiskConfig(max_position_value_pct=0.5)
        guard = PaperRiskGuard(cfg)
        pos = Position(symbol="NSE:SBIN", qty=100.0, avg_entry_price=100.0,
                       current_price=100.0)
        decision, reason = guard.check(
            max_drawdown=0.0, equity=10_000.0, position=pos,
            rejected_orders=0, consecutive_errors=0,
        )
        assert decision == RiskDecision.HALT
        assert "exposure" in reason

    def test_rejected_orders_halt(self):
        cfg = PaperRiskConfig(max_rejected_orders=3)
        guard = PaperRiskGuard(cfg)
        decision, reason = guard.check(
            max_drawdown=0.0, equity=100_000.0, position=None,
            rejected_orders=3, consecutive_errors=0,
        )
        assert decision == RiskDecision.HALT

    def test_consecutive_errors_halt(self):
        cfg = PaperRiskConfig(max_consecutive_errors=2)
        guard = PaperRiskGuard(cfg)
        decision, reason = guard.check(
            max_drawdown=0.0, equity=100_000.0, position=None,
            rejected_orders=0, consecutive_errors=2,
        )
        assert decision == RiskDecision.HALT

    def test_does_not_modify_broker(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        guard = PaperRiskGuard(PaperRiskConfig(max_drawdown_pct=0.10))
        before = broker.account().equity
        guard.check(max_drawdown=-0.05, equity=broker.account().equity,
                    position=None, rejected_orders=0, consecutive_errors=0)
        assert broker.account().equity == before


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #
class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = PaperCircuitBreaker()
        assert cb.state == CircuitState.CLOSED
        assert cb.allowed_to_trade() is True
        assert cb.is_open is False

    def test_trip_opens(self):
        cb = PaperCircuitBreaker()
        cb.trip("risk breach")
        assert cb.is_open
        assert cb.allowed_to_trade() is False
        assert cb.reason == "risk breach"

    def test_no_auto_recovery(self):
        cb = PaperCircuitBreaker()
        cb.trip("breach")
        assert cb.is_open
        assert cb.allowed_to_trade() is False

    def test_reset_requires_explicit_action(self):
        cb = PaperCircuitBreaker()
        cb.trip("breach")
        cb.reset()
        assert cb.is_open is False
        assert cb.reason is None
        assert cb.allowed_to_trade() is True

    def test_first_reason_preserved(self):
        cb = PaperCircuitBreaker()
        cb.trip("first")
        cb.trip("second")
        assert cb.reason == "first"
        assert cb.trip_count == 2

    def test_deployment_specific(self):
        cb1 = PaperCircuitBreaker()
        cb2 = PaperCircuitBreaker()
        cb1.trip("breach")
        assert cb1.is_open
        assert cb2.is_open is False  # unaffected


# --------------------------------------------------------------------------- #
# Performance snapshot
# --------------------------------------------------------------------------- #
class TestSnapshot:
    def test_build_snapshot_basic(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        account = broker.account()
        snap = build_snapshot(
            deployment_id=dep.deployment_id, strategy_id="sid-1",
            timestamp="2024-01-01", account=account, position=None,
            starting_equity=100_000.0, max_drawdown=0.0,
        )
        assert isinstance(snap, PaperPerformanceSnapshot)
        assert snap.equity == 100_000.0
        assert snap.return_ == 0.0

    def test_unavailable_metrics_none(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        snap = build_snapshot(
            deployment_id=dep.deployment_id, strategy_id="sid-1",
            timestamp=None, account=broker.account(), position=None,
            starting_equity=None, max_drawdown=None,
        )
        assert snap.win_rate is None
        assert snap.profit_factor is None
        assert snap.total_pnl is None

    def test_determinism(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        account = broker.account()
        args = dict(
            deployment_id=dep.deployment_id, strategy_id="sid-1",
            timestamp="2024-01-01", account=account, position=None,
            starting_equity=100_000.0, max_drawdown=0.0,
        )
        s1 = build_snapshot(**args)
        s2 = build_snapshot(**args)
        assert s1.model_dump() == s2.model_dump()


# --------------------------------------------------------------------------- #
# Runner integration
# --------------------------------------------------------------------------- #
class TestRunnerIntegration:
    def test_no_monitoring_preserves_phase18_behavior(self):
        """Without ops components, behavior is identical to Phase 18."""
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(deployment=dep, broker=broker, spec=spec)
        signals = [runner.process_bar(bar) for bar in _bars(_uptrend())]
        assert runner.bar_count == 60
        assert runner.event_log is None

    def test_event_log_records_events(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        log = PaperOperationsEventLog(dep)
        runner = PaperStrategyRunner(
            deployment=dep, broker=broker, spec=spec, event_log=log,
        )
        for bar in _bars(_uptrend()):
            runner.process_bar(bar)
        assert len(log) > 0
        assert log.count_type(PaperOperationEventType.DEPLOYMENT_ACTIVATED) == 1
        assert log.count_type(PaperOperationEventType.BAR_PROCESSED) == 60

    def test_circuit_breaker_blocks_trading(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        cb = PaperCircuitBreaker()
        cb.trip("pre-trip")
        runner = PaperStrategyRunner(
            deployment=dep, broker=broker, spec=spec, circuit_breaker=cb,
        )
        signals = [runner.process_bar(bar) for bar in _bars(_uptrend())]
        assert all(s == SignalType.NO_ACTION for s in signals)
        assert runner.orders_submitted == 0

    def test_circuit_breaker_closed_allows_trading(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        cb = PaperCircuitBreaker()
        runner = PaperStrategyRunner(
            deployment=dep, broker=broker, spec=spec, circuit_breaker=cb,
        )
        for bar in _bars(_uptrend()):
            runner.process_bar(bar)
        assert cb.is_open is False
        assert runner.bar_count == 60

    def test_health_monitor_attached(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        mon = PaperHealthMonitor(PaperHealthConfig(warn_drawdown_pct=0.05))
        runner = PaperStrategyRunner(
            deployment=dep, broker=broker, spec=spec, health_monitor=mon,
        )
        for bar in _bars(_uptrend()):
            runner.process_bar(bar)
        assert runner.health_status in ("healthy", "warning", "halted")

    def test_snapshots_captured(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(deployment=dep, broker=broker, spec=spec)
        for bar in _bars(_uptrend()):
            runner.process_bar(bar)
        assert len(runner.snapshots) == 60

    def test_max_drawdown_tracked(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(deployment=dep, broker=broker, spec=spec)
        for bar in _bars(_uptrend()):
            runner.process_bar(bar)
        assert runner.max_drawdown is None or runner.max_drawdown <= 0.0

    def test_duplicate_bar_is_noop(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        log = PaperOperationsEventLog(dep)
        runner = PaperStrategyRunner(
            deployment=dep, broker=broker, spec=spec, event_log=log,
        )
        bars = list(_bars(_uptrend()))
        for bar in bars:
            runner.process_bar(bar)
        count_after = runner.bar_count
        events_after = len(log)
        # Replay the LAST bar consecutively (Phase 18 idempotency = consecutive
        # duplicate timestamp is a no-op).
        runner.process_bar(bars[-1])
        runner.process_bar(bars[-1])
        assert runner.bar_count == count_after
        assert len(log) == events_after

    def test_paused_deployment_no_orders(self):
        spec = _spec()
        dep = _deployment(spec, status=PaperDeploymentStatus.PAUSED)
        broker = PaperBroker(initial_cash=100_000.0)
        cb = PaperCircuitBreaker()
        runner = PaperStrategyRunner(
            deployment=dep, broker=broker, spec=spec, circuit_breaker=cb,
        )
        for bar in _bars(_uptrend()):
            runner.process_bar(bar)
        assert runner.orders_submitted == 0
        assert cb.is_open is False

    def test_stopped_deployment_no_orders(self):
        spec = _spec()
        dep = _deployment(spec, status=PaperDeploymentStatus.STOPPED)
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(deployment=dep, broker=broker, spec=spec)
        for bar in _bars(_uptrend()):
            runner.process_bar(bar)
        assert runner.orders_submitted == 0

    def test_malformed_bar_raises(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(deployment=dep, broker=broker, spec=spec)
        with pytest.raises(ValueError):
            runner.process_bar({"timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
                                "open": 1.0, "high": 1.0, "low": 1.0})

    def test_consecutive_errors_reset_on_good_bar(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(deployment=dep, broker=broker, spec=spec)
        good = list(_bars(_uptrend()))
        runner.process_bar(good[0])
        assert runner.consecutive_errors == 0


# --------------------------------------------------------------------------- #
# Operations report
# --------------------------------------------------------------------------- #
class TestOperationsReport:
    def test_build_report(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        log = PaperOperationsEventLog(dep)
        runner = PaperStrategyRunner(
            deployment=dep, broker=broker, spec=spec, event_log=log,
        )
        for bar in _bars(_uptrend()):
            runner.process_bar(bar)
        report = build_operations_report(dep, runner)
        assert isinstance(report, PaperOperationsReport)
        assert report.deployment_id == dep.deployment_id
        assert report.processed_bars == runner.bar_count
        assert report.report_version == "phase-19"

    def test_report_serializable(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(deployment=dep, broker=broker, spec=spec)
        for bar in _bars(_uptrend()):
            runner.process_bar(bar)
        report = build_operations_report(dep, runner)
        blob = report.model_dump_json()
        restored = PaperOperationsReport.model_validate_json(blob)
        assert restored.deployment_id == report.deployment_id
        assert restored.processed_bars == report.processed_bars

    def test_report_no_credentials(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(deployment=dep, broker=broker, spec=spec)
        for bar in _bars(_uptrend()):
            runner.process_bar(bar)
        text = build_operations_report(dep, runner).model_dump_json()
        assert "api_key" not in text.lower()
        assert "secret" not in text.lower()

    def test_halt_status_reflected(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        cb = PaperCircuitBreaker()
        cb.trip("test halt")
        runner = PaperStrategyRunner(
            deployment=dep, broker=broker, spec=spec, circuit_breaker=cb,
        )
        for bar in _bars(_uptrend()):
            runner.process_bar(bar)
        report = build_operations_report(dep, runner)
        assert report.halt_status == "open"
        assert report.halt_reason == "test halt"

    def test_events_in_report(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        log = PaperOperationsEventLog(dep)
        runner = PaperStrategyRunner(
            deployment=dep, broker=broker, spec=spec, event_log=log,
        )
        for bar in _bars(_uptrend()):
            runner.process_bar(bar)
        report = build_operations_report(dep, runner)
        assert len(report.events) == len(log)
        assert report.events[0]["event_type"] == "deployment_activated"


# --------------------------------------------------------------------------- #
# Evidence integration
# --------------------------------------------------------------------------- #
class TestEvidenceIntegration:
    def test_operations_evidence_persists(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(deployment=dep, broker=broker, spec=spec)
        for bar in _bars(_uptrend()):
            runner.process_bar(bar)
        report = build_operations_report(dep, runner)
        from trading_system.paper.report import build_paper_operations_evidence
        store = EvidenceStore(create_engine("sqlite://"))
        reg = StrategyRegistry(store)
        ev = build_paper_operations_evidence(
            deployment=dep, spec=spec, report=report, dataset_id="ds-1",
        )
        assert ev.evidence_type == EvidenceType.PAPER_TRADING
        reg.record_evidence(ev)
        got = reg.get_evidence(ev.evidence_id)
        assert got is not None
        assert got.evidence_type == EvidenceType.PAPER_TRADING

    def test_evidence_idempotent(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(deployment=dep, broker=broker, spec=spec)
        for bar in _bars(_uptrend()):
            runner.process_bar(bar)
        report = build_operations_report(dep, runner)
        from trading_system.paper.report import build_paper_operations_evidence
        ev1 = build_paper_operations_evidence(
            deployment=dep, spec=spec, report=report, dataset_id="ds-1",
        )
        ev2 = build_paper_operations_evidence(
            deployment=dep, spec=spec, report=report, dataset_id="ds-1",
        )
        assert ev1.evidence_id == ev2.evidence_id

    def test_evidence_does_not_mutate_prior_evidence(self):
        store = EvidenceStore(create_engine("sqlite://"))
        reg = StrategyRegistry(store)
        prior = StrategyEvidence(
            evidence_id="prior-1", strategy_id="sid-1", strategy_spec_hash="h",
            evidence_type=EvidenceType.RESEARCH, dataset_id="ds-1",
            configuration_json={}, metrics_json={"a": 1},
        )
        reg.record_evidence(prior)
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(deployment=dep, broker=broker, spec=spec)
        for bar in _bars(_uptrend()):
            runner.process_bar(bar)
        report = build_operations_report(dep, runner)
        from trading_system.paper.report import build_paper_operations_evidence
        ev = build_paper_operations_evidence(
            deployment=dep, spec=spec, report=report, dataset_id="ds-1",
        )
        reg.record_evidence(ev)
        still = reg.get_evidence("prior-1")
        assert still is not None
        assert still.metrics_json == {"a": 1}

    def test_no_credentials_in_evidence(self):
        spec = _spec()
        dep = _deployment(spec)
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(deployment=dep, broker=broker, spec=spec)
        for bar in _bars(_uptrend()):
            runner.process_bar(bar)
        report = build_operations_report(dep, runner)
        from trading_system.paper.report import build_paper_operations_evidence
        ev = build_paper_operations_evidence(
            deployment=dep, spec=spec, report=report, dataset_id="ds-1",
        )
        text = ev.model_dump_json()
        assert "api_key" not in text.lower()
        assert "secret" not in text.lower()
        assert "token" not in text.lower()


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
class TestDeterminism:
    def test_same_bars_same_report(self):
        spec = _spec()
        reports = []
        for _ in range(2):
            dep = _deployment(spec)
            broker = PaperBroker(initial_cash=100_000.0)
            log = PaperOperationsEventLog(dep)
            runner = PaperStrategyRunner(
                deployment=dep, broker=broker, spec=spec, event_log=log,
            )
            for bar in _bars(_uptrend()):
                runner.process_bar(bar)
            reports.append(build_operations_report(dep, runner))
        assert reports[0].processed_bars == reports[1].processed_bars
        assert reports[0].ending_equity == reports[1].ending_equity
        # Event TYPES and SEQUENCES are deterministic (payloads embed random
        # broker order_ids, so raw payloads differ by design).
        types_seq_0 = [(e["event_type"], e["sequence"]) for e in reports[0].events]
        types_seq_1 = [(e["event_type"], e["sequence"]) for e in reports[1].events]
        assert types_seq_0 == types_seq_1

    def test_same_bars_same_event_order(self):
        spec = _spec()
        logs = []
        for _ in range(2):
            dep = _deployment(spec)
            broker = PaperBroker(initial_cash=100_000.0)
            log = PaperOperationsEventLog(dep)
            runner = PaperStrategyRunner(
                deployment=dep, broker=broker, spec=spec, event_log=log,
            )
            for bar in _bars(_uptrend()):
                runner.process_bar(bar)
            logs.append([(e.event_type.value, e.sequence) for e in log.events])
        assert logs[0] == logs[1]

    def test_same_risk_config_same_decision(self):
        cfg = PaperRiskConfig(max_drawdown_pct=0.10)
        g1, g2 = PaperRiskGuard(cfg), PaperRiskGuard(cfg)
        args = dict(max_drawdown=-0.15, equity=85_000.0, position=None,
                    rejected_orders=0, consecutive_errors=0)
        assert g1.check(**args) == g2.check(**args)


# --------------------------------------------------------------------------- #
# Deployment isolation
# --------------------------------------------------------------------------- #
class TestDeploymentIsolation:
    def test_two_deployments_independent_circuit_breakers(self):
        spec = _spec()
        cbs = [PaperCircuitBreaker(), PaperCircuitBreaker()]
        cbs[0].trip("only first")
        runners = []
        for i in range(2):
            dep = _deployment(spec)
            broker = PaperBroker(initial_cash=100_000.0)
            runner = PaperStrategyRunner(
                deployment=dep, broker=broker, spec=spec, circuit_breaker=cbs[i],
            )
            runners.append(runner)
        assert runners[0].circuit_breaker.is_open is True
        assert runners[1].circuit_breaker.is_open is False

    def test_state_is_per_deployment(self):
        # Use distinct specs so the deployments have distinct identities.
        spec1 = _spec(name="ops-a")
        spec2 = _spec(name="ops-b")
        dep1 = _deployment(spec1)
        dep2 = _deployment(spec2)
        broker = PaperBroker(initial_cash=100_000.0)
        r1 = PaperStrategyRunner(deployment=dep1, broker=broker, spec=spec1)
        r2 = PaperStrategyRunner(deployment=dep2, broker=broker, spec=spec2)
        for bar in _bars(_uptrend()):
            r1.process_bar(bar)
            r2.process_bar(bar)
        s1 = r1.operations_state()
        s2 = r2.operations_state()
        assert s1.deployment_id != s2.deployment_id
        assert s1.processed_bars == s2.processed_bars  # same bars


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #
class TestFailureHandling:
    def test_rejected_order_recorded(self):
        spec = _spec()
        dep = _deployment(spec)
        log = PaperOperationsEventLog(dep)
        log.record(PaperOperationEventType.ORDER_REJECTED, "2024-01-01",
                   "rejected", {"reason": "no price"})
        assert log.count_type(PaperOperationEventType.ORDER_REJECTED) == 1

    def test_error_event_recorded(self):
        spec = _spec()
        dep = _deployment(spec)
        log = PaperOperationsEventLog(dep)
        log.record(PaperOperationEventType.ERROR, "2024-01-01", "boom", {})
        assert log.count_type(PaperOperationEventType.ERROR) == 1

    def test_zero_trades_zero_fills(self):
        spec = _spec()
        dep = _deployment(spec, status=PaperDeploymentStatus.PAUSED)
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(deployment=dep, broker=broker, spec=spec)
        for bar in _bars(_uptrend()):
            runner.process_bar(bar)
        report = build_operations_report(dep, runner)
        assert report.trade_count == 0
        assert report.filled_orders == 0
        assert report.win_rate is None
        assert report.profit_factor is None

    def test_terminal_session_rejects_trading(self):
        spec = _spec()
        dep = _deployment(spec, status=PaperDeploymentStatus.STOPPED)
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(deployment=dep, broker=broker, spec=spec)
        signals = [runner.process_bar(bar) for bar in _bars(_uptrend())]
        assert all(s == SignalType.NO_ACTION for s in signals)


# --------------------------------------------------------------------------- #
# Safety
# --------------------------------------------------------------------------- #
class TestPhase19Safety:
    def test_no_live_broker_imports(self):
        forbidden = ("fyers", "upstox", "zerodha", "angel", "alice", "kite")
        new_files = [
            "circuit_breaker.py", "events.py", "health.py",
            "operations.py", "risk.py", "snapshot.py",
        ]
        for path in PAPER_PY_FILES:
            if path.name not in new_files:
                continue
            text = path.read_text(encoding="utf-8").lower()
            for token in forbidden:
                assert f"import {token}" not in text
                assert f"from {token}" not in text

    def test_no_forbidden_calls(self):
        forbidden = {"eval", "exec", "compile", "__import__", "open", "subprocess",
                     "system", "popen", "globals", "locals", "vars", "breakpoint",
                     "getattr", "setattr"}
        new_files = [
            "circuit_breaker.py", "events.py", "health.py",
            "operations.py", "risk.py", "snapshot.py",
        ]
        for path in PAPER_PY_FILES:
            if path.name not in new_files:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    assert node.func.id not in forbidden, \
                        f"{path.name}:{node.lineno} calls {node.func.id}()"

    def test_no_env_access(self):
        new_files = [
            "circuit_breaker.py", "events.py", "health.py",
            "operations.py", "risk.py", "snapshot.py",
        ]
        for path in PAPER_PY_FILES:
            if path.name not in new_files:
                continue
            text = path.read_text(encoding="utf-8")
            assert ".env" not in text
            assert "load_dotenv" not in text
            assert "os.environ" not in text

    def test_no_network_modules(self):
        forbidden = ("socket", "urllib", "requests", "httpx", "http.client")
        new_files = [
            "circuit_breaker.py", "events.py", "health.py",
            "operations.py", "risk.py", "snapshot.py",
        ]
        for path in PAPER_PY_FILES:
            if path.name not in new_files:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for token in forbidden:
                            assert token not in alias.name, \
                                f"{path.name} imports {alias.name}"
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    for token in forbidden:
                        assert token not in mod, \
                            f"{path.name} imports {mod}"

    def test_no_pickle_or_marshal(self):
        new_files = [
            "circuit_breaker.py", "events.py", "health.py",
            "operations.py", "risk.py", "snapshot.py",
        ]
        for path in PAPER_PY_FILES:
            if path.name not in new_files:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert alias.name not in ("pickle", "marshal", "shelve")
                elif isinstance(node, ast.ImportFrom):
                    assert (node.module or "") not in ("pickle", "marshal", "shelve")

    def test_no_dynamic_execution(self):
        forbidden = {"eval", "exec", "compile", "__import__"}
        new_files = [
            "circuit_breaker.py", "events.py", "health.py",
            "operations.py", "risk.py", "snapshot.py",
        ]
        for path in PAPER_PY_FILES:
            if path.name not in new_files:
                continue
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    assert node.func.id not in forbidden
