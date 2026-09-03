"""Forecast ledger (Phase 16 foundation) tests.

Guarantees:
- forecasts persist with full market context and can be resolved later,
- resolving computes hit / expected-move containment from REAL return inputs,
- summarize_calibration NEVER claims calibration (explicit status labels),
- record_from_analysis integrates with MarketIntelligenceEngine.analyze().
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine

from trading_system.research.forecast_ledger import (
    ForecastStore,
    MIN_RESOLVED_FOR_CALIBRATION,
)
from trading_system.research.intelligence import MarketIntelligenceEngine


@pytest.fixture()
def store():
    return ForecastStore(create_engine("sqlite://"))


def _df(n=120, drift=0.4, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    closes = 100 + np.cumsum(rng.normal(drift, 0.3, n))
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame(
        {"open": opens, "high": np.maximum(closes, opens) + 0.2,
         "low": np.minimum(closes, opens) - 0.2, "close": closes,
         "volume": rng.integers(1_000_000, 3_000_000, n).astype(float)},
        index=idx,
    )


def test_record_and_list_roundtrip(store):
    rec = store.record_forecast(
        instrument="NSE:NIFTY 50-INDEX", timeframe="1d", bias="bearish",
        confidence=0.72, horizon="short_term", forecast="evidence-ledger test",
        expected_move_lower_pct=-1.4, expected_move_upper_pct=-0.8,
        invalidation="Sustained move above 24300",
        selected_option={"strike": 23600, "option_type": "PE", "score": 84},
        market_state={"session": "regular", "freshness": "live"},
    )
    got = store.list_forecasts(instrument="NSE:NIFTY 50-INDEX")
    assert len(got) == 1 and got[0].id == rec.id
    assert got[0].bias == "bearish"
    assert got[0].confidence == 0.72
    assert got[0].resolved is False
    assert got[0].selected_option["strike"] == 23600
    assert store.list_forecasts(instrument="NSE:SBIN-EQ") == []


def test_resolve_computes_hit_and_expected_move_containment(store):
    bull = store.record_forecast(instrument="A", timeframe="1d", bias="bullish",
                                 confidence=0.6, horizon="short_term",
                                 expected_move_lower_pct=-1.0, expected_move_upper_pct=2.0)
    bear = store.record_forecast(instrument="A", timeframe="1d", bias="bearish",
                                 confidence=0.6, horizon="short_term",
                                 expected_move_lower_pct=-2.0, expected_move_upper_pct=0.5)
    neutral = store.record_forecast(instrument="A", timeframe="1d", bias="neutral",
                                    confidence=0.4, horizon="intraday")

    r1 = store.resolve_forecast(bull.id, actual_return_pct=1.2)
    assert r1.hit is True and r1.within_expected_move is True and r1.resolved is True
    r2 = store.resolve_forecast(bear.id, actual_return_pct=-3.0)
    assert r2.hit is True and r2.within_expected_move is False
    r3 = store.resolve_forecast(neutral.id, actual_return_pct=-0.5)
    assert r3.hit is False  # neutral "hit" only when price does not move
    assert store.list_forecasts(resolved=True) and all(
        r.resolved for r in store.list_forecasts(resolved=True))


def test_summarize_calibration_never_claims_calibration(store):
    fid = store.record_forecast(instrument="B", timeframe="1d", bias="bullish",
                                confidence=0.66, horizon="short_term")
    store.resolve_forecast(fid, actual_return_pct=0.8)
    s = store.summarize_calibration(instrument="B")
    assert s["calibration_status"] == "uncalibrated_insufficient_sample"
    assert s["resolved_count"] == 1
    assert s["directional_hit_rate"] == 1.0
    assert "NOT a probability" in s["note"]


def test_record_from_analysis_integration(store):
    """Engine.analyze() output must be recordable end-to-end."""
    eng = MarketIntelligenceEngine(lookback=60)
    analysis = eng.analyze("NSE:SBIN-EQ", "1d", _df())
    rec = store.record_from_analysis(analysis)
    if rec is not None:  # engine may legitimately decline to forecast
        assert rec.instrument == "NSE:SBIN-EQ"
        assert rec.bias in ("bullish", "bearish", "neutral")
        assert 0.0 <= rec.confidence <= 1.0
        assert rec.horizon in ("intraday", "short_term", "swing", "unknown")
        assert rec.resolved is False


def test_min_resolved_threshold_is_meaningful():
    """The threshold exists so calibration claims stay honest."""
    assert MIN_RESOLVED_FOR_CALIBRATION >= 30
