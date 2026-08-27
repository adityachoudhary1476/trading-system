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
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = requests.get(url, params=params, timeout=self.timeout)
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
    """Thin wrapper around websocket-client for FYERS data socket v2.

    Handles auth, subscription, heartbeat, stale-data detection, reconnect with
    exponential backoff, malformed-message skipping, and normalization into
    InternalMarketEvent. The AI is never called from this hot path.
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
    ) -> None:
        try:
            import websocket  # websocket-client
        except ImportError as e:
            raise RuntimeError(
                "websocket-client is required for FYERS live data: pip install websocket-client"
            ) from e

        self.client_id = client_id
        self.access_token = access_token
        self.provider = provider
        self.symbols = symbols
        self.on_event = on_event
        self.timeframe = timeframe
        self.lite_mode = lite_mode
        self.max_retries = max_retries
        self._ws = None
        self._closed = False
        self._last_msg_ts: Optional[float] = None
        self._attempt = 0
        self._ws_mod = websocket
        self._open()

    def _auth_frame(self) -> dict:
        return {
            "T": "c",
            "authorization": f"{self.client_id}:{self.access_token}",
            "src": "py-trading-system",
            "id": int(time.time()),
        }

    def _subscribe_frame(self) -> dict:
        return {"T": "t", "symbols": self.symbols}

    def _open(self) -> None:
        # Lazy import at connect time keeps import cost out of the hot path.
        self._ws = self._ws_mod.WebSocketApp(
            _WS_URL,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        # run_forever with auto-reconnect handled by our own logic (no aggressive loop).
        import threading

        self._thread = threading.Thread(
            target=self._ws.run_forever, kwargs={"ping_interval": 20}, daemon=True
        )
        self._thread.start()

    def _on_open(self, ws):
        log.info("FYERS WS open; authenticating")
        ws.send(str(self._auth_frame()).replace("'", '"'))
        ws.send(str(self._subscribe_frame()).replace("'", '"'))

    def _on_message(self, ws, raw: str):
        self._last_msg_ts = time.time()
        try:
            import json

            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("FYERS WS malformed message dropped")
            return
        event = self.provider._normalize_ws(msg, self.symbols)
        if event is not None:
            self.on_event(event)

    def _on_error(self, ws, error):
        log.error("FYERS WS error: %s", error)

    def _on_close(self, ws, *args):
        log.info("FYERS WS closed")
        if self._closed:
            return
        if self._attempt < self.max_retries:
            backoff = min(30, 2 ** self._attempt)
            self._attempt += 1
            log.info("FYERS WS reconnect in %ss (attempt %d)", backoff, self._attempt)
            time.sleep(backoff)
            try:
                self._open()
            except Exception as e:
                log.error("FYERS WS reconnect failed: %s", e)
        else:
            log.error("FYERS WS max retries reached; giving up")

    def stale(self, now: Optional[float] = None) -> bool:
        now = now or time.time()
        if self._last_msg_ts is None:
            return False
        return (now - self._last_msg_ts) > 60  # no data for 60s -> stale

    def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass


# --- WS message normalization (provider-specific) ----------------------------
def _normalize_ws(self, msg: dict, symbols: list[str]) -> Optional[InternalMarketEvent]:
    """Map a FYERS socket message to an InternalMarketEvent (best-effort)."""
    # FYERS symbolUpdate / ltp messages vary; handle the documented shape and
    # skip control frames (T=c, T=t, T=h heartbeat).
    t = msg.get("T")
    if t in ("c", "h", "sub", "unsub"):
        return None
    # SymbolUpdate: msg has "symbols" list of [symbol, ltp, ...] or "v" payloads.
    sym_field = msg.get("symbol") or (msg.get("symbols") or [None])[0] if isinstance(msg.get("symbols"), list) else msg.get("symbol")
    if sym_field is None:
        return None
    internal = self.registry.resolve(sym_field).key if False else _lookup_internal(self, sym_field)
    v = msg.get("v", msg)
    ts = datetime.now(timezone.utc)
    return InternalMarketEvent(
        event_type=EventType.QUOTE,
        symbol=internal,
        exchange=internal.split(":")[0],
        provider_symbol=sym_field,
        timestamp=ts,
        ltp=float(v.get("lp", v.get("ltp", 0))) if isinstance(v, dict) else None,
        open=float(v["o"]) if isinstance(v, dict) and "o" in v else None,
        high=float(v["h"]) if isinstance(v, dict) and "h" in v else None,
        low=float(v["l"]) if isinstance(v, dict) and "l" in v else None,
        close=float(v["c"]) if isinstance(v, dict) and "c" in v else None,
        volume=float(v.get("v", 0)) if isinstance(v, dict) else 0.0,
        raw=msg,
    )


def _lookup_internal(self, fyers_symbol: str) -> str:
    # Reverse map via registry if known; else best-effort parse.
    for instr in self.registry._by_key.values():
        if instr.provider_symbol == fyers_symbol:
            return instr.key
    try:
        from .symbol_map import from_fyers_symbol

        return from_fyers_symbol(fyers_symbol).key
    except Exception:
        return fyers_symbol


# Attach instance method (kept here to avoid clutter in the class above).
FYERSMarketDataProvider._normalize_ws = _normalize_ws
