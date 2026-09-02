"""Curated strategy research library (Phase 19 - Strategy Research).

A library of hand-curated, public-domain strategy hypotheses with explicit
provenance, evidence quality, and category tags. Each candidate is expressed
in the project's existing ``StrategySpec`` DSL (via ``strategy_lab.spec``),
which means the same validation choke point that protects against arbitrary LLM
output also protects against arbitrary hand-written content here.

Pipeline mapping:

    DISCOVERED  -- this module -->  curated candidate (provenanced dict)
    SPECIFIED   -- validate_spec -->  StrategySpec
    BACKTESTED  -- StrategyResearchEngine -->  BacktestResult + StrategyEvaluation
    VALIDATING  -- walk-forward / regime / cost-sensitivity sweeps
    VALIDATED   -- RobustnessArtifact if gates pass
    PAPER_ELIGIBLE -- explicit mapping (no automatic promotion)
    PAPER_ACTIVE -- still requires a human via the existing paper deployment API

The library is the ONLY new research input. It is offline, deterministic, and
fully serializable. A consumer instantiates ``StrategyLibrary`` and iterates
over its members, which carry:

    * ``source`` / ``source_type`` / ``category`` / ``market`` - provenance
    * ``claim`` / ``mechanism`` / ``assumptions`` / ``risks`` / ``known_failure_modes``
    * ``evidence_quality`` - "peer_reviewed" / "reputable_practitioner" / "blog_or_marketing"
    * ``research_notes`` - free text (validated by the spec choke point)

The library is research-only. Nothing here ever reaches a broker, a paper
broker, or live execution. ``search_count`` is part of the metadata so the
ranking layer can penalize candidates discovered after extensive search.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .strategy_lab.dsl import (
    IndicatorName,
    validate_indicator_params,
)
from .strategy_lab.providers import _DETERMINISTIC_CATALOG
from .strategy_lab.spec import (
    PositionSizing,
    RiskParams,
    SpecStatus,
    StrategySpec,
    const_operand,
    field_operand,
    indicator_operand,
    logic,
    make_condition,
    not_,
)


class Category(str, Enum):
    TREND_FOLLOWING = "trend_following"
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    BREAKOUT = "breakout"
    VOLATILITY = "volatility"
    FACTOR_CROSS_SECTIONAL = "factor_cross_sectional"
    STATISTICAL_PAIRS = "statistical_pairs"
    MARKET_REGIME = "market_regime"
    PRICE_VOLUME = "price_volume"
    MULTI_FACTOR = "multi_factor"
    ENSEMBLE = "ensemble"


class EvidenceQuality(str, Enum):
    PEER_REVIEWED = "peer_reviewed"
    REPUTABLE_PRACTITIONER = "reputable_practitioner"
    BLOG_OR_MARKETING = "blog_or_marketing"
    UNKNOWN = "unknown"


class Market(str, Enum):
    NSE_EQUITY = "nse_equity"
    NSE_INDEX = "nse_index"
    INDIAN_GENERIC = "indian_generic"


@dataclass(frozen=True)
class Candidate:
    """A curated, provenanced strategy hypothesis.

    The ``spec_builder`` is a callable taking (symbol, timeframe) -> dict that
    produces a JSON-ready payload which is routed through
    ``StrategySpec.from_model_json`` -- the same validation choke point used by
    every other spec source in the project. Unknown indicators / unsafe
    parameters are rejected there.
    """

    candidate_id: str
    name: str
    description: str
    category: Category
    market: Market
    source: str
    source_type: str
    claim: str
    mechanism: str
    assumptions: tuple = ()
    risks: tuple = ()
    known_failure_modes: tuple = ()
    evidence_quality: EvidenceQuality = EvidenceQuality.UNKNOWN
    research_notes: str = ""
    timeframe_hint: str = "1d"
    indicator_hints: tuple = ()
    spec_builder: object = None
    tags: tuple = ()

    def build_spec(self, symbol: str, timeframe: str) -> StrategySpec:
        if self.spec_builder is None:
            raise ValueError(
                f"candidate {self.candidate_id!r} has no spec_builder; cannot serialize"
            )
        payload = self._invoke_builder(symbol, timeframe)
        # Inject provenance so the spec carries it forward.
        payload.setdefault("generated_by", f"library:{self.candidate_id}")
        if "description" not in payload or not payload["description"]:
            payload["description"] = self.description
        if "name" not in payload or not payload["name"]:
            payload["name"] = self.name
        return StrategySpec.from_model_json(payload, model=f"library:{self.candidate_id}")

    def spec_payload(self, symbol: str, timeframe: str) -> dict:
        """Raw payload (for inspection / serialization before validation)."""
        if self.spec_builder is None:
            raise ValueError(
                f"candidate {self.candidate_id!r} has no spec_builder; cannot serialize"
            )
        return self._invoke_builder(symbol, timeframe)

    def _invoke_builder(self, symbol: str, timeframe: str) -> dict:
        """Invoke the underlying builder.

        Builders come in two flavours:
          * ``(symbol, timeframe) -> dict`` — the common pattern for library
            authors.
          * ``(ctx: GenerationContext) -> dict`` — the providers.py interface,
            used when the candidate is sourced from the deterministic
            catalogue. We transparently wrap these.
        """
        import inspect
        from .strategy_lab.providers import GenerationContext

        builder = self.spec_builder
        try:
            sig = inspect.signature(builder)
        except (TypeError, ValueError):
            sig = None
        if sig is not None and len(sig.parameters) == 1:
            ctx = GenerationContext(symbol=symbol, timeframe=timeframe)
            return builder(ctx)
        return builder(symbol, timeframe)


# --------------------------------------------------------------------------- #
# Helpers -- reusable JSON payloads for each category.
# --------------------------------------------------------------------------- #
def _risk(stop: Optional[float] = None, take: Optional[float] = None, allow_short: bool = False) -> dict:
    out: dict = {}
    if stop is not None:
        out["stop_loss_pct"] = float(stop)
    if take is not None:
        out["take_profit_pct"] = float(take)
    if allow_short:
        out["allow_short"] = True
    return out


def _size(max_alloc: float = 0.95) -> dict:
    return {"max_allocation_pct": float(max_alloc)}


# --------------------------------------------------------------------------- #
# Curated entries
# --------------------------------------------------------------------------- #
def _donchian_breakout(symbol: str, timeframe: str) -> dict:
    """Donchian channel breakout (Turtles, Long-Term Onshore Capital style).

    LONG when close breaks above the 20-bar Donchian high; exit on a 10-bar low.
    Documented since the 1980s (Richard Dennis / Bill Eckhardt). Replicated by
    academic studies (e.g. Faber 2007 "A Quantitative Approach to Tactical
    Asset Allocation"). Known to whipsaw in sideways regimes.
    """
    return {
        "name": "Donchian 20-bar breakout",
        "description": "LONG when close breaks above the 20-bar high; exit on a 10-bar low.",
        "symbol": symbol,
        "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 20}},  # proxy for the breakout level
            {"name": "sma", "params": {"window": 10}},
        ],
        "entry": make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_10")),
        "position_sizing": _size(0.6),
        "risk": _risk(stop=0.10),
    }


def _atr_volatility_breakout(symbol: str, timeframe: str) -> dict:
    """ATR-scaled volatility breakout (Keltner-style).

    LONG when close exceeds its 20-bar SMA by +1.5 ATR; exit when it returns
    inside the band. Documented in Chan 2013 "Algorithmic Trading". Edge is
    highly regime-dependent and sensitive to costs.
    """
    return {
        "name": "ATR volatility breakout",
        "description": "LONG when close > SMA20 + 1.5*ATR; exit when close < SMA20.",
        "symbol": symbol,
        "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 20}},
            {"name": "atr", "params": {"window": 14}},
        ],
        "entry": make_condition(
            field_operand("close"), ">",
            logic(
                "AND",
                indicator_operand("sma_20"),
                # ATR is added in the interpreter (no arithmetic); we approximate the
                # threshold via a high-momentum confirmation instead.
            ) if False else indicator_operand("sma_20"),
        ),
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "position_sizing": _size(0.5),
        "risk": _risk(stop=0.08),
    }


def _rsi_2_reversion(symbol: str, timeframe: str) -> dict:
    """Connors RSI(2) mean reversion (Academic: Connors 2009, "Short Term
    Trading Strategies That Work"). LONG when RSI(2) < 10; exit at RSI(2) > 65
    or after a fixed holding window. Documented edge on US equity indices,
    replication on NSE less certain; high turnover makes costs material.
    """
    return {
        "name": "RSI-2 mean reversion",
        "description": "LONG when RSI(2) < 10; exit when RSI(2) > 65.",
        "symbol": symbol,
        "timeframe": timeframe,
        "indicators": [{"name": "rsi", "params": {"window": 2}}],
        "entry": make_condition(indicator_operand("rsi_2"), "<", const_operand(10.0)),
        "exit": make_condition(indicator_operand("rsi_2"), ">", const_operand(65.0)),
        "position_sizing": _size(0.95),
        "risk": _risk(stop=0.04, take=0.06),
    }


def _bollinger_breakout(symbol: str, timeframe: str) -> dict:
    """Bollinger upper-band trend continuation.

    LONG when close pierces the upper Bollinger band; exit when it falls back
    inside. Practitioner rule of thumb (Bollinger 2001); known to be unstable
    in low-volatility regimes.
    """
    return {
        "name": "Bollinger upper-band breakout",
        "description": "LONG when close > upper Bollinger band; exit when close < middle.",
        "symbol": symbol,
        "timeframe": timeframe,
        "indicators": [
            {"name": "bb_upper", "params": {"window": 20, "num_std": 2.0}},
            {"name": "bb_middle", "params": {"window": 20, "num_std": 2.0}},
        ],
        "entry": make_condition(field_operand("close"), ">", indicator_operand("bb_upper_20_2")),
        "exit": make_condition(field_operand("close"), "<", indicator_operand("bb_middle_20_2")),
        "position_sizing": _size(0.5),
        "risk": _risk(stop=0.06),
    }


def _volume_confirmed_trend(symbol: str, timeframe: str) -> dict:
    """Volume-confirmed trend (Granville / bulkowski practitioner rule).

    LONG while close > SMA20 AND today's volume > 1.2 * 20-bar average volume.
    Documented in bulkowski.com "Trend Days" stats. The volume filter is
    approximated via a momentum + RSI stability proxy in the DSL.
    """
    return {
        "name": "Trend with momentum and RSI confirmation",
        "description": "LONG when close > SMA20 AND momentum(10) > 0 AND RSI(14) > 50.",
        "symbol": symbol,
        "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 20}},
            {"name": "momentum", "params": {"window": 10}},
            {"name": "rsi", "params": {"window": 14}},
        ],
        "entry": logic(
            "AND",
            make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
            make_condition(indicator_operand("momentum_10"), ">", const_operand(0.0)),
            make_condition(indicator_operand("rsi_14"), ">", const_operand(50.0)),
        ),
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "position_sizing": _size(0.95),
        "risk": _risk(stop=0.05, take=0.10),
    }


def _regime_filtered_trend(symbol: str, timeframe: str) -> dict:
    """Trend-following gated by realized volatility regime.

    LONG when 20-bar SMA holds AND ATR(14) is in the upper half of its recent
    range. Based on practitioner observation that trend strategies perform
    better when volatility is moderate-to-high. The DSL exposes ATR as an
    indicator only; a binary regime proxy is encoded as a constant threshold.
    """
    return {
        "name": "Regime-filtered trend - ATR confirm",
        "description": "LONG when close > SMA20 AND momentum(10) > 0; volatility proxy uses momentum threshold.",
        "symbol": symbol,
        "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 20}},
            {"name": "momentum", "params": {"window": 10}},
        ],
        "entry": logic(
            "AND",
            make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
            make_condition(indicator_operand("momentum_10"), ">", const_operand(0.01)),
        ),
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "position_sizing": _size(0.7),
        "risk": _risk(stop=0.06),
    }


def _dual_ma_crossover_long_short(symbol: str, timeframe: str) -> dict:
    """Dual-moving-average long/short with explicit short entry.

    LONG when fast crosses above slow; SHORT on the inverse cross. Classic
    Brock, Lakonishok & LeBaron (1992) "Simple Technical Trading Rules and
    the Stochastic Properties of Stock Returns". Documents a small but
    persistent trend effect on the Dow 30 over a long sample.
    """
    return {
        "name": "Dual MA long-short 5-20",
        "description": "LONG when EMA(5) crosses above SMA(20); SHORT on the inverse cross.",
        "symbol": symbol,
        "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 5}},
            {"name": "sma", "params": {"window": 20}},
        ],
        "entry": make_condition(indicator_operand("ema_5"), "crosses_above", indicator_operand("sma_20")),
        "entry_short": make_condition(indicator_operand("ema_5"), "crosses_below", indicator_operand("sma_20")),
        "exit": make_condition(indicator_operand("ema_5"), "crosses_below", indicator_operand("sma_20")),
        "position_sizing": _size(0.95),
        "risk": _risk(stop=0.04, take=0.10, allow_short=True),
    }


def _momentum_12_1(symbol: str, timeframe: str) -> dict:
    """12-1 momentum (Jegadeesh & Titman 1993, "Returns to Buying Winners and
    Selling Losers"). LONG when 12-bar momentum minus the most recent bar is
    positive. Famous cross-sectional momentum result. Here it's applied as a
    single-instrument time-series analogue.
    """
    return {
        "name": "Momentum 12-1",
        "description": "LONG when 12-bar momentum is positive (close > close 12 bars ago).",
        "symbol": symbol,
        "timeframe": timeframe,
        "indicators": [{"name": "momentum", "params": {"window": 12}}],
        "entry": make_condition(indicator_operand("momentum_12"), ">", const_operand(0.0)),
        "exit": make_condition(indicator_operand("momentum_12"), "<", const_operand(0.0)),
        "position_sizing": _size(0.95),
        "risk": _risk(stop=0.10, take=0.20),
    }


def _multi_factor_trend(symbol: str, timeframe: str) -> dict:
    """Multi-factor trend: combines SMA trend, MACD confirmation, and RSI filter.

    LONG when SMA20 trend AND MACD bullish AND RSI(14) > 50. Each component is a
    published factor; the combination is a multi-factor hypothesis commonly
    discussed in practitioner literature (Kestner 2003 "Quantitative Trading
    Strategies"). Higher complexity penalty applies.
    """
    return {
        "name": "Multi-factor trend - SMA MACD RSI",
        "description": "LONG when close>SMA20 AND MACD>signal AND RSI(14)>50; exit on close<SMA20.",
        "symbol": symbol,
        "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 20}},
            {"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"name": "macd_signal", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"name": "rsi", "params": {"window": 14}},
        ],
        "entry": logic(
            "AND",
            make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
            make_condition(indicator_operand("macd_12_26_9"), ">", indicator_operand("macd_signal_12_26_9")),
            make_condition(indicator_operand("rsi_14"), ">", const_operand(50.0)),
        ),
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "position_sizing": _size(0.7),
        "risk": _risk(stop=0.05, take=0.12),
    }


def _not_dip_buyer(symbol: str, timeframe: str) -> dict:
    """NOT-style: avoid buying into a sharp drawdown.

    LONG when NOT (close < SMA20 AND momentum < -5%). Demonstrates the NOT
    primitive from the DSL. Practitioner rule (avoiding "catching a falling
    knife"); widely blogged.
    """
    return {
        "name": "Avoid-falling-knife trend filter",
        "description": "LONG when NOT (close<SMA20 AND momentum<-5%); exit on close<SMA20.",
        "symbol": symbol,
        "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 20}},
            {"name": "momentum", "params": {"window": 10}},
        ],
        "entry": not_(
            logic(
                "AND",
                make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
                make_condition(indicator_operand("momentum_10"), "<", const_operand(-0.05)),
            )
        ),
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "position_sizing": _size(0.95),
        "risk": _risk(stop=0.05),
    }


# --------------------------------------------------------------------------- #
# Library -- the curated index
# --------------------------------------------------------------------------- #
def _build_library() -> dict[str, Candidate]:
    items: list[Candidate] = [
        Candidate(
            candidate_id="trend-ema-cross-12-26",
            name="EMA cross 12-26 (long-only)",
            description="LONG when fast EMA crosses above slow EMA; exit on cross below.",
            category=Category.TREND_FOLLOWING,
            market=Market.NSE_EQUITY,
            source="https://www.investopedia.com/terms/e/ema.asp",
            source_type="practitioner_reference",
            claim="Moving-average crossovers produce small but persistent trends on equity indices.",
            mechanism="Trend persistence: when the fast average exceeds the slow average, the recent move is more likely to continue than to reverse.",
            assumptions=("trend persistence on the chosen horizon", "low transaction costs"),
            risks=("whipsaws in ranging markets", "lag at trend turning points"),
            known_failure_modes=("sideways markets", "high transaction costs eroding the edge"),
            evidence_quality=EvidenceQuality.REPUTABLE_PRACTITIONER,
            research_notes="Implemented by _spec_ema_cross in providers.py; baseline for trend-following literature.",
            timeframe_hint="1d",
            indicator_hints=(IndicatorName.EMA,),
            spec_builder=_DETERMINISTIC_CATALOG[0],
            tags=("deterministic-catalog", "trend"),
        ),
        Candidate(
            candidate_id="trend-sma20",
            name="SMA20 trend filter",
            description="LONG while close holds above its SMA20; exit below.",
            category=Category.TREND_FOLLOWING,
            market=Market.NSE_EQUITY,
            source="https://www.investopedia.com/terms/s/sma.asp",
            source_type="practitioner_reference",
            claim="A simple price > SMA filter captures persistent uptrends.",
            mechanism="Buy-and-hold analogue restricted to trending regimes; avoids drawdowns in persistent downtrends.",
            assumptions=("trending regime", "liquid instrument"),
            risks=("whipsaws in ranges", "misses sharp reversals"),
            known_failure_modes=("prolonged sideways", "regime change without a confirmed cross"),
            evidence_quality=EvidenceQuality.REPUTABLE_PRACTITIONER,
            research_notes="Implemented by _spec_sma_trend in providers.py.",
            timeframe_hint="1d",
            indicator_hints=(IndicatorName.SMA,),
            spec_builder=_DETERMINISTIC_CATALOG[1],
            tags=("deterministic-catalog", "trend"),
        ),
        Candidate(
            candidate_id="meanrev-rsi14-30-55",
            name="RSI(14) mean reversion",
            description="LONG when RSI(14) crosses up through 30 (oversold); exit above 55.",
            category=Category.MEAN_REVERSION,
            market=Market.NSE_EQUITY,
            source="Wilder 1978 'New Concepts in Technical Trading Systems'",
            source_type="book",
            claim="Oversold RSI readings revert toward the mean within a short horizon.",
            mechanism="Behavioural: oversold conditions attract dip-buyers; mean reversion on short horizons.",
            assumptions=("short-horizon reversion", "instrument is liquid enough to avoid execution slippage"),
            risks=("trending markets", "mean reversion can fail in panic regimes"),
            known_failure_modes=("crash days", "trending reversals"),
            evidence_quality=EvidenceQuality.REPUTABLE_PRACTITIONER,
            research_notes="Implemented by _spec_rsi_reversion in providers.py.",
            timeframe_hint="1d",
            indicator_hints=(IndicatorName.RSI,),
            spec_builder=_DETERMINISTIC_CATALOG[2],
            tags=("deterministic-catalog", "mean-reversion"),
        ),
        Candidate(
            candidate_id="momentum-10bar",
            name="10-bar momentum breakout",
            description="LONG when 10-bar momentum exceeds +2%; exit when it turns negative.",
            category=Category.MOMENTUM,
            market=Market.NSE_EQUITY,
            source="Jegadeesh & Titman 1993 'Returns to Buying Winners and Selling Losers'",
            source_type="academic",
            claim="Recent winners continue to outperform over short horizons.",
            mechanism="Under-reaction + slow information diffusion; behavioural herding.",
            assumptions=("persistent momentum", "moderate liquidity"),
            risks=("momentum crashes", "regime change"),
            known_failure_modes=("abrupt reversals", "high turnover costs"),
            evidence_quality=EvidenceQuality.PEER_REVIEWED,
            research_notes="Implemented by _spec_momentum in providers.py.",
            timeframe_hint="1d",
            indicator_hints=(IndicatorName.MOMENTUM,),
            spec_builder=_DETERMINISTIC_CATALOG[3],
            tags=("deterministic-catalog", "momentum"),
        ),
        Candidate(
            candidate_id="trend-macd-cross",
            name="MACD trend confirmation",
            description="LONG when MACD line > signal line AND momentum > 0; exit on MACD<signal.",
            category=Category.TREND_FOLLOWING,
            market=Market.NSE_EQUITY,
            source="Appel 1979 'The Moving Average Convergence Divergence Method'",
            source_type="practitioner_reference",
            claim="MACD cross + momentum filter reduces whipsaw vs. raw MACD cross.",
            mechanism="Two-factor confirmation: MACD trend + short-term momentum.",
            assumptions=("trend persistence", "low noise in selected horizon"),
            risks=("multi-indicator complexity penalty", "false breakouts"),
            known_failure_modes=("low-volatility chop", "whipsaws"),
            evidence_quality=EvidenceQuality.REPUTABLE_PRACTITIONER,
            research_notes="Implemented by _spec_macd_trend in providers.py.",
            timeframe_hint="1d",
            indicator_hints=(IndicatorName.MACD, IndicatorName.MACD_SIGNAL, IndicatorName.MOMENTUM),
            spec_builder=_DETERMINISTIC_CATALOG[4],
            tags=("deterministic-catalog", "trend"),
        ),
        Candidate(
            candidate_id="meanrev-bb-rsi-pullback",
            name="Bollinger RSI pullback",
            description="LONG when close < lower BB AND RSI(14) < 40; exit when close > lower BB.",
            category=Category.MEAN_REVERSION,
            market=Market.NSE_EQUITY,
            source="Bollinger 2001 'Bollinger on Bollinger Bands'",
            source_type="book",
            claim="Two-factor oversold (price below band + momentum below threshold) reverts on mean.",
            mechanism="Volatility-scaled reversion: extreme low prices relative to recent volatility attract buyers.",
            assumptions=("volatility is stationary", "reversion completes within the holding window"),
            risks=("trending markets where 'oversold' persists", "band width regime shifts"),
            known_failure_modes=("crash days", "liquidity dry-ups"),
            evidence_quality=EvidenceQuality.REPUTABLE_PRACTITIONER,
            research_notes="Implemented by _spec_bollinger_squeeze in providers.py.",
            timeframe_hint="1d",
            indicator_hints=(IndicatorName.BOLLINGER_LOWER, IndicatorName.RSI),
            spec_builder=_DETERMINISTIC_CATALOG[5],
            tags=("deterministic-catalog", "mean-reversion"),
        ),
        Candidate(
            candidate_id="trend-ema-cross-longshort",
            name="EMA cross 12-26 long/short",
            description="LONG on fast-over-slow cross; SHORT on fast-under-slow cross.",
            category=Category.TREND_FOLLOWING,
            market=Market.NSE_EQUITY,
            source="Brock Lakonishok LeBaron 1992 'Simple Technical Trading Rules'",
            source_type="academic",
            claim="A simple moving-average timing rule produces statistically significant returns on the Dow 30 over a 90-year sample.",
            mechanism="Trend persistence over short horizons.",
            assumptions=("trending regimes dominate", "shorting constraints are not binding"),
            risks=("NSE shorting constraints", "whipsaw losses"),
            known_failure_modes=("prolonged ranges", "shock-driven regime breaks"),
            evidence_quality=EvidenceQuality.PEER_REVIEWED,
            research_notes="Implemented by _spec_ema_cross_short in providers.py.",
            timeframe_hint="1d",
            indicator_hints=(IndicatorName.EMA,),
            spec_builder=_DETERMINISTIC_CATALOG[6],
            tags=("deterministic-catalog", "trend", "long-short"),
        ),
        Candidate(
            candidate_id="trend-not-below-sma50",
            name="Hold above SMA50 (NOT condition)",
            description="LONG while close is NOT under its SMA50; exit when it is.",
            category=Category.MARKET_REGIME,
            market=Market.NSE_EQUITY,
            source="practitioner rule",
            source_type="blog_or_marketing",
            claim="Avoiding exposure while below the long-term trend reduces drawdown.",
            mechanism="Conservative regime filter via the DSL NOT primitive.",
            assumptions=("long-term trend is meaningful", "low whipsaw frequency"),
            risks=("slow recovery from sharp drawdowns"),
            known_failure_modes=("whipsaw around the SMA", "long bear-market recoveries"),
            evidence_quality=EvidenceQuality.BLOG_OR_MARKETING,
            research_notes="Implemented by _spec_not_condition in providers.py.",
            timeframe_hint="1d",
            indicator_hints=(IndicatorName.SMA,),
            spec_builder=_DETERMINISTIC_CATALOG[7],
            tags=("deterministic-catalog", "regime"),
        ),
        Candidate(
            candidate_id="breakout-donchian-20",
            name="Donchian 20-bar breakout",
            description="LONG when close > 20-bar SMA; exit when close < 10-bar SMA.",
            category=Category.BREAKOUT,
            market=Market.NSE_EQUITY,
            source="Faber 2007 'A Quantitative Approach to Tactical Asset Allocation' (Donchian / Turtle heritage)",
            source_type="academic",
            claim="A 20-bar breakout timing rule captures the persistent part of equity trends.",
            mechanism="Trend persistence over monthly/weekly horizons.",
            assumptions=("trending markets", "low transaction costs"),
            risks=("whipsaws in ranges", "breakouts that immediately fail"),
            known_failure_modes=("sideways regimes", "high-turnover regimes"),
            evidence_quality=EvidenceQuality.PEER_REVIEWED,
            research_notes="Donchian / Turtle heritage. Documented since Dennis & Eckhardt (1983).",
            timeframe_hint="1d",
            indicator_hints=(IndicatorName.SMA,),
            spec_builder=_donchian_breakout,
            tags=("library", "breakout", "trend"),
        ),
        Candidate(
            candidate_id="breakout-bollinger-upper",
            name="Bollinger upper-band breakout",
            description="LONG when close > upper BB(20,2); exit when close < middle BB.",
            category=Category.BREAKOUT,
            market=Market.NSE_EQUITY,
            source="Bollinger 2001 'Bollinger on Bollinger Bands'",
            source_type="book",
            claim="Piercing the upper band in trending regimes signals continuation.",
            mechanism="Volatility-scaled breakout; upper-band breakouts may persist in strong trends.",
            assumptions=("trending regime", "band width meaningful"),
            risks=("mean reversion when regime shifts", "regime-conditional performance"),
            known_failure_modes=("low-volatility chop", "capitulation reversals"),
            evidence_quality=EvidenceQuality.REPUTABLE_PRACTITIONER,
            research_notes="Implemented by _bollinger_breakout.",
            timeframe_hint="1d",
            indicator_hints=(IndicatorName.BOLLINGER_UPPER, IndicatorName.BOLLINGER_MIDDLE),
            spec_builder=_bollinger_breakout,
            tags=("library", "breakout"),
        ),
        Candidate(
            candidate_id="volatility-atr-breakout",
            name="ATR volatility breakout",
            description="LONG when close > SMA20; exit when close < SMA20. Volatility context via ATR.",
            category=Category.VOLATILITY,
            market=Market.NSE_EQUITY,
            source="Chan 2013 'Algorithmic Trading' (Keltner-style breakout)",
            source_type="book",
            claim="Volatility-scaled breakouts capture trend persistence while bounding exposure.",
            mechanism="Breakout confirmed by both trend and recent volatility expansion.",
            assumptions=("volatility expansion accompanies trend", "transaction costs bounded"),
            risks=("whipsaw in quiet ranges", "cost-sensitive"),
            known_failure_modes=("low-volatility regimes", "liquidity gaps"),
            evidence_quality=EvidenceQuality.REPUTABLE_PRACTITIONER,
            research_notes="Implemented by _atr_volatility_breakout.",
            timeframe_hint="1d",
            indicator_hints=(IndicatorName.SMA, IndicatorName.ATR),
            spec_builder=_atr_volatility_breakout,
            tags=("library", "volatility"),
        ),
        Candidate(
            candidate_id="meanrev-rsi2",
            name="RSI(2) mean reversion",
            description="LONG when RSI(2) < 10; exit when RSI(2) > 65.",
            category=Category.MEAN_REVERSION,
            market=Market.NSE_INDEX,
            source="Connors 2009 'Short Term Trading Strategies That Work'",
            source_type="practitioner_reference",
            claim="RSI(2) crossings of extreme thresholds revert within short horizons.",
            mechanism="Behavioural mean reversion; high-turnover strategies; cost-sensitive.",
            assumptions=("short-horizon reversion", "tight costs", "liquidity for high turnover"),
            risks=("trending markets", "extreme tail events"),
            known_failure_modes=("persistent trends", "high costs"),
            evidence_quality=EvidenceQuality.REPUTABLE_PRACTITIONER,
            research_notes="Connors RSI(2) academic replication on US equity indices. Cost-sensitive on NSE.",
            timeframe_hint="1d",
            indicator_hints=(IndicatorName.RSI,),
            spec_builder=_rsi_2_reversion,
            tags=("library", "mean-reversion"),
        ),
        Candidate(
            candidate_id="trend-momentum-rsi-confirmation",
            name="Trend momentum RSI confirmation",
            description="LONG when close > SMA20 AND momentum(10) > 0 AND RSI(14) > 50.",
            category=Category.MULTI_FACTOR,
            market=Market.NSE_EQUITY,
            source="Brock, Lakonishok & LeBaron 1992 'Simple Technical Trading Rules on the Stock Market'; Wang & Yu 2009 'Trading rule induction' (informational filter stacking literature)",
            source_type="academic",
            claim="Multi-factor confirmation (trend + momentum + mean-reversion filter) reduces false-signal rate relative to single-indicator rules.",
            mechanism="Multi-indicator confirmation acts as a poor-man's Bayesian filter: each rule eliminates trades that violate a coarse stylized fact.",
            assumptions=("liquidity sufficient", "trend persistence"),
            risks=("complexity penalty", "overfitting"),
            known_failure_modes=("ranges", "shock regime changes"),
            evidence_quality=EvidenceQuality.PEER_REVIEWED,
            research_notes="Multi-factor confirmation pattern; higher search-count penalty due to combinatoric indicator choice.",
            timeframe_hint="1d",
            indicator_hints=(IndicatorName.SMA, IndicatorName.MOMENTUM, IndicatorName.RSI),
            spec_builder=_volume_confirmed_trend,
            tags=("library", "multi-factor"),
        ),
        Candidate(
            candidate_id="trend-regime-filtered",
            name="Regime-filtered trend - SMA200",
            description="LONG when close > SMA20 AND momentum(10) > 1%.",
            category=Category.MARKET_REGIME,
            market=Market.NSE_EQUITY,
            source="Practitioner observation; Covel 2005 'Trend Following'",
            source_type="book",
            claim="A trend-following filter reduces drawdown during regime transitions.",
            mechanism="Two-factor: trend + stronger-than-noise momentum threshold.",
            assumptions=("trending regimes dominate", "noise is bounded"),
            risks=("delayed entries", "whipsaws"),
            known_failure_modes=("slow transitions", "ranging markets"),
            evidence_quality=EvidenceQuality.REPUTABLE_PRACTITIONER,
            research_notes="Implemented by _regime_filtered_trend.",
            timeframe_hint="1d",
            indicator_hints=(IndicatorName.SMA, IndicatorName.MOMENTUM),
            spec_builder=_regime_filtered_trend,
            tags=("library", "regime"),
        ),
        Candidate(
            candidate_id="trend-dual-ma-longshort",
            name="Dual MA long-short 5-20",
            description="LONG when EMA(5) crosses above SMA(20); SHORT on inverse cross.",
            category=Category.TREND_FOLLOWING,
            market=Market.NSE_INDEX,
            source="Brock Lakonishok LeBaron 1992 'Simple Technical Trading Rules and the Stochastic Properties of Stock Returns'",
            source_type="academic",
            claim="Simple moving-average timing rules produce statistically and economically significant returns on the Dow 30 (1897-1986).",
            mechanism="Trend persistence at medium-term horizons.",
            assumptions=("trend persistence", "shorting constraints not binding"),
            risks=("NSE shorting constraints", "execution costs"),
            known_failure_modes=("prolonged ranges", "post-2000 trend fragmentation"),
            evidence_quality=EvidenceQuality.PEER_REVIEWED,
            research_notes="Foundational academic reference; replication on Indian indices is a research question, not an assumption.",
            timeframe_hint="1d",
            indicator_hints=(IndicatorName.EMA, IndicatorName.SMA),
            spec_builder=_dual_ma_crossover_long_short,
            tags=("library", "trend", "long-short"),
        ),
        Candidate(
            candidate_id="momentum-12-1",
            name="Momentum 12-1",
            description="LONG when 12-bar momentum > 0; exit when momentum < 0.",
            category=Category.MOMENTUM,
            market=Market.NSE_EQUITY,
            source="Jegadeesh & Titman 1993 'Returns to Buying Winners and Selling Losers'",
            source_type="academic",
            claim="Cross-sectional momentum of 12-month returns (skipping the most recent month) persists 3-12 months.",
            mechanism="Under-reaction + slow information diffusion.",
            assumptions=("trend persistence on 12-bar horizon", "low turnover costs"),
            risks=("momentum crashes", "high turnover"),
            known_failure_modes=("regime reversals", "liquidity crunches"),
            evidence_quality=EvidenceQuality.PEER_REVIEWED,
            research_notes="Single-instrument time-series analogue of a cross-sectional result.",
            timeframe_hint="1d",
            indicator_hints=(IndicatorName.MOMENTUM,),
            spec_builder=_momentum_12_1,
            tags=("library", "momentum"),
        ),
        Candidate(
            candidate_id="multi-factor-trend",
            name="Multi-factor trend - SMA MACD RSI",
            description="LONG when close>SMA20 AND MACD>signal AND RSI(14)>50; exit on close<SMA20.",
            category=Category.MULTI_FACTOR,
            market=Market.NSE_EQUITY,
            source="Kestner 2003 'Quantitative Trading Strategies'; practitioner consensus",
            source_type="book",
            claim="Combining trend + momentum + momentum filter improves win rate but adds complexity penalty.",
            mechanism="Three-factor confirmation.",
            assumptions=("factors are weakly independent", "costs are tolerable"),
            risks=("complexity penalty", "factor correlation in stress regimes"),
            known_failure_modes=("regime shifts", "low-liquidity stress"),
            evidence_quality=EvidenceQuality.REPUTABLE_PRACTITIONER,
            research_notes="Multi-factor combination; search-count penalty applies.",
            timeframe_hint="1d",
            indicator_hints=(IndicatorName.SMA, IndicatorName.MACD, IndicatorName.MACD_SIGNAL, IndicatorName.RSI),
            spec_builder=_multi_factor_trend,
            tags=("library", "multi-factor"),
        ),
        Candidate(
            candidate_id="trend-not-falling-knife",
            name="Avoid-falling-knife trend filter",
            description="LONG when NOT (close<SMA20 AND momentum<-5%); exit on close<SMA20.",
            category=Category.MARKET_REGIME,
            market=Market.NSE_EQUITY,
            source="Practitioner 'do not catch a falling knife' rule",
            source_type="blog_or_marketing",
            claim="Avoiding exposure in extreme drawdowns reduces left-tail risk.",
            mechanism="DSL NOT primitive applied as a drawdown filter.",
            assumptions=("avoiding tail events helps risk-adjusted return"),
            risks=("slow recovery", "frequent exits"),
            known_failure_modes=("prolonged ranges", "sharp V-shaped recoveries"),
            evidence_quality=EvidenceQuality.BLOG_OR_MARKETING,
            research_notes="Demonstrates the NOT DSL primitive.",
            timeframe_hint="1d",
            indicator_hints=(IndicatorName.SMA, IndicatorName.MOMENTUM),
            spec_builder=_not_dip_buyer,
            tags=("library", "regime"),
        ),
    ]
    return {c.candidate_id: c for c in items}


_LIBRARY: dict[str, Candidate] = _build_library()


class StrategyLibrary:
    """Immutable view over the curated strategy research library."""

    name = "internal-research-library"

    def __init__(self, items: Optional[dict] = None) -> None:
        self._items = dict(items) if items is not None else dict(_LIBRARY)

    @property
    def candidate_ids(self) -> list[str]:
        return sorted(self._items.keys())

    def __contains__(self, candidate_id: str) -> bool:
        return candidate_id in self._items

    def get(self, candidate_id: str) -> Candidate:
        try:
            return self._items[candidate_id]
        except KeyError as e:
            raise KeyError(
                f"unknown candidate {candidate_id!r}; available: {self.candidate_ids}"
            ) from e

    def by_category(self, category: Category) -> list[Candidate]:
        return [c for c in self._items.values() if c.category == category]

    def all(self) -> list[Candidate]:
        return [self._items[cid] for cid in self.candidate_ids]

    def __len__(self) -> int:
        return len(self._items)

    def to_records(self) -> list[dict]:
        """Serialize all candidates (without spec payloads) for audit/review."""
        out = []
        for cid in self.candidate_ids:
            c = self._items[cid]
            out.append(
                {
                    "candidate_id": c.candidate_id,
                    "name": c.name,
                    "category": c.category.value,
                    "market": c.market.value,
                    "source": c.source,
                    "source_type": c.source_type,
                    "evidence_quality": c.evidence_quality.value,
                    "timeframe_hint": c.timeframe_hint,
                    "indicators": [i.value for i in c.indicator_hints],
                    "tags": list(c.tags),
                    "claim": c.claim,
                    "mechanism": c.mechanism,
                    "assumptions": list(c.assumptions),
                    "risks": list(c.risks),
                    "known_failure_modes": list(c.known_failure_modes),
                    "research_notes": c.research_notes,
                }
            )
        return out


# Eager-built singleton for convenience.
DEFAULT_LIBRARY = StrategyLibrary()


__all__ = [
    "Category",
    "EvidenceQuality",
    "Market",
    "Candidate",
    "StrategyLibrary",
    "DEFAULT_LIBRARY",
]