from datetime import datetime, timezone

import pytest

from trading_system.india.live_market_state import (
    MarketState,
    MarketStatePublisher,
    TimestampValidationError,
    normalize_timestamp_ms,
)
from trading_system.india.events import EventType, InternalMarketEvent
from trading_system.india.live_pipeline import LiveMarketPipeline


NOW = 1_700_000_000_000


def test_normalizes_seconds_and_milliseconds():
    assert normalize_timestamp_ms(1_700_000_000, now_ms=NOW) == NOW
    assert normalize_timestamp_ms(NOW, now_ms=NOW) == NOW
    assert normalize_timestamp_ms("2023-11-14T22:13:20Z", now_ms=NOW) == NOW


@pytest.mark.parametrize("value", [-1, 742.5, "invalid", datetime(2023, 1, 1)])
def test_rejects_untrusted_timestamps(value):
    with pytest.raises(TimestampValidationError):
        normalize_timestamp_ms(value, now_ms=NOW)


def test_missing_timestamp_is_not_fabricated():
    assert normalize_timestamp_ms(None, now_ms=NOW) is None


def test_publisher_distinguishes_new_and_unchanged_events():
    publisher = MarketStatePublisher(stale_after_ms=5_000, expired_after_ms=60_000)
    events = []
    publisher.subscribe(events.append)
    first = publisher.publish(
        symbol="NSE:SBIN", instrument_key="NSE_EQ|SBIN", price=100,
        market_timestamp=NOW, fetched_at=NOW, session="REGULAR", now_ms=NOW,
    )
    second = publisher.publish(
        symbol="NSE:SBIN", instrument_key="NSE_EQ|SBIN", price=101,
        market_timestamp=NOW, fetched_at=NOW + 1_000, session="REGULAR", now_ms=NOW + 1_000,
    )
    assert first.state is MarketState.FRESH
    assert second.state is MarketState.UNCHANGED
    assert second.is_new_market_event is False
    assert [event.version for event in events] == [1, 2]


def test_publisher_rejects_older_market_event_from_overwriting_latest():
    publisher = MarketStatePublisher()
    newer = publisher.publish(
        symbol="NSE:SBIN", instrument_key="NSE_EQ|SBIN", price=101,
        market_timestamp=NOW + 1_000, fetched_at=NOW + 1_000, session="REGULAR", now_ms=NOW + 1_000,
    )
    older = publisher.publish(
        symbol="NSE:SBIN", instrument_key="NSE_EQ|SBIN", price=99,
        market_timestamp=NOW, fetched_at=NOW + 2_000, session="REGULAR", now_ms=NOW + 2_000,
    )
    assert older.version == newer.version
    assert older.price == newer.price
    assert publisher.latest("NSE:SBIN") == newer


def test_publisher_marks_stale_and_closed_state():
    publisher = MarketStatePublisher(stale_after_ms=5_000, expired_after_ms=60_000)
    stale = publisher.publish(
        symbol="NSE:SBIN", instrument_key="NSE_EQ|SBIN", price=100,
        market_timestamp=NOW - 6_000, fetched_at=NOW, session="REGULAR", now_ms=NOW,
    )
    closed = publisher.publish(
        symbol="NSE:SBIN", instrument_key="NSE_EQ|SBIN", price=100,
        market_timestamp=NOW, fetched_at=NOW, session="CLOSED", now_ms=NOW,
    )
    assert stale.state is MarketState.STALE
    assert closed.state is MarketState.CLOSED


def test_live_event_snapshot_preserves_market_time_and_closes_candle():
    pipeline = LiveMarketPipeline(symbols=["NSE:SBIN"], timeframe="5m", analysis_interval_bars=1)
    snapshots = []
    closed = []
    pipeline.subscribe_market_state(snapshots.append)
    pipeline.candle_pipeline.on_closed(closed.append)
    pipeline.start()
    first = datetime(2024, 3, 6, 9, 15, tzinfo=timezone.utc)
    second = datetime(2024, 3, 6, 9, 20, tzinfo=timezone.utc)
    pipeline.ingest(InternalMarketEvent(
        event_type=EventType.QUOTE,
        symbol="NSE:SBIN",
        exchange="NSE",
        provider_symbol="NSE_EQ|SBIN",
        timestamp=first,
        fetched_at=first,
        ltp=100,
    ))
    pipeline.ingest(InternalMarketEvent(
        event_type=EventType.QUOTE,
        symbol="NSE:SBIN",
        exchange="NSE",
        provider_symbol="NSE_EQ|SBIN",
        timestamp=second,
        fetched_at=second,
        ltp=110,
    ))
    assert snapshots[0].market_timestamp == int(first.timestamp() * 1000)
    assert snapshots[0].fetched_at == int(first.timestamp() * 1000)
    assert snapshots[0].is_new_market_event
    assert len(closed) == 1
    assert closed[0].start.hour == 14
