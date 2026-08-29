"""Market Intelligence Engine (Day 8) — DATA/ANALYSIS ONLY, no execution.

Provider-independent. Takes normalized OHLCV (+ instrument metadata) and produces a
structured, explainable analysis an AI model can reason over. The output is a
*SignalCandidate* (an analytical hypothesis) — NEVER an order.

Reuses the existing deterministic primitives:
  * ``indicators`` (sma/ema/rsi/atr/macd/bollinger — pure & causal)
  * ``analysis.quant`` (volatility/drawdown)
  * ``models.snapshot.MarketSnapshot`` / ``MarketView`` / ``ModelProvider`` /
    ``analyze_snapshot`` / ``signals.generate_signal`` for the AI bridge.

New concepts introduced here (not duplicating the above):
  * TechnicalFeatures  — comprehensive trend/momentum/vol/volume/price-structure
  * MarketRegime       — multi-feature deterministic regime classifier
  * SignalCandidate    — analytical hypothesis (direction/setup/confidence/risk)
  * AnalysisExplanation— evidence / interpretation / uncertainty split
  * AIAnalysis         — strict structured AI output schema (rejects malformed JSON)
  * MarketReasoningProvider — wraps ModelProvider, sends structured context, parses AIAnalysis

NO LOOK-AHEAD: ``features_at(df, ts)`` and ``signal_at(...)`` only ever use data at or
before ``ts``. A signal at T is computed on a window ending at T; the engine never
reads a future candle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

from ..indicators import sma, ema, rsi, atr, rolling_std
from ..analysis.quant import annualized_volatility, TRADING_PERIODS


# --------------------------------------------------------------------------- #
# Instrument classification (provider-independent)
# --------------------------------------------------------------------------- #
class InstrumentClass(str, Enum):
    EQUITY = "equity"
    INDEX = "index"
    FUTURE = "future"
    OPTION_CE = "option_ce"
    OPTION_PE = "option_pe"
    COMMODITY_FUTURE = "commodity_future"


# --------------------------------------------------------------------------- #
# Trend / regime enums
# --------------------------------------------------------------------------- #
class TrendEnum(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class RegimeEnum(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGE_BOUND = "range_bound"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    UNKNOWN = "unknown"


class VolRegime(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class SetupType(str, Enum):
    TREND_CONTINUATION = "trend_continuation"
    BREAKOUT = "breakout"
    BREAKDOWN = "breakdown"
    MEAN_REVERSION = "mean_reversion"
    MOMENTUM = "momentum"
    NO_SETUP = "no_setup"


# --------------------------------------------------------------------------- #
# Derivative-specific feature schema (OI/IV/greeks stay None until FYERS provides)
# --------------------------------------------------------------------------- #
@dataclass
class DerivativeFeatures:
    """Optional fields for F&O / commodity futures.

    Only what the Instrument + price carry is filled. OI / IV / greeks / basis are
    ``None`` unless the data source supplies them — NEVER fabricated.
    """

    underlying: Optional[str] = None
    expiry: Optional[str] = None           # ISO date
    strike: Optional[float] = None
    option_type: Optional[str] = None     # CE/PE
    days_to_expiry: Optional[int] = None
    moneyness: Optional[float] = None      # (price - strike) / strike for options
    open_interest: Optional[float] = None
    implied_vol: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    basis: Optional[float] = None          # futures - spot (needs underlying data)
    basis_pct: Optional[float] = None


# --------------------------------------------------------------------------- #
# Technical feature bundle
# --------------------------------------------------------------------------- #
@dataclass
class TechnicalFeatures:
    """Causal technical features computed at a single decision point (closed bar)."""

    close: float
    sma_20: Optional[float] = None
    sma_50: Optional[float] = None
    sma_200: Optional[float] = None
    ema_20: Optional[float] = None
    ema_50: Optional[float] = None
    price_vs_sma20: Optional[float] = None
    price_vs_sma50: Optional[float] = None
    price_vs_sma200: Optional[float] = None
    ema20_vs_ema50: Optional[float] = None
    trend: TrendEnum = TrendEnum.NEUTRAL
    rsi_14: Optional[float] = None
    roc: Optional[float] = None             # rate of change (momentum)
    momentum_dir: TrendEnum = TrendEnum.NEUTRAL
    atr_14: Optional[float] = None
    hist_vol: Optional[float] = None        # annualized
    roll_std: Optional[float] = None
    vol_regime: VolRegime = VolRegime.NORMAL
    volume: Optional[float] = None
    volume_sma20: Optional[float] = None
    relative_volume: Optional[float] = None
    volume_trend: TrendEnum = TrendEnum.NEUTRAL
    unusual_volume: bool = False
    recent_high: Optional[float] = None
    recent_low: Optional[float] = None
    dist_from_high: Optional[float] = None  # (price - recent_high)/recent_high
    dist_from_low: Optional[float] = None   # (price - recent_low)/recent_low
    breakout_candidate: bool = False
    breakdown_candidate: bool = False
    data_points: int = 0
    insufficient: bool = False


# --------------------------------------------------------------------------- #
# Feature engine
# --------------------------------------------------------------------------- #
class FeatureEngine:
    """Deterministic, causal feature computation on normalized OHLCV."""

    # Minimum bars needed for a *useful* SMA200 / regime call.
    MIN_BARS = 30

    def __init__(self, lookback: int = 60) -> None:
        self.lookback = lookback

    def compute(self, df: pd.DataFrame, instrument_class: InstrumentClass = InstrumentClass.EQUITY) -> TechnicalFeatures:
        if df is None or len(df) == 0:
            return TechnicalFeatures(close=float("nan"), insufficient=True)
        work = df.sort_index()
        # Drop duplicate timestamps (keep first) — a later duplicate candle must
        # never overwrite an earlier closed bar's value.
        if work.index.has_duplicates:
            work = work[~work.index.duplicated(keep="first")]
        close = work["close"]
        n = len(work)
        last = float(close.iloc[-1])
        f = TechnicalFeatures(close=last, data_points=n, insufficient=n < self.MIN_BARS)

        # --- Trend (SMA / EMA) ---
        if n >= 20:
            f.sma_20 = float(sma(close, 20).iloc[-1])
            f.ema_20 = float(ema(close, 20).iloc[-1])
        if n >= 50:
            f.sma_50 = float(sma(close, 50).iloc[-1])
            f.ema_50 = float(ema(close, 50).iloc[-1])
        if n >= 200:
            f.sma_200 = float(sma(close, 200).iloc[-1])
        if f.sma_20:
            f.price_vs_sma20 = (last - f.sma_20) / f.sma_20
        if f.sma_50:
            f.price_vs_sma50 = (last - f.sma_50) / f.sma_50
        if f.sma_200:
            f.price_vs_sma200 = (last - f.sma_200) / f.sma_200
        if f.ema_20 and f.ema_50:
            f.ema20_vs_ema50 = (f.ema_20 - f.ema_50) / f.ema_50

        # Trend classification from EMA cross + price vs SMA20/50.
        bull = 0
        bear = 0
        if f.ema_20 and f.ema_50:
            bull += 1 if f.ema_20 > f.ema_50 else 0
            bear += 1 if f.ema_20 < f.ema_50 else 0
        if f.price_vs_sma20 is not None:
            bull += 1 if f.price_vs_sma20 > 0 else 0
            bear += 1 if f.price_vs_sma20 < 0 else 0
        if f.price_vs_sma50 is not None:
            bull += 1 if f.price_vs_sma50 > 0 else 0
            bear += 1 if f.price_vs_sma50 < 0 else 0
        f.trend = TrendEnum.BULLISH if bull > bear else (TrendEnum.BEARISH if bear > bull else TrendEnum.NEUTRAL)

        # --- Momentum (RSI + ROC) ---
        if n >= 14:
            f.rsi_14 = float(rsi(close, 14).iloc[-1])
        if n >= 10:
            f.roc = float(close.iloc[-1] / close.iloc[-10] - 1.0)
            f.momentum_dir = TrendEnum.BULLISH if f.roc > 0 else (TrendEnum.BEARISH if f.roc < 0 else TrendEnum.NEUTRAL)

        # --- Volatility (ATR + rolling std + annualized) ---
        if n >= 14:
            f.atr_14 = float(atr(work["high"], work["low"], close, 14).iloc[-1])
        rets = close.pct_change().dropna()
        if len(rets) > 1:
            f.roll_std = float(rolling_std(rets, 20).iloc[-1]) if n >= 20 else float(rets.std(ddof=0))
            f.hist_vol = annualized_volatility(rets, "1d")  # annualized; tf-agnostic scale
        # Vol regime vs trailing median of rolling std.
        if f.roll_std is not None and len(rets) >= 20:
            med = rolling_std(rets, 20).shift(1).median()
            f.vol_regime = VolRegime.HIGH if f.roll_std > med else VolRegime.LOW

        # --- Volume ---
        vol = work["volume"]
        if n >= 20:
            f.volume_sma20 = float(vol.rolling(20).mean().iloc[-1])
            f.relative_volume = float(vol.iloc[-1] / f.volume_sma20) if f.volume_sma20 else None
            f.volume = float(vol.iloc[-1])
            f.volume_trend = TrendEnum.BULLISH if vol.iloc[-1] > vol.iloc[-2] else TrendEnum.BEARISH
            f.unusual_volume = bool(f.relative_volume is not None and f.relative_volume >= 2.0)

        # --- Price structure (lookback window, causal: uses [T-lb .. T]) ---
        win = work.tail(self.lookback)
        f.recent_high = float(win["high"].max())
        f.recent_low = float(win["low"].min())
        if f.recent_high:
            f.dist_from_high = (last - f.recent_high) / f.recent_high
        if f.recent_low:
            f.dist_from_low = (last - f.recent_low) / f.recent_low
        # Breakout candidate: close within 1% of recent high AND above SMA20.
        f.breakout_candidate = bool(f.recent_high and f.dist_from_high is not None and f.dist_from_high > -0.01 and (f.price_vs_sma20 is None or f.price_vs_sma20 >= 0))
        f.breakdown_candidate = bool(f.recent_low and f.dist_from_low is not None and f.dist_from_low < 0.01 and (f.price_vs_sma20 is None or f.price_vs_sma20 <= 0))
        return f

    def features_at(self, df: pd.DataFrame, timestamp, instrument_class: InstrumentClass = InstrumentClass.EQUITY) -> TechnicalFeatures:
        """Causal features available AT ``timestamp``: slice data <= ts, then compute.

        Guarantees no future candle is used (unit-tested).
        """
        work = df.sort_index()
        if isinstance(timestamp, str):
            timestamp = pd.Timestamp(timestamp, tz="UTC")
        if getattr(timestamp, "tzinfo", None) is None:
            timestamp = timestamp.tz_localize("UTC")
        window = work[work.index <= timestamp]
        return self.compute(window, instrument_class)


# --------------------------------------------------------------------------- #
# Market regime engine (deterministic, multi-feature)
# --------------------------------------------------------------------------- #
@dataclass
class MarketRegime:
    regime: RegimeEnum
    confidence: float                      # bounded [0,1]; analytical, NOT a win probability
    supporting_features: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.confidence = float(min(1.0, max(0.0, self.confidence)))


def classify_regime(f: TechnicalFeatures) -> MarketRegime:
    """Combine several independent signals into a regime (no single indicator)."""
    if f.insufficient:
        return MarketRegime(RegimeEnum.UNKNOWN, 0.0, [], ["insufficient data for regime classification"])

    feats: list[str] = []
    # Volatility axis.
    if f.vol_regime == VolRegime.HIGH:
        feats.append("vol_regime=HIGH")
    elif f.vol_regime == VolRegime.LOW:
        feats.append("vol_regime=LOW")

    # Trend axis.
    trend_up = f.trend == TrendEnum.BULLISH
    trend_down = f.trend == TrendEnum.BEARISH
    if trend_up:
        feats.append("trend=BULLISH")
    elif trend_down:
        feats.append("trend=BEARISH")

    # Range detection: low volatility + neutral trend.
    range_like = (f.vol_regime == VolRegime.LOW) and f.trend == TrendEnum.NEUTRAL
    # High-vol standalone regime.
    high_vol = f.vol_regime == VolRegime.HIGH

    # Decide.
    if trend_up and high_vol:
        regime, conf = RegimeEnum.HIGH_VOLATILITY, 0.7
        feats.append("uptrend under elevated volatility")
    elif trend_down and high_vol:
        regime, conf = RegimeEnum.HIGH_VOLATILITY, 0.7
        feats.append("downtrend under elevated volatility")
    elif trend_up:
        regime, conf = RegimeEnum.TRENDING_UP, 0.7
    elif trend_down:
        regime, conf = RegimeEnum.TRENDING_DOWN, 0.7
    elif range_like:
        regime, conf = RegimeEnum.RANGE_BOUND, 0.6
    elif high_vol:
        regime, conf = RegimeEnum.HIGH_VOLATILITY, 0.6
    else:
        regime, conf = RegimeEnum.LOW_VOLATILITY, 0.5

    warnings: list[str] = []
    if f.unusual_volume:
        warnings.append("unusual volume detected")
    if f.rsi_14 is not None and (f.rsi_14 >= 70 or f.rsi_14 <= 30):
        warnings.append(f"RSI {f.rsi_14:.1f} near extreme (overbought/oversold)")
    if f.insufficient:
        warnings.append("limited history")
    return MarketRegime(regime, conf, feats, warnings)


# --------------------------------------------------------------------------- #
# Signal candidate (analytical hypothesis — NOT an order)
# --------------------------------------------------------------------------- #
@dataclass
class SignalCandidate:
    symbol: str
    contract_id: str
    timeframe: str
    direction: SignalDirection
    setup: SetupType
    confidence: float                      # analytical, [0,1]
    entry_context: str = ""
    invalidation_context: str = ""
    supporting_features: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    timestamp: Optional[datetime] = None
    instrument_class: InstrumentClass = InstrumentClass.EQUITY

    def __post_init__(self) -> None:
        self.confidence = float(min(1.0, max(0.0, self.confidence)))


def generate_signal_candidate(
    symbol: str,
    contract_id: str,
    timeframe: str,
    features: TechnicalFeatures,
    regime: MarketRegime,
    instrument_class: InstrumentClass = InstrumentClass.EQUITY,
    ts: Optional[datetime] = None,
) -> SignalCandidate:
    """Deterministic analytical hypothesis from features + regime. No LLM."""
    bull = 0
    bear = 0
    sup: list[str] = []
    if features.trend == TrendEnum.BULLISH:
        bull += 2
        sup.append("trend bullish (EMA20>EMA50, price>SMA20)")
    elif features.trend == TrendEnum.BEARISH:
        bear += 2
        sup.append("trend bearish (EMA20<EMA50, price<SMA20)")
    if features.rsi_14 is not None:
        if features.rsi_14 >= 55:
            bull += 1
            sup.append(f"RSI {features.rsi_14:.1f} momentum positive")
        elif features.rsi_14 <= 45:
            bear += 1
            sup.append(f"RSI {features.rsi_14:.1f} momentum weak")
    if features.breakout_candidate:
        bull += 1
        sup.append("near recent-high breakout candidate")
    if features.breakdown_candidate:
        bear += 1
        sup.append("near recent-low breakdown candidate")
    if features.unusual_volume and bull > bear:
        sup.append("breakout supported by unusual volume")

    if features.insufficient:
        return SignalCandidate(
            symbol, contract_id, timeframe, SignalDirection.NEUTRAL, SetupType.NO_SETUP, 0.0,
            entry_context="insufficient data", invalidation_context="n/a",
            supporting_features=[], risk_flags=["insufficient_history"], timestamp=ts, instrument_class=instrument_class,
        )

    if bull > bear and bull >= 2:
        direction = SignalDirection.LONG
        setup = SetupType.TREND_CONTINUATION if features.trend == TrendEnum.BULLISH else SetupType.BREAKOUT
        conf = min(0.9, 0.5 + bull * 0.1)
        inv = "trend structure breaks (price < SMA20 and EMA20 < EMA50)"
    elif bear > bull and bear >= 2:
        direction = SignalDirection.SHORT
        setup = SetupType.TREND_CONTINUATION if features.trend == TrendEnum.BEARISH else SetupType.BREAKDOWN
        conf = min(0.9, 0.5 + bear * 0.1)
        inv = "trend structure breaks (price > SMA20 and EMA20 > EMA50)"
    else:
        direction = SignalDirection.NEUTRAL
        setup = SetupType.NO_SETUP
        conf = 0.4
        inv = "no clear edge; wait for confirmation"

    risks = list(regime.warnings)
    if regime.regime == RegimeEnum.HIGH_VOLATILITY:
        risks.append("elevated volatility => wider invalidation")
    return SignalCandidate(
        symbol, contract_id, timeframe, direction, setup, conf,
        entry_context=f"last close {features.close:.2f}, SMA20 {features.sma_20}",
        invalidation_context=inv, supporting_features=sup, risk_flags=risks,
        timestamp=ts, instrument_class=instrument_class,
    )


# --------------------------------------------------------------------------- #
# Explanation engine
# --------------------------------------------------------------------------- #
@dataclass
class AnalysisExplanation:
    summary: str
    bullish_factors: list[str] = field(default_factory=list)
    bearish_factors: list[str] = field(default_factory=list)
    neutral_factors: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)

    @classmethod
    def from_features(cls, features: TechnicalFeatures, regime: MarketRegime, candidate: SignalCandidate) -> "AnalysisExplanation":
        bull: list[str] = []
        bear: list[str] = []
        neu: list[str] = []
        if features.price_vs_sma20 is not None:
            if features.price_vs_sma20 > 0:
                bull.append(f"price {features.price_vs_sma20*100:.2f}% above SMA20")
            else:
                bear.append(f"price {abs(features.price_vs_sma20)*100:.2f}% below SMA20")
        if features.ema20_vs_ema50 is not None:
            if features.ema20_vs_ema50 > 0:
                bull.append("EMA20 above EMA50 (short-term uptrend)")
            else:
                bear.append("EMA20 below EMA50 (short-term downtrend)")
        if features.rsi_14 is not None:
            if features.rsi_14 >= 70:
                bear.append(f"RSI {features.rsi_14:.1f} overbought")
            elif features.rsi_14 <= 30:
                bull.append(f"RSI {features.rsi_14:.1f} oversold")
            elif features.rsi_14 >= 55:
                bull.append(f"RSI {features.rsi_14:.1f} positive momentum")
        if features.unusual_volume:
            neu.append("unusual volume (interpret with care)")
        if features.vol_regime == VolRegime.HIGH:
            neu.append("high volatility regime")
        missing: list[str] = []
        if features.sma_200 is None:
            missing.append("SMA200 (need >=200 bars)")
        if features.insufficient:
            missing.append("limited history for reliable read")
        return cls(
            summary=f"{regime.regime.value} | candidate {candidate.direction.value}/{candidate.setup.value} conf={candidate.confidence:.2f}",
            bullish_factors=bull, bearish_factors=bear, neutral_factors=neu,
            risks=list(candidate.risk_flags), missing_data=missing,
        )


# --------------------------------------------------------------------------- #
# AI reasoning interface (wraps existing ModelProvider; strict output schema)
# --------------------------------------------------------------------------- #
from pydantic import BaseModel, Field, ConfigDict, ValidationError  # noqa: E402


class AIAnalysis(BaseModel):
    """Strict structured AI output. Malformed model JSON is rejected at construction."""

    model_config = ConfigDict(extra="forbid")

    conclusion: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(ge=0.0, le=1.0)   # analytical, NOT win probability
    bullish_case: list[str] = Field(default_factory=list)
    bearish_case: list[str] = Field(default_factory=list)
    key_evidence: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)

    @classmethod
    def from_model_json(cls, data: dict, model: str = "unknown") -> "AIAnalysis":
        """Parse untrusted model JSON; raise ValidationError on malformed output."""
        if not isinstance(data, dict):
            raise TypeError("model output must be a JSON object")
        d = dict(data)
        d.setdefault("model_unused", None)
        d.pop("model_unused", None)
        return cls(**d)


@dataclass
class AnalysisContext:
    """Structured, bounded context sent to the AI (never a raw DB dump)."""

    instrument: dict
    timeframe: str
    market_regime: dict
    features: dict
    signal_candidate: dict
    recent_candles: list = field(default_factory=list)
    data_quality: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "instrument": self.instrument,
            "timeframe": self.timeframe,
            "market_regime": self.market_regime,
            "features": self.features,
            "signal_candidate": self.signal_candidate,
            "recent_candles": self.recent_candles,
            "data_quality": self.data_quality,
        }


class MarketReasoningProvider:
    """Bridges the deterministic intelligence layer to an AI ModelProvider.

    Sends a bounded structured context; parses the provider's MarketView into an
    AIAnalysis. If the provider returns malformed output, rejects it (never silently
    converts to a valid-looking signal). Uses existing ``ModelProvider`` so the AI
    vendor stays decoupled (LocalRuleModel / OpenAICompatibleProvider).
    """

    def __init__(self, provider) -> None:
        self.provider = provider

    @property
    def is_available(self) -> bool:
        return bool(getattr(self.provider, "is_available", True))

    def reason(self, context: AnalysisContext) -> AIAnalysis:
        """Run AI reasoning over structured context; return validated AIAnalysis.

        Raises ``AnalysisRejected`` if the provider/parse step produces malformed
        output. No LLM call ever fabricates market facts — it only interprets the
        context we pass.
        """
        from ..models.snapshot import MarketSnapshot
        from ..models.market_view import MarketView, MarketViewEnum

        # Reuse the existing MarketSnapshot/MarketView contract: the provider analyzes
        # a snapshot. We synthesize a minimal snapshot from the context features.
        feat = context.features
        snap = MarketSnapshot(
            symbol=context.instrument.get("symbol", "?"),
            timeframe=context.timeframe,
            timestamp=datetime.now(timezone.utc),
            last_bar_timestamp=datetime.now(timezone.utc),
            latest_price=float(feat.get("close", 0.0) or 0.0),
            sma_20=feat.get("sma_20"),
            rsi_14=feat.get("rsi_14"),
            atr_14=feat.get("atr_14"),
            volatility_annualized=feat.get("hist_vol"),
            price_vs_sma20=feat.get("price_vs_sma20"),
            data_points=int(feat.get("data_points", 1)),
            lookahead_safe=True,
        )
        view: MarketView = self.provider.analyze(snap)
        # Translate the validated MarketView into the richer AIAnalysis schema.
        direction_word = {
            MarketViewEnum.BULLISH: "bullish",
            MarketViewEnum.BEARISH: "bearish",
            MarketViewEnum.NEUTRAL: "neutral",
            MarketViewEnum.CHOPPY: "choppy",
        }.get(view.market_view, "neutral")
        conclusion = (
            f"AI ({view.model}) reads {direction_word}. {view.reasoning_summary}"
        )
        try:
            return AIAnalysis(
                conclusion=conclusion,
                confidence=view.confidence,
                bullish_case=view.bullish_factors,
                bearish_case=view.bearish_factors,
                key_evidence=view.bullish_factors + view.bearish_factors,
                contradictions=[
                    "AI interpretation is heuristic; contradicts none explicitly flagged."
                ],
                risks=view.risks,
                missing_data=context.data_quality.get("missing", []),
                invalidation_conditions=view.invalidating_conditions,
            )
        except ValidationError as e:
            raise AnalysisRejected(f"AI produced malformed analysis: {e}")


class AnalysisRejected(Exception):
    """Raised when the AI provider/model output cannot be validated."""


# Convenience: derive InstrumentClass from an Instrument.
def instrument_class_of(instrument) -> InstrumentClass:
    from ..india.instruments import InstrumentType

    it = getattr(instrument, "instrument_type", None)
    if it == InstrumentType.OPTION_CE:
        return InstrumentClass.OPTION_CE
    if it == InstrumentType.OPTION_PE:
        return InstrumentClass.OPTION_PE
    if it == InstrumentType.FUTURE:
        # Distinguish commodity via exchange.
        ex = getattr(instrument.internal, "exchange", "") if hasattr(instrument, "internal") else ""
        return InstrumentClass.COMMODITY_FUTURE if ex == "MCX" else InstrumentClass.FUTURE
    if it == InstrumentType.INDEX:
        return InstrumentClass.INDEX
    return InstrumentClass.EQUITY


class MarketIntelligenceEngine:
    """Top-level orchestrator: features -> regime -> signal candidate -> explanation.

    Provider-independent. Respects the DataHealthMonitor: if feed health is not
    HEALTHY it refuses to produce a normal analysis and returns a blocked result.
    """

    def __init__(self, lookback: int = 60, feature_engine: Optional[FeatureEngine] = None) -> None:
        self.engine = feature_engine or FeatureEngine(lookback=lookback)

    def analyze(
        self,
        symbol: str,
        timeframe: str,
        df: pd.DataFrame,
        instrument=None,
        contract_id: str = "",
        health_status: Optional[str] = None,  # FeedStatus.value or None
        as_of: Optional[datetime] = None,
    ) -> dict:
        """Run the full deterministic analysis. Returns a serializable dict.

        If health_status is not HEALTHY (or data insufficient) the result is marked
        BLOCKED with a reason — no fake indicators.
        """
        from ..india.data_health import FeedStatus

        blocked_reason = None
        if health_status is not None and health_status != FeedStatus.HEALTHY.value:
            blocked_reason = f"DATA_HEALTH = {health_status.upper()}"
        if df is None or len(df) == 0:
            blocked_reason = blocked_reason or "NO_DATA"

        if blocked_reason:
            return {
                "status": "BLOCKED",
                "reason": blocked_reason,
                "symbol": symbol,
                "timeframe": timeframe,
            }

        iclass = instrument_class_of(instrument) if instrument is not None else InstrumentClass.EQUITY
        features = self.engine.compute(df, iclass)
        regime = classify_regime(features)

        ts = as_of or (df.index[-1] if getattr(df.index[-1], "tzinfo", None) else df.index[-1].tz_localize("UTC"))
        candidate = generate_signal_candidate(
            symbol, contract_id or symbol, timeframe, features, regime, iclass, ts=ts
        )
        explanation = AnalysisExplanation.from_features(features, regime, candidate)
        # Derivative fields (OI/IV/greeks stay None unless provided).
        deriv = DerivativeFeatures(
            underlying=getattr(instrument, "underlying", None),
            expiry=getattr(instrument, "expiry", None),
            strike=getattr(instrument, "strike", None),
            option_type=getattr(instrument, "option_type", None),
        )
        if instrument is not None and getattr(instrument, "expiry", None):
            try:
                exp = datetime.fromisoformat(instrument.expiry)
                deriv.days_to_expiry = (exp - datetime.now(timezone.utc).date()).days
            except (ValueError, TypeError):
                pass
        if deriv.strike is not None and features.close is not None and not features.insufficient:
            deriv.moneyness = (features.close - (deriv.strike or 0)) / (deriv.strike or 1)

        return {
            "status": "OK",
            "symbol": symbol,
            "timeframe": timeframe,
            "contract_id": contract_id or symbol,
            "instrument_class": iclass.value,
            "features": features,
            "regime": regime,
            "signal_candidate": candidate,
            "explanation": explanation,
            "derivative": deriv,
            "data_quality": {
                "rows": features.data_points,
                "insufficient": features.insufficient,
                "missing": explanation.missing_data,
            },
        }

