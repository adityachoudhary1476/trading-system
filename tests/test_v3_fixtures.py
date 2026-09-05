"""V3 fixture-matrix tests — scenarios A–P.

Invariant-based: fixtures are deterministic (seeded), so each relationship is
proven once and holds permanently. Every fixture is SYNTHETIC / TEST DATA.
No live API, broker token, or external service is required.
"""
from __future__ import annotations

import pathlib
import sys

import pandas as pd
import pytest

_TESTS = pathlib.Path(__file__).resolve().parent
for _p in (_TESTS, _TESTS.parent / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from fixtures.v3_fixtures import (  # noqa: E402
    bullish_aligned, bearish_aligned, conflicting_timeframes,
    high_volatility, low_volatility, insufficient_history,
    evidence_conflict, stale_ohlcv, regime_transition,
    strong_breadth, weak_breadth, calm_vix, high_vix,
    bullish_fii_dii, bearish_fii_dii, sector_outperforming,
    sector_underperforming, liquid_option_chain, illiquid_option_chain,
    partial_option_chain, news_context_bullish, news_context_bearish,
    cross_asset_risk_on, full_market_context, empty_market_context,
)
from trading_system.research.intelligence import (  # noqa: E402
    FeatureEngine, MarketIntelligenceEngine, SignalDirection,
    classify_regime, _build_instrument_context, analyze_multi_timeframe,
)
from trading_system.research.intelligence_v3 import (  # noqa: E402
    compute_timeframe_consensus, detect_regime_transition,
    compute_options_analytics, build_evidence_ledger_v2,
    compute_confidence_v2, replay_history, label_outcome,
    compute_calibration, analyze_feature_performance,
)
from trading_system.research.market_context import (  # noqa: E402
    DataQualityTier, EvidenceAvailability,
)

fe = FeatureEngine(lookback=60)
eng = MarketIntelligenceEngine(lookback=60)


def _ledger(df, direction=SignalDirection.LONG, **ctx):
    feats = fe.compute(df)
    regime = classify_regime(feats)
    return build_evidence_ledger_v2(feats, regime, direction, **ctx)


class TestMarketStateDiscrimination:

    def test_bullish_vs_bearish_bias(self):
        """(A/B) Opposite trends produce opposite biases."""
        up = eng.analyze("NSE:TCS-EQ", "1d", bullish_aligned(seed=11))
        dn = eng.analyze("NSE:INFY-EQ", "1d", bearish_aligned(seed=12))
        assert up["signal_candidate"].direction == SignalDirection.LONG
        assert dn["signal_candidate"].direction == SignalDirection.SHORT

    def test_aligned_beats_conflicted(self):
        """(A vs O) Agreement outranks trend-vs-momentum conflict."""
        conf_a, _ = compute_confidence_v2(_ledger(bullish_aligned(seed=11)))
        conf_o, _ = compute_confidence_v2(_ledger(evidence_conflict(seed=22)))
        assert conf_a > conf_o

    def test_high_vs_low_vol_differ(self):
        """(D/E) Volatility regimes produce different intelligence."""
        r_hi = eng.analyze("NSE:SBIN-EQ", "1d", high_volatility(seed=14))
        r_lo = eng.analyze("NSE:SBIN-EQ", "1d", low_volatility(seed=15))
        differs = (r_hi["regime"].regime != r_lo["regime"].regime
                   or r_hi["signal_candidate"].confidence
                   != r_lo["signal_candidate"].confidence)
        assert differs
# Staged second half of tests/test_v3_fixtures.py (appended by the runner step).
from sqlalchemy import create_engine  # noqa: E402

from trading_system.research.forecast_ledger import ForecastStore  # noqa: E402
from trading_system.research.market_context import NewsContext  # noqa: E402


class TestTimeframeConsensus:

    def test_conflicted_timeframes(self):
        """(C) Intraday bear + daily bull = bearish ST bias, HTF conflict."""
        mtf = analyze_multi_timeframe(
            "NSE:NIFTY 50-INDEX", conflicting_timeframes(),
            _build_instrument_context("NSE:NIFTY 50-INDEX"))
        cons = compute_timeframe_consensus(mtf)
        assert cons.short_term_bias == "bearish"
        assert cons.higher_timeframe_conflict is True
        assert cons.participating_timeframes == 4

    def test_aligned_timeframes_agree(self):
        """(A) Aligned TFs produce strong alignment, no HTF conflict."""
        dfs = {tf: bullish_aligned(seed=41) for tf in ("5m", "15m", "1h", "1d")}
        mtf = analyze_multi_timeframe("NSE:TCS-EQ", dfs,
                                      _build_instrument_context("NSE:TCS-EQ"))
        cons = compute_timeframe_consensus(mtf)
        assert cons.higher_timeframe_conflict is False
        assert cons.short_term_alignment in ("strong", "moderate")


class TestSectorRelativeStrength:

    def test_outperformance_beats_underperformance(self):
        """(F/G) Sector RS flows into evidence and confidence directionally."""
        bull = bullish_aligned(seed=11)
        led_out = _ledger(bull, sector=sector_outperforming())
        led_und = _ledger(bull, sector=sector_underperforming())
        cats_out = {str(i.category).split(".")[-1].lower() for i in led_out.supported}
        cats_und = {str(i.category).split(".")[-1].lower() for i in led_und.supported}
        assert "sector" in cats_out and "sector" in cats_und
        conf_out, _ = compute_confidence_v2(led_out)
        conf_und, _ = compute_confidence_v2(led_und)
        assert conf_out > conf_und


class TestBreadthContext:

    def test_strong_beats_weak_breadth(self):
        """(H/I) Breadth is structured evidence, not decoration."""
        bull = bullish_aligned(seed=11)
        led_strong = _ledger(bull, breadth=strong_breadth())
        led_weak = _ledger(bull, breadth=weak_breadth())
        cats = {str(i.category).split(".")[-1].lower() for i in led_strong.supported}
        assert "breadth" in cats
        conf_strong, _ = compute_confidence_v2(led_strong)
        conf_weak, _ = compute_confidence_v2(led_weak)
        assert conf_strong > conf_weak


class TestMissingOptionalData:

    def test_unavailable_is_recorded_not_invented(self):
        """(J) No context -> UNAVAILABLE items; missing data never mints evidence."""
        led = _ledger(bullish_aligned(seed=11))
        cats = {str(i.category).split(".")[-1].lower(): i.availability
                for i in led.items}
        assert cats.get("breadth") == EvidenceAvailability.UNAVAILABLE
        assert cats.get("sector") == EvidenceAvailability.UNAVAILABLE
        conf, level = compute_confidence_v2(led)
        assert conf >= 0 and "NO TRADE" not in level


class TestStaleAndThinHistory:

    def test_stale_data_blocked(self):
        """(K) Stale feed must be BLOCKED, not analyzed as fresh."""
        r = eng.analyze("NSE:SBIN-EQ", "1d", stale_ohlcv(seed=23),
                        health_status="STALE")
        assert r["status"] == "BLOCKED"

    def test_insufficient_history_flagged(self):
        """(L) Thin history is explicitly flagged, never passed as reliable."""
        r = eng.analyze("NSE:SBIN-EQ", "1d", insufficient_history(seed=21))
        assert r["status"] == "BLOCKED" or r["data_quality"]["insufficient"] is True

# Staged third-half A: options / news / transitions.
from trading_system.research.intelligence import generate_options_candidates  # noqa: E402


class TestOptionsDataSufficiency:

    def test_liquid_chain_sufficient(self):
        """(M) Full data -> analytics computed, sufficient, liquid."""
        a = compute_options_analytics(liquid_option_chain()[3], 23950.0, 1.2)
        assert a.data_sufficient is True
        assert a.liquidity_score is not None and a.liquidity_score >= 60
        assert a.spread_pct is not None and a.spread_pct < 5

    def test_partial_chain_records_missing_fields(self):
        """Missing Greeks/IV are recorded, never fabricated."""
        a = compute_options_analytics(partial_option_chain()[1], 23950.0, 1.2)
        assert a.missing_fields, "missing Greeks must be recorded"
        assert a.data_sufficient is False

    def test_illiquid_chain_rejected_for_candidates(self):
        """(N) Illiquid chain produces no candidates (valid 'no setup')."""
        feats = fe.compute(bullish_aligned(seed=11))
        regime = classify_regime(feats)
        cands = generate_options_candidates(
            feats, regime, SignalDirection.SHORT, 23950.0,
            illiquid_option_chain())
        assert cands == []


class TestNewsAndCrossAssetSchema:

    def test_news_sentiment_direction(self):
        bull = news_context_bullish()
        bear = news_context_bearish()
        bull_mean = sum(e.sentiment for e in bull.events) / len(bull.events)
        bear_mean = sum(e.sentiment for e in bear.events) / len(bear.events)
        assert bull_mean > bear_mean > -1.0

    def test_empty_news_is_unavailable_not_neutral(self):
        empty = NewsContext(events=[])
        assert "unavailable" in str(empty.news_status).lower()

    def test_cross_asset_fields_present(self):
        ca = cross_asset_risk_on()
        assert ca.usdinr == 83.1 and ca.source.startswith("SYNTHETIC")


class TestRegimeTransition:

    def test_transition_differs_from_stability(self):
        """(P) Regime change is detected vs stable regime."""
        tr_df = regime_transition()
        feats_t = fe.compute(tr_df.iloc[:90])
        feats_f = fe.compute(tr_df)
        reg_t, reg_f = classify_regime(feats_t), classify_regime(feats_f)
        tr_change = detect_regime_transition(reg_f, reg_t, feats_f)
        tr_same = detect_regime_transition(reg_t, reg_t, feats_t)
        assert (tr_change.transition_type or "") != (tr_same.transition_type or "")

    def test_current_regime_preserved(self):
        tr_df = regime_transition()
        feats = fe.compute(tr_df)
        reg = classify_regime(feats)
        tr = detect_regime_transition(reg, reg, feats)
        assert tr.regime == reg.regime

# Staged third-half B: replay / outcomes / calibration / store / labeling policy.
from sqlalchemy import create_engine  # noqa: E402

from trading_system.research.forecast_ledger import ForecastStore  # noqa: E402
from trading_system.research.market_context import NewsContext  # noqa: E402


class TestReplayNoLookahead:

    def test_replay_produces_forecasts_without_violations(self):
        res = replay_history("NSE:SBIN-EQ", "1d", bullish_aligned(seed=11),
                             start_idx=60, step=10)
        assert len(res.forecasts) > 0
        assert res.lookahead_violations == 0

    def test_replay_biases_follow_regime(self):
        bull = replay_history("NSE:SBIN-EQ", "1d", bullish_aligned(seed=11),
                              start_idx=60, step=20)
        bear = replay_history("NSE:RELIANCE-EQ", "1d", bearish_aligned(seed=12),
                              start_idx=60, step=20)
        assert sum(f.bias == "bullish" for f in bull.forecasts) > \
               sum(f.bias == "bearish" for f in bull.forecasts)
        assert sum(f.bias == "bearish" for f in bear.forecasts) > \
               sum(f.bias == "bullish" for f in bear.forecasts)


class TestOutcomeLabeling:

    def test_long_success_and_failure(self):
        ok = label_outcome("bullish", 100.0, [101.0, 102.0, 103.0],
                           expected_move_lower_pct=-1.0,
                           expected_move_upper_pct=2.0)
        bad = label_outcome("bullish", 100.0, [99.0, 98.0, 97.0],
                            expected_move_lower_pct=-1.0,
                            expected_move_upper_pct=2.0)
        assert ok.outcome == "success" and ok.target_hit is True
        assert bad.outcome == "failure" and bad.invalidation_hit is True

    def test_short_mirrored_and_neutral_band(self):
        short_ok = label_outcome("bearish", 100.0, [99.0, 98.0, 97.0],
                                 expected_move_lower_pct=-2.0,
                                 expected_move_upper_pct=1.0)
        flat = label_outcome("neutral", 100.0, [100.1, 99.9, 100.05])
        assert short_ok.outcome == "success"
        assert flat.outcome == "success"


class TestCalibrationSafeguards:

    def test_insufficient_sample_flagged(self):
        rep = compute_calibration([(70.0, True)], min_resolved=100)
        assert rep.sample_sufficient is False
        assert "Insufficient" in rep.note

    def test_buckets_populated_when_sufficient(self):
        outcomes = [(float(50 + i % 40), i % 2 == 0) for i in range(120)]
        rep = compute_calibration(outcomes, min_resolved=100)
        assert rep.sample_sufficient is True
        assert rep.total_forecasts == 120

    def test_feature_performance_sample_guard(self):
        perf = analyze_feature_performance({"trend": [(True, 1.0)] * 5},
                                           min_samples=30)
        assert perf[0].sample_confidence == "insufficient"


class TestForecastStorePersistence:

    def test_record_resolve_list(self):
        store = ForecastStore(create_engine("sqlite://", future=True))
        rec = store.record_forecast("NSE:SBIN-EQ", "1d", "bullish", 0.62,
                                    "short_term",
                                    market_state={"src": "SYNTHETIC/TEST"})
        store.resolve_forecast(rec.id, 1.4)
        rows = store.list_forecasts(resolved=True)
        assert len(rows) == 1
        assert rows[0].hit is True and rows[0].actual_return_pct == 1.4


class TestSyntheticLabelingPolicy:

    def test_every_context_fixture_is_tagged_synthetic(self):
        for obj in (strong_breadth(), weak_breadth(), calm_vix(), high_vix(),
                    bullish_fii_dii(), sector_outperforming(),
                    sector_underperforming(), cross_asset_risk_on()):
            assert str(obj.source).startswith("SYNTHETIC")
        for ev in news_context_bullish().events + news_context_bearish().events:
            assert str(ev.source).startswith("SYNTHETIC")

