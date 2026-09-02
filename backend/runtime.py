"""Trading runtime manager for live pipeline state.

Owns the LiveMarketPipeline and provides shared state for API endpoints.

ARCHITECTURE NOTE:
The existing trading-system architecture is designed for a SINGLE Upstox account.
The LiveMarketPipeline uses one Upstox WebSocket connection with one set of
credentials. This means:

1. All users share the same live data feed
2. User-specific broker connections are used only for REST API calls (analysis)
3. The pipeline health monitor reflects the single shared connection

OWNERSHIP MODEL:
The runtime uses a DEDICATED Upstox account for the live market data feed.
This is separate from individual user trading accounts. Upstox does not have a
"service account" concept - the feed uses a regular user's OAuth credentials
(client_id + access_token) from a dedicated account used solely for market data.

IMPORTANT: The dedicated market data account's credentials are configured via
environment variables (UPSTOX_CLIENT_ID, UPSTOX_SERVICE_ACCOUNT_TOKEN) and are
NEVER derived from end-user broker_connections. This ensures:
- User broker credentials are never used for the shared feed
- The feed remains available regardless of which users are connected
- No user's trading account is affected by the shared feed

If no dedicated account is configured, the runtime stays in STOPPED state
and the pipeline health endpoint reports honestly.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from config import get_settings

logger = logging.getLogger(__name__)


class RuntimeStateEnum(enum.Enum):
    """Explicit runtime state machine states."""

    DISABLED = "disabled"           # Runtime is disabled by configuration
    STOPPED = "stopped"             # Runtime is stopped
    STARTING = "starting"           # Runtime is starting up
    CONNECTING = "connecting"       # WebSocket is connecting
    CONNECTED = "connected"         # WebSocket is connected and receiving data
    DISCONNECTED = "disconnected"   # WebSocket is disconnected
    AUTH_ERROR = "auth_error"       # Authentication failed
    STOPPING = "stopping"           # Runtime is shutting down
    ERROR = "error"                 # Runtime encountered an error


@dataclass
class RuntimeState:
    """Current state of the trading runtime."""

    state: RuntimeStateEnum = RuntimeStateEnum.STOPPED
    connected: bool = False
    last_event_time: Optional[float] = None
    events_received: int = 0
    candles_generated: int = 0
    errors: int = 0
    reconnect_attempts: int = 0
    started_at: Optional[float] = None
    last_error: Optional[str] = None


class TradingRuntime:
    """Manages the live trading pipeline lifecycle.

    This is a singleton-like class that should be instantiated once at application
    startup and shared across all requests via app.state.

    The runtime owns:
    - LiveMarketPipeline instance
    - DataHealthMonitor (shared across requests)
    - Background WebSocket thread
    - Reconnection state

    State Machine:
        DISABLED → (configure) → STOPPED → (start) → STARTING → CONNECTING → CONNECTED
        CONNECTED → (disconnect) → DISCONNECTED → (reconnect) → CONNECTING
        ANY → (auth failure) → AUTH_ERROR
        ANY → (error) → ERROR
        ANY → (stop) → STOPPING → STOPPED
    """

    # Reconnection configuration
    MAX_RECONNECT_ATTEMPTS = 10
    INITIAL_RECONNECT_DELAY = 1.0  # seconds
    MAX_RECONNECT_DELAY = 60.0  # seconds
    RECONNECT_BACKOFF_FACTOR = 2.0

    def __init__(self) -> None:
        self._pipeline: Optional[object] = None
        self._health_monitor: Optional[object] = None
        self._upstox_provider: Optional[object] = None
        self._websocket: Optional[object] = None
        self._state = RuntimeState()
        self._lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._websocket_thread: Optional[threading.Thread] = None
        self._reconnect_delay = self.INITIAL_RECONNECT_DELAY

    @property
    def state(self) -> RuntimeState:
        """Get current runtime state."""
        return self._state

    @property
    def health_monitor(self) -> Optional[object]:
        """Get the shared DataHealthMonitor instance."""
        return self._health_monitor

    @property
    def pipeline(self) -> Optional[object]:
        """Get the LiveMarketPipeline instance."""
        return self._pipeline

    @property
    def is_connected(self) -> bool:
        """Check if the WebSocket is currently connected."""
        return self._state.connected

    def start(self, access_token: str, symbols: list[str], timeframe: str = "1d") -> None:
        """Start the live trading runtime.

        Args:
            access_token: Upstox access token for the WebSocket connection.
                         This should be from a DEDICATED market data account, not a user token.
            symbols: List of symbols to subscribe to
            timeframe: Candle timeframe

        Raises:
            RuntimeError: If the runtime is already running or fails to start

        Note:
            This uses a single shared access token. In the current architecture,
            all users share the same live data feed. The access token should
            belong to a dedicated account used solely for market data.
        """
        with self._lock:
            if self._state.state in (RuntimeStateEnum.STARTING, RuntimeStateEnum.CONNECTED, RuntimeStateEnum.CONNECTING):
                logger.warning("Trading runtime already running or starting")
                return

            if self._state.state == RuntimeStateEnum.STOPPING:
                logger.warning("Cannot start runtime while stopping")
                raise RuntimeError("Cannot start runtime while stopping")

            self._set_state(RuntimeStateEnum.STARTING)
            self._shutdown_event.clear()

            try:
                from src.trading_system.india.live_pipeline import LiveMarketPipeline
                from src.trading_system.india.upstox import UpstoxMarketDataProvider
                from src.trading_system.india.data_health import DataHealthMonitor
                from src.trading_system.config import settings as ts_settings

                # Get client_id from settings (dedicated market data account)
                settings = get_settings()
                client_id = settings.upstox_client_id

                if not client_id:
                    raise RuntimeError(
                        "UPSTOX_CLIENT_ID is not configured. "
                        "The live pipeline requires a dedicated Upstox account's client ID."
                    )

                # Create health monitor
                self._health_monitor = DataHealthMonitor(
                    stale_seconds=ts_settings.market.stale_seconds
                )

                # Create pipeline
                self._pipeline = LiveMarketPipeline(
                    symbols=symbols,
                    timeframe=timeframe,
                    health=self._health_monitor,
                )

                # Create Upstox provider for WebSocket
                self._upstox_provider = UpstoxMarketDataProvider(
                    client_id=client_id,
                    access_token=access_token,
                )

                # Start the pipeline
                self._pipeline.start()
                self._state.started_at = time.time()

                # Connect WebSocket in a background thread
                self._start_websocket_thread(client_id, access_token, symbols, timeframe)

                logger.info("Trading runtime started with symbols: %s", symbols)

            except Exception as e:
                logger.error("Failed to start trading runtime: %s", str(e))
                self._set_state(RuntimeStateEnum.ERROR, error=str(e))
                raise

    def _start_websocket_thread(
        self, client_id: str, access_token: str, symbols: list[str], timeframe: str
    ) -> None:
        """Start the WebSocket connection in a background thread.

        Uses Upstox V3 WebSocket protocol:
        1. Authorize to get authorized WebSocket URL
        2. Connect to authorized URL
        3. Subscribe to instruments with binary protobuf messages
        4. Decode protobuf market data messages

        The Upstox WebSocket uses websocket-client which is synchronous.
        We run it in a thread to avoid blocking the asyncio event loop.

        This method handles reconnection with bounded exponential backoff.
        """
        def run_websocket():
            self._reconnect_delay = self.INITIAL_RECONNECT_DELAY
            reconnect_attempts = 0

            while not self._shutdown_event.is_set():
                try:
                    self._set_state(RuntimeStateEnum.CONNECTING)

                    from src.trading_system.india.upstox_v3_ws import UpstoxV3WebSocket
                    from src.trading_system.india.upstox_v3_resolver import (
                        UpstoxV3InstrumentResolver,
                        UnresolvedInstrumentError,
                    )

                    # Convert internal symbols to Upstox V3 instrument keys.
                    # V3 equities require the ISIN-based key (e.g.
                    # ``NSE_EQ|INE062A01020``), NOT the legacy ``NSE_EQ|SBIN``
                    # form. The resolver looks up ISINs via the official
                    # Upstox V2 search API. Index keys are mapped locally
                    # from a well-known set.
                    resolver = UpstoxV3InstrumentResolver(
                        access_token=access_token,
                    )
                    instrument_keys: list[str] = []
                    unresolved: list[str] = []
                    for s in symbols:
                        try:
                            instrument_keys.append(resolver.resolve(s))
                        except UnresolvedInstrumentError as e:
                            logger.error(
                                "Skipping unresolved Upstox V3 instrument for %s: %s",
                                s, e,
                            )
                            unresolved.append(s)
                    if not instrument_keys:
                        raise RuntimeError(
                            f"None of the requested symbols could be resolved to "
                            f"Upstox V3 instrument keys: {symbols}"
                        )
                    if unresolved:
                        logger.warning(
                            "Subscribed with %d instruments (%d unresolved skipped): %s",
                            len(instrument_keys), len(unresolved), unresolved,
                        )

                    # Create V3 WebSocket client
                    self._websocket = UpstoxV3WebSocket(
                        access_token=access_token,
                        instrument_keys=instrument_keys,
                        on_event=self._pipeline.ingest,
                        max_retries=self.MAX_RECONNECT_ATTEMPTS,
                    )

                    # Wire health callbacks
                    self._websocket.on_connect_cb(self._on_websocket_connect)
                    self._websocket.on_disconnect_cb(self._on_websocket_disconnect)
                    self._websocket.on_auth_error_cb(self._on_websocket_auth_error)
                    self._websocket.on_error_cb(lambda e: self._health_monitor.on_invalid())

                    # Connect (this calls authorize endpoint first)
                    self._websocket.connect()

                    # Reset reconnect state on successful connection
                    reconnect_attempts = 0
                    self._reconnect_delay = self.INITIAL_RECONNECT_DELAY

                    # Wait for shutdown or disconnect
                    while not self._shutdown_event.is_set():
                        time.sleep(0.1)

                except Exception as e:
                    if self._shutdown_event.is_set():
                        break

                    reconnect_attempts += 1
                    self._state.reconnect_attempts = reconnect_attempts

                    if reconnect_attempts >= self.MAX_RECONNECT_ATTEMPTS:
                        logger.error("Max reconnect attempts reached, stopping runtime")
                        self._set_state(RuntimeStateEnum.ERROR, error="Max reconnect attempts reached")
                        break

                    logger.warning(
                        "WebSocket connection failed (attempt %d/%d): %s",
                        reconnect_attempts, self.MAX_RECONNECT_ATTEMPTS, str(e)
                    )
                    self._set_state(RuntimeStateEnum.DISCONNECTED, error=str(e))

                    # Bounded exponential backoff
                    delay = min(
                        self._reconnect_delay * (self.RECONNECT_BACKOFF_FACTOR ** (reconnect_attempts - 1)),
                        self.MAX_RECONNECT_DELAY
                    )
                    logger.info("Reconnecting in %.1f seconds...", delay)

                    # Wait with shutdown check
                    if self._shutdown_event.wait(timeout=delay):
                        break

        self._websocket_thread = threading.Thread(target=run_websocket, daemon=True)
        self._websocket_thread.start()

    def _on_websocket_connect(self) -> None:
        """Handle WebSocket connection."""
        self._set_state(RuntimeStateEnum.CONNECTED)
        logger.info("WebSocket connected")

    def _on_websocket_disconnect(self) -> None:
        """Handle WebSocket disconnection."""
        if not self._shutdown_event.is_set():
            self._set_state(RuntimeStateEnum.DISCONNECTED)
            logger.info("WebSocket disconnected, will attempt reconnect")

    def _on_websocket_auth_error(self) -> None:
        """Handle WebSocket authentication error."""
        self._set_state(RuntimeStateEnum.AUTH_ERROR)
        logger.error("WebSocket authentication failed")
        # Don't reconnect on auth errors - requires manual intervention

    def _set_state(self, new_state: RuntimeStateEnum, error: Optional[str] = None) -> None:
        """Set the runtime state."""
        old_state = self._state.state
        self._state.state = new_state
        self._state.connected = (new_state == RuntimeStateEnum.CONNECTED)
        if error:
            self._state.last_error = error
            self._state.errors += 1
        logger.debug("Runtime state: %s -> %s", old_state.value, new_state.value)

    def stop(self) -> None:
        """Stop the live trading runtime.

        This method:
        1. Signals the shutdown event to stop reconnection
        2. Stops the pipeline
        3. Closes the WebSocket
        4. Waits for the background thread to terminate
        """
        with self._lock:
            if self._state.state in (RuntimeStateEnum.STOPPED, RuntimeStateEnum.STOPPING):
                return

            self._set_state(RuntimeStateEnum.STOPPING)
            self._shutdown_event.set()

            try:
                # Stop pipeline
                if self._pipeline:
                    self._pipeline.stop()

                # Close WebSocket
                if self._websocket:
                    self._websocket.close()

                # Wait for thread to terminate (with timeout)
                if self._websocket_thread and self._websocket_thread.is_alive():
                    self._websocket_thread.join(timeout=5.0)
                    if self._websocket_thread.is_alive():
                        logger.warning("WebSocket thread did not terminate cleanly")

                self._set_state(RuntimeStateEnum.STOPPED)
                logger.info("Trading runtime stopped")

            except Exception as e:
                logger.error("Error stopping trading runtime: %s", str(e))
                self._set_state(RuntimeStateEnum.ERROR, error=str(e))

    def record_event(self) -> None:
        """Record a received event (called by pipeline)."""
        self._state.last_event_time = time.time()
        self._state.events_received += 1

    def record_candle(self) -> None:
        """Record a generated candle (called by pipeline)."""
        self._state.candles_generated += 1

    def get_pipeline_status(self) -> dict:
        """Get current pipeline status for API responses."""
        if self._state.state == RuntimeStateEnum.DISABLED:
            return {
                "status": "disabled",
                "connected": False,
                "message": "Live pipeline is disabled",
            }

        if not self._state.state == RuntimeStateEnum.CONNECTED or not self._health_monitor:
            return {
                "status": self._state.state.value,
                "connected": False,
                "message": f"Runtime state: {self._state.state.value}",
            }

        snapshot = self._health_monitor.snapshot()
        current_time = int(time.time() * 1000)

        return {
            "status": snapshot.get("status", "unknown"),
            "connected": snapshot.get("connected", False),
            "events_received": snapshot.get("events_received", 0),
            "events_rejected": snapshot.get("events_rejected", 0),
            "candles_generated": snapshot.get("candles_generated", 0),
            "last_event_time": (
                int(snapshot["latest_event_ts"] * 1000)
                if snapshot.get("latest_event_ts")
                else None
            ),
            "uptime_seconds": (
                int(time.time() - self._state.started_at)
                if self._state.started_at
                else 0
            ),
        }


# Global runtime instance (singleton)
_runtime: Optional[TradingRuntime] = None


def get_trading_runtime() -> TradingRuntime:
    """Get the global trading runtime instance."""
    global _runtime
    if _runtime is None:
        _runtime = TradingRuntime()
    return _runtime


def reset_trading_runtime() -> None:
    """Reset the global runtime (for testing)."""
    global _runtime
    if _runtime and _runtime.state.state not in (RuntimeStateEnum.STOPPED, RuntimeStateEnum.DISABLED):
        _runtime.stop()
    _runtime = None
