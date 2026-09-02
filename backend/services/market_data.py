"""Market data service for fetching live data from Upstox."""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from config import get_settings

logger = logging.getLogger(__name__)

UPSTOX_BASE = "https://api.upstox.com/v2"

ISIN_MAP = {
    "NSE:SBIN": "INE062A01020",
    "NSE:RELIANCE": "INE002A01018",
    "NSE:INFY": "INE009A01021",
}

INDEX_NAME_MAP = {
    "NIFTY50": "Nifty 50",
    "BANKNIFTY": "Nifty Bank",
    "FINNIFTY": "Nifty Fin Service",
}

INTERVAL_MAP = {
    "1m": "1minute",
    "30m": "30minute",
    "1d": "day",
    "1D": "day",
    "1w": "week",
    "1M": "month",
}


def to_upstox_symbol(symbol: str) -> str:
    """
    Convert internal symbol (NSE:SBIN) to Upstox V2 format (NSE_EQ|INE062A01020).
    """
    if "EQ" in symbol:
        return symbol
    parts = symbol.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid symbol format: {symbol}")
    exchange, sym = parts
    ex = exchange.upper()
    key = f"{ex}:{sym}"

    if sym in INDEX_NAME_MAP:
        return f"{ex}_INDEX|{INDEX_NAME_MAP[sym]}"

    isin = ISIN_MAP.get(key)
    if isin:
        return f"{ex}_EQ|{isin}"

    return f"{ex}_EQ|{sym}"


async def fetch_ohlcv(
    symbol: str,
    timeframe: str,
    access_token: str,
    bars: int = 160,
) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV candles from Upstox.

    Returns a DataFrame with columns: open, high, low, close, volume
    with a timezone-aware UTC DatetimeIndex.
    """
    try:
        import requests

        upstox_symbol = to_upstox_symbol(symbol)
        interval = INTERVAL_MAP.get(timeframe, "day")

        url = f"{UPSTOX_BASE}/historical-candle/intraday/{upstox_symbol}/{interval}"
        params = {"limit": bars}

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        if data.get("status") != "success" or "data" not in data:
            logger.error("Upstox API error: %s", data.get("error_message", "Unknown error"))
            return None

        candles = data["data"]["candles"]
        if not candles:
            logger.warning("No candles returned for %s %s", symbol, timeframe)
            return None

        # Parse candles: [timestamp, open, high, low, close, volume, oi]
        df = pd.DataFrame(
            candles,
            columns=["timestamp", "open", "high", "low", "close", "volume", "oi"],
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp")
        df = df[["open", "high", "low", "close", "volume"]].astype(float)

        return df
    except Exception as e:
        logger.error("Failed to fetch OHLCV for %s %s: %s", symbol, timeframe, str(e))
        return None


async def fetch_latest_price(symbol: str, access_token: str) -> Optional[float]:
    """Fetch the latest price for a symbol."""
    try:
        import requests

        upstox_symbol = to_upstox_symbol(symbol)
        url = f"{UPSTOX_BASE}/market-quote/quotes"
        params = {"symbol": upstox_symbol}
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        if data.get("status") == "success" and "data" in data:
            quote = data["data"].get(upstox_symbol, {})
            return quote.get("last_price")
        return None
    except Exception as e:
        logger.error("Failed to fetch latest price for %s: %s", symbol, str(e))
        return None
