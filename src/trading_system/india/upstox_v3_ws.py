"""Upstox V3 WebSocket client implementation.

This module implements the Upstox Market Data Feed V3 protocol:
1. Call authorize endpoint to get authorized WebSocket URL
2. Connect to authorized URL
3. Send subscription messages (JSON encoded as UTF-8, sent in a WebSocket binary
   frame per the V3 protocol). Server -> client responses are still binary
   Protobuf ``FeedResponse`` messages that MUST be decoded via
   ``FeedResponse.deserialize()``.

Reference: https://upstox.com/developer/api-documentation/v3/get-market-data-feed/
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

from .events import InternalMarketEvent, EventType
from .live_market_state import normalize_timestamp_ms, TimestampValidationError
from .upstox_v3_pb import (
    FeedResponse,
    FeedType,
    RequestMode,
    SubscriptionRequest,
)

log = logging.getLogger(__name__)

# Upstox V3 API endpoints
_BASE_URL = "https://api.upstox.com/v3"
_AUTHORIZE_ENDPOINT = "/feed/market-data-feed/authorize"

# Mode map: internal RequestMode enum -> V3 JSON mode string.
# The V3 protocol expects the ``mode`` field as a STRING (per official SDK),
# not a numeric varint. This map MUST be kept in sync with the SDK:
#   https://github.com/upstox/upstox-python (market_data_feeder_v3.py)
_V3_MODE_STRING = {
    RequestMode.LTPC: "ltpc",
    RequestMode.FULL_D5: "full",
    RequestMode.OPTION_GREEKS: "option_greeks",
    RequestMode.FULL_D30: "full_d30",
}

# Reverse map for tests / introspection.
_V3_METHOD_STRING = {
    "sub": "sub",
    "unsub": "unsub",
    "change_mode": "change_mode",
}


class UpstoxV3AuthorizationError(Exception):
    """Raised when V3 authorization fails."""
    pass


class UpstoxV3WebSocket:
    """Upstox V3 Market Data WebSocket client.

    Implements the V3 protocol:
    1. Authorize to get WebSocket URL
    2. Connect to authorized URL
    3. Subscribe to instruments
    4. Decode protobuf messages
    """

    def __init__(
        self,
        access_token: str,
        instrument_keys: list[str],
        on_event: Callable,
        symbol_map: Optional[dict[str, str]] = None,
        mode: RequestMode = RequestMode.LTPC,
        max_retries: int = 6,
    ) -> None:
        """
        Initialize V3 WebSocket client.

        Args:
            access_token: Upstox OAuth access token
            instrument_keys: List of instrument keys (e.g., ["NSE_EQ|INE020B01018"])
            on_event: Callback for market events
            mode: Subscription mode (LTPC, FULL_D5, OPTION_GREEKS, FULL_D30)
            max_retries: Maximum reconnection attempts
        """
        self.access_token = access_token
        self.instrument_keys = instrument_keys
        self.on_event = on_event
        self.symbol_map = symbol_map or {}
        self.mode = mode
        self.max_retries = max_retries

        self._ws = None
        self._closed = False
        self._authorized_url: Optional[str] = None
        self._on_connect_cb: Optional[Callable] = None
        self._on_disconnect_cb: Optional[Callable] = None
        self._on_auth_error_cb: Optional[Callable] = None
        self._on_error_cb: Optional[Callable] = None
        self._on_invalid_cb: Optional[Callable] = None
        self._ws_thread: Optional[threading.Thread] = None

    def _authorize(self) -> str:
        """
        Call the V3 authorize endpoint to get the authorized WebSocket URL.

        Returns:
            Authorized WebSocket URL

        Raises:
            UpstoxV3AuthorizationError: If authorization fails
        """
        url = f"{_BASE_URL}{_AUTHORIZE_ENDPOINT}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "*/*",
        }

        try:
            response = requests.get(url, headers=headers, timeout=30)

            if response.status_code == 401:
                raise UpstoxV3AuthorizationError("Access token is invalid or expired")
            if response.status_code == 403:
                raise UpstoxV3AuthorizationError("Access token does not have market data permission")

            response.raise_for_status()

            data = response.json()
            if data.get("status") != "success":
                raise UpstoxV3AuthorizationError(f"Authorization failed: {data}")

            authorized_url = data.get("data", {}).get("authorized_redirect_uri")
            if not authorized_url:
                raise UpstoxV3AuthorizationError("No authorized_redirect_uri in response")

            return authorized_url

        except requests.RequestException as e:
            raise UpstoxV3AuthorizationError(f"Authorization request failed: {e}")

    def connect(self) -> None:
        """
        Establish WebSocket connection.

        This method:
        1. Authorizes to get the WebSocket URL
        2. Connects to the authorized URL in a background thread
        3. Subscribes to instruments
        """
        if self._closed:
            return

        try:
            import websocket

            # Step 1: Authorize
            self._authorized_url = self._authorize()
            log.info("V3 authorization successful")

            # Step 2: Connect to authorized URL
            def on_open(ws):
                log.info("V3 WebSocket connected")
                self._subscribe()
                if self._on_connect_cb:
                    self._on_connect_cb()

            def on_error(ws, error):
                log.error("V3 WebSocket error: %s", error)
                if self._on_error_cb:
                    self._on_error_cb(error)

            def on_close(ws, status, message):
                log.info("V3 WebSocket closed: %s %s", status, message)
                if not self._closed and self._on_disconnect_cb:
                    self._on_disconnect_cb()

            def on_message(ws, message):
                self._handle_message(message)

            self._ws = websocket.WebSocketApp(
                self._authorized_url,
                on_open=on_open,
                on_error=on_error,
                on_close=on_close,
                on_message=on_message,
            )

            # Run WebSocket in a background thread (run_forever is blocking)
            self._ws_thread = threading.Thread(
                target=self._ws.run_forever,
                daemon=True,
            )
            self._ws_thread.start()

        except UpstoxV3AuthorizationError as e:
            log.error("V3 authorization failed: %s", e)
            if self._on_auth_error_cb:
                self._on_auth_error_cb()
            raise
        except Exception as e:
            log.error("V3 WebSocket connection failed: %s", e)
            raise

    def _subscribe(self) -> None:
        """Send a subscription request to the V3 WebSocket.

        Per the official Upstox V3 protocol, the client -> server subscription is
        a JSON object (UTF-8 encoded) sent in a WebSocket BINARY frame. Server
        responses remain binary Protobuf ``FeedResponse`` messages which are
        decoded by ``_handle_message``.
        """
        if not self._ws:
            return
        payload = self._build_json_subscription(
            method="sub", mode=self.mode, instrument_keys=self.instrument_keys,
        )
        # WebSocket opcode 2 = binary frame. The V3 protocol requires the JSON
        # payload to be sent as a binary frame, NOT as a text frame.
        self._ws.send(payload, opcode=2)
        log.info(
            "Subscribed to %d instruments in mode=%s (JSON-in-binary)",
            len(self.instrument_keys),
            _V3_MODE_STRING.get(self.mode, str(self.mode)),
        )

    @staticmethod
    def _build_json_subscription(
        method: str,
        instrument_keys: list[str],
        mode: Optional[RequestMode] = None,
        guid: Optional[str] = None,
    ) -> bytes:
        """Build the V3 subscription payload as UTF-8 JSON bytes.

        This is the canonical V3 subscription format (per the official SDK):
        the client sends a JSON object with ``guid``, ``method`` and ``data``
        (containing ``mode`` and ``instrumentKeys``) over a WebSocket binary
        frame. Returns UTF-8 encoded bytes ready to be sent as opcode=2.

        The ``mode`` argument is optional for ``unsub`` (no mode change needed).
        """
        if method not in _V3_METHOD_STRING:
            raise ValueError(
                f"Invalid V3 subscription method: {method!r} "
                f"(expected one of {sorted(_V3_METHOD_STRING)})"
            )
        if not instrument_keys:
            raise ValueError("instrument_keys must not be empty for a V3 subscription")
        if method != "unsub" and mode is None:
            raise ValueError("mode is required for V3 subscriptions other than 'unsub'")

        data: dict = {"instrumentKeys": list(instrument_keys)}
        if mode is not None:
            data["mode"] = _V3_MODE_STRING[mode]
        request = {
            "guid": guid or str(uuid.uuid4()),
            "method": method,
            "data": data,
        }
        # ensure_ascii=False so non-ASCII instrument names (e.g. "Nifty 50")
        # remain readable; encode as UTF-8.
        return json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _handle_message(self, message) -> None:
        """
        Handle incoming WebSocket message.

        V3 messages are protobuf-encoded binary data.
        """
        try:
            # Decode protobuf
            if isinstance(message, bytes):
                feed_response = FeedResponse.deserialize(message)
            elif isinstance(message, str):
                # Some messages might be JSON (e.g., errors)
                data = json.loads(message)
                log.warning("Received JSON message: %s", data)
                return
            else:
                log.warning("Unknown message type: %s", type(message))
                return

            # Process feeds
            for instrument_key, feed in feed_response.feeds.items():
                event = self._normalize(instrument_key, feed, feed_response.current_ts)
                if event and self.on_event:
                    self.on_event(event)

        except Exception as e:
            log.error("Failed to decode V3 message: %s", e)
            if self._on_invalid_cb:
                self._on_invalid_cb(e)

    def _normalize(
        self,
        instrument_key: str,
        feed,
        current_ts: int,
    ) -> Optional[InternalMarketEvent]:
        """
        Normalize V3 feed to InternalMarketEvent.

        Args:
            instrument_key: The instrument key (e.g., "NSE_EQ|INE020B01018")
            feed: Feed object
            current_ts: Timestamp from feed response

        Returns:
            InternalMarketEvent or None
        """
        # Extract LTPC data
        ltpc = feed.ltpc
        if not ltpc and feed.full_feed:
            ltpc = feed.full_feed.ltpc

        if not ltpc or not ltpc.ltp:
            return None

        # V3 timestamps are milliseconds. Missing market time is not replaced
        # with server time because that would create a false market event.
        try:
            timestamp_ms = normalize_timestamp_ms(ltpc.ltt or current_ts)
        except TimestampValidationError:
            log.warning("Dropping Upstox event with invalid market timestamp")
            return None
        if timestamp_ms is None:
            return None
        ts = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)

        # Extract OHLC if available
        open_price = None
        high_price = None
        low_price = None
        close_price = ltpc.cp if ltpc.cp else ltpc.ltp
        volume = 0

        if feed.full_feed and feed.full_feed.market_ohlc:
            # Use the first OHLC entry (usually 1-minute)
            ohlc = feed.full_feed.market_ohlc[0]
            open_price = ohlc.open if ohlc.open else None
            high_price = ohlc.high if ohlc.high else None
            low_price = ohlc.low if ohlc.low else None
            close_price = ohlc.close if ohlc.close else close_price
            volume = ohlc.vol if ohlc.vol else 0

        return InternalMarketEvent(
            event_type=EventType.QUOTE,
            symbol=self.symbol_map.get(instrument_key, instrument_key),
            exchange=instrument_key.split("|")[0] if "|" in instrument_key else "",
            provider_symbol=instrument_key,
            timestamp=ts,
            ltp=ltpc.ltp,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume=volume,
            fetched_at=datetime.now(timezone.utc),
            raw={"instrument_key": instrument_key, "ltpc": ltpc},
        )

    def on_connect_cb(self, cb: Callable) -> None:
        """Set connect callback."""
        self._on_connect_cb = cb

    def on_disconnect_cb(self, cb: Callable) -> None:
        """Set disconnect callback."""
        self._on_disconnect_cb = cb

    def on_auth_error_cb(self, cb: Callable) -> None:
        """Set auth error callback."""
        self._on_auth_error_cb = cb

    def on_error_cb(self, cb: Callable) -> None:
        """Set error callback."""
        self._on_error_cb = cb

    def on_invalid_cb(self, cb: Callable) -> None:
        """Set malformed-message callback."""
        self._on_invalid_cb = cb

    def close(self) -> None:
        """Close the WebSocket connection."""
        self._closed = True
        try:
            if self._ws is not None:
                self._ws.close()
            # Wait for background thread to terminate
            if hasattr(self, '_ws_thread') and self._ws_thread is not None:
                self._ws_thread.join(timeout=5.0)
        except Exception as e:
            log.debug("V3 WebSocket close error: %s", e)
