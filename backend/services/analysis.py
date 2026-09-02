"""Analysis service wrapping the existing MarketIntelligenceEngine."""
from __future__ import annotations

import logging
import time
from typing import Optional

from schemas.market import AIAnalysisDTO, FactorDTO

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

    # Extract regime/bias
    regime = analysis.get("regime")
    bias = "neutral"
    if regime and hasattr(regime, "regime"):
        bias = _map_regime_to_bias(
            regime.regime.value if hasattr(regime.regime, "value") else str(regime.regime)
        )
        if hasattr(regime, "confidence"):
            confidence = float(regime.confidence)

    factors = _extract_factors(analysis)
    summary = _build_summary(analysis)

    return AIAnalysisDTO(
        symbol=symbol,
        timeframe=timeframe,
        bias=bias,
        confidence=confidence,
        signal=signal_direction,
        summary=summary,
        factors=factors,
        generated_at=int(time.time() * 1000),
        model="deterministic",
    )
