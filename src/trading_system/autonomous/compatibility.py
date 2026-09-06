"""Strategy & Timeframe Compatibility Evaluation -- Phase 4.

Deterministic, paper-only compatibility layer that answers:

    Given a ranked market opportunity (Phase 3 result), which existing
    strategies and timeframes are compatible with the observed market
    conditions?

This layer deliberately stops at *compatibility evaluation*. It does NOT:
  - execute trades
  - create deployments
  - create orders
  - select one final strategy for execution
  - allocate capital
  - manage positions

Dependency direction (Phase 4 boundary)::

    Ranked Opportunities (Phase 3, from Phase 2 snapshot)
        ↓
    Strategy Registry (existing strategy_factory.discovery)
        ↓
    Strategy Compatibility Evaluator (Phase 4)
        ↓
    Compatible Configurations

Reused existing abstractions:
  - StrategyFamily (strategy_factory.metadata) — explicit, strategy-owned taxonomy
  - StrategyMetadata (strategy_factory.metadata) — eligibility, timeframes, min history
  - TRADING_PERIODS (analysis.quant) — supported timeframe formats
  - OpportunityFeatures (autonomous.ranker) — market-condition features from Phase 3
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trading_system.analysis.quant import TRADING_PERIODS
from trading_system.strategy_factory.discovery import (
    discover,
    get_strategy_class,
    registered_strategy_ids,
)
from trading_system.strategy_factory.metadata import StrategyFamily, StrategyMetadata
from trading_system.strategy_factory.validation import validate_metadata

from .ranker import OpportunityFeatures, OpportunityRankingResult

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #

class CompatibilityExclusionReason(str, Enum):
    """Structured reasons a strategy × timeframe combination was excluded."""

    STRATEGY_NOT_FOUND = "strategy_not_found"
    STRATEGY_INELIGIBLE = "strategy_ineligible"
    UNSUPPORTED_TIMEFRAME = "unsupported_timeframe"
    INSUFFICIENT_MARKET_DATA = "insufficient_market_data"
    INCOMPATIBLE_MARKET_CONDITIONS = "incompatible_market_conditions"
    INVALID_OPPORTUNITY = "invalid_opportunity"


class FeatureDirection(str, Enum):
    """Directional preference of a strategy family for a market feature.

    These are family-level preferences derived from the explicit
    ``StrategyFamily`` taxonomy (NOT inferred from strategy ID or name).
    """

    HIGH = "high"
    LOW = "low"
    CENTERED = "centered"
    NEUTRAL = "neutral"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

class CompatibilityConfig(BaseModel):
    """Typed configuration for :class:`StrategyCompatibilityEvaluator`.

    Weights are non-negative; at least one feature weight must be positive.
    All thresholds are documented defaults.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True

    top_n: Optional[int] = Field(default=None, ge=1)

    # Timeframes to evaluate (validated against TRADING_PERIODS).
    timeframes: tuple[str, ...] = ("5m", "15m", "1h", "1d")

    # Minimum compatibility score to be considered "compatible" [0, 1].
    min_compatibility_score: float = Field(default=0.0, ge=0.0, le=1.0)

    # Feature weights for the weighted-sum compatibility score.
    trend_weight: float = Field(default=0.30, ge=0.0)
    momentum_weight: float = Field(default=0.25, ge=0.0)
    volatility_weight: float = Field(default=0.25, ge=0.0)
    liquidity_weight: float = Field(default=0.20, ge=0.0)

    # Discovery module to import for finding registered strategies.
    discovery_module: str = "trading_system.strategy_factory.builtin"

    @model_validator(mode="after")
    def _validate_all(self) -> "CompatibilityConfig":
        unknown = [t for t in self.timeframes if t not in TRADING_PERIODS]
        if unknown:
            raise ValueError(
                f"unsupported timeframe(s): {unknown}; "
                f"supported: {sorted(TRADING_PERIODS)}"
            )
        weights = [
            self.trend_weight,
            self.momentum_weight,
            self.volatility_weight,
            self.liquidity_weight,
        ]
        if all(w == 0.0 for w in weights):
            raise ValueError("at least one feature weight must be positive")
        return self

    def normalized_weights(self) -> dict[str, float]:
        """Feature weights scaled to sum to 1.0."""
        raw = {
            "trend": self.trend_weight,
            "momentum": self.momentum_weight,
            "volatility": self.volatility_weight,
            "liquidity": self.liquidity_weight,
        }
        total = sum(raw.values())
        if total <= 0:
            return {k: 0.25 for k in raw}
        return {k: v / total for k, v in raw.items()}

    def config_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "timeframes": list(self.timeframes),
            "top_n": self.top_n,
            "min_compatibility_score": self.min_compatibility_score,
            "weights": self.normalized_weights(),
            "discovery_module": self.discovery_module,
        }


# --------------------------------------------------------------------------- #
# Strategy Compatibility Profiles
# --------------------------------------------------------------------------- #

class StrategyCompatibilityProfile(BaseModel):
    """Explicit, typed market-condition compatibility profile for a family.

    Declares which market-condition features a strategy family prefers and in
    which direction (high/low/centered/neutral).  This is the *smallest*
    explicit boundary: it keys on the repository's existing ``StrategyFamily``
    taxonomy (strategy-owned via ``StrategyMetadata.family``), never on
    strategy ID/name.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    family: StrategyFamily
    feature_directions: dict[str, FeatureDirection]
    min_history_bars: int = Field(default=20, ge=1)

    @property
    def supported_features(self) -> list[str]:
        return list(self.feature_directions.keys())

    def config_snapshot(self) -> dict[str, Any]:
        return {
            "family": self.family.value,
            "feature_directions": {
                k: v.value for k, v in self.feature_directions.items()
            },
            "min_history_bars": self.min_history_bars,
        }


# The explicit, auditable family → profile registry.
# Family is an explicit, strategy-owned classification (StrategyMetadata.family).
# Preferences are family-level, not inferred from strategy ID or name.
_FAMILY_PROFILES: dict[StrategyFamily, StrategyCompatibilityProfile] = {
    StrategyFamily.TREND: StrategyCompatibilityProfile(
        family=StrategyFamily.TREND,
        feature_directions={
            "trend": FeatureDirection.HIGH,
            "momentum": FeatureDirection.HIGH,
            "volatility": FeatureDirection.NEUTRAL,
            "liquidity": FeatureDirection.HIGH,
        },
        min_history_bars=20,
    ),
    StrategyFamily.MOMENTUM: StrategyCompatibilityProfile(
        family=StrategyFamily.MOMENTUM,
        feature_directions={
            "trend": FeatureDirection.NEUTRAL,
            "momentum": FeatureDirection.HIGH,
            "volatility": FeatureDirection.NEUTRAL,
            "liquidity": FeatureDirection.HIGH,
        },
        min_history_bars=20,
    ),
    StrategyFamily.MEAN_REVERSION: StrategyCompatibilityProfile(
        family=StrategyFamily.MEAN_REVERSION,
        feature_directions={
            "trend": FeatureDirection.CENTERED,
            "momentum": FeatureDirection.CENTERED,
            "volatility": FeatureDirection.LOW,
            "liquidity": FeatureDirection.HIGH,
        },
        min_history_bars=20,
    ),
    StrategyFamily.BREAKOUT: StrategyCompatibilityProfile(
        family=StrategyFamily.BREAKOUT,
        feature_directions={
            "trend": FeatureDirection.HIGH,
            "momentum": FeatureDirection.HIGH,
            "volatility": FeatureDirection.HIGH,
            "liquidity": FeatureDirection.HIGH,
        },
        min_history_bars=20,
    ),
    StrategyFamily.VOLATILITY: StrategyCompatibilityProfile(
        family=StrategyFamily.VOLATILITY,
        feature_directions={
            "trend": FeatureDirection.NEUTRAL,
            "momentum": FeatureDirection.NEUTRAL,
            "volatility": FeatureDirection.HIGH,
            "liquidity": FeatureDirection.HIGH,
        },
        min_history_bars=20,
    ),
    StrategyFamily.MARKET_STRUCTURE: StrategyCompatibilityProfile(
        family=StrategyFamily.MARKET_STRUCTURE,
        feature_directions={
            "trend": FeatureDirection.NEUTRAL,
            "momentum": FeatureDirection.NEUTRAL,
            "volatility": FeatureDirection.NEUTRAL,
            "liquidity": FeatureDirection.HIGH,
        },
        min_history_bars=20,
    ),
}


def get_family_profile(family: StrategyFamily) -> Optional[StrategyCompatibilityProfile]:
    """Look up the compatibility profile for a strategy family."""
    return _FAMILY_PROFILES.get(family)


# --------------------------------------------------------------------------- #
# Result models
# --------------------------------------------------------------------------- #

class CompatibilityFeatures(BaseModel):
    """Market-condition features extracted from a Phase 3 ranked opportunity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trend_score: float = Field(ge=0.0, le=1.0)
    momentum_score: float = Field(ge=0.0, le=1.0)
    volatility_score: float = Field(ge=0.0, le=1.0)
    liquidity_score: float = Field(ge=0.0, le=1.0)


class StrategyCompatibility(BaseModel):
    """Result of evaluating one strategy × timeframe against one opportunity.

    A single compatibility evaluation entry — one row in the final sorted
    result set.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy_id: str
    strategy_version: str
    strategy_family: str
    timeframe: str

    opportunity_symbol: str
    opportunity_rank: int

    compatibility_score: float = Field(ge=0.0, le=1.0)
    features: CompatibilityFeatures
    score_components: dict[str, float]
    freshness_factor: float = Field(ge=0.0, le=1.0)

    market_timestamp: Optional[str] = None
    ranking_id: Optional[str] = None
    scan_id: Optional[str] = None
    data_points: int = Field(default=0, ge=0)
    min_required_bars: int = Field(default=0, ge=0)


class CompatibilityExclusion(BaseModel):
    """A strategy × timeframe combination that was excluded from compatibility."""

    model_config = ConfigDict(extra="forbid")

    opportunity_symbol: str
    opportunity_rank: int
    strategy_id: str
    timeframe: str
    reason: CompatibilityExclusionReason
    detail: str = ""


class CompatibilityResult(BaseModel):
    """Full, auditable result of a Phase 4 compatibility evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: str
    ranking_id: Optional[str] = None
    scan_id: Optional[str] = None
    evaluated_at: str
    evaluated_count: int = Field(default=0, ge=0)
    compatible: list[StrategyCompatibility] = Field(default_factory=list)
    exclusions: list[CompatibilityExclusion] = Field(default_factory=list)
    config_snapshot: dict[str, Any] = Field(default_factory=dict)

    @property
    def compatible_count(self) -> int:
        return len(self.compatible)

    @property
    def excluded_count(self) -> int:
        return len(self.exclusions)

    @property
    def has_compatible(self) -> bool:
        return self.compatible_count > 0


# --------------------------------------------------------------------------- #
# Score helpers
# --------------------------------------------------------------------------- #

def _match_factor(direction: FeatureDirection, value: float) -> float:
    """Compute match score [0, 1] for a feature given its preference direction."""
    if direction == FeatureDirection.HIGH:
        return max(0.0, min(1.0, value))
    elif direction == FeatureDirection.LOW:
        return max(0.0, min(1.0, 1.0 - value))
    elif direction == FeatureDirection.CENTERED:
        d = abs(value - 0.5) * 2.0
        return max(0.0, min(1.0, 1.0 - d))
    else:  # NEUTRAL
        return 1.0


# --------------------------------------------------------------------------- #
# Result identity
# --------------------------------------------------------------------------- #

def _compute_result_id(
    ranking_result: OpportunityRankingResult,
    config: CompatibilityConfig,
    strategy_ids: list[str],
) -> str:
    """Deterministic identity for a compatibility evaluation."""
    payload = {
        "ranking_id": ranking_result.ranking_id,
        "strategies": sorted(strategy_ids),
        "config": config.config_snapshot(),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(
        ("strategy_compatibility:" + blob).encode("utf-8")
    ).hexdigest()[:64]


# --------------------------------------------------------------------------- #
# Evaluator
# --------------------------------------------------------------------------- #

class StrategyCompatibilityEvaluator:
    """Deterministic, paper-only strategy × timeframe compatibility evaluator.

    Consumes a Phase 3 :class:`OpportunityRankingResult` and evaluates which
    registered strategies are compatible with the observed market conditions,
    using only the features already computed in the ranking (no new market-data
    fetches — eliminating snapshot drift).

    Usage::

        evaluator = StrategyCompatibilityEvaluator(CompatibilityConfig())
        result = evaluator.evaluate(ranking_result)
    """

    def __init__(self, config: CompatibilityConfig) -> None:
        if not isinstance(config, CompatibilityConfig):
            raise TypeError("config must be a CompatibilityConfig")
        self.config = config

    # ------------------------------------------------------------------ #
    # Strategy discovery / eligibility
    # ------------------------------------------------------------------ #

    def discover_strategies(self) -> list[str]:
        """Discover registered strategy IDs from the configured discovery module.

        Returns a sorted list of strategy IDs.  Never raises — returns an empty
        list if discovery fails (fail-closed).
        """
        if not self.config.enabled:
            return []
        try:
            discover([self.config.discovery_module])
        except Exception:
            return []
        return sorted(registered_strategy_ids())

    def _resolve_metadata(
        self, strategy_id: str
    ) -> Optional[StrategyMetadata]:
        """Look up a strategy's metadata; return None if not found or invalid."""
        try:
            cls = get_strategy_class(strategy_id)
        except Exception:
            return None
        try:
            meta = cls.metadata
        except AttributeError:
            return None
        if not isinstance(meta, StrategyMetadata):
            return None
        if validate_metadata(meta):
            return None
        return meta

    def _resolve_strategy(
        self,
        strategy_id: str,
        allowed: frozenset[str],
        discovered_ids: list[str],
    ) -> tuple[Optional[StrategyMetadata], Optional[StrategyCompatibilityProfile], str]:
        """Resolve a strategy's metadata and profile.

        Returns (metadata, profile, status) where status is one of:
        'ok', 'not_found', 'ineligible', 'incompatible_family'.
        """
        if strategy_id not in discovered_ids:
            return None, None, "not_found"
        meta = self._resolve_metadata(strategy_id)
        if meta is None:
            return None, None, "not_found"
        if allowed and strategy_id not in allowed:
            return None, None, "ineligible"
        profile = get_family_profile(meta.family)
        if profile is None:
            return meta, None, "incompatible_family"
        return meta, profile, "ok"

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def evaluate(
        self,
        ranking_result: OpportunityRankingResult,
        allowed_strategies: Optional[frozenset[str]] = None,
        discovered_strategies: Optional[list[str]] = None,
    ) -> CompatibilityResult:
        """Evaluate strategy × timeframe compatibility for ranked opportunities.

        Parameters
        ----------
        ranking_result:
            Phase 3 ranking output (contains opportunities with features).
        allowed_strategies:
            Optional frozenset of strategy IDs the operator has approved.
            If empty/None, all discovered strategies are considered.
        discovered_strategies:
            Optional pre-discovered strategy IDs.  If provided, the evaluator
            uses these instead of calling ``discover_strategies()`` internally.
            This supports deterministic testing.

        Returns a :class:`CompatibilityResult` with deterministic ordering.
        """
        now = datetime.now(UTC)
        weights = self.config.normalized_weights()
        allowed = allowed_strategies or frozenset()

        if not self.config.enabled:
            return CompatibilityResult(
                result_id=_compute_result_id(ranking_result, self.config, list(allowed)),
                ranking_id=ranking_result.ranking_id,
                scan_id=ranking_result.scan_id,
                evaluated_at=now.isoformat(),
                evaluated_count=0,
                compatible=[],
                exclusions=[],
                config_snapshot=self.config.config_snapshot(),
            )

        # Discover eligible strategy IDs (or use pre-supplied list).
        if discovered_strategies is not None:
            discovered_ids = sorted(discovered_strategies)
        else:
            discovered_ids = self.discover_strategies()
        if allowed:
            # Include both discovered strategies and allowed IDs (the latter
            # may produce STRATEGY_NOT_FOUND exclusions if not discovered).
            all_target_ids = sorted(set(discovered_ids) | set(allowed))
        else:
            all_target_ids = discovered_ids

        # Resolve profiles for each target strategy.
        resolved: dict[str, tuple[Optional[StrategyMetadata], Optional[StrategyCompatibilityProfile], str]] = {}
        for sid in all_target_ids:
            meta, profile, status = self._resolve_strategy(sid, allowed, discovered_ids)
            resolved[sid] = (meta, profile, status)

        compatible_results: list[StrategyCompatibility] = []
        exclusions: list[CompatibilityExclusion] = []
        evaluated_count = 0

        for opp in ranking_result.opportunities:
            features = opp.features
            cf = CompatibilityFeatures(
                trend_score=features.trend_score,
                momentum_score=features.momentum_score,
                volatility_score=features.volatility_score,
                liquidity_score=features.liquidity_score,
            )

            # Use metadata timeframe as the scan timeframe for data_points check.
            scan_timeframe = opp.metadata.get("timeframe", ranking_result.scan_id or "")
            opp_data_points = opp.metadata.get("data_points", 0)
            opp_market_ts = opp.market_timestamp

            for sid in all_target_ids:
                meta, profile, status = resolved[sid]

                for tf in self.config.timeframes:
                    evaluated_count += 1

                    # --- Strategy not found ---
                    if status == "not_found":
                        exclusions.append(CompatibilityExclusion(
                            opportunity_symbol=opp.symbol,
                            opportunity_rank=opp.rank,
                            strategy_id=sid,
                            timeframe=tf,
                            reason=CompatibilityExclusionReason.STRATEGY_NOT_FOUND,
                            detail=f"strategy {sid!r} is not registered",
                        ))
                        continue

                    # --- Strategy ineligible (not in allowed set) ---
                    if status == "ineligible":
                        exclusions.append(CompatibilityExclusion(
                            opportunity_symbol=opp.symbol,
                            opportunity_rank=opp.rank,
                            strategy_id=sid,
                            timeframe=tf,
                            reason=CompatibilityExclusionReason.STRATEGY_INELIGIBLE,
                            detail=f"strategy {sid!r} is not in the allowed set",
                        ))
                        continue

                    # --- Strategy discovered but no profile ---
                    if status == "incompatible_family":
                        exclusions.append(CompatibilityExclusion(
                            opportunity_symbol=opp.symbol,
                            opportunity_rank=opp.rank,
                            strategy_id=sid,
                            timeframe=tf,
                            reason=CompatibilityExclusionReason.STRATEGY_INELIGIBLE,
                            detail=f"strategy {sid!r} has family {meta.family.value} with no compatibility profile",
                        ))
                        continue

                    assert meta is not None and profile is not None

                    # --- Unsupported timeframe ---
                    if tf not in meta.timeframes:
                        exclusions.append(CompatibilityExclusion(
                            opportunity_symbol=opp.symbol,
                            opportunity_rank=opp.rank,
                            strategy_id=sid,
                            timeframe=tf,
                            reason=CompatibilityExclusionReason.UNSUPPORTED_TIMEFRAME,
                            detail=(
                                f"strategy {sid!r} supports "
                                f"{meta.timeframes}, not {tf!r}"
                            ),
                        ))
                        continue

                    # --- Insufficient data ---
                    if opp_data_points < profile.min_history_bars:
                        exclusions.append(CompatibilityExclusion(
                            opportunity_symbol=opp.symbol,
                            opportunity_rank=opp.rank,
                            strategy_id=sid,
                            timeframe=tf,
                            reason=CompatibilityExclusionReason.INSUFFICIENT_MARKET_DATA,
                            detail=(
                                f"opportunity has {opp_data_points} data points, "
                                f"strategy requires {profile.min_history_bars}"
                            ),
                        ))
                        continue

                    # --- Compute compatibility score ---
                    score, components, freshness = self._compute_compatibility(
                        cf, profile, weights, features.freshness_score,
                    )

                    # --- Threshold check ---
                    if score < self.config.min_compatibility_score:
                        exclusions.append(CompatibilityExclusion(
                            opportunity_symbol=opp.symbol,
                            opportunity_rank=opp.rank,
                            strategy_id=sid,
                            timeframe=tf,
                            reason=CompatibilityExclusionReason.INCOMPATIBLE_MARKET_CONDITIONS,
                            detail=f"compatibility score {score:.4f} < threshold {self.config.min_compatibility_score}",
                        ))
                        continue

                    compatible_results.append(StrategyCompatibility(
                        strategy_id=sid,
                        strategy_version=meta.version,
                        strategy_family=meta.family.value,
                        timeframe=tf,
                        opportunity_symbol=opp.symbol,
                        opportunity_rank=opp.rank,
                        compatibility_score=round(score, 6),
                        features=cf,
                        score_components=components,
                        freshness_factor=round(freshness, 6),
                        market_timestamp=opp_market_ts,
                        ranking_id=ranking_result.ranking_id,
                        scan_id=ranking_result.scan_id,
                        data_points=opp_data_points,
                        min_required_bars=profile.min_history_bars,
                    ))

        # Deterministic ordering: score DESC, strategy_id ASC, timeframe ASC.
        compatible_results.sort(
            key=lambda r: (-r.compatibility_score, r.strategy_id, r.timeframe)
        )

        # Top-N cap.
        top_n = self.config.top_n
        if top_n is not None:
            compatible_results = compatible_results[:top_n]

        result_id = _compute_result_id(ranking_result, self.config, all_target_ids)

        return CompatibilityResult(
            result_id=result_id,
            ranking_id=ranking_result.ranking_id,
            scan_id=ranking_result.scan_id,
            evaluated_at=now.isoformat(),
            evaluated_count=evaluated_count,
            compatible=compatible_results,
            exclusions=exclusions,
            config_snapshot=self.config.config_snapshot(),
        )

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _compute_compatibility(
        self,
        features: CompatibilityFeatures,
        profile: StrategyCompatibilityProfile,
        weights: dict[str, float],
        freshness_score: float,
    ) -> tuple[float, dict[str, float], float]:
        """Compute compatibility score [0, 1] and component breakdown.

        Score = weighted sum of feature matches × freshness_factor.

        Each ``match_i`` is in [0, 1]:
          - HIGH:       value itself
          - LOW:        1 - value
          - CENTERED:   1 - |value - 0.5| × 2
          - NEUTRAL:    1.0
        """
        components: dict[str, float] = {}

        for feature_name, direction in profile.feature_directions.items():
            value = getattr(features, f"{feature_name}_score")
            components[feature_name] = round(_match_factor(direction, value), 6)

        # Liquidity is always HIGH-preference (all strategies prefer liquidity).
        if "liquidity" not in profile.feature_directions:
            components["liquidity"] = round(features.liquidity_score, 6)

        # Weighted sum (weights already normalized to [0, 1]).
        score = 0.0
        for feature_name, weight in weights.items():
            match = components.get(feature_name, 1.0)
            score += weight * match

        # Freshness gate: stale data reduces compatibility.
        score *= freshness_score

        return max(0.0, min(1.0, score)), components, freshness_score
