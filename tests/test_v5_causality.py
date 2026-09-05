"""V5 causality tests: no-lookahead in snapshots, HTF closed candles,
future news/options/context exclusion, replay audit."""
from __future__ import annotations

import pathlib
import sys
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

_TESTS = pathlib.Path(__file__).resolve().parent
for _p in (_TESTS, _TESTS.parent / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from trading_system.research.causal_snapshot import (  # noqa: E402
    CausalSnapshotBuilder, closed_htf_candles,
)
from trading_system.research.v5_validation import (  # noqa: E402
    audit_snapshot_causality, replay_v5_dataset,
)
from fixtures.v4_fixtures import (  # noqa: E402
    BASE_TIME, make_raw, historical_bullish_pattern,
)


def _intraday() -> pd.DataFrame:
    rng = np.random.default_rng(11)
    idx = pd.date_range("2026-01-02 09:15", periods=80, freq="15min", tz="UTC")
    c = 100 + np.cumsum(rng.normal(0.05, 0.2, 80))
    return pd.DataFrame({"open": c, "high": c + 0.1, "low": c - 0.1,
                         "close": c,
                         "volume": rng.integers(100, 1000, 80).astype(float)},
                        index=idx)


class TestClosedHtfCandles:

    def test_in_progress_candle_excluded(self):
        """At 10:17 the 1h candle ending 11:00 must NOT be visible."""
        df = _intraday()
        as_of = pd.Timestamp("2026-01-02 10:17", tz="UTC")
        htf = closed_htf_candles(df, "1h", as_of)
        assert len(htf) == 1  # only the 09:00-10:00 closed candle
        assert htf.index[0] + pd.Timedelta("1h") <= as_of

    def test_no_as_of_keeps_only_completed(self):
        d = _intraday()
        htf = closed_htf_candles(d, "1h")
        # every retained candle must be fully closed by the last input bar
        last_input = d.index[-1]
        assert all(idx + pd.Timedelta("1h") <= last_input for idx in htf.index)
        full = d.resample("1h").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum"}).dropna(subset=["open", "close"])
        assert len(htf) < len(full)  # at least the in-progress one dropped

    def test_5m_resample_valid_offset(self):
        assert len(closed_htf_candles(_intraday(), "5m")) >= 1

    def test_exact_boundary_candle_available(self):
        d = _intraday()
        htf = closed_htf_candles(d, "1h", pd.Timestamp("2026-01-02 11:00",
                                                       tz="UTC"))
        assert len(htf) == 2  # 09-10 and 10-11 closed candles


class TestNewsCausality:

    def test_future_news_excluded(self):
        from trading_system.research.news_intelligence import NewsNormalizer
        builder = CausalSnapshotBuilder()
        norm = NewsNormalizer()
        df = historical_bullish_pattern(seed=13)
        as_of = BASE_TIME + timedelta(days=1)
        snap = builder.snapshot("NSE:X-EQ", "1d", df, as_of)
        future = norm.normalize(make_raw("Reliance wins order",
                                         BASE_TIME + timedelta(days=30)))
        past = norm.normalize(make_raw("Reliance wins order",
                                       BASE_TIME - timedelta(hours=2)))
        more_past = norm.normalize(make_raw("Macro event",
                                            BASE_TIME - timedelta(days=1)))
        snap = builder.with_news(snap, [future, past, more_past])
        assert all(e.published_at <= as_of for e in snap.news)
        assert len(snap.news) == 2

    def test_empty_news_unavailable(self):
        builder = CausalSnapshotBuilder()
        df = historical_bullish_pattern(seed=13)
        snap = builder.snapshot("NSE:X-EQ", "1d", df,
                                pd.Timestamp("2026-05-06", tz="UTC"))
        snap = builder.with_news(snap, [])
        assert snap.data_availability["NEWS"] == "UNAVAILABLE"

    def test_ambiguous_timestamp_not_assumed(self):
        builder = CausalSnapshotBuilder()
        df = historical_bullish_pattern(seed=13)
        snap = builder.snapshot("NSE:X-EQ", "1d", df,
                                pd.Timestamp("2026-05-06", tz="UTC"))

        class _Amb:
            published_at = None
            discovered_at = None
        assert builder.with_news(snap, [_Amb()]).news == []

class TestOptionsCausality:

    def test_future_option_rows_excluded(self):
        builder = CausalSnapshotBuilder()
        df = historical_bullish_pattern(seed=13)
        as_of = pd.Timestamp("2026-05-06", tz="UTC")
        snap = builder.snapshot("NSE:X-EQ", "1d", df, as_of)
        rows = [
            {"strike": 100, "snapshot_ts": as_of - timedelta(days=1)},
            {"strike": 110, "snapshot_ts": as_of + timedelta(days=1)},
            {"strike": 120},  # no timestamp -> not assumed
        ]
        snap = builder.with_options(snap, rows)
        assert len(snap.options) == 1 and snap.options[0]["strike"] == 100


class TestContextCausality:

    def test_context_only_prior_and_fresh(self):
        builder = CausalSnapshotBuilder(max_context_staleness=timedelta(days=1))
        df = historical_bullish_pattern(seed=13)
        as_of = pd.Timestamp("2026-05-06", tz="UTC")
        snap = builder.snapshot("NSE:X-EQ", "1d", df, as_of)
        hist = {
            "breadth": [
                (as_of - timedelta(days=2), "stale_obj"),
                (as_of - timedelta(hours=2), "fresh_obj"),
                (as_of + timedelta(days=1), "future_obj"),
            ],
            "vix": [],
        }
        snap = builder.with_context_history(snap, hist)
        assert snap.data_availability["BREADTH"] == "AVAILABLE"
        assert snap.data_availability["VIX"] == "UNAVAILABLE"
        assert snap.context.breadth == "fresh_obj"


class TestSnapshotAudit:

    def test_audit_reports_news_violations(self):
        from trading_system.research.news_intelligence import NewsNormalizer
        builder = CausalSnapshotBuilder()
        df = historical_bullish_pattern(seed=13)
        as_of = pd.Timestamp("2026-05-06", tz="UTC")
        snap = builder.snapshot("NSE:X-EQ", "1d", df, as_of)
        future = make_raw("Reliance wins order", BASE_TIME + timedelta(days=30))
        snap.news = [NewsNormalizer().normalize(future)]
        violations = audit_snapshot_causality(snap, as_of)
        assert any(v.category == "news" for v in violations)

    def test_clean_snapshot_zero_violations(self):
        builder = CausalSnapshotBuilder()
        df = historical_bullish_pattern(seed=13)
        as_of = pd.Timestamp("2026-05-06", tz="UTC")
        snap = builder.snapshot("NSE:X-EQ", "1d", df, as_of)
        assert audit_snapshot_causality(snap, as_of) == []


class TestReplayAudit:

    def test_replay_v5_zero_violations(self):
        df = historical_bullish_pattern(seed=13)
        rows, audit = replay_v5_dataset({"NSE:SYNTH-EQ": df}, step=10,
                                        start=60, horizon_bars=5)
        assert audit.lookahead_violations == 0
        assert len(rows) >= 5

    def test_replay_has_all_configs(self):
        df = historical_bullish_pattern(seed=13)
        rows, _ = replay_v5_dataset({"NSE:SYNTH-EQ": df}, step=10, start=60,
                                    horizon_bars=5)
        assert {"V2", "V3", "V4_technical", "V4_news",
                "V4_full"}.issubset({r.config for r in rows})
