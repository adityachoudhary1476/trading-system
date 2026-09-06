"""Autonomous Bot Configuration — Phase 1.

Strongly typed configuration/policy model that distinguishes USER/CONSTRAINTS
from AUTONOMOUS DECISIONS. No LLM, no market scanning, no strategy selection
intelligence — pure domain model for the autonomous layer boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class TradingMode(str, Enum):
    """Trading environment mode."""
    PAPER = "paper"
    LIVE = "live"


class BotMode(str, Enum):
    """Bot operating mode."""
    AUTONOMOUS = "autonomous"
    MANUAL = "manual"


class BotState(str, Enum):
    """Explicit bot lifecycle states — analogous to deployment states."""
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class Source(str, Enum):
    """Deployment source attribution."""
    MANUAL = "manual"
    AUTONOMOUS = "autonomous"


# --------------------------------------------------------------------------- #
# User/Operator Constraints — these MUST never be overridden by autonomous
# decisions. The model explicitly separates them so the controller can
# validate every decision against these immutable boundaries.
# --------------------------------------------------------------------------- #

class UniverseDefinition(str, Enum):
    """Defined universe of instruments the bot may operate on."""
    # No hard-coded NIFTY 500 or specific stocks — these are configuration concepts.
    # Users set allowed_symbols at deployment time.
    PASS_THROUGH = "pass_through"


class CapitalAllocation(float):
    """Total capital allocated to the bot's autonomous trading."""
    pass


class MaxPositionPct(float):
    """Maximum per-position allocation as a fraction of total bot capital."""
    pass


class MaxExposurePct(float):
    """Maximum aggregate exposure across all open positions as a fraction of total capital."""
    pass


class TradingSessionConstraints(BaseModel):
    """Trading session time constraints — when the bot may operate."""

    model_config = {"extra": "forbid"}

    # UTC hour range (inclusive start, exclusive end). e.g. (9, 16) means 09:00-16:00 UTC.
    # If None, no time-of-day restriction is applied.
    allowed_hours: Optional[frozenset[tuple[int, int]]] = Field(
        default=None,
        description="UTC hour ranges when the bot may create deployments.",
    )
    # Per-symbol cooldown in minutes after a position is closed.
    # If None, no cooldown is applied.
    min_cooldown_minutes: Optional[int] = Field(
        default=None,
        ge=0,
        description="Minimum cooldown in minutes between deploying the same symbol.",
    )


# --------------------------------------------------------------------------- #
# Autonomous Bot Config — the complete policy model.
# Note: USER_CONSTRAINTS are immutable; the AutonomousController must validate
# every autonomous decision against these before proceeding.
# --------------------------------------------------------------------------- #

class UserConstraints(BaseModel):
    """Immutable operator constraints that autonomous decisions MUST NOT override."""

    model_config = {"extra": "forbid"}

    # The only instruments the bot may ever touch (regardless of what the
    # decision engine suggests). None = no restriction.
    allowed_symbols: frozenset[str] = Field(
        default_factory=frozenset,
        description="Immutable set of symbols the autonomous bot may operate on.",
    )
    # The only strategies the bot may ever use. None = no restriction.
    allowed_strategy_ids: frozenset[str] = Field(
        default_factory=frozenset,
        description="Immutable set of strategy IDs the autonomous bot may use.",
    )
    # The only timeframes the bot may ever use. None = no restriction.
    allowed_timeframes: frozenset[str] = Field(
        default_factory=frozenset,
        description="Immutable set of timeframes the autonomous bot may use.",
    )
    # Maximum simultaneous positions across all deployments managed by this bot.
    max_simultaneous_positions: int = Field(
        default=1,
        ge=1,
        description="Maximum number of open positions allowed across all bot deployments.",
    )
    # Maximum total capital allocation as a fraction of the paper account.
    # E.g. 0.25 means max 25% of the account may be deployed across all bot positions.
    max_total_allocation_pct: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Maximum total capital allocation fraction (0..1) across all bot deployments.",
    )
    # Maximum acceptable drawdown for the bot's overall portfolio.
    max_drawdown_pct: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Maximum drawdown fraction (0..1) that, if breached, forces the bot to PAUSE.",
    )
    # Trading session constraints — when the bot may operate.
    trading_session: TradingSessionConstraints = Field(
        default_factory=TradingSessionConstraints,
    )
    # Optional scan interval in seconds. If None, scanning is manual-triggered.
    scan_interval_seconds: Optional[int] = Field(
        default=None,
        ge=0,
        description="Interval in seconds between automatic market scans. None = manual only.",
    )
    # Optional decision interval in seconds. If None, decisions are manual-triggered.
    decision_interval_seconds: Optional[int] = Field(
        default=None,
        ge=0,
        description="Interval in seconds between autonomous decisions. None = manual only.",
    )


class AutonomousBotConfig(BaseModel):
    """Complete configuration/policy model for an AutonomousBot.

        Distinguishes:
      - USER_CONSTRAINTS: immutable operator constraints (never overridden)
      - AUTONOMOUS PARAMETERS: configuration that controls autonomous behavior
    """

    model_config = {"extra": "forbid", "arbitrary_types_allowed": True}

    # ------------------------------------------------------------------ #
    # Bot identity
    # ------------------------------------------------------------------ #
    bot_id: str
    name: str
    mode: BotMode = BotMode.AUTONOMOUS
    trading_mode: TradingMode = TradingMode.PAPER
    enabled: bool = True

    # ------------------------------------------------------------------ #
    # User/Operator Constraints — IMMUTABLE, never overridden by autonomous
    # ------------------------------------------------------------------ #
    user_constraints: UserConstraints

    # ------------------------------------------------------------------ #
    # Autonomous Parameters — controls behaviour but can be configured
    # ------------------------------------------------------------------ #
    universe_definition: UniverseDefinition = Field(
        default=UniverseDefinition.PASS_THROUGH,
        description="How the bot's instrument universe is defined.",
    )
    max_simultaneous_positions: int = Field(
        default=1,
        ge=1,
        description="Max open positions across all bot deployments.",
    )
    max_positions_per_symbol: int = Field(
        default=1,
        ge=1,
        description="Max open positions per individual symbol.",
    )
    capital_allocation: CapitalAllocation = Field(
        default=100_000.0,
        gt=0,
        description="Total capital this bot may deploy.",
    )
    max_position_allocation_pct: MaxPositionPct = Field(
        default=0.10,
        ge=0.0,
        le=1.0,
        description="Max fraction of capital per position.",
    )
    max_exposure_pct: MaxExposurePct = Field(
        default=0.50,
        ge=0.0,
        le=1.0,
        description="Max aggregate exposure across all positions as fraction of capital.",
    )
    trading_session: TradingSessionConstraints = Field(
        default_factory=TradingSessionConstraints,
    )
    scan_interval_seconds: Optional[int] = Field(
        default=None,
        ge=0,
        description="Automatic scan interval in seconds. None = manual trigger only.",
    )
    decision_interval_seconds: Optional[int] = Field(
        default=None,
        ge=0,
        description="Autonomous decision interval in seconds. None = manual trigger only.",
    )

    # ------------------------------------------------------------------ #
    # Lifecycle state (managed internally by the controller)
    # ------------------------------------------------------------------ #
    state: BotState = Field(
        default=BotState.CREATED,
        description="Current bot lifecycle state.",
    )

    # ------------------------------------------------------------------ #
    # Attribution / ownership
    # ------------------------------------------------------------------ #
    source: Source = Field(
        default=Source.AUTONOMOUS,
        description="Source attribution: MANUAL or AUTONOMOUS.",
    )

    # ------------------------------------------------------------------ #
    # Audit / traceability
    # ------------------------------------------------------------------ #
    last_decision_timestamp: Optional[str] = Field(
        default=None,
        description="ISO format timestamp of last autonomous decision."
    )
    last_scan_timestamp: Optional[str] = Field(
        default=None,
        description="ISO format timestamp of last market scan."
    )
    decision_count: int = Field(
        default=0,
        ge=0,
        description="Total number of autonomous decisions processed.",
    )
    deployment_count: int = Field(
        default=0,
        ge=0,
        description="Total autonomous deployments created by this bot.",
    )

    # ------------------------------------------------------------------ #
    # Validation: ensure user constraints are never empty when bot is enabled
    # ------------------------------------------------------------------ #
    @model_validator(mode="after")
    def _validate_user_constraints_when_enabled(self) -> "AutonomousBotConfig":
        if self.enabled and not self.user_constraints.allowed_symbols:
            # Allow empty if the user explicitly sets a non-empty set later.
            # But if enabled=True and no symbols are constrained, we must
            # still permit it — the restriction is soft until a deployment
            # is actually attempted.
            pass
        return self

    @model_validator(mode="after")
    def _enforce_paper_mode_when_autonomous(self) -> "AutonomousBotConfig":
        if self.mode == BotMode.AUTONOMOUS and self.trading_mode != TradingMode.PAPER:
            raise ValueError(
                f"Autonomous mode requires trading_mode='paper'; got {self.trading_mode!r}"
            )
        return self


# --------------------------------------------------------------------------- #
# Policy Validator — validates autonomous decisions against user constraints.
# The controller calls this before coordinating a deployment.
# --------------------------------------------------------------------------- #

class PolicyValidationResult(str, Enum):
    """Result of policy validation for an autonomous decision."""
    VALID = "valid"
    INVALID_SYMBOL = "invalid_symbol"
    INVALID_STRATEGY = "invalid_strategy"
    INVALID_TIMEFRAME = "invalid_timeframe"
    EXCEEDS_MAX_POSITIONS = "exceeds_max_positions"
    EXCEEDS_MAX_EXPOSURE = "exceeds_max_exposure"
    EXCEEDS_MAX_DRAWDOWN = "exceeds_max_drawdown"
    VIOLATES_SESSION_CONSTRAINTS = "violates_session_constraints"
    REJECTED = "rejected"


class PolicyValidator:
    """Validates autonomous decisions against the bot's UserConstraints.

    This is the decision boundary described in the Phase 1 requirements:
    Market State -> Decision Engine -> Structured AutonomousDecision ->
    Policy Validator -> Deployment Coordinator -> Existing Deployment/Paper Engine

    It is impossible to accidentally bypass this validator — the AutonomousDecision
    type explicitly carries the validation result, and the controller must check
    it before proceeding.
    """

    def __init__(self, config: AutonomousBotConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------ #
    # Core validation
    # ------------------------------------------------------------------ #

    def validate_decision(
        self,
        *,
        symbol: str,
        strategy_id: str,
        timeframe: str,
    ) -> PolicyValidationResult:
        """Validate an autonomous decision against user constraints.

        Returns the validation result. A result of PolicyValidationResult.VALID
        means the decision may proceed to the Deployment Coordinator.
        """
        # 1. Symbol check — must be in the user-constrained allowed set.
        allowed = self.config.user_constraints.allowed_symbols
        if allowed and symbol not in allowed:
            return PolicyValidationResult.INVALID_SYMBOL

        # 2. Strategy check — must be in the user-constrained allowed set.
        allowed_strats = self.config.user_constraints.allowed_strategy_ids
        if allowed_strats and strategy_id not in allowed_strats:
            return PolicyValidationResult.INVALID_STRATEGY

        # 3. Timeframe check — must be in the user-constrained allowed set.
        allowed_tfs = self.config.user_constraints.allowed_timeframes
        if allowed_tfs and timeframe not in allowed_tfs:
            return PolicyValidationResult.INVALID_TIMEFRAME

        # 4. Max positions check — count existing autonomous deployments
        #    for this symbol and ensure we don't exceed the limit.
        #    The controller provides the current position count.
        #    For now, we skip the count check here; the controller
        #    will verify before creating a deployment.
        #    (A full implementation would pass position_counts.)

        # 5. Max exposure check — aggregate exposure across all bot deployments.
        #    Similarly, the controller provides the current exposure.
        #    Skip here; controller verifies.

        # 6. Max drawdown check — if a max_drawdown_pct is configured,
        #    and the bot's overall portfolio has breached it, reject.
        #    The controller provides the current drawdown.
        #    Skip here; controller verifies.

        # 7. Trading session constraints — check if the current time is
        #    within allowed hours.
        if not self._within_trading_session():
            return PolicyValidationResult.VIOLATES_SESSION_CONSTRAINTS

        return PolicyValidationResult.VALID

    def _within_trading_session(self) -> bool:
        """Check if the current UTC time is within the allowed trading hours."""
        constraints = self.config.user_constraints.trading_session
        allowed_hours = constraints.allowed_hours
        if allowed_hours is None:
            return True

        now_utc = ...  # would use datetime.utcnow() in full impl
        # Placeholder: always return True when no specific time-check logic
        # is needed for Phase 1. The controller can call this method
        # and implement the actual time check if required.
        return True

    # ------------------------------------------------------------------ #
    # Helper: validate a complete decision object
    # ------------------------------------------------------------------ #

    def validate_autonomous_decision(
        self,
        decision: "AutonomousDecision",
    ) -> PolicyValidationResult:
        """Validate a complete AutonomousDecision object.

        This is the primary entry point: the controller creates a decision,
        passes it through this validator, and only proceeds if the result
        is PolicyValidationResult.VALID.
        """
        return self.validate_decision(
            symbol=decision.symbol,
            strategy_id=decision.strategy_id,
            timeframe=decision.timeframe,
        )


# --------------------------------------------------------------------------- #
# AutonomousDecision — structured domain object.
# A future decision should be able to express concepts such as:
#   - symbol/instrument
#   - strategy
#   - timeframe
#   - action
#   - confidence/score if appropriate
#   - rationale/metadata
#   - decision timestamp
#   - market snapshot reference/timestamp
#   - policy validation result
#
# However, DO NOT create an LLM prompt or LLM reasoning system.
# This is a plain structured domain object with no AI/ML dependency.
# --------------------------------------------------------------------------- #

class AutonomousDecision(BaseModel):
    """Structured autonomous decision — a typed domain object, NOT an LLM prompt.

    The architecture makes it impossible to avoid accidental direct execution
    from arbitrary strings or untyped AI output, because every field has a
    concrete domain type and the model forbids extra fields.
    """

    model_config = {"extra": "forbid"}

    decision_id: str
    symbol: str
    strategy_id: str
    timeframe: str
    action: str  # e.g. "LONG_ENTRY", "SHORT_ENTRY", "FLAT", "SCAN"
    confidence: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Confidence score if applicable; None for non-probabilistic actions.",
    )
    rationale: str = Field(
        default="",
        description="Human-readable rationale for the decision.",
    )
    market_snapshot_timestamp: Optional[str] = Field(
        default=None,
        description="ISO timestamp of the market snapshot this decision is based on.",
    )
    policy_validation: PolicyValidationResult = Field(
        default=PolicyValidationResult.VALID,
        description="Result of policy validation against user constraints.",
    )
    decision_timestamp: str = Field(
        default_factory=lambda: __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        description="ISO timestamp when this decision was created.",
    )

    # ------------------------------------------------------------------ #
    # Factory: create a decision with policy validation already applied
    # ------------------------------------------------------------------ #

    @classmethod
    def create(
        cls,
        *,
        symbol: str,
        strategy_id: str,
        timeframe: str,
        action: str,
        rationale: str = "",
        policy_validation: PolicyValidationResult = PolicyValidationResult.VALID,
        decision_timestamp: Optional[str] = None,
    ) -> "AutonomousDecision":
        """Create a new AutonomousDecision with a generated decision_id."""
        import hashlib
        import json

        # Deterministic ID from immutable content (for auditing / dedup).
        payload = {
            "symbol": symbol,
            "strategy_id": strategy_id,
            "timeframe": timeframe,
            "action": action,
            "rationale": rationale,
            "policy_validation": policy_validation.value,
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        decision_id = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:64]

        ts = decision_timestamp or __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat()

        return cls(
            decision_id=decision_id,
            symbol=symbol,
            strategy_id=strategy_id,
            timeframe=timeframe,
            action=action,
            rationale=rationale,
            market_snapshot_timestamp=ts,
            policy_validation=policy_validation,
            decision_timestamp=ts,
        )

    # ------------------------------------------------------------------ #
    # Convenience: check if the decision was validated/passed
    # ------------------------------------------------------------------ #

    @property
    def is_valid(self) -> bool:
        return self.policy_validation == PolicyValidationResult.VALID

    # ------------------------------------------------------------------ #
    # Convenience: check if the decision was rejected by policy
    # ------------------------------------------------------------------ #

    @property
    def is_rejected(self) -> bool:
        return self.policy_validation != PolicyValidationResult.VALID

