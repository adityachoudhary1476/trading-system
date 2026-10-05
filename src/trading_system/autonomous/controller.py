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
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TAKE_PROFIT_PCT,
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
# Action -> broker side resolution
# --------------------------------------------------------------------------- #

# EXIT is a flatten, so it resolves to a sell-to-close. Phase 23 never opens
# naked option shorts, so there is no long-to-short reversal to encode here.
# HOLD and anything unrecognised are deliberately absent: they must fail
# closed rather than be coerced into a sell.
_OPTION_ACTION_TO_SIDE: dict[str, "Side"] = {
    "buy": Side.BUY,
    "sell": Side.SELL,
    "exit": Side.SELL,
}


def _resolve_option_order_side(
    *, explicit_side: object = None, action: object = None
) -> Optional[Side]:
    """Map a declared action onto a broker ``Side``.

    ``explicit_side`` wins when supplied. Returns ``None`` when the value is
    not an executable action, so callers fail closed instead of defaulting
    to a sell.
    """
    raw = explicit_side if explicit_side is not None else action
    if raw is None:
        return None
    key = str(getattr(raw, "value", raw)).strip().lower()
    return _OPTION_ACTION_TO_SIDE.get(key)


# Underlying names recognised in Indian index option symbols. Ordered
# longest-first so BANKNIFTY is never swallowed by the NIFTY prefix.
_OPTION_UNDERLYING_NAMES = (
    "BANKNIFTY",
    "FINNIFTY",
    "MIDCPNIFTY",
    "SENSEX",
    "NIFTY",
)


def _underlying_name(raw: object) -> str:
    """Normalise a symbol / contract id to its underlying name (``"NIFTY"``).

    Accepts decision symbols (``NSE:NIFTY``), canonical contract ids
    (``NFO:NIFTY|2026-10-06|22800|CE``) and provider symbols
    (``NFO:NIFTY26OCT22800CE``). Trailing digits are dropped through the
    prefix match, so ``NIFTY50`` and ``NIFTY`` compare equal -- the same
    normalisation ``AutonomousPortfolio._underlying_symbol`` uses.

    Returns ``""`` when nothing recognised is present. Callers must treat
    ``""`` as "cannot identify" rather than as a match, or two unrelated
    symbols would compare equal as empty strings.
    """
    text = str(raw or "").strip().upper()
    if not text:
        return ""
    head = text.split("|", 1)[0]
    if ":" in head:
        head = head.split(":", 1)[1]
    for name in _OPTION_UNDERLYING_NAMES:
        if head.startswith(name):
            return name
    return ""


def _contract_identity_payload(position: object) -> dict:
    """The four fields that identify an option contract, plus held quantity.

    Used as the payload of every ``EXIT_REJECTED`` / ``SELL_*`` event so an
    operator can see *which* contract a refusal or fill was about without
    having to reconstruct it from the underlying's spot price.
    """
    return {
        "options_contract_id": getattr(position, "options_contract_id", None),
        "strike": getattr(position, "strike", None),
        "expiry": getattr(position, "expiry", None),
        "option_type": getattr(position, "option_type", None),
        "held_qty": getattr(position, "qty", None),
    }


# --------------------------------------------------------------------------- #
# AutonomousController — the primary orchestration service.
# This is the "glue" that connects the autonomous layer to the existing
# deployment/paper-trading engine. It does NOT contain any trading logic,
# market-scanning intelligence, or strategy-selection algorithms.
# It merely orchestrates the existing engine based on structured decisions.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Event types that explain a refused execution attempt. Used only for
# introspection (see AutonomousController.rejection_reason_since); every
# rejection site in execute_option_order already records one of these.
# --------------------------------------------------------------------------- #
_EXECUTION_REJECTION_EVENT_TYPES = frozenset(
    {
        AutonomousEventType.ERROR,
        AutonomousEventType.POLICY_VIOLATION,
        AutonomousEventType.DECISION_REJECTED,
    }
)

# How recently a position must have been opened for its entry delta to be
# captured as the anchor. Generous enough to cover a normal mark-to-market
# interval, tight enough that a delta measured a day into a position is never
# mistaken for the exposure it was opened with.
_GREEKS_ANCHOR_WINDOW_SECONDS = 1800.0


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
    _portfolio: Optional[Any] = None

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    def __init__(self, *, config: AutonomousBotConfig, control_center: PaperTradingControlCenter, persistence: Optional[Any] = None, phase23_registry: Optional[Any] = None) -> None:
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
        self._phase23_registry = phase23_registry

    @property
    def event_log(self) -> AutonomousEventLog:
        """Return the in-memory autonomous event log for this bot."""
        return self._event_log

    @property
    def portfolio(self) -> Any:
        """The single autonomous paper portfolio for this bot (V1).

        Lazily constructed so existing entry points (API, scheduler, tests)
        keep working unchanged. The portfolio reuses this controller's kill
        switch, option execution path and event log; ``persistence`` is shared
        so the API process can read the portfolio snapshot the scheduler
        publishes.
        """
        if self._portfolio is None:
            from .portfolio import AutonomousPortfolio

            self._portfolio = AutonomousPortfolio(
                self,
                persistence=getattr(self, "_persistence", None),
            )
        return self._portfolio

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
    # Execution-rejection introspection
    # ------------------------------------------------------------------ #

    def execution_attempt_mark(self) -> int:
        """Snapshot the event log so a later call can explain one execution attempt.

        ``execute_option_order`` signals every rejection with ``return None``,
        so a caller has no way to tell *why* it refused. Each rejection already
        records a descriptive event, but the log is cumulative: reading "the
        last rejection" after the fact can surface a stale message from an
        earlier tick. Take this mark before the attempt and pass it to
        :meth:`rejection_reason_since` to read only what that attempt recorded.
        """
        return len(self._event_log.events)

    def rejection_reason_since(self, mark: int) -> str:
        """Why the execution attempt after ``mark`` was refused, or ``''``.

        Returns the message of the most recent rejection event recorded by that
        attempt. ``''`` means the attempt recorded no rejection event, so the
        caller should not claim a cause it cannot evidence.
        """
        events = self._event_log.events
        for event in reversed(events[max(0, int(mark)):]):
            if (
                event.event_type in _EXECUTION_REJECTION_EVENT_TYPES
                and event.message
            ):
                return event.message
        return ""

    # ------------------------------------------------------------------ #
    # Lifecycle transitions
    # ------------------------------------------------------------------ #

    def _ensure_autonomous_deployments(self) -> None:
        """Auto-create paper deployments from PAPER_APPROVED and PAPER_EXPERIMENTAL strategies if none exist."""
        if self._phase23_registry is None:
            self._record_event(
                AutonomousEventType.ERROR,
                message="Phase23 registry not available; cannot discover PAPER_APPROVED/PAPER_EXPERIMENTAL strategies",
            )
            return
        try:
            from trading_system.research.phase23.discovery import Phase23Discovery
            discovery = Phase23Discovery(self._phase23_registry)
            approved = discovery.discover(max_candidates=50, include_experimental=True)
            if not approved:
                self._record_event(
                    AutonomousEventType.ERROR,
                    message="no PAPER_APPROVED/PAPER_EXPERIMENTAL strategies found in registry; tournament results may not be persisted to production",
                )
                return
            existing = [
                d for d in self.control_center.list_deployments()
                if d.notes and d.notes.startswith("bot:") and d.notes.split(":", 1)[1] == self.config.bot_id
            ]
            if existing:
                return
            for item in approved:
                try:
                    candidate = None
                    if hasattr(item, 'candidate_id'):
                        from trading_system.research.phase23.strategies import get_strategy_candidate
                        candidate = get_strategy_candidate(item.candidate_id)
                    if candidate is None:
                        self._record_event(
                            AutonomousEventType.ERROR,
                            symbol=getattr(item, 'symbol', ''),
                            message=f"candidate {getattr(item, 'candidate_id', 'unknown')} not found in universe",
                        )
                        continue
                    spec_dict = candidate.spec_builder(item.symbol, item.timeframe)
                    from trading_system.research.strategy_lab.spec import StrategySpec
                    spec = StrategySpec.model_validate(spec_dict)
                    from trading_system.paper.deployment import PaperDeploymentConfig
                    dep_config = PaperDeploymentConfig(
                        # Without these the deployment has no per-position
                        # exit at all — a position could only ever be closed
                        # by a strategy signal.
                        stop_loss_pct=DEFAULT_STOP_LOSS_PCT,
                        take_profit_pct=DEFAULT_TAKE_PROFIT_PCT,
                    )
                    result, deployment = self.create_autonomous_deployment(
                        symbol=item.symbol,
                        strategy_id=item.strategy_id,
                        timeframe=item.timeframe,
                        strategy_spec=spec,
                        deployment_config=dep_config,
                    )
                    if result != DeploymentCreationResult.SUCCESS or deployment is None:
                        self._record_event(
                            AutonomousEventType.ERROR,
                            symbol=item.symbol,
                            message=f"deployment creation failed for {item.strategy_id}: {result.value}",
                        )
                except Exception as exc:
                    self._record_event(
                        AutonomousEventType.ERROR,
                        symbol=getattr(item, 'symbol', ''),
                        message=f"deployment creation exception for {getattr(item, 'strategy_id', 'unknown')}: {exc}",
                    )
                    continue
        except Exception as exc:
            self._record_event(
                AutonomousEventType.ERROR,
                message=f"PAPER_APPROVED/PAPER_EXPERIMENTAL discovery failed: {exc}",
            )

    def start_bot(self) -> Tuple[bool, str]:
        """Start the autonomous bot.

        Transition graph: CREATED -> STARTING -> RUNNING
        Also supports restart from STOPPED: STOPPED -> STARTING -> RUNNING

        Startup validates:
          1. Canonical paper deployment exists and belongs to this bot
          2. Deployment is valid/healthy (ACTIVE status)
          3. Kill switch is not latched for an unresolved genuine safety/deployment failure

        Returns (success, message). On failure, the bot remains in its
        current state and the error message explains why.
        """
        try:
            # Recovery path: ERROR -> STOPPED -> STARTING
            if self.lifecycle.state == AutonomousBotState.ERROR:
                if not self.lifecycle.can_transition_to(AutonomousBotState.STOPPED):
                    return False, f"bot cannot recover from {self.lifecycle.state.value} to STOPPED"
                self.lifecycle.transition_to(AutonomousBotState.STOPPED)
                self.config.state = AutonomousBotState.STOPPED

            # Transition CREATED/STOPPED -> STARTING
            if not self.lifecycle.can_transition_to(AutonomousBotState.STARTING):
                return False, f"bot cannot transition from {self.lifecycle.state.value} to STARTING"

            self.lifecycle.transition_to(AutonomousBotState.STARTING)
            self.config.state = AutonomousBotState.STARTING

            # --- Auto-provision deployments from PAPER_APPROVED strategies ---
            self._ensure_autonomous_deployments()

            # --- Provision the autonomous portfolio account (V1) ---
            # The portfolio is the primary autonomous experience; ensure its
            # deployment/session exist so the RUNNING bot has an account.
            try:
                self.portfolio.on_start()
            except Exception:  # noqa: BLE001 — never block bot start on the read model
                pass

            # --- Startup validation ---
            # 1. Verify canonical paper deployment exists and belongs to this bot
            bot_deployments = [
                d for d in self.control_center.list_deployments()
                if d.notes and d.notes.startswith("bot:") and d.notes.split(":", 1)[1] == self.config.bot_id
            ]
            if not bot_deployments:
                self.lifecycle.transition_to(AutonomousBotState.ERROR)
                self.config.state = AutonomousBotState.ERROR
                self._record_event(
                    AutonomousEventType.ERROR,
                    message=f"no paper deployment found for bot {self.config.bot_id}",
                )
                return False, f"no paper deployment linked to bot {self.config.bot_id}"

            # 2. Verify at least one deployment is ACTIVE and healthy
            active_deployments = [d for d in bot_deployments if d.status == PaperDeploymentStatus.ACTIVE]
            if not active_deployments:
                self.lifecycle.transition_to(AutonomousBotState.ERROR)
                self.config.state = AutonomousBotState.ERROR
                self._record_event(
                    AutonomousEventType.ERROR,
                    message=f"no ACTIVE paper deployment for bot {self.config.bot_id}",
                )
                return False, f"no ACTIVE paper deployment for bot {self.config.bot_id}"

            # 3. Kill-switch gate: only resume if halt was intentional (NORMAL_STOP/MANUAL)
            ks = self._safety_layer.kill_switch
            if ks.is_halted:
                if ks.reason in (KillSwitchReason.NORMAL_STOP, KillSwitchReason.MANUAL):
                    # Intentional operator stop - safe to resume
                    ks.resume()
                else:
                    # Genuine safety/risk/deployment failure - remain fail-closed
                    self.lifecycle.transition_to(AutonomousBotState.ERROR)
                    self.config.state = AutonomousBotState.ERROR
                    self._record_event(
                        AutonomousEventType.ERROR,
                        message=f"start blocked: kill switch halted (reason={ks.reason.value})",
                    )
                    return False, f"kill switch halted (reason={ks.reason.value}); manual resume required"

            # Transition STARTING -> RUNNING
            if not self.lifecycle.can_transition_to(AutonomousBotState.RUNNING):
                self.lifecycle.transition_to(AutonomousBotState.ERROR)
                self.config.state = AutonomousBotState.ERROR
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

            if self.lifecycle.state == AutonomousBotState.ERROR:
                self.lifecycle.transition_to(AutonomousBotState.STOPPED)
                self.config.state = AutonomousBotState.STOPPED
                self.config.enabled = False
                self._safety_layer.kill_switch.halt(
                    KillSwitchReason.NORMAL_STOP,
                    detail="bot recovered from error by operator",
                )
                self._record_event(AutonomousEventType.BOT_STOPPED)
                self._persist()
                return True, f"bot recovered from error and stopped; state={self.lifecycle.state.value}"

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

            # Phase 7: Halt the kill switch on normal stop.
            # Use NORMAL_STOP so that a subsequent start_bot() can safely resume.
            # Genuine DEPLOYMENT_ERROR / risk halts are set elsewhere and must
            # remain fail-closed until explicitly cleared.
            self._safety_layer.kill_switch.halt(
                KillSwitchReason.NORMAL_STOP,
                detail="bot stopped by operator",
            )

            # Publish the STOPPED portfolio read model (best effort).
            try:
                self.portfolio.on_stop()
            except Exception:  # noqa: BLE001 — never block bot stop on the read model
                pass

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
            elif self.lifecycle.state == AutonomousBotState.ERROR:
                if not self.lifecycle.can_transition_to(AutonomousBotState.STOPPED):
                    return False, f"bot cannot recover from {self.lifecycle.state.value} to STOPPED"
                self.lifecycle.transition_to(AutonomousBotState.STOPPED)
                self.config.state = AutonomousBotState.STOPPED
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

    def scan_market(self, timeframe: Optional[str] = None) -> MarketScanResult:
        """Run a deterministic market scan over the bot's constrained universe.

        Builds a :class:`ScannerConfig` from the bot's ``user_constraints``
        and the control center's ``load_market_data`` callable, then delegates
        to :class:`MarketScanner`. The scan produces candidate discovery only —
        it does NOT select strategies, generate signals, place orders, or
        create deployments.

        Fails closed: if no market-data provider is configured on the control
        center, every symbol is rejected as ``MISSING_MARKET_DATA``.

        ``timeframe`` selects the bar interval to scan and defaults to ``1d``,
        which is what every caller relied on before this parameter existed. The
        scheduler passes the timeframe it has already proved has fresh data (see
        ``_run_one_tick``), so a bot configured for intraday bars is not forced
        through a daily scan.
        """
        scan_timeframe = timeframe or "1d"
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
                timeframe=scan_timeframe,
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
            timeframe=scan_timeframe,
            data_provider=self.control_center.load_market_data,
            data_provider_source="control_center.load_market_data",
            # Daily bars from Upstox are stamped at 00:00 IST (18:30 UTC) —
            # midnight, never inside the 09:15-15:30 IST regular session. The
            # default per-bar session gate therefore rejects EVERY symbol on
            # every tick and the bot can never trade. Daily-bar freshness is
            # already enforced by ``max_freshness``; the live-session gate is
            # handled by the scheduler tick itself (``_is_regular_session``),
            # so it is redundant (and fatal) here.
            require_regular_session=False,
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

    def _registered_factory_strategy_ids(self) -> list[str]:
        """Factory ids for every strategy persisted in the research registry.

        Compatibility must only consider strategies the tick can actually
        execute. ``_execute_one_decision`` resolves a decision's
        ``strategy_id`` via ``_lookup_strategy_spec``, which can only find
        specs held in the research registry. Builtin factory strategies
        (``rsi_mean_reversion``, ``ema_crossover``, ...) sit in the discovery
        catalog but NOT in the registry, so selecting one resolves to nothing,
        the tick answers ``unknown_strategy``, and the bot silently places no
        orders while still reporting a healthy tick.

        Constraining the evaluator to the registered set makes that outcome
        unrepresentable: with an empty registry this returns ``[]``, which the
        evaluator honours, so the tick produces no decisions rather than a
        decision that can never execute.
        """
        from trading_system.autonomous.spec_register import factory_id_for_db_id

        try:
            strategies = self.control_center.registry.list_strategies()
        except Exception:  # noqa: BLE001 — fail closed to "nothing tradeable"
            return []
        return [
            factory_id_for_db_id(s.strategy_id) for s in strategies if s.strategy_id
        ]

    def evaluate_strategy_compatibility(
        self,
        ranking_result: OpportunityRankingResult,
        config: Optional[CompatibilityConfig] = None,
    ) -> CompatibilityResult:
        """Evaluate which registered strategies are compatible with ranked opportunities.

        Delegates to :class:`StrategyCompatibilityEvaluator` using the operator's
        ``allowed_strategy_ids`` constraint.  Does NOT execute strategies, generate
        signals, place orders, create deployments, or perform live trading.

        The candidate set is restricted to strategies persisted in the research
        registry (see ``_registered_factory_strategy_ids``). Leaving it open to
        the whole discovery catalog lets the evaluator select a builtin that the
        execution path cannot resolve, which presents as a permanently flat
        paper account with no error anywhere.

        Snapshot consistency:
        The evaluator consumes the Phase 3 :class:`OpportunityRankingResult`
        directly — no new market-data fetches are performed.  All market-condition
        features come from the Phase 2 snapshot embedded in the ranking.
        """
        allowed_timeframes = self.config.user_constraints.allowed_timeframes
        compat_config = config or CompatibilityConfig(
            enabled=self.config.enabled,
            timeframes=tuple(allowed_timeframes) if allowed_timeframes else ("5m", "15m", "1h", "1d"),
        )
        evaluator = StrategyCompatibilityEvaluator(compat_config)
        allowed = self.config.user_constraints.allowed_strategy_ids
        return evaluator.evaluate(
            ranking_result,
            allowed_strategies=allowed,
            discovered_strategies=self._registered_factory_strategy_ids(),
        )

    # ------------------------------------------------------------------ #
    # Phase 5 -- Strategy Selection & Signal Generation
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Live position context for strategy evaluation
    # ------------------------------------------------------------------ #
    def _position_state_for(self, symbol: str) -> Optional[Any]:
        """Snapshot of what this bot already holds for ``symbol``.

        Returns a ``PositionState`` (``side``: +1 long / -1 short) or ``None``
        when the bot is flat for that symbol.

        Why this exists: an option position is keyed in the paper book by its
        *contract* symbol (``NFO:NIFTY26OCT22800CE``), never by the underlying
        the decision is made on (``NSE:NIFTY``). A plain
        ``broker.get_position(symbol)`` therefore always answers "flat" while
        an option is open, so the strategy is told it holds nothing, its
        transition table can never reach EXIT, and the option could only ever
        be closed by a price threshold rather than by the strategy that opened
        it. Option positions are matched on the underlying resolved from the
        canonical contract id, so the exit decision sees the position the book
        actually holds.

        Fail-closed: any lookup failure reads as flat. A position is never
        inferred, reconstructed from spot, or synthesised.
        """
        from ..strategy_factory.contract import PositionState

        if not symbol:
            return None
        try:
            deployments = self.control_center.list_deployments()
        except Exception:  # noqa: BLE001 — fail closed to "flat"
            return None

        wanted = _underlying_name(symbol)
        equity: Optional[Any] = None
        option: Optional[Any] = None
        for dep in deployments:
            try:
                sid = self.control_center.find_session_for_deployment(dep.deployment_id)
                if sid is None:
                    continue
                runner = self.control_center.get_runner(sid)
                if runner is None:
                    continue
                positions = list(runner.broker.positions().values())
            except Exception:  # noqa: BLE001 — one bad book must not hide the rest
                continue
            for pos in positions:
                try:
                    qty = float(getattr(pos, "qty", 0.0) or 0.0)
                except Exception:  # noqa: BLE001
                    continue
                if qty == 0.0:
                    continue
                if not getattr(pos, "is_option", False):
                    if str(getattr(pos, "symbol", "")).upper() == symbol.upper():
                        equity = pos
                        break
                elif option is None and wanted:
                    pos_raw = getattr(pos, "options_contract_id", None) or getattr(
                        pos, "symbol", None
                    )
                    if _underlying_name(pos_raw) == wanted:
                        option = pos
            if equity is not None:
                break

        chosen = equity if equity is not None else option
        if chosen is None:
            return None
        qty = float(chosen.qty)
        if qty == 0.0:
            return None
        entry = getattr(chosen, "avg_entry_price", 0.0) or 0.0
        return PositionState(
            symbol=str(getattr(chosen, "symbol", "")),
            side=1 if qty > 0 else -1,
            size=abs(qty),
            entry_price=float(entry) if entry else None,
        )

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
            # Feed the live book into the strategy so an open position is
            # visible at evaluation time; without it the transition table can
            # only ever produce another entry signal for a position it holds.
            position_provider=self._position_state_for,
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
        # premiums — these must never silently enter real paper execution.
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
                return None  # HOLD or EXIT — no new option contract

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

    def _instrument_for_position(self, position, underlying: str):
        """Resolve the quote-ready ``Instrument`` for an open option position.

        The BUY path discovered this instrument from the instrument
        repository, so its ``InternalSymbol.key`` (the broker position symbol)
        and ``provider_symbol`` (the Upstox/NSE wire token) are the only values
        that the short-selling guard in ``submit_order_intent`` and the quote
        provider resolve correctly. Reconstructing from raw position metadata
        (expiry as YYYYMMDD vs. the provider's YYMMMDD token, provider_symbol as
        the canonical contract_id pipe-string vs. the wire key) produced a key
        that never matched the stored position and a provider_symbol the Upstox
        API rejected -- both caused exits to be rejected in production.

        Returns ``None`` if the position lacks the contract metadata needed to
        identify a contract at all.
        """
        pos_contract_id = getattr(position, "options_contract_id", None)
        pos_strike = getattr(position, "strike", None)
        pos_expiry = getattr(position, "expiry", None)
        pos_option_type = getattr(position, "option_type", None)
        pos_contract_size = getattr(position, "contract_size", 1)
        if not (pos_contract_id and pos_strike and pos_expiry and pos_option_type):
            return None

        from trading_system.india.instruments import (
            Instrument,
            InstrumentType,
            InternalSymbol,
        )

        itype = InstrumentType.OPTION_CE if pos_option_type == "CE" else InstrumentType.OPTION_PE
        pos_symbol = getattr(position, "symbol", None)
        repo_instr = None
        if self._instrument_repo is not None and pos_symbol:
            try:
                repo_instr = self._instrument_repo.registry.get(
                    InternalSymbol.parse(pos_symbol)
                )
            except Exception:
                repo_instr = None
        if repo_instr is None and self._instrument_repo is not None:
            repo_instr = getattr(
                self._instrument_repo, "_derivatives", {}
            ).get(pos_contract_id)
        if repo_instr is None and self._instrument_repo is not None:
            repo_instr = self._instrument_repo.find_contract(
                underlying=underlying, expiry=pos_expiry,
                option_type=pos_option_type, strike=pos_strike,
            )

        if repo_instr is not None:
            instrument = repo_instr
            # Stamping lot_size from the position (repository may use a
            # different default; the filled contract lot must win).
            if pos_contract_size:
                instrument.lot_size = int(pos_contract_size)
            return instrument

        # Fallback when the instrument is not in the repository: reconstruct
        # from position metadata but use the position's broker symbol for BOTH
        # the InternalSymbol key and the provider_symbol, so they at least stay
        # consistent with the BUY-side position.
        if pos_symbol and ":" in pos_symbol:
            exch, sym = pos_symbol.split(":", 1)
        else:
            exch, sym = "NFO", pos_symbol or (
                f"{underlying}{pos_expiry.replace('-', '')}"
                f"{int(pos_strike)}{pos_option_type}"
            )
        instrument = Instrument(
            internal=InternalSymbol(exchange=exch.upper(), symbol=sym),
            instrument_type=itype,
            name=f"{underlying} {pos_option_type} {pos_strike} {pos_expiry}",
            provider_symbol=pos_symbol,
        )
        instrument.underlying = underlying
        instrument.expiry = pos_expiry
        instrument.strike = float(pos_strike)
        instrument.option_type = pos_option_type
        instrument.lot_size = int(pos_contract_size) if pos_contract_size else 1
        return instrument

    def _capture_position_greeks(
        self,
        pos: Any,
        quote: Any,
        *,
        ltp: float,
        spot_price: Optional[float],
    ) -> None:
        """Record greeks for a held option from a quote already in hand.

        Best-effort by design: greeks enrich the risk picture, they are not a
        safety gate, so a position whose greeks cannot be computed is left
        untouched rather than being made to fail some unrelated check. The
        delta-decay exit reads ``None`` as "cannot evaluate" and declines to
        fire.

        The entry anchor is only taken while the position is still fresh. An
        anchor captured later would be measured against today's market and the
        decay test would silently compare a delta with itself, always reporting
        no decay. Refusing to anchor late keeps that failure mode unreachable
        rather than merely unlikely.
        """
        from datetime import datetime as _dt, timezone as _tz

        from .greeks import greeks_from_market_inputs

        spot = getattr(quote, "underlying_price", None)
        if spot is None or not (float(spot) > 0.0):
            spot = spot_price
        if spot is None:
            return

        greeks = greeks_from_market_inputs(
            spot=spot,
            strike=getattr(pos, "strike", None),
            expiry=getattr(pos, "expiry", None),
            option_type=getattr(pos, "option_type", None),
            ltp=ltp,
            quote_iv=getattr(quote, "implied_vol", None),
        )
        if greeks is None:
            return

        try:
            if getattr(pos, "entry_delta", None) is None:
                age = pos.holding_seconds() if hasattr(pos, "holding_seconds") else None
                if age is not None and age <= _GREEKS_ANCHOR_WINDOW_SECONDS:
                    pos.entry_delta = greeks.delta
                    pos.entry_iv = greeks.implied_vol
            pos.last_delta = greeks.delta
            pos.last_iv = greeks.implied_vol
            pos.greeks_as_of = _dt.now(_tz.utc).isoformat()
        except AttributeError:
            # A position stand-in that does not accept the greeks attributes.
            # Observability must never be the reason a mark fails.
            return

    def mark_option_positions_to_market(
        self,
        *,
        deployment_id: str,
        underlying: str,
        positions,
        max_age_seconds: float = 300.0,
        spot_price: Optional[float] = None,
    ) -> int:
        """Refresh the broker mark for each open option position from live quotes.

        A position's ``current_price`` is otherwise only written as a side effect
        of submitting an order, so on any tick where the strategy emits no
        signal the stored mark stays at the entry premium, unrealized P&L reads
        0%, and a stop-loss or take-profit can never be reached. Risk limits are
        only as good as the mark they are measured against, so the mark is
        refreshed before any threshold is evaluated.

        Greeks are captured in the same pass, from the same quote, so the
        delta-decay exit has a current reading to compare against the entry
        anchor. ``spot_price`` is optional and used only when the quote payload
        omits the underlying price; supplying it avoids a case where greeks go
        unknown purely because of which fields Upstox happened to include.

        Fail-closed on pricing: a missing, stale or unparseable quote leaves the
        existing mark untouched rather than guessing a price. Returns the number
        of positions successfully re-marked. Never raises.
        """
        if self._quote_provider is None:
            return 0
        marked = 0
        for pos in positions or ():
            try:
                instrument = self._instrument_for_position(pos, underlying)
                if instrument is None:
                    continue
                quote = self._quote_provider.get_quote(instrument)
                if quote is None:
                    continue
                if not self._quote_provider.is_fresh(
                    quote, max_age_seconds=max_age_seconds
                ):
                    self._record_event(
                        AutonomousEventType.ERROR,
                        symbol=underlying,
                        message=(
                            f"mark-to-market quote stale: age={quote.age_seconds:.0f}s"
                            f" for {getattr(pos, 'options_contract_id', None)}"
                        ),
                    )
                    continue
                ltp = float(getattr(quote, "ltp", 0.0) or 0.0)
                if ltp <= 0:
                    continue
                if self.control_center.update_deployment_market_price(
                    deployment_id, pos.symbol, ltp
                ):
                    marked += 1
                    self._capture_position_greeks(
                        pos, quote, ltp=ltp, spot_price=spot_price
                    )
            except Exception as exc:  # noqa: BLE001
                # A bad quote for one contract must not stop the others, and must
                # never abort the tick: the stale mark simply fails to trigger.
                # Logged rather than swallowed, because a silent failure here
                # means a risk limit that silently never fires.
                self._record_event(
                    AutonomousEventType.ERROR,
                    symbol=underlying,
                    message=(
                        f"mark-to-market failed for"
                        f" {getattr(pos, 'options_contract_id', None)}: {exc}"
                    ),
                )
                continue
        return marked

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
        is_exit = existing_position is not None

        def _exit_reject(message: str) -> Optional[OrderResult]:
            """Refuse an exit before it reaches the broker, and say why.

            Every refusal site here returns ``None``; for an *exit* that bare
            ``None`` was the only observable, so a position that could not be
            sold was indistinguishable from one that was never attempted.
            ``EXIT_REJECTED`` is the fail-closed, diagnosable counterpart: an
            undeterminable contract identity, quantity or LTP must never
            become a fabricated fill.
            """
            if is_exit:
                self._record_event(
                    AutonomousEventType.EXIT_REJECTED,
                    symbol=getattr(decision, "opportunity_symbol", "") or "",
                    message=message,
                    payload=_contract_identity_payload(existing_position),
                )
            return None

        if self._safety_layer.kill_switch.is_halted:
            self._record_event(
                AutonomousEventType.ERROR,
                message=f"option execution blocked by kill switch (reason={self._safety_layer.kill_switch.reason})",
            )
            return _exit_reject("exit blocked: kill switch halted")

        # --- Resolve the broker side up-front, before any quote fetch ---
        # Rejecting a non-executable action here means a HOLD/typo can never be
        # coerced into a fabricated sell, and it saves a wasted premium fetch.
        side = _resolve_option_order_side(
            explicit_side=explicit_side,
            action=getattr(decision, "action", None),
        )
        if side is None:
            self._record_event(
                AutonomousEventType.ERROR,
                symbol=decision.opportunity_symbol,
                message=(
                    "option execution skipped: non-executable action "
                    f"explicit_side={explicit_side!r} action={getattr(decision, 'action', None)!r}"
                ),
            )
            return _exit_reject(
                "exit rejected: non-executable action "
                f"explicit_side={explicit_side!r} action={getattr(decision, 'action', None)!r}"
            )

        if is_exit:
            # The exit signal exists and resolves to a sell-to-close. Recorded
            # before any quote/identity work so a refusal later is visible as
            # "signal generated -> rejected" rather than as silence.
            self._record_event(
                AutonomousEventType.EXIT_SIGNAL_GENERATED,
                symbol=getattr(decision, "opportunity_symbol", "") or "",
                message="exit signal generated for open option position",
                payload={
                    **_contract_identity_payload(existing_position),
                    "side": side.value,
                },
            )
            # --- Quantity must be determinable and must never over-sell ---
            # An over-sized sell-to-close would drive the book through flat
            # into a short, which Phase 23 forbids; refusing is the fail-closed
            # answer.
            try:
                held_qty = float(getattr(existing_position, "qty", 0.0) or 0.0)
            except (TypeError, ValueError):
                held_qty = None
            if held_qty is None:
                return _exit_reject(
                    "exit rejected: existing position quantity is not numeric"
                )
            if held_qty == 0.0:
                return _exit_reject(
                    "exit rejected: existing position is already flat"
                )
            if float(order_quantity) <= 0.0:
                return _exit_reject(
                    f"exit rejected: order quantity {order_quantity!r} is not positive"
                )
            if float(order_quantity) > abs(held_qty):
                return _exit_reject(
                    "exit rejected: requested quantity "
                    f"{float(order_quantity)} exceeds held quantity {abs(held_qty)}"
                )

        if self._quote_provider is None:
            self._record_event(
                AutonomousEventType.ERROR,
                symbol=decision.opportunity_symbol,
                message="option execution skipped: no quote provider attached",
            )
            return _exit_reject("exit rejected: no quote provider attached (LTP unknown)")

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
                instrument = self._instrument_for_position(existing_position, underlying)
                if instrument is None:
                    self._record_event(
                        AutonomousEventType.ERROR,
                        symbol=decision.opportunity_symbol,
                        message="exit rejected: could not resolve instrument for existing position",
                    )
                    return _exit_reject(
                        "exit rejected: could not resolve instrument for existing position"
                    )

                quote = self._quote_provider.get_quote(instrument)
                if quote is None:
                    self._record_event(
                        AutonomousEventType.ERROR,
                        symbol=underlying,
                        message=f"failed to fetch exit premium for {pos_contract_id}",
                    )
                    return _exit_reject(
                        f"exit rejected: no LTP available for {pos_contract_id}"
                    )
                if not self._quote_provider.is_fresh(quote, max_age_seconds=max_quote_age_seconds):
                    self._record_event(
                        AutonomousEventType.ERROR,
                        symbol=underlying,
                        message=f"exit quote stale: age={quote.age_seconds:.0f}s for {pos_contract_id}",
                    )
                    return _exit_reject(
                        f"exit rejected: stale LTP for {pos_contract_id} "
                        f"(age={quote.age_seconds:.0f}s)"
                    )
            else:
                self._record_event(
                    AutonomousEventType.ERROR,
                    symbol=decision.opportunity_symbol,
                    message="exit rejected: existing position missing contract metadata",
                )
                return _exit_reject(
                    "exit rejected: existing position missing contract metadata"
                )
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
        action_for_id = explicit_side if explicit_side is not None else decision.action
        if client_order_id is None:
            import hashlib
            client_order_id = hashlib.sha256(
                f"{decision.decision_id}:{instrument.contract_id}:{action_for_id}".encode("utf-8")
            ).hexdigest()[:48]

        # --- Phase 7 safety: contract validity ---
        option_type = explicit_option_type or instrument.option_type
        selection = OptionsContractSelection.from_instrument(instrument)

        # An exit is any order carrying an existing position. Entry-time
        # policy must not apply to it: a rule that sizes *new* risk, or a
        # toggle that was switched off after the position was opened, cannot
        # be allowed to decide whether already-committed capital gets to
        # leave. Getting that wrong is how a position becomes permanently
        # unexitable.
        is_exit = existing_position is not None

        safety = self._safety_layer.validator.check_contract_validity(
            options_selection=selection,
            allowed_option_types=[option_type] if option_type else ["CE", "PE"],
            now_date=__import__("datetime").date.today().isoformat(),
        )
        if not safety.passed:
            # The expiry check still applies to exits, and must: there is no
            # market left to sell into, and inventing a fill price for a dead
            # contract is worse than refusing. The caller settles it at
            # intrinsic value instead. Naming expiry explicitly here stops this
            # from being reported as a generic, unexplained fail-closed.
            detail = (
                "expired contract cannot be market-exited; settle at intrinsic value"
                if is_exit
                else f"Phase 7 contract safety failed: {safety.failed_checks}"
            )
            self._record_event(
                AutonomousEventType.POLICY_VIOLATION,
                symbol=instrument.underlying or decision.opportunity_symbol,
                message=detail,
            )
            return _exit_reject(detail)

        # --- Deployment config checks ---
        # Skipped for exits: options_enabled, allowed_option_types and the
        # per-trade contract cap all constrain *entries*. Honoring them on the
        # exit leg would trap capital in exactly the position they were
        # written to avoid creating. Hard safety checks (paper-only,
        # execution mode, contract validation above) are NOT entry-only and
        # still apply.
        if options_deployment_config is not None and not is_exit:
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
            return _exit_reject(
                "exit rejected: no active session/deployment for broker"
            )

        # Pre-compute values needed for idempotency replay and OrderIntent.
        # `side` was resolved (and validated) at the top of this method.

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
            return _exit_reject(
                f"exit rejected: duplicate deployment key for {instrument.contract_id}"
            )
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
            if is_exit:
                self._record_event(
                    AutonomousEventType.SELL_FAILED,
                    symbol=instrument.underlying or decision.opportunity_symbol,
                    message=f"sell-to-close failed at submission: {exc}",
                    payload=_contract_identity_payload(existing_position),
                )
            return None
        except Exception as exc:
            self._record_event(
                AutonomousEventType.ERROR,
                symbol=instrument.underlying or decision.opportunity_symbol,
                message=f"order submission error: {exc}",
            )
            if is_exit:
                self._record_event(
                    AutonomousEventType.SELL_FAILED,
                    symbol=instrument.underlying or decision.opportunity_symbol,
                    message=f"sell-to-close failed at submission: {exc}",
                    payload=_contract_identity_payload(existing_position),
                )
            return None

        if is_exit:
            self._record_exit_lifecycle(
                result=result, session_id=sid, instrument_key=instrument.key
            )

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
    # Exit observability (SELL_* + reconciliation)
    # ------------------------------------------------------------------ #
    def _record_exit_lifecycle(
        self, *, result: "OrderResult", session_id: Any, instrument_key: str
    ) -> None:
        """Record the sell-to-close outcome and re-read the book afterwards.

        ``SELL_SUBMITTED`` / ``SELL_FILLED`` / ``SELL_FAILED`` say what the
        order did; ``POSITION_RECONCILED`` then re-reads the broker position
        so a fill that left the position open (idempotent replay, partial
        fill, stale book) is visible as such instead of looking like a
        completed exit. Reconciliation only ever reports what the book says —
        it never rewrites it.
        """
        status = str(getattr(result, "status", ""))
        base_payload = {
            "order_id": getattr(result, "order_id", None),
            "options_contract_id": getattr(result, "options_contract_id", None),
            "strike": getattr(result, "strike", None),
            "expiry": getattr(result, "expiry", None),
            "option_type": getattr(result, "option_type", None),
            "filled_quantity": getattr(result, "filled_quantity", None),
            "avg_fill_price": getattr(result, "avg_fill_price", None),
            "status": status,
            "is_idempotent_replay": bool(
                getattr(result, "is_idempotent_replay", False)
            ),
        }
        symbol = getattr(result, "symbol", None) or instrument_key

        self._record_event(
            AutonomousEventType.SELL_SUBMITTED,
            symbol=symbol,
            message=f"sell-to-close submitted ({status})",
            payload=dict(base_payload),
        )
        if status.upper() == "FILLED":
            self._record_event(
                AutonomousEventType.SELL_FILLED,
                symbol=symbol,
                message="sell-to-close filled",
                payload=dict(base_payload),
            )
        else:
            self._record_event(
                AutonomousEventType.SELL_FAILED,
                symbol=symbol,
                message=f"sell-to-close did not fill (status={status})",
                payload={
                    **base_payload,
                    "reject_reason": str(getattr(result, "reject_reason", "") or ""),
                },
            )

        qty_after: Optional[float] = None
        try:
            runner = self.control_center.get_runner(session_id)
            positions = runner.broker.positions() if runner is not None else {}
            pos = positions.get(instrument_key)
            if pos is not None:
                qty_after = float(getattr(pos, "qty", 0.0) or 0.0)
        except Exception:  # noqa: BLE001 — reconciliation must never raise
            qty_after = None
        closed = (qty_after == 0.0) if qty_after is not None else None
        if closed is True:
            message = "position reconciled flat after sell"
        elif closed is False:
            message = f"position NOT flat after sell (qty_after={qty_after})"
        else:
            message = "position state unreadable after sell (no reconciliation possible)"
        self._record_event(
            AutonomousEventType.POSITION_RECONCILED,
            symbol=symbol,
            message=message,
            payload={**base_payload, "qty_after": qty_after, "closed": closed},
        )
        if closed is False:
            # A filled sell that leaves the position open is exactly the
            # silent-failure shape this lifecycle exists to prevent, so it is
            # escalated rather than left buried in a payload.
            self._record_event(
                AutonomousEventType.ERROR,
                symbol=symbol,
                message=(
                    "sell-to-close filled but the position is still open "
                    f"(qty_after={qty_after}); reconciliation failed"
                ),
                payload={"order_id": base_payload["order_id"], "qty_after": qty_after},
            )

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
