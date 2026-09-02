"""Upstox v2 market-data adapter (provider-specific).

Implements the generic ``MarketDataProvider`` interface for Indian markets via
Upstox API v2. All Upstox-specific auth, symbol formats, REST params, and
WebSocket framing live here; downstream code only sees the normalized OHLCV
DataFrame / InternalMarketEvent.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import pandas as pd
import requests

from ..data.base import MarketDataProvider
from .instruments import Instrument, InstrumentRegistry, InstrumentType, InternalSymbol
from .symbol_map import to_upstox_symbol
from .events import InternalMarketEvent, EventType
from ..config import settings, log


_BASE = "https://api.upstox.com/v2"
_WS_URL = "wss://ws-api.upstox.com/v2/feed"

_INTERVAL_MAP = {
    "1m": "1minute", "2m": "1minute", "3m": "1minute", "5m": "5minute",
    "10m": "5minute", "15m": "15minute", "20m": "15minute", "30m": "30minute",
    "45m": "30minute", "60m": "1hour", "1h": "1hour", "2h": "1hour",
    "3h": "1hour", "4h": "1hour", "1d": "day", "1w": "week", "1M": "month",
}


class UpstoxError(Exception):
    """Base class for all Upstox provider errors."""


class UpstoxAuthError(UpstoxError):
    """Authentication failed (bad/expired token, missing credentials)."""


class UpstoxAPIError(UpstoxError):
    """Upstox returned an explicit API-level error."""


class UpstoxRateLimitError(UpstoxError):
    """Upstox signaled HTTP 429 (rate limited)."""


class UpstoxNetworkError(UpstoxError):
    """Transport-level failure (DNS, timeout, connection reset, exhausted retries)."""


class UpstoxMarketDataProvider(MarketDataProvider):
    name = "upstox"

    def __init__(
        self,
        client_id: Optional[str] = None,
        access_token: Optional[str] = None,
        registry: Optional[InstrumentRegistry] = None,
        timeout: int = 20,
        max_retries: int = 3,
    ) -> None:
        self.client_id = client_id or os.getenv("UPSTOX_CLIENT_ID", "")
        self.access_token = access_token or os.getenv("UPSTOX_ACCESS_TOKEN", "")
        self.timeout = timeout
        self.max_retries = max_retries
        self.registry = registry or InstrumentRegistry()
        self._ws: Optional[object] = None
        # V3 resolver is lazily initialised by the V3 WebSocket owner (the
        # backend runtime) so legacy REST callers don't pay the lookup cost.

    @property
    def is_real_time(self) -> bool:
        return True

    @property
    def has_historical(self) -> bool:
        return True

    @property
    def is_authenticated(self) -> bool:
        return bool(self.client_id) and bool(self.access_token)

    def _resolve(self, internal_symbol: str) -> Instrument:
        return self.registry.resolve(internal_symbol)

    def _upstox_symbol(self, internal_symbol: str) -> str:
        instr = self._resolve(internal_symbol)
        if instr.provider_symbol:
            return instr.provider_symbol
        return to_upstox_symbol(instr)

    def _registry_lookup_internal(self, upstox_symbol: str) -> str:
        """Reverse-map an Upstox wire symbol to an internal key (best effort)."""
        for instr in self.registry._by_key.values():
            if instr.provider_symbol == upstox_symbol:
                return instr.key
        try:
            from .symbol_map import from_upstox_symbol
            return from_upstox_symbol(upstox_symbol).key
        except Exception:
            return upstox_symbol

    def get_historical(
        self,
        symbol: str,
        timeframe: str,
        limit: int | None = None,
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
    ) -> pd.DataFrame:
        if timeframe not in _INTERVAL_MAP:
            raise ValueError(f"Unsupported timeframe for Upstox: {timeframe}")
        up_sym = self._upstox_symbol(symbol)

        end_ts = end if end is not None else pd.Timestamp.now(tz="UTC")
        end_ts = pd.Timestamp(end_ts).tz_convert("UTC") if getattr(end_ts, "tzinfo", None) else pd.Timestamp(end_ts, tz="UTC")
        if start is not None:
            start_ts = pd.Timestamp(start).tz_convert("UTC") if getattr(start, "tzinfo", None) else pd.Timestamp(start, tz="UTC")
        elif limit is not None:
            days = min(limit, 365)
            start_ts = end_ts - pd.Timedelta(days=days)
        else:
            start_ts = end_ts - pd.Timedelta(days=100)

        interval = _INTERVAL_MAP[timeframe]
        path = f"/historical-candle/{up_sym}/{interval}/{end_ts.strftime('%Y-%m-%d')}/{start_ts.strftime('%Y-%m-%d')}"
        raw = self._get(path)
        candles = raw.get("data", {}).get("candles", []) if isinstance(raw, dict) else []
        if not candles:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.DataFrame(
            candles, columns=["epoch", "open", "high", "low", "close", "volume", "oi"]
        )
        df["epoch"] = pd.to_datetime(df["epoch"], unit="s", utc=True)
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        df = df.set_index("epoch")
        df.index.name = "timestamp"
        return df[["open", "high", "low", "close", "volume"]]

    def get_latest_price(self, symbol: str) -> float:
        up_sym = self._upstox_symbol(symbol)
        data = self._get(f"/market-quote/quotes?symbol={up_sym}")
        try:
            return float(data["data"][up_sym]["last_price"])
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"Upstox quote parse failed: {e}")

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{_BASE}{path}"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        last_err: Optional[Exception] = None

        for attempt in range(self.max_retries):
            try:
                resp = requests.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self.timeout,
                )
                if resp.status_code == 429:
                    time.sleep(min(2 ** attempt, 30))
                    continue
                if resp.status_code >= 400:
                    payload = self._safe_parse(resp)
                    msg = "unknown error"
                    if isinstance(payload, dict):
                        errs = payload.get("errors", [])
                        if errs:
                            msg = errs[0].get("message", msg)
                    if resp.status_code in (401, 403):
                        raise UpstoxAuthError(
                            f"Upstox authentication failed (HTTP {resp.status_code}): {msg}"
                        )
                    raise UpstoxAPIError(
                        f"Upstox API error (HTTP {resp.status_code}): {msg}"
                    )
                payload = self._safe_parse(resp)
                if isinstance(payload, dict) and payload.get("status") == "error":
                    errs = payload.get("errors", [])
                    code = errs[0].get("code") if errs else None
                    msg = errs[0].get("message", "unknown error") if errs else "unknown error"
                    if code in (-16, -17) or resp.status_code in (401, 403):
                        raise UpstoxAuthError(
                            f"Upstox authentication failed (code={code}): {msg}"
                        )
                    raise UpstoxAPIError(
                        f"Upstox API error (code={code}): {msg}"
                    )
                return payload if isinstance(payload, dict) else {"raw": payload}
            except (UpstoxAuthError, UpstoxAPIError, UpstoxRateLimitError):
                raise
            except (requests.RequestException, ValueError) as e:
                last_err = e
                time.sleep(min(1.5 * (attempt + 1), 30))
        raise UpstoxNetworkError(
            f"Upstox request failed after {self.max_retries} retries: {last_err}"
        )

    @staticmethod
    def _safe_parse(resp) -> object:
        try:
            return resp.json()
        except (ValueError, AttributeError):
            try:
                return resp.text
            except Exception:
                return None

    def connect_live(
        self,
        symbols: list[str],
        on_event: Callable,
        timeframe: str = "1m",
        lite_mode: bool = False,
        max_retries: int = 6,
    ) -> "UpstoxDataSocket":
        if not self.is_authenticated:
            raise RuntimeError(
                "Upstox live data requires UPSTOX_CLIENT_ID and UPSTOX_ACCESS_TOKEN. "
                "Set them in the environment."
            )
        return UpstoxDataSocket(
            client_id=self.client_id,
            access_token=self.access_token,
            provider=self,
            symbols=symbols,
            on_event=on_event,
            timeframe=timeframe,
            max_retries=max_retries,
        )


class UpstoxDataSocket:
    """Live Upstox data socket.

    Uses ``websocket-client`` to maintain a JSON-based WebSocket connection to
    Upstox's market-data feed.
    """

    def __init__(
        self,
        client_id: str,
        access_token: str,
        provider: UpstoxMarketDataProvider,
        symbols: list[str],
        on_event: Callable,
        timeframe: str = "1m",
        max_retries: int = 6,
        auto_connect: bool = True,
    ) -> None:
        try:
            import websocket  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "websocket-client is required for Upstox live data: pip install websocket-client"
            ) from e

        if not client_id or not access_token:
            raise RuntimeError(
                "Upstox live data requires UPSTOX_CLIENT_ID and UPSTOX_ACCESS_TOKEN."
            )

        self.client_id = client_id
        self.access_token = access_token
        self.provider = provider
        self.internal_symbols = list(symbols)
        self.upstox_symbols = [provider._upstox_symbol(s) for s in symbols]
        self.on_event = on_event
        self.timeframe = timeframe
        self.max_retries = max_retries
        self._closed = False
        self._ws = None
        self._on_connect_cb = None
        self._on_disconnect_cb = None
        self._on_auth_error_cb = None
        self._on_invalid_cb = None
        self._up_to_internal = dict(zip(self.upstox_symbols, self.internal_symbols))

        if auto_connect:
            self.connect()

    def connect(self) -> None:
        if self._closed:
            return
        import websocket

        auth = {"type": "auth", "access_token": f"{self.client_id}:{self.access_token}"}
        subscribe = {"type": "subscribe", "symbols": self.upstox_symbols}

        def on_open(ws):
            log.info("Upstox WS connected")
            ws.send(json.dumps(auth))
            ws.send(json.dumps(subscribe))
            if self._on_connect_cb:
                self._on_connect_cb()

        def on_error(ws, error):
            log.error("Upstox WS error: %s", error)
            if self._on_invalid_cb:
                self._on_invalid_cb()

        def on_close(ws, status, message):
            log.info("Upstox WS closed: %s %s", status, message)
            if self._closed:
                return
            if self._on_disconnect_cb:
                self._on_disconnect_cb()

        def on_message(ws, message):
            try:
                data = json.loads(message)
            except (json.JSONDecodeError, TypeError):
                if self._on_invalid_cb:
                    self._on_invalid_cb()
                return
            event = self._normalize(data)
            if event is not None and self.on_event is not None:
                self.on_event(event)

        self._ws = websocket.WebSocketApp(
            _WS_URL,
            on_open=on_open,
            on_error=on_error,
            on_close=on_close,
            on_message=on_message,
        )
        self._ws.run_async()

    def _normalize(self, data: dict) -> Optional[InternalMarketEvent]:
        if not isinstance(data, dict):
            return None
        up_sym = data.get("symbol") or data.get("tradingsymbol")
        if not up_sym:
            return None
        internal = self._up_to_internal.get(up_sym)
        if internal is None:
            try:
                internal = self.provider._registry_lookup_internal(up_sym)
            except Exception:
                internal = up_sym
        exchange = internal.split(":", 1)[0] if ":" in internal else (
            up_sym.split(":", 1)[0] if ":" in up_sym else ""
        )
        ltp = _to_float(data.get("last_price"))
        if ltp is None:
            return None
        ts = datetime.now(timezone.utc)
        return InternalMarketEvent(
            event_type=EventType.QUOTE,
            symbol=internal,
            exchange=exchange,
            provider_symbol=up_sym,
            timestamp=ts,
            ltp=ltp,
            open=_to_float(data.get("open_price")),
            high=_to_float(data.get("high_price")),
            low=_to_float(data.get("low_price")),
            close=ltp,
            volume=_to_float(data.get("volume")),
            raw=data,
        )

    def on_connect_cb(self, cb) -> None:
        self._on_connect_cb = cb

    def on_disconnect_cb(self, cb) -> None:
        self._on_disconnect_cb = cb

    def on_auth_error_cb(self, cb) -> None:
        self._on_auth_error_cb = cb

    def on_invalid_cb(self, cb) -> None:
        self._on_invalid_cb = cb

    def close(self) -> None:
        self._closed = True
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception as e:
            log.debug("Upstox WS close error: %s", e)


def _to_float(x) -> Optional[float]:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if f != f:
        return None
    return f
