from datetime import datetime, timezone

import pandas as pd

from services.market_recovery import MarketRecovery, RecoveryState
from services.live_candle_read_model import LiveCandleReadModel
from src.trading_system.india.events import EventType, InternalMarketEvent
from src.trading_system.india.live_pipeline import LiveMarketPipeline


NOW = datetime(2024, 3, 6, 4, 0, tzinfo=timezone.utc)  # 09:30 IST


def _bars():
    return pd.DataFrame(
        {
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [10.0, 12.0],
        },
        index=pd.to_datetime(
            ["2024-03-06T03:45:00Z", "2024-03-06T03:50:00Z"], utc=True
        ),
    )


def test_recovery_backfills_and_replays_live_event_during_fetch():
    pipeline = LiveMarketPipeline(["NSE:SBIN"], timeframe="5m")
    pipeline.start()
    model = LiveCandleReadModel("5m")
    pipeline.subscribe_market_state(model.on_snapshot)

    class Provider:
        def get_historical(self, symbol, timeframe, **kwargs):
            pipeline.ingest(InternalMarketEvent(
                event_type=EventType.QUOTE,
                symbol=symbol,
                exchange="NSE",
                provider_symbol="NSE_EQ|SBIN",
                timestamp=datetime(2024, 3, 6, 3, 55, tzinfo=timezone.utc),
                ltp=103.0,
            ))
            return _bars()

    recovery = MarketRecovery(Provider(), pipeline, model)
    assert recovery.recover(["NSE:SBIN"], now=NOW)
    assert recovery.state is RecoveryState.HEALTHY
    state = model.read("NSE:SBIN", "5m", pipeline)
    assert state is not None
    assert len(state["candles"]) == 3
    assert state["current_candle"]["time"] == 1709697300000


def test_recovery_failure_is_degraded_without_fabricating_data():
    pipeline = LiveMarketPipeline(["NSE:SBIN"], timeframe="5m")
    model = LiveCandleReadModel("5m")

    class Provider:
        def get_historical(self, symbol, timeframe, **kwargs):
            raise OSError("provider unavailable")

    recovery = MarketRecovery(Provider(), pipeline, model)
    assert not recovery.recover(["NSE:SBIN"], now=NOW)
    assert recovery.state is RecoveryState.DEGRADED
    assert model.read("NSE:SBIN", "5m", pipeline) is None


def test_recovery_is_idempotent_for_same_historical_bars():
    pipeline = LiveMarketPipeline(["NSE:SBIN"], timeframe="5m")
    model = LiveCandleReadModel("5m")

    class Provider:
        def get_historical(self, symbol, timeframe, **kwargs):
            return _bars()

    recovery = MarketRecovery(Provider(), pipeline, model)
    assert recovery.recover(["NSE:SBIN"], now=NOW)
    assert recovery.recover(["NSE:SBIN"], now=NOW)
    state = model.read("NSE:SBIN", "5m", pipeline)
    assert state is not None
    assert len(state["candles"]) == 2


def test_recovery_with_store_survives_recreation(tmp_path):
    from src.trading_system.storage.database import MarketStore

    store = MarketStore(f"sqlite:///{tmp_path / 'market.db'}")

    class Provider:
        def get_historical(self, symbol, timeframe, **kwargs):
            return _bars()

    pipeline = LiveMarketPipeline(["NSE:SBIN"], timeframe="5m")
    model = LiveCandleReadModel("5m")
    first = MarketRecovery(Provider(), pipeline, model, store)
    assert first.recover(["NSE:SBIN"], now=NOW)

    restarted_pipeline = LiveMarketPipeline(["NSE:SBIN"], timeframe="5m")
    restarted_model = LiveCandleReadModel("5m")
    stored = store.load("NSE:SBIN", "5m")
    restarted_pipeline.seed_historical_df("NSE:SBIN", stored)
    restarted_model.seed_historical_df("NSE:SBIN", "5m", stored)
    second = MarketRecovery(Provider(), restarted_pipeline, restarted_model, store)
    assert second.recover(["NSE:SBIN"], now=NOW)
    assert store.count("NSE:SBIN", "5m") == 2
    assert store.get_recovery_point("NSE:SBIN", "5m")["recovery_status"] == "complete"
