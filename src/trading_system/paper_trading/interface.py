"""Paper-trading interface contract (Day 4: interface only, no execution).

The future paper trader consumes provider-agnostic types:
  * InternalMarketEvent (live ticks/quotes)
  * ClosedCandle (completed bars)
  * MarketSnapshot (structured analysis input)
  * Signal (deterministic decision output)

It must NOT care whether the source was FYERS, Angel One, Upstox, or Binance.
This module defines the consumer interface and a NO-OP reference implementation
so downstream wiring exists today without any position/trade logic.
"""
from __future__ import annotations

from typing import Protocol

from ..india.closed_candle_pipeline import ClosedCandle
from ..india.events import InternalMarketEvent
from ..models.snapshot import MarketSnapshot
from ..signals import Signal


class PaperTrader(Protocol):
    """Contract a future paper trader must satisfy."""

    def on_market_event(self, event: InternalMarketEvent) -> None:
        """Receive a normalized live market event (never raw provider JSON)."""
        ...

    def on_closed_candle(self, candle: ClosedCandle) -> None:
        """Receive a completed candle."""
        ...

    def on_snapshot(self, snapshot: MarketSnapshot) -> None:
        """Receive a structured MarketSnapshot for analysis cadence."""
        ...

    def on_signal(self, signal: Signal) -> None:
        """Receive a deterministic signal (may be ignored by paper bookkeeping)."""
        ...


class NoOpPaperTrader:
    """Reference implementation that records events for wiring/testing only.

    It performs NO trading, NO position sizing, NO order placement. It exists so
    the event bus, closed-candle pipeline, and signal engine have a valid sink
    that proves the decoupling works end-to-end without live-money logic.
    """

    def __init__(self) -> None:
        self.events: list[InternalMarketEvent] = []
        self.candles: list[ClosedCandle] = []
        self.snapshots: list[MarketSnapshot] = []
        self.signals: list[Signal] = []

    def on_market_event(self, event: InternalMarketEvent) -> None:
        self.events.append(event)

    def on_closed_candle(self, candle: ClosedCandle) -> None:
        self.candles.append(candle)

    def on_snapshot(self, snapshot: MarketSnapshot) -> None:
        self.snapshots.append(snapshot)

    def on_signal(self, signal: Signal) -> None:
        self.signals.append(signal)

    def reset(self) -> None:
        self.events.clear()
        self.candles.clear()
        self.snapshots.clear()
        self.signals.clear()
