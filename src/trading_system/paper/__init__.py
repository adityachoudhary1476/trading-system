"""Phase 18/19/20 — Paper Trading Deployment bridge.

Phase 18 exports the public paper-trading API. Phase 19 adds the
operations & monitoring layer: event log, health monitor, risk guard,
circuit breaker, performance snapshots, and the operations report.
Phase 20 adds the Control Center: orchestration, deployment discovery,
lifecycle control, session persistence, recovery, inspection, and
dashboard snapshots.
"""
from __future__ import annotations

from .circuit_breaker import CircuitState, PaperCircuitBreaker
from .control import (
    ControlCenterError,
    InvalidLifecycleTransitionError,
    NotPaperModeError,
    PaperBrokerRequiredError,
    PaperTradingControlCenter,
    UnknownDeploymentError,
)
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
)
from .deployment import (
    PaperDeploymentConfig,
    PaperDeploymentRecord,
    PaperDeploymentStatus,
    PaperDeployment,
    deployment_identity,
)
from .events import (
    PaperOperationEvent,
    PaperOperationEventType,
    PaperOperationsEventLog,
)
from .gate import DeploymentGate, GateDecision, PAPER_TRADING_GATE_REASONS
from .health import HealthStatus, PaperHealthConfig, PaperHealthMonitor
from .operations import PaperOperationsState
from .report import (
    PaperOperationsReport,
    PaperTradingReport,
    build_operations_report,
    build_paper_operations_evidence,
    build_paper_trading_evidence,
    build_report,
    run_paper_replay,
)
from .risk import PaperRiskConfig, PaperRiskGuard, RiskDecision
from .runner import PaperStrategyRunner, SignalType
from .session import (
    PaperSession,
    PaperSessionCheckpoint,
    PaperSessionStatus,
    PaperSessionStore,
    SessionIdentityError,
    SessionSchemaError,
    SESSION_SCHEMA_VERSION,
    apply_checkpoint_to_runner,
    checkpoint_from_session,
    session_from_runner,
    session_identity,
)
from .snapshot import PaperPerformanceSnapshot

__all__ = [
    # Phase 18
    "PaperDeploymentConfig",
    "PaperDeploymentRecord",
    "PaperDeploymentStatus",
    "PaperDeployment",
    "deployment_identity",
    "DeploymentGate",
    "GateDecision",
    "PaperStrategyRunner",
    "SignalType",
    "PaperTradingReport",
    "build_report",
    "build_paper_trading_evidence",
    "run_paper_replay",
    "PAPER_TRADING_GATE_REASONS",
    # Phase 19 — operations & monitoring
    "PaperOperationEvent",
    "PaperOperationEventType",
    "PaperOperationsEventLog",
    "PaperOperationsState",
    "PaperOperationsReport",
    "build_operations_report",
    "build_paper_operations_evidence",
    "PaperPerformanceSnapshot",
    "HealthStatus",
    "PaperHealthConfig",
    "PaperHealthMonitor",
    "RiskDecision",
    "PaperRiskConfig",
    "PaperRiskGuard",
    "CircuitState",
    "PaperCircuitBreaker",
    # Phase 20 — control center & recovery
    "PaperTradingControlCenter",
    "ControlCenterError",
    "InvalidLifecycleTransitionError",
    "NotPaperModeError",
    "PaperBrokerRequiredError",
    "UnknownDeploymentError",
    "PaperSession",
    "PaperSessionCheckpoint",
    "PaperSessionStatus",
    "PaperSessionStore",
    "SessionIdentityError",
    "SessionSchemaError",
    "SESSION_SCHEMA_VERSION",
    "PaperControlCenterSnapshot",
    "DashboardAccountBlock",
    "DashboardCircuitBreakerBlock",
    "DashboardDeploymentSummary",
    "DashboardEventSummary",
    "DashboardEvidenceSummary",
    "DashboardHealthBlock",
    "DashboardPerformanceBlock",
    "DashboardPositionsBlock",
    "DashboardRiskBlock",
    "DashboardStrategySummary",
    "apply_checkpoint_to_runner",
    "checkpoint_from_session",
    "session_from_runner",
    "session_identity",
]
