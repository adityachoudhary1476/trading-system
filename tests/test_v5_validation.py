"""V5 validation harness tests: metrics, bootstrap, improvement, calibration,
costs/slippage, verdict. All invariant-based on deterministic synthetic rows.
"""
from __future__ import annotations

import pathlib
import sys

import numpy as np
import pandas as pd
import pytest

_TESTS = pathlib.Path(__file__).resolve().parent
for _p in (_TESTS, _TESTS.parent / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from trading_system.research.v5_validation import (  # noqa: E402
    CostAssumptions, FinalVerdict, ImprovementVerdict, VerdictInput,
    bootstrap_ci, brier_ece, classify_verdict, compute_metrics,
    confidence_calibration, edge_survives_slippage, improvement_test,
    news_event_performance, pattern_similarity_analysis, regime_analysis,
    slippage_sensitivity, chronological_split,
)
from trading_system.research.run_registry import config_hash  # noqa: E402


def _rows(biases, rets):
    return list(zip(biases, rets))


class TestMetrics:

    def test_empty_note(self):
        m = compute_metrics("x", [])
        assert m.note == "no forecasts" and m.n == 0

    def test_correct_directional_accuracy(self):
        rows = _rows(["bullish"] * 5 + ["bearish"] * 5,
                     [1.0, 1.0, 1.0, 1.0, 1.0, -1.0, -1.0, -1.0, -1.0, -1.0])
        m = compute_metrics("t", rows)
        assert m.directional_accuracy == 1.0
        assert m.win_rate == 1.0

    def test_inaccurate_direction_reduced(self):
        good = _rows(["bullish"] * 8, [1.0] * 8)
        bad = _rows(["bullish"] * 8, [-1.0] * 8)
        assert compute_metrics("g", good).directional_accuracy > \
            compute_metrics("b", bad).directional_accuracy

    def test_cost_adjusted_lower(self):
        rows = _rows(["bullish"] * 40, [1.0] * 40)
        m = compute_metrics("t", rows, cost_pct=0.3)
        assert m.cost_adjusted_return < m.expectancy

    def test_small_sample_flagged(self):
        m = compute_metrics("t", _rows(["bullish"] * 5, [1.0] * 5))
        assert "insufficient sample" in m.note


class TestBootstrapAndStats:

    def test_bootstrap_deterministic(self):
        vals = [float(i % 7) for i in range(100)]
        a = bootstrap_ci(vals, seed=1)
        b = bootstrap_ci(vals, seed=1)
        assert a == b

    def test_bootstrap_reproducible_seed(self):
        vals = [float(i % 5) for i in range(200)]
        assert bootstrap_ci(vals, seed=3) == bootstrap_ci(vals, seed=3)

    def test_improvement_detects_positive(self):
        base = _rows(["bullish"] * 100, [0.5] * 100)
        comp = _rows(["bullish"] * 100, [2.0] * 100)
        res = improvement_test(base, comp, min_sample=30, seed=7)
        assert res.verdict == ImprovementVerdict.IMPROVEMENT

    def test_improvement_insufficient_sample(self):
        res = improvement_test(_rows([], []), _rows([], []), min_sample=30)
        assert res.verdict == ImprovementVerdict.INSUFFICIENT_SAMPLE


class TestCalibration:

    def test_bucket_counts(self):
        rows = [(float((i % 5) * 20) + 10.0, float(i % 2)) for i in range(100)]
        cc = confidence_calibration(rows)
        assert len(cc.buckets) == 5
        assert sum(b.count for b in cc.buckets) == 100

    def test_probability_status_analytical(self):
        cc = confidence_calibration([(50.0, 1.0)] * 20)
        assert cc.probability_status == "CONFIDENCE_REMAINS_ANALYTICAL_SCORE"

    def test_brier_ece(self):
        b, e = brier_ece([(50.0, 1.0), (50.0, 0.0)])
        assert b is not None and 0.0 <= b <= 1.0
        assert e is not None

class TestCostsAndSlippage:

    def test_cost_default_roundtrip(self):
        c = CostAssumptions()
        assert c.round_trip_bps() == pytest.approx(2 + 0.35 + 1.5 + 5 + 3)
        assert c.round_trip_pct() == pytest.approx(c.round_trip_bps() / 100)

    def test_cost_override(self):
        c = CostAssumptions(cost_bps=10.0)
        assert c.round_trip_bps() == 10.0

    def test_slippage_sensitivity_three_levels(self):
        rows = _rows(["bullish"] * 40, [0.5] * 40)
        out = slippage_sensitivity(rows)
        assert set(out) == {"LOW", "BASE", "HIGH"}
        assert out["LOW"].cost_adjusted_return > out["HIGH"].cost_adjusted_return
        assert len(out["LOW"].note) > 0 or out["LOW"].trades >= 30

    def test_edge_survives_only_if_positive(self):
        strong = _rows(["bullish"] * 40, [1.0] * 40)
        assert edge_survives_slippage(slippage_sensitivity(strong)) is True
        weak = _rows(["bullish"] * 40, [-0.1] * 40)
        assert edge_survives_slippage(slippage_sensitivity(weak)) is False


class TestVerdict:

    def _input(self, **kw):
        defaults = dict(
            improvement_V3=improvement_test(
                _rows(["bullish"] * 100, [0.5] * 100),
                _rows(["bullish"] * 100, [0.5] * 100), min_sample=30),
            improvement_V4=improvement_test(
                _rows(["bullish"] * 100, [0.5] * 100),
                _rows(["bullish"] * 100, [2.0] * 100), min_sample=30),
            net_expectancy_base=0.5, net_expectancy_compare=1.5,
            regime_dependent=False, edge_survives_costs=True,
            edge_survives_slippage=True, min_sample_met=True,
            oos_supported=True)
        defaults.update(kw)
        return VerdictInput(**defaults)

    def test_insufficient_data(self):
        assert classify_verdict(self._input(min_sample_met=False)) \
            == FinalVerdict.INSUFFICIENT_DATA

    def test_not_profitable_after_costs(self):
        assert classify_verdict(self._input(
            net_expectancy_base=-0.1, net_expectancy_compare=-0.1)) \
            == FinalVerdict.NOT_PROFITABLE_AFTER_COSTS

    def test_regression(self):
        v = self._input()
        v.improvement_V4 = improvement_test(
            _rows(["bullish"] * 100, [2.0] * 100),
            _rows(["bullish"] * 100, [0.5] * 100), min_sample=30)
        assert classify_verdict(v) == FinalVerdict.REGRESSION

    def test_strong_evidence(self):
        assert classify_verdict(self._input()) == FinalVerdict.STRONG_EVIDENCE

    def test_fragile_without_slippage(self):
        assert classify_verdict(self._input(edge_survives_slippage=False)) \
            == FinalVerdict.FRAGILE

    def test_deterministic(self):
        a = classify_verdict(self._input())
        b = classify_verdict(self._input())
        assert a == b


class TestAnalysisHelpers:

    def test_regime_analysis_groups(self):
        rows = [{"regime": "trending_up", "config": "V3", "bias": "bullish",
                 "realized_return": 1.0} for _ in range(35)]
        rows += [{"regime": "range", "config": "V3", "bias": "neutral",
                  "realized_return": 0.1} for _ in range(35)]
        out = regime_analysis(rows)
        assert "trending_up" in out and "range" in out

    def test_news_event_performance_sorted(self):
        events = [{"event_type": "earnings", "realized_return": 1.0,
                   "mfe": 2.0, "mae": -1.0} for _ in range(5)]
        events += [{"event_type": "regulatory", "realized_return": -1.0,
                    "mfe": 0.5, "mae": -2.0} for _ in range(5)]
        out = news_event_performance(events)
        by = {a.event_type: a for a in out}
        assert by["earnings"].expectancy > by["regulatory"].expectancy

    def test_pattern_similarity_reports_buckets(self):
        matches = [{"similarity": s, "realized_return": (1.0 if s > 0.85 else 0.1),
                    "mfe": 2.0, "mae": -1.0}
                   for s in (0.72, 0.77, 0.82, 0.87, 0.92, 0.88)]
        reports, monotonic = pattern_similarity_analysis(matches)
        assert len(reports) == 5
        assert sum(r.count for r in reports) == len(matches)

    def test_chronological_split_ordered(self):
        idx = pd.date_range("2024-01-01", periods=200, freq="1D", tz="UTC")
        windows = chronological_split(idx, n_windows=3, train_frac=0.5)
        assert len(windows) >= 1
        for ts, te, vs, ve in windows:
            assert ts <= te < vs <= ve

    def test_config_hash_deterministic(self):
        a = config_hash({"x": 1, "y": {"z": 2}})
        b = config_hash({"x": 1, "y": {"z": 2}})
        c = config_hash({"x": 1, "y": {"z": 3}})
        assert a == b and a != c
