"""V4 historical pattern engine tests — SYNTHETIC_TEST fixtures only."""
from __future__ import annotations

import pathlib
import sys
from datetime import timedelta

_TESTS = pathlib.Path(__file__).resolve().parent
for _p in (_TESTS, _TESTS.parent / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from fixtures.v4_fixtures import (  # noqa: E402
    historical_bullish_pattern, historical_bearish_pattern,
    historical_mixed_pattern, insufficient_history_for_patterns,
)
from trading_system.research.intelligence import (  # noqa: E402
    FeatureEngine, classify_regime,
)
from trading_system.research.patterns import (  # noqa: E402
    ABLATION_CONFIGS, FeatureWeights, HistoricalPatternEngine, LibraryEntry,
    PatternStatus, ablation_weights, build_pattern_report,
    fingerprint_from_features, pattern_to_evidence,
)
from trading_system.research.intelligence_v3 import (  # noqa: E402
    EvidenceAvailability, EvidenceCategory, EvidenceLedgerV2,
)
from trading_system.research.market_context import DataQualityTier  # noqa: E402

ENG = HistoricalPatternEngine()
FE = FeatureEngine(lookback=60)


def _fp_at(df, i):
    ts = df.index[i]
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    feats = FE.features_at(df, ts)
    regime = classify_regime(feats)
    return fingerprint_from_features(feats, regime, timestamp=ts.to_pydatetime()
                                     if hasattr(ts, "to_pydatetime") else ts,
                                     instrument="NSE:TEST")


class TestFingerprint:

    def test_dims_in_unit_range_or_none(self):
        fp = _fp_at(historical_bullish_pattern(seed=13), 100)
        vals = [v for v in fp.dims.values() if v is not None]
        assert len(vals) > 5
        assert all(0.0 <= v <= 1.0 for v in vals)

    def test_missing_stay_none(self):
        fp = _fp_at(historical_bullish_pattern(seed=13), 100)
        assert fp.dims.get("news_sent") is None
        assert fp.dims.get("mtf_1d") is None
        assert fp.dims.get("adv_pct") is None

    def test_deterministic(self):
        df = historical_bullish_pattern(seed=13)
        assert _fp_at(df, 100).dims == _fp_at(df, 100).dims

    def test_raw_prices_not_compared(self):
        """Normalization: NIFTY 20000 vs 25000 style — dims are relative."""
        df = historical_bullish_pattern(seed=13)
        fp_a = _fp_at(df, 100)
        scaled = df.copy()
        scaled[["open", "high", "low", "close"]] = \
            scaled[["open", "high", "low", "close"]] * 1.25
        fp_b = _fp_at(scaled, 100)
        common = [d for d in fp_a.dims
                  if fp_a.dims[d] is not None and fp_b.dims.get(d) is not None]
        diffs = [abs(fp_a.dims[d] - fp_b.dims[d]) for d in common]
        assert max(diffs) < 0.2  # same structure, different absolute level


class TestNormalizer:

    def test_output_stays_in_range_and_missing_stays_missing(self):
        df = historical_bullish_pattern(seed=13)
        as_of = df.index[130].to_pydatetime()
        lib = ENG.build_library(df, start=60, step=2, as_of=as_of)
        norm = ENG and __import__("trading_system.research.patterns",
                                  fromlist=["FingerprintNormalizer"])
        from trading_system.research.patterns import FingerprintNormalizer
        fitted = FingerprintNormalizer().fit(lib)
        out = fitted.transform(_fp_at(df, 129))
        vals = [v for v in out.dims.values() if v is not None]
        assert all(0.0 <= v <= 1.0 for v in vals)
        assert out.dims.get("news_sent") is None

class TestSimilarity:

    def test_identical_is_one(self):
        df = historical_bullish_pattern(seed=13)
        fp = _fp_at(df, 100)
        assert ENG.similarity_engine.similarity(fp, fp) == 1.0

    def test_opposite_states_less_similar(self):
        bull = _fp_at(historical_bullish_pattern(seed=13), 110)
        bear = _fp_at(historical_bearish_pattern(seed=14), 110)
        bull2 = _fp_at(historical_bullish_pattern(seed=13), 115)
        sim_opposite = ENG.similarity_engine.similarity(bull, bear)
        sim_same = ENG.similarity_engine.similarity(bull, bull2)
        assert sim_same > sim_opposite

    def test_deterministic(self):
        bull = _fp_at(historical_bullish_pattern(seed=13), 110)
        bear = _fp_at(historical_bearish_pattern(seed=14), 110)
        s1 = ENG.similarity_engine.similarity(bull, bear)
        s2 = ENG.similarity_engine.similarity(bull, bear)
        assert s1 == s2


class TestHistoricalFiltering:

    def test_future_entries_excluded_by_api(self):
        df = historical_bullish_pattern(seed=13)
        as_of = df.index[120].to_pydatetime()
        lib = ENG.build_library(df, start=60, step=1, as_of=as_of)
        future = LibraryEntry(_fp_at(df, 140), 140)  # AFTER as_of
        matches = ENG.find_matches(_fp_at(df, 119), lib + [future],
                                   as_of=as_of, min_similarity=0.0)
        assert matches
        assert all(e.fingerprint.timestamp < as_of for e, _s in matches)
        assert all(e.bar_index < 120 for e, _s in matches)

    def test_build_library_never_includes_as_of(self):
        df = historical_bullish_pattern(seed=13)
        as_of = df.index[100].to_pydatetime()
        lib = ENG.build_library(df, start=60, step=1, as_of=as_of)
        assert lib and all(e.fingerprint.timestamp < as_of for e in lib)


class TestNoLookaheadOutcomeWindows:

    def test_unclosed_outcome_windows_excluded(self):
        df = historical_bullish_pattern(seed=13)
        as_of = df.index[120].to_pydatetime()
        horizon = timedelta(days=5)
        lib = ENG.build_library(df, start=60, step=1, as_of=as_of)
        matches = ENG.find_matches(_fp_at(df, 119), lib, as_of=as_of,
                                   min_similarity=0.0, horizon=horizon)
        assert all(e.fingerprint.timestamp + horizon < as_of
                   for e, _s in matches)

    def test_walk_forward_boundaries(self):
        """Pattern library for T uses only bars whose outcomes closed by T."""
        df = historical_bullish_pattern(seed=13)
        n = len(df)
        horizon = timedelta(days=5)
        for t in (90, 110, 130):
            if t + 10 >= n:
                continue
            as_of = df.index[t].to_pydatetime()
            lib = ENG.build_library(df, start=60, step=1,
                                    as_of=as_of - horizon)
            assert all(e.fingerprint.timestamp + horizon < as_of for e in lib)

class TestMatchThreshold:

    def test_below_threshold_no_matches(self):
        df = historical_bullish_pattern(seed=13)
        as_of = df.index[120].to_pydatetime()
        lib = ENG.build_library(df, start=60, step=1, as_of=as_of)
        matches = ENG.find_matches(_fp_at(df, 119), lib, as_of=as_of,
                                   min_similarity=1.01)
        assert matches == []


class TestOutcomeAnalysis:

    def _report(self, df, t, min_matches=8, min_similarity=0.85):
        as_of = df.index[t].to_pydatetime()
        horizon = timedelta(days=5)
        lib = ENG.build_library(df, start=60, step=1, as_of=as_of - horizon)
        matches = ENG.find_matches(_fp_at(df, t - 1), lib, as_of=as_of,
                                   min_similarity=min_similarity,
                                   horizon=horizon)
        return build_pattern_report(matches, df, horizons={"1D": 5},
                                    min_matches=min_matches,
                                    min_similarity=min_similarity), matches

    def test_bullish_state_positive_rate(self):
        report, matches = self._report(historical_bullish_pattern(seed=13), 125)
        assert report.primary is not None
        if report.primary.n >= 8:
            assert report.primary.positive_rate > 0.5
            assert report.status in (PatternStatus.SUFFICIENT,
                                     PatternStatus.PATTERN_WEAK)

    def test_mfe_ge_return(self):
        report, _m = self._report(historical_bullish_pattern(seed=13), 125)
        if report.primary and report.primary.n:
            assert report.primary.avg_mfe >= report.primary.avg_return
            assert report.primary.avg_mae <= report.primary.avg_return

    def test_deterministic_results(self):
        r1, _ = self._report(historical_bullish_pattern(seed=13), 125)
        r2, _ = self._report(historical_bullish_pattern(seed=13), 125)
        assert r1.status == r2.status
        assert r1.primary.positive_rate == r2.primary.positive_rate
        assert r1.match_count == r2.match_count

    def test_insufficient_matches_status(self):
        df = insufficient_history_for_patterns()
        as_of = df.index[30].to_pydatetime()
        lib = ENG.build_library(df, start=15, step=1, as_of=as_of)
        matches = ENG.find_matches(_fp_at(df, 29), lib, as_of=as_of,
                                   min_similarity=0.0)
        report = build_pattern_report(matches, df, horizons={"1D": 5},
                                      min_matches=500)
        assert report.status == PatternStatus.INSUFFICIENT_MATCHES
        assert any("sample too small" in w for w in report.warnings)

    def test_regime_breakdown_counts_sum(self):
        report, _m = self._report(historical_bullish_pattern(seed=13), 125)
        if report.primary and report.primary.n:
            assert sum(v["count"] for v in
                       report.regime_breakdown.values()) == report.primary.n

    def test_reliability_labeling(self):
        """Never claims probability; reports historical frequency + CI."""
        report, _m = self._report(historical_bullish_pattern(seed=13), 125)
        assert any("not a probability" in w for w in report.warnings)
        if report.primary and report.primary.n:
            assert 0.0 <= report.primary.ci_low <= report.primary.ci_high <= 1.0

class TestPatternEvidence:

    def test_sufficient_maps_to_supported(self):
        from trading_system.research.patterns import HorizonOutcome, PatternReport
        report = PatternReport(
            status=PatternStatus.SUFFICIENT, match_count=40,
            primary=HorizonOutcome(horizon="1D", bars=5, n=40, positive=28,
                                   positive_rate=0.7, ci_low=0.55,
                                   ci_high=0.82),
            primary_horizon="1D", min_similarity=0.85)
        ledger = pattern_to_evidence(EvidenceLedgerV2(), report)
        item = [i for i in ledger.items
                if i.category == EvidenceCategory.HISTORICAL_PATTERN][0]
        assert item.availability == EvidenceAvailability.SUPPORTED
        assert item.direction == "bullish"
        assert "Historical matches: 40" in item.explanation

    def test_insufficient_maps_to_unavailable(self):
        report = build_pattern_report([], historical_bullish_pattern(seed=13),
                                      horizons={"1D": 5}, min_matches=15)
        ledger = pattern_to_evidence(EvidenceLedgerV2(), report)
        item = [i for i in ledger.items
                if i.category == EvidenceCategory.HISTORICAL_PATTERN][0]
        assert item.availability == EvidenceAvailability.UNAVAILABLE

    def test_conflicting_maps_to_contradictory(self):
        from trading_system.research.patterns import HorizonOutcome, PatternReport
        report = PatternReport(
            status=PatternStatus.PATTERN_CONFLICTING, match_count=30,
            primary=HorizonOutcome(horizon="1D", bars=5, n=30, positive=15,
                                   positive_rate=0.5, ci_low=0.33,
                                   ci_high=0.67),
            primary_horizon="1D", min_similarity=0.85)
        ledger = pattern_to_evidence(EvidenceLedgerV2(), report)
        item = [i for i in ledger.items
                if i.category == EvidenceCategory.HISTORICAL_PATTERN][0]
        assert item.availability == EvidenceAvailability.CONTRADICTORY


class TestAblationWeights:

    def test_all_configs_present(self):
        weights = ablation_weights()
        assert set(weights) == set(ABLATION_CONFIGS)
        assert len(weights) == 6

    def test_masks_zero_correct_groups(self):
        w = ablation_weights()
        assert w["A_technical"].news == 0.0
        assert w["A_technical"].trend > 0.0
        assert w["E_plus_news"].news > 0.0
        assert w["E_plus_news"].sector == 0.0
        assert w["F_full"].options > 0.0
