"""Autonomous Candidate Ranker — Phase 3.

Deterministic, paper-only candidate ranking that produces a ranked list of
market opportunities from a Phase 2 ``MarketScanResult``.

The ranker calculates objective features from OHLCV data using existing
repository indicators, applies a deterministic weighted score, and emits a
self-contained, auditable :class:`OpportunityRankingResult`.

This layer deliberately stops at *opportunity ranking*. It does NOT:
  - select strategies
  - generate trade signals
  - place orders or touch a broker
  - create deployments
  - perform live trading
  - choose trading timeframes

Dependency direction (Phase 3 boundary)::

    Market Data
        ->  Market Scanner (Phase 2)
        ->  MarketScanResult
        ->  Candidate Ranker (Phase 3)
        ->  Ranked Opportunities

Reused existing abstractions (no duplication):
  - trading_system.indicators.indicators.sma, .momentum  (trend / momentum)
  - trading_system.analysis.quant.simple_returns, rolling_volatility  (volatility)
  - trading_system.data.validation.validate_ohlcv  (OHLCV validity rules)
  - trading_system.autonomous.scanner.MarketScanResult, ScanCandidate
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from trading_system.analysis.quant import rolling_volatility, simple_returns
from trading_system.data.validation import validate_ohlcv
from trading_system.indicators.indicators import momentum, sma

from .scanner import MarketScanResult, ScanCandidate

UTC = timezone.utc

# --- Default normalization caps (documented, not magic) ---
DEFAULT_MOMENTUM_CAP = 0.20             # ±20 % over momentum window -> [0, 1]
DEFAULT_TREND_CAP = 0.10               # ±10 % deviation from SMA  -> [0, 1]
DEFAULT_VOLATILITY_CAP = 0.05          # 5 % per-period return vol -> [0, 1]
DEFAULT_LIQUIDITY_CAP = 1e7            # 10 M avg volume            -> [0, 1]
DEFAULT_FRESHNESS_CAP_SECONDS = 7 * 24 * 3600  # 7 days -> [1, 0]

RANKING_VERSION = "phase3-v1"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _clip(value: float, lo: float, hi: float) -> float:
    """Clamp *value* to [lo, hi].  NaN maps to *lo* (conservative)."""
    if value != value:  # NaN check
        return lo
    return max(lo, min(hi, value))


def _normalize_centered(raw: float, cap: float) -> float:
    """Map [-cap, +cap] linearly to [0, 1] (0.5 = neutral / zero)."""
    return _clip((raw + cap) / (2.0 * cap), 0.0, 1.0)


def _normalize_ratio(raw: float, cap: float) -> float:
    """Map [0, cap] monotonically to [0, 1]."""
    return _clip(raw / cap, 0.0, 1.0)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _compute_ranking_id(scan_result: MarketScanResult, cfg: "RankerConfig") -> str:
    """Deterministic identity for a ranking (same scan + config → same id).

    Includes a per-symbol data hash derived from the close prices embedded in
    each candidate at scan time, so that a ranking_id unambiguously identifies
    the market snapshot used — not just the scan config.
    """
    data_hashes: list[str] = []
    for c in sorted(scan_result.candidates, key=lambda c: c.symbol):
        prices = c.metadata.get("close_prices")
        if prices is not None:
            digest = hashlib.sha256(
                json.dumps(prices, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:32]
            data_hashes.append(f"{c.symbol}:{digest}")
        else:
            data_hashes.append(f"{c.symbol}:no_embedded_data")
    payload = {
        "scan_id": scan_result.scan_id,
        "symbols": scan_result.symbols_eligible,
        "config": cfg.config_snapshot(),
        "data_hashes": data_hashes,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(("opportunity_ranking:" + blob).encode("utf-8")).hexdigest()[:64]


# --------------------------------------------------------------------------- #
# Enums / rejection reasons
# --------------------------------------------------------------------------- #

class RankExclusionReason(str, Enum):
    """Structured reasons a candidate was excluded from ranking."""

    INSUFFICIENT_DATA = "insufficient_data"
    MISSING_MARKET_DATA = "missing_market_data"
    INVALID_FEATURE = "invalid_feature"
    COMPUTATION_ERROR = "computation_error"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

class RankerConfig(BaseModel):
    """Typed configuration for :class:`OpportunityRanker`.

    Weights are non-negative and at least one must be positive.  All
    normalization caps are documented defaults, not magic numbers.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True

    top_n: Optional[int] = Field(default=None, ge=1)

    # Feature weights (non-negative; at least one positive)
    momentum_weight: float = Field(default=0.30, ge=0.0)
    trend_weight: float = Field(default=0.20, ge=0.0)
    volatility_weight: float = Field(default=0.20, ge=0.0)
    liquidity_weight: float = Field(default=0.15, ge=0.0)
    freshness_weight: float = Field(default=0.15, ge=0.0)

    # Lookback windows for OHLCV-derived features
    momentum_window: int = Field(default=5, ge=1)
    trend_window: int = Field(default=20, ge=1)
    volatility_window: int = Field(default=20, ge=1)

    # Normalization caps
    momentum_cap: float = Field(default=DEFAULT_MOMENTUM_CAP, gt=0.0)
    trend_cap: float = Field(default=DEFAULT_TREND_CAP, gt=0.0)
    volatility_cap: float = Field(default=DEFAULT_VOLATILITY_CAP, gt=0.0)
    liquidity_cap: float = Field(default=DEFAULT_LIQUIDITY_CAP, gt=0.0)
    freshness_cap_seconds: float = Field(
        default=DEFAULT_FRESHNESS_CAP_SECONDS, gt=0.0
    )

    data_provider_source: str = Field(default="candidate_ranker")

    @model_validator(mode="after")
    def _validate_weights(self) -> "RankerConfig":
        weights = [
            self.momentum_weight,
            self.trend_weight,
            self.volatility_weight,
            self.liquidity_weight,
            self.freshness_weight,
        ]
        if all(w == 0.0 for w in weights):
            raise ValueError("at least one feature weight must be positive")
        return self

    # -- derived helpers --------------------------------------------------

    def normalized_weights(self) -> dict[str, float]:
        """Weights scaled so they sum to 1.0."""
        raw = {
            "momentum": self.momentum_weight,
            "trend": self.trend_weight,
            "volatility": self.volatility_weight,
            "liquidity": self.liquidity_weight,
            "freshness": self.freshness_weight,
        }
        total = sum(raw.values())
        if total <= 0:
            return {k: 0.0 for k in raw}
        return {k: v / total for k, v in raw.items()}

    def min_required_bars(self) -> int:
        """Minimum OHLCV bars needed for every enabled feature."""
        needs: list[int] = []
        if self.momentum_weight > 0:
            needs.append(self.momentum_window + 1)
        if self.trend_weight > 0:
            needs.append(self.trend_window)
        if self.volatility_weight > 0:
            needs.append(self.volatility_window + 1)
        if not needs:
            return 1
        return max(needs)

    def config_snapshot(self) -> dict[str, Any]:
        return {
            "top_n": self.top_n,
            "weights": self.normalized_weights(),
            "windows": {
                "momentum": self.momentum_window,
                "trend": self.trend_window,
                "volatility": self.volatility_window,
            },
            "caps": {
                "momentum": self.momentum_cap,
                "trend": self.trend_cap,
                "volatility": self.volatility_cap,
                "liquidity": self.liquidity_cap,
                "freshness_seconds": self.freshness_cap_seconds,
            },
            "min_required_bars": self.min_required_bars(),
        }


# --------------------------------------------------------------------------- #
# Result models
# --------------------------------------------------------------------------- #

class OpportunityFeatures(BaseModel):
    """Normalized feature values for a single candidate.

    Every score is in ``[0.0, 1.0]``:
      - ``momentum_score``: price momentum centered at 0.5 (neutral)
      - ``trend_score``:    price vs SMA, centered at 0.5
      - ``volatility_score``: rolling return vol, 0 → no vol, 1 → cap
      - ``liquidity_score``: avg volume, 0 → illiquid, 1 → cap
      - ``freshness_score``: data freshness, 1 → fresh, 0 → stale
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    market_timestamp: Optional[str] = None
    momentum_score: float = 0.0
    trend_score: float = 0.0
    volatility_score: float = 0.0
    liquidity_score: float = 0.0
    freshness_score: float = 0.0

    @model_validator(mode="after")
    def _check_bounds(self) -> "OpportunityFeatures":
        for name in (
            "momentum_score",
            "trend_score",
            "volatility_score",
            "liquidity_score",
            "freshness_score",
        ):
            val = getattr(self, name)
            if val != val:  # NaN
                raise ValueError(f"{name} must not be NaN")
            if val < 0.0 or val > 1.0:
                raise ValueError(f"{name} must be in [0, 1], got {val}")
        return self

    @property
    def components(self) -> dict[str, float]:
        return {
            "momentum": self.momentum_score,
            "trend": self.trend_score,
            "volatility": self.volatility_score,
            "liquidity": self.liquidity_score,
            "freshness": self.freshness_score,
        }


class RankedOpportunity(BaseModel):
    """A candidate with its opportunity score, features, and audit trail."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rank: int
    symbol: str
    opportunity_score: float
    features: OpportunityFeatures
    scan_id: Optional[str] = None
    market_timestamp: Optional[str] = None
    score_components: dict[str, float] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RankExclusion(BaseModel):
    """A candidate excluded from ranking, with a structured reason."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scan_id: Optional[str] = None
    symbol: str
    reason: RankExclusionReason
    detail: str = ""
    market_timestamp: Optional[str] = None


class OpportunityRankingResult(BaseModel):
    """Self-contained, deterministic, auditable ranking result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ranking_id: str
    scan_id: Optional[str] = None
    ranking_timestamp: str
    ranking_version: str = RANKING_VERSION
    candidates_evaluated: int
    opportunities: list[RankedOpportunity] = Field(default_factory=list)
    exclusions: list[RankExclusion] = Field(default_factory=list)
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    score_range: tuple[float, float] = (0.0, 0.0)
    data_provider_source: str = "unknown"

    @property
    def ranked_symbols(self) -> list[str]:
        return [o.symbol for o in self.opportunities]

    @property
    def excluded_symbols(self) -> list[str]:
        return [e.symbol for e in self.exclusions]

    def opportunity_for(self, symbol: str) -> Optional[RankedOpportunity]:
        for opp in self.opportunities:
            if opp.symbol == symbol:
                return opp
        return None

    def is_deterministic_with(self, other: "OpportunityRankingResult") -> bool:
        """True when two rankings share identity and produced identical order."""
        return (
            self.ranking_id == other.ranking_id
            and self.ranked_symbols == other.ranked_symbols
            and [e.symbol for e in self.exclusions]
            == [e.symbol for e in other.exclusions]
        )


# --------------------------------------------------------------------------- #
# Ranker
# --------------------------------------------------------------------------- #

class OpportunityRanker:
    """Deterministic candidate ranker.

    Accepts a :class:`MarketScanResult` (from Phase 2) and an optional
    data-provider callable for fetching raw OHLCV data needed by feature
    calculations.  The ranker does NOT re-scan the market — it ranks only the
    candidates already identified by the scan.

    Usage::

        ranker = OpportunityRanker(config, data_provider=cc.load_market_data)
        result = ranker.rank(scan_result)
    """

    def __init__(
        self,
        config: RankerConfig,
        data_provider: Optional[Callable[[str, str], Optional[pd.DataFrame]]] = None,
    ) -> None:
        if not isinstance(config, RankerConfig):
            raise TypeError("config must be a RankerConfig")
        self.config = config
        self._provider = data_provider

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def rank(self, scan_result: MarketScanResult) -> OpportunityRankingResult:
        """Rank eligible candidates from *scan_result* into ordered opportunities."""
        scan_ts = _now_utc()
        ranking_id = _compute_ranking_id(scan_result, self.config)

        if not self.config.enabled:
            return OpportunityRankingResult(
                ranking_id=ranking_id,
                scan_id=scan_result.scan_id,
                ranking_timestamp=scan_ts.isoformat(),
                candidates_evaluated=len(scan_result.candidates),
                opportunities=[],
                exclusions=[],
                config_snapshot=self.config.config_snapshot(),
                score_range=(0.0, 0.0),
                data_provider_source=self.config.data_provider_source,
            )

        # Deterministic evaluation order (alphabetical by symbol).
        candidates = sorted(scan_result.candidates, key=lambda c: c.symbol)
        weights = self.config.normalized_weights()
        min_bars = self.config.min_required_bars()

        scored: list[tuple[ScanCandidate, OpportunityFeatures, dict[str, float], float]] = []
        exclusions: list[RankExclusion] = []

        for candidate in candidates:
            features, exclusion = self._evaluate_candidate(
                candidate, scan_result, min_bars
            )
            if exclusion is not None:
                exclusions.append(exclusion)
                continue
            assert features is not None
            score = self._compute_score(features, weights)
            components = {
                k: round(w * features.components[k], 6)
                for k, w in weights.items()
            }
            scored.append((candidate, features, components, score))

        # Deterministic ranking: score DESC, symbol ASC.
        scored.sort(key=lambda x: (-x[3], x[0].symbol))

        # Top-N cap.
        top_n = self.config.top_n
        if top_n is not None:
            scored = scored[:top_n]

        opportunities: list[RankedOpportunity] = []
        all_scores: list[float] = []
        for rank, (candidate, features, components, score) in enumerate(scored, 1):
            opportunities.append(
                RankedOpportunity(
                    rank=rank,
                    symbol=candidate.symbol,
                    opportunity_score=round(score, 6),
                    features=features,
                    scan_id=scan_result.scan_id,
                    market_timestamp=candidate.market_timestamp,
                    score_components=components,
                    metadata={
                        "timeframe": candidate.timeframe,
                        "latest_price": candidate.latest_price,
                        "avg_volume": candidate.avg_volume,
                        "data_points": candidate.data_points,
                    },
                )
            )
            all_scores.append(score)

        score_range = (min(all_scores), max(all_scores)) if all_scores else (0.0, 0.0)

        # Remaining eligible candidates not in top_n are excluded from opportunities
        # but still counted as evaluated.
        total_excluded_in_ranking = len(candidates) - len(opportunities) - len(exclusions)
        # Candidates that were evaluated but cut by top_n are not "excluded" per se;
        # they are simply not in the top-N. We don't list them as exclusions.

        return OpportunityRankingResult(
            ranking_id=ranking_id,
            scan_id=scan_result.scan_id,
            ranking_timestamp=scan_ts.isoformat(),
            candidates_evaluated=len(candidates),
            opportunities=opportunities,
            exclusions=exclusions,
            config_snapshot=self.config.config_snapshot(),
            score_range=score_range,
            data_provider_source=self.config.data_provider_source,
        )

    # ------------------------------------------------------------------ #
    # Eligibility / feature evaluation
    # ------------------------------------------------------------------ #

    def _evaluate_candidate(
        self,
        candidate: ScanCandidate,
        scan_result: MarketScanResult,
        min_bars: int,
    ) -> tuple[Optional[OpportunityFeatures], Optional[RankExclusion]]:
        """Return (features, None) or (None, exclusion)."""

        # Quick pre-check using the scanner's pre-computed data_points.
        if candidate.data_points < min_bars:
            return None, RankExclusion(
                scan_id=scan_result.scan_id,
                symbol=candidate.symbol,
                reason=RankExclusionReason.INSUFFICIENT_DATA,
                detail=(
                    f"data_points={candidate.data_points} < min_required={min_bars}"
                ),
                market_timestamp=candidate.market_timestamp,
            )

        # --- Embedded snapshot path (preferred) ---
        # When the scanner embedded validated close prices, use them directly
        # — no re-fetch. This guarantees the ranker operates on the exact same
        # market snapshot the scanner validated, eliminating snapshot drift.
        embedded_prices = candidate.metadata.get("close_prices")
        if embedded_prices is not None:
            close = pd.Series(embedded_prices, dtype=float)
            if len(close) < min_bars:
                return None, RankExclusion(
                    scan_id=scan_result.scan_id,
                    symbol=candidate.symbol,
                    reason=RankExclusionReason.INSUFFICIENT_DATA,
                    detail=(
                        f"embedded close_prices count={len(close)} "
                        f"< min_required={min_bars}"
                    ),
                    market_timestamp=candidate.market_timestamp,
                )
        else:
            # --- Provider fallback path ---
            df = self._fetch_ohlcv(candidate, scan_result)
            if df is None:
                return None, RankExclusion(
                    scan_id=scan_result.scan_id,
                    symbol=candidate.symbol,
                    reason=RankExclusionReason.MISSING_MARKET_DATA,
                    detail="no market data available for feature calculation",
                    market_timestamp=candidate.market_timestamp,
                )

            # Re-validate (defensive; scanner already validated).
            try:
                report = validate_ohlcv(df, candidate.timeframe)
            except Exception as exc:  # noqa: BLE001 — record, don't crash
                return None, RankExclusion(
                    scan_id=scan_result.scan_id,
                    symbol=candidate.symbol,
                    reason=RankExclusionReason.COMPUTATION_ERROR,
                    detail=f"data validation error: {exc}",
                    market_timestamp=candidate.market_timestamp,
                )

            if not report.ok:
                return None, RankExclusion(
                    scan_id=scan_result.scan_id,
                    symbol=candidate.symbol,
                    reason=RankExclusionReason.INVALID_FEATURE,
                    detail=f"data failed validation: {report.issues}",
                    market_timestamp=candidate.market_timestamp,
                )

            valid = report.valid

            # Look-ahead protection: keep only bars at or before market_timestamp.
            if candidate.market_timestamp is not None:
                filter_ts = pd.Timestamp(candidate.market_timestamp)
                if filter_ts.tzinfo is None:
                    filter_ts = filter_ts.tz_localize("UTC")
                if "timestamp" in valid.columns:
                    ts_col = pd.to_datetime(valid["timestamp"], utc=True, errors="coerce")
                    valid = valid.loc[ts_col <= filter_ts]
                elif isinstance(valid.index, pd.DatetimeIndex):
                    valid = valid.loc[valid.index <= filter_ts]

            if len(valid) < min_bars:
                return None, RankExclusion(
                    scan_id=scan_result.scan_id,
                    symbol=candidate.symbol,
                    reason=RankExclusionReason.INSUFFICIENT_DATA,
                    detail=(
                        f"post-filter rows={len(valid)} < min_required={min_bars}"
                    ),
                    market_timestamp=candidate.market_timestamp,
                )

            close = valid["close"]

        if len(close) == 0 or close.isna().any():
            return None, RankExclusion(
                scan_id=scan_result.scan_id,
                symbol=candidate.symbol,
                reason=RankExclusionReason.INVALID_FEATURE,
                detail="no valid close prices after filtering",
                market_timestamp=candidate.market_timestamp,
            )

        # Calculate features.
        result = self._calculate_features(candidate, close)
        if result[0] is None:
            return result  # (None, exclusion)
        features, feat_exclusion = result
        if feat_exclusion is not None:
            return None, feat_exclusion  # (None, exclusion)

        return features, None

    def _fetch_ohlcv(
        self, candidate: ScanCandidate, scan_result: MarketScanResult
    ) -> Optional[pd.DataFrame]:
        """Fetch raw OHLCV for a candidate via the data provider callable."""
        if self._provider is None:
            return None
        try:
            df = self._provider(candidate.symbol, candidate.timeframe)
        except Exception:  # noqa: BLE001 — fail-closed
            return None
        if not isinstance(df, pd.DataFrame) or len(df) == 0:
            return None
        return df

    # ------------------------------------------------------------------ #
    # Feature calculation
    # ------------------------------------------------------------------ #

    def _calculate_features(
        self, candidate: ScanCandidate, close: pd.Series
    ) -> tuple[Optional[OpportunityFeatures], Optional[RankExclusion]]:
        """Compute normalized feature scores.

        Returns ``(features, None)`` on success or ``(None, exclusion)`` when a
        required feature cannot be computed (fail-closed — no fabricated scores).
        """
        cfg = self.config
        weights = cfg.normalized_weights()

        momentum_score = 0.0
        trend_score = 0.0
        volatility_score = 0.0
        liquidity_score = 0.0
        freshness_score = 0.0

        # --- Momentum ---
        if weights["momentum"] > 0 and cfg.momentum_window > 0:
            score, ok = self._momentum_score(close, cfg.momentum_window, cfg.momentum_cap)
            if not ok:
                return None, RankExclusion(
                    scan_id=candidate.scan_id,
                    symbol=candidate.symbol,
                    reason=RankExclusionReason.INVALID_FEATURE,
                    detail="momentum score is NaN",
                    market_timestamp=candidate.market_timestamp,
                )
            momentum_score = score

        # --- Trend ---
        if weights["trend"] > 0 and cfg.trend_window > 0:
            score, ok = self._trend_score(close, cfg.trend_window, cfg.trend_cap)
            if not ok:
                return None, RankExclusion(
                    scan_id=candidate.scan_id,
                    symbol=candidate.symbol,
                    reason=RankExclusionReason.INVALID_FEATURE,
                    detail="trend score is NaN",
                    market_timestamp=candidate.market_timestamp,
                )
            trend_score = score

        # --- Volatility ---
        if weights["volatility"] > 0 and cfg.volatility_window > 0:
            score, ok = self._volatility_score(close, cfg.volatility_window, cfg.volatility_cap)
            if not ok:
                return None, RankExclusion(
                    scan_id=candidate.scan_id,
                    symbol=candidate.symbol,
                    reason=RankExclusionReason.INVALID_FEATURE,
                    detail="volatility score is NaN",
                    market_timestamp=candidate.market_timestamp,
                )
            volatility_score = score

        # --- Liquidity (from scan-candidate summary, no re-fetch needed) ---
        if weights["liquidity"] > 0:
            score, ok = self._liquidity_score(candidate.avg_volume, cfg.liquidity_cap)
            if not ok:
                return None, RankExclusion(
                    scan_id=candidate.scan_id,
                    symbol=candidate.symbol,
                    reason=RankExclusionReason.INVALID_FEATURE,
                    detail="liquidity score is NaN (avg_volume unavailable)",
                    market_timestamp=candidate.market_timestamp,
                )
            liquidity_score = score

        # --- Freshness (from scan-candidate summary) ---
        if weights["freshness"] > 0:
            score, ok = self._freshness_score(candidate.freshness_seconds, cfg.freshness_cap_seconds)
            if not ok:
                return None, RankExclusion(
                    scan_id=candidate.scan_id,
                    symbol=candidate.symbol,
                    reason=RankExclusionReason.INVALID_FEATURE,
                    detail="freshness score is NaN (freshness_seconds unavailable)",
                    market_timestamp=candidate.market_timestamp,
                )
            freshness_score = score

        return (
            OpportunityFeatures(
                symbol=candidate.symbol,
                market_timestamp=candidate.market_timestamp,
                momentum_score=momentum_score,
                trend_score=trend_score,
                volatility_score=volatility_score,
                liquidity_score=liquidity_score,
                freshness_score=freshness_score,
            ),
            None,
        )

    @staticmethod
    def _momentum_score(close: pd.Series, window: int, cap: float) -> tuple[float, bool]:
        """Rate-of-change momentum, normalized to [0, 1] (cap → ±cap)."""
        mom = momentum(close, window)
        raw = float(mom.iloc[-1])
        if math.isnan(raw) or math.isinf(raw):
            return 0.5, False
        return _normalize_centered(raw, cap), True

    @staticmethod
    def _trend_score(close: pd.Series, window: int, cap: float) -> tuple[float, bool]:
        """Price deviation from SMA, normalized to [0, 1] (cap → ±cap)."""
        sma_series = sma(close, window)
        sma_latest = float(sma_series.iloc[-1])
        close_latest = float(close.iloc[-1])
        if math.isnan(sma_latest) or math.isnan(close_latest) or is_close(sma_latest, 0.0):
            return 0.5, False
        deviation = close_latest / sma_latest - 1.0
        if math.isnan(deviation) or math.isinf(deviation):
            return 0.5, False
        return _normalize_centered(deviation, cap), True

    @staticmethod
    def _volatility_score(close: pd.Series, window: int, cap: float) -> tuple[float, bool]:
        """Rolling return volatility, normalized to [0, 1] (cap → 1.0)."""
        returns = simple_returns(close)
        vol_series = rolling_volatility(returns, window)
        raw = float(vol_series.iloc[-1])
        if math.isnan(raw) or math.isinf(raw) or raw < 0:
            return 0.0, False
        return _normalize_ratio(raw, cap), True

    @staticmethod
    def _liquidity_score(avg_volume: Optional[float], cap: float) -> tuple[float, bool]:
        """Average volume normalized to [0, 1] (cap → 1.0)."""
        if avg_volume is None or math.isnan(avg_volume) or avg_volume < 0:
            return 0.0, False
        return _normalize_ratio(avg_volume, cap), True

    @staticmethod
    def _freshness_score(freshness_seconds: Optional[float], cap_seconds: float) -> tuple[float, bool]:
        """Data freshness, inverted to [0, 1] (fresh → 1.0, stale → 0.0)."""
        if freshness_seconds is None or math.isnan(freshness_seconds) or freshness_seconds < 0:
            return 0.0, False
        raw = min(freshness_seconds / cap_seconds, 1.0)
        return 1.0 - raw, True

    @staticmethod
    def _compute_score(features: OpportunityFeatures, weights: dict[str, float]) -> float:
        """Weighted composite score in [0.0, 1.0]."""
        return (
            weights["momentum"] * features.momentum_score
            + weights["trend"] * features.trend_score
            + weights["volatility"] * features.volatility_score
            + weights["liquidity"] * features.liquidity_score
            + weights["freshness"] * features.freshness_score
        )


def is_close(a: float, b: float, tol: float = 1e-12) -> bool:
    return abs(a - b) < tol
