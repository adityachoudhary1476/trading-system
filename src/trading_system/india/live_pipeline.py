"""Live market-data pipeline orchestration (provider-independent wiring).

This glues provider-specific live data (FYERS today; any future provider later)
into the normalized downstream stack:

    Provider socket -> InternalMarketEvent -> EventBus -> ClosedCandlePipeline
                      -> ClosedCandle -> DataHealthMonitor
                      -> MarketSnapshot (only every ANALYSIS_INTERVAL_BARS, on close)
                      -> AI analyst / signal layer (consumer-supplied)

SAFETY BOUNDARY: this module is DATA + PAPER only. It never places orders and
never calls brokerage execution. The AI runs on candle close at a configurable
interval — never per tick.

The pipeline is driven by a normalized ``InternalMarketEvent`` stream, so it does
not know whether the source was FYERS, Binance, or a replay fixture.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from ..config import settings, log
from .candle_aggregator import timeframe_minutes
from .closed_candle_pipeline import ClosedCandle, ClosedCandlePipeline
from .data_health import DataHealthMonitor, FeedStatus
from .event_bus import EventBus
from .events import EventType, InternalMarketEvent


# Consumer invoked when a MarketSnapshot is ready (e.g. AI analyst + signal).
SnapshotConsumer = Callable[[str, object], None]  # (symbol, MarketSnapshot) -> None


class LiveMarketPipeline:
    """Owns the normalized live-data graph and feeds it from a provider socket.

    Usage:
        pipe = LiveMarketPipeline(symbols=["NSE:SBIN"], timeframe="5m")
        pipe.on_snapshot(my_ai_consumer)          # optional
        socket = provider.connect_live(symbols, on_event=pipe.ingest)
        pipe.attach_socket(socket)                 # wires health callbacks
        ... run ...
        pipe.stop()
    """

    def __init__(
        self,
        symbols: list[str],
        timeframe: str = "1m",
        analysis_interval_bars: int | None = None,
        health: Optional[DataHealthMonitor] = None,
        bus: Optional[EventBus] = None,
    ) -> None:
        self.symbols = list(symbols)
        self.timeframe = timeframe
        self.analysis_interval_bars = (
            analysis_interval_bars or settings.market.analysis_interval_bars
        )
        self.bus = bus or EventBus()
        self.health = health or DataHealthMonitor(
            stale_seconds=settings.market.stale_seconds
        )
        self.candle_pipeline = ClosedCandlePipeline(
            timeframe, analysis_interval_bars=self.analysis_interval_bars
        )
        # Closed-candle consumers: store to DB + feed health + maybe snapshot.
        self._bars_since_snapshot: dict[str, int] = {s: 0 for s in symbols}
        self._snapshot_consumer: Optional[SnapshotConsumer] = None
        self._running = False

        # Wire closed-candle consumer: record health + maybe build snapshot.
        self.candle_pipeline.on_closed(self._on_closed_candle)

    # -- public wiring --------------------------------------------------------
    def on_snapshot(self, consumer: SnapshotConsumer) -> None:
        """Register the AI/snapshot consumer. Called only per ANALYSIS_INTERVAL_BARS
        (never per tick)."""
        self._snapshot_consumer = consumer

    def attach_socket(self, socket) -> None:
        """Wire the provider socket's health callbacks into the monitor."""
        if hasattr(socket, "on_connect_cb"):
            socket.on_connect_cb(self.health.on_connect)
        if hasattr(socket, "on_disconnect_cb"):
            socket.on_disconnect_cb(self.health.on_disconnect)
        if hasattr(socket, "on_auth_error_cb"):
            socket.on_auth_error_cb(self.health.on_auth_error)
        if hasattr(socket, "on_invalid_cb"):
            socket.on_invalid_cb(self.health.on_invalid)
        self._socket = socket

    # -- ingestion (provider -> normalized -> bus -> pipeline) ---------------
    def ingest(self, event: InternalMarketEvent) -> None:
        """Entry point for a normalized event from any provider socket."""
        if not self._running:
            return
        if event is None or event.event_type not in (EventType.QUOTE, EventType.TRADE):
            return
        # Feed the bus (other consumers may subscribe_all); then the candle pipe.
        self.bus.publish(event)
        self.health.tick(ts=event.timestamp)
        closed = self.candle_pipeline.feed_event(event)
        # closed candles already recorded by _on_closed_candle; nothing else here.
        _ = closed  # (kept explicit; snapshot logic lives in _on_closed_candle)

    # -- closed-candle handler ----------------------------------------------
    def _on_closed_candle(self, cc: ClosedCandle) -> None:
        self.health.record_candle(cc.start)
        log.debug("Closed candle %s %s", cc.symbol, cc.start)
        # Only build a snapshot when the feed is healthy AND the interval elapses.
        self._bars_since_snapshot[cc.symbol] = self._bars_since_snapshot.get(cc.symbol, 0) + 1
        if self._bars_since_snapshot[cc.symbol] < self.analysis_interval_bars:
            return
        if not self.health.is_safe_for_signals():
            log.info("Skipping snapshot: feed not healthy (%s)", self.health.status.value)
            return
        self._bars_since_snapshot[cc.symbol] = 0
        if self._snapshot_consumer is None:
            return
        # Build snapshot from CLOSED history only (no look-ahead).
        try:
            snap = self.candle_pipeline.make_snapshot(cc.symbol)
        except RuntimeError as e:
            log.warning("Snapshot build skipped: %s", e)
            return
        self._snapshot_consumer(cc.symbol, snap)

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        self._running = True
        self.health.on_connect()

    def stop(self) -> None:
        self._running = False
        self.health.on_disconnect()
        if getattr(self, "_socket", None) is not None:
            try:
                self._socket.close()
            except Exception as e:  # pragma: no cover - best effort
                log.debug("socket close error: %s", e)

    @property
    def status(self) -> FeedStatus:
        return self.health.evaluate()

    # -- historical seed (PHASE 4) -------------------------------------------
    def seed_historical_df(self, symbol: str, df) -> int:
        """Seed the closed-candle aggregator from a historical OHLCV DataFrame.

        Each stored closed bar is injected via ``CandleAggregator.seed_bar`` so the
        AI snapshot reflects real OHLC. Idempotent w.r.t. the storage layer (the
        caller persists via MarketStore separately). Returns number of bars seeded.
        """
        if df is None or len(df) == 0:
            return 0
        agg = self.candle_pipeline._agg(symbol)
        count = 0
        for ts, row in df.iterrows():
            agg.seed_bar(
                ts,
                float(row["open"]), float(row["high"]), float(row["low"]),
                float(row["close"]), float(row.get("volume", 0) or 0),
            )
            count += 1
        return count


def bootstrap_historical(
    provider,
    store,
    symbols: list[str],
    timeframe: str,
    lookback_bars: int | None = None,
    end: Optional[object] = None,
) -> dict[str, int]:
    """Load recent historical closed candles into the pipeline + storage.

    Uses the provider's historical API, normalizes each candle into the existing
    ``MarketStore`` (idempotent: re-ingesting the same key inserts nothing), and
    seeds the closed-candle pipeline so live ticks continue from the last close.

    Provider-agnostic: works for FYERS, Binance, or any MarketDataProvider.

    Returns a mapping of symbol -> number of NEW rows persisted (0 means already
    present / idempotent).
    """
    from ..storage.database import OHLCVRecord  # noqa: F401 (ensure import path)

    lookback = lookback_bars or settings.market.lookback_bars
    new_counts: dict[str, int] = {}
    for sym in symbols:
        df = provider.get_historical(sym, timeframe, limit=lookback, end=end)
        if df is None or len(df) == 0:
            new_counts[sym] = 0
            continue
        # Persist idempotently via the provider-agnostic storage layer.
        rows = []
        fy_sym = None
        try:
            fy_sym = provider._fyers_symbol(sym)
        except Exception:
            fy_sym = None
        exchange = sym.split(":", 1)[0] if ":" in sym else "UNKNOWN"
        for ts, row in df.iterrows():
            rows.append(
                {
                    "symbol": sym,
                    "timeframe": timeframe,
                    "exchange": exchange,
                    "timestamp": ts,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                    "provider": provider.name,
                }
            )
        inserted = store.upsert_many(rows)
        new_counts[sym] = inserted
        log.info("Bootstrap %s %s: %d rows (%d new)", sym, timeframe, len(rows), inserted)
    return new_counts
