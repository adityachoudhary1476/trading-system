"""Focused V3 diagnostic: protobuf binary vs JSON subscription format.

Tests the EXACT two subscription formats side by side:
1. Protobuf binary (what the current implementation sends)
2. JSON as binary frame (what the official Upstox V3 docs/SDK require)
"""
import os
import sys
import json
import time
import threading
import logging
import websocket

os.chdir(r"C:\Users\Owner\OneDrive\Desktop\trading-system\backend")
sys.path.insert(0, r"C:\Users\Owner\OneDrive\Desktop\trading-system")
sys.path.insert(0, r"C:\Users\Owner\OneDrive\Desktop\trading-system\backend")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("v3focus")

from config import Settings
settings = Settings()

from src.trading_system.india.upstox_v3_ws import UpstoxV3WebSocket
from src.trading_system.india.upstox_v3_pb import (
    FeedResponse, FeedType, RequestMode, SubscriptionRequest,
)

# Known ISINs (verified by real market data in previous test)
SBIN_ISIN = "NSE_EQ|INE020B01018"        # SBIN
RELIANCE_ISIN = "NSE_EQ|INE002A59102"  # RELIANCE Industries

# Trading symbol (current implementation format)
SBIN_TRADINGSYM = "NSE_EQ|SBIN"


def decode_protobuf(data: bytes) -> str:
    try:
        resp = FeedResponse.deserialize(data)
        parts = []
        parts.append(f"type={resp.type.name}")
        if resp.feeds:
            for key, feed in resp.feeds.items():
                info = []
                if feed.ltpc:
                    info.append(f"ltp={feed.ltpc.ltp},ltt={feed.ltpc.ltt},cp={feed.ltpc.cp}")
                parts.append(f"feeds[{key}]={','.join(info) if info else 'empty'}")
        return " | ".join(parts)
    except Exception as e:
        hex_preview = data[:40].hex() if data else "empty"
        return f"DECODE_FAIL: {e} | hex={hex_preview}"


def run_test(label, send_data_fn, instrument_key, mode, window_seconds=30):
    """Connect, send subscription via send_data_fn, capture responses."""
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"  Key: {instrument_key} | Mode: {mode} | Window: {window_seconds}s")
    print(f"{'='*60}")

    ws_client = UpstoxV3WebSocket(
        access_token=settings.upstox_service_account_token,
        instrument_keys=[],
        on_event=lambda x: None,
    )

    results = {"messages": [], "connected": False, "error": None}
    lock = threading.Lock()

    def on_open(ws):
        with lock:
            results["connected"] = True
        log.info("WebSocket connected")
        try:
            sent = send_data_fn(ws, instrument_key, mode)
            log.info(f"Subscription sent: {sent}")
        except Exception as e:
            log.error(f"Send error: {e}")
            results["error"] = str(e)

    def on_message(ws, message):
        with lock:
            results["messages"].append(message)
        if isinstance(message, bytes):
            log.info(f"  BINARY ({len(message)}b): {decode_protobuf(message)}")
        else:
            log.info(f"  JSON: {message[:200]}")

    def on_error(ws, error):
        with lock:
            results["error"] = str(error)
        log.error(f"WebSocket error: {error}")

    def on_close(ws, status, msg):
        log.info(f"WebSocket closed: {status}")

    url = ws_client._authorize()
    log.info(f"Authorized (sanitized URI shown in prior steps)")

    ws = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_error=on_error,
        on_close=on_close,
        on_message=on_message,
    )

    thread = threading.Thread(target=ws.run_forever, daemon=True)
    thread.start()

    # Wait for connection
    start = time.time()
    while time.time() - start < 10:
        with lock:
            if results["connected"] or results["error"]:
                break
        time.sleep(0.2)

    # Collect messages
    collect_start = time.time()
    while time.time() - collect_start < window_seconds:
        time.sleep(1)

    ws.close()
    thread.join(timeout=5)

    # Summarize
    binary_msgs = [m for m in results["messages"] if isinstance(m, bytes)]
    json_msgs = [m for m in results["messages"] if isinstance(m, str)]
    has_real_data = any("ltp=" in decode_protobuf(m) and "ltp=0.0" not in decode_protobuf(m) for m in binary_msgs)

    print(f"\n  RESULTS: connected={results['connected']}, "
          f"binary_msgs={len(binary_msgs)}, json_msgs={len(json_msgs)}, "
          f"real_data={has_real_data}")

    return {"connected": results["connected"], "binary_msgs": len(binary_msgs),
            "json_msgs": len(json_msgs), "has_real_data": has_real_data}


def send_json_subscription(ws, instrument_key, mode):
    """Send JSON subscription as binary frame (correct V3 format)."""
    request = {
        "guid": str(int(time.time() * 1000)),
        "method": "sub",
        "data": {
            "mode": mode,
            "instrumentKeys": [instrument_key],
        },
    }
    data = json.dumps(request).encode("utf-8")
    ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)
    return f"JSON: {data.decode('utf-8')}"


def send_protobuf_subscription(ws, instrument_key, mode):
    """Send protobuf binary subscription (what the current implementation does)."""
    req = SubscriptionRequest(
        guid=str(int(time.time() * 1000)),
        method="sub",
        mode=RequestMode.LTPC,
        instrument_keys=[instrument_key],
    )
    data = req.serialize()
    ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)
    return f"PROTOBUF ({len(data)}b): {data.hex()}"


def main():
    print("=" * 60)
    print("V3 SUBSCRIPTION FORMAT COMPARISON TEST")
    print("=" * 60)

    # --- Test 1: Protobuf binary subscription (current implementation) ---
    r1 = run_test(
        "Protobuf binary subscription (CURRENT IMPLEMENTATION)",
        send_protobuf_subscription,
        SBIN_ISIN,
        "ltpc",
        window_seconds=30,
    )

    # --- Test 2: JSON subscription with ISIN key ---
    r2 = run_test(
        "JSON subscription + ISIN key (CORRECT V3 FORMAT)",
        send_json_subscription,
        SBIN_ISIN,
        "ltpc",
        window_seconds=30,
    )

    # --- Test 3: JSON subscription with trading symbol ---
    r3 = run_test(
        "JSON subscription + trading symbol (NSE_EQ|SBIN)",
        send_json_subscription,
        SBIN_TRADINGSYM,
        "ltpc",
        window_seconds=20,
    )

    # --- Test 4: JSON subscription with RELIANCE ISIN ---
    r4 = run_test(
        "JSON subscription + RELIANCE ISIN",
        send_json_subscription,
        RELIANCE_ISIN,
        "ltpc",
        window_seconds=20,
    )

    # --- Summary ---
    print("\n" + "=" * 60)
    print("COMPARISON SUMMARY")
    print("=" * 60)
    print(f"  Protobuf binary (current impl):  connected={r1['connected']}, "
          f"binary_msgs={r1['binary_msgs']}, real_data={r1['has_real_data']}")
    print(f"  JSON + ISIN (correct format):    connected={r2['connected']}, "
          f"binary_msgs={r2['binary_msgs']}, real_data={r2['has_real_data']}")
    print(f"  JSON + trading symbol:          connected={r3['connected']}, "
          f"binary_msgs={r3['binary_msgs']}, real_data={r3['has_real_data']}")
    print(f"  JSON + RELIANCE ISIN:           connected={r4['connected']}, "
          f"binary_msgs={r4['binary_msgs']}, real_data={r4['has_real_data']}")

    print("\n" + "=" * 60)
    print("DIAGNOSTIC CONCLUSION")
    print("=" * 60)

    if r1["binary_msgs"] == 0 and r2["has_real_data"]:
        print("  ROOT CAUSE CONFIRMED:")
        print("  The current implementation sends protobuf binary subscription.")
        print("  The Upstox V3 server expects JSON (sent as binary frame).")
        print("  Server silently ignores protobuf -> no market data received.")
        print("  JSON + ISIN format -> real market data immediately.")

    if r2["has_real_data"] and not r3["has_real_data"]:
        print("\n  INSTRUMENT KEY FORMAT CONFIRMED:")
        print("  ISIN-based keys (NSE_EQ|INE020B01018) -> real data")
        print("  Trading symbol keys (NSE_EQ|SBIN) -> server responds but ltp=0.0")
        print("  V3 feed requires ISIN for NSE equities, not trading symbol.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
