"""Tests for the greeks decision-layer counters (decision D6).

The layer fails open (D1), so a broken greeks feed looks exactly like a
healthy one from the outside: no exception, no refused trade, nothing to
alert on. These tests are that guarantee in executable form — counters must
bucket correctly, snapshots must be stably persistable, and a window where
greeks were unusable must produce a reportable fallback rate rather than
silence. Pure in-memory accounting: no I/O, no clock, PAPER only.
"""
from __future__ import annotations

import json

from trading_system.autonomous.greeks_metrics import (
    GreeksMetrics,
    default_metrics,
    reset_default_metrics,
)


class TestRecordEvaluated:
    def test_available_and_skipped_are_separate_counters(self):
        m = GreeksMetrics()
        m.record_evaluated(True)
        m.record_evaluated(True)
        m.record_evaluated(False, "no_iv")
        snap = m.snapshot()
        assert snap["greeks_evaluated"] == 2
        assert snap["greeks_skipped"] == 1

    def test_reasons_bucket_and_accumulate(self):
        m = GreeksMetrics()
        for reason in ("no_iv", "NO_IV", " no iv ", "stale_quote", "Stale Quote"):
            m.record_evaluated(False, reason)
        snap = m.snapshot()
        assert snap["greeks_fallback_reason"] == {"no_iv": 3, "stale_quote": 2}
        assert snap["greeks_skipped"] == 5

    def test_missing_reason_lands_in_unknown_bucket(self):
        m = GreeksMetrics()
        m.record_evaluated(False)
        m.record_evaluated(False, None)
        m.record_evaluated(False, "   ")
        assert m.snapshot()["greeks_fallback_reason"] == {"unknown": 3}

    def test_reason_on_usable_greeks_is_not_a_fallback(self):
        m = GreeksMetrics()
        m.record_evaluated(True, "gamma_missing")
        snap = m.snapshot()
        assert snap["greeks_evaluated"] == 1
        assert snap["greeks_fallback_reason"] == {}


class TestShadowAndSelectionCounters:
    def test_shadow_counts_only_disagreements(self):
        m = GreeksMetrics()
        m.record_shadow_verdict(False)
        m.record_shadow_verdict(False)
        m.record_shadow_verdict(True)
        assert m.snapshot()["shadow_verdict_diff"] == 1

    def test_strike_buckets_by_selector_and_normalises(self):
        m = GreeksMetrics()
        m.record_strike_selected("legacy_offset")
        m.record_strike_selected("Legacy Offset")
        m.record_strike_selected("target_delta")
        assert m.snapshot()["strike_selected_by"] == {
            "legacy_offset": 2,
            "target_delta": 1,
        }

    def test_sizing_buckets_by_clamp_including_none(self):
        m = GreeksMetrics()
        m.record_sizing("max_position_allocation_pct")
        m.record_sizing(None)
        m.record_sizing(None)
        m.record_sizing("max_contracts_per_leg")
        assert m.snapshot()["sizing_bound_by"] == {
            "max_contracts_per_leg": 1,
            "max_position_allocation_pct": 1,
            "none": 2,
        }

    def test_limit_blocked_buckets_by_limit_name(self):
        m = GreeksMetrics()
        m.record_limit_blocked("max_net_delta")
        m.record_limit_blocked("Max Net Delta")
        assert m.snapshot()["greek_limit_blocked"] == {"max_net_delta": 2}


class TestSnapshot:
    def test_two_calls_are_equal_and_json_serialisable(self):
        m = GreeksMetrics()
        m.record_evaluated(False, "no_iv")
        m.record_evaluated(True)
        m.record_shadow_verdict(True)
        m.record_strike_selected("target_delta")
        first = m.snapshot()
        second = m.snapshot()
        assert first == second
        assert json.loads(json.dumps(first)) == first

    def test_top_level_keys_are_in_sorted_order(self):
        m = GreeksMetrics()
        snap = m.snapshot()
        assert list(snap.keys()) == [
            "greek_limit_blocked",
            "greeks_evaluated",
            "greeks_fallback_reason",
            "greeks_skipped",
            "shadow_verdict_diff",
            "sizing_bound_by",
            "strike_selected_by",
        ]
        assert list(snap.keys()) == sorted(snap.keys())

    def test_nested_keys_are_sorted_too(self):
        m = GreeksMetrics()
        for reason in ("zzz", "aaa", "mmm"):
            m.record_evaluated(False, reason)
        reasons = m.snapshot()["greeks_fallback_reason"]
        assert list(reasons) == ["aaa", "mmm", "zzz"]

    def test_snapshot_is_a_plain_dict_of_primitives(self):
        snap = GreeksMetrics().snapshot()
        assert type(snap) is dict
        for key, value in snap.items():
            assert type(key) is str
            assert type(value) in (int, dict)


class TestFallbackRate:
    def test_zero_with_no_data_and_no_division_by_zero(self):
        assert GreeksMetrics().fallback_rate() == 0.0

    def test_high_fallback_window_produces_a_reportable_rate(self):
        m = GreeksMetrics()
        for _ in range(10):
            m.record_evaluated(True)
        for _ in range(90):
            m.record_evaluated(False, "unusable_quote")
        rate = m.fallback_rate()
        assert rate == 0.9
        assert 0.0 < rate <= 1.0

    def test_completely_broken_window_still_signals(self):
        m = GreeksMetrics()
        for _ in range(5):
            m.record_evaluated(False, "no_chain")
        assert m.fallback_rate() == 1.0

    def test_half_and_fully_healthy_windows(self):
        half = GreeksMetrics()
        half.record_evaluated(True)
        half.record_evaluated(False, "no_iv")
        assert half.fallback_rate() == 0.5
        healthy = GreeksMetrics()
        healthy.record_evaluated(True)
        assert healthy.fallback_rate() == 0.0


class TestResetAndSharedInstance:
    def test_reset_clears_every_counter(self):
        m = GreeksMetrics()
        m.record_evaluated(True)
        m.record_evaluated(False, "no_iv")
        m.record_shadow_verdict(True)
        m.record_strike_selected("target_delta")
        m.record_sizing("risk_budget")
        m.record_limit_blocked("max_net_delta")
        m.reset()
        assert m.snapshot() == GreeksMetrics().snapshot()
        assert m.fallback_rate() == 0.0

    def test_default_metrics_is_resettable(self):
        reset_default_metrics()
        try:
            default_metrics().record_evaluated(False, "no_iv")
            assert default_metrics().snapshot()["greeks_skipped"] == 1
        finally:
            reset_default_metrics()
        assert default_metrics().snapshot() == GreeksMetrics().snapshot()
