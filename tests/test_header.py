"""Phase 1 - Autonomous architecture tests.

Covers the complete autonomous layer: configuration, lifecycle, controller,
policy validation, decision model, deployment coordinator, and failure safety.

All tests are deterministic, paper-only, and follow the repository testing
conventions (inline fixtures, no conftest, SQLite in-memory, rich assert
messages, pytest.raises for exceptions).
"""
from __future__ import annotations

import pytest

from trading_system.paper.deployment import (
    PaperDeployment,
    PaperDeploymentConfig,
    PaperDeploymentStatus,
)
from trading_system.paper.control import (
    InvalidLifecycleTransitionError,
    PaperTradingControlCenter,
    UnknownDeploymentError,
)
from trading_system.research.strategy_lab.spec import StrategySpec

from trading_system.autonomous import (
    AutonomousBotConfig,
    AutonomousBotLifecycle,
    AutonomousController,
    AutonomousDecision,
    DeploymentCreationResult,
    PolicyValidator,
    PolicyValidationResult,
    Source,
)
from trading_system.autonomous.bot_lifecycle import (
    AutonomousBotState,
    BotTransitionError,
    is_valid_transition,
)
from trading_system.autonomous.bot_config import (
    UserConstraints,
    TradingMode,
    BotMode,
    BotState,
    TradingSessionConstraints,
)
