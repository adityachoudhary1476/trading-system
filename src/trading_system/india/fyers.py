"""FYERS v3 market-data adapter (provider-specific).

Implements the generic `MarketDataProvider` interface for Indian markets. All
FYERS-specific auth, symbol formats, REST params, and WebSocket framing live here;
downstream code only sees the normalized OHLCV DataFrame / InternalMarketEvent.

Verified from current official FYERS v3 docs / SDK samples (Day 3 research):
  * REST data base : https://api-t1.fyers.in/data
  * History        : GET /history?symbol=NSE:SBIN-EQ&resolution=D&date_format=0
                     &range_from=<epoch>&range_to=<epoch>&cont_flag=1
  * Candle payload : [epoch, open, high, low, close, volume]  (OI appended if oi_flag)
  * Response       : {"s":"ok","candles":[[...]]}
  * Resolutions    : "1","5","15","60","D","1W","1M" (and more)
  * WebSocket      : wss://api.fyers.in/socket/v2/data/  ; auth frame {"T":"c", ...}
                     subscribe {"T":"t","symbols":[...]} ; mode Lite/SymbolUpdate.
  * Auth           : OAuth2 (client_id + secret + redirect -> auth code -> access token).
                     Access token used as "APPID:AccessToken" for the socket.

PRICING / LIMITS: as of Day 3 research we could NOT confirm a published free tier or
data-feed fee from official sources, so it is documented as UNVERIFIED. Historical
caps (per official skill docs): minute resolutions 100 days/request; day/week/month
366 days/request; seconds only last 30 trading days. Orders (not used here) require
an active FYERS account and, per SEBI algo rules, a validated static IP after
Apr-2026 — irrelevant to data-only Day 3.

This adapter does NOT place orders and reads all secrets from the environment.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests

from ..data.base import MarketDataProvider
from .instruments import Instrument, InstrumentRegistry, InstrumentType, InternalSymbol
from .symbol_map import to_fyers_symbol
from .events import InternalMarketEvent, EventType
from ..config import settings, log


# FYERS resolution tokens for the History API.
_RESOLUTION = {
    "1m": "1", "2m": "2", "3m": "3", "5m": "5", "10m": "10", "15m": "15",
    "20m": "20", "30m": "30", "45m": "45", "60m": "60", "1h": "60",
    "2h": "120", "3h": "180", "4h": "240", "1d": "D", "1w": "1W", "1M": "1M",
}
_DATA_BASE = "https://api-t1.fyers.in/data"
_WS_URL = "wss://api.fyers.in/socket/v2/data/"


class FYERSMarketDataProvider(MarketDataProvider):
    name = "fyers"

    def __init__(
        self,
        client_id: Optional[str] = None,
        access_token: Optional[str] = None,
        registry: Optional[InstrumentRegistry] = None,
        timeout: int = 20,
        max_retries: int = 3,
    ) -> None:
        # Secrets come from env, never stored on the object beyond the token needed
        # for transport. Prefer explicit args (already env-loaded) over re-reading.
        self.client_id = client_id or os.getenv("FYERS_CLIENT_ID", "")
        self.access_token = access_token or os.getenv("FYERS_ACCESS_TOKEN", "")
        self.timeout = timeout
        self.max_retries = max_retries
        self.registry = registry or InstrumentRegistry()
        self._ws: Optional[object] = None  # websocket object, set on connect()

    # --- capability flags ---------------------------------------------------
    @property
    def is_real_time(self) -> bool:
        # WebSocket provides near-real-time; the History API explicitly is NOT
        # real-time (per official docs). We treat the provider as real-time-capable.
        return True

    @property
    def has_historical(self) -> bool:
        return True

    @property
    def is_authenticated(self) -> bool:
        return bool(self.client_id) and bool(self.access_token)

    # --- symbol resolution --------------------------------------------------
    def _resolve(self, internal_symbol: str) -> Instrument:
        return self.registry.resolve(internal_symbol)

    def _fyers_symbol(self, internal_symbol: str) -> str:
        instr = self._resolve(internal_symbol)
        if instr.provider_symbol:
            return instr.provider_symbol
        return to_fyers_symbol(instr)

    def _registry_lookup_internal(self, fyers_symbol: str) -> str:
        """Reverse-map a FYERS wire symbol to an internal key (best effort)."""
        for instr in self.registry._by_key.values():
            if instr.provider_symbol == fyers_symbol:
                return instr.key
        try:
            from .symbol_map import from_fyers_symbol

            return from_fyers_symbol(fyers_symbol).key
        except Exception:
            return fyers_symbol

    # --- historical ---------------------------------------------------------
    def get_historical(
        self,
        symbol: str,
        timeframe: str,
        limit: int | None = None,
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
    ) -> pd.DataFrame:
        if timeframe not in _RESOLUTION:
            raise ValueError(f"Unsupported timeframe for FYERS: {timeframe}")
        fy_sym = self._fyers_symbol(symbol)

        # Determine date range. FYERS caps: minute=100d/req, day+=366d/req.
        end_ts = (end or pd.Timestamp.now(tz="UTC")).floor("D")
        if start is not None:
            start_ts = pd.Timestamp(start).tz_convert("UTC") if start.tzinfo else pd.Timestamp(start, tz="UTC")
        elif limit is not None:
            # Approximate window from limit; clamps to official caps below.
            days = min(limit, 366) if timeframe in ("1d", "1w", "1M") else min(limit, 100)
            start_ts = end_ts - pd.Timedelta(days=days)
        else:
            start_ts = end_ts - pd.Timedelta(days=100)

        params = {
            "symbol": fy_sym,
            "resolution": _RESOLUTION[timeframe],
            "date_format": "0",
            "range_from": int(start_ts.timestamp()),
            "range_to": int(end_ts.timestamp()),
            "cont_flag": "1",
        }

        raw = self._get("/history", params)
        candles = raw.get("candles", []) if isinstance(raw, dict) else []
        if not candles:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.DataFrame(
            candles, columns=["epoch", "open", "high", "low", "close", "volume"]
        )
        df["epoch"] = pd.to_datetime(df["epoch"], unit="s", utc=True)
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        df = df.set_index("epoch")
        df.index.name = "timestamp"
        return df[["open", "high", "low", "close", "volume"]]

    def get_latest_price(self, symbol: str) -> float:
        fy_sym = self._fyers_symbol(symbol)
        data = self._get("/quotes", {"symbols": fy_sym})
        # Response shape: {"s":"ok","d":[{"v":{"lp":123.4}, ...}]}
        try:
            return float(data["d"][0]["v"]["lp"])
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"FYERS quote parse failed: {e}")

    # --- REST helper --------------------------------------------------------
    def _get(self, path: str, params: dict) -> dict:
        url = f"{_DATA_BASE}{path}"
        headers = {
            "Authorization": f"{self.client_id}:{self.access_token}",
        }
    
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
                    time.sleep(2 ** attempt)
                    continue
    
                resp.raise_for_status()
                return resp.json()
    
            except (requests.RequestException, ValueError) as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
    
        raise RuntimeError(f"FYERS request failed after retries: {last_err}")

    # --- WebSocket (live) ---------------------------------------------------
    def connect_live(
        self,
        symbols: list[str],
        on_event,
        timeframe: str = "1m",
        lite_mode: bool = False,
        max_retries: int = 6,
    ) -> "FyersDataSocket":
        """Open a live data socket. Returns a controller with .close().

        Requires authentication. Raises a clear error if credentials are missing
        (no fabricated stream). The callback receives InternalMarketEvent objects.
        """
        if not self.is_authenticated:
            raise RuntimeError(
                "FYERS live data requires FYERS_CLIENT_ID and FYERS_ACCESS_TOKEN. "
                "Set them in the environment; no credentials present in Day 3 env."
            )
        return FyersDataSocket(
            client_id=self.client_id,
            access_token=self.access_token,
            provider=self,
            symbols=symbols,
            on_event=on_event,
            timeframe=timeframe,
            lite_mode=lite_mode,
            max_retries=max_retries,
        )


class FyersDataSocket:
    """Live FYERS data socket, backed by the official ``fyers_apiv3`` SDK.

    The real FYERS v3 *data* WebSocket is **binary protobuf**
    (``wss://socket.fyers.in/hsm/v1-5/prod``): auth is a binary HSM token frame,
    subscription is binary, and market messages are protobuf ``MarketFeed`` blobs
    that the SDK decodes into plain dicts (with ``precision``/``multiplier`` already
    applied). Hand-rolling that framing over raw ``websocket-client`` would never
    connect, so we deliberately use the vendor SDK as the transport and keep ALL
    provider-specific behavior inside this file. The SDK also provides its own
    exponential-backoff reconnect, so we do not re-implement a spin loop.

    What this class owns:
      * translating internal symbols -> FYERS symbols for subscription,
      * normalizing the SDK's decoded dict into ``InternalMarketEvent``,
      * forwarding normalized events to the caller's ``on_event`` callback,
      * exposing health hooks (on_connect / on_disconnect / on_error).
    It does NOT call the AI or place orders.
    """

    def __init__(
        self,
        client_id: str,
        access_token: str,
        provider: FYERSMarketDataProvider,
        symbols: list[str],
        on_event,
        timeframe: str = "1m",
        lite_mode: bool = False,
        max_retries: int = 6,
        auto_connect: bool = True,
    ) -> None:
        try:
            from fyers_apiv3.FyersWebsocket import data_ws  # official SDK
        except ImportError as e:  # pragma: no cover - SDK expected installed
            raise RuntimeError(
                "fyers_apiv3 is required for FYERS live data: pip install fyers_apiv3"
            ) from e

        if not client_id or not access_token:
            raise RuntimeError(
                "FYERS live data requires FYERS_CLIENT_ID and FYERS_ACCESS_TOKEN."
            )

        self.client_id = client_id
        self.access_token = access_token
        self.provider = provider
        self.internal_symbols = list(symbols)
        # FYERS wire symbols (e.g. NSE:SBIN-EQ) for subscription.
        self.fyers_symbols = [provider._fyers_symbol(s) for s in symbols]
        self.on_event = on_event
        self.timeframe = timeframe
        self.lite_mode = lite_mode
        self.max_retries = max_retries
        self._closed = False

        # The SDK takes the *access token* (not client_id:token) and decodes the
        # HSM key from it internally. reconnect=True gives bounded backoff.
        self._ws = data_ws.FyersDataSocket(
            access_token=access_token,
            litemode=lite_mode,
            reconnect=True,
            reconnect_retry=max_retries,
            on_message=self._on_sdk_message,
            on_error=self._on_sdk_error,
            on_connect=self._on_sdk_connect,
            on_close=self._on_sdk_close,
        )
        # Map by FYERS symbol so reverse-lookup is O(1) in the hot path.
        self._fy_to_internal = dict(zip(self.fyers_symbols, self.internal_symbols))
        if auto_connect:
            self.connect()

    # -- connection control --------------------------------------------------
    def connect(self) -> None:
        if self._closed:
            return
        # SDK subscribe expects the FYERS wire symbols.
        self._ws.subscribe(self.fyers_symbols, data_type="SymbolUpdate", channel=11)
        self._ws.connect()
        log.info("FYERS WS connecting to %d symbol(s)", len(self.fyers_symbols))

    def _on_sdk_connect(self) -> None:
        log.info("FYERS WS connected & authenticated")
        if self._on_connect_cb:
            self._on_connect_cb()

    def _on_sdk_close(self, message: dict | None = None) -> None:
        log.info("FYERS WS closed: %s", message)
        if self._closed:
            return
        if self._on_disconnect_cb:
            self._on_disconnect_cb()

    def _on_sdk_error(self, message) -> None:
        log.error("FYERS WS error: %s", message)
        # The SDK surfaces auth failures as OnError dicts (type=AUTH_TYPE).
        msg = message if isinstance(message, dict) else {}
        if msg.get("type") in ("auth", "AUTH_TYPE") or msg.get("code") in (
            801, 802, 803, 804, 805,
        ):
            if self._on_auth_error_cb:
                self._on_auth_error_cb()
        # Malformed/invalid data also arrive here; route to invalid-data hook.
        elif self._on_invalid_cb:
            self._on_invalid_cb()

    def _on_sdk_message(self, data: dict) -> None:
        event = self._normalize(data)
        if event is not None and self.on_event is not None:
            self.on_event(event)

    # -- health/lifecycle callbacks (set by the live pipeline) ----------------
    _on_connect_cb = None
    _on_disconnect_cb = None
    _on_auth_error_cb = None
    _on_invalid_cb = None

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
            self._ws.close_connection()
        except Exception as e:  # pragma: no cover - best effort
            log.debug("FYERS WS close error: %s", e)

    # -- normalization (provider-specific) ------------------------------------
    def _normalize(self, data: dict) -> Optional[InternalMarketEvent]:
        """Map an SDK-decoded FYERS market dict to a normalized InternalMarketEvent.

        The SDK dict shape (decoded, precision/multiplier applied):
          litemode : {"symbol": "NSE:SBIN-EQ", "ltp": <float>, "type": "sf", ...}
          full     : {"symbol", "ltp", "open_price", "high_price", "low_price",
                      "vol_traded_today", "prev_close_price", "ch", "chp", "type", ...}
        Control/response frames (no "symbol" or no price) are skipped.
        """
        if not isinstance(data, dict):
            return None
        fy_sym = data.get("symbol")
        if not fy_sym:
            # ack / subscription-response / heartbeat frames: nothing to emit.
            return None
        internal = self._fy_to_internal.get(fy_sym)
        if internal is None:
            # Reverse-map via registry / best-effort (e.g. unknown index).
            try:
                internal = self.provider._registry_lookup_internal(fy_sym)
            except Exception:
                internal = fy_sym
        exchange = internal.split(":", 1)[0] if ":" in internal else (
            fy_sym.split(":", 1)[0] if ":" in fy_sym else ""
        )
        ltp = _to_float(data.get("ltp"))
        if ltp is None:
            return None  # no price yet -> skip (don't emit empty event)
        ts = datetime.now(timezone.utc)
        return InternalMarketEvent(
            event_type=EventType.QUOTE,
            symbol=internal,
            exchange=exchange,
            provider_symbol=fy_sym,
            timestamp=ts,
            ltp=ltp,
            open=_to_float(data.get("open_price")),
            high=_to_float(data.get("high_price")),
            low=_to_float(data.get("low_price")),
            close=ltp,  # FYERS quote has no separate "close"; close == last price
            volume=_to_float(data.get("vol_traded_today")),
            raw=data,
        )


def _to_float(x) -> Optional[float]:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f
