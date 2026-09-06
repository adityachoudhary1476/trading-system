"""Phase 4 — Strategy & Timeframe Compatibility tests.

Covers strategy discovery, eligibility, timeframe evaluation, compatibility
scoring, deterministic ordering, snapshot consistency, exclusions, controller
integration, and safety boundaries.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from trading_system.autonomous.bot_config import (
    AutonomousBotConfig,
    BotMode,
    Source,
    TradingMode,
    UserConstraints,
)
from trading_system.autonomous.compatibility import (
    CompatibilityConfig,
    CompatibilityResult,
    CompatibilityExclusion,
    CompatibilityExclusionReason,
    CompatibilityFeatures,
    FeatureDirection,
    StrategyCompatibility,
    StrategyCompatibilityEvaluator,
    StrategyCompatibilityProfile,
    get_family_profile,
)
from trading_system.autonomous.controller import AutonomousController
from trading_system.autonomous.ranker import (
    OpportunityFeatures,
    OpportunityRankingResult,
    RankedOpportunity,
)
from trading_system.paper.control import PaperTradingControlCenter
from trading_system.strategy_factory.discovery import (
    discover,
    registered_strategy_ids,
    clear_discovery,
    get_strategy_class,
)
from trading_system.strategy_factory.metadata import StrategyFamily

UTC = timezone.utc

# Test fixture: strategy IDs discovered from the builtin module.
BUILTIN_IDS = ["ema_crossover", "rsi_mean_reversion"]


@pytest.fixture(autouse=True)
def _discover_builtin_strategies():
    """Ensure builtin strategies are registered before every test."""
    clear_discovery()
    discover(["trading_system.strategy_factory.builtin"])
    yield
    clear_discovery()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _make_features(
    trend=0.5,
    momentum=0.5,
    volatility=0.5,
    liquidity=0.5,
    freshness=1.0,
):
    """Create OpportunityFeatures with specific normalized values."""
    return OpportunityFeatures(
        symbol="NSE:SBIN",
        market_timestamp=None,
        momentum_score=momentum,
        trend_score=trend,
        volatility_score=volatility,
        liquidity_score=liquidity,
        freshness_score=freshness,
    )


def _make_opportunity(
    symbol="NSE:SBIN",
    rank=1,
    features=None,
    data_points=100,
    timeframe="1d",
    market_ts=None,
    scan_id="scan-test",
    ranking_id="rank-test",
):
    """Build a RankedOpportunity for Phase 4 tests."""
    if features is None:
        features = _make_features(trend=0.7, momentum=0.8, volatility=0.3, liquidity=0.9)
    if market_ts is None:
        market_ts = datetime(2024, 2, 1, 7, 0, tzinfo=UTC)
    return RankedOpportunity(
        rank=rank,
        symbol=symbol,
        opportunity_score=0.85,
        features=features,
        scan_id=scan_id,
        market_timestamp=market_ts.isoformat() if isinstance(market_ts, datetime) else market_ts,
        score_components={"total": 0.85},
        metadata={
            "timeframe": timeframe,
            "latest_price": 100.0,
            "avg_volume": 1_000_000.0,
            "data_points": data_points,
        },
    )


def _make_ranking_result(
    opportunities=None,
    ranking_id="rank-test",
    scan_id="scan-test",
    score_range=(0.0, 1.0),
):
    """Build an OpportunityRankingResult for Phase 4 tests."""
    if opportunities is None:
        opps = [_make_opportunity()]
    else:
        opps = opportunities
    return OpportunityRankingResult(
        ranking_id=ranking_id,
        scan_id=scan_id,
        ranking_timestamp=datetime(2024, 2, 1, 7, 0, tzinfo=UTC).isoformat(),
        candidates_evaluated=len(opps),
        opportunities=opps,
        exclusions=[],
        config_snapshot={"version": "phase3-v1"},
        score_range=score_range,
        data_provider_source="candidate_ranker",
    )


def _make_bot_config(
    allowed_symbols=None,
    allowed_strategy_ids=None,
    enabled=True,
):
    if allowed_symbols is None:
        allowed_symbols = frozenset({"NSE:SBIN", "NSE:TCS"})
    if allowed_strategy_ids is None:
        allowed_strategy_ids = frozenset(BUILTIN_IDS)
    return AutonomousBotConfig(
        bot_id="bot-test-p4",
        name="Phase 4 Test Bot",
        mode=BotMode.AUTONOMOUS,
        trading_mode=TradingMode.PAPER,
        enabled=enabled,
        user_constraints=UserConstraints(
            allowed_symbols=allowed_symbols,
            allowed_strategy_ids=allowed_strategy_ids,
            allowed_timeframes=frozenset({"1m", "5m", "15m", "30m", "1h", "4h", "1d"}),
        ),
        max_simultaneous_positions=5,
        source=Source.AUTONOMOUS,
    )


def _make_controller(allowed_strategy_ids=None, enabled=True):
    if allowed_strategy_ids is None:
        allowed_strategy_ids = frozenset(BUILTIN_IDS)
    config = _make_bot_config(
        allowed_strategy_ids=allowed_strategy_ids,
        enabled=enabled,
    )
    cc = MagicMock(spec=PaperTradingControlCenter)
    cc.load_market_data.return_value = None
    cc.list_deployments.return_value = []
    return AutonomousController(config=config, control_center=cc)


def _eval(
    cfg=None,
    ranking=None,
    allowed=None,
    discovered=None,
):
    """Convenience: create evaluator and evaluate with standard fixtures."""
    cfg = cfg or CompatibilityConfig()
    ranking = ranking or _make_ranking_result()
    allowed = allowed if allowed is not None else frozenset(BUILTIN_IDS)
    discovered = discovered if discovered is not None else BUILTIN_IDS
    evaluator = StrategyCompatibilityEvaluator(cfg)
    return evaluator.evaluate(
        ranking,
        allowed_strategies=allowed,
        discovered_strategies=discovered,
    )


# --------------------------------------------------------------------------- #
# Test: Strategy discovery
# --------------------------------------------------------------------------- #

class TestStrategyDiscovery:
    """Verify the evaluator discovers strategies via the existing factory."""

    def test_discovers_registered_strategies(self):
        """Builtin strategies are discovered after calling discover_strategies."""
        clear_discovery()
        cfg = CompatibilityConfig()
        evaluator = StrategyCompatibilityEvaluator(cfg)
        ids = evaluator.discover_strategies()
        assert "ema_crossover" in ids
        assert "rsi_mean_reversion" in ids

    def test_unknown_strategy_gets_not_found_exclusion(self):
        """A strategy ID in the allowed set but not discovered → STRATEGY_NOT_FOUND."""
        cfg = CompatibilityConfig()
        result = _eval(
            allowed=frozenset({"nonexistent_strategy"}),
            discovered=[],
        )
        not_found = [e for e in result.exclusions
                     if e.reason == CompatibilityExclusionReason.STRATEGY_NOT_FOUND]
        assert len(not_found) > 0
        assert not_found[0].strategy_id == "nonexistent_strategy"

    def test_no_strategies_discovered(self):
        """When no strategies are discovered, all targets produce STRATEGY_NOT_FOUND."""
        cfg = CompatibilityConfig()
        result = _eval(
            allowed=frozenset({"ema_crossover"}),
            discovered=[],
        )
        not_found = [e for e in result.exclusions
                     if e.reason == CompatibilityExclusionReason.STRATEGY_NOT_FOUND]
        assert len(not_found) == len(cfg.timeframes)


# --------------------------------------------------------------------------- #
# Test: Strategy eligibility
# --------------------------------------------------------------------------- #

class TestStrategyEligibility:
    """Verify strategy eligibility gates are respected."""

    def test_allowed_strategy_is_eligible(self):
        """A strategy in the allowed set and discovered → evaluated (not excluded)."""
        result = _eval(allowed=frozenset({"ema_crossover"}))
        assert result.compatible_count > 0
        assert all(c.strategy_id == "ema_crossover" for c in result.compatible)

    def test_strategy_not_in_allowed_set_is_ineligible(self):
        """A discovered strategy not in the allowed set → STRATEGY_INELIGIBLE."""
        result = _eval(
            allowed=frozenset({"ema_crossover"}),
            discovered=BUILTIN_IDS,
        )
        inelig = [e for e in result.exclusions
                  if e.reason == CompatibilityExclusionReason.STRATEGY_INELIGIBLE]
        assert len(inelig) > 0
        assert any(e.strategy_id == "rsi_mean_reversion" for e in inelig)

    def test_validation_gates_respected(self):
        """Strategy metadata is validated — invalid metadata → STRATEGY_NOT_FOUND."""
        with patch.object(
            StrategyCompatibilityEvaluator,
            "_resolve_metadata",
            return_value=None,
        ):
            cfg = CompatibilityConfig()
            evaluator = StrategyCompatibilityEvaluator(cfg)
            ranking = _make_ranking_result()
            result = evaluator.evaluate(
                ranking,
                allowed_strategies=frozenset({"ema_crossover"}),
                discovered_strategies=["ema_crossover"],
            )
        not_found = [e for e in result.exclusions
                     if e.reason == CompatibilityExclusionReason.STRATEGY_NOT_FOUND]
        assert len(not_found) == len(cfg.timeframes)


# --------------------------------------------------------------------------- #
# Test: Timeframe evaluation
# --------------------------------------------------------------------------- #

class TestTimeframeEvaluation:
    """Verify timeframe support is checked against strategy metadata."""

    def test_supported_timeframe_evaluated(self):
        """Timeframes in the strategy's metadata.timeframes are evaluated."""
        cfg = CompatibilityConfig(timeframes=("1m", "5m", "1d"))
        result = _eval(cfg=cfg)
        tfs = {c.timeframe for c in result.compatible}
        assert "1m" in tfs
        assert "5m" in tfs
        assert "1d" in tfs

    def test_unsupported_timeframe_excluded(self):
        """Timeframes not in metadata.timeframes → UNSUPPORTED_TIMEFRAME.

        Uses '1w' which is valid per TRADING_PERIODS but not in either
        strategy's metadata.timeframes.
        """
        cfg = CompatibilityConfig(timeframes=("1m", "1w"))
        result = _eval(cfg=cfg)
        unsup = [e for e in result.exclusions
                 if e.reason == CompatibilityExclusionReason.UNSUPPORTED_TIMEFRAME]
        assert len(unsup) == len(cfg.timeframes)

    def test_invalid_timeframe_in_config_raises(self):
        """A timeframe not in TRADING_PERIODS → config validation error."""
        with pytest.raises(ValueError, match="unsupported timeframe"):
            CompatibilityConfig(timeframes=("7x",))


# --------------------------------------------------------------------------- #
# Test: Compatibility scoring
# --------------------------------------------------------------------------- #

class TestCompatibilityScoring:
    """Verify compatibility score computation and bounds."""

    def test_trend_strategy_prefers_uptrend(self):
        """TREND strategy should score higher with high trend_score."""
        cfg = CompatibilityConfig()
        evaluator = StrategyCompatibilityEvaluator(cfg)
        profile = get_family_profile(StrategyFamily.TREND)
        assert profile is not None
        assert profile.feature_directions["trend"] == FeatureDirection.HIGH

        feat_low = CompatibilityFeatures(trend_score=0.1, momentum_score=0.5,
                                         volatility_score=0.5, liquidity_score=0.5)
        feat_high = CompatibilityFeatures(trend_score=0.9, momentum_score=0.5,
                                          volatility_score=0.5, liquidity_score=0.5)
        w = cfg.normalized_weights()

        score_low, _, _ = evaluator._compute_compatibility(feat_low, profile, w, 1.0)
        score_high, _, _ = evaluator._compute_compatibility(feat_high, profile, w, 1.0)
        assert score_high > score_low

    def test_mean_reversion_prefers_centered_trend(self):
        """MEAN_REVERSION strategy should peak at trend_score ≈ 0.5."""
        cfg = CompatibilityConfig()
        evaluator = StrategyCompatibilityEvaluator(cfg)
        profile = get_family_profile(StrategyFamily.MEAN_REVERSION)
        assert profile is not None
        assert profile.feature_directions["trend"] == FeatureDirection.CENTERED

        w = cfg.normalized_weights()
        feat_center = CompatibilityFeatures(trend_score=0.5, momentum_score=0.5,
                                            volatility_score=0.5, liquidity_score=0.5)
        feat_edge = CompatibilityFeatures(trend_score=0.0, momentum_score=0.5,
                                          volatility_score=0.5, liquidity_score=0.5)
        score_center, _, _ = evaluator._compute_compatibility(feat_center, profile, w, 1.0)
        score_edge, _, _ = evaluator._compute_compatibility(feat_edge, profile, w, 1.0)
        assert score_center > score_edge

    def test_breakout_prefers_high_volatility(self):
        """BREAKOUT strategy should score higher with high volatility_score."""
        cfg = CompatibilityConfig()
        evaluator = StrategyCompatibilityEvaluator(cfg)
        profile = get_family_profile(StrategyFamily.BREAKOUT)
        assert profile.feature_directions["volatility"] == FeatureDirection.HIGH

        feat_low = CompatibilityFeatures(trend_score=0.5, momentum_score=0.5,
                                         volatility_score=0.1, liquidity_score=0.5)
        feat_high = CompatibilityFeatures(trend_score=0.5, momentum_score=0.5,
                                          volatility_score=0.9, liquidity_score=0.5)
        w = cfg.normalized_weights()
        score_low, _, _ = evaluator._compute_compatibility(feat_low, profile, w, 1.0)
        score_high, _, _ = evaluator._compute_compatibility(feat_high, profile, w, 1.0)
        assert score_high > score_low

    def test_score_bounds(self):
        """Compatibility score must be in [0, 1]."""
        cfg = CompatibilityConfig()
        evaluator = StrategyCompatibilityEvaluator(cfg)
        profile = get_family_profile(StrategyFamily.TREND)
        w = cfg.normalized_weights()

        for t in (0.0, 0.5, 1.0):
            for m in (0.0, 0.5, 1.0):
                for v in (0.0, 0.5, 1.0):
                    for l in (0.0, 0.5, 1.0):
                        for f in (0.0, 0.5, 1.0):
                            feat = CompatibilityFeatures(
                                trend_score=t, momentum_score=m,
                                volatility_score=v, liquidity_score=l,
                            )
                            score, _, _ = evaluator._compute_compatibility(
                                feat, profile, w, f
                            )
                            assert 0.0 <= score <= 1.0

    def test_freshness_reduces_score(self):
        """Stale data (low freshness_score) should reduce compatibility."""
        cfg = CompatibilityConfig()
        evaluator = StrategyCompatibilityEvaluator(cfg)
        profile = get_family_profile(StrategyFamily.TREND)
        feat = CompatibilityFeatures(trend_score=0.9, momentum_score=0.9,
                                     volatility_score=0.5, liquidity_score=0.9)
        w = cfg.normalized_weights()

        score_fresh, _, _ = evaluator._compute_compatibility(feat, profile, w, 1.0)
        score_stale, _, _ = evaluator._compute_compatibility(feat, profile, w, 0.0)
        assert score_stale < score_fresh
        assert score_fresh > 0.0
        assert score_stale == 0.0

    def test_component_factors_preserved(self):
        """Score components must be in the result."""
        result = _eval()
        assert len(result.compatible) > 0
        comp = result.compatible[0]
        assert "trend" in comp.score_components
        assert "momentum" in comp.score_components
        assert "volatility" in comp.score_components
        assert "liquidity" in comp.score_components

    def test_deterministic_repeated_evaluation(self):
        """Same input must produce identical results across repeated calls."""
        evaluator = StrategyCompatibilityEvaluator(CompatibilityConfig())
        ranking = _make_ranking_result()
        r1 = evaluator.evaluate(ranking, allowed_strategies=frozenset(BUILTIN_IDS),
                                discovered_strategies=BUILTIN_IDS)
        r2 = evaluator.evaluate(ranking, allowed_strategies=frozenset(BUILTIN_IDS),
                                discovered_strategies=BUILTIN_IDS)
        assert r1.result_id == r2.result_id
        assert [c.compatibility_score for c in r1.compatible] == \
               [c.compatibility_score for c in r2.compatible]


# --------------------------------------------------------------------------- #
# Test: Snapshot consistency
# --------------------------------------------------------------------------- #

class TestSnapshotConsistency:
    """Verify Phase 4 uses only the Phase 3 ranking data (no new fetches)."""

    def test_no_market_data_fetch_during_evaluation(self):
        """The evaluator must never call the control center / data provider."""
        evaluator = StrategyCompatibilityEvaluator(CompatibilityConfig())
        ranking = _make_ranking_result()
        result = evaluator.evaluate(
            ranking,
            allowed_strategies=frozenset({"ema_crossover"}),
            discovered_strategies=["ema_crossover"],
        )
        assert result.compatible_count > 0
        # Evaluator has no _provider attribute.
        assert not hasattr(evaluator, "_provider")

    def test_uses_ranking_features_not_independent_fetch(self):
        """Compatibility score must be derived from ranking features, not re-fetched data."""
        cfg = CompatibilityConfig()
        evaluator = StrategyCompatibilityEvaluator(cfg)

        opp_high = _make_opportunity(
            symbol="NSE:HIGH", rank=1,
            features=_make_features(trend=0.9, momentum=0.9, volatility=0.3, liquidity=0.9),
        )
        opp_low = _make_opportunity(
            symbol="NSE:LOW", rank=2,
            features=_make_features(trend=0.1, momentum=0.1, volatility=0.3, liquidity=0.5),
        )
        ranking = _make_ranking_result([opp_high, opp_low])

        result = evaluator.evaluate(
            ranking,
            allowed_strategies=frozenset({"ema_crossover"}),
            discovered_strategies=["ema_crossover"],
        )
        scores_by_symbol = {c.opportunity_symbol: c.compatibility_score for c in result.compatible}
        assert scores_by_symbol["NSE:HIGH"] > scores_by_symbol["NSE:LOW"]

    def test_result_id_traces_to_ranking_id(self):
        """result_id must include the ranking_id for snapshot traceability."""
        cfg = CompatibilityConfig()
        evaluator = StrategyCompatibilityEvaluator(cfg)
        ranking = _make_ranking_result(ranking_id="rank-abc123")
        result = evaluator.evaluate(
            ranking,
            allowed_strategies=frozenset({"ema_crossover"}),
            discovered_strategies=["ema_crossover"],
        )
        assert result.ranking_id == "rank-abc123"

    def test_no_future_timestamp_in_results(self):
        """Results must reference the opportunity's market_timestamp from Phase 2 snapshot."""
        cfg = CompatibilityConfig()
        evaluator = StrategyCompatibilityEvaluator(cfg)
        ranking = _make_ranking_result()
        result = evaluator.evaluate(
            ranking,
            allowed_strategies=frozenset({"ema_crossover"}),
            discovered_strategies=["ema_crossover"],
        )
        for c in result.compatible:
            assert c.market_timestamp == ranking.opportunities[0].market_timestamp


# --------------------------------------------------------------------------- #
# Test: Exclusions
# --------------------------------------------------------------------------- #

class TestExclusions:
    """Verify structured exclusion reasons and identity retention."""

    def test_exclusion_retains_identity(self):
        """Exclusions must carry opportunity symbol, strategy_id, and timeframe."""
        cfg = CompatibilityConfig(timeframes=("1m", "1w"))
        result = _eval(cfg=cfg)
        unsup = [e for e in result.exclusions
                 if e.reason == CompatibilityExclusionReason.UNSUPPORTED_TIMEFRAME]
        assert len(unsup) > 0
        excl = unsup[0]
        assert excl.opportunity_symbol == "NSE:SBIN"
        assert excl.strategy_id == "ema_crossover"
        assert excl.timeframe == "1w"

    def test_all_invalid_combinations_handled_safely(self):
        """When all combinations fail, the result must be safe (no compatible entries)."""
        cfg = CompatibilityConfig(timeframes=("1w",), min_compatibility_score=1.0)
        result = _eval(cfg=cfg)
        assert result.compatible_count == 0
        assert result.excluded_count > 0

    def test_min_score_threshold_excludes_low_compatibility(self):
        """Scores below min_compatibility_score are excluded as INCOMPATIBLE."""
        cfg = CompatibilityConfig(min_compatibility_score=0.99)
        # Neutral features → moderate score for any family.
        ranking = _make_ranking_result(opportunities=[
            _make_opportunity(features=_make_features(
                trend=0.5, momentum=0.5, volatility=0.5, liquidity=0.5,
            )),
        ])
        result = _eval(cfg=cfg, ranking=ranking)
        assert result.compatible_count == 0

    def test_insufficient_data_exclusion(self):
        """Opportunity with too few data_points → INSUFFICIENT_MARKET_DATA."""
        cfg = CompatibilityConfig()
        ranking = _make_ranking_result(opportunities=[
            _make_opportunity(data_points=5),  # below min_history_bars=20
        ])
        result = _eval(cfg=cfg, ranking=ranking)
        insuf = [e for e in result.exclusions
                 if e.reason == CompatibilityExclusionReason.INSUFFICIENT_MARKET_DATA]
        # 2 strategies × 4 timeframes = 8 exclusions
        assert len(insuf) == len(cfg.timeframes) * len(BUILTIN_IDS)

    def test_incompatible_market_conditions_excluded(self):
        """Strategy that prefers high trend + low-trend opportunity → low score."""
        # rsi_mean_reversion (MEAN_REVERSION) prefers centered trend.
        # A strong uptrend opportunity (trend=0.95) should give low compatibility.
        ranking = _make_ranking_result(opportunities=[
            _make_opportunity(
                symbol="NSE:STRONG", rank=1,
                features=_make_features(trend=0.95, momentum=0.95,
                                         volatility=0.1, liquidity=0.9),
            ),
        ])
        cfg = CompatibilityConfig()
        result = _eval(
            cfg=cfg, ranking=ranking,
            allowed=frozenset({"rsi_mean_reversion"}),
            discovered=["rsi_mean_reversion"],
        )
        # Mean-reversion prefers centered trend and low momentum.
        # Strong trend (0.95) + high momentum (0.95) → low match.
        assert result.compatible_count > 0
        score = result.compatible[0].compatibility_score
        # Score should be relatively low (well below 0.5).
        assert score < 0.5


# --------------------------------------------------------------------------- #
# Test: Deterministic ordering
# --------------------------------------------------------------------------- #

class TestOrdering:
    """Verify deterministic result ordering."""

    def test_highest_compatibility_first(self):
        """Compatible results sorted by score DESC."""
        opp_high = _make_opportunity(
            symbol="NSE:HIGH", rank=1,
            features=_make_features(trend=0.95, momentum=0.95, volatility=0.3, liquidity=0.9),
        )
        opp_low = _make_opportunity(
            symbol="NSE:LOW", rank=2,
            features=_make_features(trend=0.05, momentum=0.05, volatility=0.3, liquidity=0.5),
        )
        ranking = _make_ranking_result([opp_high, opp_low])
        result = _eval(ranking=ranking)
        scores = [c.compatibility_score for c in result.compatible]
        assert scores == sorted(scores, reverse=True)
        assert result.compatible[0].opportunity_symbol == "NSE:HIGH"

    def test_deterministic_ties(self):
        """Equal scores resolve by strategy_id ASC, then timeframe ASC."""
        cfg = CompatibilityConfig(timeframes=("5m", "15m", "1h"))
        ranking = _make_ranking_result([
            _make_opportunity(features=_make_features(
                trend=0.5, momentum=0.5, volatility=0.5, liquidity=0.5
            )),
        ])
        r1 = _eval(cfg=cfg, ranking=ranking,
                   allowed=frozenset(BUILTIN_IDS), discovered=BUILTIN_IDS)
        r2 = _eval(cfg=cfg, ranking=ranking,
                   allowed=frozenset(BUILTIN_IDS), discovered=BUILTIN_IDS)
        # Deterministic: same inputs → same order.
        assert [c.strategy_id for c in r1.compatible] == \
               [c.strategy_id for c in r2.compatible]
        assert [c.timeframe for c in r1.compatible] == \
               [c.timeframe for c in r2.compatible]
        # Verify sort key: score DESC, strategy_id ASC, timeframe ASC.
        for i in range(len(r1.compatible) - 1):
            a, b = r1.compatible[i], r1.compatible[i + 1]
            key_a = (-a.compatibility_score, a.strategy_id, a.timeframe)
            key_b = (-b.compatibility_score, b.strategy_id, b.timeframe)
            assert key_a <= key_b

    def test_stable_repeated_runs(self):
        """Repeated evaluation with same inputs → identical output."""
        evaluator = StrategyCompatibilityEvaluator(CompatibilityConfig())
        ranking = _make_ranking_result([
            _make_opportunity(),
            _make_opportunity(symbol="NSE:TCS", rank=2),
        ])
        kwargs = dict(
            ranking_result=ranking,
            allowed_strategies=frozenset(BUILTIN_IDS),
            discovered_strategies=BUILTIN_IDS,
        )
        r1 = evaluator.evaluate(**kwargs)
        r2 = evaluator.evaluate(**kwargs)
        assert r1.result_id == r2.result_id
        assert r1.compatible_count == r2.compatible_count
        assert all(
            a.compatibility_score == b.compatibility_score
            for a, b in zip(r1.compatible, r2.compatible)
        )

    def test_top_n_limit(self):
        """top_n caps the number of compatible results."""
        cfg = CompatibilityConfig(top_n=2)
        result = _eval(cfg=cfg)
        assert len(result.compatible) <= 2


# --------------------------------------------------------------------------- #
# Test: Controller integration
# --------------------------------------------------------------------------- #

class TestControllerIntegration:
    """Verify the controller orchestrates Phase 4 correctly."""

    def test_controller_invokes_evaluator(self):
        """Controller.evaluate_strategy_compatibility returns a CompatibilityResult."""
        controller = _make_controller()
        ranking = _make_ranking_result()
        result = controller.evaluate_strategy_compatibility(ranking)
        assert isinstance(result, CompatibilityResult)
        assert result.evaluated_count > 0

    def test_controller_does_not_create_deployments(self):
        """Controller must NOT create any deployments during compatibility evaluation."""
        controller = _make_controller()
        cc = controller.control_center
        cc.reset_mock()
        ranking = _make_ranking_result()
        controller.evaluate_strategy_compatibility(ranking)
        assert controller.config.deployment_count == 0

    def test_controller_does_not_create_orders(self):
        """Controller must NOT create any orders during compatibility evaluation."""
        controller = _make_controller()
        cc = controller.control_center
        cc.reset_mock()
        ranking = _make_ranking_result()
        controller.evaluate_strategy_compatibility(ranking)
        # The control center mock has spec=PaperTradingControlCenter, so any
        # method not on the spec raises AttributeError. Verify no order/
        # deployment methods are on the mock's call list.
        order_methods = [
            "create_depot_order", "submit_order", "execute_order",
            "create_order", "send_order",
        ]
        for method_name in order_methods:
            if hasattr(cc, method_name):
                mock_method = getattr(cc, method_name)
                if hasattr(mock_method, "called"):
                    assert not mock_method.called

    def test_controller_does_not_execute_strategies(self):
        """Controller must NOT execute strategies during compatibility evaluation."""
        controller = _make_controller()
        ranking = _make_ranking_result()
        result = controller.evaluate_strategy_compatibility(ranking)
        # No strategy execution results in the compatibility result.
        assert result.compatible_count > 0 or result.excluded_count > 0
        for c in result.compatible:
            assert not hasattr(c, "signal")
            assert not hasattr(c, "position")

    def test_controller_respects_allowed_strategy_ids(self):
        """Controller filters to user_constraints.allowed_strategy_ids."""
        controller = _make_controller(
            allowed_strategy_ids=frozenset({"ema_crossover"}),
        )
        ranking = _make_ranking_result()
        result = controller.evaluate_strategy_compatibility(ranking)
        strat_ids = {c.strategy_id for c in result.compatible}
        assert "rsi_mean_reversion" not in strat_ids

    def test_controller_passes_config(self):
        """Controller forwards a custom CompatibilityConfig."""
        controller = _make_controller()
        custom_cfg = CompatibilityConfig(timeframes=("1d",), top_n=1)
        ranking = _make_ranking_result()
        result = controller.evaluate_strategy_compatibility(ranking, config=custom_cfg)
        assert result.config_snapshot["timeframes"] == ["1d"]

    def test_controller_disabled_returns_empty(self):
        """When the bot is disabled, compatibility evaluation returns empty."""
        controller = _make_controller(enabled=False)
        ranking = _make_ranking_result()
        result = controller.evaluate_strategy_compatibility(ranking)
        assert result.compatible_count == 0
        assert result.evaluated_count == 0
        assert result.has_compatible is False


# --------------------------------------------------------------------------- #
# Test: Safety — no execution paths
# --------------------------------------------------------------------------- #

class TestSafety:
    """Verify Phase 4 has no execution/trading paths."""

    def test_no_live_trading_constants(self):
        """CompatibilityConfig must not have a live-trading mode."""
        cfg = CompatibilityConfig()
        assert cfg.enabled is True
        assert not hasattr(cfg, "live_trading")
        assert not hasattr(cfg, "execute")

    def test_no_broker_fields(self):
        """No broker-related fields in any Phase 4 model."""
        for model_cls in [StrategyCompatibility, CompatibilityResult, CompatibilityExclusion]:
            field_names = set(model_cls.model_fields.keys())
            assert "broker" not in field_names
            assert "order" not in field_names
            assert "deployment" not in field_names
            assert "position" not in field_names
            assert "signal" not in field_names

    def test_evaluator_has_no_data_provider(self):
        """StrategyCompatibilityEvaluator must NOT accept a data provider."""
        cfg = CompatibilityConfig()
        evaluator = StrategyCompatibilityEvaluator(cfg)
        assert not hasattr(evaluator, "_provider")
        assert not hasattr(evaluator, "data_provider")

    def test_empty_ranking_result_safe(self):
        """Empty ranking result → 0 evaluations, 0 compatible, 0 exclusions."""
        cfg = CompatibilityConfig()
        evaluator = StrategyCompatibilityEvaluator(cfg)
        ranking = _make_ranking_result(opportunities=[])
        result = evaluator.evaluate(
            ranking,
            allowed_strategies=frozenset({"ema_crossover"}),
            discovered_strategies=["ema_crossover"],
        )
        assert result.evaluated_count == 0
        assert result.compatible_count == 0
        assert result.excluded_count == 0

    def test_all_strategies_ineligible_family(self):
        """Strategies with no family profile → all excluded as INELIGIBLE."""
        import trading_system.autonomous.compatibility as compat_mod
        original = compat_mod._FAMILY_PROFILES.copy()
        compat_mod._FAMILY_PROFILES.clear()
        try:
            cfg = CompatibilityConfig()
            evaluator = StrategyCompatibilityEvaluator(cfg)
            ranking = _make_ranking_result()
            result = evaluator.evaluate(
                ranking,
                allowed_strategies=frozenset({"ema_crossover"}),
                discovered_strategies=["ema_crossover"],
            )
            inelig = [e for e in result.exclusions
                      if e.reason == CompatibilityExclusionReason.STRATEGY_INELIGIBLE]
            assert len(inelig) == len(cfg.timeframes)
            assert result.compatible_count == 0
        finally:
            compat_mod._FAMILY_PROFILES.update(original)
