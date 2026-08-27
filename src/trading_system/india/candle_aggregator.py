"""Candle aggregator: turn ticks/quotes into OHLCV bars at a target timeframe.

Deterministic and timezone-aware. Used when a provider (FYERS symbolUpdate mode)
delivers quote updates rather than ready-made candles. Aggregation rules:
  * Open  = first price in the bar window
  * High  = max price
  * Low   = min price
  * Close = last price
  * Volume= summed traded volume (0 if unavailable)
Session boundaries are respected: a bar never spans across a market close.

The aggregator is intentionally simple and fully unit-testable. It does NOT talk
to the network and does NOT emit provisional/closed status — that is the caller's
job via `CandleState`.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from .market_calendar import KOLKATA


# Timeframe -> minutes.
_TIMEFRAMES = {
    "1m": 1, "2m": 2, "3m": 3, "5m": 5, "10m": 10, "15m": 15,
    "20m": 20, "30m": 30, "45m": 45, "60m": 60, "1h": 60,
    "2h": 120, "3h": 180, "4h": 240, "1d": 1440,
}


def timeframe_minutes(tf: str) -> int:
    if tf not in _TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe: {tf}")
    return _TIMEFRAMES[tf]


def _bar_start(ts: datetime, tf: str) -> datetime:
    mins = timeframe_minutes(tf)
    # Align to the bar grid in Asia/Kolkata (session-local).
    k = ts.astimezone(KOLKATA)
    minutes_since_midnight = k.hour * 60 + k.minute
    bucket = (minutes_since_midnight // mins) * mins
    bar = k.replace(hour=bucket // 60, minute=bucket % 60, second=0, microsecond=0)
    return bar


@dataclass
class AggregatedBar:
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    ticks: int = 0


class CandleAggregator:
    """Aggregates ticks into bars of a fixed timeframe."""

    def __init__(self, timeframe: str) -> None:
        self.timeframe = timeframe
        self._current: AggregatedBar | None = None
        self._completed: deque[AggregatedBar] = deque()

    def update(self, ts: datetime, price: float, volume: float = 0.0) -> list[AggregatedBar]:
        """Feed a tick. Returns any bars that just closed."""
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=__import__("datetime").timezone.utc)
        bar_start = _bar_start(ts, self.timeframe)
        if self._current is None:
            self._current = AggregatedBar(
                start=bar_start, open=price, high=price, low=price,
                close=price, volume=volume, ticks=1,
            )
            return []
        if bar_start == self._current.start:
            b = self._current
            b.high = max(b.high, price)
            b.low = min(b.low, price)
            b.close = price
            b.volume += volume
            b.ticks += 1
            return []
        # New bar -> close the old one.
        finished = self._current
        self._completed.append(finished)
        self._current = AggregatedBar(
            start=bar_start, open=price, high=price, low=price,
            close=price, volume=volume, ticks=1,
        )
        return [finished]

    @property
    def provisional(self) -> AggregatedBar | None:
        """The currently-forming (provisional) bar. Never treat as closed."""
        return self._current

    def seed_bar(
        self,
        start: datetime,
        open: float,
        high: float,
        low: float,
        close: float,
        volume: float = 0.0,
    ) -> None:
        """Inject an already-closed bar (e.g. from historical bootstrap).

        Appended to the completed queue so downstream snapshots reflect real
        OHLC without re-aggregating from ticks. The current provisional bar is
        left untouched so live ticks continue from the last close.
        """
        if start.tzinfo is None:
            start = start.replace(tzinfo=__import__("datetime").timezone.utc)
        self._completed.append(
            AggregatedBar(
                start=start, open=open, high=high, low=low, close=close,
                volume=volume, ticks=1,
            )
        )

    def flush_completed(self) -> list[AggregatedBar]:
        out = list(self._completed)
        self._completed.clear()
        return out

    def to_dataframe(self, include_provisional: bool = False) -> pd.DataFrame:
        bars = list(self._completed)
        if include_provisional and self._current is not None:
            bars = bars + [self._current]
        if not bars:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame(
            [
                {
                    "timestamp": b.start,
                    "open": b.open, "high": b.high, "low": b.low,
                    "close": b.close, "volume": b.volume,
                }
                for b in bars
            ]
        ).set_index("timestamp")
        df.index.name = "timestamp"
        return df
