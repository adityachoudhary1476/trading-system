"""Phase 5 — Strategy Selection & Signal Generation.

Deterministic, paper-only decision layer that sits between Phase 4
(compatibility analysis) and Phase 6 (risk/portfolio).  The engine answers two
questions for every ranked opportunity that has at least one compatible
configuration:

1. *Which* strategy × timeframe should be selected?
2. What signal does the *existing* strategy engine produce for the selected
   configuration under the authoritative market snapshot?

The output is a structured, auditable :class:`TradingDecision` (or a structured
rejection).  Phase 5 **does not** execute anything:

    - no order creation
    - no deployment creation
    - no capital allocation
    - no broker interaction
    - no paper execution
    - no live trading

Dependency direction (Phase 5 boundary)::

    Phase 4 CompatibilityResult  →  Phase 5 StrategyDecisionEngine
                                         ├─ StrategySelection (deterministic)
                                         ├─ Existing StrategyEvaluation  (StrategyRuntime)
                                         └─ Structured TradingDecision

Reused existing abstractions (no duplication):
  - StrategyCompatibility / CompatibilityResult  (Phase 4 output)
  - Strategy / MarketState / StrategySignal      (strategy_factory.contract)
  - StrategyRuntime / create_strategy_runtime    (strategy_factory.runtime)
  - MarketContext / Capabilities                 (strategy_factory.capability)
  - build_from_discovery / discover              (strategy_factory.discovery)
  - validate_ohlcv  (data.validation)            (OHLCV validation)
  - TRADING_PERIODS (analysis.quant)             (timeframe resolution)
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from trading_system.analysis.quant import TRADING_PERIODS
from trading_system.data.validation import validate_ohlcv
from trading_system.strategy_factory.capability import (
    DataRequirement,
    MarketContext,
    capabilities_from,
)
from trading_system.strategy_factory.contract import (
    MarketState,
    StrategySignal,
)
from trading_system.strategy_factory.discovery import (
    build_from_discovery,
    clear_discovery,
    discover,
    registered_strategy_ids,
)
from trading_system.strategy_factory.runtime import StrategyRuntime
from trading_system.strategy_factory.validation import validate_signal

from .options.model import OptionsContractSelection

from .compatibility import (
    CompatibilityResult,
    StrategyCompatibility,
)

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #

class DecisionExclusionReason(str, Enum):
    """Structured reasons a trading decision was rejected."""

    NO_COMPATIBLE_CONFIGURATION = "no_compatible_configuration"
    STRATEGY_NOT_FOUND = "strategy_not_found"
    STRATEGY_INELIGIBLE = "strategy_ineligible"
    UNSUPPORTED_TIMEFRAME = "unsupported_timeframe"
    INSUFFICIENT_DATA = "insufficient_data"
    STALE_DATA = "stale_data"
    INVALID_MARKET_SNAPSHOT = "invalid_market_snapshot"
    INVALID_STRATEGY_OUTPUT = "invalid_strategy_output"
    MISSING_SNAPSHOT = "missing_snapshot"
    EVALUATION_ERROR = "evaluation_error"
    NO_DATA_PROVIDER = "no_data_provider"


class DecisionStatus(str, Enum):
    """Terminal status of a Phase 5 decision."""

    VALID = "valid"
    REJECTED = "rejected"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

class SelectionConfig(BaseModel):
    """Typed, deterministic configuration for the Phase 5 engine.

    The selection policy is fixed and documented (not configurable per-run) to
    guarantee determinism.  The config only tunes *operational* parameters
    (discovery module, enabled flag).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True

    #: Module discovered at startup to populate the strategy catalog.
    discovery_module: str = "trading_system.strategy_factory.builtin"

    def config_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "discovery_module": self.discovery_module,
        }


# --------------------------------------------------------------------------- #
# Selection helpers / models
# --------------------------------------------------------------------------- #

class SelectionFactor(BaseModel):
    """One objective, structured factor considered during selection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    observed_value: Optional[Any] = None
    required_value: str
    matched: bool


class SelectedConfiguration(BaseModel):
    """The winning strategy × timeframe configuration for one opportunity.

    Created from the Phase 4 :class:`StrategyCompatibility` entry that won the
    deterministic selection policy.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy_id: str
    strategy_version: str
    strategy_family: str
    timeframe: str
    compatibility_score: float
    compatibility_order: int  # 0-based position in Phase 4's sorted *group*
    score_components: dict[str, float]
    freshness_factor: float
    data_points: int
    min_required_bars: int
    selection_factors: list[SelectionFactor] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Decision model
# --------------------------------------------------------------------------- #

class TradingDecision(BaseModel):
    """Phase 5 structured trading decision — **not executed**.

    A single opportunity's decision, containing either a selected configuration
    with an evaluated signal, or a structured rejection.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True, frozen=True)

    decision_id: str
    # --- Identity ---
    scan_id: Optional[str] = None
    ranking_id: Optional[str] = None
    opportunity_symbol: str
    opportunity_rank: int
    market_timestamp: str
    snapshot_identity: str
    # --- Selection ---
    selected_configuration: Optional[SelectedConfiguration] = None
    selection_factors: list[SelectionFactor] = Field(default_factory=list)
    # --- Evaluation ---
    signal: Optional[StrategySignal] = None  # arbitrary_types_allowed
    evaluation_error: Optional[str] = None
    # --- Rejection ---
    exclusion_reason: Optional[str] = None
    exclusion_detail: str = ""
    # --- Status ---
    is_valid: bool
    status: str  # DecisionStatus.value
    # --- Audit ---
    decision_timestamp: str
    config_snapshot: dict[str, Any] = Field(default_factory=dict)

    # --- Phase 8: Options contract selection (optional) ---
    options_contract: Optional[OptionsContractSelection] = None

    @property
    def action(self) -> Optional[str]:
        """The strategy's signal action ('buy'/'sell'/'hold'/'exit') or None."""
        if self.signal is not None:
            return self.signal.action.value
        return None

    @property
    def is_rejected(self) -> bool:
        return not self.is_valid

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable representation for audit / Phase 6 consumption."""
        sig = self.signal
        return {
            "decision_id": self.decision_id,
            "scan_id": self.scan_id,
            "ranking_id": self.ranking_id,
            "opportunity_symbol": self.opportunity_symbol,
            "opportunity_rank": self.opportunity_rank,
            "market_timestamp": self.market_timestamp,
            "snapshot_identity": self.snapshot_identity,
            "selected_configuration": (
                self.selected_configuration.model_dump() if self.selected_configuration else None
            ),
            "selection_factors": [f.model_dump() for f in self.selection_factors],
            "signal": sig.to_dict() if sig else None,
            "evaluation_error": self.evaluation_error,
            "exclusion_reason": self.exclusion_reason,
            "exclusion_detail": self.exclusion_detail,
            "is_valid": self.is_valid,
            "status": self.status,
            "decision_timestamp": self.decision_timestamp,
            "config_snapshot": self.config_snapshot,
            "options_contract": (
                self.options_contract.to_dict() if self.options_contract else None
            ),
        }


TradingDecision.model_rebuild()


class DecisionResult(BaseModel):
    """Complete Phase 5 output for all opportunities in a compatibility result."""

    model_config = ConfigDict(extra="forbid")

    result_id: str
    ranking_id: Optional[str] = None
    scan_id: Optional[str] = None
    evaluated_at: str
    decisions: list[TradingDecision] = Field(default_factory=list)
    config_snapshot: dict[str, Any] = Field(default_factory=dict)

    @property
    def valid_count(self) -> int:
        return sum(1 for d in self.decisions if d.is_valid)

    @property
    def rejected_count(self) -> int:
        return sum(1 for d in self.decisions if not d.is_valid)

    @property
    def empty(self) -> bool:
        return len(self.decisions) == 0

    def decision_for(self, symbol: str) -> Optional[TradingDecision]:
        for d in self.decisions:
            if d.opportunity_symbol == symbol:
                return d
        return None


# --------------------------------------------------------------------------- #
# Result identity
# --------------------------------------------------------------------------- #

def _compute_result_id(
    ranking_id: Optional[str],
    scan_id: Optional[str],
    config_snapshot: dict[str, Any],
) -> str:
    """Deterministic identity for a Phase 5 decision result."""
    payload = {
        "ranking_id": ranking_id,
        "scan_id": scan_id,
        "config": config_snapshot,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(("phase5_decision:" + blob).encode("utf-8")).hexdigest()[:64]


def _compute_decision_id(
    ranking_id: Optional[str],
    opportunity_symbol: str,
    opportunity_rank: int,
    market_timestamp: str,
    selected: Optional[SelectedConfiguration],
    snapshot_identity: str,
) -> str:
    """Deterministic identity for a single trading decision."""
    payload = {
        "ranking_id": ranking_id,
        "opportunity_symbol": opportunity_symbol,
        "opportunity_rank": opportunity_rank,
        "market_timestamp": market_timestamp,
        "selected_strategy_id": selected.strategy_id if selected else None,
        "selected_timeframe": selected.timeframe if selected else None,
        "selected_strategy_version": selected.strategy_version if selected else None,
        "snapshot_identity": snapshot_identity,
        "exclusion_reason": selected is None and "no_compatible_configuration",
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(("phase5_trading_decision:" + blob).encode("utf-8")).hexdigest()[:64]


def _compute_snapshot_identity(
    symbol: str,
    timeframe: str,
    market_timestamp: str,
    bars: pd.DataFrame,
) -> str:
    """Deterministic identity for the market snapshot used for evaluation.

    Captures the close prices and bar timestamps so that Phase 6 can verify the
    decision was based on the correct, look-ahead-safe data.
    """
    payload = {
        "symbol": symbol,
        "timeframe": timeframe,
        "market_timestamp": market_timestamp,
        "bar_count": int(len(bars)),
        "close_prices": [float(c) for c in bars["close"].tolist()],
        "bar_timestamps": [ts.isoformat() for ts in bars.index],
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(("phase5_snapshot:" + blob).encode("utf-8")).hexdigest()[:64]


# --------------------------------------------------------------------------- #
# Selection policy
# --------------------------------------------------------------------------- #

def _timeframe_sort_key(timeframe: str) -> tuple:
    """Chronological sort key for timeframe strings.

    Shorter intervals sort before longer intervals.  Falls back to string
    comparison for unknown timeframes.
    """
    if timeframe in TRADING_PERIODS:
        # TRADING_PERIODS maps tf -> periods_per_year; higher = shorter interval.
        # Negate so that shorter timeframes (higher periods_per_year) sort first
        # when ascending, which matches "1m before 5m before 1h before 1d".
        return (0, -TRADING_PERIODS[timeframe])
    return (1, timeframe)


class SelectionPolicy:
    """Deterministic, auditable strategy-selection policy.

    Inspects the repository and finds **no** pre-existing selection policy.
    The Phase 4 compatibility layer already sorts compatible configurations by
    ``(-compatibility_score, strategy_id, timeframe)``.  Phase 5's policy
    honours and makes that ordering explicit:

    1. ``compatibility_score`` DESC — the primary selection criterion.
    2. ``strategy_id`` ASC — deterministic tie-break (alphabetical).
    3. ``timeframe`` ASC — chronological tie-break (shortest interval first).

    The policy is **independent of execution** — it never considers capital,
    position sizing, risk, or portfolio state.
    """

    #: Primary key: higher compatibility score wins.
    PRIMARY_KEY = "compatibility_score"

    @staticmethod
    def selection_key(cfg: StrategyCompatibility) -> tuple:
        """Sort key matching the documented selection priority.

        ``(-score, strategy_id, timeframe_chronological)``
        """
        return (
            -cfg.compatibility_score,
            cfg.strategy_id,
            _timeframe_sort_key(cfg.timeframe),
        )

    @staticmethod
    def sort_group(configs: list[StrategyCompatibility]) -> list[StrategyCompatibility]:
        """Deterministically sort a group of compatible configs for one opportunity.

        Phase 4 already emits a globally sorted list; this re-sorts within an
        opportunity group to make the selection policy explicit and independent
        of Phase 4's global ordering.
        """
        return sorted(configs, key=SelectionPolicy.selection_key)


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class StrategyDecisionEngine:
    """Phase 5 — Strategy Selection & Signal Generation engine.

    Consumes a Phase 4 :class:`CompatibilityResult` and produces a
    :class:`DecisionResult` containing one :class:`TradingDecision` per
    opportunity that has at least one compatible configuration.

    The engine is deterministic: identical inputs (same compatibility result +
    same data-provider responses) always produce identical decisions.

    Safety:
      * Never creates orders, deployments, or broker interactions.
      * Never allocates capital or manages positions.
      * Fails closed: any evaluation error produces a structured rejection,
        never an accidental BUY/SELL.
    """

    def __init__(
        self,
        config: SelectionConfig,
        *,
        data_provider: Optional[Callable[[str, str], Optional[pd.DataFrame]]] = None,
    ) -> None:
        if not isinstance(config, SelectionConfig):
            raise TypeError("config must be a SelectionConfig")
        self.config = config
        self._provider = data_provider
        self._discovered: bool = False
        self._discovered_ids: list[str] = []

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #

    def _ensure_discovered(self) -> None:
        """Populate the strategy discovery catalog (idempotent)."""
        if self._discovered:
            return
        if not self.config.enabled:
            return
        try:
            clear_discovery()
            discover([self.config.discovery_module])
            self._discovered_ids = registered_strategy_ids()
        except Exception:
            self._discovered_ids = []
        self._discovered = True

    @property
    def discovered_strategy_ids(self) -> list[str]:
        self._ensure_discovered()
        return list(self._discovered_ids)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def generate_decisions(
        self,
        compatibility_result: CompatibilityResult,
    ) -> DecisionResult:
        """Produce structured trading decisions for every opportunity.

        Parameters
        ----------
        compatibility_result:
            Phase 4 output — the compatible configurations and exclusions
            for all ranked opportunities.

        Returns
        -------
        DecisionResult with one TradingDecision per opportunity that had at
        least one compatible configuration.  Opportunities with no compatible
        configurations are omitted (Phase 4 already excluded them).
        """
        now = datetime.now(UTC)
        self._ensure_discovered()

        # Fail-closed: when disabled, produce no decisions at all.
        if not self.config.enabled:
            result_id = _compute_result_id(
                compatibility_result.ranking_id,
                compatibility_result.scan_id,
                self.config.config_snapshot(),
            )
            return DecisionResult(
                result_id=result_id,
                ranking_id=compatibility_result.ranking_id,
                scan_id=compatibility_result.scan_id,
                evaluated_at=now.isoformat(),
                decisions=[],
                config_snapshot=self.config.config_snapshot(),
            )

        # Group compatible configs by opportunity.
        groups = self._group_by_opportunity(compatibility_result.compatible)

        decisions: list[TradingDecision] = []
        for (symbol, rank), configs in groups.items():
            decision = self._process_opportunity(
                symbol=symbol,
                rank=rank,
                configs=configs,
                compat_result=compatibility_result,
            )
            decisions.append(decision)

        result_id = _compute_result_id(
            compatibility_result.ranking_id,
            compatibility_result.scan_id,
            self.config.config_snapshot(),
        )

        return DecisionResult(
            result_id=result_id,
            ranking_id=compatibility_result.ranking_id,
            scan_id=compatibility_result.scan_id,
            evaluated_at=now.isoformat(),
            decisions=decisions,
            config_snapshot=self.config.config_snapshot(),
        )

    # ------------------------------------------------------------------ #
    # Opportunity processing
    # ------------------------------------------------------------------ #

    def _group_by_opportunity(
        self,
        compatible: list[StrategyCompatibility],
    ) -> dict[tuple[str, int], list[StrategyCompatibility]]:
        """Group compatible configs by (opportunity_symbol, opportunity_rank).

        Within each group, configs are re-sorted using the explicit selection
        policy so the selection is deterministic and independent of Phase 4's
        global ordering.
        """
        raw: dict[tuple[str, int], list[StrategyCompatibility]] = {}
        for cfg in compatible:
            key = (cfg.opportunity_symbol, cfg.opportunity_rank)
            raw.setdefault(key, []).append(cfg)
        return {k: SelectionPolicy.sort_group(v) for k, v in raw.items()}

    def _process_opportunity(
        self,
        *,
        symbol: str,
        rank: int,
        configs: list[StrategyCompatibility],
        compat_result: CompatibilityResult,
    ) -> TradingDecision:
        """Select, evaluate, and build a decision for one opportunity."""
        # Phase 5 step 1: select the best configuration.
        selected, selection_factors = self._select_configuration(configs)

        # Determine the authoritative market timestamp.
        market_timestamp = configs[0].market_timestamp or ""

        # Phase 5 step 2: build the selection / rejection.
        if selected is None:
            snapshot_identity = _compute_snapshot_identity_empty(symbol, market_timestamp)
            decision_id = _compute_decision_id(
                compat_result.ranking_id, symbol, rank, market_timestamp,
                None, snapshot_identity,
            )
            return TradingDecision(
                decision_id=decision_id,
                scan_id=compat_result.scan_id,
                ranking_id=compat_result.ranking_id,
                opportunity_symbol=symbol,
                opportunity_rank=rank,
                market_timestamp=market_timestamp,
                snapshot_identity=snapshot_identity,
                selected_configuration=None,
                selection_factors=[],
                signal=None,
                evaluation_error=None,
                exclusion_reason=DecisionExclusionReason.NO_COMPATIBLE_CONFIGURATION.value,
                exclusion_detail="no compatible configuration survived Phase 4 selection",
                is_valid=False,
                status=DecisionStatus.REJECTED.value,
                decision_timestamp=datetime.now(UTC).isoformat(),
                config_snapshot=self.config.config_snapshot(),
            )

        # Phase 5 step 3: evaluate the selected strategy.
        signal, eval_error, snapshot_identity = self._evaluate_selected_strategy(
            symbol=symbol,
            selected=selected,
            market_timestamp=market_timestamp,
        )

        # Decision identity (deterministic from immutable inputs).
        decision_id = _compute_decision_id(
            compat_result.ranking_id, symbol, rank, market_timestamp,
            selected, snapshot_identity,
        )

        is_valid = signal is not None and eval_error is None
        status = DecisionStatus.VALID.value if is_valid else DecisionStatus.REJECTED.value

        # If evaluation failed, carry the structured exclusion reason.
        exclusion_reason = None
        exclusion_detail = ""
        if not is_valid:
            exclusion_reason = DecisionExclusionReason.EVALUATION_ERROR.value
            exclusion_detail = eval_error or "strategy evaluation failed"

        return TradingDecision(
            decision_id=decision_id,
            scan_id=compat_result.scan_id,
            ranking_id=compat_result.ranking_id,
            opportunity_symbol=symbol,
            opportunity_rank=rank,
            market_timestamp=market_timestamp,
            snapshot_identity=snapshot_identity,
            selected_configuration=selected,
            selection_factors=selection_factors,
            signal=signal,
            evaluation_error=eval_error,
            exclusion_reason=exclusion_reason,
            exclusion_detail=exclusion_detail,
            is_valid=is_valid,
            status=status,
            decision_timestamp=datetime.now(UTC).isoformat(),
            config_snapshot=self.config.config_snapshot(),
        )

    # ------------------------------------------------------------------ #
    # Strategy selection (Phase 5 step 1)
    # ------------------------------------------------------------------ #

    def _select_configuration(
        self,
        configs: list[StrategyCompatibility],
    ) -> tuple[Optional[SelectedConfiguration], list[SelectionFactor]]:
        """Deterministically select the best configuration from a group.

        The group is already sorted by the SelectionPolicy.  The first entry
        wins.  Returns (SelectedConfiguration | None, factors).

        ``None`` is returned when the group is empty — this should not occur
        because we only create groups from non-empty compatible lists, but the
        guard is explicit (fail-closed).
        """
        if not configs:
            return None, []

        best = configs[0]

        factors = [
            SelectionFactor(
                name="compatibility_score",
                observed_value=round(best.compatibility_score, 6),
                required_value="highest among candidates",
                matched=True,
            ),
            SelectionFactor(
                name="strategy_id",
                observed_value=best.strategy_id,
                required_value="alphabetically first on tie",
                matched=True,
            ),
            SelectionFactor(
                name="timeframe",
                observed_value=best.timeframe,
                required_value="shortest chronological on tie",
                matched=True,
            ),
            SelectionFactor(
                name="data_adequacy",
                observed_value=best.data_points,
                required_value=f">= {best.min_required_bars}",
                matched=best.data_points >= best.min_required_bars,
            ),
            SelectionFactor(
                name="family_profile",
                observed_value=best.strategy_family,
                required_value="has compatibility profile",
                matched=True,
            ),
        ]

        selected = SelectedConfiguration(
            strategy_id=best.strategy_id,
            strategy_version=best.strategy_version,
            strategy_family=best.strategy_family,
            timeframe=best.timeframe,
            compatibility_score=best.compatibility_score,
            compatibility_order=0,
            score_components=best.score_components,
            freshness_factor=best.freshness_factor,
            data_points=best.data_points,
            min_required_bars=best.min_required_bars,
            selection_factors=factors,
        )

        return selected, factors

    # ------------------------------------------------------------------ #
    # Strategy evaluation (Phase 5 step 2)
    # ------------------------------------------------------------------ #

    def _evaluate_selected_strategy(
        self,
        *,
        symbol: str,
        selected: SelectedConfiguration,
        market_timestamp: str,
    ) -> tuple[Optional[StrategySignal], Optional[str], str]:
        """Evaluate the selected strategy on look-ahead-safe market data.

        Returns ``(signal, error, snapshot_identity)``.  Exactly one of
        ``signal`` / ``error`` is non-None on return.
        """
        if self._provider is None:
            return (
                None,
                DecisionExclusionReason.NO_DATA_PROVIDER.value,
                _compute_snapshot_identity_empty(symbol, market_timestamp),
            )

        # --- Resolve strategy instance from discovery ---
        try:
            strategy = build_from_discovery(selected.strategy_id)
        except KeyError:
            return (
                None,
                DecisionExclusionReason.STRATEGY_NOT_FOUND.value,
                _compute_snapshot_identity_empty(symbol, market_timestamp),
            )
        except Exception as exc:
            return (
                None,
                f"{DecisionExclusionReason.EVALUATION_ERROR.value}: {exc}",
                _compute_snapshot_identity_empty(symbol, market_timestamp),
            )

        # Verify version matches the Phase 4 compatibility record.
        actual_version = strategy.metadata.version
        if actual_version != selected.strategy_version:
            return (
                None,
                f"{DecisionExclusionReason.INVALID_STRATEGY_OUTPUT.value}: "
                f"version mismatch: result={selected.strategy_version} "
                f"actual={actual_version}",
                _compute_snapshot_identity_empty(symbol, market_timestamp),
            )

        # --- Build look-ahead-safe MarketState ---
        bars, build_error = self._build_market_state(
            symbol=symbol,
            timeframe=selected.timeframe,
            market_timestamp=market_timestamp,
            min_bars=max(
                selected.min_required_bars,
                capabilities_from(strategy).minimum_bars,
            ),
        )
        if bars is None:
            reason = build_error or DecisionExclusionReason.INSUFFICIENT_DATA.value
            return (
                None,
                reason,
                _compute_snapshot_identity_empty(symbol, market_timestamp),
            )

        snapshot_identity = _compute_snapshot_identity(
            symbol, selected.timeframe, market_timestamp, bars,
        )

        state = MarketState(
            symbol=symbol,
            timeframe=selected.timeframe,
            timestamp=bars.index[-1],
            bars=bars,
            position=None,
            lookahead_safe=True,
        )

        # --- Evaluate via the existing strategy engine ---
        try:
            runtime = StrategyRuntime(
                strategy,
                MarketContext(
                    instrument=symbol,
                    timeframe=selected.timeframe,
                    bar_count=int(len(bars)),
                    data_available=[DataRequirement.OHLCV],
                ),
                strict_compatibility=False,
            )
            raw_signal = runtime.evaluate(state)
        except Exception as exc:
            return (
                None,
                f"{DecisionExclusionReason.EVALUATION_ERROR.value}: {exc}",
                snapshot_identity,
            )

        # --- Validate the signal structure ---
        errors = validate_signal(raw_signal)
        if errors:
            return (
                None,
                f"{DecisionExclusionReason.INVALID_STRATEGY_OUTPUT.value}: "
                f"signal validation failed: {'; '.join(errors)}",
                snapshot_identity,
            )

        return raw_signal, None, snapshot_identity

    # ------------------------------------------------------------------ #
    # Market-state construction (look-ahead-safe)
    # ------------------------------------------------------------------ #

    def _build_market_state(
        self,
        *,
        symbol: str,
        timeframe: str,
        market_timestamp: str,
        min_bars: int,
    ) -> tuple[Optional[pd.DataFrame], Optional[str]]:
        """Fetch, validate, and filter OHLCV bars for ``MarketState``.

        Look-ahead protection:
          * Bars are fetched from the injected data provider (no hidden
            fetches).
          * The authoritative timestamp boundary is ``market_timestamp``
            (carried from Phase 2 through Phase 4).
          * Only bars with timestamp <= ``market_timestamp`` are retained.
          * The returned frame's last bar timestamp becomes the decision
            timestamp (enforced by ``MarketState.__post_init__``).
        """
        # --- Fetch ---
        try:
            df = self._provider(symbol, timeframe)
        except Exception as exc:
            return None, (
                f"{DecisionExclusionReason.EVALUATION_ERROR.value}: "
                f"data provider error: {exc}"
            )

        if not isinstance(df, pd.DataFrame) or len(df) == 0:
            return (
                None,
                f"{DecisionExclusionReason.INSUFFICIENT_DATA.value}: "
                f"no market data for {symbol}@{timeframe}",
            )

        # --- Validate (reuse repository's authoritative OHLCV rules) ---
        try:
            report = validate_ohlcv(df, timeframe)
        except Exception as exc:
            return None, (
                f"{DecisionExclusionReason.INVALID_MARKET_SNAPSHOT.value}: "
                f"data validation error: {exc}"
            )

        if not report.ok:
            detail = _summarize_issues(report.issues)
            return None, (
                f"{DecisionExclusionReason.INVALID_MARKET_SNAPSHOT.value}: "
                f"{detail}"
            )

        valid = report.valid
        if len(valid) == 0:
            return (
                None,
                f"{DecisionExclusionReason.INVALID_MARKET_SNAPSHOT.value}: "
                f"no valid rows after OHLCV validation",
            )

        # --- Convert to tz-aware DatetimeIndex ---
        ts = pd.to_datetime(valid["timestamp"], utc=True, errors="coerce")
        if ts.isna().any():
            return None, (
                f"{DecisionExclusionReason.INVALID_MARKET_SNAPSHOT.value}: "
                f"unparseable timestamps"
            )
        bars = valid.set_index(ts)
        # Drop the timestamp column so only OHLCV remain.
        bars = bars.drop(columns=["timestamp"], errors="ignore")
        # Ensure required columns.
        required = {"open", "high", "low", "close", "volume"}
        if not required.issubset(set(bars.columns)):
            missing = sorted(required - set(bars.columns))
            return None, (
                f"{DecisionExclusionReason.INVALID_MARKET_SNAPSHOT.value}: "
                f"missing OHLCV columns: {missing}"
            )
        # Sort by index (chronological).
        bars = bars.sort_index()

        # --- Look-ahead protection: filter to <= market_timestamp ---
        if not market_timestamp:
            return None, (
                f"{DecisionExclusionReason.MISSING_SNAPSHOT.value}: "
                f"market_timestamp is required for look-ahead protection"
            )

        filter_ts = pd.Timestamp(market_timestamp)
        if filter_ts.tzinfo is None:
            filter_ts = filter_ts.tz_localize("UTC")
        else:
            filter_ts = filter_ts.tz_convert("UTC")

        # Reject if the latest bar is in the future relative to the boundary.
        if bars.index[-1] > filter_ts:
            # Keep only bars at or before the boundary.
            bars = bars.loc[bars.index <= filter_ts]

        if len(bars) == 0:
            return (
                None,
                f"{DecisionExclusionReason.INSUFFICIENT_DATA.value}: "
                f"no bars at or before market_timestamp {market_timestamp}",
            )

        # --- Insufficient-data gate ---
        if len(bars) < min_bars:
            return None, (
                f"{DecisionExclusionReason.INSUFFICIENT_DATA.value}: "
                f"strategy requires >= {min_bars} bars, "
                f"only {len(bars)} available at or before market_timestamp"
            )

        # --- Numeric sanity (no NaN/inf in close) ---
        close = bars["close"]
        if close.isna().any() or not np.isfinite(close).all():
            return None, (
                f"{DecisionExclusionReason.INVALID_MARKET_SNAPSHOT.value}: "
                f"NaN or infinite values in close prices"
            )

        return bars, None


def _summarize_issues(issues: list[Any]) -> str:
    parts = [f"{iss.code}: {iss.message}" for iss in issues]
    return "; ".join(parts) if parts else "data failed validation"


def _compute_snapshot_identity_empty(
    symbol: str, market_timestamp: str
) -> str:
    """Snapshot identity when no OHLCV data was available (empty bar set)."""
    payload = {
        "symbol": symbol,
        "market_timestamp": market_timestamp,
        "bar_count": 0,
        "close_prices": [],
        "bar_timestamps": [],
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(("phase5_snapshot:" + blob).encode("utf-8")).hexdigest()[:64]
