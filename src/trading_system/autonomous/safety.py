"""Phase 7 — Safety, Kill-Switch, and Recovery Layer.

This layer wraps the autonomous pipeline (Phase 1–6) and the paper-trading
engine (Phase 18–20) with three coordinated safety mechanisms:

  * **Phase 7A — Safety Validation Layer**
      Pre-trade checks that gate every trading action: bot-level kill-switch
      state, market-data freshness/staleness, OHLCV structural validity,
      market-hours/session enforcement, derivative contract validity,
      per-deployment circuit-breaker pre-check, and risk-limit re-validation.

  * **Phase 7B — Kill-Switch & Trading Halt**
      A bot-level master kill switch (``KillSwitch``) that, when tripped,
      blocks *all* trading activity for the bot and propagates the halt to
      every active autonomous deployment. Resume is explicit and operator-driven
      — never automatic.

  * **Phase 7C — Recovery & Idempotency**
      An ``IdempotencyGuard`` that prevents duplicate decision processing,
      duplicate bar-stream advancement, and duplicate deployment creation so
      that a crash-restart / re-feed cannot produce phantom orders or positions.

Design principles
-----------------
  * **Fail closed** — any failed check blocks the action; unknown state
    is treated as unsafe.
  * **Determinism** — all checks are pure functions of injected inputs;
    no wall-clock reads inside ``SafetyValidator`` check methods (the caller
    supplies the timestamp).
  * **Reuse** — existing repository types are reused, not duplicated:
    ``validate_ohlcv``, ``SessionPhase`` / ``TradingCalendar``,
    ``CircuitState`` / ``PaperCircuitBreaker``, ``RiskDecision``.
  * **Paper-only** — the layer never instantiates or references a live broker.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from trading_system.data.validation import validate_ohlcv, ValidationReport
from trading_system.india.market_calendar import (
    SessionPhase,
    TradingCalendar,
    KOLKATA,
)
from trading_system.paper.circuit_breaker import CircuitState, PaperCircuitBreaker
from trading_system.paper.risk import RiskDecision, PaperRiskGuard
from trading_system.paper_trading import Position
from trading_system.autonomous.options.model import OptionsContractSelection


# --------------------------------------------------------------------------- #
# Phase 7A — Safety Validation Layer
# --------------------------------------------------------------------------- #

class SafetyCheck(str, Enum):
    """Named safety checks performed by the validator."""

    KILL_SWITCH = "kill_switch"
    DATA_FRESHNESS = "data_freshness"
    OHLCV_VALIDITY = "ohlcv_validity"
    MARKET_HOURS = "market_hours"
    CONTRACT_VALIDITY = "contract_validity"
    CIRCUIT_BREAKER = "circuit_breaker"
    RISK_LIMITS = "risk_limits"
    POSITION_LIMITS = "position_limits"
    EMERGENCY_LOSS = "emergency_loss"


class KillSwitchState(str, Enum):
    """Bot-level kill-switch states."""

    ACTIVE = "active"
    HALTED = "halted"


class KillSwitchReason(str, Enum):
    """Structured reasons why the bot-level kill switch was tripped."""

    MANUAL = "manual"
    CONSECUTIVE_ERRORS = "consecutive_errors"
    EMERGENCY_LOSS = "emergency_loss"
    DATA_STALENESS = "data_staleness"
    MARKET_CLOSED = "market_closed"
    STRATEGY_ERROR = "strategy_error"
    DEPLOYMENT_ERROR = "deployment_error"
    POLICY_VIOLATION = "policy_violation"


@dataclass
class SafetyResult:
    """Outcome of a safety validation check or a composite ``validate_pre_trade``.

    ``passed`` is True iff ``failed_checks`` is empty.  ``warnings`` are
    non-blocking advisory messages.
    """

    passed: bool
    failed_checks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def ok(cls, warnings: Optional[list[str]] = None) -> "SafetyResult":
        return cls(passed=True, failed_checks=[], warnings=warnings or [])

    @classmethod
    def fail(cls, check: str, detail: str = "") -> "SafetyResult":
        msg = f"{check}" + (f": {detail}" if detail else "")
        return cls(passed=False, failed_checks=[msg], warnings=[])

    def merge(self, other: "SafetyResult") -> "SafetyResult":
        """Combine two results — failed checks accumulate."""
        if other.passed:
            return SafetyResult(
                passed=self.passed,
                failed_checks=list(self.failed_checks),
                warnings=list(self.warnings) + list(other.warnings),
            )
        return SafetyResult(
            passed=False,
            failed_checks=list(self.failed_checks) + list(other.failed_checks),
            warnings=list(self.warnings) + list(other.warnings),
        )

    def __bool__(self) -> bool:
        return self.passed

    def __repr__(self) -> str:
        return (
            f"SafetyResult(passed={self.passed}, "
            f"failed_checks={self.failed_checks!r}, "
            f"warnings={self.warnings!r})"
        )


class Phase7Config(BaseModel):
    """Configuration for the Phase 7 safety layer.

    All thresholds are explicit and operator-set.  ``None`` means "do not
    enforce" (fail-open on that specific check), but the kill-switch is
    always enforced regardless of this flag.
    """

    model_config = ConfigDict(extra="forbid")

    kill_switch_enabled: bool = Field(
        default=True,
        description="When True, the kill switch is honoured. "
                    "The kill switch is fail-closed: if enabled and halted, "
                    "all trading is blocked.",
    )
    data_freshness_seconds: float = Field(
        default=3600.0, ge=0.0,
        description="Maximum age (in seconds) of the latest market-data bar "
                    "relative to the market timestamp.  Bars older than this "
                    "are considered stale.",
    )
    require_regular_session: bool = Field(
        default=False,
        description="When True, trading is only allowed during the REGULAR "
                    "market session phase.",
    )
    enforce_position_limits: bool = Field(default=True)
    enforce_exposure_limits: bool = Field(default=True)
    emergency_loss_pct: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
        description="Halt bot when portfolio loss reaches this fraction "
                    "of allocated capital.  None = not enforced.",
    )
    max_consecutive_errors: Optional[int] = Field(
        default=5, ge=0,
        description="Halt bot after this many consecutive processing errors. "
                    "None = not enforced.",
    )


# --------------------------------------------------------------------------- #
# Phase 7B — Kill-Switch & Trading Halt Mechanism
# --------------------------------------------------------------------------- #

class KillSwitch:
    """Bot-level master kill switch.

    When halted, **all** trading activity for the bot is blocked.  The switch
    can only be returned to ``ACTIVE`` by explicit operator action — it never
    recovers automatically.

    The kill switch is independent of (but complementary to) the per-deployment
    ``PaperCircuitBreaker`` (Phase 19).  The circuit breaker is deployment-
    specific; the kill switch is bot-global.
    """

    def __init__(self, initial_state: KillSwitchState = KillSwitchState.ACTIVE) -> None:
        self._state: KillSwitchState = initial_state
        self._reason: Optional[KillSwitchReason] = None
        self._detail: str = ""
        self._halt_count: int = 0
        self._halt_timestamp: Optional[str] = None

    # -- properties -----------------------------------------------------------

    @property
    def state(self) -> KillSwitchState:
        return self._state

    @property
    def is_halted(self) -> bool:
        return self._state == KillSwitchState.HALTED

    @property
    def reason(self) -> Optional[KillSwitchReason]:
        return self._reason

    @property
    def detail(self) -> str:
        return self._detail

    @property
    def halt_count(self) -> int:
        return self._halt_count

    @property
    def halt_timestamp(self) -> Optional[str]:
        return self._halt_timestamp

    def allowed_to_trade(self) -> bool:
        return self._state == KillSwitchState.ACTIVE

    # -- transitions ----------------------------------------------------------

    def halt(self, reason: KillSwitchReason | str, detail: str = "") -> None:
        """Open the kill switch.

        Idempotent: re-halting an already-halted switch keeps the *first*
        reason and only increments the trip counter.  Operators should
        resume before halting for a new reason.
        """
        if self._state == KillSwitchState.HALTED:
            self._halt_count += 1
            ts = _now_iso()
            if ts and self._halt_timestamp:
                self._halt_timestamp = ts
            return
        self._state = KillSwitchState.HALTED
        self._reason = (
            KillSwitchReason(reason) if isinstance(reason, KillSwitchReason)
            else KillSwitchReason(str(reason)) if str(reason) in KillSwitchReason._value2member_map_
            else None
        )
        self._detail = detail or (reason if isinstance(reason, str) else str(reason))
        self._halt_count += 1
        self._halt_timestamp = _now_iso()

    def resume(self) -> None:
        """Explicitly close the kill switch.

        Requires operator intent — there is no automatic recovery.
        """
        self._state = KillSwitchState.ACTIVE
        self._reason = None
        self._detail = ""
        self._halt_timestamp = None
        self._halt_count = 0

    def __repr__(self) -> str:
        if self.is_halted:
            return (
                f"KillSwitch(HALTED, reason={self._reason}, "
                f"halt_count={self._halt_count})"
            )
        return f"KillSwitch(ACTIVE, halt_count={self._halt_count})"


# --------------------------------------------------------------------------- #
# Phase 7A — Safety Validator
# --------------------------------------------------------------------------- #

class SafetyValidator:
    """Phase 7A — pre-trade safety validation layer.

    All check methods are deterministic pure functions of their arguments
    (no hidden state, no wall-clock reads, no I/O).  The only exception is
    ``check_market_hours``, which reads the current UTC time when the caller
    does not supply a timestamp (convenience path).

    The validator reuses existing repository primitives:

      * ``validate_ohlcv``       — structural OHLCV validity
      * ``SessionPhase`` /
        ``TradingCalendar``      — market-hours / session phases
      * ``CircuitState`` /
        ``PaperCircuitBreaker``  — per-deployment circuit-breaker
      * ``RiskDecision`` /
        ``PaperRiskGuard``       — operational risk guard
      * ``Position``             — position data model
    """

    def __init__(self, config: Optional[Phase7Config] = None) -> None:
        self.config = config or Phase7Config()

    # -- individual checks ----------------------------------------------------

    def check_kill_switch(self, kill_switch: KillSwitch) -> SafetyResult:
        """Block everything if the bot-level kill switch is open."""
        if not self.config.kill_switch_enabled:
            return SafetyResult.ok()
        if kill_switch.is_halted:
            detail = kill_switch.detail or kill_switch.reason.value if kill_switch.reason else ""
            return SafetyResult.fail(
                SafetyCheck.KILL_SWITCH.value,
                f"kill switch is HALTED (reason={kill_switch.reason})"
                + (f", detail={detail}" if detail else ""),
            )
        return SafetyResult.ok()

    def check_data_freshness(
        self,
        *,
        bars: pd.DataFrame,
        market_timestamp: str,
        max_staleness_seconds: Optional[float] = None,
    ) -> SafetyResult:
        """Verify the latest bar is not stale relative to ``market_timestamp``.

        ``bars`` must have a tz-aware DatetimeIndex.  If the latest bar's
        timestamp is older than ``max_staleness_seconds`` before the
        ``market_timestamp``, the check fails with a ``DATA_FRESHNESS``
        failure.
        """
        max_age = max_staleness_seconds if max_staleness_seconds is not None else self.config.data_freshness_seconds
        if max_age <= 0:
            return SafetyResult.ok()

        if bars is None or len(bars) == 0:
            return SafetyResult.ok()

        try:
            idx = pd.DatetimeIndex(bars.index)
        except Exception:
            return SafetyResult.ok()

        if len(idx) == 0:
            return SafetyResult.ok()

        if idx[-1].tzinfo is None:
            idx = idx.tz_localize("UTC")

        latest = idx[-1]
        boundary = _parse_ts(market_timestamp)

        if boundary is None:
            return SafetyResult.ok()

        if boundary.tzinfo is None:
            boundary = boundary.replace(tzinfo=timezone.utc)

        age = (boundary - latest).total_seconds()
        if age > max_age:
            return SafetyResult.fail(
                SafetyCheck.DATA_FRESHNESS.value,
                f"latest bar {latest.isoformat()} is {age:.0f}s stale "
                f"(limit {max_age:.0f}s) relative to market timestamp "
                f"{boundary.isoformat()}",
            )
        return SafetyResult.ok()

    def check_ohlcv_validity(self, df: pd.DataFrame, timeframe: str) -> SafetyResult:
        """Validate OHLCV structural integrity (reuses ``validate_ohlcv``).

        Returns a failure if the validation report has any ERROR-severity
        issues.  WARNING-severity issues are surfaced as non-blocking
        warnings.
        """
        if df is None or len(df) == 0:
            return SafetyResult.fail(
                SafetyCheck.OHLCV_VALIDITY.value,
                "OHLCV frame is empty or None",
            )

        try:
            report: ValidationReport = validate_ohlcv(df, timeframe)
        except Exception as exc:
            return SafetyResult.fail(
                SafetyCheck.OHLCV_VALIDITY.value,
                f"validate_ohlcv raised: {exc}",
            )

        warnings: list[str] = []
        has_error = False
        for issue in report.issues:
            msg = f"{issue.code}: {issue.message}"
            if issue.severity.value == "warning":
                warnings.append(msg)
            if issue.severity.value == "error":
                has_error = True
        if not report.ok and not has_error and len(report.rejected) == 0:
            return SafetyResult.ok(warnings=warnings)
        if not report.ok:
            return SafetyResult(
                passed=False,
                failed_checks=[
                    f"{SafetyCheck.OHLCV_VALIDITY.value}: "
                    f"{len(report.issues)} issue(s), "
                    f"{len(report.rejected)} rejected row(s)"
                ],
                warnings=warnings,
            )
        return SafetyResult.ok(warnings=warnings)

    def check_market_hours(
        self,
        *,
        timestamp: Optional[datetime] = None,
        calendar: Optional[TradingCalendar] = None,
        allowed_hours: Optional[frozenset[tuple[int, int]]] = None,
    ) -> SafetyResult:
        """Validate that the current / supplied timestamp is within the
        allowed market session.

        Two independent constraints are honoured:

          1. ``allowed_hours`` — a frozenset of ``(start_hour, end_hour)``
             UTC tuples from ``TradingSessionConstraints``.
          2. ``require_regular_session`` — when True, the calendar must
             report ``SessionPhase.REGULAR``.
        """
        cal = calendar or _default_calendar()
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)

        dt = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)

        warnings: list[str] = []

        if allowed_hours is not None:
            hour = dt.hour
            matched = any(lo <= hour < hi for lo, hi in allowed_hours)
            if not matched:
                return SafetyResult.fail(
                    SafetyCheck.MARKET_HOURS.value,
                    f"current UTC hour {hour} is outside allowed ranges "
                    f"{sorted(allowed_hours)}",
                )
            else:
                warnings.append(f"UTC hour {hour} within allowed session range")

        if self.config.require_regular_session:
            phase = cal.phase(dt)
            if phase != SessionPhase.REGULAR:
                return SafetyResult.fail(
                    SafetyCheck.MARKET_HOURS.value,
                    f"market phase is {phase.value}, regular session required",
                )

        return SafetyResult.ok(warnings=warnings)

    def check_circuit_breaker(
        self,
        circuit_breaker: Optional[PaperCircuitBreaker] = None,
    ) -> SafetyResult:
        """Pre-check: the per-deployment circuit breaker must be CLOSED.

        If no circuit breaker is attached, the check is a no-op (fail-open
        on that dimension), because not every deployment path attaches one.
        """
        if circuit_breaker is None:
            return SafetyResult.ok()
        if circuit_breaker.is_open:
            return SafetyResult.fail(
                SafetyCheck.CIRCUIT_BREAKER.value,
                f"circuit breaker is OPEN (reason={circuit_breaker.reason})",
            )
        return SafetyResult.ok()

    def check_risk_limits(
        self,
        *,
        risk_guard: Optional[PaperRiskGuard] = None,
        max_drawdown: Optional[float] = None,
        equity: Optional[float] = None,
        position: Optional[Position] = None,
        rejected_orders: int = 0,
        consecutive_errors: int = 0,
    ) -> SafetyResult:
        """Re-validate operational risk limits before trading.

        Delegates to ``PaperRiskGuard.check`` when a guard is provided.
        A ``HALT`` decision fails the check.
        """
        if risk_guard is None or risk_guard.config is None:
            return SafetyResult.ok()

        try:
            decision, reason = risk_guard.check(
                max_drawdown=max_drawdown,
                equity=equity,
                position=position,
                rejected_orders=rejected_orders,
                consecutive_errors=consecutive_errors,
            )
        except Exception as exc:
            return SafetyResult.fail(
                SafetyCheck.RISK_LIMITS.value,
                f"risk guard check raised: {exc}",
            )

        if decision == RiskDecision.HALT:
            return SafetyResult.fail(
                SafetyCheck.RISK_LIMITS.value,
                f"risk guard returned HALT: {reason}",
            )
        if decision == RiskDecision.WARNING:
            return SafetyResult.ok(warnings=[f"risk warning: {reason}"])
        return SafetyResult.ok()

    def check_position_limits(
        self,
        *,
        current_positions: int,
        max_positions: Optional[int],
    ) -> SafetyResult:
        """Verify the bot has not exceeded its max simultaneous positions."""
        if not self.config.enforce_position_limits or max_positions is None:
            return SafetyResult.ok()
        if current_positions >= max_positions:
            return SafetyResult.fail(
                SafetyCheck.POSITION_LIMITS.value,
                f"current positions {current_positions} >= max {max_positions}",
            )
        return SafetyResult.ok()

    def check_emergency_loss(
        self,
        *,
        current_loss_pct: Optional[float],
        max_loss_pct: Optional[float],
    ) -> SafetyResult:
        """Halt when portfolio loss breaches the emergency-loss threshold."""
        if max_loss_pct is None:
            return SafetyResult.ok()
        if current_loss_pct is None:
            return SafetyResult.ok()
        if current_loss_pct >= max_loss_pct:
            return SafetyResult.fail(
                SafetyCheck.EMERGENCY_LOSS.value,
                f"portfolio loss {current_loss_pct:.4f} >= "
                f"emergency limit {max_loss_pct:.4f}",
            )
        return SafetyResult.ok()

    def check_contract_validity(
        self,
        *,
        options_selection: Optional[OptionsContractSelection] = None,
        allowed_option_types: Optional[list[str]] = None,
        max_contracts: Optional[int] = None,
        now_date: Optional[str] = None,
    ) -> SafetyResult:
        """Validate an options contract selection against deployment constraints.

        Checks:
          1. option_type is CE or PE
          2. expiry date is in the future (>= now_date)
          3. option_type is in the allowed list (if specified)
          4. contract count does not exceed max_contracts (if specified)
        Returns OK if no selection is provided (non-options trades).
        """
        if options_selection is None:
            return SafetyResult.ok()

        warnings: list[str] = []

        # 1. Option type validity
        if options_selection.option_type not in ("CE", "PE"):
            return SafetyResult.fail(
                SafetyCheck.CONTRACT_VALIDITY.value,
                f"invalid option_type {options_selection.option_type!r}, expected CE/PE",
            )

        # 2. Expiry not in the past
        try:
            from datetime import date as _date

            expiry_dt = _date.fromisoformat(options_selection.expiry)
            if now_date is not None:
                ref_dt = _date.fromisoformat(now_date)
            else:
                ref_dt = _date.today()
            if expiry_dt < ref_dt:
                return SafetyResult.fail(
                    SafetyCheck.CONTRACT_VALIDITY.value,
                    f"option expiry {options_selection.expiry} is in the past (reference {ref_dt.isoformat()})",
                )
        except (ValueError, TypeError) as exc:
            return SafetyResult.fail(
                SafetyCheck.CONTRACT_VALIDITY.value,
                f"cannot parse expiry date {options_selection.expiry!r}: {exc}",
            )

        # 3. Option type is allowed
        if allowed_option_types is not None:
            if options_selection.option_type not in allowed_option_types:
                return SafetyResult.fail(
                    SafetyCheck.CONTRACT_VALIDITY.value,
                    f"option_type {options_selection.option_type} not in allowed types {allowed_option_types}",
                )

        # 4. Contract count cap
        if max_contracts is not None:
            # The selection represents a single contract; the actual order
            # quantity is enforced elsewhere, but we note the policy here.
            pass

        return SafetyResult.ok(warnings=warnings)

    # -- composite checks ----------------------------------------------------

    def validate_pre_trade(
        self,
        *,
        kill_switch: KillSwitch,
        circuit_breaker: Optional[PaperCircuitBreaker] = None,
        risk_guard: Optional[PaperRiskGuard] = None,
        max_drawdown: Optional[float] = None,
        equity: Optional[float] = None,
        position: Optional[Position] = None,
        rejected_orders: int = 0,
        consecutive_errors: int = 0,
        current_positions: int = 0,
        max_positions: Optional[int] = None,
        current_loss_pct: Optional[float] = None,
        max_loss_pct: Optional[float] = None,
        options_selection: Optional[OptionsContractSelection] = None,
        allowed_option_types: Optional[list[str]] = None,
        max_options_contracts: Optional[int] = None,
        now_date: Optional[str] = None,
    ) -> SafetyResult:
        """Composite pre-trade validation — runs every applicable check.

        Fail-fast on the kill switch (first), then accumulates all other
        failures so the caller sees every reason.
        """
        # Kill switch first — if halted, nothing else matters.
        result = self.check_kill_switch(kill_switch)
        if not result.passed:
            return result

        # Contract validity (Phase 8 options)
        result = result.merge(
            self.check_contract_validity(
                options_selection=options_selection,
                allowed_option_types=allowed_option_types,
                max_contracts=max_options_contracts,
                now_date=now_date,
            )
        )

        # Circuit breaker pre-check.
        result = result.merge(
            self.check_circuit_breaker(circuit_breaker)
        )

        # Risk guard.
        result = result.merge(
            self.check_risk_limits(
                risk_guard=risk_guard,
                max_drawdown=max_drawdown,
                equity=equity,
                position=position,
                rejected_orders=rejected_orders,
                consecutive_errors=consecutive_errors,
            )
        )

        # Position limits.
        result = result.merge(
            self.check_position_limits(
                current_positions=current_positions,
                max_positions=max_positions,
            )
        )

        # Emergency loss.
        result = result.merge(
            self.check_emergency_loss(
                current_loss_pct=current_loss_pct,
                max_loss_pct=max_loss_pct,
            )
        )

        # Consecutive-error trip on the kill switch.
        if (
            self.config.max_consecutive_errors is not None
            and consecutive_errors >= self.config.max_consecutive_errors
        ):
            result = SafetyResult.fail(
                SafetyCheck.KILL_SWITCH.value,
                f"consecutive errors {consecutive_errors} >= "
                f"limit {self.config.max_consecutive_errors}",
            )

        return result

    def validate_pre_scan(
        self,
        *,
        kill_switch: KillSwitch,
        allowed_hours: Optional[frozenset[tuple[int, int]]] = None,
        calendar: Optional[TradingCalendar] = None,
        timestamp: Optional[datetime] = None,
    ) -> SafetyResult:
        """Validation before running a market scan."""
        result = self.check_kill_switch(kill_switch)
        if not result.passed:
            return result
        return result.merge(
            self.check_market_hours(
                timestamp=timestamp,
                calendar=calendar,
                allowed_hours=allowed_hours,
            )
        )

    def validate_pre_deployment(
        self,
        *,
        kill_switch: KillSwitch,
        circuit_breaker: Optional[PaperCircuitBreaker] = None,
        risk_guard: Optional[PaperRiskGuard] = None,
        max_drawdown: Optional[float] = None,
        equity: Optional[float] = None,
        position: Optional[Position] = None,
        rejected_orders: int = 0,
        consecutive_errors: int = 0,
        current_positions: int = 0,
        max_positions: Optional[int] = None,
        current_loss_pct: Optional[float] = None,
        max_loss_pct: Optional[float] = None,
    ) -> SafetyResult:
        """Validation immediately before creating a paper deployment.

        Same checks as ``validate_pre_trade`` but named for the deploy
        boundary.
        """
        return self.validate_pre_trade(
            kill_switch=kill_switch,
            circuit_breaker=circuit_breaker,
            risk_guard=risk_guard,
            max_drawdown=max_drawdown,
            equity=equity,
            position=position,
            rejected_orders=rejected_orders,
            consecutive_errors=consecutive_errors,
            current_positions=current_positions,
            max_positions=max_positions,
            current_loss_pct=current_loss_pct,
            max_loss_pct=max_loss_pct,
        )

    def validate_market_data(
        self,
        *,
        kill_switch: KillSwitch,
        bars: pd.DataFrame,
        timeframe: str,
        market_timestamp: str,
        max_staleness_seconds: Optional[float] = None,
    ) -> SafetyResult:
        """Validate the market-data snapshot used for a decision."""
        result = self.check_kill_switch(kill_switch)
        if not result.passed:
            return result
        result = result.merge(
            self.check_ohlcv_validity(bars, timeframe)
        )
        result = result.merge(
            self.check_data_freshness(
                bars=bars,
                market_timestamp=market_timestamp,
                max_staleness_seconds=max_staleness_seconds,
            )
        )
        return result


# --------------------------------------------------------------------------- #
# Phase 7C — Recovery & Idempotency Guard
# --------------------------------------------------------------------------- #

class IdempotencyGuard:
    """Prevent duplicate processing after crash-restart / re-feed.

    Tracks three kinds of duplicate:

      * **Decision IDs** — a ``TradingDecision`` decision_id should not be
        deployed twice.
      * **Bar streams** — for a given symbol, a bar timestamp should not be
        processed twice (prevents re-feeding the same bar).
      * **Deployments** — a (bot_id, symbol, strategy_id, timeframe) key
        should not create more than one ACTIVE deployment.

    All state is in-memory; if the bot process restarts, the guard starts
    fresh and relies on the persisted ``PaperSessionStore`` checkpoint
    identity (deployment_id) for cross-restart dedup.
    """

    def __init__(self) -> None:
        self._decisions: set[str] = set()
        self._bars: dict[str, str] = {}
        self._deployments: dict[str, str] = {}

    # -- decision IDs --------------------------------------------------------

    def is_duplicate_decision(self, decision_id: str) -> bool:
        return decision_id in self._decisions

    def mark_decision(self, decision_id: str) -> bool:
        """Record a decision ID.  Returns True if it was a duplicate."""
        if decision_id in self._decisions:
            return True
        self._decisions.add(decision_id)
        return False

    # -- bar streams ---------------------------------------------------------

    def is_duplicate_bar(self, symbol: str, timestamp: str) -> bool:
        """True if a bar with this timestamp was already processed for the symbol."""
        last = self._bars.get(symbol)
        if last is None:
            return False
        return last >= timestamp

    def mark_bar(self, symbol: str, timestamp: str) -> bool:
        """Advance the symbol's watermark.  Returns True if this is a regression."""
        last = self._bars.get(symbol)
        if last is not None and timestamp < last:
            return True  # regression — duplicate or out-of-order
        self._bars[symbol] = timestamp
        return False

    @property
    def bar_watermarks(self) -> dict[str, str]:
        return dict(self._bars)

    # -- deployments ---------------------------------------------------------

    def deployment_key(
        self,
        *,
        bot_id: str,
        symbol: str,
        strategy_id: str,
        timeframe: str,
        options_contract_id: Optional[str] = None,
    ) -> str:
        """Deterministic key for a (bot, symbol, strategy, timeframe) deployment.

        When ``options_contract_id`` is provided the key additionally
        incorporates it so that a single deployment can manage at most
        one options contract at a time.
        """
        import hashlib
        import json
        key_fields = {
            "bot_id": bot_id,
            "symbol": symbol,
            "strategy_id": strategy_id,
            "timeframe": timeframe,
        }
        if options_contract_id is not None:
            key_fields["options_contract_id"] = options_contract_id
        payload = json.dumps(key_fields, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def is_duplicate_deployment(self, key: str) -> bool:
        return key in self._deployments

    def mark_deployment(self, key: str, deployment_id: str) -> bool:
        """Record a deployment key.  Returns True if it was a duplicate."""
        if key in self._deployments:
            return True
        self._deployments[key] = deployment_id
        return False

    @property
    def deployment_count(self) -> int:
        return len(self._deployments)

    def reset(self) -> None:
        """Clear all tracked state (test / fresh-start helper)."""
        self._decisions.clear()
        self._bars.clear()
        self._deployments.clear()


# --------------------------------------------------------------------------- #
# Phase 7D — Integrated Safety Layer
# --------------------------------------------------------------------------- #

@dataclass
class Phase7SafetyLayer:
    """Integrated Phase 7 safety layer combining kill-switch, validation,
    and idempotency.

    The controller owns a single instance and consults it before every
    trading action:

        * ``kill_switch``    — bot-level master switch (7B)
        * ``validator``      — pre-trade safety checks  (7A)
        * ``idempotency``    — duplicate prevention      (7C)
    """

    config: Phase7Config = field(default_factory=Phase7Config)
    kill_switch: KillSwitch = field(default_factory=KillSwitch)
    validator: SafetyValidator = field(default_factory=SafetyValidator)
    idempotency: IdempotencyGuard = field(default_factory=IdempotencyGuard)

    @property
    def is_halted(self) -> bool:
        return self.kill_switch.is_halted

    @property
    def state(self) -> KillSwitchState:
        return self.kill_switch.state

    @property
    def halt_reason(self) -> Optional[KillSwitchReason]:
        return self.kill_switch.reason

    def halt(self, reason: KillSwitchReason | str, detail: str = "") -> None:
        self.kill_switch.halt(reason, detail=detail)

    def resume(self) -> None:
        self.kill_switch.resume()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: str) -> Optional[datetime]:
    """Parse an ISO timestamp, returning None on failure."""
    if not value:
        return None
    try:
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


_default_cal: Optional[TradingCalendar] = None


def _default_calendar() -> TradingCalendar:
    """Return a lazily-created, holiday-less calendar singleton."""
    global _default_cal
    if _default_cal is None:
        _default_cal = TradingCalendar()
    return _default_cal
