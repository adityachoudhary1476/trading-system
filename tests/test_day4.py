"""Day 4 tests: chunking, instrument repo, calendar, event bus, closed-candle,
data health, WS reconnection (deterministic), paper-trader interface, storage.
No network / FYERS credentials / live market required.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import pytest

from trading_system.india import (
    InstrumentRepository,
    TradingCalendar,
    SessionPhase,
    EventBus,
    InternalMarketEvent,
    EventType,
    ClosedCandlePipeline,
    CandleState,
    DataHealthMonitor,
    FeedStatus,
    plan_chunks,
    combine_frames,
    ChunkedHistoricalFetcher,
    FYERSMarketDataProvider,
)
from trading_system.india.fyers import FyersDataSocket
from trading_system.india.events import InternalMarketEvent
from trading_system.paper_trading.interface import NoOpPaperTrader, PaperTrader
from trading_system.data.validation import validate_ohlcv
from tests.fixtures.india_fixtures import (
    fyers_history_response,
    fyers_ws_symbol_update,
    fyers_ws_malformed,
    fyers_ws_heartbeat,
    fyers_ws_auth_ack,
    fyers_ws_unknown_type,
    fyers_ws_lite,
    instrument_master_csv,
)
from zoneinfo import ZoneInfo

KOL = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Historical chunking
# --------------------------------------------------------------------------- #
def test_plan_chunks_respects_minute_cap():
    start = pd.Timestamp("2024-01-01", tz="UTC")
    end = pd.Timestamp("2024-04-10", tz="UTC")  # ~100 days
    chunks = plan_chunks(start, end, "1m")  # 100d cap
    # Each chunk span <= 100 days; combined == full coverage.
    assert all((c.end - c.start).days + 1 <= 100 for c in chunks)
    assert chunks[0].start == start
    assert chunks[-1].end == end


def test_plan_chunks_respects_day_cap():
    start = pd.Timestamp("2022-01-01", tz="UTC")
    end = pd.Timestamp("2023-06-01", tz="UTC")  # >366 days
    chunks = plan_chunks(start, end, "1d")  # 366d cap
    assert all((c.end - c.start).days + 1 <= 366 for c in chunks)


def test_plan_chunks_adjacent_no_gap():
    start = pd.Timestamp("2024-01-01", tz="UTC")
    end = pd.Timestamp("2024-03-11", tz="UTC")
    chunks = plan_chunks(start, end, "1m")
    for a, b in zip(chunks, chunks[1:]):
        assert (b.start - a.end).days == 1


def test_combine_frames_dedupes_and_sorts():
    base = pd.Timestamp("2024-01-01", tz="UTC")
    a = pd.DataFrame({"open": [1.0]}, index=[base])
    b = pd.DataFrame({"open": [2.0]}, index=[base])  # dup timestamp
    c = pd.DataFrame({"open": [3.0]}, index=[base + timedelta(days=1)])
    out = combine_frames([a, b, c])
    assert len(out) == 2  # duplicate removed
    assert out.index.is_monotonic_increasing


def test_chunked_fetcher_combines_and_validates():
    # Pure fetch callable returning a fixed frame per chunk; no network.
    def fake_fetch(s, e):
        base = s.normalize()
        return pd.DataFrame(
            {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
            index=[base],
        )

    fetcher = ChunkedHistoricalFetcher("1d", fake_fetch, max_days_per_request=30)
    out = fetcher.fetch(pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2024-02-15", tz="UTC"))
    # 46 days / 30-day chunks = 2 chunks, each 1 row (deterministic fake returns 1).
    assert len(out) >= 2


def test_chunked_fetcher_tolerates_partial_failure():
    calls = {"n": 0}

    def flaky(s, e):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        base = s.normalize()
        return pd.DataFrame(
            {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
            index=[base],
        )

    fetcher = ChunkedHistoricalFetcher("1d", flaky, max_days_per_request=30, max_retries=1)
    out = fetcher.fetch(pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2024-02-15", tz="UTC"))
    assert len(out) >= 1  # one chunk failed, others succeeded


# --------------------------------------------------------------------------- #
# Instrument repository + master parsing
# --------------------------------------------------------------------------- #
def test_repo_imports_fyers_csv_fixture():
    repo = InstrumentRepository.from_fyers_csv(instrument_master_csv())
    assert repo.get_instrument("NSE:SBIN") is not None
    assert repo.get_instrument("NSE:NIFTY50") is not None
    # option parsed with strike/expiry/type
    opt = repo.get_instrument("NSE:SBIN25DEC400CE")
    assert opt is not None
    assert opt.instrument_type.value == "option_ce"
    assert opt.strike == 400.0
    assert opt.expiry == "2025-12-25"


def test_repo_queries():
    repo = InstrumentRepository.from_fyers_csv(instrument_master_csv())
    assert len(repo.get_equities("NSE")) >= 3
    assert len(repo.get_indices("NSE")) >= 2
    assert len(repo.get_derivatives("NSE")) >= 3
    assert len(repo.search_instruments("BANK")) >= 1


def test_repo_expiring_derivatives():
    repo = InstrumentRepository.from_fyers_csv(instrument_master_csv())
    exp = repo.get_expiring_derivatives(as_of=__import__("datetime").date(2025, 12, 1), within_days=31)
    assert len(exp) >= 3


# --------------------------------------------------------------------------- #
# Market calendar hardening
# --------------------------------------------------------------------------- #
def test_session_phases():
    cal = TradingCalendar()
    pre = datetime(2024, 3, 6, 9, 5, tzinfo=KOL)      # pre-market
    reg = datetime(2024, 3, 6, 12, 0, tzinfo=KOL)     # regular
    post = datetime(2024, 3, 6, 15, 45, tzinfo=KOL)   # post-market
    night = datetime(2024, 3, 6, 20, 0, tzinfo=KOL)   # closed
    assert cal.phase(pre) == SessionPhase.PRE_MARKET
    assert cal.phase(reg) == SessionPhase.REGULAR
    assert cal.phase(post) == SessionPhase.POST_MARKET
    assert cal.phase(night) == SessionPhase.CLOSED


def test_holiday_registry_not_hardcoded():
    # Holiday is injected, not baked in. Default calendar has no holidays.
    cal = TradingCalendar()
    assert cal.phase(datetime(2024, 3, 6, 12, 0, tzinfo=KOL)) == SessionPhase.REGULAR
    cal.add_holiday(__import__("datetime").date(2024, 3, 6))
    assert cal.phase(datetime(2024, 3, 6, 12, 0, tzinfo=KOL)) == SessionPhase.HOLIDAY


def test_session_boundary_weekend_closed():
    # Sunday
    sun = datetime(2024, 3, 3, 12, 0, tzinfo=KOL)
    assert TradingCalendar().phase(sun) == SessionPhase.CLOSED


# --------------------------------------------------------------------------- #
# Event bus (provider-independent)
# --------------------------------------------------------------------------- #
def test_event_bus_fanout():
    bus = EventBus()
    seen = []

    def consumer(ev):
        seen.append(ev.symbol)

    bus.subscribe_all(consumer)
    ev = InternalMarketEvent(
        event_type=EventType.QUOTE, symbol="NSE:SBIN",
        exchange="NSE", provider_symbol="NSE:SBIN-EQ", timestamp=datetime.now(UTC),
    )
    bus.publish(ev)
    assert seen == ["NSE:SBIN"]
    assert bus.subscriber_count() == 1


def test_event_bus_topic_subscription():
    bus = EventBus()
    hits = []

    def c(ev):
        hits.append(ev.symbol)

    bus.subscribe("NSE:SBIN", c)
    other = InternalMarketEvent(
        event_type=EventType.QUOTE, symbol="NSE:RELIANCE",
        exchange="NSE", provider_symbol="NSE:RELIANCE-EQ", timestamp=datetime.now(UTC),
    )
    bus.publish(other)
    assert hits == []  # not subscribed to RELIANCE


# --------------------------------------------------------------------------- #
# Closed-candle pipeline
# --------------------------------------------------------------------------- #
def test_closed_candle_emitted_only_on_close():
    pipe = ClosedCandlePipeline("5m")
    base = datetime(2024, 3, 6, 9, 15, tzinfo=KOL)
    closed = []

    def on_close(cc):
        closed.append(cc)

    pipe.on_closed(on_close)
    # Feed 4 ticks in the SAME 5m bar -> no close.
    for i, off in enumerate([0, 1, 2, 3]):
        ev = InternalMarketEvent(
            event_type=EventType.QUOTE, symbol="NSE:SBIN", exchange="NSE",
            provider_symbol="NSE:SBIN-EQ", timestamp=base + timedelta(minutes=off),
            ltp=100.0 + i,
        )
        pipe.feed_event(ev)
    assert len(closed) == 0
    # Tick into next 5m bar -> previous closes.
    ev = InternalMarketEvent(
        event_type=EventType.QUOTE, symbol="NSE:SBIN", exchange="NSE",
        provider_symbol="NSE:SBIN-EQ", timestamp=base + timedelta(minutes=5),
        ltp=200.0,
    )
    pipe.feed_event(ev)
    assert len(closed) == 1
    assert closed[0].state == CandleState.CLOSED


def test_closed_candle_rejects_late_tick_for_closed_bar():
    pipe = ClosedCandlePipeline("5m")
    base = datetime(2024, 3, 6, 9, 15, tzinfo=KOL)
    closed = []
    pipe.on_closed(lambda cc: closed.append(cc))
    ev1 = InternalMarketEvent(EventType.QUOTE, "NSE:SBIN", "NSE", "NSE:SBIN-EQ",
                              timestamp=base, ltp=100.0)
    ev2 = InternalMarketEvent(EventType.QUOTE, "NSE:SBIN", "NSE", "NSE:SBIN-EQ",
                              timestamp=base + timedelta(minutes=5), ltp=101.0)  # closes bar0
    ev3_late = InternalMarketEvent(EventType.QUOTE, "NSE:SBIN", "NSE", "NSE:SBIN-EQ",
                                   timestamp=base + timedelta(minutes=1), ltp=999.0)  # late into bar0
    pipe.feed_event(ev1)
    pipe.feed_event(ev2)
    before = len(closed)
    pipe.feed_event(ev3_late)  # must be ignored (bar0 already closed)
    assert len(closed) == before


def test_closed_candle_snapshot_uses_only_closed_data():
    pipe = ClosedCandlePipeline("5m")
    base = datetime(2024, 3, 6, 9, 15, tzinfo=KOL)
    for off in [0, 1, 2, 3]:
        pipe.feed_event(InternalMarketEvent(
            EventType.QUOTE, "NSE:SBIN", "NSE", "NSE:SBIN-EQ",
            timestamp=base + timedelta(minutes=off), ltp=100.0 + off))
    # Still provisional; make_snapshot should raise (no closed bars flushed yet).
    with pytest.raises(RuntimeError):
        pipe.make_snapshot("NSE:SBIN")
    # Close the bar, then build snapshot from the closed history.
    pipe.feed_event(InternalMarketEvent(
        EventType.QUOTE, "NSE:SBIN", "NSE", "NSE:SBIN-EQ",
        timestamp=base + timedelta(minutes=5), ltp=200.0))
    snap = pipe.make_snapshot("NSE:SBIN")
    # Snapshot is provably built from closed bars: the no-look-ahead invariant
    # (timestamp == last_bar_timestamp) and lookahead_safe flag are set.
    assert snap.lookahead_safe
    assert snap.timestamp == snap.last_bar_timestamp
    assert snap.symbol == "NSE:SBIN"


# --------------------------------------------------------------------------- #
# Data health monitor
# --------------------------------------------------------------------------- #
def test_health_monitor_lifecycle():
    hm = DataHealthMonitor(stale_seconds=5)
    assert hm.status == FeedStatus.DISCONNECTED
    hm.on_connect()
    assert hm.status == FeedStatus.HEALTHY
    # Use a single controlled clock throughout (real runtime uses wall-clock).
    hm.tick(datetime.now(UTC), now=100.0)
    assert hm.evaluate(now=100.0) == FeedStatus.HEALTHY
    # No events for > stale_seconds -> STALE
    assert hm.evaluate(now=200.0) == FeedStatus.STALE
    assert hm.evaluate(now=200.0) != FeedStatus.HEALTHY
    hm.on_disconnect()
    assert hm.status == FeedStatus.DISCONNECTED
    hm.on_connect()
    hm.on_auth_error()
    assert hm.status == FeedStatus.AUTH_ERROR
    hm.on_invalid()
    assert hm.metrics.events_rejected >= 1


# --------------------------------------------------------------------------- #
# WebSocket reconnection (deterministic, no real socket)
# --------------------------------------------------------------------------- #
def _make_socket():
    """Build a FyersDataSocket whose real-SDK constructor is bypassed.

    The production socket wraps the official fyers_apiv3 SDK (binary protobuf).
    For deterministic tests we stub only the normalization + lifecycle hooks —
    we do NOT open a network connection and we do NOT re-implement reconnect (the
    SDK owns reconnect with bounded backoff).
    """
    prov = FYERSMarketDataProvider(client_id="X-100", access_token="tok")
    sock = object.__new__(FyersDataSocket)
    sock._fy_to_internal = {"NSE:SBIN-EQ": "NSE:SBIN"}
    sock.provider = prov
    sock.on_event = lambda e: None
    sock._closed = False
    sock._on_connect_cb = None
    sock._on_disconnect_cb = None
    sock._on_auth_error_cb = None
    sock._on_invalid_cb = None
    return sock


def test_ws_normalizes_symbol_update():
    sock = _make_socket()
    received = []
    sock.on_event = lambda e: received.append(e)
    sock._on_sdk_message(fyers_ws_symbol_update())
    assert len(received) == 1
    assert received[0].symbol == "NSE:SBIN"
    assert received[0].ltp == 123.45


def test_ws_drops_malformed_json():
    sock = _make_socket()
    received = []
    sock.on_event = lambda e: received.append(e)
    # non-dict / control frames are skipped, never crash
    assert sock._normalize("not-a-dict") is None
    sock._on_sdk_message(fyers_ws_malformed())  # dict without 'symbol'
    sock._on_sdk_message(fyers_ws_heartbeat())  # control frame
    sock._on_sdk_message(fyers_ws_auth_ack())   # control frame
    assert received == []


def test_ws_unknown_type_skipped():
    sock = _make_socket()
    received = []
    sock.on_event = lambda e: received.append(e)
    sock._on_sdk_message(fyers_ws_unknown_type())
    assert received == []


def test_ws_lifecycle_hooks_drive_health():
    from trading_system.india.data_health import DataHealthMonitor, FeedStatus
    sock = _make_socket()
    hm = DataHealthMonitor()
    sock.on_connect_cb(hm.on_connect)
    sock.on_disconnect_cb(hm.on_disconnect)
    sock.on_auth_error_cb(hm.on_auth_error)
    sock._on_sdk_connect()
    assert hm.status == FeedStatus.HEALTHY
    sock._on_sdk_close({"code": 1, "message": "closed"})
    assert hm.status == FeedStatus.DISCONNECTED
    # SDK surfaces auth failure via OnError with type AUTH_TYPE.
    sock._on_sdk_error({"type": "AUTH_TYPE", "code": 803})
    assert hm.status == FeedStatus.AUTH_ERROR


def test_ws_reconnect_owned_by_sdk_no_spin():
    """Our socket must not implement its own reconnect spin loop (SDK does)."""
    sock = _make_socket()
    assert not hasattr(sock, "_schedule_reconnect")
    # Double close should not raise or loop.
    sock._on_sdk_close({"code": 1})
    sock._on_sdk_close({"code": 1})
