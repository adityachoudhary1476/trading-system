"""Shared data types for the market-data layer."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class OHLCV:
    """A single OHLCV bar. Timestamps are timezone-aware (UTC)."""

    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: str
    provider: str = ""

    def __post_init__(self) -> None:
        # Ensure tz-aware timestamps; naive -> assume UTC.
        if self.timestamp.tzinfo is None:
            object.__setattr__(
                self, "timestamp", self.timestamp.replace(tzinfo=timezone.utc)
            )
