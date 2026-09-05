"""V4 integration tests: news + patterns -> ledger -> forecast -> comparison.

SYNTHETIC_TEST fixtures only; no network, no live data.
"""
from __future__ import annotations

import copy
import pathlib
import sys
from datetime import timedelta

_TESTS = pathlib.Path(__file__).resolve().parent
for _p in (_TESTS, _TESTS.parent / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from fixtures.v4_fixtures import (  # noqa: E402
    BASE_TIME, conflicting_sources, fresh_news, historical_bullish_pattern,
    make_raw, news_agreeing_with_technicals, news_conflicting_with_technicals,
    stale_news,
)
from trading_system.research.intelligence import (  # noqa: E402
    FeatureEngine, classify_regime,
)
from trading_system.research.intelligence_v3 import (  # noqa: E402
    EvidenceAvailability, EvidenceCategory, EvidenceLedgerV2,
    build_evidence_ledger_v2, compute_confidence_v2,
)
from trading_system.research.news_intelligence import (  # noqa: E402
    NewsPipeline, NewsPipelineResult, build_news_context, news_to_evidence,
    to_v3_context,
)
from trading_system.research.patterns import (  # noqa: E402
    HistoricalPatternEngine, ablation_weights, build_pattern_report,
    fingerprint_from_features, pattern_to_evidence,
)
from trading_system.research.market_context import NewsContext  # noqa: E402
from trading_system.research.v4_compare import (  # noqa: E402
    CONFIGS, _metrics, _vote_direction, compare_strategies,
)

ENG = HistoricalPatternEngine()
FE = FeatureEngine(lookback=60)


def _fp_at(df, i):
    ts = df.index[i]
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    feats = FE.features_at(df, ts)
    return fingerprint_from_features(
        feats, classify_regime(feats), timestamp=ts.to_pydatetime(),
        instrument="NSE:RELIANCE-EQ")


class TestNewsToLedger:

    def test_fresh_positive_news_supported(self):
        result = NewsPipeline().run([fresh_news()], as_of=BASE_TIME)
        ctx = build_news_context(result, as_of=BASE_TIME,
                                 target_ticker="HDFCBANK")
        ledger = news_to_evidence(EvidenceLedgerV2(), ctx, as_of=BASE_TIME)
        items = [i for i in ledger.items
                 if i.category == EvidenceCategory.NEWS
                 and i.signal.startswith("event:")]
        assert items
        assert items[0].availability == EvidenceAvailability.SUPPORTED
        assert items[0].direction == "bullish"

    def test_relevance_gates_irrelevant_news(self):
        result = NewsPipeline().run([fresh_news()], as_of=BASE_TIME)
        ctx = build_news_context(result, as_of=BASE_TIME,
                                 target_ticker="TCS")  # unrelated ticker
        assert ctx.events == []
        ledger = news_to_evidence(EvidenceLedgerV2(), ctx)
        assert any(i.availability == EvidenceAvailability.UNAVAILABLE
                   for i in ledger.items)


class TestPatternToLedger:

    def test_pattern_evidence_flows_to_ledger(self):
        df = historical_bullish_pattern(seed=13)
        as_of = df.index[125].to_pydatetime()
        horizon = timedelta(days=5)
        lib = ENG.build_library(df, start=60, step=1, as_of=as_of - horizon)
        matches = ENG.find_matches(_fp_at(df, 124), lib, as_of=as_of,
                                   min_similarity=0.85, horizon=horizon)
        report = build_pattern_report(matches, df, horizons={"1D": 5},
                                      min_matches=8)
        ledger = pattern_to_evidence(EvidenceLedgerV2(), report,
                                     instrument="NSE:RELIANCE-EQ")
        items = [i for i in ledger.items
                 if i.category == EvidenceCategory.HISTORICAL_PATTERN]
        assert len(items) == 1
        if report.status.name == "SUFFICIENT":
            assert items[0].availability == EvidenceAvailability.SUPPORTED
        elif report.status.name == "INSUFFICIENT_MATCHES":
            assert items[0].availability == EvidenceAvailability.UNAVAILABLE


class TestConfidenceIntegration:

    def _base_ledger(self):
        df = historical_bullish_pattern(seed=13)
        feats = FE.features_at(df, df.index[120])
        regime = classify_regime(feats)
        from trading_system.research.intelligence import SignalDirection
        return build_evidence_ledger_v2(feats, regime, SignalDirection.LONG)

    def test_supportive_evidence_raises_conflicting_lowers(self):
        from trading_system.research.patterns import (
            HorizonOutcome, PatternReport, PatternStatus,
        )
        base = self._base_ledger()
        conf_base, _ = compute_confidence_v2(base)

        good = copy.deepcopy(base)
        pattern_to_evidence(good, PatternReport(
            status=PatternStatus.SUFFICIENT, match_count=40,
            primary=HorizonOutcome(horizon="1D", bars=5, n=40, positive=30,
                                   positive_rate=0.75, ci_low=0.6,
                                   ci_high=0.87),
            primary_horizon="1D", min_similarity=0.85))

        bad = copy.deepcopy(base)
        pattern_to_evidence(bad, PatternReport(
            status=PatternStatus.PATTERN_CONFLICTING, match_count=40,
            primary=HorizonOutcome(horizon="1D", bars=5, n=40, positive=19,
                                   positive_rate=0.475, ci_low=0.32,
                                   ci_high=0.63),
            primary_horizon="1D", min_similarity=0.85))

        conf_good, _ = compute_confidence_v2(good)
        conf_bad, _ = compute_confidence_v2(bad)
        assert conf_good > conf_bad
        # Additional evidence is NOT automatic confidence:
        assert conf_bad <= conf_base

class TestWalkForwardComparison:

    def test_all_configs_same_steps_and_honest_notes(self):
        df, news_result = news_agreeing_with_technicals()
        metrics = compare_strategies(
            df, news_result, symbol="NSE:RELIANCE-EQ",
            horizons={"1D": 5}, step=10, start=80, min_pattern_matches=8)
        assert set(metrics) == set(CONFIGS)
        assert metrics["V3"].n > 0
        assert metrics["V3"].n == metrics["V4_technical"].n
        assert metrics["V3"].n == metrics["V4_full"].n
        # honest small-sample reporting (synthetic demo scale is tiny)
        assert all("insufficient sample" in (m.note or "") or m.trades >= 30
                   for m in metrics.values())

    def test_runnable_without_news(self):
        df = historical_bullish_pattern(seed=21)
        metrics = compare_strategies(df, None, symbol="NSE:TCS-EQ",
                                     horizons={"1D": 5}, step=10, start=80,
                                     min_pattern_matches=8)
        assert metrics["V4_full"].n == metrics["V3"].n


class TestReplaySafetyNews:

    def test_future_news_never_used(self):
        df, _ = news_agreeing_with_technicals()
        future_event = make_raw("Reliance Industries wins order",
                                BASE_TIME + timedelta(days=30))
        result = NewsPipeline().run([future_event], as_of=BASE_TIME)
        ctx = build_news_context(result, as_of=BASE_TIME,
                                 target_ticker="RELIANCE")
        assert ctx.events == []
        assert ctx.news_status == "unavailable"

    def test_conflicting_news_flows_to_forecast_evidence(self):
        result = NewsPipeline().run(conflicting_sources(), as_of=BASE_TIME)
        ctx = build_news_context(result, as_of=BASE_TIME,
                                 target_ticker="RELIANCE")
        ledger = news_to_evidence(EvidenceLedgerV2(), ctx, as_of=BASE_TIME)
        assert any(i.availability == EvidenceAvailability.CONTRADICTORY
                   for i in ledger.items)


class TestV3Compatibility:

    def test_to_v3_context(self):
        df, news_result = news_agreeing_with_technicals()
        ctx = build_news_context(news_result, as_of=BASE_TIME,
                                 target_ticker="RELIANCE")
        v3 = to_v3_context(ctx)
        assert isinstance(v3, NewsContext)
        assert len(v3.events) == len(ctx.events)


class TestVoteAndMetrics:

    def test_empty_ledger_votes_neutral(self):
        assert _vote_direction(EvidenceLedgerV2()) == "neutral"

    def test_metrics_empty_note(self):
        m = _metrics("x", [])
        assert m.note == "no forecasts" and m.n == 0

    def test_metrics_small_sample_flagged(self):
        m = _metrics("x", [("bullish", 1.0)] * 5)
        assert "insufficient sample" in m.note


class TestAblationHarness:

    def test_six_configs_available(self):
        w = ablation_weights()
        assert len(w) == 6
        assert set(w) == {"A_technical", "B_plus_mtf", "C_plus_sector",
                          "D_plus_context", "E_plus_news", "F_full"}
