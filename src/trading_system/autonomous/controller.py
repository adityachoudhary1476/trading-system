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
    ControlCenterError,
    InvalidLifecycleTransitionError,
    UnknownDeploymentError,
)
from trading_system.paper.deployment import (
    PaperDeploymentStatus,
    PaperDeploymentConfig,
)
from trading_system.india.instrument_repository import InstrumentRepository
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
    TradingDecision,
)
from .events import AutonomousEventLog, AutonomousEventType
from .options.model import OptionsContractSelection
from .options.discovery import (
    CurrentOptionDiscoverer,
    DiscoveryConfig,
    CandidateEvaluationResult,
)
from ..india.option_quotes import CurrentOptionQuoteProvider, OptionQuote
from ..execution.orders import OrderIntent, OrderType, Side, OrderResult
from ..india.instruments import Instrument
from .options_contract import (
    DEFAULT_OPTIONS_CONFIG,
    InMemoryOptionsChainProvider,
    OptionsChainProvider,
    OptionsStructureBuilder,
    OptionsTradeConfig,
    OptionsTradePlan,
)
from .safety import (
    IdempotencyGuard,
    KillSwitch,
    KillSwitchReason,
    KillSwitchState,
    Phase7Config,
    Phase7SafetyLayer,
    SafetyResult,
    SafetyValidator,
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
    _event_log: Optional[AutonomousEventLog] = None
    _safety_layer: Optional[Phase7SafetyLayer] = None
    _options_builder: Optional[OptionsStructureBuilder] = None
    _chain_provider: Optional[OptionsChainProvider] = None
    _option_discoverer: Optional[CurrentOptionDiscoverer] = None
    _instrument_repo: Optional[InstrumentRepository] = None
    _quote_provider: Optional[object] = None

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    def __init__(self, *, config: AutonomousBotConfig, control_center: PaperTradingControlCenter, persistence: Optional[Any] = None) -> None:
        lifecycle = AutonomousBotLifecycle(
            initial_state=AutonomousBotState.CREATED
        )
        super().__init__(
            config=config,
            control_center=control_center,
            lifecycle=lifecycle,
        )
        self._validator = PolicyValidator(config)
        self._event_log = AutonomousEventLog(config.bot_id)
        self._safety_layer = Phase7SafetyLayer(
            config=Phase7Config(),
            kill_switch=KillSwitch(),
            validator=SafetyValidator(Phase7Config()),
            idempotency=IdempotencyGuard(),
        )
        self._options_builder = OptionsStructureBuilder()
        self._persistence = persistence

    @property
    def event_log(self) -> AutonomousEventLog:
        """Return the in-memory autonomous event log for this bot."""
        return self._event_log

    @property
    def kill_switch(self) -> KillSwitch:
        """The bot-level Phase 7 kill switch."""
        return self._safety_layer.kill_switch

    @property
    def safety_validator(self) -> SafetyValidator:
        """The Phase 7 safety validator."""
        return self._safety_layer.validator

    @property
    def idempotency_guard(self) -> IdempotencyGuard:
        """The Phase 7 idempotency guard."""
        return self._safety_layer.idempotency

    @property
    def is_halted(self) -> bool:
        """True if the bot-level kill switch is tripped."""
        return self._safety_layer.kill_switch.is_halted

    def load_state(self, state: dict[str, Any]) -> None:
        """Restore controller state from persisted storage."""
        if not state:
            return
        bot_state = state.get("state")
        if bot_state:
            try:
                self.config.state = AutonomousBotState(bot_state)
                self.lifecycle = AutonomousBotLifecycle(
                    initial_state=AutonomousBotState(bot_state)
                )
            except ValueError:
                pass
        enabled = state.get("enabled")
        if enabled is not None:
            self.config.enabled = bool(enabled)
        kill_switch_state = state.get("kill_switch_state")
        if kill_switch_state == "halted":
            self._safety_layer.kill_switch.halt(
                state.get("kill_switch_reason") or KillSwitchReason.MANUAL,
                detail=state.get("kill_switch_reason") or "",
            )

    def _persist(self) -> None:
        """Persist the current bot state."""
        if self._persistence is None:
            return
        try:
            self._persistence.save_state(
                bot_id=self.config.bot_id,
                state=self.config.state.value,
                enabled=self.config.enabled,
                kill_switch_state=self._safety_layer.kill_switch.state.value,
                kill_switch_reason=(
                    self._safety_layer.kill_switch.reason.value
                    if self._safety_layer.kill_switch.reason
                    else None
                ),
                kill_switch_halted_at=self._safety_layer.kill_switch.halt_timestamp,
            )
        except Exception:
            pass

    def _record_event(self, event_type: AutonomousEventType, **kwargs: Any) -> None:
        """Record an event defensively — never blocks the calling operation."""
        try:
            self._event_log.record(event_type, **kwargs)
        except Exception:
            pass

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
            self.config.enabled = True

            self._record_event(AutonomousEventType.BOT_STARTED)

            # Record the start timestamp.
            self.config.last_decision_timestamp = __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat()

            self._persist()

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
            if self.lifecycle.state == AutonomousBotState.STOPPED:
                return True, f"bot already stopped; state={self.lifecycle.state.value}"

            # Transition RUNNING/PAUSED -> STOPPING
            if not self.lifecycle.can_transition_to(AutonomousBotState.STOPPING):
                return False, f"bot cannot transition from {self.lifecycle.state.value} to STOPPING"

            self.lifecycle.transition_to(AutonomousBotState.STOPPING)
            self.config.state = AutonomousBotState.STOPPING

            # Stop all autonomous deployments for this bot.
            # This must not prevent the bot from reaching STOPPED if the
            # database is missing columns or otherwise unhealthy.
            try:
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
            except Exception:
                pass

            # Transition STOPPING -> STOPPED
            if not self.lifecycle.can_transition_to(AutonomousBotState.STOPPED):
                self.lifecycle.transition_to(AutonomousBotState.STOPPED)
                return False, "bot transition STOPPING -> STOPPED failed unexpectedly"

            self.lifecycle.transition_to(AutonomousBotState.STOPPED)
            self.config.state = AutonomousBotState.STOPPED
            self.config.enabled = False

            # Phase 7: Halt the kill switch on stop.
            self._safety_layer.kill_switch.halt(
                KillSwitchReason.DEPLOYMENT_ERROR,
                detail="bot stopped - all trading halted",
            )

            self._record_event(AutonomousEventType.BOT_STOPPED)

            # Clear the decision timestamp.
            self.config.last_decision_timestamp = None

            self._persist()

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

            self._record_event(AutonomousEventType.BOT_PAUSED)

            self._persist()

            return True, f"bot paused successfully; state={self.lifecycle.state.value}"

        except BotTransitionError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"unexpected error pausing bot: {exc}"

    def resume_bot(self) -> Tuple[bool, str]:
        """Resume the autonomous bot.

        If the kill switch is tripped (Phase 7), clears it.  If the bot is
        PAUSED, performs the PAUSED -> RUNNING lifecycle transition.  If the
        bot is already RUNNING, only clears the kill switch.

        Returns (success, message).
        """
        try:
            was_halted = self._safety_layer.kill_switch.is_halted
            # Phase 7: Always clear the kill switch on resume.
            self._safety_layer.kill_switch.resume()

            if self.lifecycle.state == AutonomousBotState.RUNNING:
                if was_halted:
                    # Bot was running but kill-switched — just clear and succeed.
                    self._record_event(AutonomousEventType.BOT_RESUMED)
                    return True, f"bot resumed (kill switch cleared); state={self.lifecycle.state.value}"
                return False, "bot is already running"

            if self.lifecycle.state == AutonomousBotState.PAUSED:
                if not self.lifecycle.can_transition_to(AutonomousBotState.RUNNING):
                    return False, f"bot cannot transition from {self.lifecycle.state.value} to RUNNING"
                self.lifecycle.transition_to(AutonomousBotState.RUNNING)
                self.config.state = AutonomousBotState.RUNNING
                self.config.enabled = True
            else:
                return False, f"bot cannot resume from {self.lifecycle.state.value}"

            self._record_event(AutonomousEventType.BOT_RESUMED)

            self._persist()

            return True, f"bot resumed successfully; state={self.lifecycle.state.value}"

        except BotTransitionError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"unexpected error resuming bot: {exc}"

    def halt_bot(self, reason: KillSwitchReason, detail: str = "") -> Tuple[bool, str]:
        """Immediately halt the bot-level kill switch (Phase 7).

        Halts the kill switch, pauses all active bot-owned deployments via the
        PaperTradingControlCenter, and records a BOT_HALTED event.

        Returns (success, message).
        """
        self._safety_layer.kill_switch.halt(reason, detail=detail)
        self._record_event(
            AutonomousEventType.BOT_HALTED,
            message=detail or reason.value,
            payload={"reason": reason.value, "detail": detail},
        )

        # Pause all bot-owned deployments.
        try:
            autonomous_deps = self.control_center.list_deployments()
            bot_deployments = [
                d for d in autonomous_deps
                if d.notes and d.notes.startswith("bot:") and d.notes.split(":", 1)[1] == self.config.bot_id
            ]
            for dep in bot_deployments:
                try:
                    self.control_center.pause_deployment(deployment_id=dep.deployment_id)
                except Exception:
                    pass
        except Exception:
            pass

        self._persist()

        return True, f"bot halted (reason={reason.value}); kill_switch={KillSwitchState.HALTED.value}"

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
        # Phase 7: Reject if kill switch is tripped.
        if self._safety_layer.kill_switch.is_halted:
            self._record_event(
                AutonomousEventType.ERROR,
                symbol=symbol,
                message=f"deployment blocked by kill switch (reason={self._safety_layer.kill_switch.reason})",
            )
            return DeploymentCreationResult.HALTED, None

        # Step 1: Policy validation.
        validation_result, validation_message = self.validate_decision(
            symbol=symbol,
            strategy_id=strategy_id,
            timeframe=timeframe,
        )
        if validation_result != PolicyValidationResult.VALID:
            self._record_event(
                AutonomousEventType.POLICY_VIOLATION,
                symbol=symbol,
                message=validation_message,
                payload={"strategy_id": strategy_id, "validation_result": validation_result.value},
            )
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
            self._record_event(
                AutonomousEventType.DEPLOYMENT_CREATED,
                deployment_id=deployment.deployment_id,
                symbol=deployment.symbol,
                timeframe=deployment.timeframe,
            )
        elif result == DeploymentCreationResult.FAILURE:
            self._record_event(
                AutonomousEventType.ERROR,
                symbol=symbol,
                message="deployment creation failed",
                payload={"result": result.value},
            )

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
            self._record_event(
                AutonomousEventType.DEPLOYMENT_STOPPED,
                deployment_id=deployment_id,
                message=f"deployment already stopped: {exc}",
            )
            return DeploymentCreationResult.SUCCESS, f"deployment already stopped: {exc}"
        except Exception as exc:  # noqa: BLE001
            self._record_event(
                AutonomousEventType.ERROR,
                deployment_id=deployment_id,
                message=f"error stopping deployment: {exc}",
            )
            return DeploymentCreationResult.FAILURE, f"error stopping deployment: {exc}"

        deployment = self.control_center.get_deployment(deployment_id)
        if deployment is None:
            self._record_event(
                AutonomousEventType.ERROR,
                deployment_id=deployment_id,
                message="deployment not found",
            )
            return DeploymentCreationResult.FAILURE, "deployment not found"

        # Verify ownership.
        if deployment.notes and deployment.notes.startswith("bot:"):
            bot_id = deployment.notes.split(":", 1)[1]
            if bot_id != self.config.bot_id:
                self._record_event(
                    AutonomousEventType.ERROR,
                    deployment_id=deployment_id,
                    message="deployment does not belong to this bot",
                )
                return DeploymentCreationResult.FAILURE, "deployment does not belong to this bot"

        # Update bot lifecycle if needed.
        # If this was the last active deployment, consider pausing the bot.
        # For Phase 1, we just report the result.
        self._record_event(
            AutonomousEventType.DEPLOYMENT_STOPPED,
            deployment_id=deployment_id,
            symbol=deployment.symbol,
            timeframe=deployment.timeframe,
        )
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
        if self._safety_layer.kill_switch.is_halted:
            self._record_event(
                AutonomousEventType.ERROR,
                message=f"scan blocked by kill switch (reason={self._safety_layer.kill_switch.reason})",
            )
            from datetime import datetime as _dt, timezone as _tz
            _now = _dt.now(_tz.utc).isoformat()
            return MarketScanResult(
                scan_id=f"halted_{self.config.bot_id}",
                scan_timestamp=_now,
                timeframe="1d",
                enabled=False,
                universe_size=0,
                scanned_count=0,
                eligible_count=0,
                rejected_count=0,
                skipped_count=0,
                started_at=_now,
                completed_at=_now,
            )

        allowed = self.config.user_constraints.allowed_symbols
        universe = MarketUniverse(symbols=list(allowed)) if allowed else MarketUniverse()

        config = ScannerConfig(
            enabled=self.config.enabled,
            universe=universe,
            timeframe="1d",
            data_provider=self.control_center.load_market_data,
            data_provider_source="control_center.load_market_data",
        )
        self._record_event(AutonomousEventType.SCAN_STARTED, timeframe=config.timeframe)

        result = MarketScanner(config).scan()

        self._record_event(AutonomousEventType.SCAN_COMPLETED, timeframe=config.timeframe)
        self._record_event(
            AutonomousEventType.CANDIDATES_FOUND,
            timeframe=config.timeframe,
            payload={"eligible_count": result.eligible_count},
        )

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
        if self._safety_layer.kill_switch.is_halted:
            self._record_event(
                AutonomousEventType.ERROR,
                message=f"decision generation blocked by kill switch (reason={self._safety_layer.kill_switch.reason})",
            )
            from datetime import datetime as _dt, timezone as _tz
            _now = _dt.now(_tz.utc).isoformat()
            return DecisionResult(
                result_id=f"halted_{self.config.bot_id}_{_now}",
                evaluated_at=_now,
            )

        sel_config = config or SelectionConfig(enabled=self.config.enabled)
        engine = StrategyDecisionEngine(
            sel_config,
            data_provider=self.control_center.load_market_data,
        )
        return engine.generate_decisions(compatibility_result)

    # ------------------------------------------------------------------ #
    # Phase 8 — Options Contract Selection
    # ------------------------------------------------------------------ #

    def set_chain_provider(self, provider: Optional[OptionsChainProvider]) -> None:
        """Attach an options chain provider for Phase 8 contract selection.

        When set, ``resolve_options_plan`` can translate a TradingDecision's
        underlying signal into option contract legs. Pass ``None`` to detach.
        """
        self._chain_provider = provider

    def resolve_options_plan(
        self,
        decision: "TradingDecision",
        *,
        chain_provider: Optional[OptionsChainProvider] = None,
        config: Optional[OptionsTradeConfig] = None,
        existing_positions: Optional[list] = None,
    ) -> Optional[OptionsTradePlan]:
        """Resolve a Phase 5 TradingDecision to a Phase 8 OptionsTradePlan.

        Translates the decision's underlying-based signal (BUY/SELL/EXIT/HOLD)
        into an options contract trade plan: legs, risk metrics, and
        ``OrderIntent`` objects for the paper broker.

        Returns ``None`` for HOLD signals, invalid decisions, or when no
        chain provider is available.
        """
        if self._safety_layer.kill_switch.is_halted:
            self._record_event(
                AutonomousEventType.ERROR,
                message=f"options resolution blocked by kill switch (reason={self._safety_layer.kill_switch.reason})",
            )
            return None

        provider = chain_provider or self._chain_provider
        if provider is None:
            return None

        # Phase 8A guard: reject synthetic chain providers in production.
        # InMemoryOptionsChainProvider generates deterministic/synthetic
        # premiums � these must never silently enter real paper execution.
        from trading_system.autonomous.options_contract import InMemoryOptionsChainProvider
        if isinstance(provider, InMemoryOptionsChainProvider):
            self._record_event(
                AutonomousEventType.ERROR,
                message="resolve_options_plan rejected: InMemoryOptionsChainProvider "
                "is synthetic and must not be used for production pricing",
            )
            return None

        cfg = config or DEFAULT_OPTIONS_CONFIG
        return self._options_builder.build(
            decision=decision,
            chain_provider=provider,
            config=cfg,
            existing_positions=existing_positions,
        )

    # ------------------------------------------------------------------ #
    # Phase 8B — Current-market option candidate discovery
    # ------------------------------------------------------------------ #

    def set_option_discoverer(
        self,
        discoverer: CurrentOptionDiscoverer,
        repository: Optional[InstrumentRepository] = None,
    ) -> None:
        """Attach a current-market option discoverer.

        The discoverer queries the real ``InstrumentRepository`` for currently
        available option contracts and evaluates candidates using actual
        instrument data (strike, expiry, option type).

        When set, ``discover_options_contract`` can translate a bullish/bearish
        thesis into a concrete, repository-resolved ``Instrument``.
        """
        self._option_discoverer = discoverer
        if repository is not None:
            self._instrument_repo = repository

    def discover_options_contract(
        self,
        *,
        decision: "TradingDecision",
        spot_price: float,
        config: Optional[DiscoveryConfig] = None,
        as_of: Optional[str] = None,
        explicit_direction: Optional[Any] = None,
    ) -> Optional[CandidateEvaluationResult]:
        """Discover and evaluate current option candidates for a decision.

        Uses the actual ``InstrumentRepository`` to find currently-available
        option contracts. Does NOT fabricate contracts.

        Returns ``None`` if no discoverer is attached, or if no suitable
        contract is found.
        """
        if self._safety_layer.kill_switch.is_halted:
            self._record_event(
                AutonomousEventType.ERROR,
                symbol=decision.opportunity_symbol,
                message=f"option discovery blocked by kill switch (reason={self._safety_layer.kill_switch.reason})",
            )
            return None

        discoverer = self._option_discoverer
        if discoverer is None:
            return None

        from .options.model import OptionDirection
        if explicit_direction is not None:
            direction = explicit_direction
        else:
            action = decision.action
            if action == "buy":
                direction = OptionDirection.CALL
            elif action == "sell":
                direction = OptionDirection.PUT
            else:
                return None  # HOLD or EXIT � no new option contract

        underlying = decision.opportunity_symbol
        # Strip exchange prefix if present (e.g. "NSE:NIFTY" → "NIFTY")
        if ":" in underlying:
            underlying = underlying.split(":", 1)[1]

        result = discoverer.discover(
            underlying=underlying,
            direction=direction,
            spot_price=spot_price,
            config=config,
            as_of=as_of,
        )

        if result is None:
            self._record_event(
                AutonomousEventType.ERROR,
                symbol=underlying,
                message="option discovery returned no result",
                payload={},
            )
            return None

        if not result.has_selection:
            self._record_event(
                AutonomousEventType.DECISION_REJECTED,
                symbol=underlying,
                message=f"no suitable option contract selected ({len(result.candidates)} candidates evaluated)",
                payload=result.to_dict(),
            )
            return result

        self._record_event(
            AutonomousEventType.DECISION_CREATED,
            symbol=underlying,
            message=f"selected option contract {result.selected.instrument.key}",
            payload=result.to_dict(),
        )

        return result

    # ------------------------------------------------------------------ #
    # Phase 8C — Current option premium integration
    # ------------------------------------------------------------------ #

    def set_quote_provider(self, provider: "CurrentOptionQuoteProvider") -> None:
        """Attach a current option quote provider for Phase 8C.

        When set, ``execute_option_order`` can fetch the real-time option
        premium for the exact selected contract from Upstox market data.

        Pass ``None`` to detach (disables autonomous option premium fetch).
        """
        self._quote_provider = provider

    @property
    def quote_provider(self) -> Optional[object]:
        """The attached Phase 8C quote provider, or None."""
        return self._quote_provider

    def fetch_option_premium(
        self,
        *,
        decision: "TradingDecision",
        spot_price: float,
        config: Optional[DiscoveryConfig] = None,
        as_of: Optional[str] = None,
        max_quote_age_seconds: Optional[float] = None,
        explicit_direction: Optional[Any] = None,
    ) -> Optional[OptionQuote]:
        """Discover the exact option contract and fetch its current premium.

        Phase 8C integration: discovery (Phase 8B) + live premium fetch.

        Returns ``OptionQuote`` on success, ``None`` on any failure
        (no discoverer, no selection, no quote provider, quote failure,
        or stale quote).  Never raises — fail-closed by design.
        """
        if self._quote_provider is None:
            self._record_event(
                AutonomousEventType.ERROR,
                symbol=decision.opportunity_symbol,
                message="option premium fetch skipped: no quote provider attached",
            )
            return None

        result = self.discover_options_contract(
            decision=decision,
            spot_price=spot_price,
            config=config,
            as_of=as_of,
            explicit_direction=explicit_direction,
        )
        if result is None or not result.has_selection:
            return None

        instrument = result.selected.instrument
        quote = self._quote_provider.get_quote(instrument)
        if quote is None:
            self._record_event(
                AutonomousEventType.ERROR,
                symbol=instrument.underlying or decision.opportunity_symbol,
                message=f"failed to fetch option premium for {instrument.contract_id}",
            )
            return None

        if not self._quote_provider.is_fresh(quote, max_age_seconds=max_quote_age_seconds):
            self._record_event(
                AutonomousEventType.ERROR,
                symbol=instrument.underlying or decision.opportunity_symbol,
                message=f"option quote stale: age={quote.age_seconds:.0f}s for {instrument.contract_id}",
            )
            return None

        return quote

    def execute_option_order(
        self,
        *,
        decision: "TradingDecision",
        spot_price: float,
        deployment_id: Optional[str] = None,
        session_id: Optional[str] = None,
        config: Optional[DiscoveryConfig] = None,
        as_of: Optional[str] = None,
        max_quote_age_seconds: Optional[float] = None,
        order_quantity: float = 1.0,
        client_order_id: Optional[str] = None,
        options_deployment_config: Optional[Any] = None,
        explicit_option_type: Optional[str] = None,
        explicit_side: Optional[str] = None,
        explicit_instrument: Optional[Any] = None,
        existing_position: Optional[Any] = None,
    ) -> Optional["OrderResult"]:
        """Full autonomous options execution: discovery → premium → safety → paper fill.

        The caller must NOT supply ``current_price`` — this method obtains
        the option's current premium automatically from Upstox market data.

        For exits, pass ``existing_position`` (a ``Position`` or namespace with
        ``options_contract_id``, ``strike``, ``expiry``, ``option_type``,
        ``contract_size``) to close that exact contract instead of discovering
        a new one.

        Flow:
          1. Phase 7 kill-switch check (fail closed).
          2. If exiting: build Instrument from existing position.
          3. If entering: Phase 8B discover exact option contract from InstrumentRepository.
          4. Phase 8C: fetch current option premium from Upstox (real-time).
          5. Validate quote freshness.
          6. Phase 7 safety: validate contract validity (CE/PE, expiry).
          7. PaperBroker.update_market_price() with the exact option premium.
          8. Construct OrderIntent with current_price=premium (no manual injection).
          9. submit_order_intent through the control center → PaperBroker fill.
          10. Paper position created/closed with premium-based avg_entry_price.

        Returns ``OrderResult`` on success, ``None`` on any rejection/failure.
        """
        if self._safety_layer.kill_switch.is_halted:
            self._record_event(
                AutonomousEventType.ERROR,
                message=f"option execution blocked by kill switch (reason={self._safety_layer.kill_switch.reason})",
            )
            return None

        if self._quote_provider is None:
            self._record_event(
                AutonomousEventType.ERROR,
                symbol=decision.opportunity_symbol,
                message="option execution skipped: no quote provider attached",
            )
            return None

        # --- Resolve instrument + premium ---
        from trading_system.india.instruments import (
            Instrument,
            InstrumentType,
            InternalSymbol,
        )

        instrument = None
        quote = None

        if existing_position is not None:
            pos_contract_id = getattr(existing_position, "options_contract_id", None)
            pos_strike = getattr(existing_position, "strike", None)
            pos_expiry = getattr(existing_position, "expiry", None)
            pos_option_type = getattr(existing_position, "option_type", None)
            pos_contract_size = getattr(existing_position, "contract_size", 1)

            if pos_contract_id and pos_strike and pos_expiry and pos_option_type:
                underlying = decision.opportunity_symbol.split(":")[-1] if ":" in decision.opportunity_symbol else decision.opportunity_symbol
                itype = InstrumentType.OPTION_CE if pos_option_type == "CE" else InstrumentType.OPTION_PE
                token = f"{underlying}{pos_expiry.replace('-', '')}{int(pos_strike)}{pos_option_type}"
                instrument = Instrument(
                    internal=InternalSymbol(exchange="NFO", symbol=token),
                    instrument_type=itype,
                    name=f"{underlying} {pos_option_type} {pos_strike} {pos_expiry}",
                )
                instrument.underlying = underlying
                instrument.expiry = pos_expiry
                instrument.strike = float(pos_strike)
                instrument.option_type = pos_option_type
                instrument.lot_size = int(pos_contract_size) if pos_contract_size else 1
                instrument.provider_symbol = pos_contract_id

                quote = self._quote_provider.get_quote(instrument)
                if quote is None:
                    self._record_event(
                        AutonomousEventType.ERROR,
                        symbol=underlying,
                        message=f"failed to fetch exit premium for {pos_contract_id}",
                    )
                    return None
                if not self._quote_provider.is_fresh(quote, max_age_seconds=max_quote_age_seconds):
                    self._record_event(
                        AutonomousEventType.ERROR,
                        symbol=underlying,
                        message=f"exit quote stale: age={quote.age_seconds:.0f}s for {pos_contract_id}",
                    )
                    return None
            else:
                self._record_event(
                    AutonomousEventType.ERROR,
                    symbol=decision.opportunity_symbol,
                    message="exit rejected: existing position missing contract metadata",
                )
                return None
        else:
            # --- Phase 8B + 8C: discover + fetch premium ---
            from .options.model import OptionDirection
            explicit_direction = None
            if explicit_option_type == "CE":
                explicit_direction = OptionDirection.CALL
            elif explicit_option_type == "PE":
                explicit_direction = OptionDirection.PUT

            if explicit_instrument is not None:
                instrument = explicit_instrument
                quote = None
                if self._quote_provider is not None:
                    quote = self._quote_provider.get_quote(instrument)
                    if quote is None or not self._quote_provider.is_fresh(quote, max_age_seconds=max_age_seconds):
                        self._record_event(
                            AutonomousEventType.ERROR,
                            symbol=getattr(instrument, "underlying", None) or decision.opportunity_symbol,
                            message="option quote unavailable or stale for existing contract " + getattr(instrument, "contract_id", ""),
                        )
                        return None
            else:
                quote = self.fetch_option_premium(
                    decision=decision,
                    spot_price=spot_price,
                    config=config,
                    as_of=as_of,
                    max_quote_age_seconds=max_quote_age_seconds,
                    explicit_direction=explicit_direction,
                )
                if quote is None:
                    return None
                instrument = quote.instrument

        # Deterministic client_order_id for idempotency (generated before
        # the idempotency guard so cached results can be looked up on replay).
        action_for_id = decision.action
        if client_order_id is None:
            import hashlib
            client_order_id = hashlib.sha256(
                f"{decision.decision_id}:{instrument.contract_id}:{action_for_id}".encode("utf-8")
            ).hexdigest()[:48]

        # --- Phase 7 safety: contract validity ---
        option_type = explicit_option_type or instrument.option_type
        selection = OptionsContractSelection.from_instrument(instrument)
        safety = self._safety_layer.validator.check_contract_validity(
            options_selection=selection,
            allowed_option_types=[option_type] if option_type else ["CE", "PE"],
            now_date=__import__("datetime").date.today().isoformat(),
        )
        if not safety.passed:
            self._record_event(
                AutonomousEventType.POLICY_VIOLATION,
                symbol=instrument.underlying or decision.opportunity_symbol,
                message=f"Phase 7 contract safety failed: {safety.failed_checks}",
            )
            return None

        # --- Deployment config checks ---
        if options_deployment_config is not None:
            if not getattr(options_deployment_config, "options_enabled", True):
                self._record_event(
                    AutonomousEventType.ERROR,
                    symbol=instrument.underlying or decision.opportunity_symbol,
                    message="option execution skipped: options not enabled on deployment",
                )
                return None
            allowed_types = getattr(options_deployment_config, "allowed_option_types", []) or []
            if option_type not in allowed_types:
                self._record_event(
                    AutonomousEventType.POLICY_VIOLATION,
                    symbol=instrument.underlying or decision.opportunity_symbol,
                    message=f"option type {option_type} not allowed; allowed={allowed_types}",
                )
                return None
            max_contracts = getattr(options_deployment_config, "max_options_contracts_per_trade", None)
            if max_contracts is not None and order_quantity > max_contracts:
                self._record_event(
                    AutonomousEventType.POLICY_VIOLATION,
                    symbol=instrument.underlying or decision.opportunity_symbol,
                    message=f"order quantity {order_quantity} exceeds max {max_contracts}",
                )
                return None

        # --- Resolve session ---
        sid = session_id
        if sid is None and deployment_id is not None:
            sid = self.control_center.find_session_for_deployment(deployment_id)
        if sid is None:
            self._record_event(
                AutonomousEventType.ERROR,
                symbol=instrument.underlying or decision.opportunity_symbol,
                message="option execution skipped: no active session/deployment for broker",
            )
            return None

        # Pre-compute values needed for idempotency replay and OrderIntent.
        action = decision.action
        side = Side.BUY if str(action).lower() == "buy" else Side.SELL

        # --- Feed premium to PaperBroker BEFORE order submission ---
        # This sets _last_price so the broker has a current market price for
        # the exact option symbol, enabling correct mark-to-market later.
        self.control_center.update_deployment_market_price(
            deployment_id=deployment_id or "",
            symbol=instrument.key,
            price=quote.ltp,
        )

        # --- Construct OrderIntent with the fetched premium (no manual injection) ---
        intent = OrderIntent(
            symbol=instrument.key,
            side=side,
            quantity=order_quantity,
            order_type=OrderType.MARKET,
            current_price=quote.ltp,
            client_order_id=client_order_id,
            options_contract_id=instrument.contract_id,
            strike=instrument.strike,
            expiry=instrument.expiry,
            option_type=option_type,
            contract_size=getattr(instrument, "lot_size", None),
        )

        # --- Phase 7: idempotency guard ---
        dep_key = self._safety_layer.idempotency.deployment_key(
            bot_id=self.config.bot_id,
            symbol=decision.opportunity_symbol,
            strategy_id=decision.selected_configuration.strategy_id if decision.selected_configuration else "unknown",
            timeframe=decision.selected_configuration.timeframe if decision.selected_configuration else "1d",
            options_contract_id=instrument.contract_id,
            action=action_for_id,
        )
        if self._safety_layer.idempotency.is_duplicate_deployment(dep_key):
            # Return cached result from the session store if available.
            if client_order_id is not None and sid is not None:
                try:
                    cached = self.control_center.session_store.get_order(sid, client_order_id)
                    if cached is not None:
                        import json
                        result_data = json.loads(cached.result_json or "{}")
                        from ..paper.control import OrderResult, OrderStatus
                        return OrderResult(
                            order_id=result_data.get("order_id", cached.order_id),
                            client_order_id=result_data.get("client_order_id", cached.client_order_id),
                            symbol=result_data.get("symbol", instrument.key),
                            side=result_data.get("side", side.value),
                            quantity=result_data.get("quantity", order_quantity),
                            order_type=result_data.get("order_type", OrderType.MARKET.value),
                            limit_price=result_data.get("limit_price"),
                            status=result_data.get("status", OrderStatus.FILLED.value),
                            filled_quantity=result_data.get("filled_quantity", 0.0),
                            avg_fill_price=result_data.get("avg_fill_price", 0.0),
                            fills=[],
                            cash_after=result_data.get("cash_after"),
                            equity_after=result_data.get("equity_after"),
                            realized_pnl_after=result_data.get("realized_pnl_after"),
                            unrealized_pnl_after=result_data.get("unrealized_pnl_after"),
                            position_qty_after=result_data.get("position_qty_after"),
                            reject_reason=result_data.get("reject_reason", ""),
                            is_idempotent_replay=True,
                            options_contract_id=result_data.get("options_contract_id"),
                            strike=result_data.get("strike"),
                            expiry=result_data.get("expiry"),
                            option_type=result_data.get("option_type"),
                        )
                except Exception:  # noqa: BLE001
                    pass
            self._record_event(
                AutonomousEventType.DECISION_REJECTED,
                symbol=instrument.underlying or decision.opportunity_symbol,
                message=f"duplicate deployment key for {instrument.contract_id}",
            )
            return None
        self._safety_layer.idempotency.mark_deployment(dep_key, sid)

        # --- Submit through the control center → PaperBroker ---
        try:
            result = self.control_center.submit_order_intent(
                session_id=sid,
                intent=intent,
            )
        except ControlCenterError as exc:
            self._record_event(
                AutonomousEventType.ERROR,
                symbol=instrument.underlying or decision.opportunity_symbol,
                message=f"order submission rejected: {exc}",
            )
            return None
        except Exception as exc:
            self._record_event(
                AutonomousEventType.ERROR,
                symbol=instrument.underlying or decision.opportunity_symbol,
                message=f"order submission error: {exc}",
            )
            return None

        self._record_event(
            AutonomousEventType.DEPLOYMENT_CREATED,
            symbol=instrument.underlying or decision.opportunity_symbol,
            message=f"option order filled at premium {quote.ltp} for {instrument.contract_id}",
            payload={
                "order_id": result.order_id,
                "symbol": result.symbol,
                "avg_fill_price": result.avg_fill_price,
                "filled_quantity": result.filled_quantity,
                "options_contract_id": result.options_contract_id,
                "strike": result.strike,
                "expiry": result.expiry,
                "option_type": result.option_type,
                "quote_timestamp": quote.timestamp.isoformat(),
                "quote_fetched_at": quote.fetched_at.isoformat(),
                "quote_age_seconds": round(quote.age_seconds, 3),
            },
        )

        self.config.decision_count += 1
        return result

    # ------------------------------------------------------------------ #
    # Inspect bot state
    # ------------------------------------------------------------------ #

    def inspect(self) -> dict[str, Any]:
        """Return a snapshot of the bot's current state for dashboard/UI."""
        events = self._event_log.events
        last_event = events[-1] if events else None
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
            "event_count": len(events),
            "last_event_type": last_event.event_type.value if last_event else None,
            "last_event_timestamp": last_event.timestamp if last_event else None,
            "source": self.config.source.value,
            "allowed_symbols": sorted(self.config.user_constraints.allowed_symbols),
            "allowed_strategy_ids": sorted(self.config.user_constraints.allowed_strategy_ids),
            "allowed_timeframes": sorted(self.config.user_constraints.allowed_timeframes),
            "max_simultaneous_positions": self.config.max_simultaneous_positions,
            "max_position_allocation_pct": self.config.max_position_allocation_pct,
            "max_exposure_pct": self.config.max_exposure_pct,
            "max_drawdown_pct": self.config.user_constraints.max_drawdown_pct,
            "safety": {
                "kill_switch_state": self._safety_layer.kill_switch.state.value,
                "kill_switch_reason": self._safety_layer.kill_switch.reason,
                "kill_switch_halted_at": self._safety_layer.kill_switch.halt_timestamp,
            },
        }
