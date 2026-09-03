"""Normalized market event model shared by all Indian providers.

The future paper trader consumes these InternalMarketEvent objects, never raw
FYERS responses. This is the decoupling boundary required by the architecture.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class EventType(str, Enum):
    QUOTE = "quote"          # LTP / OHLCV update
    TRADE = "trade"          # individual trade tick
    DEPTH = "depth"          # market depth (not modeled in Day 3)
    CANDLE = "candle"        # completed/aggregated bar


@dataclass
class InternalMarketEvent:
    event_type: EventType
    symbol: str              # InternalSymbol.key, e.g. "NSE:SBIN"
    exchange: str
    provider_symbol: str     # FYERS symbol, e.g. "NSE:SBIN-EQ"
    timestamp: datetime      # tz-aware UTC
    ltp: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    # For CANDLE events, whether the bar has closed (vs provisional).
    is_closed: bool = False
    raw: Optional[dict] = None
    fetched_at: Optional[datetime] = None
    source_sequence: Optional[str] = None

    def as_ohlcv_row(self) -> dict:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "open": self.open if self.open is not None else self.ltp,
            "high": self.high if self.high is not None else self.ltp,
            "low": self.low if self.low is not None else self.ltp,
            "close": self.close if self.close is not None else self.ltp,
            "volume": self.volume or 0.0,
        }
