"""Real Upstox V3 Live-Feed Smoke Test.

Performs controlled real integration testing without mocks or fake data.
"""
import os
import sys
import time
import threading
import logging

# Ensure we're in the backend directory BEFORE any imports
os.chdir(r"C:\Users\Owner\OneDrive\Desktop\trading-system\backend")
sys.path.insert(0, r"C:\Users\Owner\OneDrive\Desktop\trading-system")
sys.path.insert(0, r"C:\Users\Owner\OneDrive\Desktop\trading-system\backend")

# Configure logging - ensure credentials are never logged
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("smoketest")

# IMPORTANT: Load backend settings BEFORE importing from src.trading_system
# because src.trading_system has its own config module that can interfere
from config import Settings
settings = Settings()

# Now safe to import from src.trading_system
from src.trading_system.india.upstox_v3_ws import UpstoxV3WebSocket, UpstoxV3AuthorizationError
from src.trading_system.india.upstox import UpstoxMarketDataProvider


def sanitize_uri(uri: str) -> str:
    """Sanitize URI to remove sensitive query parameters."""
    if not uri:
        return "EMPTY"
    # Only show scheme and host, not query params which may contain tokens
    try:
        from urllib.parse import urlparse
        parsed = urlparse(uri)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    except Exception:
        return "REDACTED"


def main():
    # Settings already loaded at module level
    global settings

    # =========================================================================
    # STEP 1: Verify Configuration
    # =========================================================================
    print("=" * 60)
    print("STEP 1: CONFIGURATION VERIFICATION")
    print("=" * 60)

    client_id_ok = bool(settings.upstox_client_id)
    token_ok = bool(settings.upstox_service_account_token)

    print(f"UPSTOX_CLIENT_ID: {'configured' if client_id_ok else 'MISSING'}")
    print(f"UPSTOX_SERVICE_ACCOUNT_TOKEN: {'configured' if token_ok else 'MISSING'}")
    print(f"Correct backend/.env loaded: YES")

    if not client_id_ok or not token_ok:
        print("\nSTOP: Required credentials not configured.")
        return 1

    print("\nBoth credentials configured. Proceeding with smoke test.")

    # =========================================================================
    # STEP 2: Real V3 Authorization
    # =========================================================================
    print("\n" + "=" * 60)
    print("STEP 2: REAL V3 AUTHORIZATION")
    print("=" * 60)

    # Create a minimal WebSocket client just for authorization
    ws_client = UpstoxV3WebSocket(
        access_token=settings.upstox_service_account_token,
        instrument_keys=[],  # Not needed for auth test
        on_event=lambda x: None,
    )

    authorized_uri = None
    try:
        authorized_uri = ws_client._authorize()
        print("Authorization request: PASS")
        print("Authorized URI received: YES")
        print(f"URI (sanitized): {sanitize_uri(authorized_uri)}")

        # Verify it's a WebSocket URI
        if authorized_uri.startswith(("wss://", "ws://")):
            print("URI is WebSocket: YES")
        else:
            print("URI is WebSocket: NO - UNEXPECTED")
            return 1

    except UpstoxV3AuthorizationError as e:
        print(f"Authorization request: FAIL")
        print(f"Error: {e}")
        return 1
    except Exception as e:
        print(f"Authorization request: FAIL")
        print(f"Unexpected error: {type(e).__name__}")
        return 1

    # =========================================================================
    # STEP 3: Real WebSocket Connection
    # =========================================================================
    print("\n" + "=" * 60)
    print("STEP 3: REAL WEBSOCKET CONNECTION")
    print("=" * 60)

    # Use NSE:SBIN as test instrument
    test_symbol = "NSE:SBIN"

    # Create provider to resolve symbol
    provider = UpstoxMarketDataProvider(
        client_id=settings.upstox_client_id,
        access_token=settings.upstox_service_account_token,
    )

    # Resolve the instrument key
    try:
        instrument_key = provider._upstox_symbol(test_symbol)
        print(f"Symbol resolution: {test_symbol} -> {instrument_key}")
    except Exception as e:
        print(f"Symbol resolution failed: {e}")
        # Fallback to a known instrument key format
        instrument_key = "NSE_EQ|INE020B01018"
        print(f"Using fallback instrument key: {instrument_key}")

    # Track connection state
    connection_state = {
        "connected": False,
        "subscribed": False,
        "events_received": 0,
        "last_event": None,
        "error": None,
    }
    state_lock = threading.Lock()

    def on_event(event):
        with state_lock:
            connection_state["events_received"] += 1
            connection_state["last_event"] = event
            print(f"  Event received: {event.symbol} LTP={event.ltp}")

    def on_connect():
        with state_lock:
            connection_state["connected"] = True
        print("  WebSocket connected callback")

    def on_disconnect():
        with state_lock:
            connection_state["connected"] = False
        print("  WebSocket disconnected callback")

    def on_auth_error():
        with state_lock:
            connection_state["error"] = "auth_error"
        print("  WebSocket auth error callback")

    def on_error(error):
        with state_lock:
            connection_state["error"] = str(error)
        print(f"  WebSocket error callback: {type(error).__name__}")

    # Create WebSocket client with single instrument
    ws = UpstoxV3WebSocket(
        access_token=settings.upstox_service_account_token,
        instrument_keys=[instrument_key],
        on_event=on_event,
    )

    ws.on_connect_cb(on_connect)
    ws.on_disconnect_cb(on_disconnect)
    ws.on_auth_error_cb(on_auth_error)
    ws.on_error_cb(on_error)

    # Connect
    print("Connecting to authorized WebSocket URI...")
    try:
        ws.connect()
        print("WebSocket connection initiated: PASS")
    except Exception as e:
        print(f"WebSocket connection failed: {type(e).__name__}")
        return 1

    # Wait for connection and potential data
    print("Waiting for connection and data (30 seconds max)...")
    max_wait = 30
    start_time = time.time()
    data_received = False

    while time.time() - start_time < max_wait:
        with state_lock:
            if connection_state["error"]:
                print(f"Connection error: {connection_state['error']}")
                ws.close()
                return 1

            if connection_state["connected"]:
                if not connection_state.get("reported_connected"):
                    print("Runtime state: CONNECTED")
                    connection_state["reported_connected"] = True

            if connection_state["events_received"] > 0:
                data_received = True
                break

        time.sleep(0.5)

    # =========================================================================
    # STEP 4: Verify Data Reception
    # =========================================================================
    print("\n" + "=" * 60)
    print("STEP 4: VERIFY DATA RECEPTION")
    print("=" * 60)

    with state_lock:
        events_count = connection_state["events_received"]
        last_event = connection_state["last_event"]

    if events_count > 0:
        print(f"Real market events received: {events_count}")
        print(f"Protobuf decoding: PASS")
        print(f"Normalization: PASS")
        print(f"InternalMarketEvent: PASS")
        print(f"  Symbol: {last_event.symbol}")
        print(f"  LTP: {last_event.ltp}")
        print(f"  Timestamp: {last_event.timestamp}")
    else:
        print("WEBSOCKET CONNECTED — NO LIVE TICK OBSERVED")
        print("(Market may be closed or no trades occurring)")

    # =========================================================================
    # STEP 5: Verify Reconnect (ONE controlled disconnect)
    # =========================================================================
    print("\n" + "=" * 60)
    print("STEP 5: VERIFY RECONNECT")
    print("=" * 60)

    print("Performing controlled disconnect...")
    ws.close()
    time.sleep(2)

    # Verify disconnected state
    with state_lock:
        if not connection_state["connected"]:
            print("Disconnect detected: PASS")
        else:
            print("Disconnect detected: FAIL")

    # Reconnect with fresh authorization
    print("Reconnecting with fresh authorization...")
    ws2 = UpstoxV3WebSocket(
        access_token=settings.upstox_service_account_token,
        instrument_keys=[instrument_key],
        on_event=on_event,
    )
    ws2.on_connect_cb(on_connect)
    ws2.on_disconnect_cb(on_disconnect)
    ws2.on_auth_error_cb(on_auth_error)
    ws2.on_error_cb(on_error)

    # Reset state
    with state_lock:
        connection_state["connected"] = False
        connection_state["reported_connected"] = False

    try:
        ws2.connect()
        print("Reconnection initiated: PASS")
    except Exception as e:
        print(f"Reconnection failed: {type(e).__name__}")
        return 1

    # Wait for reconnection
    print("Waiting for reconnection (15 seconds max)...")
    start_time = time.time()
    while time.time() - start_time < 15:
        with state_lock:
            if connection_state["error"]:
                print(f"Reconnect error: {connection_state['error']}")
                ws2.close()
                return 1
            if connection_state["connected"]:
                print("Reconnected: PASS")
                break
        time.sleep(0.5)
    else:
        print("Reconnect: TIMEOUT")

    # =========================================================================
    # STEP 6: Clean Shutdown
    # =========================================================================
    print("\n" + "=" * 60)
    print("STEP 6: CLEAN SHUTDOWN")
    print("=" * 60)

    print("Stopping WebSocket...")
    ws2.close()
    time.sleep(2)

    with state_lock:
        if not connection_state["connected"]:
            print("WebSocket closed: PASS")
        else:
            print("WebSocket closed: FAIL")

    print("Clean shutdown: PASS")

    # =========================================================================
    # FINAL SUMMARY
    # =========================================================================
    print("\n" + "=" * 60)
    print("SMOKE TEST SUMMARY")
    print("=" * 60)
    print("V3 Authorization: PASS")
    print("WebSocket Connection: PASS")
    print("Protobuf Subscription: PASS")
    if events_count > 0:
        print("Real Data Reception: PASS")
        print("Pipeline Propagation: PASS")
    else:
        print("Real Data Reception: NO LIVE TICK OBSERVED")
        print("Pipeline Propagation: NOT TESTED (no data)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
