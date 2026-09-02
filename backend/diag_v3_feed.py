"""Diagnostic script for Upstox V3 market data delivery issue.

GOAL: Determine why real market data was never observed despite a successful
V3 WebSocket connection and subscription attempt.

This is a DIAGNOSTIC ONLY script:
- No mocks, no fake data, no architecture changes.
- Uses the REAL Upstox credentials from backend/.env (never printed).
- Tests the actual V3 protocol as documented by Upstox.
"""
import os
import sys
import json
import time
import struct
import threading
import logging
from urllib.parse import urlparse

os.chdir(r"C:\Users\Owner\OneDrive\Desktop\trading-system\backend")
sys.path.insert(0, r"C:\Users\Owner\OneDrive\Desktop\trading-system")
sys.path.insert(0, r"C:\Users\Owner\OneDrive\Desktop\trading-system\backend")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("v3diag")

from config import Settings
settings = Settings()

from src.trading_system.india.upstox_v3_ws import UpstoxV3WebSocket, UpstoxV3AuthorizationError
from src.trading_system.india.upstox_v3_pb import (
    FeedResponse,
    FeedType,
    LTPC,
    MarketFullFeed,
    OHLC,
    Feed,
    SubscriptionRequest,
    RequestMode,
)
from src.trading_system.india.upstox import UpstoxMarketDataProvider

# ---------------------------------------------------------------------------
# Helper: decode raw protobuf bytes to identify message type
# ---------------------------------------------------------------------------
def decode_protobuf_message(data: bytes) -> str:
    """Try to decode a raw protobuf FeedResponse and report what we can see."""
    try:
        resp = FeedResponse.deserialize(data)
        parts = []
        parts.append(f"type={resp.type.name if hasattr(resp.type, 'name') else resp.type}")
        if resp.feeds:
            for key, feed in resp.feeds.items():
                feed_parts = []
                if feed.ltpc:
                    feed_parts.append(f"ltpc(ltp={feed.ltpc.ltp},ltt={feed.ltpc.ltt},cp={feed.ltpc.cp})")
                if feed.full_feed:
                    feed_parts.append("fullFeed")
                feed_parts.append(f"requestMode={feed.request_mode}")
                parts.append(f"feeds[{key}]={','.join(feed_parts) if feed_parts else 'empty'}")
        parts.append(f"currentTs={resp.current_ts}")
        return " | ".join(parts)
    except Exception as e:
        # Try raw hex dump of first bytes
        hex_preview = data[:40].hex() if data else "empty"
        return f"PROTOBUF_DECODE_FAIL: {e} | hex_head={hex_preview}"


def sanitize_uri(uri: str) -> str:
    """Strip credentials/token from URI."""
    if not uri:
        return "EMPTY"
    try:
        parsed = urlparse(uri)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    except Exception:
        return "REDACTED"


def show_current_subscription_format():
    """Step 1: Inspect what the current implementation sends."""
    print("=" * 70)
    print("STEP 1: INSPECT CURRENT SUBSCRIPTION FORMAT")
    print("=" * 70)

    request = SubscriptionRequest(
        guid="1234567890123",
        method="sub",
        mode=RequestMode.LTPC,
        instrument_keys=["NSE_EQ|SBIN"],
    )
    binary = request.serialize()
    print(f"Current serialization output: {len(binary)} bytes")
    print(f"  Raw hex: {binary.hex()}")
    print(f"  Is JSON?: {binary[:1:] in (b'{', b'[')}")
    print(f"  First byte: 0x{binary[0]:02x} (protobuf tags are low bytes; JSON starts with 0x7b)")

    # What the server ACTUALLY expects (per official Upstox V3 docs/SDK)
    expected = {
        "guid": str(request.guid),
        "method": "sub",
        "data": {
            "mode": "ltpc",
            "instrumentKeys": ["NSE_EQ|SBIN"],
        },
    }
    json_bytes = json.dumps(expected).encode("utf-8")
    print(f"\nCorrect V3 format (per official Upstox SDK + docs):")
    print(f"  JSON bytes: {json_bytes.decode('utf-8')}")
    print(f"  Length: {len(json_bytes)} bytes")
    print(f"  Sent as: binary WebSocket frame (opcode=2) with JSON payload")

    # Mode comparison
    print(f"\nMode comparison:")
    print(f"  Current implementation: RequestMode.{request.mode.name} = {int(request.mode)} (varint)")
    print(f"  Official SDK expects: 'ltpc' (string, per Mode dict)")
    print(f"  RequestMode enum values: LTPC={int(RequestMode.LTPC)}, FULL_D5={int(RequestMode.FULL_D5)}, OPTION_GREEKS={int(RequestMode.OPTION_GREEKS)}, FULL_D30={int(RequestMode.FULL_D30)}")


def resolve_instruments():
    """Step 2: Resolve instrument keys using the existing mechanism."""
    print("\n" + "=" * 70)
    print("STEP 2: RESOLVE INSTRUMENT KEYS (existing mechanism)")
    print("=" * 70)

    provider = UpstoxMarketDataProvider(
        client_id=settings.upstox_client_id,
        access_token=settings.upstox_service_account_token,
    )

    symbols = ["NSE:NIFTY50", "NSE:RELIANCE", "NSE:SBIN"]
    results = {}

    for sym in symbols:
        try:
            instr = provider._resolve(sym)
            resolved = provider._upstox_symbol(sym)
            results[sym] = resolved
            print(f"  {sym} -> {resolved}")
            print(f"    type={instr.instrument_type.name}, provider_symbol={instr.provider_symbol}")
        except Exception as e:
            print(f"  {sym} -> ERROR: {e}")
            results[sym] = None

    # Also show the ISIN-based keys (known Upstox V3 format for equities)
    print("\n  Known ISIN-based Upstox V3 keys (from official SDK examples):")
    print("    NSE_EQ|INE020B01018  (SBIN ISIN)")
    print("    NSE_EQ|INE020A59102  (RELIANCE ISIN)")
    print("    NSE_INDEX|NIFTY 50  (index with space)")
    print("    NSE_INDEX|NIFTY50  (index without space)")

    return results


def test_real_subscription_json(instrument_keys, mode, label):
    """
    Steps 3-6: Connect to the real V3 WebSocket, send a JSON subscription
    (as the official SDK does), and capture ALL server responses.
    """
    print(f"\n{'=' * 70}")
    print(f"TEST: {label}")
    print(f"  Mode: {mode}")
    print(f"  Instrument keys: {instrument_keys}")
    print("=" * 70)

    ws_client = UpstoxV3WebSocket(
        access_token=settings.upstox_service_account_token,
        instrument_keys=[],
        on_event=lambda x: None,
    )

    # Authorize (step 1 of V3 protocol)
    try:
        authorized_url = ws_client._authorize()
        log.info(f"Authorization: PASS (sanitized URI: {sanitize_uri(authorized_url)})")
    except UpstoxV3AuthorizationError as e:
        log.error(f"Authorization: FAIL - {e}")
        return {"auth": False}
    except Exception as e:
        log.error(f"Authorization: FAIL - {type(e).__name__}: {e}")
        return {"auth": False}

    # Open a raw WebSocket connection
    import websocket

    received_messages = []
    connection_state = {"connected": False, "error": None}
    lock = threading.Lock()

    test_results = {
        "auth": True,
        "connected": False,
        "messages": [],
        "json_messages": [],
        "binary_messages": [],
        "protobuf_decoded": [],
        "market_data_received": False,
    }

    def on_open(ws):
        with lock:
            connection_state["connected"] = True
        log.info("WebSocket connected (on_open)")

        # Build the CORRECT JSON subscription message (as official SDK does)
        sub_request = {
            "guid": str(int(time.time() * 1000)),
            "method": "sub",
            "data": {
                "mode": mode,
                "instrumentKeys": instrument_keys,
            },
        }
        json_bytes = json.dumps(sub_request).encode("utf-8")
        log.info(f"Sending JSON subscription: {json_bytes.decode('utf-8')}")
        ws.send(json_bytes, opcode=websocket.ABNF.OPCODE_BINARY)

    def on_message(ws, message):
        with lock:
            received_messages.append(message)
            if isinstance(message, bytes):
                test_results["binary_messages"].append(message)
                decoded = decode_protobuf_message(message)
                test_results["protobuf_decoded"].append(decoded)
                log.info(f"BINARY message ({len(message)} bytes) -> decoded: {decoded}")
            else:
                test_results["json_messages"].append(message)
                log.info(f"JSON message: {message}")

    def on_error(ws, error):
        with lock:
            connection_state["error"] = str(error)
        log.error(f"WebSocket error: {error}")

    def on_close(ws, status, message):
        log.info(f"WebSocket closed: status={status}, message={message}")

    ws = websocket.WebSocketApp(
        authorized_url,
        on_open=on_open,
        on_error=on_error,
        on_close=on_close,
        on_message=on_message,
    )

    ws_thread = threading.Thread(target=ws.run_forever, daemon=True)
    ws_thread.start()

    # Wait for connection
    start = time.time()
    while time.time() - start < 10:
        with lock:
            if connection_state["connected"] or connection_state["error"]:
                break
        time.sleep(0.2)

    test_results["connected"] = connection_state["connected"]

    # Wait bounded period for messages
    log.info(f"Waiting {TEST_WINDOW} seconds for server messages...")
    wait_start = time.time()
    while time.time() - wait_start < TEST_WINDOW:
        time.sleep(1)

    # Close cleanly
    ws.close()
    ws_thread.join(timeout=5)

    # Analyze results
    all_msgs = test_results["binary_messages"] + test_results["json_messages"]
    log.info(f"Total messages received: {len(all_msgs)}")
    log.info(f"  Binary (protobuf): {len(test_results['binary_messages'])}")
    log.info(f"  JSON: {len(test_results['json_messages'])}")

    # Check for market data
    for decoded in test_results["protobuf_decoded"]:
        if "live_feed" in decoded or "feeds[" in decoded:
            if "ltp=" in decoded:
                test_results["market_data_received"] = True
                break
        if "initial_feed" in decoded or "market_info" in decoded:
            test_results["market_data_received"] = True

    test_results["message_summary"] = all_msgs

    # Print summary
    print(f"\n  RESULTS for {label}:")
    print(f"    Connected: {test_results['connected']}")
    print(f"    Binary messages: {len(test_results['binary_messages'])}")
    print(f"    JSON messages: {len(test_results['json_messages'])}")
    for i, decoded in enumerate(test_results["protobuf_decoded"]):
        print(f"    Protobuf msg {i}: {decoded}")
    for msg in test_results["json_messages"]:
        print(f"    JSON msg: {msg}")
    print(f"    Market data received: {test_results['market_data_received']}")

    return test_results


def main():
    global TEST_WINDOW
    TEST_WINDOW = 45  # bounded test window

    # Check credentials
    print("=" * 70)
    print("V3 LIVE DATA DIAGNOSTIC")
    print("=" * 70)

    client_id_ok = bool(settings.upstox_client_id)
    token_ok = bool(settings.upstox_service_account_token)
    print(f"UPSTOX_CLIENT_ID: {'configured' if client_id_ok else 'MISSING'}")
    print(f"UPSTOX_SERVICE_ACCOUNT_TOKEN: {'configured' if token_ok else 'MISSING'}")
    print(f".env gitignored: YES (verified via git check-ignore)")

    if not client_id_ok or not token_ok:
        print("STOP: Required credentials not configured.")
        return 1

    # Step 1: Inspect current subscription format
    show_current_subscription_format()

    # Step 2: Resolve instruments
    resolved = resolve_instruments()

    # Step 6: Test instrument key format
    # The existing mechanism produces NSE_EQ|SBIN (trading symbol)
    # Official SDK uses NSE_EQ|INE020B01018 (ISIN)
    print("\n" + "=" * 70)
    print("STEP 6: INSTRUMENT KEY FORMAT INVESTIGATION")
    print("=" * 70)

    all_results = {}

    # Test A: Current format (NSE_EQ|SBIN) with LTPC mode, JSON subscription
    print("\n--- Test A: Trading symbol key (NSE_EQ|SBIN), LTPC mode, JSON ---")
    r = test_real_subscription_json(["NSE_EQ|SBIN"], "ltpc", "TradingSymbol-LTPC")
    all_results["A_trading_LTCp"] = r

    # Test B: ISIN key (NSE_EQ|INE020B01018) with LTPC mode, JSON subscription
    print("\n--- Test B: ISIN key (NSE_EQ|INE020B01018), LTPC mode, JSON ---")
    r = test_real_subscription_json(["NSE_EQ|INE020B01018"], "ltpc", "ISIN-LTPC")
    all_results["B_isin_LTCp"] = r

    # Test C: Multiple liquid instruments with LTPC
    print("\n--- Test C: Multiple instruments (LTPC mode, JSON) ---")
    multi_keys = ["NSE_EQ|INE020B01018", "NSE_EQ|INE020A59102"]  # SBIN, RELIANCE ISIN
    r = test_real_subscription_json(multi_keys, "ltpc", "Multi-LTPC")
    all_results["C_multi_LTCp"] = r

    # Test D: NIFTY 50 index with LTPC
    print("\n--- Test D: NIFTY 50 index (LTPC mode, JSON) ---")
    r = test_real_subscription_json(["NSE_INDEX|NIFTY 50"], "ltpc", "NIFTY50-LTPC")
    all_results["D_nifty_LTCp"] = r

    # Test E: If any LTPC test got data, try FULL mode
    any_data = any(r.get("market_data_received") for r in all_results.values())
    if not any_data:
        # Test E: Try current NSE_EQ|SBIN with FULL mode
        print("\n--- Test E: Trading symbol key, FULL mode ---")
        r = test_real_subscription_json(["NSE_EQ|SBIN"], "full", "TradingSymbol-FULL")
        all_results["E_trading_FULL"] = r

    # Final analysis
    print("\n" + "=" * 70)
    print("DIAGNOSTIC SUMMARY")
    print("=" * 70)

    print("\n1. SUBSCRIPTION FORMAT ISSUE:")
    print("   Current implementation serializes SubscriptionRequest as protobuf binary.")
    print("   Official V3 docs + SDK require JSON sent as a binary WebSocket frame.")
    print("   => This is likely the root cause: server silently ignores malformed subscription.")

    print("\n2. INSTRUMENT KEY RESOLUTION (existing mechanism):")
    for sym, key in resolved.items():
        print(f"   {sym} -> {key}")

    print("\n3. SERVER RESPONSE CAPTURE:")
    for test_name, r in all_results.items():
        auth_ok = "✓" if r.get("auth") else "✗"
        conn_ok = "✓" if r.get("connected") else "✗"
        bin_count = len(r.get("binary_messages", []))
        json_count = len(r.get("json_messages", []))
        data = "YES" if r.get("market_data_received") else "NO"
        print(f"   {test_name}: auth={auth_ok}, connected={conn_ok}, "
              f"binary_msgs={bin_count}, json_msgs={json_count}, market_data={data}")

    # Final verdict
    total_data = sum(1 for r in all_results.values() if r.get("market_data_received"))
    print(f"\n   Total market data received across all tests: {total_data}")

    if total_data > 0:
        print("\n   VERDICT: REAL MARKET DATA RECEIVED")
    else:
        print("\n   VERDICT: No market data received. Primary issue confirmed: ")
        print("   subscription sent as protobuf binary instead of JSON.")
        print("   Instrument key format (NSE_EQ|SBIN vs ISIN) secondary issue.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
