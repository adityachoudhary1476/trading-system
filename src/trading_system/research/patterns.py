"""V4 historical pattern engine: state similarity + historical outcomes.

Given the CURRENT market state, find similar HISTORICAL states (strictly
before the forecast timestamp — enforced inside the API, not by caller
discipline) and report what happened afterward as HISTORICAL CONDITIONAL
FREQUENCY. This is not a probability and not a promise:

"Historical pattern statistics describe past observations and do not
guarantee future outcomes."

Normalization uses only information available at each historical timestamp
(causal percentiles/z-scores) so NIFTY 20000 and NIFTY 25000 are comparable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Optional

import pandas as pd

from .intelligence import (
    FeatureEngine, TechnicalFeatures, VolRegime, classify_regime, MarketRegime,
)
from .intelligence_v3 import (
    EvidenceAvailability, EvidenceCategory, EvidenceItem, EvidenceLedgerV2,
    TimeframeAnalysis,
)
from .market_context import DataQualityTier

UTC = timezone.utc
SYNTHETIC_TEST = "SYNTHETIC_TEST"


def _clip01(x: Optional[float]) -> Optional[float]:
    if x is None:
        return None
    return max(0.0, min(1.0, x))


def _squash(x: Optional[float], scale: float) -> Optional[float]:
    """Map a signed raw feature to 0..1 via symmetric clipping (x/scale + 0.5)."""
    if x is None:
        return None
    return _clip01(x / scale + 0.5)


def _wilson(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (sample-size honest)."""
    if n == 0:
        return (0.0, 1.0)
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


# --------------------------------------------------------------------------- #
# Feature weights (explicit, ablatable) — group weight per dimension group
# --------------------------------------------------------------------------- #
@dataclass
class FeatureWeights:
    """Explicit pattern weights. Groups map to fingerprint dimensions via
    DIM_GROUPS. Zeroing a group is the ablation mechanism."""

    price: float = 1.0
    trend: float = 1.5
    momentum: float = 1.2
    volume: float = 0.8
    volatility: float = 1.0
    mtf: float = 1.0
    sector: float = 0.8
    breadth: float = 0.6
    news: float = 0.8
    options: float = 0.6

    def masked(self, groups: set[str]) -> "FeatureWeights":
        """Copy with every group NOT in ``groups`` zeroed (ablation)."""
        out = FeatureWeights(**vars(self))
        for g in ("price", "trend", "momentum", "volume", "volatility",
                  "mtf", "sector", "breadth", "news", "options"):
            if g not in groups:
                setattr(out, g, 0.0)
        return out


# Ablation configurations (Phase 27): A..F
ABLATION_CONFIGS: dict[str, set[str]] = {
    "A_technical": {"price", "trend", "momentum", "volatility", "volume"},
    "B_plus_mtf": {"price", "trend", "momentum", "volatility", "volume", "mtf"},
    "C_plus_sector": {"price", "trend", "momentum", "volatility", "volume",
                      "mtf", "sector"},
    "D_plus_context": {"price", "trend", "momentum", "volatility", "volume",
                       "mtf", "sector", "breadth", "options"},
    "E_plus_news": {"price", "trend", "momentum", "volatility", "volume",
                    "mtf", "news"},
    "F_full": {"price", "trend", "momentum", "volatility", "volume", "mtf",
               "sector", "breadth", "options", "news"},
}

DIM_GROUPS: dict[str, str] = {
    # price structure
    "mom10": "price", "dist_from_high": "price", "dist_from_low": "price",
    "atr_pct": "price", "breakout": "price", "breakdown": "price",
    # trend
    "trend_st": "trend", "trend_mt": "trend", "trend_lt": "trend",
    "trend_strength": "trend",
    # momentum
    "rsi_norm": "momentum", "mom_dir": "momentum",
    # volume
    "rel_vol": "volume", "vol_trend_up": "volume",
    # volatility
    "hist_vol": "volatility", "high_vol_flag": "volatility",
    # multi-timeframe
    "mtf_5m": "mtf", "mtf_15m": "mtf", "mtf_1h": "mtf", "mtf_1d": "mtf",
    # market context
    "sector_strength": "sector", "rs_market": "sector",
    "adv_pct": "breadth", "breadth_strength": "breadth",
    "iv_regime": "options", "pcr": "options",
    # news
    "news_sent": "news", "news_count": "news", "news_impact": "news",
    "news_fresh": "news",
}


class PatternStatus(str, Enum):
    SUFFICIENT = "SUFFICIENT"
    INSUFFICIENT_MATCHES = "INSUFFICIENT_MATCHES"
    PATTERN_CONFLICTING = "PATTERN_CONFLICTING"
    PATTERN_WEAK = "PATTERN_WEAK"

# --------------------------------------------------------------------------- #
# Market-state fingerprint
# --------------------------------------------------------------------------- #
@dataclass
class MarketStateFingerprint:
    """Normalized market state. Every dim is 0..1 or None (missing stays
    missing — never imputed). ``regime`` is the regime label at that time."""

    dims: dict[str, Optional[float]] = field(default_factory=dict)
    regime: str = "unknown"
    timestamp: Optional[datetime] = None
    instrument: str = ""

    def available_dims(self) -> list[str]:
        return [d for d, v in self.dims.items() if v is not None]


def _bias_to_01(bias) -> Optional[float]:
    try:
        name = getattr(bias, "name", str(bias)).upper()
    except Exception:
        return None
    if name == "BULLISH":
        return 1.0
    if name == "BEARISH":
        return 0.0
    if name == "NEUTRAL":
        return 0.5
    return None


def fingerprint_from_features(
    features: TechnicalFeatures,
    regime: MarketRegime,
    timestamp: Optional[datetime] = None,
    instrument: str = "",
    multi_tf: Optional[dict[str, "TimeframeAnalysis"]] = None,
    context=None,           # optional MarketIntelligenceContext
    news_report=None,       # optional NewsPipelineResult
    extra_dims: Optional[dict[str, Optional[float]]] = None,
) -> MarketStateFingerprint:
    """Build a normalized fingerprint from V3 features + optional contexts.

    Missing inputs stay None (never imputed, never fabricated).
    """
    d: dict[str, Optional[float]] = {
        "mom10": _squash(features.roc, 0.10),
        "dist_from_high": _squash(features.dist_from_high, 0.10),
        "dist_from_low": _squash(features.dist_from_low, 0.10),
        "atr_pct": _squash((features.atr_14 / features.close) if
                           (features.atr_14 and features.close) else None, 0.05),
        "breakout": 1.0 if features.breakout_candidate else 0.0,
        "breakdown": 1.0 if features.breakdown_candidate else 0.0,
        "trend_st": _squash(features.price_vs_sma20, 0.05),
        "trend_mt": _squash(features.price_vs_sma50, 0.10),
        "trend_lt": _squash(features.price_vs_sma200, 0.15),
        "trend_strength": _squash(features.ema20_vs_ema50, 0.03),
        "rsi_norm": _clip01(features.rsi_14 / 100.0
                            if features.rsi_14 is not None else None),
        "mom_dir": _bias_to_01(features.momentum_dir),
        "rel_vol": _clip01(features.relative_volume / 3.0 if
                           features.relative_volume is not None else None),
        "vol_trend_up": _bias_to_01(features.volume_trend),
        "hist_vol": _clip01(features.hist_vol / 0.60 if features.hist_vol else None),
        "high_vol_flag": 1.0 if features.vol_regime == VolRegime.HIGH else 0.0,
    }
    for tf, ta in (multi_tf or {}).items():
        key = {"5m": "mtf_5m", "15m": "mtf_15m", "1h": "mtf_1h",
               "1d": "mtf_1d"}.get(tf)
        if key:
            d[key] = _bias_to_01(ta.bias)
    if context is not None:
        sec = getattr(context, "sector", None)
        if sec is not None:
            d["sector_strength"] = _squash(getattr(sec, "sector_return", None), 5.0)
            d["rs_market"] = _squash(getattr(sec, "relative_strength", None), 3.0)
        br = getattr(context, "breadth", None)
        if br is not None and getattr(br, "available", False):
            tot = (br.advancing_count or 0) + (br.declining_count or 0)
            d["adv_pct"] = _clip01((br.advancing_count or 0) / tot if tot else None)
            d["breadth_strength"] = _squash(
                (br.advance_decline_ratio - 1.0) if br.advance_decline_ratio
                else None, 2.0)
        vix = getattr(context, "vix", None)
        if vix is not None and getattr(vix, "india_vix", None) is not None:
            d["iv_regime"] = _clip01(vix.india_vix / 40.0)
    if news_report is not None:
        agg = getattr(news_report, "aggregate_sentiment", None)
        if agg is not None:
            d["news_sent"] = _squash(agg, 1.0)
        d["news_count"] = _clip01(len(getattr(news_report, "events", []) or []) / 10.0)
        impacts = [e.impact_score for e in getattr(news_report, "events", []) or []
                   if e.impact_score is not None]
        d["news_impact"] = max(impacts) if impacts else None
        fresh = getattr(news_report, "freshness_weight", None)
        if fresh is not None:
            d["news_fresh"] = _clip01(fresh)
    if extra_dims:
        d.update(extra_dims)

    regime_label = getattr(getattr(regime, "regime", None), "value", "unknown")
    return MarketStateFingerprint(dims=d, regime=str(regime_label),
                                  timestamp=timestamp, instrument=instrument)

# --------------------------------------------------------------------------- #
# Causal normalization across a historical library
# --------------------------------------------------------------------------- #
class FingerprintNormalizer:
    """Rolling z-score/percentile normalization fitted on the LIBRARY ONLY.

    The library contains exclusively pre-as_of states, so statistics derived
    from it are available at forecast time — no future observations are used
    to normalize historical states.
    """

    def __init__(self) -> None:
        self._mean: dict[str, float] = {}
        self._std: dict[str, float] = {}

    def fit(self, library: list["LibraryEntry"]) -> "FingerprintNormalizer":
        cols: dict[str, list[float]] = {}
        for e in library:
            for k, v in e.fingerprint.dims.items():
                if v is not None:
                    cols.setdefault(k, []).append(v)
        self._mean = {k: sum(v) / len(v) for k, v in cols.items()}
        self._std = {k: (sum((x - self._mean[k]) ** 2 for x in v) / len(v)) ** 0.5
                     for k, v in cols.items()}
        return self

    def transform(self, fp: MarketStateFingerprint) -> MarketStateFingerprint:
        """Z-score each dim against library statistics; missing stays None.
        Constant dims (std==0) map to 0.5."""
        out: dict[str, Optional[float]] = {}
        for k, v in fp.dims.items():
            if v is None or k not in self._mean:
                out[k] = v
                continue
            sd = self._std.get(k, 0.0)
            out[k] = 0.5 if sd < 1e-9 else _clip01(0.5 + (v - self._mean[k]) / (4 * sd))
        return MarketStateFingerprint(dims=out, regime=fp.regime,
                                      timestamp=fp.timestamp,
                                      instrument=fp.instrument)

# --------------------------------------------------------------------------- #
# Similarity + historical library + pattern search
# --------------------------------------------------------------------------- #
@dataclass
class LibraryEntry:
    fingerprint: MarketStateFingerprint
    bar_index: int = -1


class SimilarityEngine:
    """Configurable deterministic similarity over common available dims.

    Weight per dim = group weight / (#common dims in that group), so each
    GROUP contributes constant mass regardless of missing dims. Zeroed
    groups (ablation) contribute nothing. Methods: weighted_euclidean,
    cosine."""

    def __init__(self, weights: Optional[FeatureWeights] = None,
                 method: str = "weighted_euclidean") -> None:
        self.weights = weights or FeatureWeights()
        self.method = method

    def similarity(self, a: MarketStateFingerprint,
                   b: MarketStateFingerprint) -> float:
        common = [d for d in a.dims
                  if a.dims[d] is not None and b.dims.get(d) is not None]
        if not common:
            return 0.0
        from collections import Counter
        gc = Counter(DIM_GROUPS.get(d, "price") for d in common)
        if self.method == "cosine":
            va, vb = [], []
            for d in common:
                g = DIM_GROUPS.get(d, "price")
                w = getattr(self.weights, g, 0.0) / max(1, gc[g])
                va.append((a.dims[d] - 0.5) * w)
                vb.append((b.dims[d] - 0.5) * w)
            num = sum(x * y for x, y in zip(va, vb))
            da = math.sqrt(sum(x * x for x in va))
            db = math.sqrt(sum(y * y for y in vb))
            if da == 0 or db == 0:
                return 0.0
            return max(0.0, num / (da * db))
        wsum = acc = 0.0
        for d in common:
            g = DIM_GROUPS.get(d, "price")
            w = getattr(self.weights, g, 0.0) / max(1, gc[g])
            wsum += w
            acc += w * ((a.dims[d] - b.dims[d]) ** 2)
        if wsum <= 0:
            return 0.0
        return max(0.0, 1.0 - math.sqrt(acc / wsum))


class HistoricalPatternEngine:
    """Pattern search over strictly-historical states.

    LOOKAHEAD GUARANTEES (enforced here — not caller discipline):
      1. find_matches returns ONLY entries with fingerprint.timestamp < as_of.
      2. With a horizon, only entries whose OUTCOME WINDOW HAS CLOSED by
         as_of (timestamp + horizon < as_of) are returned.
      3. build_library computes each bar's fingerprint causally
         (FeatureEngine.features_at slices data <= bar timestamp) and never
         includes bars at/after ``as_of``.
    """

    def __init__(self, engine: Optional[FeatureEngine] = None,
                 weights: Optional[FeatureWeights] = None,
                 similarity: str = "weighted_euclidean") -> None:
        self.engine = engine or FeatureEngine(lookback=60)
        self.weights = weights or FeatureWeights()
        self.similarity_engine = SimilarityEngine(self.weights, similarity)

    def build_library(self, df: pd.DataFrame, instrument: str = "",
                      start: int = 60, step: int = 1,
                      as_of: Optional[datetime] = None) -> list[LibraryEntry]:
        work = df.sort_index()
        n = len(work)
        entries: list[LibraryEntry] = []
        for i in range(start, n, step):
            ts = work.index[i]
            if not getattr(ts, "tzinfo", None):
                ts = ts.tz_localize("UTC")
            if as_of is not None and ts >= as_of:
                break  # RULE 1: nothing at/after as_of enters the library
            feats = self.engine.features_at(df, ts)
            regime = classify_regime(feats)
            fp = fingerprint_from_features(feats, regime, timestamp=ts,
                                           instrument=instrument)
            entries.append(LibraryEntry(fp, i))
        return entries

    def find_matches(
        self, current: MarketStateFingerprint,
        library: list[LibraryEntry], as_of: datetime,
        min_similarity: float = 0.80,
        horizon: Optional[timedelta] = None,
        max_matches: int = 50,
        normalizer: Optional[FingerprintNormalizer] = None,
    ) -> list[tuple[LibraryEntry, float]]:
        """Only historical states, only closed outcome windows, only above
        threshold. Deterministic ordering: similarity desc, timestamp asc."""
        as_of = as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)
        cur = normalizer.transform(current) if normalizer else current
        out: list[tuple[LibraryEntry, float]] = []
        for e in library:
            ts = e.fingerprint.timestamp
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts >= as_of:                                   # RULE: past only
                continue
            if horizon is not None and (ts + horizon) >= as_of:
                continue                                      # RULE: closed window
            cand = normalizer.transform(e.fingerprint) if normalizer else e.fingerprint
            sim = self.similarity_engine.similarity(cur, cand)
            if sim >= min_similarity:
                out.append((e, sim))
        out.sort(key=lambda t: (-t[1], t[0].fingerprint.timestamp or datetime.min.replace(tzinfo=UTC)))
        return out[:max_matches]

# --------------------------------------------------------------------------- #
# Outcome analysis + reliability + evidence
# --------------------------------------------------------------------------- #
@dataclass
class HorizonOutcome:
    """Outcome stats for one horizon. ``positive_rate`` is a HISTORICAL
    CONDITIONAL FREQUENCY — explicitly not a probability."""

    horizon: str = ""
    bars: int = 0
    n: int = 0
    positive: int = 0
    negative: int = 0
    neutral: int = 0
    positive_rate: Optional[float] = None
    avg_return: Optional[float] = None
    median_return: Optional[float] = None
    avg_mfe: Optional[float] = None
    avg_mae: Optional[float] = None
    avg_post_vol: Optional[float] = None
    ci_low: float = 0.0
    ci_high: float = 1.0


def analyze_outcomes(matches: list[tuple[LibraryEntry, float]],
                     df: pd.DataFrame,
                     horizons: dict[str, int]) -> dict[str, HorizonOutcome]:
    """Forward return / MFE / MAE / post-volatility per horizon. Outcomes are
    what happened AFTER the matched historical state — used only for
    historical statistics, never as current-state input."""
    closes, highs, lows = df["close"], df["high"], df["low"]
    n = len(df)
    out: dict[str, HorizonOutcome] = {}
    for name, bars in horizons.items():
        rets: list[float] = []
        mfes: list[float] = []
        maes: list[float] = []
        vols: list[float] = []
        pos = neg = neu = 0
        for entry, _sim in matches:
            i = entry.bar_index
            if i < 0 or i + bars >= n:
                continue
            e0 = float(closes.iloc[i])
            win = closes.iloc[i + 1:i + bars + 1]
            hi = highs.iloc[i + 1:i + bars + 1]
            lo = lows.iloc[i + 1:i + bars + 1]
            r = (float(win.iloc[-1]) / e0 - 1) * 100
            rets.append(r)
            mfes.append((float(hi.max()) / e0 - 1) * 100)
            maes.append((float(lo.min()) / e0 - 1) * 100)
            pr = win.pct_change().dropna()
            vols.append(float(pr.std(ddof=0)) * 100 if len(pr) > 1 else 0.0)
            if r > 0.05:
                pos += 1
            elif r < -0.05:
                neg += 1
            else:
                neu += 1
        m = len(rets)
        rate = pos / m if m else None
        lo_ci, hi_ci = _wilson(rate, m) if rate is not None else (0.0, 1.0)
        out[name] = HorizonOutcome(
            horizon=name, bars=bars, n=m, positive=pos, negative=neg,
            neutral=neu, positive_rate=rate,
            avg_return=(sum(rets) / m) if m else None,
            median_return=(sorted(rets)[m // 2] if m else None),
            avg_mfe=(sum(mfes) / m) if m else None,
            avg_mae=(sum(maes) / m) if m else None,
            avg_post_vol=(sum(vols) / m) if m else None,
            ci_low=lo_ci, ci_high=hi_ci)
    return out

@dataclass
class PatternReport:
    status: PatternStatus = PatternStatus.INSUFFICIENT_MATCHES
    match_count: int = 0
    primary: Optional[HorizonOutcome] = None
    primary_horizon: str = ""
    by_horizon: dict[str, HorizonOutcome] = field(default_factory=dict)
    regime_breakdown: dict[str, dict[str, Any]] = field(default_factory=dict)
    min_similarity: float = 0.0
    similarity_avg: Optional[float] = None
    warnings: list[str] = field(default_factory=list)


def build_pattern_report(
    matches: list[tuple[LibraryEntry, float]],
    df: pd.DataFrame,
    horizons: Optional[dict[str, int]] = None,
    min_matches: int = 15,
    min_similarity: float = 0.80,
) -> PatternReport:
    """Aggregate match outcomes + regime breakdown + honest status."""
    horizons = horizons or {"1D": 5}
    by_h = analyze_outcomes(matches, df, horizons)
    primary_name = next(iter(horizons))
    primary = by_h.get(primary_name)
    bars = horizons[primary_name]
    closes = df["close"]
    n = len(df)
    by_regime: dict[str, list[float]] = {}
    for entry, _s in matches:
        i = entry.bar_index
        if i < 0 or i + bars >= n:
            continue
        r = (float(closes.iloc[i + bars]) / float(closes.iloc[i]) - 1) * 100
        by_regime.setdefault(entry.fingerprint.regime, []).append(r)
    breakdown = {}
    for regime, rets in by_regime.items():
        rate = sum(1 for r in rets if r > 0.05) / len(rets)
        lo, hi = _wilson(rate, len(rets))
        breakdown[regime] = {"count": len(rets),
                             "positive_rate": round(rate, 3),
                             "avg_return": round(sum(rets) / len(rets), 3),
                             "ci": [round(lo, 3), round(hi, 3)]}
    warnings = ["historical conditional frequency — not a probability"]
    if primary is None or primary.n < min_matches:
        status = PatternStatus.INSUFFICIENT_MATCHES
        warnings.append(f"sample too small: {0 if primary is None else primary.n}"
                        f" < min_matches={min_matches}")
    elif primary.positive_rate is not None and \
            0.42 < primary.positive_rate < 0.58:
        status = PatternStatus.PATTERN_CONFLICTING
        warnings.append("mixed historical outcomes — no directional answer forced")
    elif primary.positive_rate is not None and \
            primary.ci_low <= 0.5 <= primary.ci_high:
        status = PatternStatus.PATTERN_WEAK
        warnings.append("confidence interval spans 50% — unreliable")
    else:
        status = PatternStatus.SUFFICIENT
    if primary is not None and primary.n < 50:
        warnings.append("small sample (<50 resolved outcomes)")
    sims = [s for _e, s in matches]
    return PatternReport(
        status=status, match_count=len(matches), primary=primary,
        primary_horizon=primary_name, by_horizon=by_h,
        regime_breakdown=breakdown, min_similarity=min_similarity,
        similarity_avg=(sum(sims) / len(sims)) if sims else None,
        warnings=warnings)


def pattern_to_evidence(ledger: EvidenceLedgerV2, report: PatternReport,
                        instrument: str = "") -> EvidenceLedgerV2:
    """Pattern evidence as its OWN ledger category — separate from technical,
    news and options evidence. Statuses map honestly onto availability."""
    if report.status == PatternStatus.INSUFFICIENT_MATCHES or \
            report.primary is None:
        ledger.add(EvidenceItem(
            category=EvidenceCategory.HISTORICAL_PATTERN,
            signal="historical_similarity",
            availability=EvidenceAvailability.UNAVAILABLE,
            data_quality=DataQualityTier.UNAVAILABLE,
            explanation=(f"pattern_status=INSUFFICIENT_MATCHES "
                         f"(matches={report.match_count})")))
        return ledger
    p = report.primary
    direction = ("bullish" if p.positive_rate > 0.55
                 else "bearish" if p.positive_rate < 0.45 else "neutral")
    avail = {PatternStatus.SUFFICIENT: EvidenceAvailability.SUPPORTED,
             PatternStatus.PATTERN_CONFLICTING: EvidenceAvailability.CONTRADICTORY,
             PatternStatus.PATTERN_WEAK: EvidenceAvailability.PARTIAL,
             }[report.status]
    dq = DataQualityTier.THIN if report.match_count < 50 else DataQualityTier.HEALTHY
    regimes = {k: v["count"] for k, v in report.regime_breakdown.items()}
    explanation = (f"Historical matches: {report.match_count}; "
                   f"positive rate: {p.positive_rate:.1%}; "
                   f"CI {p.ci_low:.0%}-{p.ci_high:.0%}; "
                   f"horizon={report.primary_horizon}; "
                   f"threshold={report.min_similarity:.2f}; "
                   f"regimes={regimes}; instrument={instrument or 'n/a'}; "
                   "historical conditional frequency, not probability")
    ledger.add(EvidenceItem(
        category=EvidenceCategory.HISTORICAL_PATTERN,
        signal="historical_similarity", direction=direction,
        strength=round((p.positive_rate or 0.5) * 100, 1), weight=1.2,
        source="pattern_engine", data_quality=dq, availability=avail,
        explanation=explanation))
    return ledger


def ablation_weights() -> dict[str, FeatureWeights]:
    """Explicit ablation weight sets A..F (Phase 27)."""
    return {name: FeatureWeights().masked(groups)
            for name, groups in ABLATION_CONFIGS.items()}
