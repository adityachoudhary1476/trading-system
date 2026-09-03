"""Closed-candle pipeline: generate MarketSnapshot only from COMPLETED candles.

Responsibilities:
  * Feed normalized events (or raw ticks) into a per-symbol CandleAggregator.
  * Distinguish PROVISIONAL (currently forming) from CLOSED (interval completed).
  * On a candle CLOSE, emit a `ClosedCandle` to subscribers (and optionally build a
    MarketSnapshot for the AI analyst at a configurable interval). The AI NEVER runs
    on provisional data and NEVER on the tick hot path.
  * Guard correctness at boundaries: late ticks, duplicate ticks, out-of-order
    ticks, missing ticks, session boundaries, and market close.

The current (provisional) candle must never overwrite closed-candle state. The
`MarketSnapshot` used for analysis is built strictly from closed history.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable

from ..config import settings
from ..india.candle_aggregator import AggregatedBar, CandleAggregator, timeframe_minutes
from ..india.events import EventType, InternalMarketEvent
from ..india.market_calendar import DEFAULT_CALENDAR, KOLKATA
from ..models.snapshot import MarketSnapshot, build_snapshot_from_df


class CandleState(str, Enum):
    PROVISIONAL = "provisional"   # forming; not safe for analysis
    CLOSED = "closed"             # completed; safe for analysis


@dataclass
class ClosedCandle:
    symbol: str
    timeframe: str
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    state: CandleState = CandleState.CLOSED


# A consumer of a closed candle (e.g. DB writer, indicator engine, snapshot builder).
ClosedCandleConsumer = Callable[[ClosedCandle], None]


class ClosedCandlePipeline:
    """Per-symbol aggregation + closed-candle emission with boundary guards."""

    def __init__(self, timeframe: str, analysis_interval_bars: int | None = None) -> None:
        self.timeframe = timeframe
        self.minutes = timeframe_minutes(timeframe)
        self.aggregators: dict[str, CandleAggregator] = {}
        self.closed_consumers: list[ClosedCandleConsumer] = []
        self._closed_persistence: Callable[[ClosedCandle], bool] | None = None
        # Track seen (symbol, bar_start) to drop duplicate/late ticks deterministically.
        self._seen: dict[str, set[datetime]] = {}
        self._analysis_interval = analysis_interval_bars or settings.market.analysis_interval_bars

    def on_closed(self, consumer: ClosedCandleConsumer) -> None:
        self.closed_consumers.append(consumer)

    def set_closed_persistence(self, consumer: Callable[[ClosedCandle], bool]) -> None:
        self._closed_persistence = consumer

    def _agg(self, symbol: str) -> CandleAggregator:
        return self.aggregators.setdefault(symbol, CandleAggregator(self.timeframe))

    def _seen_for(self, symbol: str) -> set[datetime]:
        return self._seen.setdefault(symbol, set())

    def feed_event(self, event: InternalMarketEvent) -> list[ClosedCandle]:
        """Process one normalized event; return any candles that closed."""
        if event.event_type not in (EventType.QUOTE, EventType.TRADE):
            return []
        price = event.ltp if event.ltp is not None else (event.close or 0.0)
        if price <= 0:
            return []
        # Use the event's timestamp (provider-normalized UTC).
        ts = event.timestamp
        phase = DEFAULT_CALENDAR.phase(ts).value
        if phase != "regular":
            if phase == "post_market":
                return self._close_active(event.symbol)
            return []
        bar_start = self._bar_start(ts)
        seen = self._seen_for(event.symbol)
        # Session boundary: if the event is outside a trading session, still record
        # the bar start but never let a provisional closed-candle overwrite history.
        if bar_start in seen:
            # Duplicate tick for an already-closed bar -> ignore to avoid double-count.
            # (Out-of-order/late ticks for the current bar are folded into the agg.)
            if self._agg(event.symbol).provisional and self._agg(event.symbol).provisional.start == bar_start:
                pass  # late tick into current bar: fold in below
            else:
                return []
        completed = self._agg(event.symbol).update(ts, price, volume=event.volume or 0.0)
        out: list[ClosedCandle] = []
        for b in completed:
            seen.add(b.start)  # mark closed so late/duplicate ticks are dropped
            cc = ClosedCandle(
                symbol=event.symbol, timeframe=self.timeframe,
                start=b.start, open=b.open, high=b.high, low=b.low,
                close=b.close, volume=b.volume, state=CandleState.CLOSED,
            )
            if self._closed_persistence is not None and not self._closed_persistence(cc):
                continue
            out.append(cc)
            for c in self.closed_consumers:
                c(cc)
        return out

    def _close_active(self, symbol: str) -> list[ClosedCandle]:
        """Close a regular-session bar at the first post-market boundary."""
        agg = self.aggregators.get(symbol)
        if agg is None:
            return []
        bar = agg.close_current()
        if bar is None:
            return []
        seen = self._seen_for(symbol)
        if bar.start in seen:
            return []
        seen.add(bar.start)
        candle = ClosedCandle(
            symbol=symbol, timeframe=self.timeframe, start=bar.start,
            open=bar.open, high=bar.high, low=bar.low, close=bar.close,
            volume=bar.volume, state=CandleState.CLOSED,
        )
        if self._closed_persistence is not None and not self._closed_persistence(candle):
            return []
        for consumer in self.closed_consumers:
            consumer(candle)
        return [candle]

    def _bar_start(self, ts: datetime) -> datetime:
        from ..india.candle_aggregator import _bar_start as _bs

        return _bs(ts, self.timeframe)

    def provisional_snapshot_is_safe(self) -> bool:
        """The pipeline NEVER builds analysis snapshots from provisional candles."""
        return False

    def provisional_candle(self, symbol: str) -> AggregatedBar | None:
        """Return a copy of the active candle for read-model consumers."""
        provisional = self.aggregators.get(symbol)
        bar = provisional.provisional if provisional is not None else None
        if bar is None:
            return None
        return AggregatedBar(
            start=bar.start, open=bar.open, high=bar.high, low=bar.low,
            close=bar.close, volume=bar.volume, ticks=bar.ticks,
        )

    def build_closed_history_df(self, symbol: str) -> "object":
        """Collect closed bars for a symbol into a DataFrame for snapshot building."""
        import pandas as pd

        agg = self.aggregators.get(symbol)
        if agg is None:
            return pd.DataFrame()
        bars = list(agg.completed)  # only completed bars; preserve history for later snapshots
        if not bars:
            return pd.DataFrame()
        df = pd.DataFrame(
            [
                {"timestamp": b.start, "open": b.open, "high": b.high,
                 "low": b.low, "close": b.close, "volume": b.volume}
                for b in bars
            ]
        ).set_index("timestamp")
        df.index.name = "timestamp"
        return df

    def make_snapshot(self, symbol: str) -> MarketSnapshot:
        """Build a MarketSnapshot strictly from CLOSED bars (no look-ahead)."""
        df = self.build_closed_history_df(symbol)
        if df.empty:
            raise RuntimeError(f"No closed candles yet for {symbol}")
        snap = build_snapshot_from_df(df, symbol, self.timeframe)
        # Structural guarantee that the snapshot uses only closed-bar data:
        # the no-look-ahead invariant must hold (timestamp == last closed bar).
        assert snap.lookahead_safe
        assert snap.timestamp == snap.last_bar_timestamp
        return snap
