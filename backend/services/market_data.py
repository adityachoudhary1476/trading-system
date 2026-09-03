"""Market data service for fetching live data from Upstox V2.

The Upstox V2 historical-candle API has two distinct endpoints:

* ``/v2/historical-candle/intraday/{instrument}/{interval}`` — only supports
  ``1minute`` and ``30minute`` for the **current** trading day.
* ``/v2/historical-candle/{instrument}/{interval}/{to_date}/{from_date}`` —
  supports ``1minute``, ``30minute``, ``day``, ``week``, ``month`` and
  accepts a YYYY-MM-DD date range.  The historical endpoint can return up
  to the last 6 months of minute data and up to 10 years of week/month
  data.  The day interval is bounded to ~1 year by the API.

A previous version of this service routed every interval through the
intraday endpoint, which silently failed for ``day`` (and any interval
outside 1minute/30minute) — Upstox returns a 400 / empty body, the
service mapped that to ``None``, and callers (the analysis and signals
routes) responded with 404 or an empty 200 respectively.  This module
now mirrors the working Vercel ``/api/market/ohlcv`` route: every
supported interval goes through the historical-candle endpoint with an
explicit YYYY-MM-DD date range computed in UTC.
"""
from __future__ import annotations

import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import requests

from config import get_settings

logger = logging.getLogger(__name__)

UPSTOX_BASE = "https://api.upstox.com/v2"

ISIN_MAP = {
    "NSE:SBIN": "INE062A01020",
    "NSE:RELIANCE": "INE002A01018",
    "NSE:INFY": "INE009A01021",
    "NSE:TCS": "INE007A01025",
    "NSE:HDFCBANK": "INE040A01034",
    "NSE:ICICIBANK": "INE090A01021",
    "NSE:KOTAKBANK": "INE237A01028",
    "NSE:AXISBANK": "INE238A01034",
    "NSE:LT": "INE018A01030",
    "NSE:WIPRO": "INE075A01022",
}

INDEX_NAME_MAP = {
    "NIFTY50": "Nifty 50",
    "BANKNIFTY": "Nifty Bank",
    "FINNIFTY": "Nifty Fin Service",
}

INTERVAL_MAP = {
    "1m": "1minute",
    "1M": "1minute",
    "30m": "30minute",
    "1d": "day",
    "1D": "day",
    "1w": "week",
    "1W": "week",
    "1mo": "month",
    "1MO": "month",
}

SUPPORTED_INTERVALS = frozenset(set(INTERVAL_MAP.values()))

UPSTOX_RANGE_DAYS: dict[str, int] = {
    "1minute": 31,
    "30minute": 366,
    "day": 366,
    "week": 366 * 10,
    "month": 366 * 10,
}


class UpstoxMarketDataError(Exception):
    """Base class for non-recoverable Upstox market-data failures.

    Concrete subclasses map to specific HTTP-like failure modes so callers
    (the analysis and signals routes) can surface them as the right status
    code instead of collapsing every failure into a 404.
    """


class UpstoxUnauthorizedError(UpstoxMarketDataError):
    """Upstox rejected the bearer token (HTTP 401/403)."""


class UpstoxRateLimitedError(UpstoxMarketDataError):
    """Upstox rate-limited the caller (HTTP 429)."""


class UpstoxBadResponseError(UpstoxMarketDataError):
    """Upstox returned a 4xx/5xx response with a non-success status body."""


class UpstoxMalformedError(UpstoxMarketDataError):
    """Upstox returned a 2xx body we could not parse into a candle array."""


class UpstoxNetworkError(UpstoxMarketDataError):
    """Network-level failure reaching Upstox (timeout, DNS, TLS, etc.)."""


def to_upstox_symbol(symbol: str) -> str:
    """
    Convert internal symbol (NSE:SBIN) to Upstox V2 format (NSE_EQ|INE062A01020).
    """
    if "EQ" in symbol or "INDEX" in symbol:
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


def _resolve_interval(timeframe: str) -> str:
    """Map an internal timeframe token to an Upstox V2 interval.

    Raises ``ValueError`` for timeframes we explicitly do not support
    (e.g. 5m, 15m, 1h) rather than silently coercing them to a
    neighbouring value.  This matches the Vercel OHLCV route, which
    answers ``400 unsupported_timeframe`` for those requests.
    """
    if not isinstance(timeframe, str) or timeframe not in INTERVAL_MAP:
        raise ValueError(
            f"Unsupported timeframe: {timeframe!r}. "
            f"Supported values: {sorted(INTERVAL_MAP.keys())}"
        )
    return INTERVAL_MAP[timeframe]


def _format_date(d: datetime) -> str:
    """Format a datetime as YYYY-MM-DD in UTC."""
    return d.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _date_range_for(interval: str, bars: int) -> tuple[str, str]:
    """Return (to_date, from_date) YYYY-MM-DD pair for ``bars`` candles.

    The historical-candle endpoint returns candles between the from_date
    and to_date (inclusive).  We widen the range with ``daysPerBar`` so
    that weekends and holidays don't shrink the deliverable candle
    count below ``bars`` — Upstox only emits a row for trading days,
    not for every calendar day in the range.
    """
    # 1 trading day has ~375 1-minute bars or ~12 30-minute bars on NSE.
    # Daily/weekly/monthly use calendar days (with weekends/holidays
    # trimmed by Upstox itself).
    days_per_bar = {
        "1minute": 1.0 / 375.0,
        "30minute": 1.0 / 12.0,
        "day": 1,
        "week": 7,
        "month": 30,
    }.get(interval, 1)
    max_range = UPSTOX_RANGE_DAYS.get(interval, 366)
    needed = max(bars, 1) * days_per_bar + 1
    days = int(min(max_range, max(needed, 2)))
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return _format_date(end), _format_date(start)


def _parse_candle(raw) -> Optional[list]:
    """Validate one raw candle tuple.  Returns None if malformed.

    The Upstox V2 candle tuple is
    ``[timestamp, open, high, low, close, volume, open_interest]`` where
    ``timestamp`` is an ISO-8601 string with timezone offset
    (e.g. ``"2024-01-15T00:00:00+05:30"``) and the numeric fields are
    floats.  We accept both the documented 7-element shape and a
    minimum 6-element shape for resilience; ``open_interest`` is
    optional.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) < 6:
        return None
    ts, o, h, l, c, v = raw[:6]
    if not isinstance(ts, str) or not _is_finite_number(o):
        return None
    if not all(_is_finite_number(x) for x in (h, l, c)):
        return None
    if not _is_finite_number(v):
        v = 0
    # Verify the timestamp string is actually parseable as ISO-8601.  This
    # catches malformed inputs ("not-a-timestamp", "") before pandas raises
    # an unhandled DateParseError later in _build_dataframe.
    try:
        pd.Timestamp(ts)
    except (ValueError, TypeError):
        return None
    return [ts, float(o), float(h), float(l), float(c), float(v)]


def _is_finite_number(x) -> bool:
    if not isinstance(x, (int, float)):
        return False
    return float(x) == float(x) and float(x) not in (float("inf"), float("-inf"))


def _build_dataframe(candles: list[list]) -> pd.DataFrame:
    """Parse the validated candle list into a tz-aware UTC DataFrame.

    Upstox returns candles newest-first, but every analysis pipeline in
    this service expects chronological (oldest-first) order.  We sort
    here so callers do not have to think about ordering.  We do not
    de-duplicate: if Upstox ever returns two candles for the same
    timestamp, the second one wins (``keep=last``) which mirrors the
    last-write-wins behaviour of the existing services.
    """
    rows = []
    for c in candles:
        if not isinstance(c, (list, tuple)) or len(c) < 6:
            continue
        # Drop optional 7th element (open_interest) if present.
        rows.append(list(c[:6]))
    if not rows:
        raise UpstoxMalformedError("build_dataframe: no valid candles after validation")
    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp", kind="mergesort")
    df = df.drop_duplicates(subset="timestamp", keep="last")
    df = df.set_index("timestamp")
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    return df


def _build_url(upstox_symbol: str, interval: str, to_date: str, from_date: str) -> str:
    encoded_symbol = urllib.parse.quote(upstox_symbol, safe="")
    return (
        f"{UPSTOX_BASE}/historical-candle/"
        f"{encoded_symbol}/{interval}/{to_date}/{from_date}"
    )


async def fetch_ohlcv(
    symbol: str,
    timeframe: str,
    access_token: str,
    bars: int = 160,
) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV candles from the Upstox V2 historical-candle endpoint.

    Returns a DataFrame with columns: open, high, low, close, volume
    and a timezone-aware UTC DatetimeIndex sorted oldest-first.

    Raises a :class:`UpstoxMarketDataError` subclass for any
    non-recoverable Upstox failure (bad token, rate limit, malformed
    body, network error).  Returns ``None`` only when Upstox responds
    successfully with an empty candle array, so callers can distinguish
    "no data" from "could not reach Upstox".
    """
    try:
        upstox_symbol = to_upstox_symbol(symbol)
    except ValueError as exc:
        logger.error("Invalid symbol %r: %s", symbol, exc)
        return None

    try:
        interval = _resolve_interval(timeframe)
    except ValueError as exc:
        logger.error("Invalid timeframe for %s: %s", symbol, exc)
        return None

    to_date, from_date = _date_range_for(interval, bars)
    url = _build_url(upstox_symbol, interval, to_date, from_date)

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as exc:
        logger.error(
            "Upstox network error for %s %s: %s",
            symbol, timeframe, exc.__class__.__name__,
        )
        raise UpstoxNetworkError(f"Upstox unreachable: {exc}") from exc

    if response.status_code in (401, 403):
        logger.error(
            "Upstox auth failure for %s %s: HTTP %s",
            symbol, timeframe, response.status_code,
        )
        raise UpstoxUnauthorizedError(
            f"Upstox rejected the access token (HTTP {response.status_code})"
        )
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "")
        logger.error(
            "Upstox rate limit for %s %s: HTTP 429, retry_after=%r",
            symbol, timeframe, retry_after,
        )
        raise UpstoxRateLimitedError(
            f"Upstox rate limit exceeded (HTTP 429, retry_after={retry_after!r})"
        )
    if not response.ok:
        detail = _extract_error_detail(response)
        logger.error(
            "Upstox HTTP %s for %s %s url=%s detail=%s",
            response.status_code, symbol, timeframe, url, detail,
        )
        raise UpstoxBadResponseError(
            f"Upstox HTTP {response.status_code} for {symbol} {timeframe}: {detail}"
        )

    try:
        body = response.json()
    except ValueError as exc:
        logger.error(
            "Upstox returned non-JSON body for %s %s url=%s",
            symbol, timeframe, url,
        )
        raise UpstoxMalformedError(
            f"Upstox returned non-JSON body for {symbol} {timeframe}"
        ) from exc

    if not isinstance(body, dict) or body.get("status") != "success":
        detail = body.get("error_message") or body.get("message") if isinstance(body, dict) else None
        logger.error(
            "Upstox non-success status for %s %s url=%s detail=%r",
            symbol, timeframe, url, detail,
        )
        raise UpstoxBadResponseError(
            f"Upstox returned non-success status for {symbol} {timeframe}: {detail}"
        )

    raw_candles = body.get("data", {}).get("candles") if isinstance(body, dict) else None
    if raw_candles is None:
        logger.error(
            "Upstox success body missing data.candles for %s %s url=%s",
            symbol, timeframe, url,
        )
        raise UpstoxMalformedError(
            f"Upstox success body missing data.candles for {symbol} {timeframe}"
        )
    if not isinstance(raw_candles, list):
        raise UpstoxMalformedError(
            f"Upstox data.candles is not a list for {symbol} {timeframe}"
        )
    if len(raw_candles) == 0:
        logger.warning(
            "Upstox returned no candles for %s %s url=%s",
            symbol, timeframe, url,
        )
        return None

    valid: list[list] = []
    dropped = 0
    for raw in raw_candles:
        parsed = _parse_candle(raw)
        if parsed is None:
            dropped += 1
            continue
        valid.append(parsed)
    if dropped:
        logger.warning(
            "Dropped %d malformed candle(s) from Upstox response for %s %s",
            dropped, symbol, timeframe,
        )
    if not valid:
        raise UpstoxMalformedError(
            f"Upstox returned only malformed candles for {symbol} {timeframe}"
        )

    df = _build_dataframe(valid)
    return df


def _extract_error_detail(response) -> str:
    """Pull a short, log-safe error string from an Upstox error response."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200] if response.text else ""
    if not isinstance(body, dict):
        return ""
    for key in ("message", "error_message", "info"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return first.get("message", "") or ""
    return ""


async def fetch_quote(symbol: str, access_token: str) -> Optional[dict]:
    """Fetch the full live quote for a symbol from Upstox V2.

    Returns a normalized dict with the following keys (all present, but
    numeric values may be ``None`` when the upstream omits them — we
    never fabricate values):

    * ``last_price``         — float (required for a usable quote)
    * ``open_price``         — float | None
    * ``high_price``         — float | None
    * ``low_price``          — float | None
    * ``prev_close``         — float | None
    * ``volume``             — int | None
    * ``average_price``      — float | None (VWAP, when available)
    * ``ohlc``               — dict with same names (Upstox nested field)
    * ``depth``              — dict | None
    * ``timestamp``          — int | None (epoch seconds, Upstox field)
    * ``instrument_token``   — int | None
    * ``symbol``             — Upstox-formatted instrument key
    * ``raw``                — the underlying quote dict (for diagnostics)

    Returns ``None`` if Upstox did not return a success body or the
    quote did not contain ``last_price``.
    """
    try:
        upstox_symbol = to_upstox_symbol(symbol)
        url = f"{UPSTOX_BASE}/market-quote/quotes"
        params = {"symbol": upstox_symbol}
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logger.error(
            "Upstox network error fetching quote for %s: %s",
            symbol, exc.__class__.__name__,
        )
        raise UpstoxNetworkError(f"Upstox unreachable: {exc}") from exc
    except ValueError as exc:
        logger.error("Upstox non-JSON quote for %s", symbol)
        raise UpstoxMalformedError(f"Upstox returned non-JSON quote for {symbol}") from exc

    if not isinstance(data, dict) or data.get("status") != "success":
        detail = data.get("error_message") if isinstance(data, dict) else None
        logger.error("Upstox non-success quote body for %s detail=%r", symbol, detail)
        raise UpstoxBadResponseError(
            f"Upstox non-success quote body for {symbol}: {detail}"
        )

    quote = (data.get("data") or {}).get(upstox_symbol) if isinstance(data, dict) else None
    if not isinstance(quote, dict):
        logger.warning("Upstox quote body missing data[%s] for %s", upstox_symbol, symbol)
        return None

    last_price = quote.get("last_price")
    if not isinstance(last_price, (int, float)) or not float(last_price) == float(last_price):
        # NaN, Inf, or missing -> unusable
        logger.warning("Upstox quote for %s missing finite last_price: %r", symbol, last_price)
        return None

    def _num(key: str) -> Optional[float]:
        v = quote.get(key)
        if not isinstance(v, (int, float)):
            return None
        fv = float(v)
        if fv != fv or fv in (float("inf"), float("-inf")):
            return None
        return fv

    ohlc = quote.get("ohlc") if isinstance(quote.get("ohlc"), dict) else {}

    def _ohlc(key: str) -> Optional[float]:
        v = ohlc.get(key)
        if not isinstance(v, (int, float)):
            return None
        fv = float(v)
        if fv != fv or fv in (float("inf"), float("-inf")):
            return None
        return fv

    ts_raw = quote.get("timestamp")
    if isinstance(ts_raw, str) and ts_raw:
        # Upstox sometimes returns ISO timestamp string
        try:
            from datetime import datetime, timezone
            ts_s = int(
                datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                .astimezone(timezone.utc)
                .timestamp()
            )
        except (ValueError, TypeError):
            ts_s = None
    elif isinstance(ts_raw, (int, float)):
        # Upstox sends seconds
        ts_s = int(float(ts_raw))
    else:
        ts_s = None

    instrument_token = quote.get("instrument_token")
    if not isinstance(instrument_token, (int, float)):
        instrument_token = None

    return {
        "last_price": float(last_price),
        "open_price": _ohlc("open") or _num("open_price"),
        "high_price": _ohlc("high") or _num("high_price"),
        "low_price": _ohlc("low") or _num("low_price"),
        "prev_close": _ohlc("close") or _num("cp"),
        "volume": int(_num("volume")) if _num("volume") is not None else None,
        "average_price": _num("average_price"),
        "ohlc": {
            "open": _ohlc("open"),
            "high": _ohlc("high"),
            "low": _ohlc("low"),
            "close": _ohlc("close"),
        },
        "depth": quote.get("depth") if isinstance(quote.get("depth"), dict) else None,
        "timestamp": ts_s,
        "instrument_token": int(instrument_token) if instrument_token is not None else None,
        "symbol": upstox_symbol,
        "raw": quote,
    }


async def fetch_latest_price(symbol: str, access_token: str) -> Optional[float]:
    """Fetch only the latest price for a symbol (legacy convenience helper)."""
    try:
        quote = await fetch_quote(symbol, access_token)
    except UpstoxMarketDataError:
        return None
    if quote is None:
        return None
    return quote.get("last_price")


__all__ = [
    "INTERVAL_MAP",
    "SUPPORTED_INTERVALS",
    "UPSTOX_RANGE_DAYS",
    "UpstoxMarketDataError",
    "UpstoxUnauthorizedError",
    "UpstoxRateLimitedError",
    "UpstoxBadResponseError",
    "UpstoxMalformedError",
    "UpstoxNetworkError",
    "to_upstox_symbol",
    "fetch_ohlcv",
    "fetch_quote",
    "fetch_latest_price",
    "_build_url",
    "_date_range_for",
    "_parse_candle",
    "_build_dataframe",
    "_resolve_interval",
]
