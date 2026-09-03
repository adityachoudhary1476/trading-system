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
# Time Horizon Enum
# --------------------------------------------------------------------------- #
class TimeHorizon(str, Enum):
    INTRADAY = "intraday"
    SHORT_TERM = "short_term"
    SWING = "swing"


# --------------------------------------------------------------------------- #
# Evidence Ledger — tracks supporting/contradicting evidence
# --------------------------------------------------------------------------- #
@dataclass
class EvidenceLedger:
    """Structured evidence for explainable analysis."""
    positive: list[str] = field(default_factory=list)
    negative: list[str] = field(default_factory=list)
    neutral: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def agreement(self) -> str:
        """Classify evidence agreement level."""
        pos = len(self.positive)
        neg = len(self.negative)
        total = pos + neg
        if total == 0:
            return "neutral"
        ratio = abs(pos - neg) / total
        if ratio >= 0.6:
            return "strong"
        elif ratio >= 0.3:
            return "moderate"
        else:
            return "mixed"


# --------------------------------------------------------------------------- #
# Multi-Timeframe Analysis Result
# --------------------------------------------------------------------------- #
@dataclass
class TimeframeAnalysis:
    """Analysis for a single timeframe."""
    timeframe: str
    bias: TrendEnum
    confidence: float  # 0..100
    trend_score: float = 0.0
    momentum_score: float = 0.0
    volume_score: float = 0.0
    volatility_score: float =  0.0
    evidence: EvidenceLedger = field(default_factory=EvidenceLedger)


# --------------------------------------------------------------------------- #
# Expected Move Estimate
# --------------------------------------------------------------------------- #
@dataclass
class ExpectedMove:
    """Estimated price range based on volatility."""
    lower_pct: float  # e.g., -1.4 for -1.4%
    upper_pct: float  # e.g., -0.8 for -0.8%
    basis: str  # "atr" or "volatility"
    horizon: TimeHorizon = TimeHorizon.INTRADAY


# --------------------------------------------------------------------------- #
# Options Candidate
# --------------------------------------------------------------------------- #
@dataclass
class OptionsCandidate:
    """A scored options contract candidate."""
    strike: float
    option_type: str  # CE or PE
    expiry: Optional[str] = None
    moneyness: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    implied_vol: Optional[float] = None
    open_interest: Optional[float] = None
    volume: Optional[float] = None
    bid_ask_spread: Optional[float] = None
    score: float = 0.0  # 0..100
    rationale: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Instrument-specific analysis context
# --------------------------------------------------------------------------- #
@dataclass
class InstrumentContext:
    """Instrument-specific analysis parameters."""
    instrument_class: InstrumentClass
    symbol: str
    # Index-specific
    is_index: bool = False
    is_bank_nifty: bool = False
    is_nifty: bool = False
    # Stock-specific
    is_sector_available: bool = False
    sector: Optional[str] = None
    # Volatility parameters (instrument-specific)
    high_vol_threshold: float = 0.30
    low_vol_threshold: float = 0.15
    # ATR-based expected move multiplier
    atr_move_multiplier: float =  1.0


def _build_instrument_context(symbol: str, instrument=None) -> InstrumentContext:
    """Build instrument-specific context for analysis.

    Index detection: prefer the instrument object's class; when no instrument
    object is available, infer index-ness from well-known index symbols
    (NIFTY50 / BANKNIFTY / FINNIFTY / MIDCPNIFTY / SENSEX). Never fabricates
    data — this is classification only.
    """
    iclass = instrument_class_of(instrument) if instrument is not None else None

    sym_upper = symbol.upper()
    _INDEX_TOKENS = ("NIFTY50", "NIFTY 50", "NIFTYBANK", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")
    _looks_like_index = any(tok in sym_upper for tok in _INDEX_TOKENS)
    if iclass is None:
        iclass = InstrumentClass.INDEX if _looks_like_index else InstrumentClass.EQUITY

    ctx = InstrumentContext(
        instrument_class=iclass,
        symbol=symbol,
        is_index=iclass == InstrumentClass.INDEX,
    )

    # Identify specific indices
    ctx.is_nifty = "NIFTY" in sym_upper and "BANK" not in sym_upper and "FIN" not in sym_upper
    ctx.is_bank_nifty = "BANK" in sym_upper and "NIFTY" in sym_upper

    # Instrument-specific volatility thresholds
    if ctx.is_bank_nifty:
        ctx.high_vol_threshold = 0.35  # BANK NIFTY is more volatile
        ctx.low_vol_threshold = 0.18
        ctx.atr_move_multiplier = 1.2
    elif ctx.is_nifty:
        ctx.high_vol_threshold = 0.25
        ctx.low_vol_threshold = 0.12
        ctx.atr_move_multiplier = 1.0
    elif iclass == InstrumentClass.EQUITY:
        ctx.high_vol_threshold = 0.40  # Individual stocks can be more volatile
        ctx.low_vol_threshold = 0.20
        ctx.atr_move_multiplier = 0.9

    return ctx


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
# Evidence-based confidence calculation
# --------------------------------------------------------------------------- #
def compute_evidence_confidence(
    features: TechnicalFeatures,
    regime: MarketRegime,
    context: InstrumentContext,
) -> tuple[float, EvidenceLedger]:
    """Compute confidence from structured evidence (0..100."""
    ledger = EvidenceLedger()
    scores = {}
    weights = {}

    # --- Trend alignment (weight: 25) ---
    trend_score = 50.0
    if features.price_vs_sma20 is not None:
        if features.price_vs_sma20 >  0.03:
            trend_score = 80.0
            ledger.positive.append(f"Price {features.price_vs_sma20*100:.1f}% above SMA20")
        elif features.price_vs_sma20 >  0:
            trend_score =  65.0
            ledger.positive.append(f"Price {features.price_vs_sma20*100:.1f}% above SMA20")
        elif features.price_vs_sma20 > -0.03:
            trend_score =  40.0
            ledger.negative.append(f"Price {abs(features.price_vs_sma20)*100:.1f}% below SMA20")
        else:
            trend_score =  25.0
            ledger.negative.append(f"Price {abs(features.price_vs_sma20)*100:.1f}% below SMA20")
    else:
        ledger.missing.append("SMA20 unavailable")

    if features.price_vs_sma50 is not None:
        
        if features.price_vs_sma50 >  0:
            trend_score = min(trend_score +  10, 100)
        else:
            trend_score = max(trend_score -  10, 0)

    scores["trend"] = trend_score
    weights["trend"] =  25

    # --- Momentum (weight:  20) ---
    mom_score =  50.0
    if features.rsi_14 is not None:
        
        if features.rsi_14 >  70:
            mom_score =  30.0
            ledger.negative.append(f"RSI {features.rsi_14:.1f} overbought")
        elif features.rsi_14 >  60:
            mom_score =  65.0
            ledger.positive.append(f"RSI {features.rsi_14:.1f} momentum positive")
        elif features.rsi_14 >  40:
            mom_score =  50.0
            ledger.neutral.append(f"RSI {features.rsi_14:.1f} neutral")
        elif features.rsi_14 >  30:
            mom_score =  40.0
            ledger.negative.append(f"RSI {features.rsi_14:.1f} momentum weak")
        else:
            mom_score =  70.0
            ledger.positive.append(f"RSI {features.rsi_14:.1f} oversold (potential bounce)")
    else:
        ledger.missing.append("RSI unavailable")

    if features.roc is not None:
        if features.roc >  0.03:
            mom_score = min(mom_score +  10, 100)
        elif features.roc < -0.03:
            mom_score = max(mom_score - 10, 0)

    scores["momentum"] = mom_score
    weights["momentum"] =  20

    # --- Volume (weight: 15) ---
    vol_score =  50.0
    if features.relative_volume is not None:
        if features.unusual_volume and features.relative_volume >  2.0:
            vol_score =  75.0
            ledger.positive.append(f"Unusual volume: {features.relative_volume:.1f}x average")
        elif features.relative_volume >  1.2:
            vol_score =  60.0
            ledger.neutral.append(f"Volume {features.relative_volume:.1f}x average")
        elif features.relative_volume <  0.7:
            vol_score =  35.0
            ledger.negative.append(f"Low volume: {features.relative_volume:.1f}x average")
        else:
            vol_score =  50.0
            ledger.neutral.append(f"Volume near average ({features.relative_volume:.1f}x)")
    else:
        ledger.missing.append("Volume data unavailable")

    scores["volume"] = vol_score
    weights["volume"] =  15

    # --- Volatility/Regime (weight: 15, instrument-aware) ---
    reg_score =  50.0
    if regime.regime == RegimeEnum.TRENDING_UP:
        reg_score =  75.0
        ledger.positive.append("Market in confirmed uptrend")
    elif regime.regime == RegimeEnum.TRENDING_DOWN:
                
        reg_score =  25.0
        ledger.negative.append("Market in confirmed downtrend")
    elif regime.regime == RegimeEnum.HIGH_VOLATILITY:
 
        reg_score =  40.0
        ledger.negative.append("High volatility regime")
        # Instrument-specific annualized-vol check: an index is far less
        # tolerant of elevated vol than a single stock (thresholds differ).
        if features.hist_vol is not None:
            if features.hist_vol > context.high_vol_threshold:
                reg_score = 25.0
                ledger.negative.append(
                    f"Annualized vol {features.hist_vol:.0%} above "
                    f"{context.symbol} normal band ({context.high_vol_threshold:.0%})"
                )
            elif features.hist_vol < context.low_vol_threshold:
                reg_score = 55.0
                ledger.neutral.append(
                    f"Annualized vol {features.hist_vol:.0%} below "
                    f"{context.symbol} typical band ({context.low_vol_threshold:.0%})"
                )
        else:
            ledger.missing.append("Annualized volatility unavailable")
    elif regime.regime == RegimeEnum.RANGE_BOUND: 
                
        reg_score =  50.0
        ledger.neutral.append("Range-bound market")
    elif regime.regime == RegimeEnum.LOW_VOLATILITY: 
            
        reg_score =  60.0
        ledger.positive.append("Low volatility (potential breakout setup)")

    scores["regime"] = reg_score
    weights["regime"] =  15

    # --- Structure (weight: 10) ---
    sr_score =  50.0
    if features.recent_high is not None and features.recent_low is not None:

        rng_size = features.recent_high - features.recent_low  
        if rng_size >  0:
            position = (features.close - features.recent_low) / rng_size  
            if position >  0.8:
                sr_score =  70.0
                ledger.positive.append("Price near recent high (breakout candidate)")
            elif position >  0.5:
                sr_score =  60.0
                ledger.neutral.append("Price in upper half of recent range")
            elif position >  0.2:
                sr_score =  45.0
                ledger.neutral.append("Price in lower half of recent range")
            else:
                sr_score =  35.0
                ledger.negative.append("Price near recent low")

    scores["structure"] = sr_score
    weights["structure"] =  10

    # --- Data quality (weight: 15) ---
    dq_score =  100.0
    if features.insufficient:
        dq_score =  30.0
        ledger.missing.append("Insufficient historical data")
    elif features.data_points <  50:
        dq_score =  60.0
        ledger.missing.append("Limited data points")
    if features.sma_200 is None:
        dq_score -=  10
    if features.rsi_14 is None:
        dq_score -=  10

    scores["data_quality"] = max(dq_score, 0)
    weights["data_quality"] =  15

    # --- Compute weighted average ---
    total_weight = sum(weights.values())
    weighted_sum = sum(scores[k] * weights[k] for k in scores)
    confidence = weighted_sum / total_weight if total_weight >  0 else  50.0

    # Adjust for evidence agreement
    agreement = ledger.agreement
    if agreement == "mixed":
        confidence *=  0.85
    elif agreement == "strong":
        confidence = min(confidence *  1.1, 100)

    return round(min(max(confidence, 0), 100), 1), ledger


# --------------------------------------------------------------------------- #
# Expected move calculation
# --------------------------------------------------------------------------- #
def compute_expected_move(
    features: TechnicalFeatures,
    horizon: TimeHorizon,
    context: InstrumentContext,
) -> Optional[ExpectedMove]:
    """Calculate expected price move based on ATR/volatility.

    Uses ATR when available (daily-anchored), otherwise annualized
    historical volatility scaled to the horizon. Instrument context
    provides a volatility scaling multiplier (indices move differently
    than single stocks).
    """
    if features.close is None or features.close <= 0:
        return None

    atr = features.atr_14
    vol = features.hist_vol

    if atr is None and vol is None:
        return None

    multiplier = context.atr_move_multiplier

    if horizon == TimeHorizon.INTRADAY:
        if atr is not None:
            pct = (atr / features.close) * 100 * multiplier
            return ExpectedMove(
                lower_pct=round(-pct, 2),
                upper_pct=round(pct, 2),
                basis="atr",
                horizon=horizon,
            )
        elif vol is not None:
            pct = vol * 100 * 0.5 * multiplier
            return ExpectedMove(
                lower_pct=round(-pct, 2),
                upper_pct=round(pct, 2),
                basis="volatility",
                horizon=horizon,
            )
    elif horizon == TimeHorizon.SHORT_TERM:
        if atr is not None:
            pct = (atr / features.close) * 100 * 1.5 * multiplier
            return ExpectedMove(
                lower_pct=round(-pct, 2),
                upper_pct=round(pct, 2),
                basis="atr",
                horizon=horizon,
            )
        elif vol is not None:
            pct = vol * 100 * np.sqrt(5) * multiplier
            return ExpectedMove(
                lower_pct=round(-pct, 2),
                upper_pct=round(pct, 2),
                basis="volatility",
                horizon=horizon,
            )
    elif horizon == TimeHorizon.SWING:
        if atr is not None:
            pct = (atr / features.close) * 100 * 2.0 * multiplier
            return ExpectedMove(
                lower_pct=round(-pct, 2),
                upper_pct=round(pct, 2),
                basis="atr",
                horizon=horizon,
            )
        elif vol is not None:
            pct = vol * 100 * np.sqrt(20) * multiplier
            return ExpectedMove(
                lower_pct=round(-pct, 2),
                upper_pct=round(pct, 2),
                basis="volatility",
                horizon=horizon,
            )
    return None


# --------------------------------------------------------------------------- #
# Time horizon determination
# --------------------------------------------------------------------------- #
def determine_horizon(features: TechnicalFeatures, regime: MarketRegime) -> TimeHorizon:

    """Determine the most relevant time horizon based on evidence."""
    if features.insufficient:
        return TimeHorizon.INTRADAY

    if regime.regime in (RegimeEnum.TRENDING_UP, RegimeEnum.TRENDING_DOWN):
        return TimeHorizon.SWING
 
    elif regime.regime == RegimeEnum.HIGH_VOLATILITY:
        
        return TimeHorizon.SHORT_TERM
 
    elif regime.regime == RegimeEnum.RANGE_BOUND: 
        
        return TimeHorizon.INTRADAY

    elif regime.regime == RegimeEnum.LOW_VOLATILITY: 
        
        return TimeHorizon.SHORT_TERM

    return TimeHorizon.SHORT_TERM


# --------------------------------------------------------------------------- #
# Invalidation level calculation
# --------------------------------------------------------------------------- #
def compute_invalidation(features: TechnicalFeatures, direction: SignalDirection) -> Optional[str]:
    """Compute invalidation level based on market structure."""
    if features.close is None:
        return None

    if direction == SignalDirection.LONG:
        if features.recent_low is not None:
            return f"Sustained move below {features.recent_low:.2f}"
        if features.sma_50 is not None:
            return f"Close below SMA50 ({features.sma_50:.2f})"
        if features.sma_20 is not None:
            return f"Close below SMA20 ({features.sma_20:.2f})"
    elif direction == SignalDirection.SHORT:
        if features.recent_high is not None:
            return f"Sustained move above {features.recent_high:.2f}"
        if features.sma_50 is not None:
            return f"Close above SMA50 ({features.sma_50:.2f})"
        if features.sma_20 is not None:
            return f"Close above SMA20 ({features.sma_20:.2f})"

    return None


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
    horizon: Optional[TimeHorizon] = None
    expected_move: Optional["ExpectedMove"] = None

    def __post_init__(self) -> None:
        self.confidence = float(min(1.0, max(0.0, self.confidence)))


def generate_signal_candidate(
    symbol: str,
    contract_id: str,
    timeframe: str,
    features: TechnicalFeatures,
    regime: MarketRegime,
    instrument_class: InstrumentClass = InstrumentClass.EQUITY,
    context: Optional[InstrumentContext] = None,
    ts: Optional[datetime] = None,
) -> SignalCandidate:
    """Deterministic analytical hypothesis from features + regime. No LLM."""
    # Build instrument context if not provided
    if context is None:
        context = _build_instrument_context(symbol)

    # Direction determination
    if regime.regime == RegimeEnum.TRENDING_UP:
        direction = SignalDirection.LONG
    elif regime.regime == RegimeEnum.TRENDING_DOWN:
        direction = SignalDirection.SHORT
    elif features.trend == TrendEnum.BULLISH:
        direction = SignalDirection.LONG
    elif features.trend == TrendEnum.BEARISH:
        direction = SignalDirection.SHORT
    else:
        direction = SignalDirection.NEUTRAL

    # Setup type
    if features.breakout_candidate:
        setup = SetupType.BREAKOUT
    elif features.breakdown_candidate:
        setup = SetupType.BREAKDOWN
    elif regime.regime in (RegimeEnum.TRENDING_UP, RegimeEnum.TRENDING_DOWN):
        setup = SetupType.TREND_CONTINUATION
    elif regime.regime == RegimeEnum.RANGE_BOUND:
        setup = SetupType.MEAN_REVERSION
    elif features.roc is not None and abs(features.roc) >  0.03:
        setup = SetupType.MOMENTUM
    else:
        setup = SetupType.NO_SETUP

    # Evidence-based confidence (0..100 scale, convert to 0..1)
    confidence_score, ledger = compute_evidence_confidence(features, regime, context)
    confidence = confidence_score / 100.0

    # Horizon
    horizon = determine_horizon(features, regime)

    # Expected move
    expected_move = compute_expected_move(features, horizon, context)

    # Invalidation
    inv = compute_invalidation(features, direction) or "Structure break"

    # Supporting features from ledger
    sup = ledger.positive + ledger.negative

    # Risk flags
    risks = list(regime.warnings)
    if regime.regime == RegimeEnum.HIGH_VOLATILITY:        
        risks.append("elevated volatility => wider invalidation")
    if features.insufficient:
        risks.append("insufficient_history")

    return SignalCandidate(
        symbol, contract_id, timeframe, direction, setup, round(confidence, 4),
        entry_context=f"last close {features.close:.2f}, SMA20 {features.sma_20}",
        invalidation_context=inv, supporting_features=sup, risk_flags=risks,
        timestamp=ts, instrument_class=instrument_class,
        horizon=horizon, expected_move=expected_move,
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
        option_chain: Optional[list[dict]] = None,
    ) -> dict:
        """Run the full deterministic analysis. Returns a serializable dict.

        If health_status is not HEALTHY (or data insufficient) the result is marked
        BLOCKED with a reason — no fake indicators.

        ``option_chain``: optional live option-chain rows (dicts with strike /
        option_type / delta / theta / implied_vol / open_interest / volume /
        bid / ask). When provided, scored OptionsCandidate objects are generated
        for directional views. When absent, options intelligence is explicitly
        reported as unavailable — NEVER fabricated.
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
        context = _build_instrument_context(symbol, instrument)
        features = self.engine.compute(df, iclass)
        regime = classify_regime(features)

        ts = as_of or (df.index[-1] if getattr(df.index[-1], "tzinfo", None) else df.index[-1].tz_localize("UTC"))
        candidate = generate_signal_candidate(
            symbol, contract_id or symbol, timeframe, features, regime, iclass, context, ts=ts
        )
        explanation = AnalysisExplanation.from_features(features, regime, candidate)

        # Options intelligence: only from a real chain. No chain => explicit
        # unavailability, never synthetic candidates.
        options_candidates: list[OptionsCandidate] = []
        options_status = "unavailable_no_chain"
        if option_chain:
            options_candidates = generate_options_candidates(
                features, regime, candidate.direction, features.close, option_chain
            )
            options_status = f"{len(options_candidates)} candidate(s)" if options_candidates else "no_attractive_setup"

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
            "instrument_context": context,
            "features": features,
            "regime": regime,
            "signal_candidate": candidate,
            "explanation": explanation,
            "derivative": deriv,
            "options_candidates": options_candidates,
            "options_status": options_status,
            "data_quality": {
                "rows": features.data_points,
                "insufficient": features.insufficient,
                "missing": explanation.missing_data,
            },
        }

# --------------------------------------------------------------------------- #
# Multi-timeframe analysis (per-horizon directional assessments)
# --------------------------------------------------------------------------- #
def analyze_multi_timeframe(
    symbol: str,
    dfs: dict[str, pd.DataFrame],
    context: InstrumentContext,
) -> dict[str, TimeframeAnalysis]:
    """Analyze multiple timeframes and produce per-timeframe assessments.

    Each timeframe gets its OWN features, regime, evidence ledger and
    confidence — horizons may legitimately disagree (e.g. bearish intraday,
    bullish swing). No data is fabricated: insufficient data produces an
    explicit NEUTRAL assessment with the reason recorded.
    """
    results: dict[str, TimeframeAnalysis] = {}
    engine = FeatureEngine(lookback=60)

    for tf, df in dfs.items():
        if df is None or len(df) < FeatureEngine.MIN_BARS:
            results[tf] = TimeframeAnalysis(
                timeframe=tf,
                bias=TrendEnum.NEUTRAL,
                confidence=0.0,
                evidence=EvidenceLedger(missing=[f"Insufficient data for {tf}"]),
            )
            continue

        features = engine.compute(df, context.instrument_class)
        regime = classify_regime(features)
        confidence, ledger = compute_evidence_confidence(features, regime, context)

        if regime.regime == RegimeEnum.TRENDING_UP:
            bias = TrendEnum.BULLISH
        elif regime.regime == RegimeEnum.TRENDING_DOWN:
            bias = TrendEnum.BEARISH
        else:
            bias = features.trend

        results[tf] = TimeframeAnalysis(
            timeframe=tf,
            bias=bias,
            confidence=confidence,
            trend_score=getattr(regime, "confidence", 0.0) * 100,
            momentum_score=features.rsi_14 if features.rsi_14 else 50.0,
            volume_score=min(features.relative_volume * 50, 100) if features.relative_volume else 50.0,
            volatility_score=min(features.hist_vol * 100, 100) if features.hist_vol else 50.0,
            evidence=ledger,
        )

    return results



# --------------------------------------------------------------------------- #
# Options Strategy Engine (chain-driven candidate generation + scoring)
# --------------------------------------------------------------------------- #
def generate_options_candidates(
    features: TechnicalFeatures,
    regime: MarketRegime,
    direction: SignalDirection,
    spot_price: float,
    option_chain: Optional[list[dict]] = None,
) -> list[OptionsCandidate]:
    """Generate scored options candidates from a directional forecast.

    Candidates come ONLY from the provided (live) option chain — strikes are
    never invented. A NEUTRAL forecast or a missing/empty chain yields an
    empty list: "no attractive options setup" is a valid result.
    """
    candidates: list[OptionsCandidate] = []

    if direction == SignalDirection.NEUTRAL:
        return candidates

    if not option_chain:
        return candidates

    # Filter by direction: calls for bullish, puts for bearish.
    if direction == SignalDirection.LONG:
        relevant = [c for c in option_chain if c.get("option_type") == "CE"]
    else:
        relevant = [c for c in option_chain if c.get("option_type") == "PE"]

    for contract in relevant:
        score = 0.0
        rationale: list[str] = []
        risks: list[str] = []

        strike = contract.get("strike", 0)
        delta = contract.get("delta")
        gamma = contract.get("gamma")
        theta = contract.get("theta")
        vega = contract.get("vega")
        iv = contract.get("implied_vol")
        oi = contract.get("open_interest")
        volume = contract.get("volume")
        bid = contract.get("bid")
        ask = contract.get("ask")

        bid_ask_spread: Optional[float] = None
        if bid is not None and ask is not None and bid > 0:
            bid_ask_spread = (ask - bid) / bid * 100

        # Hard liquidity gate: untradeable contracts are REJECTED outright
        # (not merely downscored) — "no attractive setup" is the honest result.
        _vol = volume if volume is not None else 0
        _oi = oi if oi is not None else 0
        if _vol < 100 and _oi < 500:
            continue
        if bid_ask_spread is not None and bid_ask_spread > 20:
            continue

        # Moneyness scoring (ATM / slightly OTM preferred for directional trades)
        moneyness = None
        if strike and strike > 0 and spot_price and spot_price > 0:
            moneyness = (spot_price - strike) / strike
            if direction == SignalDirection.LONG:
                if -0.02 <= moneyness <= 0.03:
                    score += 25
                    rationale.append("ATM/slightly OTM (good directional exposure)")
                elif moneyness > 0.03:
                    score += 15
                else:
                    score += 10
            else:
                if -0.03 <= moneyness <= 0.02:
                    score += 25
                    rationale.append("ATM/slightly OTM (good directional exposure)")
                elif moneyness > 0.02:
                    score += 15
                else:
                    score += 10

        # Delta suitability
        if delta is not None:
            abs_delta = abs(delta)
            if 0.3 <= abs_delta <= 0.6:
                score += 20
                rationale.append(f"Delta {delta:.2f} (good balance)")
            elif 0.2 <= abs_delta <= 0.7:
                score += 15
            else:
                score += 5
                risks.append(f"Delta {delta:.2f} (extreme)")

        # Liquidity (volume + OI)
        if volume is not None and volume > 1000:
            score += 15
        elif volume is not None and volume > 100:
            score += 10
        else:
            risks.append("Low liquidity")

        if oi is not None and oi > 5000:
            score += 10
            rationale.append("High open interest")
        elif oi is not None and oi > 500:
            score += 5
        elif oi is not None:
            risks.append("Thin open interest")

        # Spread quality
        if bid_ask_spread is not None:
            if bid_ask_spread < 5:
                score += 10
                rationale.append(f"Tight spread ({bid_ask_spread:.1f}%)")
            elif bid_ask_spread < 10:
                score += 5
            else:
                risks.append(f"Wide spread: {bid_ask_spread:.1f}%")
        else:
            risks.append("Bid/ask unavailable")

        # Theta risk
        if theta is not None and theta < -0.05:
            risks.append("High theta decay")
            score -= 5

        # IV suitability
        if iv is not None:
            if iv > 0.5:
                risks.append(f"High IV: {iv:.0%}")
                score -= 5
            elif iv < 0.1:
                score -= 3
            else:
                score += 5
                rationale.append(f"IV {iv:.0%} in workable range")

        candidates.append(
            OptionsCandidate(
                strike=strike,
                option_type=contract.get("option_type", "CE" if direction == SignalDirection.LONG else "PE"),
                expiry=contract.get("expiry"),
                moneyness=moneyness,
                delta=delta,
                gamma=gamma,
                theta=theta,
                vega=vega,
                implied_vol=iv,
                open_interest=oi,
                volume=volume,
                bid_ask_spread=bid_ask_spread,
                score=min(max(score, 0), 100),
                rationale=rationale,
                risks=risks,
            )
        )

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:5]

