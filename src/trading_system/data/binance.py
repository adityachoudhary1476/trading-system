"""Binance public REST provider (no API key required for market data).

DATA-SOURCE FACTS (verified Day 1):
  * Endpoint: https://api.binance.com/api/v3/klines  (public, unauthenticated)
  * Auth: NONE needed for market data / klines.
  * Rate limit: weight-based, 6000 weight/min per IP for public endpoints;
    a single klines call with limit<=1000 costs weight 2. Very generous.
  * Historical coverage: essentially all history since 2017 for active pairs.
  * Granularity: 1m..1M. Real-time: candles close at interval boundaries;
    the most recent candle is the live/open one (effectively real-time for
    market-data research, though we treat the newest bar as provisional).
  * Licensing: Binance market data API terms apply; for a research system this
    is fine. Do NOT redistribute the raw feed commercially without review.
  * Crypto only (BTCUSDT, ETHUSDT, ...). No equities.

This module does NOT place orders and never sends credentials.
"""
from __future__ import annotations

import time
from typing import Optional

import pandas as pd
import requests

from .base import MarketDataProvider

# Binance interval -> REST interval token.
_INTERVALS = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "4h": "4h", "1d": "1d", "1w": "1w", "1M": "1M",
}
_REST_URL = "https://api.binance.com/api/v3/klines"


class BinanceProvider(MarketDataProvider):
    name = "binance"

    def __init__(self, timeout: int = 20, max_retries: int = 3) -> None:
        self.timeout = timeout
        self.max_retries = max_retries

    @property
    def is_real_time(self) -> bool:
        # The newest candle is the live/open bar; we treat the feed as
        # near-real-time for research while flagging the last bar as provisional.
        return True

    def _fetch(self, params: dict) -> list:
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = requests.get(
                    _REST_URL, params=params, timeout=self.timeout
                )
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Binance request failed after retries: {last_err}")

    def get_historical(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
    ) -> pd.DataFrame:
        if timeframe not in _INTERVALS:
            raise ValueError(f"Unsupported timeframe for Binance: {timeframe}")
        params = {
            "symbol": symbol.upper(),
            "interval": _INTERVALS[timeframe],
            "limit": min(int(limit), 1000),
        }
        if start is not None:
            params["startTime"] = int(pd.Timestamp(start).timestamp() * 1000)
        if end is not None:
            params["endTime"] = int(pd.Timestamp(end).timestamp() * 1000)

        raw = self._fetch(params)
        if not raw:
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume"]
            )

        df = pd.DataFrame(
            raw,
            columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "qav", "trades", "tbav", "tqav", "ignore",
            ],
        )
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df = df.set_index("open_time")
        df.index.name = "timestamp"
        return df[["open", "high", "low", "close", "volume"]]

    def get_latest_price(self, symbol: str) -> float:
        url = "https://api.binance.com/api/v3/ticker/price"
        for attempt in range(self.max_retries):
            try:
                resp = requests.get(
                    url, params={"symbol": symbol.upper()}, timeout=self.timeout
                )
                resp.raise_for_status()
                return float(resp.json()["price"])
            except (requests.RequestException, ValueError, KeyError) as e:
                if attempt == self.max_retries - 1:
                    raise RuntimeError(f"Binance price fetch failed: {e}")
                time.sleep(1.0)
        raise RuntimeError("Unreachable")
