"""Provider-independent internal event bus.

A simple pub/sub bus that decouples market-data producers (FYERS WebSocket,
Binance, a replay source) from consumers (candle aggregator, indicators, DB,
paper trader, Telegram, AI snapshot generator). Downstream consumers receive only
normalized `InternalMarketEvent` objects — never provider-specific JSON.

The AI must NOT run per-tick (see closed_candle_pipeline / config). The bus is the
single fan-out point; placement of the AI is decided by subscribers.
"""
from __future__ import annotations

from typing import Callable

from .events import InternalMarketEvent


# A consumer is any callable accepting a single InternalMarketEvent.
EventConsumer = Callable[[InternalMarketEvent], None]


class EventBus:
    """In-process event bus with named topic subscriptions."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventConsumer]] = {}
        self._any: list[EventConsumer] = []

    def subscribe(self, topic: str, consumer: EventConsumer) -> None:
        self._subscribers.setdefault(topic, []).append(consumer)

    def subscribe_all(self, consumer: EventConsumer) -> None:
        self._any.append(consumer)

    def publish(self, event: InternalMarketEvent) -> None:
        for c in self._any:
            c(event)
        topic = event.symbol
        for c in self._subscribers.get(topic, []):
            c(event)

    def subscriber_count(self) -> int:
        return len(self._any) + sum(len(v) for v in self._subscribers.values())

    def clear(self) -> None:
        self._subscribers.clear()
        self._any.clear()
