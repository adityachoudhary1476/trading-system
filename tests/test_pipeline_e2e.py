"""End-to-end ingestion test using a STUB provider (no network).

Proves the pipeline: request -> validate -> normalize -> store -> report,
and that duplicate ingestion is a no-op. Uses a fake provider so the test is
deterministic and offline.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from trading_system.data.pipeline import IngestionPipeline, IngestionResult
from trading_system.storage.database import MarketStore
from trading_system.config import Settings


class StubProvider:
    name = "stub"

    def __init__(self):
        self.calls = 0

    @property
    def is_real_time(self):
        return False

    def get_historical(self, symbol, timeframe, limit, start=None, end=None):
        self.calls += 1
        idx = pd.date_range("2024-01-01", periods=limit, freq="1d", tz="UTC")
        rng = __import__("numpy").random.default_rng(42)
        close = 100 + rng.normal(0, 1, limit).cumsum()
        return pd.DataFrame(
            {
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": rng.integers(100, 1000, limit).astype(float),
            },
            index=idx,
        )

    def get_latest_price(self, symbol):
        return 100.0


def _make_pipeline(tmp_path):
    db = tmp_path / "e2e.db"
    store = MarketStore(f"sqlite:///{db}")
    pipe = IngestionPipeline(store=store)
    pipe.provider = StubProvider()
    return pipe, store


def test_ingest_reports_and_stores(tmp_path):
    pipe, store = _make_pipeline(tmp_path)
    res = pipe.ingest_symbol("TESTUSDT", "1d", limit=30)
    assert isinstance(res, IngestionResult)
    assert res.received == 30
    assert res.valid == 30
    assert res.inserted == 30
    assert res.error is None
    assert store.count("TESTUSDT", "1d") == 30


def test_reingest_is_idempotent(tmp_path):
    pipe, store = _make_pipeline(tmp_path)
    pipe.ingest_symbol("TESTUSDT", "1d", limit=30)
    res2 = pipe.ingest_symbol("TESTUSDT", "1d", limit=30)
    assert res2.inserted == 0  # nothing new the second time
    assert store.count("TESTUSDT", "1d") == 30


def test_ingest_rejects_bad_data(tmp_path):
    pipe, store = _make_pipeline(tmp_path)
    # Monkeypatch provider to return invalid data.
    bad = pd.DataFrame(
        {
            "open": [1.0],
            "high": [-5.0],  # invalid OHLC / impossible
            "low": [2.0],
            "close": [3.0],
            "volume": [10.0],
        },
        index=pd.date_range("2024-01-01", periods=1, freq="1d", tz="UTC"),
    )
    pipe.provider.get_historical = lambda *a, **k: bad  # type: ignore
    res = pipe.ingest_symbol("BADUSDT", "1d", limit=1)
    assert res.error is not None
    assert res.inserted == 0
    assert store.count("BADUSDT", "1d") == 0
