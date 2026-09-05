"""V4 news intelligence: provider-agnostic ingestion to evidence.

FREE / PROVIDER-AGNOSTIC. Core RSS support uses only the Python standard
library (no new dependencies). NEWS_ENABLED=false (default) keeps the whole
trading system fully functional with no network access. Nothing here
fabricates news: fetch failures yield empty results and are logged.

Pipeline:
  NewsProvider -> NewsNormalizer -> NewsDeduplicator -> EntityResolver ->
  EventClassifier -> SentimentEngine -> ImpactClassifier -> RelevanceScorer ->
  NoveltyScorer -> NewsContext / EvidenceLedger -> MarketIntelligence

News feeds provide information availability, not guaranteed real-time
execution-grade market data.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any, Callable, Iterable, Optional
from urllib import request as _urlreq
from urllib.error import HTTPError, URLError

from .market_context import DataQualityTier, EvidenceAvailability
from .intelligence_v3 import (EvidenceCategory, EvidenceItem, EvidenceLedgerV2)

log = logging.getLogger("finova.news")

SYNTHETIC_TEST = "SYNTHETIC_TEST"
UTC = timezone.utc
MAX_TITLE_LEN = 512          # oversized-entry guard
MAX_DESC_LEN = 1024


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _ensure_tz(dt: Optional[datetime]) -> Optional[datetime]:
    """All timestamps MUST be timezone-aware; naive is assumed UTC (documented)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_published(raw: Optional[str]) -> Optional[datetime]:
    """Parse RFC-822 or ISO-8601 timestamps; None when absent/malformed."""
    if not raw:
        return None
    txt = raw.strip()
    try:
        return _ensure_tz(parsedate_to_datetime(txt))
    except (TypeError, ValueError):
        pass
    try:
        return _ensure_tz(datetime.fromisoformat(txt.replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Freshness (configurable windows; per-event-type overrides)
# --------------------------------------------------------------------------- #
class FreshnessTier(str, Enum):
    VERY_FRESH = "very_fresh"   # < 15 minutes
    FRESH = "fresh"             # 15-60 minutes
    RECENT = "recent"           # 1-6 hours
    STALE = "stale"             # 6-24 hours
    OLD = "old"                 # > 24 hours
    UNKNOWN = "unknown"         # timestamp missing


DEFAULT_FRESHNESS_HOURS: dict[str, tuple[float, float]] = {
    FreshnessTier.VERY_FRESH.value: (0.0, 0.25),
    FreshnessTier.FRESH.value: (0.25, 1.0),
    FreshnessTier.RECENT.value: (1.0, 6.0),
    FreshnessTier.STALE.value: (6.0, 24.0),
}

# Macro/policy events decay slower than company-specific news (configurable).
EVENT_FRESHNESS_OVERRIDES: dict[str, float] = {
    "rbi_decision": 48.0,
    "government_policy": 72.0,
    "budget_policy": 72.0,
    "regulatory_approval": 48.0,
    "regulatory_action": 48.0,
    "geopolitical_event": 72.0,
    "commodity_shock": 48.0,
    "macroeconomic_event": 48.0,
}

FRESHNESS_WEIGHT = {  # decay weight used for aggregates
    FreshnessTier.VERY_FRESH.value: 1.0,
    FreshnessTier.FRESH.value: 0.8,
    FreshnessTier.RECENT.value: 0.5,
    FreshnessTier.STALE.value: 0.2,
    FreshnessTier.OLD.value: 0.05,
    FreshnessTier.UNKNOWN.value: 0.1,
}


def classify_freshness(
    published_at: Optional[datetime],
    as_of: Optional[datetime] = None,
    overrides: Optional[dict[str, float]] = None,
    event_type: Optional[str] = None,
) -> FreshnessTier:
    """Freshness from published_at relative to as_of (never discovered_at)."""
    if published_at is None:
        return FreshnessTier.UNKNOWN
    as_of = _ensure_tz(as_of) or _utcnow()
    age_h = max((as_of - published_at).total_seconds() / 3600.0, 0.0)
    table = {k: (lo, hi) for k, (lo, hi) in DEFAULT_FRESHNESS_HOURS.items()}
    if event_type and overrides:
        cap = overrides.get(event_type)
        if cap is not None:
            lo = table[FreshnessTier.STALE.value][0]
            table[FreshnessTier.STALE.value] = (lo, cap)
    for tier in (FreshnessTier.VERY_FRESH, FreshnessTier.FRESH,
                 FreshnessTier.RECENT, FreshnessTier.STALE):
        lo, hi = table[tier.value]
        if lo <= age_h < hi:
            return tier
    return FreshnessTier.OLD

# --------------------------------------------------------------------------- #
# Normalized news event (V4 data contract)
# --------------------------------------------------------------------------- #
@dataclass
class NewsEventV4:
    """Normalized, provider-neutral news event.

    Metadata/headline level only — full article text is NOT stored. All
    timestamps are timezone-aware. ``published_at`` (source time) and
    ``discovered_at`` (ingestion time) are kept SEPARATE: historical replay
    may only use events with published_at <= as_of.
    """

    event_id: str = ""
    source: str = ""                    # provider id, e.g. "rss:nse_announcements"
    source_url: str = ""
    publisher: str = ""
    title: str = ""
    description: str = ""
    published_at: Optional[datetime] = None
    discovered_at: Optional[datetime] = None
    language: str = "en"
    country: str = "IN"
    tickers: list[str] = field(default_factory=list)
    company_names: list[str] = field(default_factory=list)
    sectors: list[str] = field(default_factory=list)
    indices: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    event_type: str = "other"           # see EventClassifier.TYPES
    sentiment: str = "UNKNOWN"          # POSITIVE/NEGATIVE/NEUTRAL/MIXED/UNKNOWN
    sentiment_score: Optional[float] = None        # -1..1
    sentiment_confidence: Optional[float] = None   # 0..1
    relevance_score: Optional[float] = None        # 0..1 (set per target symbol)
    impact_score: Optional[float] = None           # 0..1
    impact_level: str = ""              # HIGH/MEDIUM/LOW
    affected_assets: list[str] = field(default_factory=list)
    novelty_score: Optional[float] = None          # 0..1 (1 = first report)
    source_reliability: float = 0.7     # 0..1 per-feed configuration
    market_horizon: str = "short"       # very_short/short/medium/long
    data_quality: DataQualityTier = DataQualityTier.HEALTHY
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    # deduplication
    canonical_event_id: Optional[str] = None
    supporting_sources: list[str] = field(default_factory=list)

    @property
    def is_synthetic(self) -> bool:
        return self.source.startswith(SYNTHETIC_TEST)


# --------------------------------------------------------------------------- #
# Providers (stdlib RSS; no third-party dependencies)
# --------------------------------------------------------------------------- #
@dataclass
class FeedConfig:
    """One configurable feed. URLs are CONFIGURATION, never code assumptions.
    Edit/extend without touching the intelligence layer."""

    name: str
    url: str
    category: str = "general"
    publisher: str = ""
    source_reliability: float = 0.7
    enabled: bool = True


# NSE corporate RSS endpoints (public, documented by NSE). Configuration only —
# if NSE relocates them, edit config; do NOT add undocumented endpoints.
DEFAULT_FEEDS: list[FeedConfig] = [
    FeedConfig("nse_announcements",
               "https://nsearchives.nseindia.com/content/RSS/Corporate_Anouncements.xml",
               "announcements", "NSE", 0.95),
    FeedConfig("nse_financial_results",
               "https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml",
               "financial_results", "NSE", 0.95),
    FeedConfig("nse_board_meetings",
               "https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml",
               "board_meeting", "NSE", 0.95),
    FeedConfig("nse_corporate_actions",
               "https://nsearchives.nseindia.com/content/RSS/Corporate_Actions.xml",
               "corporate_action", "NSE", 0.95),
    FeedConfig("nse_circulars",
               "https://nsearchives.nseindia.com/content/RSS/Circulars.xml",
               "circulars", "NSE", 0.95),
]


@dataclass
class RawNewsItem:
    """Provider-neutral raw item pre-normalization."""

    source: str
    title: str = ""
    url: str = ""
    description: str = ""
    published_raw: str = ""
    publisher: str = ""
    raw_metadata: dict[str, Any] = field(default_factory=dict)


class NewsProvider:
    """Provider-agnostic source of RawNewsItems. fetch() must NEVER raise —
    failures degrade to fewer items and are logged."""

    name = "base"

    def fetch(self, feed: FeedConfig, timeout: float = 10.0) -> list[RawNewsItem]:
        raise NotImplementedError

    def fetch_all(self, feeds: Iterable[FeedConfig],
                  timeout: float = 10.0) -> list[RawNewsItem]:
        out: list[RawNewsItem] = []
        for f in feeds:
            if not f.enabled:
                continue
            try:
                out.extend(self.fetch(f, timeout))
            except Exception as exc:  # pragma: no cover — defensive by contract
                log.warning("provider %s feed %s failed: %s", self.name, f.name, exc)
        return out

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class RssNewsProvider(NewsProvider):
    """RSS 2.0 / Atom parser on the standard library. Defensive by design:
    malformed XML, oversized entries, invalid URLs, bad encodings and missing
    fields degrade to fewer items — never an exception."""

    name = "rss"
    _NS = {"atom": "http://www.w3.org/2005/Atom"}
    _MAX_FEED_BYTES = 2_000_000

    def fetch(self, feed: FeedConfig, timeout: float = 10.0) -> list[RawNewsItem]:
        if not feed.url or not feed.url.lower().startswith(("http://", "https://")):
            log.warning("feed %s has invalid URL; skipped", feed.name)
            return []
        req = _urlreq.Request(feed.url, headers={"User-Agent": "FinovaNews/1.0"})
        try:
            with _urlreq.urlopen(req, timeout=timeout) as resp:
                payload = resp.read(self._MAX_FEED_BYTES)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            log.warning("rss fetch failed feed=%s err=%s", feed.name, exc)
            return []
        return self.parse(payload, feed)

    # --- parsing (pure, unit-testable without network) ----------------------
    def parse(self, payload: bytes, feed: FeedConfig) -> list[RawNewsItem]:
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            log.warning("rss parse failed feed=%s err=%s", feed.name, exc)
            return []
        items: list[RawNewsItem] = []
        seen: set[str] = set()
        for node in self._item_nodes(root):
            item = self._to_raw(node, feed)
            if item is None:
                continue
            key = item.url or item.title
            if not key or key in seen:  # duplicate URL/title within one feed
                continue
            seen.add(key)
            items.append(item)
        return items

    def _item_nodes(self, root: ET.Element) -> list[ET.Element]:
        tag = root.tag.rsplit("}", 1)[-1]
        if tag == "rss":
            ch = root.find("channel")
            return ch.findall("item") if ch is not None else []
        if tag == "feed":  # Atom
            return root.findall("atom:entry", self._NS)
        return root.findall(".//item") or root.findall(".//{*}entry")

    def _to_raw(self, node: ET.Element, feed: FeedConfig) -> Optional[RawNewsItem]:
        def txt(path: str) -> str:
            el = node.find(path)
            if el is None:
                el = node.find(f"atom:{path}", self._NS)
            return (el.text or "").strip() if el is not None and el.text else ""

        title = _CTRL.sub("", txt("title"))[:MAX_TITLE_LEN]
        if not title:
            return None
        link = txt("link")
        if not link:
            lel = node.find("atom:link", self._NS)
            if lel is not None:
                link = lel.get("href", "") or ""
        desc = _CTRL.sub("", txt("description") or txt("summary"))[:MAX_DESC_LEN]
        pub = txt("pubDate") or txt("published") or txt("updated")
        return RawNewsItem(
            source=f"rss:{feed.name}", title=title, url=link.strip(),
            description=desc, published_raw=pub,
            publisher=feed.publisher or feed.name,
            raw_metadata={"category": feed.category,
                          "reliability": feed.source_reliability})


class GoogleNewsSearchProvider(NewsProvider):
    """OPTIONAL generic search-feed provider (public Google News RSS).
    Never the only source; aggregator reliability is capped low."""

    name = "gnews"
    _URL = ("https://news.google.com/rss/search?q={query}"
            "&hl=en-IN&gl=IN&ceid=IN:en")

    def __init__(self) -> None:
        self._rss = RssNewsProvider()

    def build_feed(self, query: str, name: str = "gnews_search",
                   reliability: float = 0.6) -> FeedConfig:
        from urllib.parse import quote
        return FeedConfig(name, self._URL.format(query=quote(query)),
                          "general", "Google News", reliability)

    def fetch(self, feed: FeedConfig, timeout: float = 10.0) -> list[RawNewsItem]:
        return self._rss.fetch(feed, timeout)


class NewsNormalizer:
    """Raw -> NewsEventV4. Deterministic event_id; tz-aware timestamps;
    published_at and discovered_at stored separately."""

    def normalize(self, raw: RawNewsItem,
                  discovered_at: Optional[datetime] = None) -> NewsEventV4:
        discovered = _ensure_tz(discovered_at) or _utcnow()
        published = _parse_published(raw.published_raw)
        basis = raw.url or f"{raw.source}|{raw.title}|{published.isoformat() if published else ''}"
        eid = hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()[:16]
        dq = DataQualityTier.HEALTHY
        if published is None:
            dq = DataQualityTier.DEGRADED  # missing publication time
        return NewsEventV4(
            event_id=eid, source=raw.source, source_url=raw.url,
            publisher=raw.publisher, title=raw.title, description=raw.description,
            published_at=published, discovered_at=discovered,
            source_reliability=float(raw.raw_metadata.get("reliability", 0.7)),
            data_quality=dq, raw_metadata=dict(raw.raw_metadata),
        )

# --------------------------------------------------------------------------- #
# Deduplication (deterministic)
# --------------------------------------------------------------------------- #
_STOPWORDS = {"the", "a", "an", "of", "in", "on", "for", "to", "and", "or",
              "is", "are", "at", "by", "with", "from", "as", "its", "his",
              "her", "after", "before", "over", "under", "says", "said",
              "report", "reports", "update"}


def _title_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class NewsDeduplicator:
    """Deterministic dedup: normalized-title similarity + publication-time
    proximity + entity overlap. 20 copies of one event become ONE canonical
    event with supporting_sources[] — never 20 independent bullish signals.

    Conservative on purpose: events merge only when titles are similar AND
    times are close AND (entities overlap OR same publisher). Unrelated
    events are NOT merged."""

    def __init__(self, jaccard_threshold: float = 0.6,
                 window_hours: float = 24.0) -> None:
        self.jt = jaccard_threshold
        self.window = timedelta(hours=window_hours)

    def _matches(self, canonical: NewsEventV4, ev: NewsEventV4) -> bool:
        ta = canonical.published_at or canonical.discovered_at
        tb = ev.published_at or ev.discovered_at
        if ta is None or tb is None or \
                abs((ta - tb).total_seconds()) > self.window.total_seconds():
            return False
        if _jaccard(_title_tokens(canonical.title),
                    _title_tokens(ev.title)) < self.jt:
            return False
        same_publisher = (canonical.publisher == ev.publisher
                          and bool(canonical.publisher))
        entity_overlap = bool(
            set(canonical.tickers) & set(ev.tickers)
            or set(canonical.sectors) & set(ev.sectors))
        return same_publisher or entity_overlap or not (canonical.tickers or ev.tickers)

    def deduplicate(self, events: list[NewsEventV4]) -> list[NewsEventV4]:
        ordered = sorted(events, key=lambda e: (
            e.published_at or e.discovered_at or _utcnow(), e.event_id))
        canonical: list[NewsEventV4] = []
        for ev in ordered:
            target = next((c for c in canonical if self._matches(c, ev)), None)
            if target is None:
                ev.canonical_event_id = ev.event_id
                ev.novelty_score = 1.0          # first report: full novelty
                canonical.append(ev)
            else:
                ev.canonical_event_id = target.event_id
                k = len(target.supporting_sources) + 1
                ev.novelty_score = 1.0 / (1.0 + k)   # repeats decay
                if ev.source not in target.supporting_sources:
                    target.supporting_sources.append(ev.source)
                if ev.source_reliability > target.source_reliability:
                    target.source_reliability = ev.source_reliability
                if target.published_at is None and ev.published_at is not None:
                    target.published_at = ev.published_at
                if ev.data_quality == DataQualityTier.HEALTHY:
                    target.data_quality = DataQualityTier.HEALTHY
        return canonical

# --------------------------------------------------------------------------- #
# Entity resolution (Indian markets; aliases; never guess)
# --------------------------------------------------------------------------- #
@dataclass
class EntityAlias:
    ticker: Optional[str]
    company: Optional[str]
    sector: Optional[str]
    is_index: bool = False


# Factual reference mapping (curated; extensible via constructor). Unknown
# entities remain UNRESOLVED — never guessed.
ENTITY_ALIASES: dict[str, EntityAlias] = {
    a: EntityAlias(t, c, s)
    for a, (t, c, s) in {
        "reliance industries": ("RELIANCE", "Reliance Industries", "Energy"),
        "reliance": ("RELIANCE", "Reliance Industries", "Energy"),
        "state bank of india": ("SBIN", "State Bank of India", "Banking"),
        "sbin": ("SBIN", "State Bank of India", "Banking"),
        "tata consultancy": ("TCS", "Tata Consultancy Services", "IT"),
        "tcs": ("TCS", "Tata Consultancy Services", "IT"),
        "infosys": ("INFY", "Infosys", "IT"),
        "hdfc bank": ("HDFCBANK", "HDFC Bank", "Banking"),
        "icici bank": ("ICICIBANK", "ICICI Bank", "Banking"),
        "axis bank": ("AXISBANK", "Axis Bank", "Banking"),
        "kotak mahindra": ("KOTAKBANK", "Kotak Mahindra Bank", "Banking"),
        "bank of baroda": ("BANKBARODA", "Bank of Baroda", "PSU Bank"),
        "punjab national bank": ("PNB", "Punjab National Bank", "PSU Bank"),
        "larsen": ("LT", "Larsen & Toubro", "Infra"),
        "tata motors": ("TATAMOTORS", "Tata Motors", "Auto"),
        "maruti": ("MARUTI", "Maruti Suzuki", "Auto"),
        "bajaj finance": ("BAJFINANCE", "Bajaj Finance", "NBFC"),
        "tata steel": ("TATASTEEL", "Tata Steel", "Metals"),
        "jsw steel": ("JSWSTEEL", "JSW Steel", "Metals"),
        "ongc": ("ONGC", "ONGC", "Energy"),
        "ntpc": ("NTPC", "NTPC", "Power"),
        "coal india": ("COALINDIA", "Coal India", "Energy"),
        "bharti airtel": ("BHARTIARTL", "Bharti Airtel", "Telecom"),
        "hindustan unilever": ("HINDUNILVR", "Hindustan Unilever", "FMCG"),
        "sun pharma": ("SUNPHARMA", "Sun Pharma", "Pharma"),
        "wipro": ("WIPRO", "Wipro", "IT"),
        "hcl tech": ("HCLTECH", "HCL Technologies", "IT"),
        "ultratech": ("ULTRACEMCO", "UltraTech Cement", "Cement"),
        "titan": ("TITAN", "Titan Company", "Consumer"),
        "asian paints": ("ASIANPAINT", "Asian Paints", "Consumer"),
    }.items()
}

INDEX_ALIASES: dict[str, str] = {
    "nifty": "NIFTY50", "nifty 50": "NIFTY50", "nifty50": "NIFTY50",
    "nifty bank": "BANKNIFTY", "bank nifty": "BANKNIFTY",
    "banknifty": "BANKNIFTY", "fin nifty": "FINNIFTY", "finnifty": "FINNIFTY",
    "sensex": "SENSEX",
}

SECTOR_KEYWORDS: dict[str, str] = {
    "banking": "Banking", "psu bank": "PSU Bank", "it sector": "IT",
    "software stocks": "IT", "pharma": "Pharma", "auto sector": "Auto",
    "automaker": "Auto", "metal": "Metals", "steel": "Metals",
    "cement": "Cement", "fmcg": "FMCG", "oil": "Energy", "crude": "Energy",
    "power sector": "Power", "telecom": "Telecom", "infra": "Infra",
}

# Factual index-membership reference (well-known constituents; used ONLY for
# relevance scoring; extensible via constructor).
INDEX_MEMBERS: dict[str, set[str]] = {
    "NIFTY50": {"RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "ITC",
                "LT", "AXISBANK", "KOTAKBANK", "SBIN", "BHARTIARTL",
                "HINDUNILVR", "TATAMOTORS", "TATASTEEL", "MARUTI", "TITAN",
                "ASIANPAINT", "SUNPHARMA", "ONGC", "NTPC", "POWERGRID",
                "ADANIENT", "ADANIPORTS", "COALINDIA", "WIPRO", "HCLTECH",
                "TECHM", "ULTRACEMCO", "GRASIM", "BAJFINANCE", "INDUSINDBK",
                "DRREDDY", "CIPLA", "APOLLOHOSP", "HEROMOTOCO", "HINDALCO"},
    "BANKNIFTY": {"HDFCBANK", "ICICIBANK", "AXISBANK", "KOTAKBANK", "SBIN",
                  "INDUSINDBK", "BANKBARODA", "PNB"},
}

_COMPANY_TOKEN = re.compile(
    r"\b([A-Z][a-z]+ [A-Z][a-zA-Z]+(?: Ltd| Limited| Industries| Bank| Motors)?)\b")


class EntityResolver:
    """Maps news text to NSE ticker / company / sector / index / market topic.

    Unknown entities stay UNRESOLVED (tracked, never fabricated)."""

    def __init__(self, aliases: Optional[dict[str, EntityAlias]] = None,
                 index_aliases: Optional[dict[str, str]] = None) -> None:
        self.aliases = aliases or ENTITY_ALIASES
        self.index_aliases = index_aliases or INDEX_ALIASES
        self.last_unresolved: list[str] = []

    def _find_entities(self, text: str) -> list[EntityAlias]:
        low = f" {text.lower()} "
        return [ent for alias, ent in self.aliases.items()
                if f" {alias} " in low or f" {alias}." in low
                or f" {alias}," in low]

    def resolve(self, ev: NewsEventV4) -> NewsEventV4:
        text = f"{ev.title} {ev.description}"
        for ent in self._find_entities(text):
            if ent.ticker and ent.ticker not in ev.tickers:
                ev.tickers.append(ent.ticker)
            if ent.company and ent.company not in ev.company_names:
                ev.company_names.append(ent.company)
            if ent.sector and ent.sector not in ev.sectors:
                ev.sectors.append(ent.sector)
        low = f" {text.lower()} "
        for alias, idx in self.index_aliases.items():
            if f" {alias} " in low and idx not in ev.indices:
                ev.indices.append(idx)
        for kw, sector in SECTOR_KEYWORDS.items():
            if f" {kw}" in low and sector not in ev.sectors:
                ev.sectors.append(sector)
        for m in _COMPANY_TOKEN.findall(text):
            known = any(ent.company and
                        ent.company.lower().startswith(m.lower()[:8])
                        for ent in self.aliases.values())
            if not known and m not in self.last_unresolved:
                self.last_unresolved.append(m)
        return ev

    def resolve_all(self, events: list[NewsEventV4]) -> list[NewsEventV4]:
        self.last_unresolved = []
        return [self.resolve(e) for e in events]

# --------------------------------------------------------------------------- #
# Event classification (never forced; unknown -> other)
# --------------------------------------------------------------------------- #
class EventClassifier:
    """Rule-based classifier over the full V4 event-type vocabulary. The NSE
    feed category (raw_metadata) acts as a prior; text keywords decide.
    Unmatched -> 'other' (never forced)."""

    TYPES = (
        "earnings", "earnings_guidance", "revenue_update", "profit_warning",
        "acquisition", "merger", "demerger", "order_win", "contract", "capex",
        "expansion", "management_change", "promoter_action", "insider_activity",
        "buyback", "dividend", "fundraising", "debt", "credit_rating",
        "regulatory_approval", "regulatory_action", "litigation",
        "government_policy", "rbi_decision", "sebi_action", "tax_change",
        "budget_policy", "commodity_shock", "geopolitical_event",
        "sector_event", "macroeconomic_event", "analyst_update",
        "product_launch", "plant_shutdown", "production_disruption", "other",
    )

    # Ordered: earlier rules win on overlap (e.g. RBI before macro).
    RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("rbi_decision", ("rbi", "repo rate", "rate cut", "rate hike",
                          "monetary policy", "mpc meeting")),
        ("sebi_action", ("sebi",)),
        ("credit_rating", ("rating upgrade", "rating downgrade",
                           "rating assigned", "rating reaffirmed", "crisil",
                           "icra", "moody", "fitch")),
        ("profit_warning", ("profit warning", "cuts guidance", "warns of",
                            "weak demand")),
        ("earnings", ("quarterly results", "q1 results", "q2 results",
                      "q3 results", "q4 results", "net profit", "net loss",
                      "reports pat", "consolidated profit")),
        ("earnings_guidance", ("guidance", "outlook for fy")),
        ("revenue_update", ("revenue", "turnover", "topline", "sales rose",
                            "sales fell")),
        ("acquisition", ("acquire", "acquisition", "stake in", "buys stake")),
        ("merger", ("merger", "amalgamation")),
        ("demerger", ("demerger", "spin-off", "hiving off")),
        ("order_win", ("order win", "wins order", "bagged order",
                       "letter of award", "receives order", "order for")),
        ("contract", ("contract", "agreement with")),
        ("capex", ("capex", "capital expenditure", "new plant",
                   "greenfield")),
        ("expansion", ("expansion", "capacity addition", "brownfield")),
        ("management_change", ("appoints", "appointment", "resigns",
                               "named ceo", "named cfo", "managing director",
                               "chairman")),
        ("promoter_action", ("promoter", "pledge of shares")),
        ("insider_activity", ("insider", "open market purchase")),
        ("buyback", ("buyback", "buy-back", "share repurchase")),
        ("dividend", ("dividend",)),
        ("fundraising", ("qip", "preferential issue", "fpo", "ipo",
                         "fund raise", "fundraising")),
        ("debt", ("debt", "borrowing", "nclt", "insolvency", "defaults on")),
        ("regulatory_approval", ("approval", "clearance", "usfda", "nod from")),
        ("regulatory_action", ("penalty", "show-cause", "notice from",
                               "probe", "investigation", "bans")),
        ("litigation", ("lawsuit", "court", "litigation", "tribunal")),
        ("government_policy", ("government", "ministry", "cabinet")),
        ("tax_change", ("gst", "tax", "duty", "cess")),
        ("budget_policy", ("budget", "fiscal")),
        ("commodity_shock", ("crude", "oil price", "commodity", "gold price")),
        ("geopolitical_event", ("war", "geopolitical", "sanctions",
                                "conflict", "tariff")),
        ("sector_event", ("sector", "industry-wide")),
        ("macroeconomic_event", ("gdp", "cpi", "inflation", "iip", "pmi",
                                 "trade deficit", "rupee")),
        ("analyst_update", ("brokerage", "upgrade", "downgrade",
                            "target price", "initiates coverage")),
        ("product_launch", ("launch", "unveils")),
        ("plant_shutdown", ("shutdown", "shut down", "lockout")),
        ("production_disruption", ("disruption", "halt", "stops production",
                                   "fire at", "explosion")),
    )

    _CATEGORY_PRIOR = {
        "financial_results": "earnings",
        "corporate_action": "dividend",
        "announcements": "other",
        "board_meeting": "other",
        "circulars": "other",
    }

    def classify(self, ev: NewsEventV4) -> str:
        text = f"{ev.title} {ev.description}".lower()
        for etype, kws in self.RULES:
            if any(k in text for k in kws):
                return etype
        cat = str(ev.raw_metadata.get("category", ""))
        return self._CATEGORY_PRIOR.get(cat, "other")

# --------------------------------------------------------------------------- #
# Sentiment (deterministic lexicon; SENTIMENT != MARKET IMPACT)
# --------------------------------------------------------------------------- #
_POSITIVE = ("surge", "soar", "jumps", "rise", "rises", "gain", "gains",
             "beats", "beat estimates", "record profit", "record high",
             "strong", "robust", "growth", "wins order", "bagged",
             "approval", "clearance", "upgrade", "raised guidance",
             "expands", "boost", "stellar", "outperform", "buyback",
             "highest-ever", "wins")
_NEGATIVE = ("falls", "fall", "slips", "drops", "plunge", "crash", "loss",
             "losses", "weak", "misses", "downgrade", "cuts guidance",
             "probe", "investigation", "penalty", "fraud", "resigns",
             "raid", "bans", "lawsuit", "default", "insolvency", "strike",
             "shutdown", "fire at", "warning", "decline", "declines",
             "risk", "concern", "delay", "halt", "weak demand")
_NEGATORS = ("not", "no", "denies", "denied", "rejects", "fails to")


def _count_hits(text: str, lexicon: tuple[str, ...]) -> int:
    hits = 0
    for kw in lexicon:
        idx = 0
        while True:
            i = text.find(kw, idx)
            if i < 0:
                break
            prefix = text[max(0, i - 12):i].split()
            if prefix and any(neg in prefix[-2:] for neg in _NEGATORS):
                idx = i + len(kw)
                continue  # negated -> not counted as polar
            hits += 1
            idx = i + len(kw)
    return hits


class SentimentEngine:
    """Deterministic lexicon sentiment with negation handling.

    POSITIVE / NEGATIVE / NEUTRAL / MIXED / UNKNOWN + score (-1..1) +
    confidence (0..1). No hits => UNKNOWN (never a fake neutral)."""

    def analyze(self, ev: NewsEventV4) -> NewsEventV4:
        text = f"{ev.title} {ev.description}".lower()
        p, n = _count_hits(text, _POSITIVE), _count_hits(text, _NEGATIVE)
        if p == 0 and n == 0:
            ev.sentiment = "UNKNOWN"
            ev.sentiment_score = None
            ev.sentiment_confidence = 0.0
            return ev
        if p and n:
            ev.sentiment = "MIXED"
        elif p:
            ev.sentiment = "POSITIVE"
        else:
            ev.sentiment = "NEGATIVE"
        ev.sentiment_score = round((p - n) / (p + n), 3)
        ev.sentiment_confidence = round(min(0.85, 0.4 + 0.1 * (p + n)), 2)
        return ev

# --------------------------------------------------------------------------- #
# Event-based market impact (SEPARATE from sentiment) + relevance
# --------------------------------------------------------------------------- #
_IMPACT_BASE = {  # event_type -> (score 0..1, level)
    "rbi_decision": (0.9, "HIGH"), "budget_policy": (0.9, "HIGH"),
    "profit_warning": (0.85, "HIGH"), "credit_rating": (0.8, "HIGH"),
    "acquisition": (0.8, "HIGH"), "order_win": (0.75, "HIGH"),
    "earnings": (0.7, "HIGH"), "demerger": (0.75, "HIGH"),
    "merger": (0.75, "HIGH"), "fundraising": (0.7, "HIGH"),
    "commodity_shock": (0.7, "HIGH"), "geopolitical_event": (0.7, "HIGH"),
    "regulatory_action": (0.7, "HIGH"),
    "regulatory_approval": (0.6, "MEDIUM"), "buyback": (0.55, "MEDIUM"),
    "dividend": (0.45, "MEDIUM"), "capex": (0.5, "MEDIUM"),
    "expansion": (0.5, "MEDIUM"), "analyst_update": (0.5, "MEDIUM"),
    "revenue_update": (0.5, "MEDIUM"), "tax_change": (0.6, "MEDIUM"),
    "government_policy": (0.65, "MEDIUM"),
    "product_launch": (0.35, "LOW"), "litigation": (0.45, "MEDIUM"),
    "debt": (0.5, "MEDIUM"), "plant_shutdown": (0.55, "MEDIUM"),
    "production_disruption": (0.55, "MEDIUM"),
    "management_change": (0.25, "LOW"), "promoter_action": (0.3, "LOW"),
    "insider_activity": (0.3, "LOW"), "sector_event": (0.4, "LOW"),
    "contract": (0.45, "MEDIUM"), "other": (0.2, "LOW"),
}

_AFFECTED = {
    "rbi_decision": ["BANKS", "BANKNIFTY", "NIFTY50", "INR"],
    "budget_policy": ["NIFTY50", "SECTORS"],
    "commodity_shock": ["METALS", "ENERGY", "NIFTY50"],
    "geopolitical_event": ["NIFTY50", "INR", "GOLD"],
    "macroeconomic_event": ["NIFTY50", "INR", "BONDS"],
    "tax_change": ["NIFTY50", "AFFECTED_SECTORS"],
}

_HORIZON = {
    "commodity_shock": "very_short", "geopolitical_event": "very_short",
    "earnings": "short", "order_win": "short", "regulatory_action": "short",
    "capex": "medium", "expansion": "medium", "merger": "medium",
    "demerger": "medium", "fundraising": "medium",
    "government_policy": "long", "budget_policy": "long",
}

_IMPACT_BOOSTERS = ("record", "major", "surprise", "largest", "massive",
                    "highest-ever", "blockbuster")
_IMPACT_DAMPENERS = ("small", "minor", "routine")


class ImpactClassifier:
    """MARKET IMPACT != SENTIMENT. 'Record profit but misses expectations'
    can be sentiment=MIXED with company impact HIGH."""

    def classify(self, ev: NewsEventV4) -> NewsEventV4:
        text = f"{ev.title} {ev.description}".lower()
        base, level = _IMPACT_BASE.get(ev.event_type, (0.2, "LOW"))
        score = base
        if any(b in text for b in _IMPACT_BOOSTERS):
            score = min(1.0, score + 0.2)
        if any(d in text for d in _IMPACT_DAMPENERS):
            score = max(0.05, score - 0.2)
        ev.impact_score = round(score, 3)
        ev.impact_level = level
        ev.market_horizon = _HORIZON.get(ev.event_type, "short")
        assets = list(_AFFECTED.get(ev.event_type, []))
        assets.extend(ev.tickers)          # company events affect the ticker
        if ev.sectors:
            assets.extend(f"SECTOR:{s}" for s in ev.sectors)
        ev.affected_assets = list(dict.fromkeys(assets))
        return ev


class RelevanceScorer:
    """Company/sector/index/market relevance derived from the event-entity
    relationship plus event-type breadth. No per-company hardcoding beyond
    the factual alias/ membership reference tables."""

    _MARKET_BREADTH = {
        "rbi_decision": 0.9, "budget_policy": 0.9,
        "macroeconomic_event": 0.9, "government_policy": 0.8,
        "commodity_shock": 0.7, "geopolitical_event": 0.7,
        "tax_change": 0.7, "sector_event": 0.6,
    }
    # Company-specific event types have NO market-wide breadth by default:
    # an unrelated ticker must not inherit relevance from them (gated to 0).
    _DEFAULT_BREADTH = 0.0

    def score(self, ev: NewsEventV4, target_ticker: Optional[str] = None,
              target_sector: Optional[str] = None,
              target_index: Optional[str] = None) -> float:
        company = 1.0 if (target_ticker and target_ticker in ev.tickers) else 0.0
        sector = 0.8 if (target_sector and target_sector in ev.sectors) else 0.0
        index = 0.0
        if target_index and target_index in ev.indices:
            index = 0.5
        market = self._MARKET_BREADTH.get(ev.event_type, self._DEFAULT_BREADTH)
        return round(max(company, sector, index, market), 3)

# --------------------------------------------------------------------------- #
# Pipeline orchestration + conflicts
# --------------------------------------------------------------------------- #
@dataclass
class NewsPipelineResult:
    """Canonical (deduplicated) events + processing statistics."""

    events: list[NewsEventV4] = field(default_factory=list)
    duplicates_removed: int = 0
    articles_ingested: int = 0
    unresolved_entities: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)


def detect_conflicts(events: list[NewsEventV4],
                     score_gap: float = 0.6) -> list[dict[str, Any]]:
    """Opposing strong sentiments about the SAME entity -> CONFLICTING.
    Never blindly averaged; surfaced to the ledger as conflict."""
    by_entity: dict[str, list[NewsEventV4]] = {}
    for e in events:
        for t in e.tickers:
            by_entity.setdefault(t, []).append(e)
    conflicts: list[dict[str, Any]] = []
    for entity, evs in by_entity.items():
        pos = [e for e in evs if (e.sentiment_score or 0) > 0.3]
        neg = [e for e in evs if (e.sentiment_score or 0) < -0.3]
        if pos and neg:
            hi = max(e.sentiment_score for e in pos)
            lo = min(e.sentiment_score for e in neg)
            if hi - lo >= score_gap:
                conflicts.append({
                    "entity": entity,
                    "positive_events": [e.event_id for e in pos],
                    "negative_events": [e.event_id for e in neg],
                    "gap": round(hi - lo, 3),
                })
    return conflicts


class NewsPipeline:
    """normalize -> deduplicate -> resolve -> classify -> sentiment -> impact.
    Relevance is set later per target symbol (build_news_context)."""

    def __init__(self, dedup: Optional[NewsDeduplicator] = None,
                 resolver: Optional[EntityResolver] = None,
                 classifier: Optional[EventClassifier] = None,
                 sentiment: Optional[SentimentEngine] = None,
                 impact: Optional[ImpactClassifier] = None) -> None:
        self.dedup = dedup or NewsDeduplicator()
        self.resolver = resolver or EntityResolver()
        self.classifier = classifier or EventClassifier()
        self.sentiment = sentiment or SentimentEngine()
        self.impact = impact or ImpactClassifier()

    def run(self, raw_items: Iterable[RawNewsItem],
            as_of: Optional[datetime] = None) -> NewsPipelineResult:
        events = [NewsNormalizer().normalize(r, as_of) for r in raw_items]
        articles = len(events)
        canonical = self.dedup.deduplicate(events)
        self.resolver.resolve_all(canonical)
        for e in canonical:
            e.event_type = self.classifier.classify(e)
            self.sentiment.analyze(e)
            self.impact.classify(e)
        conflicts = detect_conflicts(canonical)
        return NewsPipelineResult(
            events=canonical,
            duplicates_removed=articles - len(canonical),
            articles_ingested=articles,
            unresolved_entities=list(self.resolver.last_unresolved),
            conflicts=conflicts,
        )


# --------------------------------------------------------------------------- #
# Per-symbol news context (V4)
# --------------------------------------------------------------------------- #
@dataclass
class NewsContextResult:
    """Per-symbol news context — feeds evidence + fingerprints."""

    events: list[NewsEventV4] = field(default_factory=list)
    aggregate_sentiment: Optional[float] = None
    news_status: str = "unavailable"     # available/conflicting/unavailable
    data_quality: DataQualityTier = DataQualityTier.UNAVAILABLE
    freshness_weight: Optional[float] = None
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.news_status in ("available", "conflicting")

def build_news_context(
    result: NewsPipelineResult,
    as_of: Optional[datetime] = None,
    target_ticker: Optional[str] = None,
    target_sector: Optional[str] = None,
    target_index: Optional[str] = None,
    freshness_overrides: Optional[dict[str, float]] = None,
) -> NewsContextResult:
    """Score relevance per event, apply freshness, aggregate honestly.

    - No events -> news_status=unavailable (NOT neutral).
    - Conflicts for the target entity -> news_status=conflicting.
    - Aggregate sentiment is relevance x impact x freshness x novelty weighted.
    - Replay-safety: only events with published_at <= as_of are considered.
    """
    as_of = _ensure_tz(as_of) or _utcnow()
    overrides = freshness_overrides or EVENT_FRESHNESS_OVERRIDES
    scorer = RelevanceScorer()
    events: list[NewsEventV4] = []
    weighted: list[float] = []
    for e in result.events:
        if e.published_at is not None and e.published_at > as_of:
            continue  # future news never used
        e.relevance_score = scorer.score(e, target_ticker, target_sector,
                                         target_index)
        fresh = classify_freshness(e.published_at, as_of, overrides,
                                   e.event_type)
        e.raw_metadata["freshness"] = fresh.value
        if e.relevance_score <= 0.0:
            continue  # irrelevant to this target
        events.append(e)
        fw = FRESHNESS_WEIGHT.get(fresh.value, 0.1)
        w = ((e.relevance_score or 0.0) * (e.impact_score or 0.5) * fw
             * (e.novelty_score or 1.0))
        weighted.append(w * (e.sentiment_score or 0.0))
    conflicts = [c for c in result.conflicts
                 if not target_ticker or c["entity"] == target_ticker]
    denom = sum(abs(x) for x in weighted)
    agg = (sum(weighted) / denom) if denom > 0 else None
    status = "unavailable"
    dq = DataQualityTier.UNAVAILABLE
    if events:
        dq = DataQualityTier.HEALTHY
        status = "conflicting" if conflicts else "available"
    fresh_w = None
    if events:
        fresh_w = sum(FRESHNESS_WEIGHT.get(
            classify_freshness(e.published_at, as_of, overrides,
                               e.event_type).value, 0.1)
            for e in events) / len(events)
    return NewsContextResult(events=events, aggregate_sentiment=agg,
                             news_status=status, data_quality=dq,
                             freshness_weight=fresh_w, conflicts=conflicts)


def to_v3_context(ctx: NewsContextResult) -> Any:
    """Bridge to the V3 ``NewsContext`` schema (best-effort, lossy)."""
    from .market_context import NewsContext, NewsEvent, NewsEventType
    type_map = {
        "earnings": NewsEventType.EARNINGS,
        "analyst_update": NewsEventType.ANALYST,
        "rbi_decision": NewsEventType.MACRO,
        "macroeconomic_event": NewsEventType.MACRO,
        "government_policy": NewsEventType.MACRO,
        "budget_policy": NewsEventType.MACRO,
        "regulatory_action": NewsEventType.REGULATORY,
        "regulatory_approval": NewsEventType.REGULATORY,
        "sebi_action": NewsEventType.REGULATORY,
        "geopolitical_event": NewsEventType.GEOPOLITICAL,
    }
    evs = [NewsEvent(timestamp=e.published_at, source=e.source,
                     headline=e.title,
                     symbol=(e.tickers[0] if e.tickers else None),
                     sector=(e.sectors[0] if e.sectors else None),
                     sentiment=e.sentiment_score,
                     sentiment_confidence=e.sentiment_confidence,
                     relevance=e.relevance_score,
                     event_type=type_map.get(e.event_type, NewsEventType.GENERAL),
                     data_quality=e.data_quality) for e in ctx.events]
    return NewsContext(events=evs, aggregate_sentiment=ctx.aggregate_sentiment,
                       news_status=ctx.news_status, data_quality=ctx.data_quality)

# --------------------------------------------------------------------------- #
# News -> Evidence Ledger V2
# --------------------------------------------------------------------------- #
def news_to_evidence(ledger: EvidenceLedgerV2, ctx: NewsContextResult,
                     as_of: Optional[datetime] = None,
                     max_items: int = 3) -> EvidenceLedgerV2:
    """Add news evidence with explicit states.

    UNAVAILABLE when there is no news (never neutral). CONTRADICTORY when the
    entity has conflicting coverage. STALE / LOW_QUALITY / PARTIAL as
    warranted. Non-SUPPORTED items contribute no confidence weight."""
    if not ctx.events:
        ledger.add(EvidenceItem(
            category=EvidenceCategory.NEWS, signal="news_context",
            availability=EvidenceAvailability.UNAVAILABLE,
            explanation="news_status=unavailable (no events)",
            data_quality=DataQualityTier.UNAVAILABLE))
        return ledger
    conflicted = {c["entity"] for c in ctx.conflicts}
    top = sorted(ctx.events, key=lambda e: (e.relevance_score or 0.0,
                                            e.impact_score or 0.0),
                 reverse=True)[:max_items]
    dq_map = {FreshnessTier.VERY_FRESH: DataQualityTier.HEALTHY,
              FreshnessTier.FRESH: DataQualityTier.HEALTHY,
              FreshnessTier.RECENT: DataQualityTier.HEALTHY,
              FreshnessTier.STALE: DataQualityTier.STALE,
              FreshnessTier.OLD: DataQualityTier.STALE,
              FreshnessTier.UNKNOWN: DataQualityTier.DEGRADED}
    for e in top:
        fresh = classify_freshness(e.published_at, as_of,
                                   EVENT_FRESHNESS_OVERRIDES, e.event_type)
        direction = None
        if e.sentiment_score is not None:
            direction = ("bullish" if e.sentiment_score > 0.1
                         else "bearish" if e.sentiment_score < -0.1 else None)
        if any(t in conflicted for t in e.tickers):
            avail = EvidenceAvailability.CONTRADICTORY
        elif fresh in (FreshnessTier.STALE, FreshnessTier.OLD):
            avail = EvidenceAvailability.STALE
        elif (e.data_quality == DataQualityTier.DEGRADED
              or e.source_reliability < 0.5):
            avail = EvidenceAvailability.LOW_QUALITY
        elif e.sentiment in ("MIXED", "UNKNOWN"):
            avail = EvidenceAvailability.PARTIAL
        else:
            avail = EvidenceAvailability.SUPPORTED
        strength = 100.0 * (e.relevance_score or 0.0) * (e.impact_score or 0.5)
        explanation = (f"{e.event_type} | {e.sentiment} "
                       f"(score={e.sentiment_score}) | rel={e.relevance_score}"
                       f" | impact={e.impact_level} | fresh={fresh.value}"
                       f" | src={e.publisher} | novelty={e.novelty_score}"
                       f" | supporting={len(e.supporting_sources)}")
        ledger.add(EvidenceItem(
            category=EvidenceCategory.NEWS, signal=f"event:{e.event_id[:8]}",
            direction=direction, strength=round(strength, 1), weight=1.0,
            source=e.publisher or e.source, data_quality=dq_map[fresh],
            timestamp=e.published_at, availability=avail,
            explanation=explanation))
    agg_dir = None
    if ctx.aggregate_sentiment is not None:
        agg_dir = ("bullish" if ctx.aggregate_sentiment > 0.1
                   else "bearish" if ctx.aggregate_sentiment < -0.1 else None)
    ledger.add(EvidenceItem(
        category=EvidenceCategory.NEWS, signal="news_aggregate",
        direction=agg_dir,
        strength=round(abs(ctx.aggregate_sentiment or 0.0) * 100
                       * (ctx.freshness_weight or 0.1), 1),
        weight=1.2, source="news_pipeline", data_quality=ctx.data_quality,
        availability=(EvidenceAvailability.CONTRADICTORY if ctx.conflicts
                      else EvidenceAvailability.SUPPORTED),
        explanation=(f"news_status={ctx.news_status}; n={len(ctx.events)}; "
                     f"aggregate={ctx.aggregate_sentiment}; "
                     f"freshness_weight={ctx.freshness_weight}")))
    return ledger

# --------------------------------------------------------------------------- #
# Polling service (development scale; OFF by default)
# --------------------------------------------------------------------------- #
def news_enabled_from_env(default: str = "false") -> bool:
    import os
    return os.getenv("NEWS_ENABLED", default).strip().lower() in ("1", "true", "yes")


class NewsPollingService:
    """Development-scale polling. Non-aggressive: configurable interval,
    per-feed timeout, retries with exponential backoff, graceful shutdown,
    duplicate suppression across polls, structured logging. Never started
    unless NEWS_ENABLED is explicitly turned on."""

    def __init__(self, providers: list[NewsProvider],
                 feeds: list[FeedConfig],
                 pipeline: Optional[NewsPipeline] = None,
                 interval_s: float = 300.0, timeout_s: float = 10.0,
                 max_retries: int = 3, backoff_base_s: float = 30.0) -> None:
        self.providers = providers
        self.feeds = [f for f in feeds if f.enabled]
        self.pipeline = pipeline or NewsPipeline()
        self.interval_s = interval_s
        self.timeout_s = timeout_s
        self.max_retries = max(1, int(max_retries))
        self.backoff_base_s = backoff_base_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seen: set[str] = set()
        self.failures: dict[str, int] = {}
        self.last_result: Optional[NewsPipelineResult] = None

    def poll_once(self, as_of: Optional[datetime] = None) -> NewsPipelineResult:
        raw: list[RawNewsItem] = []
        for p in self.providers:
            for f in self.feeds:
                key = f"{p.name}:{f.name}"
                for attempt in range(self.max_retries):
                    try:
                        raw.extend(p.fetch(f, self.timeout_s))
                        self.failures[key] = 0
                        break
                    except Exception as exc:  # providers usually self-guard
                        self.failures[key] = attempt + 1
                        log.warning("poll %s attempt %s failed: %s",
                                    key, attempt + 1, exc)
                        if attempt + 1 < self.max_retries:
                            self._stop.wait(self.backoff_base_s * (2 ** attempt))
        fresh_items = []
        for item in raw:
            fp = item.url or f"{item.source}|{item.title}"
            if fp in self._seen:
                continue
            self._seen.add(fp)
            fresh_items.append(item)
        self.last_result = self.pipeline.run(fresh_items,
                                             as_of=as_of or _utcnow())
        return self.last_result

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop,
                                        name="finova-news-poll", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # pragma: no cover — never crash the loop
                log.exception("news poll cycle failed")
            self._stop.wait(self.interval_s)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
