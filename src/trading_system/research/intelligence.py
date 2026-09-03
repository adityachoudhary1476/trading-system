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
# Data completeness & freshness (Phase 21 auditability)
# --------------------------------------------------------------------------- #
@dataclass
class DataCompleteness:
    """Score describing how much evidence is actually available.

    ``completeness`` is a 0..1 multiplier that CAPS achievable confidence:
    missing indicators or short history reduce it.  It is NOT added as a
    sub-score (which would inflate confidence toward the baseline).
    """
    completeness: float  # 0..1
    data_points: int = 0
    insufficient: bool = False
    missing_indicators: list[str] = field(default_factory=list)
    missing_data_sources: list[str] = field(default_factory=list)
    freshness_ms: Optional[float] = None  # age of the last bar in ms
    staleness_note: Optional[str] = None

    @property
    def score(self) -> float:
        return round(self.completeness * 100, 1)


def compute_data_completeness(
    features: TechnicalFeatures,
    context: InstrumentContext,
    *,
    freshness_ms: Optional[float] = None,
    news_available: bool = False,
    derivatives_available: bool = False,
    relative_strength_available: bool = False,
) -> DataCompleteness:
    """Score data completeness (completeness = 0..1, never fabricated).

    Starts at 1.0 and is reduced for:
    - insufficient data points
    - missing core indicators
    - stale data (when freshness_ms is provided)
    - unavailable data sources (news, derivatives, relative strength)
    """
    score = 1.0
    missing_ind: list[str] = []
    missing_sources: list[str] = []

    if features.insufficient:
        score = 0.3
        missing_ind.append("Insufficient historical data")
    else:
        if features.data_points < 50:
            score *= 0.6
            missing_ind.append("Limited data points (<50)")
        elif features.data_points < 100:
            score *= 0.8
            missing_ind.append("Limited data points (<100)")

    if features.sma_200 is None:
        score *= 0.9
        missing_ind.append("SMA200 (needs >= 200 bars)")
    if features.rsi_14 is None:
        score *= 0.9
        missing_ind.append("RSI14")
    if features.atr_14 is None:
        score *= 0.95
        missing_ind.append("ATR14")
    if features.relative_volume is None:
        score *= 0.9
        missing_ind.append("Volume / relative volume")

    # Missing contextual data sources reduce completeness (but not to zero)
    if not news_available:
        missing_sources.append("News / sentiment intelligence")
        score *= 0.9
    if not derivatives_available:
        missing_sources.append("Derivatives data (OI/IV/Greeks)")
        score *= 0.9
    if not relative_strength_available and context.instrument_class == InstrumentClass.EQUITY:
        missing_sources.append("Index relative-strength benchmark")
        score *= 0.9

    # Freshness: stale data caps confidence
    freshness_note = None
    if freshness_ms is not None and freshness_ms > 0:
        # Linear decay: data older than 24h → 0 freshness cap
        # 1h = 1.0, 6h = 0.75, 12h = 0.5, 24h = 0.0
        hours = freshness_ms / 3_600_000.0
        freshness_cap = max(0.0, min(1.0, 1.0 - hours / 24.0))
        if freshness_cap < 1.0:
            score *= freshness_cap
            freshness_note = f"Data {hours:.0f}h stale (completeness reduced)"

    return DataCompleteness(
        completeness=max(0.0, min(1.0, score)),
        data_points=features.data_points,
        insufficient=features.insufficient,
        missing_indicators=missing_ind,
        missing_data_sources=missing_sources,
        freshness_ms=freshness_ms,
        staleness_note=freshness_note,
    )


# --------------------------------------------------------------------------- #
# Relative strength (Phase 5) — equity vs index / sector context
# --------------------------------------------------------------------------- #
@dataclass
class RelativeStrength:
    """Relative performance of an instrument vs its benchmark."""
    symbol_return_pct: float          # instrument return over window
    benchmark_return_pct: float       # benchmark (e.g. NIFTY) return
    outperformance_pct: float         # symbol_return - benchmark_return
    timeframe: str
    available: bool = True
    note: str = ""


def compute_relative_strength(
    symbol_df: pd.DataFrame,
    benchmark_df: Optional[pd.DataFrame],
    timeframe: str = "1d",
) -> Optional[RelativeStrength]:
    """Compute relative strength of an equity vs its benchmark index.

    Returns None when benchmark data is unavailable (never fabricates).
    Uses the overlapping window of both series.
    """
    if benchmark_df is None or len(benchmark_df) == 0:
        return None
    if symbol_df is None or len(symbol_df) == 0:
        return None

    symbol_close = symbol_df["close"].sort_index()
    bench_close = benchmark_df["close"].sort_index()

    # Align on common timestamps (causal: only uses overlapping historical bars)
    aligned = pd.concat([symbol_close, bench_close], axis=1, join="inner").dropna()
    if len(aligned) < 2:
        return RelativeStrength(
            symbol_return_pct=0.0,
            benchmark_return_pct=0.0,
            outperformance_pct=0.0,
            timeframe=timeframe,
            available=False,
            note="Insufficient overlapping data for relative strength",
        )

    sym_ret = aligned.iloc[:, 0].iloc[-1] / aligned.iloc[:, 0].iloc[0] - 1.0
    bench_ret = aligned.iloc[:, 1].iloc[-1] / aligned.iloc[:, 1].iloc[0] - 1.0
    rs = sym_ret - bench_ret

    return RelativeStrength(
        symbol_return_pct=float(sym_ret) * 100,
        benchmark_return_pct=float(bench_ret) * 100,
        outperformance_pct=float(rs) * 100,
        timeframe=timeframe,
    )


# --------------------------------------------------------------------------- #
# Implied directional bias from features + regime
# --------------------------------------------------------------------------- #
def _implied_direction(features: TechnicalFeatures, regime: MarketRegime) -> SignalDirection:
    """Determine the directional bias the evidence implies.

    Mirrors the regime/trend precedence used in ``generate_signal_candidate``.
    """
    if regime.regime == RegimeEnum.TRENDING_UP:
        return SignalDirection.LONG
    if regime.regime == RegimeEnum.TRENDING_DOWN:
        return SignalDirection.SHORT
    if features.trend == TrendEnum.BULLISH:
        return SignalDirection.LONG
    if features.trend == TrendEnum.BEARISH:
        return SignalDirection.SHORT
    return SignalDirection.NEUTRAL


# --------------------------------------------------------------------------- #
# Evidence-based confidence calculation (Phase 2)
# --------------------------------------------------------------------------- #
def compute_evidence_confidence(
    features: TechnicalFeatures,
    regime: MarketRegime,
    context: InstrumentContext,
    *,
    index_features: Optional[TechnicalFeatures] = None,
    data_freshness_ms: Optional[float] = None,
) -> tuple[float, EvidenceLedger]:
    """Compute confidence from structured evidence (0..100).

    Scoring is **evidence-based, not midpoint-defaulting**.  Each dimension
    is scored by how much its evidence *agrees* with the implied direction:

    * 100 = strong agreement, 0 = strong contradiction, ~25 = no signal.
    * A dimension with **no data** contributes 0 to the weighted average
      (missing evidence does not inflate confidence).
    * ``data_quality`` is NOT a sub-score — it CAPS the achievable maximum
      via :func:`compute_data_completeness`, so missing indicators can
      never produce a false-confident 70%.
    * Conflicting evidence (``agreement == "mixed"``) is penalised harder
      (``*0.7``) than agreement is boosted (``*1.05``).
    """
    ledger = EvidenceLedger()

    if features.insufficient:
        ledger.missing.append("Insufficient historical data (< MIN_BARS)")
        # Score what little we have, but cap very low via completeness.
        completeness = 0.3
    else:
        completeness = compute_data_completeness(
            features, context,
            freshness_ms=data_freshness_ms,
            news_available=False,
            derivatives_available=False,
            relative_strength_available=index_features is not None,
        ).completeness
        for m in compute_data_completeness(
            features, context, freshness_ms=data_freshness_ms,
            news_available=False, derivatives_available=False,
            relative_strength_available=index_features is not None,
        ).missing_indicators:
            if m not in ledger.missing:
                ledger.missing.append(m)

    direction = _implied_direction(features, regime)

    # Each entry: (agreement_score 0..100, weight)
    dims: list[tuple[float, int]] = []

    # --- Trend (weight 25): price vs SMA20, SMA50, EMA cross ---
    d_score = 0.0
    d_max_weight = 25
    if features.price_vs_sma20 is not None:
        d = features.price_vs_sma20
        aligned = (direction == SignalDirection.LONG and d > 0) or \
                  (direction == SignalDirection.SHORT and d < 0)
        mag = min(abs(d) / 0.05, 1.0)  # 0.05 displacement → max score
        if aligned:
            d_score = mag * 90 + 10  # 10-100
            ledger.positive.append(
                f"Price {abs(d)*100:.1f}% {'above' if d>0 else 'below'} SMA20 (aligned with {direction.value})"
            )
        else:
            d_score = mag * 30  # 0-30: trend exists but contradicts
            ledger.negative.append(
                f"Price {abs(d)*100:.1f}% {'above' if d>0 else 'below'} SMA20 (contradicts {direction.value})"
            )
    else:
        ledger.missing.append("SMA20 unavailable")

    if features.price_vs_sma50 is not None:
        d50 = features.price_vs_sma50
        aligned50 = (direction == SignalDirection.LONG and d50 > 0) or \
                    (direction == SignalDirection.SHORT and d50 < 0)
        mag = min(abs(d50) / 0.05, 1.0)
        if aligned50:
            d_score = min(d_score + mag * 25 + 5, 100)
            ledger.positive.append(
                f"Price above SMA50" if d50 > 0 else "Price below SMA50 (confirms trend)"
            )
        else:
            d_score = max(d_score - mag * 15 - 5, 0)
            ledger.negative.append(
                f"Price contradicts SMA50"
            )
    else:
        ledger.missing.append("SMA50 unavailable")

    if features.ema20_vs_ema50 is not None:
        e = features.ema20_vs_ema50
        aligned_e = (direction == SignalDirection.LONG and e > 0) or \
                    (direction == SignalDirection.SHORT and e < 0)
        if aligned_e:
            d_score = min(d_score + 15, 100)
            ledger.positive.append("EMA20/EMA50 cross confirms direction")
        else:
            d_score = max(d_score - 15, 0)
            ledger.negative.append("EMA20/EMA50 cross contradicts direction")

    if direction == SignalDirection.NEUTRAL:
        d_score = 20  # no clear direction → low trend confidence
        ledger.neutral.append("No clear directional trend from price vs moving averages")

    dims.append((d_score, d_max_weight))

    # --- Momentum (weight 20): RSI, ROC ---
    m_score = 0.0
    if features.rsi_14 is not None:
        rsi = features.rsi_14
        if direction == SignalDirection.LONG:
            if rsi > 70:
                m_score = 20
                ledger.negative.append(f"RSI {rsi:.1f} overbought (divergence risk for long)")
            elif rsi > 50:
                m_score = min((rsi - 50) / 50 * 80 + 20, 100)
                ledger.positive.append(f"RSI {rsi:.1f} confirms upward momentum")
            elif rsi > 30:
                m_score = 25
                ledger.neutral.append(f"RSI {rsi:.1f} neutral for long case")
            elif rsi > 20:
                m_score = 80
                ledger.positive.append(f"RSI {rsi:.1f} oversold (bounce potential for long)")
            else:
                m_score = 70
                ledger.positive.append(f"RSI {rsi:.1f} deeply oversold")
        elif direction == SignalDirection.SHORT:
            if rsi < 30:
                m_score = 20
                ledger.negative.append(f"RSI {rsi:.1f} oversold (bounce risk for short)")
            elif rsi < 50:
                m_score = min((50 - rsi) / 50 * 80 + 20, 100)
                ledger.positive.append(f"RSI {rsi:.1f} confirms downward momentum")
            elif rsi < 70:
                m_score = 25
                ledger.neutral.append(f"RSI {rsi:.1f} neutral for short case")
            elif rsi < 80:
                m_score = 80
                ledger.positive.append(f"RSI {rsi:.1f} overbought (downside for short)")
            else:
                m_score = 70
                ledger.positive.append(f"RSI {rsi:.1f} deeply overbought")
        else:
            # NEUTRAL direction
            if rsi > 70 or rsi < 30:
                m_score = 10
                ledger.neutral.append(f"RSI {rsi:.1f} at extreme — waiting for direction")
            else:
                m_score = 20
                ledger.neutral.append(f"RSI {rsi:.1f} mid-range — no directional edge")
    else:
        ledger.missing.append("RSI unavailable")

    if features.roc is not None:
        roc = features.roc
        if direction != SignalDirection.NEUTRAL:
            aligned_roc = (direction == SignalDirection.LONG and roc > 0) or \
                          (direction == SignalDirection.SHORT and roc < 0)
            if aligned_roc:
                m_score = min(m_score + min(abs(roc) / 0.05 * 20, 20), 100)
                ledger.positive.append(f"ROC {roc*100:.1f}% reinforces direction")
            else:
                m_score = max(m_score - 10, 0)
                ledger.negative.append(f"ROC {roc*100:.1f}% contradicts direction")
        else:
            m_score = max(m_score, 10)
            ledger.neutral.append(f"ROC {roc*100:.1f}% — no direction context")

    dims.append((m_score, 20))

    # --- Volume (weight 15) ---
    v_score = 0.0
    if features.relative_volume is not None:
        rv = features.relative_volume
        if features.unusual_volume and rv > 2.0:
            v_score = 85
            ledger.positive.append(f"Unusual volume: {rv:.1f}x average (confirms move)")
        elif rv > 1.2:
            v_score = 60
            ledger.neutral.append(f"Volume {rv:.1f}x average (supports but not conclusive)")
        elif rv > 0.9:
            v_score = 40
            ledger.neutral.append(f"Volume near average ({rv:.1f}x)")
        elif rv > 0.7:
            v_score = 25
            ledger.neutral.append(f"Volume slightly below average ({rv:.1f}x)")
        else:
            v_score = 15
            ledger.negative.append(f"Low volume: {rv:.1f}x average (weak conviction)")
    else:
        ledger.missing.append("Volume data unavailable")
    dims.append((v_score, 15))

    # --- Regime (weight 15) ---
    r_score = 0.0
    if regime.regime == RegimeEnum.TRENDING_UP:
        r_score = 85
        ledger.positive.append("Market in confirmed uptrend (trending regime)")
    elif regime.regime == RegimeEnum.TRENDING_DOWN:
        r_score = 15
        ledger.negative.append("Market in confirmed downtrend (trending regime)")
    elif regime.regime == RegimeEnum.HIGH_VOLATILITY:
        r_score = 35
        ledger.negative.append("High volatility regime (direction less certain)")
        if features.hist_vol is not None:
            if features.hist_vol > context.high_vol_threshold:
                r_score = 20
                ledger.negative.append(
                    f"Annualized vol {features.hist_vol:.0%} above "
                    f"{context.symbol} normal band ({context.high_vol_threshold:.0%})"
                )
            elif features.hist_vol < context.low_vol_threshold:
                r_score = 50
                ledger.neutral.append(
                    f"Annualized vol {features.hist_vol:.0%} below "
                    f"{context.symbol} typical band ({context.low_vol_threshold:.0%})"
                )
        else:
            ledger.missing.append("Annualized volatility unavailable")
    elif regime.regime == RegimeEnum.RANGE_BOUND:
        r_score = 55
        ledger.neutral.append("Range-bound market (expect mean reversion)")
    elif regime.regime == RegimeEnum.LOW_VOLATILITY:
        r_score = 45
        ledger.neutral.append("Low volatility regime (compression, breakout risk)")
    else:  # UNKNOWN
        r_score = 20
        ledger.missing.append("Regime indeterminate (insufficient data)")
    dims.append((r_score, 15))

    # --- Structure (weight 10): range position ---
    s_score = 0.0
    if features.recent_high is not None and features.recent_low is not None:
        rng = features.recent_high - features.recent_low
        if rng > 0:
            pos = (features.close - features.recent_low) / rng
            if direction == SignalDirection.LONG:
                if pos > 0.8:
                    s_score = 85
                    ledger.positive.append("Price near recent high (upside extension)")
                elif pos > 0.5:
                    s_score = 70
                    ledger.positive.append("Price in upper range (constructive)")
                elif pos > 0.2:
                    s_score = 40
                    ledger.neutral.append("Price in mid-range")
                else:
                    s_score = 20
                    ledger.negative.append("Price near recent low (weak for long)")
            elif direction == SignalDirection.SHORT:
                if pos < 0.2:
                    s_score = 85
                    ledger.positive.append("Price near recent low (downside room)")
                elif pos < 0.5:
                    s_score = 70
                    ledger.positive.append("Price in lower range (constructive for short)")
                elif pos < 0.8:
                    s_score = 40
                    ledger.neutral.append("Price in mid-range")
                else:
                    s_score = 20
                    ledger.negative.append("Price near recent high (weak for short)")
            else:
                # NEUTRAL — structure itself doesn't imply a direction
                if pos > 0.8:
                    s_score = 45
                    ledger.neutral.append("Price near recent high — extended but no clear edge")
                elif pos < 0.2:
                    s_score = 45
                    ledger.neutral.append("Price near recent low — oversold but no clear edge")
                else:
                    s_score = 30
                    ledger.neutral.append("Price in mid-range — balanced structure")
    else:
        ledger.missing.append("Price range unavailable")
    dims.append((s_score, 10))

    # --- Relative strength (weight 10, equities only + index context) ---
    rs_score = _score_relative_strength(features, index_features, direction, context, ledger)
    if rs_score is not None:
        dims.append((rs_score, 10))

    # --- Data freshness (weight 10, when data_freshness_ms is provided) ---
    if data_freshness_ms is not None:
        f_score = 100.0
        if data_freshness_ms > 0:
            hours = data_freshness_ms / 3_600_000.0
            if hours > 24:
                f_score = 20
                ledger.missing.append(f"Data {hours:.0f}h stale (severe)")
            elif hours > 6:
                f_score = 40
                ledger.neutral.append(f"Data {hours:.0f}h stale (moderate)")
            elif hours > 1:
                f_score = 70
                ledger.neutral.append(f"Data {minutes_str(hours)} stale (mild)")
            else:
                f_score = 100
        dims.append((f_score, 10))

    # --- Compute weighted average ---
    total_weight = sum(w for _, w in dims)
    weighted_sum = sum(s * w for s, w in dims)
    raw_confidence = weighted_sum / total_weight if total_weight > 0 else 30.0

    # --- Evidence agreement adjustment (stronger conflict penalty) ---
    agreement = ledger.agreement
    if agreement == "mixed":
        raw_confidence *= 0.7
    elif agreement == "moderate":
        raw_confidence *= 0.92
    elif agreement == "strong":
        raw_confidence *= 1.05
    elif agreement == "neutral":
        raw_confidence *= 0.85

    # --- Cap by data completeness (not a contributor — a LIMIT) ---
    max_conf = completeness * 100
    confidence = min(raw_confidence, max_conf)
    confidence = max(0, min(100, confidence))

    return round(confidence, 1), ledger


def _score_relative_strength(
    features: TechnicalFeatures,
    index_features: Optional[TechnicalFeatures],
    direction: SignalDirection,
    context: InstrumentContext,
    ledger: EvidenceLedger,
) -> Optional[float]:
    """Score whether the instrument is outperforming its benchmark."""
    if index_features is None:
        return None  # not available — doesn't count as a dimension
    if context.instrument_class != InstrumentClass.EQUITY:
        # For indices, relative strength vs self is meaningless; skip.
        return None

    # Compare price_vs_sma20 of stock vs index
    stock_d = features.price_vs_sma20
    idx_d = index_features.price_vs_sma20
    if stock_d is None or idx_d is None:
        ledger.missing.append("Relative strength unavailable (incomplete benchmark)")
        return 20.0

    rs = stock_d - idx_d  # stock outperforms index when positive
    if direction == SignalDirection.LONG:
        if rs > 0.02:
            ledger.positive.append(f"Stock outperforming index (RS {rs*100:.1f}%)")
            return min(80 + rs * 500, 100)
        elif rs > 0:
            ledger.neutral.append(f"Stock roughly in line with index (RS {rs*100:.1f}%)")
            return 50
        else:
            ledger.negative.append(f"Stock underperforming index (RS {rs*100:.1f}%)")
            return max(20, 50 + rs * 300)
    elif direction == SignalDirection.SHORT:
        if rs < -0.02:
            ledger.positive.append(f"Stock underperforming index (RS {rs*100:.1f}%)")
            return min(80 + abs(rs) * 500, 100)
        elif rs < 0:
            ledger.neutral.append(f"Stock roughly in line with index (RS {rs*100:.1f}%)")
            return 50
        else:
            ledger.negative.append(f"Stock outperforming index (RS {rs*100:.1f}%)")
            return max(20, 50 - rs * 300)
    else:
        ledger.neutral.append(f"Relative strength {rs*100:.1f}% — no directional context")
        return 30


def _score_freshness(data_freshness_ms: float, ledger: EvidenceLedger) -> float:
    """Score 0-100 based on data age."""
    hours = data_freshness_ms / 3_600_000.0
    if hours > 24:
        return 20.0
    elif hours > 6:
        return 40.0
    elif hours > 1:
        return 70.0
    return 100.0


def minutes_str(hours: float) -> str:
    mins = int(hours * 60)
    if mins >= 60:
        return f"{mins/60:.1f}h"
    return f"{mins}m"


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
    invalidation: Optional[str] = None
    evidence_ledger: Optional[EvidenceLedger] = None

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
    index_features: Optional["TechnicalFeatures"] = None,
    data_freshness_ms: Optional[float] = None,
) -> SignalCandidate:
    """Deterministic analytical hypothesis from features + regime. No LLM.

    ``index_features``: optional features computed for the benchmark/instrument index
        (e.g. NIFTY 50) — used to compute relative strength.
    ``data_freshness_ms``: optional milliseconds elapsed since the data used for
        ``features`` was last refreshed — penalizes stale data.
    """
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
    confidence_score, ledger = compute_evidence_confidence(
        features, regime, context,
        index_features=index_features,
        data_freshness_ms=data_freshness_ms,
    )
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
        invalidation=inv, evidence_ledger=ledger,
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
    # Traceability: the timestamp and price of the last *closed* bar the
    # analysis was computed on.  reason() uses these for the MarketSnapshot so
    # the AI decision snapshot is auditable rather than stamped with the
    # wall-clock time of the call.
    decision_timestamp: Optional[datetime] = None
    decision_price: Optional[float] = None

    def to_json(self) -> dict:
        return {
            "instrument": self.instrument,
            "timeframe": self.timeframe,
            "market_regime": self.market_regime,
            "features": self.features,
            "signal_candidate": self.signal_candidate,
            "recent_candles": self.recent_candles,
            "data_quality": self.data_quality,
            "decision_timestamp": self.decision_timestamp.isoformat() if self.decision_timestamp else None,
            "decision_price": self.decision_price,
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
        # decision_timestamp is the last closed-bar timestamp the analysis was
        # computed on (see MarketIntelligenceEngine.analyze).  This makes the AI
        # snapshot auditable instead of stamping the wall-clock call time.
        decision_ts = context.decision_timestamp or datetime.now(timezone.utc)
        decision_px = context.decision_price
        if decision_px is None:
            decision_px = float(feat.get("close", 0.0) or 0.0)
        snap = MarketSnapshot(
            symbol=context.instrument.get("symbol", "?"),
            timeframe=context.timeframe,
            timestamp=decision_ts,
            last_bar_timestamp=decision_ts,
            latest_price=decision_px,
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
         benchmark_df: Optional[pd.DataFrame] = None,
         data_freshness_ms: Optional[float] = None,
         news_sentiment: Optional[dict] = None,
         multi_timeframe_dfs: Optional[dict[str, pd.DataFrame]] = None,
     ) -> dict:
        """Run the full deterministic analysis. Returns a serializable dict.

        If health_status is not HEALTHY (or data insufficient) the result is marked
        BLOCKED with a reason — no fake indicators.

        ``option_chain``: optional live option-chain rows (dicts with strike /
        option_type / delta / theta / implied_vol / open_interest / volume /
        bid / ask). When provided, scored OptionsCandidate objects are generated
        for directional views. When absent, options intelligence is explicitly
        reported as unavailable — NEVER fabricated.

        ``benchmark_df``: optional DataFrame for a benchmark/instrument index
            (e.g. NIFTY 50). Used to compute relative strength. When absent,
            relative-strength intelligence is reported as unavailable.
        ``data_freshness_ms``: optional milliseconds elapsed since the data was
            last refreshed — penalizes stale data.
        ``news_sentiment``: optional pre-computed news/sentiment summary dict.
            When absent, news intelligence is reported as unavailable.
        ``multi_timeframe_dfs``: optional dict of timeframe -> DataFrame for
            multi-timeframe analysis.
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

        # Compute index features for relative-strength if benchmark df provided
        index_features = None
        if benchmark_df is not None and len(benchmark_df) > 0:
            index_features = self.engine.compute(benchmark_df, InstrumentClass.INDEX)

        candidate = generate_signal_candidate(
            symbol, contract_id or symbol, timeframe, features, regime, iclass, context,
            ts=ts, index_features=index_features, data_freshness_ms=data_freshness_ms,
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

        # Data completeness report
        completeness = compute_data_completeness(
            features, context,
            freshness_ms=data_freshness_ms,
            news_available=news_sentiment is not None,
            derivatives_available=option_chain is not None,
            relative_strength_available=index_features is not None,
        )

        # Multi-timeframe analysis
        multi_tf_results = None
        if multi_timeframe_dfs:
            multi_tf_results = analyze_multi_timeframe(symbol, multi_timeframe_dfs, context)

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
            "decision_timestamp": ts,
            "decision_price": features.close,
            "data_quality": {
                "rows": features.data_points,
                "insufficient": features.insufficient,
                "missing": explanation.missing_data,
            },
            "data_completeness": completeness,
            "relative_strength": index_features is not None and compute_relative_strength(features, index_features),
            "news_sentiment": news_sentiment if news_sentiment is not None else {"status": "unavailable", "summary": "No news sentiment provided"},
            "multi_timeframe": multi_tf_results,
        }

# --------------------------------------------------------------------------- #
# Multi-timeframe analysis (per-horizon directional assessments)
# --------------------------------------------------------------------------- #
def analyze_multi_timeframe(
    symbol: str,
    dfs: dict[str, pd.DataFrame],
    context: InstrumentContext,
    index_features: Optional["TechnicalFeatures"] = None,
    data_freshness_ms: Optional[float] = None,
) -> dict[str, TimeframeAnalysis]:
    """Analyze multiple timeframes and produce per-timeframe assessments.

    Each timeframe gets its OWN features, regime, evidence ledger and
    confidence — horizons may legitimately disagree (e.g. bearish intraday,
    bullish swing). No data is fabricated: insufficient data produces an
    explicit NEUTRAL assessment with the reason recorded.

    ``index_features``: optional benchmark features for relative strength.
    ``data_freshness_ms``: optional data staleness in milliseconds.
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
        confidence, ledger = compute_evidence_confidence(
            features, regime, context,
            index_features=index_features,
            data_freshness_ms=data_freshness_ms,
        )

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

        # Moneyness scoring: continuous preference for ATM / slightly OTM.
        # LONG (CE): ideal ~1% OTM; SHORT (PE): mirrored. Deep ITM/OTM decays.
        moneyness = None
        if strike and strike > 0 and spot_price and spot_price > 0:
            moneyness = (spot_price - strike) / strike
            target = -0.01 if direction == SignalDirection.LONG else 0.01
            dist = abs(moneyness - target)
            score += max(0.0, 25.0 * (1.0 - dist / 0.05))
            if dist <= 0.02:
                rationale.append("ATM/slightly OTM (good directional exposure)")

        # Delta suitability: continuous, ideal |delta| ~0.45 (0.3-0.6 band noted).
        if delta is not None:
            abs_delta = abs(delta)
            score += max(0.0, 20.0 * (1.0 - abs(abs_delta - 0.45) / 0.45))
            if 0.3 <= abs_delta <= 0.6:
                rationale.append(f"Delta {delta:.2f} (good balance)")
            elif abs_delta < 0.2 or abs_delta > 0.7:
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

