"""Analysis service wrapping the existing MarketIntelligenceEngine."""
from __future__ import annotations

import logging
import time
from typing import Optional

from schemas.market import (
    AIAnalysisDTO,
    FactorDTO,
    ExpectedMoveDTO,
    EvidenceDTO,
    OptionsCandidateDTO,
)

logger = logging.getLogger(__name__)


def _map_direction_to_signal(direction: str) -> str:
    """Map engine direction to frontend signal format."""
    mapping = {
        "LONG": "long",
        "SHORT": "short",
        "HOLD": "hold",
    }
    return mapping.get(direction.upper(), "no_signal")


def _map_regime_to_bias(regime: str) -> str:
    """Map engine regime to frontend bias format."""
    mapping = {
        "BULLISH": "bullish",
        "BEARISH": "bearish",
        "NEUTRAL": "neutral",
        "CHOPPY": "choppy",
    }
    return mapping.get(regime.upper(), "neutral")


def _extract_factors(analysis: dict) -> list[FactorDTO]:
    """Extract human-readable factors from analysis result."""
    factors = []

    # Add regime as a factor
    regime = analysis.get("regime")
    if regime and hasattr(regime, "regime"):
        factors.append(FactorDTO(
            label="Market Regime",
            value=regime.regime.value if hasattr(regime.regime, "value") else str(regime.regime),
            tone="positive" if "bull" in str(regime.regime).lower() else
                  "negative" if "bear" in str(regime.regime).lower() else "neutral",
        ))

    # Add trend from features
    features = analysis.get("features")
    if features and hasattr(features, "trend"):
        trend_val = features.trend.value if hasattr(features.trend, "value") else str(features.trend)
        factors.append(FactorDTO(
            label="Trend",
            value=trend_val,
            tone="positive" if "bull" in str(trend_val).lower() else
                  "negative" if "bear" in str(trend_val).lower() else "neutral",
        ))

    # Add momentum from RSI
    if features and hasattr(features, "rsi_14") and features.rsi_14 is not None:
        rsi = features.rsi_14
        if rsi > 70:
            tone = "warning"
            value = f"Overbought ({rsi:.1f})"
        elif rsi < 30:
            tone = "warning"
            value = f"Oversold ({rsi:.1f})"
        else:
            tone = "neutral"
            value = f"Neutral ({rsi:.1f})"
        factors.append(FactorDTO(label="RSI", value=value, tone=tone))

    # Add volatility
    if features and hasattr(features, "hist_vol") and features.hist_vol is not None:
        factors.append(FactorDTO(
            label="Volatility",
            value=f"{features.hist_vol:.2%}",
            tone="warning" if features.hist_vol > 0.3 else "neutral",
        ))

    # Add evidence confidence breakdown
    candidate = analysis.get("signal_candidate")
    if candidate and hasattr(candidate, "evidence") and candidate.evidence:
        ledger = candidate.evidence
        if ledger.positive:
            factors.append(FactorDTO(
                label="Positive Evidence",
                value=f"{len(ledger.positive)} factors",
                tone="positive",
            ))
        if ledger.negative:
            factors.append(FactorDTO(
                label="Negative Evidence",
                value=f"{len(ledger.negative)} factors",
                tone="negative",
            ))
        factors.append(FactorDTO(
            label="Evidence Agreement",
            value=ledger.agreement,
            tone="positive" if ledger.agreement == "strong" else
                  "negative" if ledger.agreement == "mixed" else "neutral",
        ))

    return factors


def _build_summary(analysis: dict) -> str:
    """Build a human-readable summary from analysis result."""
    parts = []

    regime = analysis.get("regime")
    if regime and hasattr(regime, "regime"):
        regime_val = regime.regime.value if hasattr(regime.regime, "value") else str(regime.regime)
        parts.append(f"Market regime: {regime_val}")

    features = analysis.get("features")
    if features and hasattr(features, "trend"):
        trend_val = features.trend.value if hasattr(features.trend, "value") else str(features.trend)
        parts.append(f"Trend: {trend_val}")

    candidate = analysis.get("signal_candidate")
    if candidate and hasattr(candidate, "direction"):
        direction_val = candidate.direction.value if hasattr(candidate.direction, "value") else str(candidate.direction)
        parts.append(f"Signal: {direction_val}")

    if features and hasattr(features, "data_points"):
        parts.append(f"Based on {features.data_points} data points")

    return ". ".join(parts) + "." if parts else "Analysis complete."


async def analyze_market(
    symbol: str,
    timeframe: str,
    df,
    health_status: Optional[str] = None,
) -> AIAnalysisDTO:
    """
    Run market analysis using the existing MarketIntelligenceEngine.

    Args:
        symbol: Trading symbol (e.g., "NSE:SBIN")
        timeframe: Timeframe (e.g., "1d", "1h")
        df: OHLCV DataFrame with tz-aware UTC index
        health_status: Optional feed health status

    Returns:
        AIAnalysisDTO with analysis results
    """
    from src.trading_system.research.intelligence import MarketIntelligenceEngine

    engine = MarketIntelligenceEngine(lookback=60)
    analysis = engine.analyze(
        symbol=symbol,
        timeframe=timeframe,
        df=df,
        health_status=health_status,
    )

    # Handle blocked analysis
    if analysis.get("status") == "BLOCKED":
        return AIAnalysisDTO(
            symbol=symbol,
            timeframe=timeframe,
            bias="neutral",
            confidence=0.0,
            signal="no_signal",
            summary=f"Analysis unavailable: {analysis.get('reason', 'Unknown')}",
            factors=[],
            generated_at=int(time.time() * 1000),
            model="blocked",
            decisionTimestamp=int(time.time() * 1000),
        )

    # Extract signal from candidate
    candidate = analysis.get("signal_candidate")
    signal_direction = "no_signal"
    confidence = 0.0
    if candidate:
        if hasattr(candidate, "direction"):
            signal_direction = _map_direction_to_signal(
                candidate.direction.value if hasattr(candidate.direction, "value") else str(candidate.direction)
            )
        if hasattr(candidate, "confidence"):
            confidence = float(candidate.confidence)

    # Extract regime/bias - use candidate confidence (evidence-based)
    regime = analysis.get("regime")
    bias = "neutral"
    if regime and hasattr(regime, "regime"):
        bias = _map_regime_to_bias(
            regime.regime.value if hasattr(regime.regime, "value") else str(regime.regime)
        )

    factors = _extract_factors(analysis)
    summary = _build_summary(analysis)

    # --- Phase 3-13 intelligence fields (evidence-based, never hardcoded) ---
    candidate = analysis.get("signal_candidate")
    horizon_val: Optional[str] = None
    expected_move_val = None
    evidence_val = None
    invalidation_val: Optional[str] = None
    instrument_class_val: Optional[str] = None

    context = analysis.get("instrument_context")
    if context is not None:
        instrument_class_val = getattr(context, "instrument_class", None)
        if hasattr(instrument_class_val, "value"):
            instrument_class_val = instrument_class_val.value

    if candidate:
        if getattr(candidate, "horizon", None) is not None:
            horizon_val = candidate.horizon.value
        em = getattr(candidate, "expected_move", None)
        if em is not None and getattr(em, "lower_pct", None) is not None:
            expected_move_val = ExpectedMoveDTO(
                lowerPct=em.lower_pct, upperPct=em.upper_pct, basis=em.basis
            )
        if getattr(candidate, "invalidation", None):
            invalidation_val = candidate.invalidation
        ledger = getattr(candidate, "evidence_ledger", None)
        if ledger is not None:
            evidence_val = EvidenceDTO(
                positive=list(ledger.positive),
                negative=list(ledger.negative),
                neutral=list(ledger.neutral),
                agreement=ledger.agreement,
            )

    options_candidates_val = [
        OptionsCandidateDTO(
            strike=c.strike,
            option_type=c.option_type,
            expiry=getattr(c, "expiry", None),
            delta=getattr(c, "delta", None),
            gamma=getattr(c, "gamma", None),
            theta=getattr(c, "theta", None),
            vega=getattr(c, "vega", None),
            implied_vol=getattr(c, "implied_vol", None),
            open_interest=getattr(c, "open_interest", None),
            volume=getattr(c, "volume", None),
            bid_ask_spread=getattr(c, "bid_ask_spread", None),
            score=getattr(c, "score", 0.0),
            rationale=list(getattr(c, "rationale", []) or []),
            risks=list(getattr(c, "risks", []) or []),
        )
        for c in (analysis.get("options_candidates") or [])
    ]
    options_status_val = analysis.get("options_status")

    last_close = None
    if df is not None and hasattr(df, "shape") and df.shape[0] > 0:
        try:
            last_close = float(df["close"].iloc[-1])
        except (TypeError, ValueError, KeyError):
            last_close = None

    # Snapshot context: a single decision time anchors generated_at,
    # decisionTimestamp and dataFreshnessMs so they are mutually consistent
    # rather than re-sampling time.time() (which would race across fields).
    now_ms = int(time.time() * 1000)
    market_ts = (
        _bar_ts_ms(df)
        if df is not None and hasattr(df, "index") and len(df.index) > 0
        else None
    )
    # Freshness = age of the source candle at decision time. Clamped to >= 0
    # to absorb minor exchange/clock skew; null when no market timestamp.
    data_freshness_ms = max(0, now_ms - market_ts) if market_ts is not None else None

    return AIAnalysisDTO(
        symbol=symbol,
        timeframe=timeframe,
        bias=bias,
        confidence=confidence,
        signal=signal_direction,
        summary=summary,
        factors=factors,
        generated_at=now_ms,
        model="deterministic",
        horizon=horizon_val,
        expectedMove=expected_move_val,
        evidence=evidence_val,
        invalidation=invalidation_val,
        instrumentClass=instrument_class_val,
        optionsCandidates=options_candidates_val,
        optionsStatus=options_status_val,
        decisionPrice=_safe_float(last_close),
        decisionTimestamp=now_ms,
        marketTimestamp=market_ts,
        dataFreshnessMs=data_freshness_ms,
    )


def _safe_float(v):
    import math
    if v is None:
        return None
    try:
        fv = float(v)
        return fv if math.isfinite(fv) else None
    except (TypeError, ValueError):
        return None


def _bar_ts_ms(df) -> int | None:
    from datetime import timezone
    idx = df.index[-1]
    ts = idx
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.astimezone(timezone.utc)
    return int(ts.timestamp() * 1000)
