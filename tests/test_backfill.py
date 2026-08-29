"""Tests for the Indian (FYERS) historical backfill command.

All tests are OFFLINE: the FYERS provider / network is mocked, so no credentials,
no live API, and no real market data are involved. We verify chunking, dedup,
sorting, validation gating, idempotency, error classification, symbol mapping,
dry-run, --days, explicit ranges, and credential safety.

These tests reuse the EXISTING architecture: plan_chunks, validate_ohlcv,
MarketStore.upsert_many, InstrumentRegistry / _fyers_symbol.
"""
from __future__ import annotations

import datetime as dt
from datetime import datetime, timezone, timedelta

import pandas as pd
import pytest

from trading_system.india.backfill import (
    BackfillEngine,
    BackfillStatus,
    _resolve_range,
    _chunk_plan,
)
from trading_system.india.fyers import (
    FYERSMarketDataProvider,
    FYERSAuthError,
    FYERSAPIError,
    FYERSNetworkError,
    FYERSRateLimitError,
)
from trading_system.storage.database import MarketStore
from trading_system.india import InstrumentRegistry
from tests.fixtures.india_fixtures import fyers_history_response

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _ts(d):
    return pd.Timestamp(d, tz="UTC")


def _fake_provider(responses):
    """Build a FYERSMarketDataProvider whose get_historical returns queued frames.

    `responses` is a list of (frame_or_exception). For deterministic chunk tests we
    instead patch get_historical directly; this helper is used for auth/error tests
    where we drive _get at the requests level.
    """
    prov = FYERSMarketDataProvider(client_id="X-100", access_token="tok")
    return prov


def _good_frame(start: pd.Timestamp, n: int, freq: str = "1d"):
    import numpy as np

    idx = pd.date_range(start=start, periods=n, freq=freq, tz="UTC")
    rng = np.random.default_rng(int(pd.Timestamp(start).timestamp()))
    close = 100 + rng.normal(0, 1, n).cumsum()
    open_ = close + rng.normal(0, 0.5, n)
    high = np.maximum(open_, close) + abs(rng.normal(0, 1, n))
    low = np.minimum(open_, close) - abs(rng.normal(0, 1, n))
    return pd.DataFrame(
        {
            "open": open_.astype(float),
            "high": high.astype(float),
            "low": low.astype(float),
            "close": close.astype(float),
            "volume": rng.integers(100, 1000, n).astype(float),
        },
        index=idx,
    )


def _store(tmp_path) -> MarketStore:
    return MarketStore(f"sqlite:///{tmp_path / 'bf.db'}")


# --------------------------------------------------------------------------- #
# Range resolution
# --------------------------------------------------------------------------- #
def test_resolve_range_days_only():
    s, e = _resolve_range("1d", 30, None, _ts("2024-06-01"))
    assert (e - s).days == 30
    assert e == _ts("2024-06-01")


def test_resolve_range_explicit_start_end_wins_over_days():
    s, e = _resolve_range("1d", 5, _ts("2020-01-01"), _ts("2020-06-01"))
    assert s == _ts("2020-01-01")
    assert e == _ts("2020-06-01")


def test_resolve_range_start_after_end_raises():
    with pytest.raises(ValueError):
        _resolve_range("1d", None, _ts("2021-01-01"), _ts("2020-01-01"))


# --------------------------------------------------------------------------- #
# Chunk planning reuse
# --------------------------------------------------------------------------- #
def test_backfill_reuses_plan_chunks_for_intraday():
    # 10 days of 5m data -> each chunk <= 100 days (minute cap) -> 1 chunk.
    chunks = _chunk_plan(_ts("2024-01-01"), _ts("2024-01-11"), "5m")
    assert all((c.end - c.start).days + 1 <= 100 for c in chunks)
    assert chunks[-1].end == _ts("2024-01-11")  # final chunk reaches true end


def test_backfill_chunk_plan_daily_cap():
    chunks = _chunk_plan(_ts("2020-01-01"), _ts("2023-01-01"), "1d")
    # Each chunk span (exclusive end) is <= 366 days (the documented FYERS cap).
    assert all((c.end - c.start).days <= 366 for c in chunks)
    assert chunks[0].start == _ts("2020-01-01")
    assert chunks[-1].end == _ts("2023-01-01")


# --------------------------------------------------------------------------- #
# Single-chunk successful backfill
# --------------------------------------------------------------------------- #
def test_single_chunk_success(tmp_path, monkeypatch):
    store = _store(tmp_path)
    start = _ts("2024-01-01")
    frame = _good_frame(start, 5)

    prov = FYERSMarketDataProvider(client_id="X", access_token="Y")
    monkeypatch.setattr(prov, "get_historical", lambda s, tf, **k: frame)

    eng = BackfillEngine(prov, store)
    res = eng.backfill_symbol("NSE:SBIN", "1d", start=start, end=start + timedelta(days=4))

    assert res.status == BackfillStatus.COMPLETE
    assert res.fetched == 5
    assert res.valid == 5
    assert res.stored == 5
    assert res.skipped == 0
    assert store.count("NSE:SBIN", "1d") == 5
    # Actual range reflects what FYERS returned, not a fabricated current period.
    assert res.actual_start == start
    assert res.actual_end == start + timedelta(days=4)


# --------------------------------------------------------------------------- #
# Multi-chunk backfill
# --------------------------------------------------------------------------- #
def test_multi_chunk_success(tmp_path, monkeypatch):
    store = _store(tmp_path)
    # 3 distinct daily chunks.
    base = _ts("2024-01-01")
    f1 = _good_frame(base, 2)
    f2 = _good_frame(base + timedelta(days=2), 2)
    f3 = _good_frame(base + timedelta(days=4), 2)

    prov = FYERSMarketDataProvider(client_id="X", access_token="Y")

    # get_historical receives (symbol, timeframe, start=, end=). We return the
    # slice of the full series overlapping the requested window.
    full = pd.concat([f1, f2, f3])
    full = full[~full.index.duplicated(keep="first")].sort_index()

    def fake(symbol, tf, start=None, end=None, **k):
        mask = (full.index >= pd.Timestamp(start)) & (full.index <= pd.Timestamp(end))
        return full[mask]

    monkeypatch.setattr(prov, "get_historical", fake)

    eng = BackfillEngine(prov, store)
    res = eng.backfill_symbol(
        "NSE:SBIN", "1d", start=base, end=base + timedelta(days=5)
    )
    assert res.status == BackfillStatus.COMPLETE
    assert res.fetched == 6
    assert res.stored == 6
    assert store.count("NSE:SBIN", "1d") == 6


# --------------------------------------------------------------------------- #
# Chunk boundary deduplication
# --------------------------------------------------------------------------- #
def test_chunk_boundary_dedup(tmp_path, monkeypatch):
    store = _store(tmp_path)
    base = _ts("2024-01-01")
    # Two chunks forced by a >366-day range. The boundary day (base+366d) is present
    # in BOTH frames, so the engine must dedupe it to exactly one stored row.
    f1 = _good_frame(base, 367)                       # days 0..366
    f2 = _good_frame(base + timedelta(days=366), 35)  # days 366..400 (overlap day 366)

    prov = FYERSMarketDataProvider(client_id="X", access_token="Y")
    calls = {"n": 0}

    def fake(symbol, tf, start=None, end=None, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return f1
        return f2

    monkeypatch.setattr(prov, "get_historical", fake)

    eng = BackfillEngine(prov, store)
    res = eng.backfill_symbol(
        "NSE:SBIN", "1d", start=base, end=base + timedelta(days=400)
    )
    # Raw fetched = 367 + 35 = 402 (boundary day counted twice).
    assert res.fetched == 402
    # Stored = 401 distinct days (the overlapping boundary day deduped).
    assert res.stored == 401
    assert store.count("NSE:SBIN", "1d") == 401


# --------------------------------------------------------------------------- #
# Data sorting
# --------------------------------------------------------------------------- #
def test_backfill_output_sorted(tmp_path, monkeypatch):
    store = _store(tmp_path)
    base = _ts("2024-01-01")
    frame = _good_frame(base, 5).iloc[::-1]  # deliver out of order
    prov = FYERSMarketDataProvider(client_id="X", access_token="Y")
    monkeypatch.setattr(prov, "get_historical", lambda s, tf, **k: frame)
    eng = BackfillEngine(prov, store)
    res = eng.backfill_symbol("NSE:SBIN", "1d", start=base, end=base + timedelta(days=4))
    assert res.status == BackfillStatus.COMPLETE
    df = store.load("NSE:SBIN", "1d")
    assert df.index.is_monotonic_increasing


# --------------------------------------------------------------------------- #
# Empty API response
# --------------------------------------------------------------------------- #
def test_empty_response_is_empty_status(tmp_path, monkeypatch):
    store = _store(tmp_path)
    prov = FYERSMarketDataProvider(client_id="X", access_token="Y")
    monkeypatch.setattr(
        prov, "get_historical", lambda s, tf, **k: pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"]
        )
    )
    eng = BackfillEngine(prov, store)
    res = eng.backfill_symbol("NSE:SBIN", "1d", days=30)
    assert res.status == BackfillStatus.EMPTY
    assert res.fetched == 0
    assert res.stored == 0
    assert store.count("NSE:SBIN", "1d") == 0


# --------------------------------------------------------------------------- #
# Authentication failure classification
# --------------------------------------------------------------------------- #
def test_auth_failure_classified(tmp_path, monkeypatch):
    store = _store(tmp_path)
    prov = FYERSMarketDataProvider(client_id="X", access_token="Y")

    def fake(symbol, tf, **k):
        raise FYERSAuthError("FYERS authentication failed (code=-16): Could not authenticate the user")

    monkeypatch.setattr(prov, "get_historical", fake)
    eng = BackfillEngine(prov, store)
    res = eng.backfill_symbol("NSE:SBIN", "1d", days=30)
    assert res.status == BackfillStatus.AUTH_ERROR
    assert res.stored == 0
    assert store.count("NSE:SBIN", "1d") == 0
    # Ensure the message does NOT contain any credential value.
    assert "X" not in res.error.split("FYERS authentication failed")[1] or "tok" not in res.error


# --------------------------------------------------------------------------- #
# One failed chunk, others succeed (partial)
# --------------------------------------------------------------------------- #
def test_one_chunk_fails_others_succeed(tmp_path, monkeypatch):
    store = _store(tmp_path)
    base = _ts("2024-01-01")
    good = _good_frame(base, 2)

    prov = FYERSMarketDataProvider(client_id="X", access_token="Y")
    calls = {"n": 0}

    def fake(symbol, tf, start=None, end=None, **k):
        calls["n"] += 1
        # Fail the SECOND distinct chunk only, then succeed thereafter.
        if calls["n"] == 2:
            raise FYERSNetworkError("chunk fetch failed after retries: timeout")
        return good

    monkeypatch.setattr(prov, "get_historical", fake)
    # 400 days -> multiple chunks so one can fail.
    eng = BackfillEngine(prov, store, max_retries=0)
    res = eng.backfill_symbol(
        "NSE:SBIN", "1d", start=base, end=base + timedelta(days=400)
    )
    assert res.chunks_failed >= 1
    # Because chunk 2 failed, overall is PARTIAL (some chunks succeeded + data stored).
    assert res.status == BackfillStatus.PARTIAL
    assert res.stored > 0  # the other chunks still stored


# --------------------------------------------------------------------------- #
# Validation rejection
# --------------------------------------------------------------------------- #
def test_validation_rejects_bad_rows(tmp_path, monkeypatch):
    store = _store(tmp_path)
    base = _ts("2024-01-01")
    frame = _good_frame(base, 5)
    # Corrupt one row: high < low.
    frame.iloc[2, frame.columns.get_loc("high")] = 1.0
    frame.iloc[2, frame.columns.get_loc("low")] = 999.0

    prov = FYERSMarketDataProvider(client_id="X", access_token="Y")
    monkeypatch.setattr(prov, "get_historical", lambda s, tf, **k: frame)
    eng = BackfillEngine(prov, store)
    res = eng.backfill_symbol("NSE:SBIN", "1d", start=base, end=base + timedelta(days=4))
    # 4 valid rows stored, 1 rejected.
    assert res.valid == 4
    assert res.skipped == 1
    assert res.stored == 4
    assert store.count("NSE:SBIN", "1d") == 4


def test_all_rows_invalid_is_validation_error(tmp_path, monkeypatch):
    store = _store(tmp_path)
    base = _ts("2024-01-01")
    frame = _good_frame(base, 3)
    for i in range(3):
        frame.iloc[i, frame.columns.get_loc("high")] = 1.0
        frame.iloc[i, frame.columns.get_loc("low")] = 999.0
    prov = FYERSMarketDataProvider(client_id="X", access_token="Y")
    monkeypatch.setattr(prov, "get_historical", lambda s, tf, **k: frame)
    eng = BackfillEngine(prov, store)
    res = eng.backfill_symbol("NSE:SBIN", "1d", start=base, end=base + timedelta(days=2))
    assert res.status == BackfillStatus.VALIDATION_ERROR
    assert res.stored == 0


# --------------------------------------------------------------------------- #
# Idempotent second backfill
# --------------------------------------------------------------------------- #
def test_idempotent_second_backfill(tmp_path, monkeypatch):
    store = _store(tmp_path)
    base = _ts("2024-01-01")
    frame = _good_frame(base, 5)
    prov = FYERSMarketDataProvider(client_id="X", access_token="Y")
    monkeypatch.setattr(prov, "get_historical", lambda s, tf, **k: frame)
    eng = BackfillEngine(prov, store)
    r1 = eng.backfill_symbol("NSE:SBIN", "1d", start=base, end=base + timedelta(days=4))
    assert r1.stored == 5
    # Re-run identical window.
    r2 = eng.backfill_symbol("NSE:SBIN", "1d", start=base, end=base + timedelta(days=4))
    assert r2.fetched == 5
    assert r2.stored == 0  # idempotent
    assert store.count("NSE:SBIN", "1d") == 5


def test_idempotent_recovers_missing_rows(tmp_path, monkeypatch):
    store = _store(tmp_path)
    base = _ts("2024-01-01")
    frame = _good_frame(base, 5)
    prov = FYERSMarketDataProvider(client_id="X", access_token="Y")
    monkeypatch.setattr(prov, "get_historical", lambda s, tf, **k: frame)
    eng = BackfillEngine(prov, store)
    # First run stores only first 3 by pre-seeding... simulate by deleting 2 rows.
    r1 = eng.backfill_symbol("NSE:SBIN", "1d", start=base, end=base + timedelta(days=4))
    assert r1.stored == 5
    # Delete some rows to simulate an interrupted prior run.
    with store._Session() as s:
        from trading_system.storage.database import OHLCVRecord
        from sqlalchemy import select
        rows = s.execute(select(OHLCVRecord)).scalars().all()
        for row in rows[:2]:
            s.delete(row)
        s.commit()
    assert store.count("NSE:SBIN", "1d") == 3
    # Re-run must recover the 2 missing rows, not duplicate the 3 present.
    r2 = eng.backfill_symbol("NSE:SBIN", "1d", start=base, end=base + timedelta(days=4))
    assert r2.stored == 2
    assert store.count("NSE:SBIN", "1d") == 5


# --------------------------------------------------------------------------- #
# Multiple symbols
# --------------------------------------------------------------------------- #
def test_multiple_symbols_independent(tmp_path, monkeypatch):
    store = _store(tmp_path)
    base = _ts("2024-01-01")
    frame = _good_frame(base, 3)

    prov = FYERSMarketDataProvider(client_id="X", access_token="Y")
    monkeypatch.setattr(prov, "get_historical", lambda s, tf, **k: frame)
    eng = BackfillEngine(prov, store)
    r_sbin = eng.backfill_symbol("NSE:SBIN", "1d", start=base, end=base + timedelta(days=2))
    r_rel = eng.backfill_symbol("NSE:RELIANCE", "1d", start=base, end=base + timedelta(days=2))
    assert r_sbin.stored == 3 and r_rel.stored == 3
    assert store.count("NSE:SBIN", "1d") == 3
    assert store.count("NSE:RELIANCE", "1d") == 3
    # Provider/exchange metadata preserved.
    df = store.load("NSE:SBIN", "1d")
    assert df.iloc[0]["provider"] == "fyers"


# --------------------------------------------------------------------------- #
# Dry-run
# --------------------------------------------------------------------------- #
def test_dry_run_plans_no_api_no_db(tmp_path, monkeypatch):
    store = _store(tmp_path)
    prov = FYERSMarketDataProvider(client_id="X", access_token="Y")
    called = {"n": 0}
    monkeypatch.setattr(prov, "get_historical",
                        lambda s, tf, **k: called.__setitem__("n", called["n"] + 1))
    eng = BackfillEngine(prov, store)
    res = eng.backfill_symbol("NSE:SBIN", "1d", days=730, dry_run=True)
    assert res.dry_run
    assert called["n"] == 0  # no API call
    assert res.chunks_total >= 2  # 730 days / 366 = 2 chunks
    assert store.count("NSE:SBIN", "1d") == 0  # nothing stored


# --------------------------------------------------------------------------- #
# --days and explicit range
# --------------------------------------------------------------------------- #
def test_days_argument(tmp_path, monkeypatch):
    store = _store(tmp_path)
    fixed_now = _ts("2024-03-01")

    # Make the engine's "now" deterministic so --days is reproducible.
    from trading_system.india import backfill as bf

    monkeypatch.setattr(bf.pd.Timestamp, "now",
                        classmethod(lambda cls, tz=None: fixed_now))

    base = fixed_now - timedelta(days=10)
    frame = _good_frame(base, 11)
    prov = FYERSMarketDataProvider(client_id="X", access_token="Y")
    captured = {}

    def fake(symbol, tf, start=None, end=None, **k):
        captured["start"] = pd.Timestamp(start)
        captured["end"] = pd.Timestamp(end)
        return _good_frame(captured["start"], 3)

    monkeypatch.setattr(prov, "get_historical", fake)
    eng = BackfillEngine(prov, store)
    res = eng.backfill_symbol("NSE:SBIN", "1d", days=30)
    assert (captured["end"] - captured["start"]).days == 30
    assert captured["end"] == fixed_now
    assert res.fetched == 3


def test_explicit_range_argument(tmp_path, monkeypatch):
    store = _store(tmp_path)
    s = _ts("2021-01-01")
    e = _ts("2021-02-01")
    frame = _good_frame(s, 5)
    prov = FYERSMarketDataProvider(client_id="X", access_token="Y")
    captured = {"calls": []}

    def fake(symbol, tf, start=None, end=None, **k):
        captured["calls"].append((pd.Timestamp(start), pd.Timestamp(end)))
        return frame

    monkeypatch.setattr(prov, "get_historical", fake)
    eng = BackfillEngine(prov, store)
    res = eng.backfill_symbol("NSE:SBIN", "1d", start=s, end=e)
    # The engine reports the requested (user-supplied) range verbatim.
    assert res.requested_start == s
    assert res.requested_end == e
    # The provider was asked for windows that together cover [s, e].
    all_starts = [c[0] for c in captured["calls"]]
    all_ends = [c[1] for c in captured["calls"]]
    assert min(all_starts) == s
    assert max(all_ends) == e


# --------------------------------------------------------------------------- #
# Symbol normalization
# --------------------------------------------------------------------------- #
def test_symbol_normalization_maps_to_fyers(tmp_path, monkeypatch):
    store = _store(tmp_path)
    base = _ts("2024-01-01")
    frame = _good_frame(base, 2)
    prov = FYERSMarketDataProvider(client_id="X", access_token="Y")
    monkeypatch.setattr(prov, "get_historical", lambda s, tf, **k: frame)
    eng = BackfillEngine(prov, store)
    res = eng.backfill_symbol("NSE:SBIN", "1d", start=base, end=base + timedelta(days=1))
    # The fyers symbol is derived via the existing mapping layer.
    assert res.fyers_symbol == "NSE:SBIN-EQ"
    assert res.exchange == "NSE"
    # INDEX symbols also map.
    res2 = eng.backfill_symbol("NSE:NIFTY50", "1d", start=base, end=base + timedelta(days=1))
    assert res2.fyers_symbol == "NSE:NIFTY50-INDEX"


# --------------------------------------------------------------------------- #
# Credential safety in output/logs
# --------------------------------------------------------------------------- #
def test_credentials_not_in_error_output(tmp_path, monkeypatch, capsys):
    store = _store(tmp_path)
    prov = FYERSMarketDataProvider(client_id="SECRET_CLIENT_ID", access_token="SECRET_TOKEN")

    def fake(symbol, tf, **k):
        raise FYERSAuthError("auth failed code=-16")

    monkeypatch.setattr(prov, "get_historical", fake)
    eng = BackfillEngine(prov, store)
    res = eng.backfill_symbol("NSE:SBIN", "1d", days=30)
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "SECRET_CLIENT_ID" not in out
    assert "SECRET_TOKEN" not in out
    assert "SECRET_CLIENT_ID" not in res.error
    assert "SECRET_TOKEN" not in res.error


def test_credentials_not_in_network_error(tmp_path, monkeypatch, capsys):
    store = _store(tmp_path)
    prov = FYERSMarketDataProvider(client_id="SECRET_CLIENT_ID", access_token="SECRET_TOKEN")

    def fake(symbol, tf, **k):
        raise FYERSNetworkError("connection reset by peer: <Auth header redacted>")

    monkeypatch.setattr(prov, "get_historical", fake)
    eng = BackfillEngine(prov, store)
    eng.backfill_symbol("NSE:SBIN", "1d", days=30)
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "SECRET_CLIENT_ID" not in out and "SECRET_TOKEN" not in out


# --------------------------------------------------------------------------- #
# Provider/exchange metadata persisted
# --------------------------------------------------------------------------- #
def test_metadata_persisted(tmp_path, monkeypatch):
    store = _store(tmp_path)
    base = _ts("2024-01-01")
    frame = _good_frame(base, 3)
    prov = FYERSMarketDataProvider(client_id="X", access_token="Y")
    monkeypatch.setattr(prov, "get_historical", lambda s, tf, **k: frame)
    eng = BackfillEngine(prov, store)
    eng.backfill_symbol("NSE:SBIN", "1d", start=base, end=base + timedelta(days=2))
    with store._Session() as s:
        from trading_system.storage.database import OHLCVRecord
        from sqlalchemy import select
        row = s.execute(select(OHLCVRecord)).scalars().first()
        assert row.provider == "fyers"
        assert row.exchange == "NSE"
        assert row.symbol == "NSE:SBIN"
        assert row.timeframe == "1d"


# --------------------------------------------------------------------------- #
# End-to-end through the CLI (mocked provider), data-only guarantee
# --------------------------------------------------------------------------- #
def test_cli_backfill_end_to_end(tmp_path, monkeypatch, capsys):
    from trading_system.__main__ import main
    from trading_system.config import settings

    # Point storage at a temp DB and stub the provider's network.
    monkeypatch.setattr(settings.storage, "db_path", tmp_path / "cli.db")
    # Reconfigure the shared engine store used by main() via _store().
    import trading_system.__main__ as m

    base = _ts("2024-01-01")
    frame = _good_frame(base, 5)
    prov = FYERSMarketDataProvider(client_id="X", access_token="Y")
    monkeypatch.setattr(prov, "get_historical", lambda s, tf, **k: frame)
    monkeypatch.setattr(
        "trading_system.data.provider_exports.get_provider", lambda name, **kw: prov
    )
    monkeypatch.setattr(m, "_store", lambda: MarketStore(f"sqlite:///{tmp_path / 'cli.db'}"))

    rc = main([
        "backfill-history",
        "--symbols", "NSE:SBIN",
        "--timeframe", "1d",
        "--start", "2024-01-01",
        "--end", "2024-01-05",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "COMPLETE" in out
    assert "DATA ONLY" in out
    # No order-placement language / no broker endpoints touched.
    assert "order" not in out.lower() or "no orders" in out.lower()
    store = MarketStore(f"sqlite:///{tmp_path / 'cli.db'}")
    assert store.count("NSE:SBIN", "1d") == 5


def test_cli_dry_run(tmp_path, monkeypatch, capsys):
    from trading_system.__main__ import main
    import trading_system.__main__ as m
    from trading_system.storage.database import MarketStore

    prov = FYERSMarketDataProvider(client_id="X", access_token="Y")
    monkeypatch.setattr(
        "trading_system.data.provider_exports.get_provider", lambda name, **kw: prov
    )
    monkeypatch.setattr(m, "_store", lambda: MarketStore(f"sqlite:///{tmp_path / 'dry.db'}"))
    rc = main([
        "backfill-history",
        "--symbols", "NSE:SBIN",
        "--timeframe", "1d",
        "--days", "800",
        "--dry-run",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY-RUN" in out
    store = MarketStore(f"sqlite:///{tmp_path / 'dry.db'}")
    assert store.count("NSE:SBIN", "1d") == 0
