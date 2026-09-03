from datetime import datetime, timezone

from services.live_candle_read_model import LiveCandleReadModel
from src.trading_system.india.closed_candle_pipeline import ClosedCandlePipeline
from src.trading_system.india.events import EventType, InternalMarketEvent
from src.trading_system.india.live_market_state import LiveMarketSnapshot, MarketState


def _snapshot(timestamp: int, version: int = 1) -> LiveMarketSnapshot:
    return LiveMarketSnapshot(
        snapshot_id=f"NSE:SBIN:{version}", version=version,
        symbol="NSE:SBIN", instrument_key="NSE_EQ|SBIN", provider="upstox",
        price=100.0, quote_type="trade", market_timestamp=timestamp,
        fetched_at=timestamp, session="REGULAR", freshness_ms=0,
        state=MarketState.FRESH, source_sequence=None,
        is_new_market_event=True,
    )


def test_read_model_exposes_provisional_and_closed_candles():
    timeframe = "5m"
    pipeline = ClosedCandlePipeline(timeframe)
    model = LiveCandleReadModel(timeframe)
    closed = []
    pipeline.on_closed(closed.append)
    ts = datetime(2024, 3, 6, 9, 0, tzinfo=timezone.utc)
    model.on_snapshot(_snapshot(int(ts.timestamp() * 1000)))
    pipeline.feed_event(InternalMarketEvent(
        event_type=EventType.QUOTE, symbol="NSE:SBIN", exchange="NSE",
        provider_symbol="NSE_EQ|SBIN", timestamp=ts, ltp=100.0,
    ))
    first = model.read("NSE:SBIN", timeframe, pipeline)
    assert first is not None
    assert first["current_candle"]["is_closed"] is False
    assert first["version"] == 1

    close_ts = datetime(2024, 3, 6, 9, 5, tzinfo=timezone.utc)
    model.on_snapshot(_snapshot(int(close_ts.timestamp() * 1000), version=2))
    pipeline.feed_event(InternalMarketEvent(
        event_type=EventType.QUOTE, symbol="NSE:SBIN", exchange="NSE",
        provider_symbol="NSE_EQ|SBIN", timestamp=close_ts, ltp=101.0,
    ))
    assert len(closed) == 1
    model.on_closed_candle(closed[0])
    second = model.read("NSE:SBIN", timeframe, pipeline)
    assert second is not None
    assert second["current_candle"] is not None
    assert second["current_candle"]["is_closed"] is False
    assert len(second["candles"]) == 2
    assert second["candles"][0]["is_closed"] is True
    assert second["version"] == 3