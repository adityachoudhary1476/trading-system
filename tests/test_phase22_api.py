"""Phase 22 — Adaptive Multi-Strategy Market Intelligence tests.

Covers:

  * Phase 22 module: strategy specs, regime classification, adaptive selector,
    regime-aware scoring, compatibility matrix.
  * Phase 22 API routes: GET /strategies, GET /regime (with + without market
    data provider), GET /allocation (with + without market data provider),
    route registration.

All test data is deterministic synthetic — NOT live market data.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine

from trading_system.paper.control import PaperTradingControlCenter
from trading_system.paper_api.router import PaperAPIRouter
from trading_system.research.phase22 import (
    AdaptiveStrategySelector,
    Phase22Regime,
    RegimeAwareScorer,
    RegimeAwareScore,
    RegimeClassifier,
    StrategyCategory,
    StrategyWeight,
    build_phase22_strategy_specs,
    regime_compatibility,
)
from trading_system.research.strategy_lab.spec import (
    StrategySpec,
    IndicatorDef,
    PositionSizing,
    RiskParams,
    field_operand,
    indicator_operand,
    make_condition,
)
from trading_system.research.strategy_lab.dsl import (
    Comparison,
    ComparisonOp,
    indicator_ref,
    const,
)
from trading_system.research.strategy_intelligence import (
    EvidenceFreshnessConfig,
    EvidenceRequirement,
    StrategyIntelligence,
)
from trading_system.research.strategy_registry import StrategyRegistry
from trading_system.research.evidence import (
    EvidenceStore,
    strategy_identity,
)

# --------------------------------------------------------------------------- #
# Synthetic market data helpers (reused pattern from test_phase21_api.py)
# --------------------------------------------------------------------------- #
def _uptrend(n=60, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    close = 100 + np.cumsum(rng.normal(0.5, 0.8, n))
    return pd.DataFrame({
        "open": close + 0.1, "high": close + 1.0, "low": close - 1.0,
        "close": close, "volume": rng.integers(100, 1000, n).astype(float),
    }, index=idx)


def _downtrend(n=60, seed=2):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    close = 100 - np.cumsum(rng.normal(0.5, 0.8, n))
    return pd.DataFrame({
        "open": close - 0.1, "high": close + 1.0, "low": close - 1.0,
        "close": close, "volume": rng.integers(100, 1000, n).astype(float),
    }, index=idx)


def _range(n=60, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    closes = 100 + 5 * np.sin(np.arange(n) * 2 * np.pi / 10) + rng.normal(0, 0.5, n)
    return pd.DataFrame({
        "open": closes + 0.1, "high": closes + 0.5, "low": closes - 0.5,
        "close": closes, "volume": rng.integers(100, 1000, n).astype(float),
    }, index=idx)


def _market_data(symbol="NSE:SBIN", timeframe="1d"):
    """Return a deterministic market-data provider for the control center."""
    return lambda s, tf: _uptrend(60) if (s == symbol and tf == timeframe) else None


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def engine():
    return create_engine("sqlite://")


@pytest.fixture()
def center_with_md(engine):
    """Control center with a market data provider that returns synthetic data."""
    store = None
    from trading_system.research.evidence import EvidenceStore
    store = EvidenceStore(engine)
    registry = StrategyRegistry(store)
    intelligence = StrategyIntelligence(registry)
    center = PaperTradingControlCenter.from_engine(
        engine,
        market_data_provider=_market_data(),
    )
    return center


@pytest.fixture()
def center_no_md(engine):
    """Control center without a market data provider."""
    return PaperTradingControlCenter.from_engine(engine)


@pytest.fixture()
def router_with_md(center_with_md):
    return PaperAPIRouter(center_with_md)


@pytest.fixture()
def router_no_md(center_no_md):
    return PaperAPIRouter(center_no_md)


# --------------------------------------------------------------------------- #
# Phase 22 module tests
# --------------------------------------------------------------------------- #
class TestStrategySpecs:
    def test_has_five_strategies(self):
        specs = build_phase22_strategy_specs()
        assert len(specs) == 5
        assert "trend_following_ema" in specs
        assert "momentum_rsi" in specs
        assert "breakout_nbar" in specs
        assert "mean_reversion_rsi" in specs
        assert "vwap_mean_rev" in specs

    def test_all_market_data_synthetic(self):
        """No strategy may claim live market data as its source."""
        specs = build_phase22_strategy_specs()
        for name, spec in specs.items():
            assert spec.generated_by == "phase22"
            assert spec.symbol == "NSE:SBIN"
            assert spec.timeframe == "1d"

    def test_strategy_id_is_deterministic(self):
        specs = build_phase22_strategy_specs()
        sid = strategy_identity(specs["trend_following_ema"])
        assert isinstance(sid, str) and len(sid) == 64
        # Idempotency: same spec → same id
        assert strategy_identity(build_phase22_strategy_specs()["trend_following_ema"]) == sid

    def test_trend_following_has_ema_indicators(self):
        spec = build_phase22_strategy_specs()["trend_following_ema"]
        indicator_names = [d.name for d in spec.indicators]
        assert "ema" in indicator_names
        assert spec.risk.allow_short is True
        assert spec.entry is not None

    def test_breakout_does_not_allow_short(self):
        spec = build_phase22_strategy_specs()["breakout_nbar"]
        assert spec.risk.allow_short is False

    def test_momentum_has_rsi_entry(self):
        spec = build_phase22_strategy_specs()["momentum_rsi"]
        assert spec.entry is not None
        assert spec.entry.op == ComparisonOp.GT
        assert spec.exit is not None


class TestPhase22Regime:
    def test_all_regimes_present(self):
        expected = {
            "trending_up", "trending_down", "range_bound",
            "high_volatility", "low_volatility",
            "volatility_expansion", "volatility_contraction", "unknown",
        }
        actual = {r.value for r in Phase22Regime}
        assert actual == expected

    def test_from_regime_enum_mapping(self):
        from trading_system.research.intelligence import RegimeEnum
        assert Phase22Regime.from_regime_enum(RegimeEnum.TRENDING_UP) == Phase22Regime.TRENDING_UP
        assert Phase22Regime.from_regime_enum(RegimeEnum.TRENDING_DOWN) == Phase22Regime.TRENDING_DOWN
        assert Phase22Regime.from_regime_enum(RegimeEnum.RANGE_BOUND) == Phase22Regime.RANGE_BOUND
        assert Phase22Regime.from_regime_enum(RegimeEnum.HIGH_VOLATILITY) == Phase22Regime.HIGH_VOLATILITY
        assert Phase22Regime.from_regime_enum(RegimeEnum.LOW_VOLATILITY) == Phase22Regime.LOW_VOLATILITY
        assert Phase22Regime.from_regime_enum(RegimeEnum.UNKNOWN) == Phase22Regime.UNKNOWN


class TestRegimeClassifier:
    def test_classify_empty_dataframe(self):
        clf = RegimeClassifier()
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        result = clf.classify(df)
        assert result.regime == Phase22Regime.UNKNOWN
        assert result.confidence == 0.0
        assert "empty_dataframe" in result.features

    def test_classify_uptrend(self):
        clf = RegimeClassifier()
        result = clf.classify(_uptrend(60))
        assert result.regime in (Phase22Regime.TRENDING_UP, Phase22Regime.VOLATILITY_EXPANSION)
        assert 0.0 <= result.confidence <= 0.95

    def test_classify_downtrend(self):
        clf = RegimeClassifier()
        result = clf.classify(_downtrend(60))
        assert result.regime in (Phase22Regime.TRENDING_DOWN, Phase22Regime.HIGH_VOLATILITY)
        assert 0.0 <= result.confidence <= 0.95

    def test_classify_range(self):
        clf = RegimeClassifier()
        result = clf.classify(_range(60))
        # Range data may classify as range-bound or volatility-dependent
        assert 0.0 <= result.confidence <= 0.95

    def test_classify_deduplicates_index(self):
        clf = RegimeClassifier()
        df = pd.concat([_uptrend(40).iloc[:30], _uptrend(40).iloc[:30]])  # duplicate timestamps
        # Should not raise — internally deduplicates
        result = clf.classify(df)
        assert result.regime is not None


class TestRegimeCompatibility:
    def test_trend_following_compatible_uptrend(self):
        assert regime_compatibility(StrategyCategory.TREND_FOLLOWING, Phase22Regime.TRENDING_UP) == 1.0
        assert regime_compatibility(StrategyCategory.TREND_FOLLOWING, Phase22Regime.TRENDING_DOWN) == 1.0

    def test_trend_following_incompatible_range(self):
        assert regime_compatibility(StrategyCategory.TREND_FOLLOWING, Phase22Regime.RANGE_BOUND) == 0.0

    def test_mean_reversion_strong_range(self):
        assert regime_compatibility(StrategyCategory.MEAN_REVERSION, Phase22Regime.RANGE_BOUND) == 1.0

    def test_unknown_regime_is_zero(self):
        for cat in StrategyCategory:
            assert regime_compatibility(cat, Phase22Regime.UNKNOWN) == 0.0


class TestAdaptiveStrategySelector:
    @pytest.fixture()
    def selector(self, engine):
        from trading_system.research.evidence import EvidenceStore
        store = EvidenceStore(engine)
        registry = StrategyRegistry(store)
        intelligence = StrategyIntelligence(registry)
        specs = build_phase22_strategy_specs()
        for spec in specs.values():
            registry.register_strategy(spec)
        return AdaptiveStrategySelector(intelligence)

    def test_list_strategies(self, selector):
        names = selector.list_strategies()
        assert len(names) == 5
        assert "trend_following_ema" in names

    def test_categorize(self, selector):
        assert selector._categorize("trend_following_ema") == StrategyCategory.TREND_FOLLOWING
        assert selector._categorize("momentum_rsi") == StrategyCategory.MOMENTUM
        assert selector._categorize("breakout_nbar") == StrategyCategory.BREAKOUT
        assert selector._categorize("mean_reversion_rsi") == StrategyCategory.MEAN_REVERSION
        assert selector._categorize("vwap_mean_rev") == StrategyCategory.MEAN_REVERSION

    def test_allocate_uptrend(self, selector):
        result = selector.allocate(_uptrend(60))
        assert result.regime != Phase22Regime.UNKNOWN
        assert result.total_strategies_available == 5
        assert len(result.selected_strategies) <= 3  # max_strategies default
        # Weights should sum to ~1.0
        total_w = sum(sw.weight for sw in result.selected_strategies)
        assert abs(total_w - 1.0) < 0.01

    def test_allocate_empty_dataframe(self, selector):
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        result = selector.allocate(df)
        assert result.regime == Phase22Regime.UNKNOWN
        assert len(result.selected_strategies) == 0


class TestRegimeAwareScorer:
    def test_score_uptrend(self, engine):
        store = EvidenceStore(engine)
        registry = StrategyRegistry(store)
        intelligence = StrategyIntelligence(registry)
        specs = build_phase22_strategy_specs()
        for spec in specs.values():
            registry.register_strategy(spec)
        selector = AdaptiveStrategySelector(intelligence)
        scorer = RegimeAwareScorer(selector)
        score = scorer.score("trend_following_ema", _uptrend(60))
        assert isinstance(score, RegimeAwareScore)
        assert score.strategy_name == "trend_following_ema"
        assert 0.0 <= score.aggregate_score <= 1.0
        assert 0.0 <= score.regime_compatibility <= 1.0


# --------------------------------------------------------------------------- #
# Phase 22 API route tests
# --------------------------------------------------------------------------- #
class TestPhase22Routes:
    def test_strategies_route_returns_all_specs(self, router_with_md):
        env = router_with_md.dispatch("GET", "/strategies")
        assert env.status == 200
        body = env.body
        assert isinstance(body, list)
        assert len(body) == 5
        names = [s["name"] for s in body]
        assert "trend_following_ema" in names
        assert "momentum_rsi" in names
        assert "breakout_nbar" in names
        assert "mean_reversion_rsi" in names
        assert "vwap_mean_rev" in names
        for spec in body:
            assert "strategy_id" in spec
            assert "spec_name" in spec
            assert "description" in spec
            assert "symbol" in spec
            assert "timeframe" in spec
            assert "indicators" in spec
            assert "entry_condition" in spec
            assert "allow_short" in spec
            assert "generated_by" in spec

    def test_regime_route_no_market_data(self, router_no_md):
        env = router_no_md.dispatch("GET", "/regime?symbol=NSE:SBIN&timeframe=1d")
        assert env.status == 400
        assert env.body["error"]["code"] == "bad_request"
        assert "no market data provider" in env.body["error"]["message"]

    def test_regime_route_with_market_data(self, router_with_md):
        env = router_with_md.dispatch("GET", "/regime?symbol=NSE:SBIN&timeframe=1d")
        assert env.status == 200
        body = env.body
        assert body["regime"] in [r.value for r in Phase22Regime]
        assert 0.0 <= body["confidence"] <= 1.0
        assert "features" in body
        assert "warnings" in body
        assert "regime_at_ms" in body

    def test_allocation_route_no_market_data(self, router_no_md):
        env = router_no_md.dispatch("GET", "/allocation?symbol=NSE:SBIN&timeframe=1d")
        assert env.status == 400
        assert env.body["error"]["code"] == "bad_request"
        assert "no market data provider" in env.body["error"]["message"]

    def test_allocation_route_with_market_data(self, router_with_md):
        env = router_with_md.dispatch("GET", "/allocation?symbol=NSE:SBIN&timeframe=1d")
        assert env.status == 200
        body = env.body
        assert body["regime"] in [r.value for r in Phase22Regime]
        assert "regime_confidence" in body
        assert "regime_fit" in body
        assert "total_strategies_available" in body
        assert body["total_strategies_available"] == 5
        assert "selected_strategies" in body
        assert isinstance(body["selected_strategies"], list)
        for sw in body["selected_strategies"]:
            assert "strategy_name" in sw
            assert "category" in sw
            assert "regime_compatibility" in sw
            assert "research_score" in sw
            assert "weight" in sw

    def test_strategies_route_method_not_allowed(self, router_with_md):
        env = router_with_md.dispatch("POST", "/strategies")
        assert env.status == 405

    def test_regime_route_validates_limit_param(self, router_no_md):
        env = router_no_md.dispatch("GET", "/regime?symbol=NSE:SBIN&timeframe=1d&limit=abc")
        assert env.status == 400
        assert "must be an integer" in env.body["error"]["message"]

    def test_regime_route_rejects_limit_below_minimum(self, router_no_md):
        env = router_no_md.dispatch("GET", "/regime?symbol=NSE:SBIN&timeframe=1d&limit=5")
        assert env.status == 400
        assert "must be in" in env.body["error"]["message"]

    def test_route_registration(self, router_with_md):
        routes = router_with_md.routes()
        patterns = [p for p, _ in routes]
        assert "^/strategies$" in patterns
        assert "^/regime$" in patterns
        assert "^/allocation$" in patterns
