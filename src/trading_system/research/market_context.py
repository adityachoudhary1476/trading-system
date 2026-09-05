"""Market data context abstractions (V3 Phase 1/4/6/7/14).

Provider-neutral contracts for additional market intelligence inputs that the
intelligence engine can consume when available. Every context carries a
``data_quality`` field and an ``available`` flag so the engine can clearly
distinguish:

  SUPPORTED            -> data present, used as evidence
  CONTRADICTORY        -> data present, contradicts the thesis
  UNAVAILABLE          -> data source not provided (NOT turned into neutral)
  INSUFFICIENT_DATA    -> data provided but too thin to trust

RULE: unavailable data never becomes fake neutral evidence. Missing fields stay
``None`` unless explicitly provided (synthetic test fixtures are the only
exception, and those are always clearly labeled).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


# --------------------------------------------------------------------------- #
# Unified data-quality model (Phase 14)
# --------------------------------------------------------------------------- #
class DataQualityTier(str, Enum):
    """Quality tier for any optional data source."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    THIN = "thin"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class EvidenceAvailability(str, Enum):
    """How an evidence item should be treated by the ledger.

    Only SUPPORTED items contribute weight toward confidence. CONTRADICTORY
    items actively reduce it. All other states are recorded transparently
    without inventing directional signal.
    """

    SUPPORTED = "supported"
    CONTRADICTORY = "contradictory"
    UNAVAILABLE = "unavailable"
    INSUFFICIENT_DATA = "insufficient_data"
    PARTIAL = "partial"          # usable but incomplete (e.g. MIXED sentiment)
    STALE = "stale"              # outside the freshness window
    LOW_QUALITY = "low_quality"  # degraded source quality / low reliability


# --------------------------------------------------------------------------- #
# A. Market Breadth (Phase 1)
# --------------------------------------------------------------------------- #
@dataclass
class MarketBreadth:
    """Market breadth context. All fields optional; unavailable stays None."""

    advancing_count: Optional[int] = None
    declining_count: Optional[int] = None
    unchanged_count: Optional[int] = None
    new_highs: Optional[int] = None
    new_lows: Optional[int] = None
    timestamp: Optional[datetime] = None
    source: Optional[str] = None
    data_quality: DataQualityTier = DataQualityTier.UNAVAILABLE

    @property
    def available(self) -> bool:
        return self.advancing_count is not None and self.declining_count is not None

    @property
    def advance_decline_ratio(self) -> Optional[float]:
        if not self.available or self.declining_count == 0:
            return None
        return self.advancing_count / self.declining_count  # type: ignore[operator]

    @property
    def advance_percent(self) -> Optional[float]:
        total = (self.advancing_count or 0) + (self.declining_count or 0) + (self.unchanged_count or 0)
        if total == 0:
            return None
        return (self.advancing_count or 0) / total * 100.0

    @property
    def decline_percent(self) -> Optional[float]:
        total = (self.advancing_count or 0) + (self.declining_count or 0) + (self.unchanged_count or 0)
        if total == 0:
            return None
        return (self.declining_count or 0) / total * 100.0

    @property
    def breadth_strength(self) -> Optional[str]:
        """Qualitative breadth read: 'strong', 'moderate', 'weak', or None."""
        if not self.available:
            return None
        ratio = self.advance_decline_ratio
        if ratio is None:
            return None
        if ratio >= 2.0:
            return "strong"
        elif ratio >= 1.0:
            return "moderate"
        else:
            return "weak"


# --------------------------------------------------------------------------- #
# B. India VIX / Volatility context (Phase 1)
# --------------------------------------------------------------------------- #
@dataclass
class IndiaVIXContext:
    """India VIX / volatility regime context."""

    india_vix: Optional[float] = None
    vix_change: Optional[float] = None
    vix_percentile: Optional[float] = None  # 0..100
    vix_regime: Optional[str] = None  # e.g. "elevated", "normal", "suppressed"
    timestamp: Optional[datetime] = None
    source: Optional[str] = None
    data_quality: DataQualityTier = DataQualityTier.UNAVAILABLE

    @property
    def available(self) -> bool:
        return self.india_vix is not None


# --------------------------------------------------------------------------- #
# C. FII / DII flow context (Phase 1)
# --------------------------------------------------------------------------- #
@dataclass
class InstitutionalFlow:
    """Buy/sell/net for one institutional category."""

    buy: Optional[float] = None
    sell: Optional[float] = None

    @property
    def net(self) -> Optional[float]:
        if self.buy is None or self.sell is None:
            return None
        return self.buy - self.sell

    @property
    def available(self) -> bool:
        return self.buy is not None and self.sell is not None


@dataclass
class FIIDIIFlow:
    """FII + DII institutional flow context."""

    fii: InstitutionalFlow = field(default_factory=InstitutionalFlow)
    dii: InstitutionalFlow = field(default_factory=InstitutionalFlow)
    date: Optional[str] = None
    source: Optional[str] = None
    data_quality: DataQualityTier = DataQualityTier.UNAVAILABLE

    @property
    def available(self) -> bool:
        return self.fii.available or self.dii.available

    INSUFFICIENT_DATA = "insufficient_data"

# --------------------------------------------------------------------------- #
# D. Sector context (Phase 1/4)
# --------------------------------------------------------------------------- #
@dataclass
class SectorContext:
    """Sector-level context for a stock, or a sector benchmark."""

    sector_symbol: Optional[str] = None
    sector_name: Optional[str] = None
    sector_return: Optional[float] = None  # recent return, fraction
    relative_strength: Optional[float] = None  # sector vs market
    trend: Optional[str] = None
    momentum: Optional[float] = None
    volatility: Optional[float] = None
    breadth: Optional[MarketBreadth] = None
    timestamp: Optional[datetime] = None
    source: Optional[str] = None
    data_quality: DataQualityTier = DataQualityTier.UNAVAILABLE

    @property
    def available(self) -> bool:
        return self.sector_symbol is not None and self.sector_return is not None


# --------------------------------------------------------------------------- #
# E. News / sentiment (Phase 6)
# --------------------------------------------------------------------------- #
class NewsEventType(str, Enum):
    EARNINGS = "earnings"
    CORPORATE_ACTION = "corporate_action"
    MACRO = "macro"
    REGULATORY = "regulatory"
    ANALYST = "analyst"
    GEOPOLITICAL = "geopolitical"
    MARKET = "market"
    GENERAL = "general"


@dataclass
class NewsEvent:
    """A single structured news/sentiment event."""

    timestamp: Optional[datetime] = None
    source: Optional[str] = None
    headline: str = ""
    symbol: Optional[str] = None
    sector: Optional[str] = None
    sentiment: Optional[float] = None  # -1..1
    sentiment_confidence: Optional[float] = None  # 0..1
    relevance: Optional[float] = None  # 0..1
    event_type: Optional[NewsEventType] = None
    data_quality: DataQualityTier = DataQualityTier.UNAVAILABLE

    @property
    def available(self) -> bool:
        return bool(self.headline) and self.sentiment is not None


@dataclass
class NewsContext:
    """Aggregated news/sentiment context."""

    events: list[NewsEvent] = field(default_factory=list)
    aggregate_sentiment: Optional[float] = None  # -1..1
    news_status: str = "unavailable"
    data_quality: DataQualityTier = DataQualityTier.UNAVAILABLE

    @property
    def available(self) -> bool:
        return len(self.events) > 0 and self.aggregate_sentiment is not None


# --------------------------------------------------------------------------- #
# F. Cross-asset context (Phase 7)
# --------------------------------------------------------------------------- #
@dataclass
class CrossAssetContext:
    """Optional provider-neutral cross-asset context."""

    usdinr: Optional[float] = None
    usdinr_change: Optional[float] = None
    us_index_change: Optional[float] = None  # S&P 500 / Nasdaq
    crude_oil_change: Optional[float] = None
    gold_change: Optional[float] = None
    bond_yield_10y: Optional[float] = None
    timestamp: Optional[datetime] = None
    source: Optional[str] = None
    data_quality: DataQualityTier = DataQualityTier.UNAVAILABLE

    @property
    def available(self) -> bool:
        return any(
            v is not None
            for v in (
                self.usdinr_change,
                self.us_index_change,
                self.crude_oil_change,
                self.gold_change,
                self.bond_yield_10y,
            )
        )


# --------------------------------------------------------------------------- #
# Bundle: all optional contexts in one container
# --------------------------------------------------------------------------- #
@dataclass
class MarketIntelligenceContext:
    """Aggregates every optional V3 context the engine can consume.

    Unavailable contexts are represented by default-instances (all fields None,
    data_quality=UNAVAILABLE) — never by synthetic values.
    """

    breadth: Optional[MarketBreadth] = None
    vix: Optional[IndiaVIXContext] = None
    institutional_flow: Optional[FIIDIIFlow] = None
    sector: Optional[SectorContext] = None
    news: Optional[NewsContext] = None
    cross_asset: Optional[CrossAssetContext] = None

    def available_contexts(self) -> list[str]:
        """Names of contexts that are present and usable."""
        out = []
        if self.breadth and self.breadth.available:
            out.append("breadth")
        if self.vix and self.vix.available:
            out.append("vix")
        if self.institutional_flow and self.institutional_flow.available:
            out.append("institutional_flow")
        if self.sector and self.sector.available:
            out.append("sector")
        if self.news and self.news.available:
            out.append("news")
        if self.cross_asset and self.cross_asset.available:
            out.append("cross_asset")
        return out

