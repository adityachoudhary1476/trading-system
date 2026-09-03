"""Authoritative live market state primitives."""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from typing import Callable, Optional


class TimestampValidationError(ValueError):
    """Raised when a provider timestamp cannot be trusted."""


def normalize_timestamp_ms(value: object, *, now_ms: Optional[int] = None) -> Optional[int]:
    """Normalize provider epoch seconds/milliseconds or ISO time to epoch ms."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise TimestampValidationError("timestamp must be timezone-aware")
        result = int(value.astimezone(timezone.utc).timestamp() * 1000)
    elif isinstance(value, bool) or not isinstance(value, (int, float)):
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise TimestampValidationError("invalid ISO timestamp") from exc
            if parsed.tzinfo is None:
                raise TimestampValidationError("timestamp must be timezone-aware")
            result = int(parsed.astimezone(timezone.utc).timestamp() * 1000)
        else:
            raise TimestampValidationError("timestamp must be epoch seconds, ms, or ISO")
    else:
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0:
            raise TimestampValidationError("timestamp must be finite and non-negative")
        result = int(numeric * 1000) if numeric < 1e12 else int(numeric)

    minimum_ms = 946684800000
    reference_ms = now_ms if now_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    if result < minimum_ms or result > reference_ms + 86_400_000:
        raise TimestampValidationError("timestamp is outside the plausible market-time range")
    return result


class MarketState(str, Enum):
    FRESH = "fresh"
    UNCHANGED = "unchanged"
    STALE = "stale"
    EXPIRED = "expired"
    UNAVAILABLE = "unavailable"
    CLOSED = "closed"


@dataclass(frozen=True)
class LiveMarketSnapshot:
    snapshot_id: str
    version: int
    symbol: str
    instrument_key: str
    provider: str
    price: float
    quote_type: str
    market_timestamp: Optional[int]
    fetched_at: int
    session: str
    freshness_ms: Optional[int]
    state: MarketState
    source_sequence: Optional[str]
    is_new_market_event: bool


SnapshotListener = Callable[[LiveMarketSnapshot], None]


class MarketStatePublisher:
    """Thread-safe, ordered publication of one latest snapshot per symbol."""

    def __init__(self, *, stale_after_ms: int = 5_000, expired_after_ms: int = 60_000) -> None:
        if stale_after_ms <= 0 or expired_after_ms < stale_after_ms:
            raise ValueError("invalid freshness thresholds")
        self.stale_after_ms = stale_after_ms
        self.expired_after_ms = expired_after_ms
        self._latest: dict[str, LiveMarketSnapshot] = {}
        self._listeners: set[SnapshotListener] = set()
        self._version = 0
        self._lock = RLock()

    def subscribe(self, listener: SnapshotListener) -> None:
        with self._lock:
            self._listeners.add(listener)

    def unsubscribe(self, listener: SnapshotListener) -> None:
        with self._lock:
            self._listeners.discard(listener)

    def latest(self, symbol: str) -> Optional[LiveMarketSnapshot]:
        with self._lock:
            return self._latest.get(symbol)

    def publish(self, *, symbol: str, instrument_key: str, price: float,
                market_timestamp: object, fetched_at: object, session: str,
                source_sequence: object = None, provider: str = "upstox",
                quote_type: str = "trade", now_ms: Optional[int] = None) -> LiveMarketSnapshot:
        if not math.isfinite(price) or price <= 0:
            raise ValueError("price must be finite and greater than zero")
        observed_ms = normalize_timestamp_ms(fetched_at, now_ms=now_ms)
        if observed_ms is None:
            raise TimestampValidationError("fetched_at is required")
        try:
            market_ms = normalize_timestamp_ms(market_timestamp, now_ms=observed_ms)
        except TimestampValidationError:
            market_ms = None

        sequence = None if source_sequence is None else str(source_sequence)
        with self._lock:
            previous = self._latest.get(symbol)
            is_new = previous is None or (
                sequence is not None and sequence != previous.source_sequence
            ) or (
                sequence is None and market_ms is not None and market_ms != previous.market_timestamp
            )
            if previous is not None and market_ms is not None and previous.market_timestamp is not None:
                if market_ms < previous.market_timestamp:
                    return replace(previous, state=MarketState.STALE, is_new_market_event=False)

            freshness = None if market_ms is None else max(0, observed_ms - market_ms)
            if session in {"CLOSED", "HOLIDAY"}:
                state = MarketState.CLOSED
            elif market_ms is None:
                state = MarketState.UNAVAILABLE
            elif freshness is not None and freshness > self.expired_after_ms:
                state = MarketState.EXPIRED
            elif freshness is not None and freshness > self.stale_after_ms:
                state = MarketState.STALE
            elif not is_new:
                state = MarketState.UNCHANGED
            else:
                state = MarketState.FRESH

            self._version += 1
            snapshot = LiveMarketSnapshot(
                snapshot_id=f"{symbol}:{self._version}", version=self._version,
                symbol=symbol, instrument_key=instrument_key, provider=provider,
                price=price, quote_type=quote_type, market_timestamp=market_ms,
                fetched_at=observed_ms, session=session, freshness_ms=freshness,
                state=state, source_sequence=sequence, is_new_market_event=is_new,
            )
            self._latest[symbol] = snapshot
            listeners = tuple(self._listeners)
        for listener in listeners:
            listener(snapshot)
        return snapshot