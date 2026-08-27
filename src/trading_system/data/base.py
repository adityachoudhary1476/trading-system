"""Abstract market-data provider interface.

Keeping this abstract means the rest of the system never depends on a specific
exchange or API. Swapping Binance for Stooq (or a paid feed) is a one-line
factory change and nothing downstream needs to know.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

import pandas as pd

from .types import OHLCV


class MarketDataProvider(ABC):
    """Generic interface every data source must satisfy."""

    name: str = "abstract"

    @abstractmethod
    def get_historical(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
    ) -> pd.DataFrame:
        """Return historical OHLCV as a DataFrame.

        Columns: open, high, low, close, volume.
        Index: a tz-aware (UTC) DatetimeIndex.
        May also carry an extra 'raw' attribute but core columns are required.
        """
        ...

    @abstractmethod
    def get_latest_price(self, symbol: str) -> float:
        """Return the latest traded price for a symbol."""
        ...

    @property
    def is_real_time(self) -> bool:
        """Subclasses should declare whether data is real-time or delayed."""
        return False

    @property
    def has_historical(self) -> bool:
        return True
