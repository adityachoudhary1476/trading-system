"""Tests for SQLite storage, especially idempotent (no-duplicate) insertion."""
from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from trading_system.storage.database import MarketStore, OHLCVRecord, init_db, Base


@pytest.fixture
def store(tmp_path) -> MarketStore:
    db = tmp_path / "test.db"
    return MarketStore(f"sqlite:///{db}")


def _rows(n=5, provider="binance"):
    idx = pd.date_range("2024-01-01", periods=n, freq="1d", tz="UTC")
    out = []
    for i, ts in enumerate(idx):
        out.append(
            {
                "symbol": "BTCUSDT",
                "timeframe": "1d",
                "timestamp": ts,
                "open": 100 + i,
                "high": 101 + i,
                "low": 99 + i,
                "close": 100.5 + i,
                "volume": 1000 + i,
                "provider": provider,
            }
        )
    return out


def test_insert_counts_new_rows(store):
    n = store.upsert_many(_rows(5))
    assert n == 5
    assert store.count("BTCUSDT", "1d") == 5


def test_idempotent_reinsert_inserts_zero(store):
    store.upsert_many(_rows(5))
    n = store.upsert_many(_rows(5))  # identical
    assert n == 0
    assert store.count("BTCUSDT", "1d") == 5


def test_partial_overlap_only_new_rows(store):
    store.upsert_many(_rows(3))
    # Re-insert first 3 plus 2 new.
    n = store.upsert_many(_rows(5))
    assert n == 2
    assert store.count("BTCUSDT", "1d") == 5


def test_load_returns_sorted_ohlcv(store):
    store.upsert_many(_rows(5))
    df = store.load("BTCUSDT", "1d")
    assert not df.empty
    assert list(df.columns[:5]) == ["open", "high", "low", "close", "volume"]
    # Index must be monotonic increasing.
    assert df.index.is_monotonic_increasing


def test_naive_timestamp_normalized_to_utc(store):
    rows = _rows(2)
    rows[0]["timestamp"] = dt.datetime(2024, 1, 1)  # naive -> treated as UTC
    store.upsert_many(rows)
    df = store.load("BTCUSDT", "1d")
    assert df.index[0].tzinfo is not None


def test_atomic_recovery_commit_persists_candles_and_cursor(store):
    rows = _rows(2, provider="upstox")
    latest = rows[-1]["timestamp"]
    assert store.commit_recovery("BTCUSDT", "1d", rows, latest) == 2
    point = store.get_recovery_point("BTCUSDT", "1d")
    assert point is not None
    assert point["latest_closed_candle"] == latest.to_pydatetime()
    assert point["recovery_status"] == "complete"


def test_recovery_commit_historical_data_wins_existing_candle(store):
    original = _rows(1, provider="upstox")[0]
    store.upsert_many([original])
    corrected = dict(original)
    corrected["close"] = 999.0
    assert store.commit_recovery("BTCUSDT", "1d", [corrected], original["timestamp"]) == 0
    assert float(store.load("BTCUSDT", "1d")["close"].iloc[0]) == 999.0


def test_recovery_cursor_does_not_advance_on_status_failure(store):
    store.record_recovery_status("BTCUSDT", "1d", "degraded", "provider unavailable")
    point = store.get_recovery_point("BTCUSDT", "1d")
    assert point is not None
    assert point["latest_closed_candle"] is None
    assert point["recovery_status"] == "degraded"
