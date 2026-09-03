"""Day 5 tests: real FYERS pipeline integration (all OFFLINE + deterministic).

No network / FYERS credentials / live market / current prices / current time.
The live socket is faked at the boundary: we drive FyersDataSocket._normalize and
LiveMarketPipeline.ingest directly with synthetic SDK-shaped dicts, and a fake
provider + fake socket for bootstrap/historical + health wiring.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import pytest

from trading_system.india.events import EventType, InternalMarketEvent
from trading_system.india.event_bus import EventBus
from trading_system.india.closed_candle_pipeline import ClosedCandlePipeline
from trading_system.india.data_health import DataHealthMonitor, FeedStatus
from trading_system.india.live_pipeline import LiveMarketPipeline, bootstrap_historical
from trading_system.india.fyers import FYERSMarketDataProvider


# --- helpers ----------------------------------------------------------------
KOL = timezone(timedelta(hours=5, minutes=30))


def _event(symbol="NSE:SBIN", ltp=100.0, ts=None, exch="NSE", fy="NSE:SBIN-EQ") -> InternalMarketEvent:
    return InternalMarketEvent(
        event_type=EventType.QUOTE, symbol=symbol, exchange=exch,
        provider_symbol=fy, timestamp=ts or datetime.now(KOL), ltp=ltp,
    )


def _sdk_dict(symbol="NSE:SBIN-EQ", ltp=123.4, **extra):
    d = {"symbol": symbol, "ltp": ltp, "type": "sf"}
    d.update(extra)
    return d


# --- 1. FYERS event -> EventBus ---------------------------------------------
def test_fyers_event_to_eventbus():
    bus = EventBus()
    received = []
    bus.subscribe_all(received.append)
    prov = FYERSMarketDataProvider()
    sock = object()  # we only need the normalizer
    # Use the provider's normalization helper through a minimal socket-like object.
    from trading_system.india.fyers import FyersDataSocket

    class _FakeSocket(FyersDataSocket):
        def __init__(self):
            # bypass FyersDataSocket.__init__ (which builds the real SDK object)
            self._fy_to_internal = {"NSE:SBIN-EQ": "NSE:SBIN"}
            self.provider = prov
            self.on_event = None

    fs = _FakeSocket()
    ev = fs._normalize(_sdk_dict("NSE:SBIN-EQ", 123.4))
    assert ev is not None
    assert ev.symbol == "NSE:SBIN"
    assert ev.ltp == 123.4
    bus.publish(ev)
    assert len(received) == 1
    assert received[0].symbol == "NSE:SBIN"


# --- 2. EventBus -> closed candle pipeline ----------------------------------
def test_eventbus_to_closed_candle():
    bus = EventBus()
    pipe = ClosedCandlePipeline("5m")
    closed = []

    def capture(cc):
        closed.append(cc)

    pipe.on_closed(capture)
    # Wire the bus fan-out into the candle pipeline (same boundary the live
    # pipeline uses in LiveMarketPipeline.ingest).
    bus.subscribe_all(lambda e: pipe.feed_event(e))
    # publish events that span two 5m bars
    base = datetime(2024, 3, 6, 9, 15, tzinfo=KOL)
    for off in (0, 1, 2):
        bus.publish(_event(ts=base + timedelta(minutes=off), ltp=100 + off))
    bus.publish(_event(ts=base + timedelta(minutes=5), ltp=200.0))  # closes bar 09:15
    assert len(closed) == 1
    assert closed[0].symbol == "NSE:SBIN"
    # close of 09:15 bar = last price within it (ltp 102); 200 is the next bar's open
    assert closed[0].close == 102.0


# --- 3. closed candle -> snapshot -------------------------------------------
def test_closed_candle_to_snapshot():
    pipe = ClosedCandlePipeline("5m", analysis_interval_bars=1)
    pipe.feed_event(_event(ts=datetime(2024, 3, 6, 9, 15, tzinfo=KOL), ltp=100.0))
    pipe.feed_event(_event(ts=datetime(2024, 3, 6, 9, 20, tzinfo=KOL), ltp=110.0))
    snap = pipe.make_snapshot("NSE:SBIN")
    assert snap.symbol == "NSE:SBIN"
    assert snap.lookahead_safe
    assert snap.timestamp == snap.last_bar_timestamp


# --- 4. duplicate tick rejection -------------------------------------------
def test_duplicate_tick_rejection():
    pipe = LiveMarketPipeline(symbols=["NSE:SBIN"], timeframe="5m")
    pipe.start()
    base = datetime(2024, 3, 6, 9, 15, tzinfo=KOL)
    # First tick closes at 09:20
    pipe.ingest(_event(ts=base, ltp=100.0))
    pipe.ingest(_event(ts=base + timedelta(minutes=5), ltp=200.0))  # closes 09:15
    before = pipe.health.metrics.candles_generated
    # Late/duplicate ticks for the now-closed 09:15 bar must be ignored.
    pipe.ingest(_event(ts=base, ltp=999.0))
    pipe.ingest(_event(ts=base, ltp=888.0))
    assert pipe.health.metrics.candles_generated == before


# --- 5. late tick rejection (closed bar) ------------------------------------
def test_late_tick_rejection():
    pipe = ClosedCandlePipeline("5m")
    base = datetime(2024, 3, 6, 9, 15, tzinfo=KOL)
    pipe.feed_event(_event(ts=base, ltp=100.0))
    closed = pipe.feed_event(_event(ts=base + timedelta(minutes=5), ltp=150.0))  # closes
    assert len(closed) == 1
    # A tick arriving "at" the closed bar start again -> ignored (no new close).
    again = pipe.feed_event(_event(ts=base, ltp=1234.0))
    assert again == []


def test_out_of_order_tick_cannot_move_active_bar_backwards():
    from trading_system.india.candle_aggregator import CandleAggregator

    agg = CandleAggregator("5m")
    base = datetime(2024, 3, 6, 9, 15, tzinfo=KOL)
    agg.update(base, 100.0)
    agg.update(base + timedelta(minutes=10), 120.0)
    assert agg.provisional is not None
    assert agg.provisional.start == base + timedelta(minutes=10)
    assert agg.update(base + timedelta(minutes=5), 110.0) == []
    assert agg.provisional.start == base + timedelta(minutes=10)


def test_closed_history_reads_are_non_destructive():
    pipe = ClosedCandlePipeline("5m")
    base = datetime(2024, 3, 6, 9, 15, tzinfo=KOL)
    pipe.feed_event(_event(ts=base, ltp=100.0))
    pipe.feed_event(_event(ts=base + timedelta(minutes=5), ltp=110.0))
    first = pipe.build_closed_history_df("NSE:SBIN")
    second = pipe.build_closed_history_df("NSE:SBIN")
    assert len(first) == 1
    assert second.equals(first)


def test_post_market_boundary_closes_regular_bar_without_creating_post_bar():
    pipe = ClosedCandlePipeline("5m")
    closed = []
    pipe.on_closed(closed.append)
    regular = datetime(2024, 3, 6, 15, 25, tzinfo=KOL)
    boundary = datetime(2024, 3, 6, 15, 30, tzinfo=KOL)
    pipe.feed_event(_event(ts=regular, ltp=100.0))
    result = pipe.feed_event(_event(ts=boundary, ltp=110.0))
    assert len(result) == 1
    assert result[0].start == regular
    assert pipe._agg("NSE:SBIN").provisional is None


# --- 6. unhealthy feed suppresses signals -----------------------------------
def test_unhealthy_feed_suppresses_signals():
    pipe = LiveMarketPipeline(symbols=["NSE:SBIN"], timeframe="5m", analysis_interval_bars=1)
    snaps = []
    pipe.on_snapshot(lambda s, snap: snaps.append(snap))
    pipe.start()
    # Force AUTH_ERROR state.
    pipe.health.on_auth_error()
    base = datetime(2024, 3, 6, 9, 15, tzinfo=KOL)
    pipe.ingest(_event(ts=base, ltp=100.0))
    pipe.ingest(_event(ts=base + timedelta(minutes=5), ltp=150.0))  # closes a candle
    # Even though a candle closed, snapshot must NOT be built while not HEALTHY.
    assert pipe.health.status != FeedStatus.HEALTHY
    assert len(snaps) == 0


# --- 7. historical bootstrap persistence ------------------------------------
def test_historical_bootstrap_persistence(tmp_path):
    from trading_system.storage.database import MarketStore

    store = MarketStore(f"sqlite:///{tmp_path/'m.db'}")
    # Fake provider with historical data.
    class FakeProv:
        name = "fake"

        def _fyers_symbol(self, s):
            return s

        def get_historical(self, sym, tf, limit=None, end=None):
            idx = pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC")
            return pd.DataFrame(
                {"open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3],
                 "close": [1, 2, 3], "volume": [10, 20, 30]}, index=idx
            ).rename_axis("timestamp")

    counts = bootstrap_historical(FakeProv(), store, ["NSE:SBIN"], "1d", lookback_bars=3)
    assert counts["NSE:SBIN"] == 3
    rows = store.load("NSE:SBIN", "1d")
    assert len(rows) == 3


# --- 8. historical bootstrap idempotency ------------------------------------
def test_historical_bootstrap_idempotent(tmp_path):
    from trading_system.storage.database import MarketStore

    store = MarketStore(f"sqlite:///{tmp_path/'m.db'}")

    class FakeProv:
        name = "fake"

        def _fyers_symbol(self, s):
            return s

        def get_historical(self, sym, tf, limit=None, end=None):
            idx = pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC")
            return pd.DataFrame(
                {"open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3],
                 "close": [1, 2, 3], "volume": [10, 20, 30]}, index=idx
            ).rename_axis("timestamp")

    c1 = bootstrap_historical(FakeProv(), store, ["NSE:SBIN"], "1d", lookback_bars=3)
    c2 = bootstrap_historical(FakeProv(), store, ["NSE:SBIN"], "1d", lookback_bars=3)
    assert c1["NSE:SBIN"] == 3
    assert c2["NSE:SBIN"] == 0  # re-ingest inserts nothing


# --- 9. malformed WS message handling ---------------------------------------
def test_malformed_ws_message_handling():
    from trading_system.india.fyers import FyersDataSocket
    from trading_system.india.fyers import FYERSMarketDataProvider

    class _FakeSocket(FyersDataSocket):
        def __init__(self):
            self._fy_to_internal = {"NSE:SBIN-EQ": "NSE:SBIN"}
            self.provider = FYERSMarketDataProvider()
            self.on_event = None

    fs = _FakeSocket()
    # No symbol -> skip (control frame)
    assert fs._normalize({"type": "ack"}) is None
    # No price -> skip
    assert fs._normalize({"symbol": "NSE:SBIN-EQ"}) is None
    # Non-dict -> skip
    assert fs._normalize("garbage") is None
    # Valid
    ev = fs._normalize(_sdk_dict("NSE:SBIN-EQ", 555.0, open_price=550, high_price=560,
                                 low_price=540, vol_traded_today=1000))
    assert ev is not None
    assert ev.open == 550 and ev.high == 560 and ev.low == 540 and ev.volume == 1000


# --- 10. disconnect/reconnect behavior (no tight loop) ----------------------
def test_socket_disconnect_does_not_spin():
    # Drive the socket's lifecycle callbacks; reconnect is owned by the SDK, so
    # our code must simply update health and not raise / not loop.
    from trading_system.india.fyers import FyersDataSocket
    from trading_system.india.fyers import FYERSMarketDataProvider
    import time

    health = DataHealthMonitor()

    class _FakeSocket(FyersDataSocket):
        def __init__(self):
            self._closed = False
            self._on_connect_cb = None
            self._on_disconnect_cb = None
            self._on_auth_error_cb = None
            self._on_invalid_cb = None

    fs = _FakeSocket()
    fs.on_connect_cb(health.on_connect)
    fs.on_disconnect_cb(health.on_disconnect)
    fs.on_auth_error_cb(health.on_auth_error)
    health.on_connect()
    assert health.status == FeedStatus.HEALTHY
    # Simulate a disconnect (the SDK would handle the actual reconnect).
    fs._on_sdk_close({"code": 1, "message": "closed"})
    assert health.status == FeedStatus.DISCONNECTED
    # Simulate auth error
    fs._on_sdk_error({"type": "AUTH_TYPE", "code": 803})
    assert health.status == FeedStatus.AUTH_ERROR


# --- 11. no AI call per tick -------------------------------------------------
def test_no_ai_per_tick():
    pipe = LiveMarketPipeline(symbols=["NSE:SBIN"], timeframe="5m", analysis_interval_bars=3)
    ai_calls = []
    pipe.on_snapshot(lambda s, snap: ai_calls.append(snap))
    pipe.start()
    base = datetime(2024, 3, 6, 9, 15, tzinfo=KOL)
    # Many ticks within a single bar -> zero snapshot calls.
    for i in range(10):
        pipe.ingest(_event(ts=base + timedelta(seconds=10 * i), ltp=100 + i * 0.1))
    assert len(ai_calls) == 0


# --- 12. AI invocation follows ANALYSIS_INTERVAL_BARS ------------------------
def test_ai_invocation_follows_interval():
    pipe = LiveMarketPipeline(symbols=["NSE:SBIN"], timeframe="5m", analysis_interval_bars=2)
    ai_calls = []
    pipe.on_snapshot(lambda s, snap: ai_calls.append(snap))
    pipe.start()
    base = datetime(2024, 3, 6, 9, 15, tzinfo=KOL)
    # bar A: 09:15..09:20 (close at 09:20)
    pipe.ingest(_event(ts=base, ltp=100.0))
    pipe.ingest(_event(ts=base + timedelta(minutes=5), ltp=110.0))  # closes A (bar1)
    # bar B: 09:20..09:25 (close at 09:25)
    pipe.ingest(_event(ts=base + timedelta(minutes=10), ltp=120.0))  # closes B (bar2 -> snapshot #1)
    assert len(ai_calls) == 1
    # bar C -> closes at 09:30; only after 2 more bars do we snapshot again.
    pipe.ingest(_event(ts=base + timedelta(minutes=15), ltp=130.0))  # closes C (bar3)
    pipe.ingest(_event(ts=base + timedelta(minutes=20), ltp=140.0))  # closes D (bar4 -> snapshot #2)
    assert len(ai_calls) == 2


# --- provider-independent: normalization works for index too ----------------
def test_normalize_index_symbol():
    from trading_system.india.fyers import FyersDataSocket
    from trading_system.india.fyers import FYERSMarketDataProvider

    class _FakeSocket(FyersDataSocket):
        def __init__(self):
            self._fy_to_internal = {"NSE:NIFTY50-INDEX": "NSE:NIFTY50"}
            self.provider = FYERSMarketDataProvider()
            self.on_event = None

    fs = _FakeSocket()
    ev = fs._normalize(_sdk_dict("NSE:NIFTY50-INDEX", 22000.5))
    assert ev is not None
    assert ev.symbol == "NSE:NIFTY50"
    assert ev.exchange == "NSE"


# --- regression: observed FYERS control frames (real connect 2026-08-27) -----
def test_observed_fyers_control_frames_skipped():
    """Real FYERS v3 WS sends these exact control frames on connect; they must
    not produce market events."""
    from trading_system.india.fyers import FyersDataSocket
    from trading_system.india.fyers import FYERSMarketDataProvider

    class _FakeSocket(FyersDataSocket):
        def __init__(self):
            self._fy_to_internal = {"NSE:SBIN-EQ": "NSE:SBIN"}
            self.provider = FYERSMarketDataProvider()
            self.on_event = None

    fs = _FakeSocket()
    received = []
    fs.on_event = lambda e: received.append(e)
    # Exactly the frames observed from a live FYERS session:
    fs._on_sdk_message({"type": "cn", "code": 200, "message": "Authentication done", "s": "ok"})
    fs._on_sdk_message({"type": "lit", "code": 200, "message": "Lite Mode On", "s": "ok"})
    assert received == []


# --- historical seed into closed-candle pipeline ----------------------------
def test_seed_historical_df_feeds_pipeline():
    pipe = LiveMarketPipeline(symbols=["NSE:SBIN"], timeframe="1d", analysis_interval_bars=1)
    idx = pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC")
    df = pd.DataFrame(
        {"open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3],
         "close": [1, 2, 3], "volume": [10, 20, 30]}, index=idx
    ).rename_axis("timestamp")
    n = pipe.seed_historical_df("NSE:SBIN", df)
    assert n == 3
    snap = pipe.candle_pipeline.make_snapshot("NSE:SBIN")
    assert snap.data_points == 3
    assert snap.latest_price == 3.0
