"""Autonomous Controller — Phase 1.

Skeleton orchestration layer that sits ABOVE the existing deployment/paper-trading
engine. It does NOT make autonomous trading decisions, market scans, or strategy
selections. It provides:

- Lifecycle management for the AutonomousBot
- Policy validation boundary
- Deployment coordination through the existing control center
- Extension points for future: MarketScanner, StrategySelector,
  TimeframeSelector, DecisionEngine, RiskPolicy, DeploymentCoordinator

The controller fails closed on missing/configuration errors and never
introduces a path to live/real order execution.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from pydantic import BaseModel, Field, model_validator

from trading_system.paper.control import (
    PaperTradingControlCenter,
    InvalidLifecycleTransitionError,
    UnknownDeploymentError,
)
from trading_system.paper.deployment import (
    PaperDeploymentStatus,
    PaperDeploymentConfig,
)
from trading_system.research.strategy_lab.spec import StrategySpec

from .bot_config import (
    AutonomousBotConfig,
    BotMode,
    BotState,
    PolicyValidationResult,
    PolicyValidator,
    Source,
    UserConstraints,
    AutonomousDecision,
)
from .bot_lifecycle import (
    AutonomousBotState,
    AutonomousBotLifecycle,
    BotTransitionError,
)
from .coordinator import (
    AutonomousDeploymentCoordinator,
    DeploymentCreationResult,
)
from .ranker import (
    OpportunityRanker,
    OpportunityRankingResult,
    RankerConfig,
)
from .scanner import (
    MarketScanResult,
    MarketScanner,
    MarketUniverse,
    ScannerConfig,
)
from .compatibility import (
    CompatibilityConfig,
    CompatibilityResult,
    StrategyCompatibilityEvaluator,
)
from .decision import (
    DecisionResult,
    SelectionConfig,
    StrategyDecisionEngine,
)


# --------------------------------------------------------------------------- #
# AutonomousController — the primary orchestration service.
# This is the "glue" that connects the autonomous layer to the existing
# deployment/paper-trading engine. It does NOT contain any trading logic,
# market-scanning intelligence, or strategy-selection algorithms.
# It merely orchestrates the existing engine based on structured decisions.
# --------------------------------------------------------------------------- #


class AutonomousController(BaseModel):
    """Phase 1 Autonomous Controller — lifecycle/orchestration skeleton.

    Responsibilities (Phase 1 skeleton, future phases will add intelligence):
      - starting a bot   -> transition CREATED -> STARTING -> RUNNING
      - stopping a bot   -> RUNNING -> STOPPING -> STOPPED
      - pausing a bot    -> RUNNING -> PAUSED
      - resuming a bot   -> PAUSED -> RUNNING
      - evaluating policy for decisions
      - creating/stopping autonomous deployments
      - monitoring active autonomous deployments
      - handling failures / recovery

    It EXTENSION POINTS (interfaces/ports) for future intelligent behavior:
      - MarketScanner         -> scan market, return candidates
      - StrategySelector      -> select a strategy for a candidate
      - TimeframeSelector     -> select timeframe for a candidate
      - DecisionEngine        -> generate an AutonomousDecision
      - RiskPolicy / PolicyValidator -> validate decisions against constraints
      - DeploymentCoordinator -> create/manage deployments

    NOT one of these (Phase 1, deliberately omitted):
      - autonomous trading decisions
      - market-scanning algorithms
      - strategy-selection intelligence
      - LLM integration
      - live/trading execution paths
    """

    model_config = {"extra": "forbid", "arbitrary_types_allowed": True}

    config: AutonomousBotConfig
    control_center: PaperTradingControlCenter
    lifecycle: AutonomousBotLifecycle
    _validator: Optional[PolicyValidator] = None

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    def __init__(self, *, config: AutonomousBotConfig, control_center: PaperTradingControlCenter) -> None:
        lifecycle = AutonomousBotLifecycle(
            initial_state=AutonomousBotState.CREATED
        )
        super().__init__(
            config=config,
            control_center=control_center,
            lifecycle=lifecycle,
        )
        self._validator = PolicyValidator(config)

    # ------------------------------------------------------------------ #
    # Lifecycle transitions
    # ------------------------------------------------------------------ #

    def start_bot(self) -> Tuple[bool, str]:
        """Start the autonomous bot.

        Transition graph: CREATED -> STARTING -> RUNNING

        Returns (success, message). On failure, the bot remains in its
        current state and the error message explains why.
        """
        try:
            # Transition CREATED -> STARTING
            if not self.lifecycle.can_transition_to(AutonomousBotState.STARTING):
                return False, f"bot cannot transition from {self.lifecycle.state.value} to STARTING"

            self.lifecycle.transition_to(AutonomousBotState.STARTING)
            self.config.state = AutonomousBotState.STARTING

            # Transition STARTING -> RUNNING
            if not self.lifecycle.can_transition_to(AutonomousBotState.RUNNING):
                # This shouldn't happen if the graph is correct, but be safe.
                self.lifecycle.transition_to(AutonomousBotState.CREATED)
                return False, "bot transition STARTING -> RUNNING failed unexpectedly"

            self.lifecycle.transition_to(AutonomousBotState.RUNNING)
            self.config.state = AutonomousBotState.RUNNING

            # Record the start timestamp.
            self.config.last_decision_timestamp = __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat()

            return True, f"bot started successfully; state={self.lifecycle.state.value}"

        except BotTransitionError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 — guard against unexpected errors
            # Fail closed: if we can't start, bot stays CREATED or goes to ERROR.
            try:
                self.lifecycle.transition_to(AutonomousBotState.ERROR)
                self.config.state = AutonomousBotState.ERROR
            except Exception:
                pass
            return False, f"unexpected error starting bot: {exc}"

    def stop_bot(self) -> Tuple[bool, str]:
        """Stop the autonomous bot.

        Transition graph: RUNNING -> STOPPING -> STOPPED
        Also stops any active autonomous deployments.

        Returns (success, message).
        """
        try:
            # Transition RUNNING -> STOPPING
            if not self.lifecycle.can_transition_to(AutonomousBotState.STOPPING):
                return False, f"bot cannot transition from {self.lifecycle.state.value} to STOPPING"

            self.lifecycle.transition_to(AutonomousBotState.STOPPING)
            self.config.state = AutonomousBotState.STOPPING

            # Stop all autonomous deployments for this bot.
            autonomous_deps = self.control_center.list_deployments()
            bot_deployments = [
                d for d in autonomous_deps
                if d.notes and d.notes.startswith("bot:") and d.notes.split(":", 1)[1] == self.config.bot_id
            ]

            for dep in bot_deployments:
                try:
                    self.control_center.stop_deployment(deployment_id=dep.deployment_id)
                except Exception:
                    # Continue stopping other deployments even if one fails.
                    pass

            # Transition STOPPING -> STOPPED
            if not self.lifecycle.can_transition_to(AutonomousBotState.STOPPED):
                self.lifecycle.transition_to(AutonomousBotState.STOPPED)
                return False, "bot transition STOPPING -> STOPPED failed unexpectedly"

            self.lifecycle.transition_to(AutonomousBotState.STOPPED)
            self.config.state = AutonomousBotState.STOPPED

            # Clear the decision timestamp.
            self.config.last_decision_timestamp = None

            return True, f"bot stopped successfully; state={self.lifecycle.state.value}"

        except BotTransitionError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            # Fail closed: enter ERROR safely if possible.
            try:
                self.lifecycle.transition_to(AutonomousBotState.ERROR)
                self.config.state = AutonomousBotState.ERROR
            except Exception:
                pass
            return False, f"unexpected error stopping bot: {exc}"

    def pause_bot(self) -> Tuple[bool, str]:
        """Pause the autonomous bot.

        Transition graph: RUNNING -> PAUSED

        Returns (success, message).
        """
        try:
            if not self.lifecycle.can_transition_to(AutonomousBotState.PAUSED):
                return False, f"bot cannot transition from {self.lifecycle.state.value} to PAUSED"

            self.lifecycle.transition_to(AutonomousBotState.PAUSED)
            self.config.state = AutonomousBotState.PAUSED

            return True, f"bot paused successfully; state={self.lifecycle.state.value}"

        except BotTransitionError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"unexpected error pausing bot: {exc}"

    def resume_bot(self) -> Tuple[bool, str]:
        """Resume the autonomous bot.

        Transition graph: PAUSED -> RUNNING

        Returns (success, message).
        """
        try:
            if not self.lifecycle.can_transition_to(AutonomousBotState.RUNNING):
                return False, f"bot cannot transition from {self.lifecycle.state.value} to RUNNING"

            self.lifecycle.transition_to(AutonomousBotState.RUNNING)
            self.config.state = AutonomousBotState.RUNNING

            return True, f"bot resumed successfully; state={self.lifecycle.state.value}"

        except BotTransitionError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"unexpected error resuming bot: {exc}"

    # ------------------------------------------------------------------ #
    # Policy validation for decisions
    # ------------------------------------------------------------------ #

    def validate_decision(
        self,
        *,
        symbol: str,
        strategy_id: str,
        timeframe: str,
    ) -> Tuple[PolicyValidationResult, str]:
        """Validate an autonomous decision against user constraints.

        Returns (result, message). If result is VALID, the decision may
        proceed to the Deployment Coordinator. If REJECTED, the message
        explains why.
        """
        result = self._validator.validate_decision(
            symbol=symbol,
            strategy_id=strategy_id,
            timeframe=timeframe,
        )
        if result == PolicyValidationResult.VALID:
            return PolicyValidationResult.VALID, "decision is valid"
        # Translate the validation result into a human-readable message.
        messages = {
            PolicyValidationResult.INVALID_SYMBOL: f"symbol '{symbol}' is not in the user-constrained allowed set",
            PolicyValidationResult.INVALID_STRATEGY: f"strategy_id '{strategy_id}' is not in the user-constrained allowed set",
            PolicyValidationResult.INVALID_TIMEFRAME: f"timeframe '{timeframe}' is not in the user-constrained allowed set",
            PolicyValidationResult.EXCEEDS_MAX_POSITIONS: "decision exceeds maximum simultaneous positions",
            PolicyValidationResult.EXCEEDS_MAX_EXPOSURE: "decision exceeds maximum aggregate exposure",
            PolicyValidationResult.EXCEEDS_MAX_DRAWDOWN: "decision exceeds maximum drawdown threshold",
            PolicyValidationResult.VIOLATES_SESSION_CONSTRAINTS: "decision violates trading session constraints",
        }
        return result, messages.get(result, "decision rejected by policy validator")

    # ------------------------------------------------------------------ #
    # Create autonomous deployment
    # ------------------------------------------------------------------ #

    def create_autonomous_deployment(
        self,
        *,
        symbol: str,
        strategy_id: str,
        timeframe: str,
        strategy_spec: StrategySpec,
        deployment_config: PaperDeploymentConfig,
    ) -> Tuple[DeploymentCreationResult, Optional["PaperDeployment"]]:
        """Attempt to create an autonomous deployment.

        This is the primary orchestration method. The flow:
          1. PolicyValidator validates the decision against user constraints.
          2. If valid, the AutonomousDeploymentCoordinator checks for
             duplicates and creates the deployment via the control center.
          3. On success, a runner + broker are attached and the deployment
             is activated.

        Returns (result, deployment) tuple.
        """
        # Step 1: Policy validation.
        validation_result, validation_message = self.validate_decision(
            symbol=symbol,
            strategy_id=strategy_id,
            timeframe=timeframe,
        )
        if validation_result != PolicyValidationResult.VALID:
            # Bot state unchanged; decision rejected.
            return DeploymentCreationResult.POLICY_VIOLATION, None

        # Step 2: Use the coordinator to create the deployment.
        coordinator = AutonomousDeploymentCoordinator(
            config=self.config,
            control_center=self.control_center,
        )

        result, deployment = coordinator.create_autonomous_deployment(
            symbol=symbol,
            strategy_id=strategy_id,
            timeframe=timeframe,
            strategy_spec=strategy_spec,
            deployment_config=deployment_config,
        )

        # Step 3: If successful, update bot decision count and timestamp.
        if result == DeploymentCreationResult.SUCCESS and deployment is not None:
            self.config.decision_count += 1
            self.config.last_decision_timestamp = __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat()

        return result, deployment

    # ------------------------------------------------------------------ #
    # Stop autonomous deployment
    # ------------------------------------------------------------------ #

    def stop_autonomous_deployment(self, deployment_id: str) -> Tuple[DeploymentCreationResult, str]:
        """Stop an autonomous deployment by ID.

        Returns (result, message).
        """
        try:
            self.control_center.stop_deployment(deployment_id)
        except InvalidLifecycleTransitionError as exc:
            # Already stopped or in a terminal state — treat as success.
            return DeploymentCreationResult.SUCCESS, f"deployment already stopped: {exc}"
        except Exception as exc:  # noqa: BLE001
            return DeploymentCreationResult.FAILURE, f"error stopping deployment: {exc}"

        deployment = self.control_center.get_deployment(deployment_id)
        if deployment is None:
            return DeploymentCreationResult.FAILURE, "deployment not found"

        # Verify ownership.
        if deployment.notes and deployment.notes.startswith("bot:"):
            bot_id = deployment.notes.split(":", 1)[1]
            if bot_id != self.config.bot_id:
                return DeploymentCreationResult.FAILURE, "deployment does not belong to this bot"

        # Update bot lifecycle if needed.
        # If this was the last active deployment, consider pausing the bot.
        # For Phase 1, we just report the result.
        return DeploymentCreationResult.SUCCESS, f"deployment {deployment_id} stopped"

    # ------------------------------------------------------------------ #
    # List autonomous deployments
    # ------------------------------------------------------------------ #

    def list_autonomous_deployments(self) -> list["PaperDeployment"]:
        """List all autonomous deployments belonging to this bot."""
        all_deps = self.control_center.list_deployments()
        return [
            d for d in all_deps
            if d.notes and d.notes.startswith("bot:") and d.notes.split(":", 1)[1] == self.config.bot_id
        ]

    # ------------------------------------------------------------------ #
    # Market scanning (Phase 2)
    # ------------------------------------------------------------------ #

    def scan_market(self) -> MarketScanResult:
        """Run a deterministic market scan over the bot's constrained universe.

        Builds a :class:`ScannerConfig` from the bot's ``user_constraints``
        and the control center's ``load_market_data`` callable, then delegates
        to :class:`MarketScanner`. The scan produces candidate discovery only —
        it does NOT select strategies, generate signals, place orders, or
        create deployments.

        Fails closed: if no market-data provider is configured on the control
        center, every symbol is rejected as ``MISSING_MARKET_DATA``.
        """
        allowed = self.config.user_constraints.allowed_symbols
        universe = MarketUniverse(symbols=list(allowed)) if allowed else MarketUniverse()

        config = ScannerConfig(
            enabled=self.config.enabled,
            universe=universe,
            timeframe="1d",
            data_provider=self.control_center.load_market_data,
            data_provider_source="control_center.load_market_data",
        )
        result = MarketScanner(config).scan()

        from datetime import datetime as _dt, timezone as _tz

        self.config.last_scan_timestamp = _dt.now(_tz.utc).isoformat()
        return result

    def rank_candidates(
        self,
        scan_result: MarketScanResult,
        config: Optional[RankerConfig] = None,
    ) -> OpportunityRankingResult:
        """Rank eligible candidates from a Phase 2 scan result into opportunities.

        Delegates to :class:`OpportunityRanker` with the control center's
        ``load_market_data`` callable for feature calculation.  Does NOT select
        strategies, generate signals, place orders, or create deployments.
        """
        ranker_config = config or RankerConfig(enabled=self.config.enabled)
        ranker = OpportunityRanker(
            config=ranker_config,
            data_provider=self.control_center.load_market_data,
        )
        return ranker.rank(scan_result)

    # ------------------------------------------------------------------ #
    # Phase 4 — Strategy & Timeframe Compatibility
    # ------------------------------------------------------------------ #

    def evaluate_strategy_compatibility(
        self,
        ranking_result: OpportunityRankingResult,
        config: Optional[CompatibilityConfig] = None,
    ) -> CompatibilityResult:
        """Evaluate which registered strategies are compatible with ranked opportunities.

        Delegates to :class:`StrategyCompatibilityEvaluator` using the operator's
        ``allowed_strategy_ids`` constraint.  Does NOT execute strategies, generate
        signals, place orders, create deployments, or perform live trading.

        Snapshot consistency:
        The evaluator consumes the Phase 3 :class:`OpportunityRankingResult`
        directly — no new market-data fetches are performed.  All market-condition
        features come from the Phase 2 snapshot embedded in the ranking.
        """
        compat_config = config or CompatibilityConfig(enabled=self.config.enabled)
        evaluator = StrategyCompatibilityEvaluator(compat_config)
        allowed = self.config.user_constraints.allowed_strategy_ids
        return evaluator.evaluate(ranking_result, allowed_strategies=allowed)

    # ------------------------------------------------------------------ #
    # Phase 5 -- Strategy Selection & Signal Generation
    # ------------------------------------------------------------------ #

    def generate_strategy_decisions(
        self,
        compatibility_result: CompatibilityResult,
        config: Optional[SelectionConfig] = None,
    ) -> DecisionResult:
        """Phase 5 -- select a strategy/timeframe per opportunity and generate signals.

        Orchestrates Phase 5 on top of a Phase 4 compatibility result:
        CompatibilityResult -> StrategySelection -> StrategyEvaluation -> DecisionResult.

        Delegates to :class:`StrategyDecisionEngine` using the control center's
        ``load_market_data`` callable for look-ahead-safe OHLCV data.  Does NOT
        create orders, create deployments, allocate capital, submit broker
        orders, or manage positions.
        """
        sel_config = config or SelectionConfig(enabled=self.config.enabled)
        engine = StrategyDecisionEngine(
            sel_config,
            data_provider=self.control_center.load_market_data,
        )
        return engine.generate_decisions(compatibility_result)

    # ------------------------------------------------------------------ #
    # Inspect bot state
    # ------------------------------------------------------------------ #

    def inspect(self) -> dict[str, Any]:
        """Return a snapshot of the bot's current state for dashboard/UI."""
        return {
            "bot_id": self.config.bot_id,
            "name": self.config.name,
            "state": self.lifecycle.state.value,
            "mode": self.config.mode.value,
            "trading_mode": self.config.trading_mode.value,
            "enabled": self.config.enabled,
            "decision_count": self.config.decision_count,
            "deployment_count": self.config.deployment_count,
            "last_decision_timestamp": self.config.last_decision_timestamp,
            "last_scan_timestamp": self.config.last_scan_timestamp,
            "source": self.config.source.value,
            "allowed_symbols": sorted(self.config.user_constraints.allowed_symbols),
            "allowed_strategy_ids": sorted(self.config.user_constraints.allowed_strategy_ids),
            "allowed_timeframes": sorted(self.config.user_constraints.allowed_timeframes),
            "max_simultaneous_positions": self.config.max_simultaneous_positions,
            "max_position_allocation_pct": self.config.max_position_allocation_pct,
            "max_exposure_pct": self.config.max_exposure_pct,
            "max_drawdown_pct": self.config.user_constraints.max_drawdown_pct,
        }