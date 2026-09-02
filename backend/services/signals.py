"""Signal service using real MarketIntelligenceEngine analysis.

This service generates trading signals by:
1. Fetching real market data from Upstox
2. Running MarketIntelligenceEngine analysis
3. Converting the analysis to a MarketView
4. Calling generate_signal() with the real snapshot and view
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from schemas.market import SignalDTO

logger = logging.getLogger(__name__)


async def generate_signals(
    symbol: str,
    timeframe: str,
    df,
    access_token: str,
    limit: int = 12,
) -> list[SignalDTO]:
    """
    Generate trading signals using real analysis.

    Args:
        symbol: Trading symbol
        timeframe: Timeframe
        df: OHLCV DataFrame
        access_token: Upstox access token (unused but kept for API compatibility)
        limit: Maximum number of signals (currently returns 1 signal per symbol)

    Returns:
        List of SignalDTO objects

    Note:
        The current architecture supports one signal per symbol analysis.
        The 'limit' parameter is accepted for API compatibility but the
        actual number of signals depends on the number of symbols analyzed.
    """
    try:
        from src.trading_system.models.snapshot import build_snapshot_from_df
        from src.trading_system.signals import generate_signal, SignalConfig

        # Build snapshot from DataFrame
        snapshot = build_snapshot_from_df(df, symbol, timeframe)
        if not snapshot:
            return []

        # Run real analysis to get MarketView
        view = await _analyze_and_build_view(symbol, timeframe, df)
        if not view:
            return []

        # Generate signal using real snapshot and view
        config = SignalConfig(min_data_points=30, min_confidence=0.5)
        signal = generate_signal(snapshot, view, config)

        # Convert to DTO
        return [_signal_to_dto(signal, symbol)]

    except Exception as e:
        logger.error("Failed to generate signals for %s: %s", symbol, str(e))
        return []


async def _analyze_and_build_view(
    symbol: str,
    timeframe: str,
    df,
) -> Optional[object]:
    """
    Run MarketIntelligenceEngine analysis and convert to MarketView.

    This is the canonical transformation from analysis results to MarketView.
    It uses the existing engine's output to build a properly validated view.
    """
    try:
        from src.trading_system.research.intelligence import MarketIntelligenceEngine
        from src.trading_system.models.market_view import MarketView, MarketViewEnum

        # Run analysis
        engine = MarketIntelligenceEngine(lookback=60)
        analysis = engine.analyze(
            symbol=symbol,
            timeframe=timeframe,
            df=df,
        )

        # Handle blocked analysis
        if analysis.get("status") == "BLOCKED":
            logger.warning("Analysis blocked for %s: %s", symbol, analysis.get("reason"))
            return None

        # Extract components
        regime = analysis.get("regime")
        features = analysis.get("features")
        candidate = analysis.get("signal_candidate")
        explanation = analysis.get("explanation")

        # Map regime to MarketView
        market_view = _map_regime_to_view(regime)

        # Get confidence from candidate or regime
        confidence = _extract_confidence(candidate, regime)

        # Build reasoning summary from explanation
        reasoning_summary = _build_reasoning_summary(explanation, regime, features)

        # Extract factors
        bullish_factors, bearish_factors = _extract_factors(features, regime)

        # Create MarketView
        view = MarketView(
            symbol=symbol,
            timeframe=timeframe,
            market_view=market_view,
            confidence=confidence,
            reasoning_summary=reasoning_summary,
            bullish_factors=bullish_factors,
            bearish_factors=bearish_factors,
            risks=_extract_risks(regime),
            invalidating_conditions=_extract_invalidating_conditions(candidate),
            model="deterministic",
            generated_at=str(int(time.time() * 1000)),
        )

        return view

    except Exception as e:
        logger.error("Failed to analyze and build view for %s: %s", symbol, str(e))
        return None


def _map_regime_to_view(regime) -> object:
    """Map MarketRegime to MarketViewEnum."""
    from src.trading_system.models.market_view import MarketViewEnum

    if not regime or not hasattr(regime, "regime"):
        return MarketViewEnum.NEUTRAL

    regime_val = regime.regime.value if hasattr(regime.regime, "value") else str(regime.regime)

    mapping = {
        "trending_up": MarketViewEnum.BULLISH,
        "trending_down": MarketViewEnum.BEARISH,
        "range_bound": MarketViewEnum.CHOPPY,
        "high_volatility": MarketViewEnum.CHOPPY,
        "low_volatility": MarketViewEnum.NEUTRAL,
        "unknown": MarketViewEnum.NEUTRAL,
    }

    return mapping.get(regime_val, MarketViewEnum.NEUTRAL)


def _extract_confidence(candidate, regime) -> float:
    """Extract confidence from candidate or regime."""
    if candidate and hasattr(candidate, "confidence"):
        return float(candidate.confidence)
    if regime and hasattr(regime, "confidence"):
        return float(regime.confidence)
    return 0.5


def _build_reasoning_summary(explanation, regime, features) -> str:
    """Build reasoning summary from analysis explanation."""
    parts = []

    if explanation and hasattr(explanation, "summary"):
        parts.append(explanation.summary)
    elif regime and hasattr(regime, "regime"):
        regime_val = regime.regime.value if hasattr(regime.regime, "value") else str(regime.regime)
        parts.append(f"Market regime: {regime_val}")

    if features and hasattr(features, "data_points"):
        parts.append(f"Based on {features.data_points} data points")

    return ". ".join(parts) if parts else "Analysis complete."


def _extract_factors(features, regime) -> tuple[list[str], list[str]]:
    """Extract bullish and bearish factors from features."""
    bullish = []
    bearish = []

    if not features:
        return bullish, bearish

    # Trend factors
    if hasattr(features, "trend"):
        trend_val = features.trend.value if hasattr(features.trend, "value") else str(features.trend)
        if "bull" in str(trend_val).lower():
            bullish.append(f"Bullish trend: {trend_val}")
        elif "bear" in str(trend_val).lower():
            bearish.append(f"Bearish trend: {trend_val}")

    # RSI factors
    if hasattr(features, "rsi_14") and features.rsi_14 is not None:
        rsi = features.rsi_14
        if rsi <= 30:
            bullish.append(f"RSI oversold: {rsi:.1f}")
        elif rsi >= 70:
            bearish.append(f"RSI overbought: {rsi:.1f}")

    # Price vs SMA
    if hasattr(features, "price_vs_sma20") and features.price_vs_sma20 is not None:
        if features.price_vs_sma20 > 0:
            bullish.append(f"Price {features.price_vs_sma20*100:.1f}% above SMA20")
        else:
            bearish.append(f"Price {abs(features.price_vs_sma20)*100:.1f}% below SMA20")

    # Volume
    if hasattr(features, "unusual_volume") and features.unusual_volume:
        bullish.append("Unusual volume detected")

    return bullish, bearish


def _extract_risks(regime) -> list[str]:
    """Extract risks from regime."""
    risks = []
    if regime and hasattr(regime, "warnings"):
        risks.extend(regime.warnings)
    return risks


def _extract_invalidating_conditions(candidate) -> list[str]:
    """Extract invalidating conditions from signal candidate."""
    if candidate and hasattr(candidate, "invalidation_context"):
        return [candidate.invalidation_context] if candidate.invalidation_context else []
    return []


def _signal_to_dto(signal, symbol: str) -> SignalDTO:
    """Convert a Signal dataclass to SignalDTO."""
    direction_val = signal.direction.value if hasattr(signal.direction, "value") else str(signal.direction)

    return SignalDTO(
        id=str(uuid.uuid4()),
        symbol=symbol,
        direction=direction_val,
        confidence=float(signal.confidence),
        generated_at=int(time.time() * 1000),
        price=float(getattr(signal, "price", 0.0)),
        bias=getattr(signal, "market_view", "neutral"),
        reason=signal.reason,
        source=signal.source,
    )
