"""Market Intelligence V3 — consensus, transitions, replay, calibration.

Builds ON TOP of the V2 engine (``intelligence.py``) without rewriting it.
The V2 engine computes per-timeframe / per-instrument features, regime and
evidence. This module adds the layers that synthesize those signals:

  Phase 2  Multi-timeframe consensus (disagreement & higher-TF conflict)
  Phase 3  Regime transition detection
  Phase 5  Options V2 derived analytics
  Phase 8  Evidence Ledger V2 (structured evidence items)
  Phase 9  Confidence Engine V2 integration
  Phase 10 Historical replay engine (strict no-lookahead)
  Phase 11 Forecast outcome labeling
  Phase 12 Calibration foundation
  Phase 13 Feature performance analysis

Rules:
  - Unavailable data is NEVER fabricated or turned into neutral evidence.
  - Confidence stays deterministic — the LLM never controls it.
  - Historical replay uses only data available at time T (no lookahead).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

from .intelligence import (
    FeatureEngine,
    InstrumentClass,
    InstrumentContext,
    MarketRegime,
    RegimeEnum,
    SignalDirection,
    TechnicalFeatures,
    TimeframeAnalysis,
    TrendEnum,
    _build_instrument_context,
    analyze_multi_timeframe,
    classify_regime,
    compute_expected_move,
    compute_invalidation,
    determine_horizon,
)
from .market_context import (
    DataQualityTier,
    EvidenceAvailability,
    MarketBreadth,
    SectorContext,
)

# --------------------------------------------------------------------------- #
# Phase 2 — Multi-timeframe consensus
# --------------------------------------------------------------------------- #
class TimeframeRole(str, Enum):
    """Logical role of a timeframe in consensus formation."""

    INTRADAY = "intraday"          # 5m, 15m
    SHORT_TERM = "short_term"      # 1h
    HIGHER = "higher"              # 1D (trend anchor)


def _role_for(timeframe: str) -> TimeframeRole:
    tf = timeframe.lower()
    if tf in ("5m", "15m"):
        return TimeframeRole.INTRADAY
    if tf in ("1h", "60m", "30m"):
        return TimeframeRole.SHORT_TERM
    return TimeframeRole.HIGHER


@dataclass
class MultiTimeframeConsensus:
    """Consensus across timeframes — NOT a simple average.

    Distinguishes direction agreement/disagreement, higher-TF trend,
    lower-TF momentum, and data quality per timeframe.
    """

    short_term_bias: Optional[str] = None
    short_term_alignment: str = "none"         # strong/moderate/weak/conflicted
    swing_bias: Optional[str] = None
    higher_timeframe_conflict: bool = False
    intraday_momentum: Optional[str] = None
    regime_agreement: str = "none"
    volatility_agreement: str = "none"
    participating_timeframes: int = 0
    total_timeframes: int = 0
    data_quality: DataQualityTier = DataQualityTier.UNAVAILABLE
    notes: list[str] = field(default_factory=list)


def _align(items: list[TimeframeAnalysis]) -> tuple[Optional[str], str]:
    """Return (dominant_bias, alignment) for a bucket of timeframes."""
    if not items:
        return None, "none"
    biases = [ta.bias.value for ta in items]
    n = len(biases)
    pos = biases.count("bullish")
    neg = biases.count("bearish")
    neu = biases.count("neutral")
    if pos > neg and pos > neu:
        dom = "bullish"
    elif neg > pos and neg > neu:
        dom = "bearish"
    else:
        dom = "neutral"
    majority = max(pos, neg, neu)
    ratio = majority / n
    if ratio >= 0.75 and neu != majority:
        align = "strong"
    elif ratio >= 0.5:
        align = "moderate"
    elif majority > 1:
        align = "weak"
    else:
        align = "conflicted"
    return dom, align


def compute_timeframe_consensus(
    tf_results: dict[str, TimeframeAnalysis],
) -> MultiTimeframeConsensus:
    """Derive consensus from per-timeframe analyses.

    A 5m/15m bearish + 1D bullish setup does NOT become a meaningless
    average — it produces a conflicted short-term bias with a higher-TF
    conflict flag.
    """
    consensus = MultiTimeframeConsensus()
    if not tf_results:
        consensus.notes.append("No timeframe data provided")
        return consensus

    consensus.total_timeframes = len(tf_results)

    intraday: list[TimeframeAnalysis] = []
    short_term: list[TimeframeAnalysis] = []
    higher: list[TimeframeAnalysis] = []
    for tf, ta in tf_results.items():
        role = _role_for(tf)
        if role == TimeframeRole.INTRADAY:
            intraday.append(ta)
        elif role == TimeframeRole.SHORT_TERM:
            short_term.append(ta)
        else:
            higher.append(ta)

    consensus.participating_timeframes = len(tf_results)

    st_bucket = intraday + short_term
    if st_bucket:
        consensus.short_term_bias, consensus.short_term_alignment = _align(st_bucket)

    if higher:
        consensus.swing_bias, _ = _align(higher)

    if intraday:
        consensus.intraday_momentum, _ = _align(intraday)

    # Higher-TF conflict.
    if st_bucket and higher:
        st_dom = consensus.short_term_bias
        sw_dom = consensus.swing_bias
        if st_dom and sw_dom and st_dom != "neutral" and sw_dom != "neutral":
            if st_dom != sw_dom:
                consensus.higher_timeframe_conflict = True
                consensus.notes.append(
                    f"Higher-TF conflict: short-term {st_dom} vs swing {sw_dom}"
                )

    # Regime agreement via evidence agreement.
    agreements = [ta.evidence.agreement for ta in tf_results.values()]
    strong = agreements.count("strong")
    mixed = agreements.count("mixed")
    if strong >= len(agreements) * 0.6:
        consensus.regime_agreement = "strong"
    elif mixed >= len(agreements) * 0.5:
        consensus.regime_agreement = "weak"
    else:
        consensus.regime_agreement = "moderate"

    # Volatility agreement.
    vol_scores = [ta.volatility_score for ta in tf_results.values()]
    if vol_scores:
        spread = max(vol_scores) - min(vol_scores)
        if spread < 15:
            consensus.volatility_agreement = "strong"
        elif spread < 30:
            consensus.volatility_agreement = "moderate"
        else:
            consensus.volatility_agreement = "weak"

        confs = [ta.confidence for ta in tf_results.values()]
    if confs:
        avg_conf = sum(confs) / len(confs)
        if avg_conf < 25:
            consensus.data_quality = DataQualityTier.THIN
        elif avg_conf < 45:
            consensus.data_quality = DataQualityTier.DEGRADED
        else:
            consensus.data_quality = DataQualityTier.HEALTHY

    return consensus


# --------------------------------------------------------------------------- #
# Phase 3 — Regime transition detection
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Phase 3 — Regime transition detection
# --------------------------------------------------------------------------- #
class TransitionRisk(str, Enum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


@dataclass
class RegimeTransition:
    """Current regime + transition risk."""

    regime: RegimeEnum = RegimeEnum.UNKNOWN
    transition_risk: TransitionRisk = TransitionRisk.LOW
    transition_type: Optional[str] = None
    notes: list[str] = field(default_factory=list)


def detect_regime_transition(
    current: MarketRegime,
    previous: Optional[MarketRegime] = None,
    features: Optional[TechnicalFeatures] = None,
) -> RegimeTransition:
    """Detect regime transitions using actual features.

    Distinguishes CURRENT REGIME from REGIME TRANSITION RISK.
    ``regime=TRENDING_UP, transition_risk=HIGH`` is valid.
    """
    result = RegimeTransition(regime=current.regime)

    if previous is None or previous.regime == current.regime:
        if features is not None:
            result.transition_risk = _assess_transition_risk(current, features)
        return result

    # Regime changed between previous and current.
    result.transition_type = f"{previous.regime.value}_to_{current.regime.value}"
    result.notes.append(
        f"Regime changed: {previous.regime.value} -> {current.regime.value}"
    )
    result.transition_risk = TransitionRisk.MODERATE

    volatile_shifts = {
        (RegimeEnum.TRENDING_UP, RegimeEnum.TRENDING_DOWN),
        (RegimeEnum.TRENDING_DOWN, RegimeEnum.TRENDING_UP),
        (RegimeEnum.LOW_VOLATILITY, RegimeEnum.HIGH_VOLATILITY),
        (RegimeEnum.RANGE_BOUND, RegimeEnum.HIGH_VOLATILITY),
    }
    if (previous.regime, current.regime) in volatile_shifts:
        result.transition_risk = TransitionRisk.HIGH
        result.notes.append("Volatile regime shift detected")

    if features is not None:
        feature_risk = _assess_transition_risk(current, features)
        if feature_risk == TransitionRisk.HIGH:
            result.transition_risk = TransitionRisk.HIGH
        elif feature_risk == TransitionRisk.MODERATE and result.transition_risk == TransitionRisk.LOW:
            result.transition_risk = TransitionRisk.MODERATE

    return result


def _assess_transition_risk(
    regime: MarketRegime, features: TechnicalFeatures
) -> TransitionRisk:
    """Heuristic transition risk from current features."""
    risk = TransitionRisk.LOW

    if features.rsi_14 is not None:
        if features.rsi_14 >= 80 or features.rsi_14 <= 20:
            risk = TransitionRisk.HIGH
        elif features.rsi_14 >= 70 or features.rsi_14 <= 30:
            risk = TransitionRisk.MODERATE

    if features.price_vs_sma20 is not None and features.roc is not None:
        stretched = abs(features.price_vs_sma20) > 0.05
        momentum_weakening = (
            (features.price_vs_sma20 > 0 and features.roc < 0)
            or (features.price_vs_sma20 < 0 and features.roc > 0)
        )
        if stretched and momentum_weakening:
            if risk == TransitionRisk.LOW:
                risk = TransitionRisk.MODERATE

    return risk


# --------------------------------------------------------------------------- #
# Phase 5 — Options V2 derived analytics
# --------------------------------------------------------------------------- #
@dataclass
class OptionsAnalytics:
    """Derived analytics for an options contract (only when data supplied)."""

    strike: float = 0.0
    option_type: str = "CE"
    moneyness: Optional[float] = None
    spread_pct: Optional[float] = None
    liquidity_score: Optional[float] = None      # 0..100
    iv_suitability: Optional[float] = None       # 0..100
    oi_significance: Optional[float] = None      # 0..100
    delta_suitability: Optional[float] = None    # 0..100
    theta_risk: Optional[float] = None           # 0..100 (higher = more risk)
    expected_move_compatible: Optional[bool] = None
    data_sufficient: bool = False
    missing_fields: list[str] = field(default_factory=list)


def compute_options_analytics(
    contract: dict,
    spot: float,
    expected_move_pct: Optional[float] = None,
) -> OptionsAnalytics:
    """Compute derived options analytics from a caller-supplied contract.

    Does NOT fabricate Greeks/IV/OI. Missing fields recorded explicitly.
    """
    analytics = OptionsAnalytics(
        strike=float(contract.get("strike", 0)),
        option_type=str(contract.get("option_type", "CE")),
    )

    # Moneyness.
    strike = analytics.strike
    if strike > 0 and spot > 0:
        analytics.moneyness = (spot - strike) / strike
    else:
        analytics.missing_fields.append("moneyness (invalid strike/spot)")

    # Spread percentage.
    bid = contract.get("bid")
    ask = contract.get("ask")
    if bid is not None and ask is not None and bid > 0:
        analytics.spread_pct = (ask - bid) / bid * 100.0
    else:
        analytics.missing_fields.append("spread")

    # Liquidity score (volume + OI based).
    volume = contract.get("volume")
    oi = contract.get("open_interest")
    liq = 0.0
    has_liq = False
    if volume is not None:
        if volume > 5000:
            liq += 40
        elif volume > 1000:
            liq += 25
        elif volume > 100:
            liq += 10
        has_liq = True
    if oi is not None:
        if oi > 20000:
            liq += 40
        elif oi > 5000:
            liq += 25
        elif oi > 500:
            liq += 10
        has_liq = True
    if has_liq:
        analytics.liquidity_score = min(liq, 100)
    else:
        analytics.missing_fields.append("liquidity (volume/OI)")

    # IV suitability.
    iv = contract.get("implied_vol")
    if iv is not None:
        if 0.10 <= iv <= 0.45:
            analytics.iv_suitability = 80.0
        elif 0.05 <= iv < 0.10 or 0.45 < iv <= 0.60:
            analytics.iv_suitability = 50.0
        else:
            analytics.iv_suitability = 20.0
    else:
        analytics.missing_fields.append("implied_vol")

    # OI significance.
    if oi is not None:
        if oi > 10000:
            analytics.oi_significance = 90.0
        elif oi > 2000:
            analytics.oi_significance = 60.0
        elif oi > 200:
            analytics.oi_significance = 30.0
        else:
            analytics.oi_significance = 10.0

    # Delta suitability.
    delta = contract.get("delta")
    if delta is not None:
        ad = abs(delta)
        if 0.30 <= ad <= 0.60:
            analytics.delta_suitability = 90.0
        elif 0.20 <= ad <= 0.70:
            analytics.delta_suitability = 60.0
        else:
            analytics.delta_suitability = 30.0
    else:
        analytics.missing_fields.append("delta")

    # Theta risk.
    theta = contract.get("theta")
    if theta is not None:
        analytics.theta_risk = min(abs(theta) * 500, 100)
    else:
        analytics.missing_fields.append("theta")

    # Expected-move compatibility.
    if expected_move_pct is not None and analytics.moneyness is not None:
        # For a directional trade, the strike should be near the expected move.
        move_pct = abs(expected_move_pct) / 100.0
        analytics.expected_move_compatible = analytics.moneyness <= move_pct * 1.5

        # Data sufficiency: need at least delta + IV + one liquidity field.
    has_delta = contract.get("delta") is not None
    has_iv = contract.get("implied_vol") is not None
    has_liquidity = volume is not None or oi is not None
    analytics.data_sufficient = has_delta and has_iv and has_liquidity

    return analytics


# --------------------------------------------------------------------------- #
# Phase 8 — Evidence Ledger V2 (structured evidence items)
# --------------------------------------------------------------------------- #
class EvidenceCategory(str, Enum):
    TREND = "trend"
    MOMENTUM = "momentum"
    VOLUME = "volume"
    VOLATILITY = "volatility"
    STRUCTURE = "structure"
    RELATIVE_STRENGTH = "relative_strength"
    BREADTH = "breadth"
    INDIA_VIX = "india_vix"
    FII_DII = "fii_dii"
    SECTOR = "sector"
    OPTIONS = "options"
    NEWS = "news"
    CROSS_ASSET = "cross_asset"
    TIMEFRAME_ALIGNMENT = "timeframe_alignment"
    REGIME_TRANSITION = "regime_transition"
    HISTORICAL_PATTERN = "historical_pattern"   # V4: pattern-match evidence


@dataclass
class EvidenceItem:
    """A single structured evidence entry."""

    category: EvidenceCategory
    signal: str
    direction: Optional[str] = None
    strength: float = 0.0                        # 0..100
    weight: float = 1.0
    source: Optional[str] = None
    data_quality: DataQualityTier = DataQualityTier.HEALTHY
    timestamp: Optional[datetime] = None
    availability: EvidenceAvailability = EvidenceAvailability.SUPPORTED
    explanation: str = ""

    @property
    def effective_weight(self) -> float:
        if self.availability != EvidenceAvailability.SUPPORTED:
            return 0.0
        return self.weight * (self.strength / 100.0)


@dataclass
class EvidenceLedgerV2:
    """Structured evidence ledger distinguishing supported / contradictory /
    unavailable / insufficient.
    """

    items: list[EvidenceItem] = field(default_factory=list)

    def add(self, item: EvidenceItem) -> None:
        self.items.append(item)

    @property
    def supported(self) -> list[EvidenceItem]:
        return [i for i in self.items if i.availability == EvidenceAvailability.SUPPORTED]

    @property
    def contradictory(self) -> list[EvidenceItem]:
        return [i for i in self.items if i.availability == EvidenceAvailability.CONTRADICTORY]

    @property
    def unavailable(self) -> list[EvidenceItem]:
        return [i for i in self.items if i.availability == EvidenceAvailability.UNAVAILABLE]

    @property
    def insufficient(self) -> list[EvidenceItem]:
        return [i for i in self.items if i.availability == EvidenceAvailability.INSUFFICIENT_DATA]

    @property
    def agreement(self) -> str:
        bullish = sum(i.effective_weight for i in self.supported if i.direction == "bullish")
        bearish = sum(i.effective_weight for i in self.supported if i.direction == "bearish")
        total = bullish + bearish
        if total == 0:
            return "neutral"
        ratio = abs(bullish - bearish) / total
        if ratio >= 0.6:
            return "strong"
        elif ratio >= 0.3:
            return "moderate"
        else:
            return "mixed"


# --------------------------------------------------------------------------- #
# Phase 9 — Confidence Engine V2 integration
# --------------------------------------------------------------------------- #
def build_evidence_ledger_v2(
    features: TechnicalFeatures,
    regime: MarketRegime,
    direction: SignalDirection,
    consensus: Optional[MultiTimeframeConsensus] = None,
    breadth: Optional[MarketBreadth] = None,
    sector: Optional[SectorContext] = None,
    transition: Optional[RegimeTransition] = None,
) -> EvidenceLedgerV2:
    """Build a structured V2 evidence ledger from all available contexts.

    Missing data is recorded as UNAVAILABLE — never converted to neutral.
    """
    ledger = EvidenceLedgerV2()

    # Trend evidence.
    if features.price_vs_sma20 is not None:
        d = features.price_vs_sma20
        aligned = (direction == SignalDirection.LONG and d > 0) or (
            direction == SignalDirection.SHORT and d < 0
        )
        avail = EvidenceAvailability.SUPPORTED if aligned else EvidenceAvailability.CONTRADICTORY
        dir_label = "bullish" if d > 0 else "bearish"
        strength = min(abs(d) / 0.05 * 100, 100)
        ledger.add(EvidenceItem(
            category=EvidenceCategory.TREND, signal="price_vs_sma20",
            direction=dir_label, strength=strength, weight=2.0,
            availability=avail,
            explanation=f"Price {d*100:.1f}% vs SMA20",
        ))
    else:
        ledger.add(EvidenceItem(
            category=EvidenceCategory.TREND, signal="price_vs_sma20",
            availability=EvidenceAvailability.UNAVAILABLE,
            explanation="SMA20 unavailable",
        ))

    # Momentum evidence (RSI).
    if features.rsi_14 is not None:
        rsi = features.rsi_14
        if direction == SignalDirection.LONG:
            dir_label = "bullish" if 30 < rsi <= 70 else "bearish"
            strength = 70.0 if 40 <= rsi <= 65 else 40.0
        elif direction == SignalDirection.SHORT:
            dir_label = "bearish" if 30 <= rsi < 70 else "bullish"
            strength = 70.0 if 35 <= rsi <= 60 else 40.0
        else:
            dir_label = "neutral"
            strength = 30.0
        ledger.add(EvidenceItem(
            category=EvidenceCategory.MOMENTUM, signal="rsi_14",
            direction=dir_label, strength=strength, weight=1.5,
            explanation=f"RSI {rsi:.1f}",
        ))
    else:
        ledger.add(EvidenceItem(
            category=EvidenceCategory.MOMENTUM, signal="rsi_14",
            availability=EvidenceAvailability.UNAVAILABLE,
            explanation="RSI unavailable",
        ))

    # Breadth evidence.
    if breadth is not None and breadth.available:
        strength_map = {"strong": 85.0, "moderate": 60.0, "weak": 35.0}
        bstrength = strength_map.get(breadth.breadth_strength or "", 50.0)
        dir_label = "bullish" if (
            breadth.advance_decline_ratio is not None and breadth.advance_decline_ratio > 1
        ) else "bearish"
        ledger.add(EvidenceItem(
            category=EvidenceCategory.BREADTH, signal="advance_decline",
            direction=dir_label, strength=bstrength, weight=1.0,
            data_quality=breadth.data_quality,
            explanation=f"A/D ratio {breadth.advance_decline_ratio:.2f}",
        ))
    else:
        ledger.add(EvidenceItem(
            category=EvidenceCategory.BREADTH, signal="advance_decline",
            availability=EvidenceAvailability.UNAVAILABLE,
            explanation="Breadth data unavailable",
        ))

    # Sector evidence.
    if sector is not None and sector.available:
        dir_label = "bullish" if (sector.sector_return or 0) > 0 else "bearish"
        strength = min(abs(sector.sector_return or 0) * 1000, 100)
        ledger.add(EvidenceItem(
            category=EvidenceCategory.SECTOR, signal="sector_return",
            direction=dir_label, strength=strength, weight=1.0,
            data_quality=sector.data_quality,
            explanation=f"Sector {sector.sector_name} return {(sector.sector_return or 0)*100:.2f}%",
        ))
    else:
        ledger.add(EvidenceItem(
            category=EvidenceCategory.SECTOR, signal="sector_return",
            availability=EvidenceAvailability.UNAVAILABLE,
            explanation="Sector context unavailable",
        ))

    # Multi-timeframe alignment.
    if consensus is not None and consensus.participating_timeframes > 0:
        dir_label = consensus.short_term_bias or "neutral"
        alignment_map = {
            "strong": 90.0, "moderate": 65.0, "weak": 40.0, "conflicted": 20.0,
        }
        strength = alignment_map.get(consensus.short_term_alignment, 30.0)
        ledger.add(EvidenceItem(
            category=EvidenceCategory.TIMEFRAME_ALIGNMENT, signal="consensus",
            direction=dir_label, strength=strength, weight=1.5,
            data_quality=consensus.data_quality,
            explanation=f"Short-term {dir_label} ({consensus.short_term_alignment})",
        ))
    else:
        ledger.add(EvidenceItem(
            category=EvidenceCategory.TIMEFRAME_ALIGNMENT, signal="consensus",
            availability=EvidenceAvailability.UNAVAILABLE,
            explanation="No multi-timeframe data",
        ))

    # Regime transition.
    if transition is not None:
        risk_map = {
            TransitionRisk.LOW: 80.0,
            TransitionRisk.MODERATE: 50.0,
            TransitionRisk.HIGH: 25.0,
        }
        strength = risk_map.get(transition.transition_risk, 50.0)
        ledger.add(EvidenceItem(
            category=EvidenceCategory.REGIME_TRANSITION, signal="transition_risk",
            direction="neutral", strength=strength, weight=1.0,
            explanation=f"Regime {transition.regime.value}, "
                        f"transition risk {transition.transition_risk.value}",
        ))
    else:
        ledger.add(EvidenceItem(
            category=EvidenceCategory.REGIME_TRANSITION, signal="transition_risk",
            availability=EvidenceAvailability.UNAVAILABLE,
            explanation="No transition data",
        ))

    return ledger


def compute_confidence_v2(ledger: EvidenceLedgerV2) -> tuple[float, str]:
    """Compute deterministic confidence from the V2 evidence ledger.

    Returns (confidence_0_100, level_label).
    Missing data never inflates confidence.
    """
    if not ledger.supported:
        return 0.0, "NO TRADE / INSUFFICIENT EVIDENCE"

    total_weight = sum(i.weight for i in ledger.supported)
    if total_weight == 0:
        return 0.0, "NO TRADE / INSUFFICIENT EVIDENCE"

    weighted = sum(i.effective_weight for i in ledger.supported)
    raw = (weighted / total_weight) * 100

    # Penalize for contradictory evidence.
    contra = sum(i.strength for i in ledger.contradictory)
    if contra > 50:
        raw *= 0.7
    elif contra > 20:
        raw *= 0.85

    # Agreement adjustment.
    agreement = ledger.agreement
    if agreement == "mixed":
        raw *= 0.75
    elif agreement == "strong":
        raw = min(raw * 1.1, 100)

    confidence = round(min(max(raw, 0), 100), 1)

    if confidence >= 70:
        level = "HIGH"
    elif confidence >= 45:
        level = "MEDIUM"
    elif confidence >= 25:
        level = "LOW"
    else:
        level = "NO TRADE / INSUFFICIENT EVIDENCE"

    return confidence, level


# --------------------------------------------------------------------------- #
# Phase 10 — Historical replay engine (strict no-lookahead)
# --------------------------------------------------------------------------- #
@dataclass
class ReplayForecast:
    """A single forecast produced during historical replay."""

    timestamp: datetime
    instrument: str
    timeframe: str
    bias: str
    confidence: float
    horizon: str
    direction: str
    expected_move_lower_pct: Optional[float] = None
    expected_move_upper_pct: Optional[float] = None
    invalidation: Optional[str] = None


@dataclass
class ReplayResult:
    """Result of a historical replay run."""

    forecasts: list[ReplayForecast] = field(default_factory=list)
    lookahead_violations: int = 0


def replay_history(
    instrument: str,
    timeframe: str,
    df: pd.DataFrame,
    forecast_store: Optional[object] = None,
    start_idx: int = 60,
    step: int = 5,
) -> ReplayResult:
    """Historical market replay with STRICT no-lookahead guarantee.

    At each historical timestamp T:
      1. only data with index <= T is exposed (causal slice)
      2. features/regime/evidence/forecast computed on that slice
      3. forecast stored (if forecast_store provided)
      4. outcome determined from ACTUAL future bars (only for labeling,
         never fed back into the forecast)

    ABSOLUTE RULE: a forecast at T never uses future OHLCV/indicators/news.
    """
    result = ReplayResult()
    if df is None or len(df) < start_idx + 5:
        return result

    work = df.sort_index()
    engine = FeatureEngine(lookback=60)

    idx = start_idx
    while idx < len(work) - 5:
        ts = work.index[idx]

        # CAUSAL SLICE: only data available at or before T.
        causal = work.iloc[: idx + 1]
        if causal.index[-1] > ts:
            raise ValueError(
                f"Lookahead violation: causal slice extends past {ts}"
            )

        features = engine.compute(causal, InstrumentClass.EQUITY)
        regime = classify_regime(features)

                # Direction: regime first, then fall back to feature trend (matches
        # generate_signal_candidate semantics).
        if regime.regime == RegimeEnum.TRENDING_UP:
            direction = SignalDirection.LONG
            bias = "bullish"
        elif regime.regime == RegimeEnum.TRENDING_DOWN:
            direction = SignalDirection.SHORT
            bias = "bearish"
        elif features.trend == TrendEnum.BULLISH:
            direction = SignalDirection.LONG
            bias = "bullish"
        elif features.trend == TrendEnum.BEARISH:
            direction = SignalDirection.SHORT
            bias = "bearish"
        else:
            direction = SignalDirection.NEUTRAL
            bias = "neutral"

        horizon = determine_horizon(features, regime)
        instr_ctx = _build_instrument_context(instrument)
        expected_move = compute_expected_move(features, horizon, instr_ctx)
        invalidation = compute_invalidation(features, direction)

        ledger = build_evidence_ledger_v2(features, regime, direction)
        confidence, _ = compute_confidence_v2(ledger)

        forecast = ReplayForecast(
            timestamp=ts,
            instrument=instrument,
            timeframe=timeframe,
            bias=bias,
            confidence=confidence,
            horizon=horizon.value,
            direction=direction.value,
            expected_move_lower_pct=expected_move.lower_pct if expected_move else None,
            expected_move_upper_pct=expected_move.upper_pct if expected_move else None,
            invalidation=invalidation,
        )
        result.forecasts.append(forecast)

        if forecast_store is not None:
            forecast_store.record_forecast(
                instrument=instrument,
                timeframe=timeframe,
                bias=bias,
                confidence=confidence / 100.0,
                horizon=horizon.value,
                forecast=f"Replay forecast at {ts}",
                expected_move_lower_pct=forecast.expected_move_lower_pct,
                expected_move_upper_pct=forecast.expected_move_upper_pct,
                invalidation=invalidation,
                created_at=ts,
            )

        idx += step

    return result


# --------------------------------------------------------------------------- #
# Phase 11 — Forecast outcome labeling
# --------------------------------------------------------------------------- #
@dataclass
class OutcomeLabel:
    """Outcome label for a directional forecast."""

    outcome: str = "unknown"          # success / failure / neutral
    realized_return_pct: Optional[float] = None
    max_favorable_excursion_pct: Optional[float] = None
    max_adverse_excursion_pct: Optional[float] = None
    target_hit: Optional[bool] = None
    invalidation_hit: Optional[bool] = None
    horizon_completed: bool = False


def label_outcome(
    bias: str,
    entry_price: float,
    future_prices: list[float],
    expected_move_lower_pct: Optional[float] = None,
    expected_move_upper_pct: Optional[float] = None,
) -> OutcomeLabel:
    """Label a forecast outcome from ACTUAL future prices.

    LONG: success if price reaches expected-move upper before lower (adverse).
    SHORT: success if price reaches expected-move lower before upper.
    NEUTRAL: success if price stays within a tight band.
    """
    label = OutcomeLabel()
    if not future_prices or entry_price <= 0:
        return label

    returns = [(p - entry_price) / entry_price * 100.0 for p in future_prices]
    label.max_favorable_excursion_pct = max(returns)
    label.max_adverse_excursion_pct = min(returns)
    label.realized_return_pct = returns[-1] if returns else None
    label.horizon_completed = True

    if bias == "bullish":
        target = expected_move_upper_pct if expected_move_upper_pct is not None else 1.0
        invalid = expected_move_lower_pct if expected_move_lower_pct is not None else -1.0
        label.target_hit = any(r >= target for r in returns)
        label.invalidation_hit = any(r <= invalid for r in returns)
        if label.target_hit and not label.invalidation_hit:
            label.outcome = "success"
        elif label.invalidation_hit and not label.target_hit:
            label.outcome = "failure"
        elif label.target_hit and label.invalidation_hit:
            target_idx = next(i for i, r in enumerate(returns) if r >= target)
            invalid_idx = next(i for i, r in enumerate(returns) if r <= invalid)
            label.outcome = "success" if target_idx <= invalid_idx else "failure"
        else:
            label.outcome = "neutral" if abs(returns[-1]) < 0.5 else (
                "success" if returns[-1] > 0 else "failure"
            )
    elif bias == "bearish":
        target = abs(expected_move_lower_pct if expected_move_lower_pct is not None else -1.0)
        invalid = abs(expected_move_upper_pct if expected_move_upper_pct is not None else 1.0)
        label.target_hit = any(r <= -target for r in returns)
        label.invalidation_hit = any(r >= invalid for r in returns)
        if label.target_hit and not label.invalidation_hit:
            label.outcome = "success"
        elif label.invalidation_hit and not label.target_hit:
            label.outcome = "failure"
        elif label.target_hit and label.invalidation_hit:
            target_idx = next(i for i, r in enumerate(returns) if r <= -target)
            invalid_idx = next(i for i, r in enumerate(returns) if r >= invalid)
            label.outcome = "success" if target_idx <= invalid_idx else "failure"
        else:
            label.outcome = "neutral" if abs(returns[-1]) < 0.5 else (
                "success" if returns[-1] < 0 else "failure"
            )
    else:  # neutral
        label.outcome = "success" if all(abs(r) <= 0.5 for r in returns) else "failure"
        label.target_hit = label.outcome == "success"
        label.invalidation_hit = False

    return label


# --------------------------------------------------------------------------- #
# Phase 12 — Calibration foundation
# --------------------------------------------------------------------------- #
@dataclass
class CalibrationBucket:
    """One confidence bucket in a calibration report."""

    bucket_range: str = ""
    forecasts: int = 0
    wins: int = 0
    win_rate: Optional[float] = None
    avg_return_pct: Optional[float] = None


@dataclass
class CalibrationReport:
    """Calibration report across confidence buckets."""

    buckets: list[CalibrationBucket] = field(default_factory=list)
    total_forecasts: int = 0
    total_resolved: int = 0
    sample_sufficient: bool = False
    note: str = ""


def compute_calibration(
    outcomes: list[tuple[float, bool]],  # (confidence_0_100, was_hit)
    min_resolved: int = 100,
) -> CalibrationReport:
    """Compute calibration from (confidence, hit) pairs.

    DOES NOT claim confidence is probability. Reports raw observed
    frequencies and explicitly flags insufficient sample size.
    """
    report = CalibrationReport()
    report.total_resolved = len(outcomes)
    report.sample_sufficient = len(outcomes) >= min_resolved

    if not report.sample_sufficient:
        report.note = (
            f"Insufficient sample size: {len(outcomes)} resolved "
            f"(need {min_resolved} for provisional calibration)."
        )

    edges = [(0, 20), (20, 40), (40, 50), (50, 60), (60, 70), (70, 80), (80, 100)]
    for lo, hi in edges:
        bucket = CalibrationBucket(bucket_range=f"{lo}-{hi}")
        for conf, hit in outcomes:
            if lo <= conf < hi or (hi == 100 and conf == 100):
                bucket.forecasts += 1
                if hit:
                    bucket.wins += 1
        if bucket.forecasts > 0:
            bucket.win_rate = bucket.wins / bucket.forecasts
        report.buckets.append(bucket)
        report.total_forecasts += bucket.forecasts

    return report


# --------------------------------------------------------------------------- #
# Phase 13 — Feature performance analysis
# --------------------------------------------------------------------------- #
@dataclass
class FeaturePerformance:
    """Performance metrics for one evidence category."""

    category: str = ""
    forecast_count: int = 0
    win_count: int = 0
    win_rate: Optional[float] = None
    avg_return_pct: Optional[float] = None
    sample_confidence: str = "insufficient"  # insufficient / provisional / adequate


def analyze_feature_performance(
    feature_outcomes: dict[str, list[tuple[bool, float]]],
    min_samples: int = 30,
) -> list[FeaturePerformance]:
    """Measure which evidence categories correlate with successful outcomes.

    ``feature_outcomes`` maps category -> [(was_hit, return_pct), ...].
    Research-only analysis layer — does not change production weights.
    """
    results = []
    for category, outcomes in feature_outcomes.items():
        perf = FeaturePerformance(category=category)
        perf.forecast_count = len(outcomes)
        if perf.forecast_count == 0:
            results.append(perf)
            continue

        wins = sum(1 for hit, _ in outcomes if hit)
        returns = [ret for _, ret in outcomes]
        perf.win_count = wins
        perf.win_rate = wins / perf.forecast_count
        perf.avg_return_pct = sum(returns) / len(returns) if returns else None

        if perf.forecast_count < min_samples:
            perf.sample_confidence = "insufficient"
        elif perf.forecast_count < min_samples * 3:
            perf.sample_confidence = "provisional"
        else:
            perf.sample_confidence = "adequate"

        results.append(perf)

    return results

