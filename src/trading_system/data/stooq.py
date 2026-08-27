"""Stooq provider (FALLBACK, not the Day-1 default).

Documented findings (verified Day 1):
  * Endpoint (documented): https://stooq.com/q/d/l/?s=SYMBOL&i=d
  * Reality: this endpoint frequently returns HTTP 404 / 'Invalid Symbol'
    for programmatic access from some networks / user-agents, and is rate
    limited and sometimes delayed. It works well for manual CSV download but
    is unreliable as an automated feed.
  * Coverage: global equities, indices, FX, commodities. Free, no auth.
  * Licensing: Stooq data is provided 'as is'; redistribution limits apply.

Because it 404'd during Day 1 verification, it is implemented but NOT selected
as the default provider. Kept as a documented alternative and to prove the
provider abstraction works. It will surface a clear error rather than fake data.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd
import requests

from .base import MarketDataProvider

_BASE = "https://stooq.com/q/d/l/"


class StooqProvider(MarketDataProvider):
    name = "stooq"

    def __init__(self, timeout: int = 20) -> None:
        self.timeout = timeout

    @property
    def is_real_time(self) -> bool:
        return False  # Stooq free CSV is delayed / EOD for many symbols.

    def get_historical(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
    ) -> pd.DataFrame:
        if timeframe not in ("1d", "1w", "1M"):
            raise ValueError("Stooq daily endpoint supports 1d/1w/1M only")
        params = {"s": symbol.lower(), "i": "d", "d1": "19000101", "d2": "20300101"}
        resp = requests.get(_BASE, params=params, timeout=self.timeout)
        if resp.status_code != 200 or "Date" not in resp.text:
            raise RuntimeError(
                f"Stooq returned status {resp.status_code}; "
                "endpoint unreliable for automated access (verified Day 1)."
            )
        from io import StringIO

        df = pd.read_csv(StringIO(resp.text))
        df["Date"] = pd.to_datetime(df["Date"], utc=True)
        df = df.set_index("Date").rename(
            columns={"Open": "open", "High": "high", "Low": "low",
                     "Close": "close", "Volume": "volume"}
        )
        df = df[["open", "high", "low", "close", "volume"]].tail(limit)
        df.index.name = "timestamp"
        return df

    def get_latest_price(self, symbol: str) -> float:
        df = self.get_historical(symbol, "1d", 1)
        return float(df["close"].iloc[-1])
