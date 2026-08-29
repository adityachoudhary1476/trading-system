"""Market-data health monitor (stale-data / feed-quality detection).

Tracks feed liveness and data quality. Exposes a simple status enum:
  HEALTHY, STALE, DISCONNECTED, AUTH_ERROR, INVALID_DATA

Signals/analysis must NOT be generated when the feed is STALE / DISCONNECTED /
AUTH_ERROR / INVALID_DATA. The monitor is pure (no network) and is driven by the
live pipeline calling `tick()`, `on_connect`, `on_disconnect`, `on_error`,
`on_invalid`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class FeedStatus(str, Enum):
    HEALTHY = "healthy"
    STALE = "stale"
    DISCONNECTED = "disconnected"
    AUTH_ERROR = "auth_error"
    INVALID_DATA = "invalid_data"


@dataclass
class FeedMetrics:
    events_received: int = 0
    events_rejected: int = 0
    duplicate_events: int = 0
    candles_generated: int = 0
    candles_rejected: int = 0
    latest_event_ts: Optional[datetime] = None
    latest_closed_candle: Optional[datetime] = None
    last_update_ts: float = 0.0
    connected: bool = False
    auth_ok: bool = True


@dataclass
class ConnectionState:
    connected: bool = False
    auth_ok: bool = True
    last_status: FeedStatus = FeedStatus.DISCONNECTED


class DataHealthMonitor:
    def __init__(self, stale_seconds: int = 60) -> None:
        self.stale_seconds = stale_seconds
        self.metrics = FeedMetrics()
        self.status = FeedStatus.DISCONNECTED
        self._last_msg_wall = 0.0

    def on_connect(self) -> None:
        self.metrics.connected = True
        self.status = FeedStatus.HEALTHY

    def on_disconnect(self) -> None:
        self.metrics.connected = False
        self.status = FeedStatus.DISCONNECTED

    def on_auth_error(self) -> None:
        self.metrics.auth_ok = False
        self.metrics.connected = False
        self.status = FeedStatus.AUTH_ERROR

    def on_auth_status(self, status: str) -> None:
        """Map an external AuthStatus (from india.token_manager) onto the monitor.

        Only AUTH_OK clears the auth-error flag; everything else marks auth broken.
        Existing DataHealthMonitor behavior (STALE/DISCONNECTED/INVALID_DATA) is intact.
        """
        if status == "auth_ok":
            self.metrics.auth_ok = True
            if self.metrics.connected:
                self.status = FeedStatus.HEALTHY
        else:
            self.metrics.auth_ok = False
            self.metrics.connected = False
            self.status = FeedStatus.AUTH_ERROR

    def on_invalid(self) -> None:
        self.metrics.events_rejected += 1
        self.metrics.candles_rejected += 1
        self.status = FeedStatus.INVALID_DATA

    def tick(self, ts: Optional[datetime] = None, now: Optional[float] = None) -> None:
        """Record a received (valid) event and refresh liveness."""
        import time

        now = now if now is not None else time.time()
        self.metrics.events_received += 1
        self.metrics.latest_event_ts = ts or datetime.now(timezone.utc)
        self.metrics.last_update_ts = now
        self._last_msg_wall = now
        if self.metrics.auth_ok and self.metrics.connected:
            self.status = FeedStatus.HEALTHY

    def record_duplicate(self) -> None:
        self.metrics.duplicate_events += 1

    def record_candle(self, closed_ts: datetime) -> None:
        self.metrics.candles_generated += 1
        self.metrics.latest_closed_candle = closed_ts

    def evaluate(self, now: Optional[float] = None) -> FeedStatus:
        """Recompute status. Call periodically (or after tick)."""
        import time

        now = now if now is not None else time.time()
        if self.status == FeedStatus.AUTH_ERROR:
            return self.status
        if not self.metrics.connected:
            self.status = FeedStatus.DISCONNECTED
            return self.status
        if not self.metrics.auth_ok:
            self.status = FeedStatus.AUTH_ERROR
            return self.status
        if self._last_msg_wall and (now - self._last_msg_wall) > self.stale_seconds:
            self.status = FeedStatus.STALE
        else:
            self.status = FeedStatus.HEALTHY
        return self.status

    def is_safe_for_signals(self) -> bool:
        return self.evaluate() == FeedStatus.HEALTHY

    def snapshot(self) -> dict:
        m = self.metrics
        return {
            "status": self.status.value,
            "events_received": m.events_received,
            "events_rejected": m.events_rejected,
            "duplicate_events": m.duplicate_events,
            "candles_generated": m.candles_generated,
            "candles_rejected": m.candles_rejected,
            "latest_event_ts": m.latest_event_ts.isoformat() if m.latest_event_ts else None,
            "latest_closed_candle": m.latest_closed_candle.isoformat() if m.latest_closed_candle else None,
            "connected": m.connected,
        }
