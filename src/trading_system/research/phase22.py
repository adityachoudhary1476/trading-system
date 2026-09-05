"""Phase 22 - Adaptive Multi-Strategy Market Intelligence.

Extends the existing Phase 17 intelligence engine with:
* Phase 22C strategy implementations (Trend Following, Momentum, Breakout,
  Mean Reversion, VWAP-based) as declarative StrategySpec objects.
* Phase 22D enhanced regime classification (extends RegimeEnum with
  volatility expansion/contraction).
* Phase 22F strategy/regime compatibility configuration.
* Phase 22G regime-aware scoring (extends Phase 17 research scoring).
* Phase 22L Adaptive Strategy Selector (deterministic allocation).

All components are deterministic, causal, auditable, and paper-only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

from .intelligence import (
    FeatureEngine, TechnicalFeatures, MarketRegime, RegimeEnum,
    TrendEnum, VolRegime, classify_regime,
)
from .strategy_lab.spec import (
    StrategySpec, IndicatorDef, PositionSizing, RiskParams,
)
from .strategy_lab.interpreter import SpecStrategy, build_strategy
from .strategy_lab.dsl import (
    Comparison, ComparisonOp, indicator_ref, const,
)
from .strategy_intelligence import (
    StrategyIntelligence, EvidenceFreshnessConfig, ComparisonConfig,
)
from .strategy_registry import StrategyRegistry, strategy_identity


class Phase22Regime(str, Enum):
    """Finer-grained regimes used by the Phase 22 adaptive selector."""
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGE_BOUND = "range_bound"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    VOLATILITY_EXPANSION = "volatility_expansion"
    VOLATILITY_CONTRACTION = "volatility_contraction"
    UNKNOWN = "unknown"

    @classmethod
    def from_regime_enum(cls, r: RegimeEnum) -> "Phase22Regime":
        mapping = {
            RegimeEnum.TRENDING_UP: cls.TRENDING_UP,
            RegimeEnum.TRENDING_DOWN: cls.TRENDING_DOWN,
            RegimeEnum.RANGE_BOUND: cls.RANGE_BOUND,
            RegimeEnum.HIGH_VOLATILITY: cls.HIGH_VOLATILITY,
            RegimeEnum.LOW_VOLATILITY: cls.LOW_VOLATILITY,
            RegimeEnum.UNKNOWN: cls.UNKNOWN,
        }
        return mapping.get(r, cls.UNKNOWN)


@dataclass
class Phase22RegimeClassification:
    """Result of Phase 22 regime classification."""
    regime: Phase22Regime
    confidence: float
    features: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    regime_at_ms: int = 0


class StrategyCategory(str, Enum):
    """Phase 22 strategy categories for regime compatibility."""
    TREND_FOLLOWING = "trend_following"
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    BREAKOUT = "breakout"
    VOLATILITY = "volatility"
    MULTI_STRATEGY = "multi_strategy"


# Strategy -> regime compatibility matrix (1.0 = fully compatible, 0.0 = incompatible)
_STRATEGY_REGIME_COMPAT = {
    "trend_following": {
        Phase22Regime.TRENDING_UP: 1.0, Phase22Regime.TRENDING_DOWN: 1.0,
        Phase22Regime.RANGE_BOUND: 0.0, Phase22Regime.HIGH_VOLATILITY: 0.4,
        Phase22Regime.LOW_VOLATILITY: 0.4, Phase22Regime.VOLATILITY_EXPANSION: 0.6,
        Phase22Regime.VOLATILITY_CONTRACTION: 0.3, Phase22Regime.UNKNOWN: 0.0,
    },
    "momentum": {
        Phase22Regime.TRENDING_UP: 0.9, Phase22Regime.TRENDING_DOWN: 0.3,
        Phase22Regime.RANGE_BOUND: 0.2, Phase22Regime.HIGH_VOLATILITY: 0.6,
        Phase22Regime.LOW_VOLATILITY: 0.7, Phase22Regime.VOLATILITY_EXPANSION: 0.8,
        Phase22Regime.VOLATILITY_CONTRACTION: 0.4, Phase22Regime.UNKNOWN: 0.0,
    },
    "mean_reversion": {
        Phase22Regime.TRENDING_UP: 0.1, Phase22Regime.TRENDING_DOWN: 0.1,
        Phase22Regime.RANGE_BOUND: 1.0, Phase22Regime.HIGH_VOLATILITY: 0.7,
        Phase22Regime.LOW_VOLATILITY: 0.9, Phase22Regime.VOLATILITY_EXPANSION: 0.3,
        Phase22Regime.VOLATILITY_CONTRACTION: 0.8, Phase22Regime.UNKNOWN: 0.0,
    },
    "breakout": {
        Phase22Regime.TRENDING_UP: 0.9, Phase22Regime.TRENDING_DOWN: 0.6,
        Phase22Regime.RANGE_BOUND: 0.5, Phase22Regime.HIGH_VOLATILITY: 0.8,
        Phase22Regime.LOW_VOLATILITY: 0.4, Phase22Regime.VOLATILITY_EXPANSION: 0.9,
        Phase22Regime.VOLATILITY_CONTRACTION: 0.6, Phase22Regime.UNKNOWN: 0.0,
    },
    "volatility": {
        Phase22Regime.TRENDING_UP: 0.3, Phase22Regime.TRENDING_DOWN: 0.3,
        Phase22Regime.RANGE_BOUND: 0.4, Phase22Regime.HIGH_VOLATILITY: 1.0,
        Phase22Regime.LOW_VOLATILITY: 0.2, Phase22Regime.VOLATILITY_EXPANSION: 1.0,
        Phase22Regime.VOLATILITY_CONTRACTION: 0.2, Phase22Regime.UNKNOWN: 0.0,
    },
}


def regime_compatibility(category: StrategyCategory, regime: Phase22Regime) -> float:
    """Return compatibility score [0, 1] of a strategy category in a regime."""
    return _STRATEGY_REGIME_COMPAT.get(category.value, {}).get(regime, 0.0)


def build_phase22_strategy_specs() -> dict[str, StrategySpec]:
    """Build all Phase 22 strategy specs (deterministic)."""
    specs: dict[str, StrategySpec] = {}

    # Trend Following - EMA fast/slow cross
    specs["trend_following_ema"] = StrategySpec(
        name="Phase22_TrendFollowing_EMA",
        description="Trend following via EMA fast/slow cross. Long on bullish cross, short on bearish.",
        symbol="NSE:SBIN", timeframe="1d",
        indicators=[
            IndicatorDef(name="ema", params={"window": 9}),
            IndicatorDef(name="ema", params={"window": 21}),
            IndicatorDef(name="atr", params={"window": 14}),
        ],
        entry=Comparison(
            op=ComparisonOp.CROSSES_ABOVE,
            left=indicator_ref("ema_9"), right=indicator_ref("ema_21"),
        ),
        entry_short=Comparison(
            op=ComparisonOp.CROSSES_BELOW,
            left=indicator_ref("ema_9"), right=indicator_ref("ema_21"),
        ),
        exit=None, allow_long=True,
        position_sizing=PositionSizing(max_allocation_pct=0.30),
        risk=RiskParams(max_loss_per_trade_pct=0.01, allow_short=True),
        generated_by="phase22",
    )

    # Momentum - RSI momentum
    specs["momentum_rsi"] = StrategySpec(
        name="Phase22_Momentum_RSI",
        description="Momentum strategy using RSI for entry/exit signals.",
        symbol="NSE:SBIN", timeframe="1d",
        indicators=[
            IndicatorDef(name="rsi", params={"window": 14}),
            IndicatorDef(name="ema", params={"window": 50}),
        ],
        entry=Comparison(
            op=ComparisonOp.GT,
            left=indicator_ref("rsi_14"), right=const(70.0),
        ),
        entry_short=Comparison(
            op=ComparisonOp.LT,
            left=indicator_ref("rsi_14"), right=const(30.0),
        ),
        exit=Comparison(
            op=ComparisonOp.GT,
            left=indicator_ref("rsi_14"), right=const(50.0),
        ),
        allow_long=True,
        position_sizing=PositionSizing(max_allocation_pct=0.25),
        risk=RiskParams(max_loss_per_trade_pct=0.015, allow_short=True),
        generated_by="phase22",
    )

    # Breakout - momentum signal
    specs["breakout_nbar"] = StrategySpec(
        name="Phase22_Breakout_NBar",
        description="Breakout strategy: long on momentum breakout signal.",
        symbol="NSE:SBIN", timeframe="1d",
        indicators=[
            IndicatorDef(name="sma", params={"window": 20}),
            IndicatorDef(name="atr", params={"window": 14}),
            IndicatorDef(name="momentum", params={"window": 10}),
        ],
        entry=Comparison(
            op=ComparisonOp.GT,
            left=indicator_ref("momentum_10"), right=const(1.0),
        ),
        entry_short=None,
        exit=Comparison(
            op=ComparisonOp.LT,
            left=indicator_ref("momentum_10"), right=const(0.0),
        ),
        allow_long=True,
        position_sizing=PositionSizing(max_allocation_pct=0.20),
        risk=RiskParams(max_loss_per_trade_pct=0.01, allow_short=False),
        generated_by="phase22",
    )

    # Mean Reversion - RSI oversold bounce
    specs["mean_reversion_rsi"] = StrategySpec(
        name="Phase22_MeanReversion_RSI",
        description="Mean reversion strategy: long on RSI oversold, short on overbought.",
        symbol="NSE:SBIN", timeframe="1d",
        indicators=[
            IndicatorDef(name="rsi", params={"window": 14}),
            IndicatorDef(name="sma", params={"window": 20}),
        ],
        entry=Comparison(
            op=ComparisonOp.LT,
            left=indicator_ref("rsi_14"), right=const(30.0),
        ),
        entry_short=Comparison(
            op=ComparisonOp.GT,
            left=indicator_ref("rsi_14"), right=const(70.0),
        ),
        exit=Comparison(
            op=ComparisonOp.GT,
            left=indicator_ref("rsi_14"), right=const(50.0),
        ),
        allow_long=True,
        position_sizing=PositionSizing(max_allocation_pct=0.25),
        risk=RiskParams(max_loss_per_trade_pct=0.01, allow_short=True),
        generated_by="phase22",
    )

    # VWAP-based mean reversion strategy
    specs["vwap_mean_rev"] = StrategySpec(
        name="Phase22_VWAP_MeanReversion",
        description="Mean reversion around VWAP proxy: long on RSI>30, short on RSI<70.",
        symbol="NSE:SBIN", timeframe="1d",
        indicators=[
            IndicatorDef(name="sma", params={"window": 20}),
            IndicatorDef(name="rsi", params={"window": 14}),
        ],
        entry=Comparison(
            op=ComparisonOp.GT,
            left=indicator_ref("rsi_14"), right=const(30.0),
        ),
        entry_short=Comparison(
            op=ComparisonOp.LT,
            left=indicator_ref("rsi_14"), right=const(70.0),
        ),
        exit=Comparison(
            op=ComparisonOp.GT,
            left=indicator_ref("rsi_14"), right=const(50.0),
        ),
        allow_long=True,
        position_sizing=PositionSizing(max_allocation_pct=0.20),
        risk=RiskParams(max_loss_per_trade_pct=0.008, allow_short=True),
        generated_by="phase22",
    )

    return specs


@dataclass
class StrategyWeight:
    """Weight assigned to a strategy in a given regime."""
    strategy_name: str
    category: str
    regime_compatibility: float
    research_score: Optional[float]
    weight: float


@dataclass
class AllocationResult:
    """Result of the adaptive strategy allocation."""
    regime: Phase22Regime
    regime_confidence: float
    selected_strategies: list[StrategyWeight]
    timestamp_ms: int
    regime_fit: float
    total_strategies_available: int


class RegimeClassifier:
    """Phase 22 regime classifier.

    Wraps classify_regime from intelligence.py and adds volatility
    expansion/contraction detection using rolling ATR.
    """
    def __init__(self, atr_window: int = 20, vol_ratio_threshold: float = 1.5) -> None:
        self.atr_window = atr_window
        self.vol_ratio_threshold = vol_ratio_threshold

    def classify(self, df: pd.DataFrame) -> Phase22RegimeClassification:
        if df is None or len(df) == 0:
            return Phase22RegimeClassification(
                regime=Phase22Regime.UNKNOWN, confidence=0.0,
                features=["empty_dataframe"], regime_at_ms=_utc_now_ms(),
            )
        work = df.sort_index()
        if work.index.has_duplicates:
            work = work[~work.index.duplicated(keep="first")]
        engine = FeatureEngine(lookback=60)
        feats = engine.compute(work)
        base = classify_regime(feats)
        base_regime = Phase22Regime.from_regime_enum(base.regime)
        regime = self._augment(work, feats, base_regime)
        confidence = base.confidence
        if feats.data_points >= 120:
            confidence = min(confidence + 0.1, 0.95)
        return Phase22RegimeClassification(
            regime=regime, confidence=confidence,
            features=list(base.supporting_features), warnings=list(base.warnings),
            regime_at_ms=_utc_now_ms(),
        )

    def _augment(self, df: pd.DataFrame, feats: TechnicalFeatures, base: Phase22Regime) -> Phase22Regime:
        if feats.atr_14 is None or feats.data_points < self.atr_window + 4:
            return base
        from ..indicators import atr
        close = df["close"]
        atr_series = atr(df["high"], df["low"], close, 14)
        dropna = atr_series.dropna()
        if len(dropna) < self.atr_window:
            return base
        tail = dropna.tail(self.atr_window)
        med = float(tail.median())
        current = float(tail.iloc[-1])
        if med > 0:
            vol_ratio = current / med
            if vol_ratio >= self.vol_ratio_threshold:
                if base == Phase22Regime.LOW_VOLATILITY:
                    return Phase22Regime.VOLATILITY_EXPANSION
            elif vol_ratio <= (1.0 / self.vol_ratio_threshold):
                if base == Phase22Regime.HIGH_VOLATILITY:
                    return Phase22Regime.VOLATILITY_CONTRACTION
        return base


class AdaptiveStrategySelector:
    """Phase 22L - Deterministic adaptive strategy selector.

    Given market data and the Phase 17 research intelligence, determines which
    Phase 22 strategies to allocate capital to and with what weight, based on
    regime compatibility and research quality scores.
    """
    def __init__(
        self,
        intelligence: StrategyIntelligence,
        min_confidence: float = 0.5,
        max_strategies: int = 3,
        weight_floor: float = 0.05,
    ) -> None:
        self.intelligence = intelligence
        self.min_confidence = min_confidence
        self.max_strategies = max_strategies
        self.weight_floor = weight_floor
        self.regime_classifier = RegimeClassifier()
        self._specs = build_phase22_strategy_specs()

    def allocate(self, df: pd.DataFrame) -> AllocationResult:
        """Compute adaptive allocation for the current market state."""
        regime_cls = self.regime_classifier.classify(df)
        if regime_cls.regime == Phase22Regime.UNKNOWN or regime_cls.confidence < self.min_confidence:
            return AllocationResult(
                regime=Phase22Regime.UNKNOWN, regime_confidence=0.0,
                selected_strategies=[], timestamp_ms=_utc_now_ms(),
                regime_fit=0.0, total_strategies_available=len(self._specs),
            )
        candidates: list[tuple[str, StrategyCategory, float, Optional[float]]] = []
        for name in self._specs:
            category = self._categorize(name)
            compat = regime_compatibility(category, regime_cls.regime)
            research_score = self._get_research_score(name)
            if compat > 0.0:
                candidates.append((name, category, compat, research_score))
        weights = self._compute_weights(candidates)
        weights.sort(key=lambda w: w.weight, reverse=True)
        selected = weights[:self.max_strategies]
        total_w = sum(w.weight for w in selected)
        if total_w > 0:
            for w in selected:
                w.weight = w.weight / total_w
        regime_fit = sum(w.regime_compatibility * w.weight for w in selected) if selected else 0.0
        return AllocationResult(
            regime=regime_cls.regime, regime_confidence=regime_cls.confidence,
            selected_strategies=selected, timestamp_ms=_utc_now_ms(),
            regime_fit=regime_fit, total_strategies_available=len(self._specs),
        )

    def _categorize(self, name: str) -> StrategyCategory:
        """Map strategy spec name to category based on naming conventions."""
        name_lower = name.lower()
        if "trend" in name_lower:
            return StrategyCategory.TREND_FOLLOWING
        if "momentum" in name_lower:
            return StrategyCategory.MOMENTUM
        if "mean" in name_lower or "reversion" in name_lower or "vwap" in name_lower:
            return StrategyCategory.MEAN_REVERSION
        if "breakout" in name_lower:
            return StrategyCategory.BREAKOUT
        if "vol" in name_lower:
            return StrategyCategory.VOLATILITY
        return StrategyCategory.MULTI_STRATEGY

    def _get_research_score(self, name: str) -> Optional[float]:
        """Get the research score for a strategy from Phase 17 intelligence."""
        spec = self._specs.get(name)
        if spec is None:
            return None
        sid = strategy_identity(spec)
        try:
            strat = self.intelligence.registry.get_strategy(sid)
            if strat is None:
                return None
            report = self.intelligence.compare_strategies([sid], EvidenceFreshnessConfig())
            if report.strategies:
                return report.strategies[0].research_score
            return None
        except (KeyError, ValueError):
            return None

    def _compute_weights(self, candidates: list[tuple[str, StrategyCategory, float, Optional[float]]]) -> list[StrategyWeight]:
        """Compute weights from regime compatibility x research score."""
        raw_scores: list[float] = []
        for name, cat, compat, research in candidates:
            if research is not None:
                score = 0.6 * compat + 0.4 * float(np.clip(research, 0.0, 1.0))
            else:
                score = compat
            raw_scores.append(max(score, 0.05))
        total = sum(raw_scores)
        if total <= 0:
            n = len(candidates)
            weights = [1.0 / n if n > 0 else 0.0 for _ in candidates]
        else:
            weights = [s / total for s in raw_scores]
        return [
            StrategyWeight(
                strategy_name=name, category=cat.value,
                regime_compatibility=compat, research_score=research, weight=w,
            )
            for (name, cat, compat, research), w in zip(candidates, weights)
        ]

    def list_strategies(self) -> list[str]:
        return list(self._specs.keys())

    def get_strategy_spec(self, name: str) -> Optional[StrategySpec]:
        return self._specs.get(name)

    def build_interpreter_strategy(self, name: str, symbol: str, timeframe: str) -> Optional[SpecStrategy]:
        """Compile a Phase 22 spec into an interpretable SpecStrategy."""
        spec = self._specs.get(name)
        if spec is None:
            return None
        spec_dict = spec.model_dump()
        spec_dict["symbol"] = symbol
        spec_dict["timeframe"] = timeframe
        new_spec = StrategySpec(**spec_dict)
        return build_strategy(new_spec)


@dataclass
class RegimeAwareScore:
    """Regime-aware score for a strategy given current market data."""
    strategy_name: str
    category: str
    regime: Phase22Regime
    regime_confidence: float
    regime_compatibility: float
    research_score: Optional[float]
    aggregate_score: float
    timestamp_ms: int


class RegimeAwareScorer:
    """Phase 22G - Extends Phase 17 scoring with regime fit."""
    def __init__(self, selector: AdaptiveStrategySelector) -> None:
        self.selector = selector
        self.regime_classifier = selector.regime_classifier

    def score(self, strategy_name: str, df: pd.DataFrame) -> RegimeAwareScore:
        """Compute a regime-aware score for a strategy given market data."""
        regime_cls = self.regime_classifier.classify(df)
        category = self.selector._categorize(strategy_name)
        compat = regime_compatibility(category, regime_cls.regime)
        research_score = self.selector._get_research_score(strategy_name)
        if research_score is not None:
            aggregate = 0.6 * compat + 0.4 * float(np.clip(research_score, 0.0, 1.0))
        else:
            aggregate = compat
        return RegimeAwareScore(
            strategy_name=strategy_name, category=category.value,
            regime=regime_cls.regime, regime_confidence=regime_cls.confidence,
            regime_compatibility=compat, research_score=research_score,
            aggregate_score=aggregate, timestamp_ms=_utc_now_ms(),
        )


def _utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)