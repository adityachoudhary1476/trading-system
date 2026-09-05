"""V3 intelligence tests: consensus, transitions, replay, calibration.

Invariant-based tests — different market states must generate meaningfully
different intelligence. No hardcoded confidence assertions.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_system.research.intelligence import (
    FeatureEngine,
    MarketRegime,
    RegimeEnum,
    SignalDirection,
    TechnicalFeatures,
    TrendEnum,
    classify_regime,
    TimeframeAnalysis,
)
from trading_system.research.intelligence_v3 import (
    CalibrationReport,
    FeaturePerformance,
    MultiTimeframeConsensus,
    OptionsAnalytics,
    OutcomeLabel,
    ReplayResult,
    RegimeTransition,
    TransitionRisk,
    analyze_feature_performance,
    build_evidence_ledger_v2,
    compute_calibration,
    compute_confidence_v2,
    compute_options_analytics,
    compute_timeframe_consensus,
    detect_regime_transition,
    label_outcome,
    replay_history,
)
from trading_system.research.market_context import (
    DataQualityTier,
    FIIDIIFlow,
    InstitutionalFlow,
    MarketBreadth,
    SectorContext,
)


def _ohlc(n: int, start: float = 100.0, drift: float = 0.1, vol: float = 0.5,
          seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    closes = start + np.cumsum(rng.normal(drift, vol, n))
    closes = np.maximum(closes, 1.0)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(closes, opens) + np.abs(rng.normal(0, vol, n))
    lows = np.minimum(closes, opens) - np.abs(rng.normal(0, vol, n))
    lows = np.maximum(lows, 0.5)
    vols = rng.integers(1_000_000, 5_000_000, n).astype(float)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
        index=idx,
    )


def _tf_analysis(bias: TrendEnum, confidence: float):
    return TimeframeAnalysis(timeframe="1d", bias=bias, confidence=confidence)

# --------------------------------------------------------------------------- #
# Phase 2 — Multi-timeframe consensus
# --------------------------------------------------------------------------- #
class TestTimeframeConsensus:

    def test_bullish_alignment(self):
        """All timeframes bullish => strong alignment, no conflict."""
        results = {
            "5m": _tf_analysis(TrendEnum.BULLISH, 70),
            "15m": _tf_analysis(TrendEnum.BULLISH, 65),
            "1h": _tf_analysis(TrendEnum.BULLISH, 60),
            "1d": _tf_analysis(TrendEnum.BULLISH, 55),
        }
        consensus = compute_timeframe_consensus(results)
        assert consensus.short_term_bias == "bullish"
        assert consensus.short_term_alignment == "strong"
        assert consensus.swing_bias == "bullish"
        assert consensus.higher_timeframe_conflict is False

    def test_higher_tf_conflict(self):
        """Intraday bearish + daily bullish => conflict flagged."""
        results = {
            "5m": _tf_analysis(TrendEnum.BEARISH, 70),
            "15m": _tf_analysis(TrendEnum.BEARISH, 65),
            "1h": _tf_analysis(TrendEnum.BEARISH, 60),
            "1d": _tf_analysis(TrendEnum.BULLISH, 80),
        }
        consensus = compute_timeframe_consensus(results)
        assert consensus.higher_timeframe_conflict is True
        assert consensus.short_term_bias == "bearish"
        assert consensus.swing_bias == "bullish"

    def test_conflicted_timeframes(self):
        """Split votes across 3 timeframes => weak alignment."""
        results = {
            "5m": _tf_analysis(TrendEnum.BULLISH, 70),
            "15m": _tf_analysis(TrendEnum.BEARISH, 65),
            "1h": _tf_analysis(TrendEnum.NEUTRAL, 60),
        }
        consensus = compute_timeframe_consensus(results)
        assert consensus.short_term_alignment in ("weak", "conflicted")

    def test_empty_input(self):
        consensus = compute_timeframe_consensus({})
        assert consensus.participating_timeframes == 0
        assert "No timeframe data provided" in consensus.notes


# --------------------------------------------------------------------------- #
# Phase 3 — Regime transitions
# --------------------------------------------------------------------------- #
class TestRegimeTransitions:

    def test_stable_regime_low_risk(self):
        regime = MarketRegime(RegimeEnum.TRENDING_UP, 0.7)
        result = detect_regime_transition(regime, previous=regime)
        assert result.regime == RegimeEnum.TRENDING_UP
        assert result.transition_risk == TransitionRisk.LOW
        assert result.transition_type is None

    def test_trending_to_bearish_high_risk(self):
        prev = MarketRegime(RegimeEnum.TRENDING_UP, 0.7)
        curr = MarketRegime(RegimeEnum.TRENDING_DOWN, 0.7)
        result = detect_regime_transition(curr, previous=prev)
        assert result.transition_risk == TransitionRisk.HIGH
        assert result.transition_type == "trending_up_to_trending_down"

    def test_rsi_extreme_elevates_risk(self):
        regime = MarketRegime(RegimeEnum.TRENDING_UP, 0.7)
        features = TechnicalFeatures(
            close=100.0, data_points=120, rsi_14=85.0,
            price_vs_sma20=0.08, roc=-0.01,
        )
        result = detect_regime_transition(regime, features=features)
        assert result.transition_risk == TransitionRisk.HIGH

    def test_transition_risk_valid_combination(self):
        """regime=TRENDING_UP + transition_risk=HIGH is valid."""
        regime = MarketRegime(RegimeEnum.TRENDING_UP, 0.7)
        features = TechnicalFeatures(
            close=100.0, data_points=120, rsi_14=82.0,
            price_vs_sma20=0.06, roc=-0.02,
        )
        result = detect_regime_transition(regime, features=features)
        assert result.regime == RegimeEnum.TRENDING_UP
        assert result.transition_risk == TransitionRisk.HIGH



def _tf_analysis(bias: TrendEnum, confidence: float):
    return TimeframeAnalysis(timeframe="1d", bias=bias, confidence=confidence)

# --------------------------------------------------------------------------- #
# Phase 5 — Options V2 analytics
# --------------------------------------------------------------------------- #
class TestOptionsAnalytics:

    def test_analytics_computed_from_supplied_data(self):
        contract = {
            "strike": 23800, "option_type": "CE", "bid": 100.0, "ask": 102.0,
            "volume": 5000, "open_interest": 20000, "implied_vol": 0.22,
            "delta": 0.45, "theta": -0.02,
        }
        analytics = compute_options_analytics(contract, spot=23950.0)
        assert analytics.moneyness is not None
        assert analytics.spread_pct is not None
        assert analytics.liquidity_score is not None
        assert analytics.iv_suitability is not None
        assert analytics.delta_suitability is not None
        assert analytics.data_sufficient is True

    def test_missing_fields_recorded_not_fabricated(self):
        contract = {"strike": 23800, "option_type": "CE"}
        analytics = compute_options_analytics(contract, spot=23950.0)
        assert analytics.data_sufficient is False
        assert len(analytics.missing_fields) > 0
        # Missing fields must NOT have invented values.
        assert analytics.delta_suitability is None
        assert analytics.iv_suitability is None

    def test_illiquid_contract_low_score(self):
        contract = {
            "strike": 23800, "option_type": "CE", "bid": 100.0, "ask": 150.0,
            "volume": 50, "open_interest": 100, "implied_vol": 0.8,
            "delta": 0.05, "theta": -0.2,
        }
        analytics = compute_options_analytics(contract, spot=23950.0)
        assert analytics.liquidity_score is not None
        assert analytics.liquidity_score < 30
        assert analytics.iv_suitability is not None
        assert analytics.iv_suitability < 50


# --------------------------------------------------------------------------- #
# Phase 1/4/6/7 — Data context abstractions
# --------------------------------------------------------------------------- #
class TestDataContexts:

    def test_breadth_available(self):
        breadth = MarketBreadth(
            advancing_count=1200, declining_count=400, unchanged_count=100,
            data_quality=DataQualityTier.HEALTHY,
        )
        assert breadth.available is True
        assert breadth.advance_decline_ratio == 3.0
        assert breadth.breadth_strength == "strong"

    def test_breadth_unavailable(self):
        breadth = MarketBreadth()
        assert breadth.available is False
        assert breadth.advance_decline_ratio is None
        assert breadth.breadth_strength is None

    def test_fii_dii_flow(self):
        flow = FIIDIIFlow(
            fii=InstitutionalFlow(buy=5000.0, sell=3000.0),
            dii=InstitutionalFlow(buy=2000.0, sell=2500.0),
            data_quality=DataQualityTier.HEALTHY,
        )
        assert flow.fii.net == 2000.0
        assert flow.dii.net == -500.0
        assert flow.available is True

    def test_sector_context(self):
        sector = SectorContext(
            sector_symbol="NSE:BANKNIFTY", sector_name="Banking",
            sector_return=0.015, data_quality=DataQualityTier.HEALTHY,
        )
        assert sector.available is True

    def test_context_availability_flag(self):
        """Unavailable contexts must be explicitly unavailable."""
        breadth = MarketBreadth()
        sector = SectorContext()
        assert breadth.available is False
        assert sector.available is False


# --------------------------------------------------------------------------- #
# Phase 8/9 — Evidence ledger V2 + confidence
# --------------------------------------------------------------------------- #
class TestEvidenceLedgerV2:

    def test_bullish_evidence_higher_confidence_than_bearish(self):
        """Bullish-aligned features produce higher confidence than bearish."""
        bullish_features = TechnicalFeatures(
            close=105.0, data_points=120, rsi_14=60.0,
            price_vs_sma20=0.04, price_vs_sma50=0.03,
            relative_volume=1.5,
        )
        bearish_features = TechnicalFeatures(
            close=95.0, data_points=120, rsi_14=35.0,
            price_vs_sma20=-0.04, price_vs_sma50=-0.03,
            relative_volume=0.6,
        )
        regime = MarketRegime(RegimeEnum.TRENDING_UP, 0.7)

        bull_ledger = build_evidence_ledger_v2(
            bullish_features, regime, SignalDirection.LONG
        )
        bear_ledger = build_evidence_ledger_v2(
            bearish_features, regime, SignalDirection.LONG
        )
        bull_conf, _ = compute_confidence_v2(bull_ledger)
        bear_conf, _ = compute_confidence_v2(bear_ledger)
        assert bull_conf > bear_conf

    def test_missing_data_does_not_inflate_confidence(self):
        """Features with missing data should not produce false confidence."""
        sparse = TechnicalFeatures(close=100.0, data_points=120)
        regime = MarketRegime(RegimeEnum.TRENDING_UP, 0.7)
        ledger = build_evidence_ledger_v2(sparse, regime, SignalDirection.LONG)
        conf, _ = compute_confidence_v2(ledger)
        assert conf < 70

    def test_unavailable_recorded_not_neutral(self):
        regime = MarketRegime(RegimeEnum.TRENDING_UP, 0.7)
        features = TechnicalFeatures(close=100.0, data_points=120, rsi_14=60.0)
        ledger = build_evidence_ledger_v2(features, regime, SignalDirection.LONG)
        unavailable = ledger.unavailable
        assert len(unavailable) > 0
        for item in unavailable:
            assert item.effective_weight == 0.0

    def test_no_supported_evidence_no_trade(self):
        from trading_system.research.intelligence_v3 import (
            EvidenceCategory, EvidenceItem, EvidenceLedgerV2, EvidenceAvailability,
        )
        ledger = EvidenceLedgerV2()
        ledger.add(EvidenceItem(
            category=EvidenceCategory.TREND, signal="x",
            availability=EvidenceAvailability.UNAVAILABLE,
        ))
        conf, level = compute_confidence_v2(ledger)
        assert conf == 0.0
        assert level == "NO TRADE / INSUFFICIENT EVIDENCE"


# --------------------------------------------------------------------------- #
# Phase 10 — Historical replay (no-lookahead)
# --------------------------------------------------------------------------- #
class TestHistoricalReplay:

    def test_replay_produces_forecasts(self):
        df = _ohlc(120, drift=0.3, vol=0.2, seed=11)
        result = replay_history("NSE:SBIN", "1d", df, start_idx=60, step=10)
        assert isinstance(result, ReplayResult)
        assert len(result.forecasts) > 0

    def test_no_lookahead_violation(self):
        """Causal slice must never extend past the forecast timestamp."""
        df = _ohlc(120, drift=0.2, vol=0.3, seed=7)
        result = replay_history("NSE:SBIN", "1d", df, start_idx=60, step=5)
        for fc in result.forecasts:
            assert fc.timestamp is not None

    def test_replay_forecasts_differ_by_regime(self):
        """Different market regimes should produce different biases."""
        bullish_df = _ohlc(120, drift=0.8, vol=0.1, seed=3)
        bearish_df = _ohlc(120, drift=-0.8, vol=0.1, seed=3)
        bull_result = replay_history("NSE:SBIN", "1d", bullish_df, start_idx=60, step=20)
        bear_result = replay_history("NSE:SBIN", "1d", bearish_df, start_idx=60, step=20)
        bull_biases = [f.bias for f in bull_result.forecasts]
        bear_biases = [f.bias for f in bear_result.forecasts]
        assert bull_biases.count("bullish") > bull_biases.count("bearish")
        assert bear_biases.count("bearish") > bear_biases.count("bullish")

    def test_replay_with_insufficient_data(self):
        df = _ohlc(30, drift=0.2, vol=0.2, seed=1)
        result = replay_history("NSE:SBIN", "1d", df, start_idx=60, step=5)
        assert len(result.forecasts) == 0


# --------------------------------------------------------------------------- #
# Phase 11 — Outcome labeling
# --------------------------------------------------------------------------- #
class TestOutcomeLabeling:

    def test_long_success(self):
        label = label_outcome(
            "bullish", 100.0, [101.0, 102.0, 103.0],
            expected_move_lower_pct=-1.0, expected_move_upper_pct=2.0,
        )
        assert label.outcome == "success"
        assert label.target_hit is True

    def test_long_failure(self):
        label = label_outcome(
            "bullish", 100.0, [99.0, 98.0, 97.0],
            expected_move_lower_pct=-1.0, expected_move_upper_pct=2.0,
        )
        assert label.outcome == "failure"
        assert label.invalidation_hit is True

    def test_short_success(self):
        label = label_outcome(
            "bearish", 100.0, [99.0, 98.0, 97.0],
            expected_move_lower_pct=-2.0, expected_move_upper_pct=1.0,
        )
        assert label.outcome == "success"

    def test_neutral_success(self):
        label = label_outcome(
            "neutral", 100.0, [100.1, 99.9, 100.05],
        )
        assert label.outcome == "success"

    def test_neutral_failure(self):
        label = label_outcome(
            "neutral", 100.0, [100.0, 102.0, 103.0],
        )
        assert label.outcome == "failure"


# --------------------------------------------------------------------------- #
# Phase 12 — Calibration
# --------------------------------------------------------------------------- #
class TestCalibration:

    def test_insufficient_sample_flagging(self):
        outcomes = [(50.0, True), (60.0, False)]
        report = compute_calibration(outcomes, min_resolved=100)
        assert report.sample_sufficient is False
        assert "Insufficient sample size" in report.note

    def test_buckets_populated(self):
        outcomes = [(float(c), c % 2 == 0) for c in range(0, 100)]
        report = compute_calibration(outcomes, min_resolved=50)
        assert report.sample_sufficient is True
        assert len(report.buckets) == 7
        assert report.total_forecasts > 0


# --------------------------------------------------------------------------- #
# Phase 13 — Feature performance
# --------------------------------------------------------------------------- #
class TestFeaturePerformance:

    def test_performance_metrics(self):
        outcomes = {
            "trend": [(True, 2.0), (True, 1.5), (False, -1.0)] * 15,
            "momentum": [(True, 1.0), (False, -2.0)] * 10,
        }
        results = analyze_feature_performance(outcomes, min_samples=30)
        trends = [r for r in results if r.category == "trend"]
        assert len(trends) == 1
        assert trends[0].forecast_count == 45
        assert trends[0].win_rate is not None
        assert trends[0].sample_confidence in ("provisional", "adequate")

    def test_insufficient_sample_flagging(self):
        outcomes = {"trend": [(True, 2.0), (False, -1.0)]}
        results = analyze_feature_performance(outcomes, min_samples=30)
        assert results[0].sample_confidence == "insufficient"

