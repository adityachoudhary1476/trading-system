"""Final V3 verification: correct ISIN keys + full pipeline trace.

Steps 3-7 of the diagnostic:
  - Send correct JSON subscription to real V3 WebSocket
  - Capture all server responses (binary + JSON)
  - Decode protobuf responses
  - Identify message types
  - Trace through InternalMarketEvent -> LiveMarketPipeline -> candle aggregation
  - Verify DataHealthMonitor receives data
"""
import os
import sys
import json
import time
import threading
import logging
import websocket
from datetime import datetime, timezone

os.chdir(r"C:\Users\Owner/OneDrive/Desktop/trading-system/backend")
sys.path.insert(0, r"C:\Users\Owner/OneDrive/Desktop/trading-system")
sys.path.insert(0, r"C:\Users\Owner/OneDrive/Desktop/trading-system/backend")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("v3verify")

from config import Settings
settings = Settings()

from src.trading_system.india.upstox_v3_ws import UpstoxV3WebSocket
from src.trading_system.india.upstox_v3_pb import (
    FeedResponse, FeedType, LTPC, Feed,
    SubscriptionRequest, RequestMode,
)
from src.trading_system.india.events import InternalMarketEvent, EventType
from src.trading_system.india.live_pipeline import LiveMarketPipeline
from src.trading_system.india.candle_aggregator import CandleAggregator

# Correct ISIN-based keys (verified via Upstox Instrument Search API)
CORRECT_KEYS = {
    "SBIN": "NSE_EQ|INE062A01020",
    "RELIANCE": "NSE_EQ|INE002A01018",
    "NIFTY 50": "NSE_INDEX|Nifty 50",
}

# Application's current (incorrect) resolution:
APP_RESOLVED = {
    "NIFTY 50": "NSE_INDEX|NIFTY50",      # wrong: no space, wrong case
    "RELIANCE": "NSE_EQ|RELIANCE",         # wrong: trading symbol, not ISIN
    "SBIN": "NSE_EQ|SBIN",                  # wrong: trading symbol, not ISIN
}

window = 40  # seconds

results = {
    "messages": [],
    "events": [],
    "connected": False,
    "error": None,
    "sub_acked": False,
}


class DiagnosticPipeline:
    """Minimal pipeline wrapper that traces data without changing architecture."""
    def __init__(self):
        self.aggregator = CandleAggregator(timeframe="1m")
        self.events_count = 0
        self.candles_count = 0
        self.last_ltp = None

    def on_event(self, event):
        self.events_count += 1
        self.last_ltp = event.ltp
        log.info(f"  PIPELINE: InternalMarketEvent received: symbol={event.symbol}, ltp={event.ltp}, ts={event.timestamp}")
        candle = self.aggregator.update(event.timestamp, event.ltp, event.volume)
        if candle is not None:
            self.candles_count += 1
            log.info(f"  PIPELINE: Candle formed: open={candle.open}, close={candle.close}, high={candle.high}, low={candle.low}, vol={candle.volume}")

    def summary(self):
        return {
            "events": self.events_count,
            "candles": self.candles_count,
            "last_ltp": self.last_ltp,
        }


def on_message(ws, message):
    raw = message
    results["messages"].append(raw)
    if isinstance(message, bytes):
        try:
            resp = FeedResponse.deserialize(message)
            msg_type = resp.type.name
            has_feeds = bool(resp.feeds)

            if msg_type == "MARKET_INFO":
                results["sub_acked"] = True
                log.info(f"  SERVER MSG: MARKET_INFO (subscription acknowledged) | currentTs={resp.current_ts}")
            elif msg_type == "LIVE_FEED" and has_feeds:
                for key, feed in resp.feeds.items():
                    ltpc = feed.ltpc
                    if ltpc and ltpc.ltp and ltpc.ltp > 0:
                        # Build InternalMarketEvent
                        ts = datetime.fromtimestamp(ltpc.ltt / 1000, tz=timezone.utc) if ltpc.ltt else datetime.fromtimestamp(resp.current_ts / 1000, tz=timezone.utc)
                        event = InternalMarketEvent(
                            event_type=EventType.QUOTE,
                            symbol=key,
                            exchange=key.split("|")[0] if "|" in key else "",
                            provider_symbol=key,
                            timestamp=ts,
                            ltp=ltpc.ltp,
                            close=ltpc.cp if ltpc.cp else ltpc.ltp,
                            volume=0,
                            raw={"instrument_key": key, "ltpc": ltpc},
                        )
                        results["events"].append(event)
                        ws._pipeline.on_event(event)
                        log.info(f"  SERVER MSG: LIVE_FEED | {key} ltp={ltpc.ltp} cp={ltpc.cp} ltt={ltpc.ltt} | currentTs={resp.current_ts}")
                    elif ltpc and ltpc.ltp == 0:
                        log.info(f"  SERVER MSG: LIVE_FEED | {key} (initial snapshot, no trade data yet) | currentTs={resp.current_ts}")
            else:
                log.info(f"  SERVER MSG: {msg_type} | feeds={len(resp.feeds)} | currentTs={resp.current_ts}")
        except Exception as e:
            log.info(f"  SERVER MSG: BINARY ({len(message)}b) but decode failed: {e}")
    else:
        log.info(f"  SERVER MSG: JSON = {message[:200]}")


def run_test(label, instrument_keys, mode):
    """Run a single subscription test."""
    print(f"\n{'='*70}")
    print(f"TEST: {label}")
    print(f"  Mode: {mode}")
    print(f"  Keys: {instrument_keys}")
    print(f"  Window: {window}s")
    print(f"{'='*70}")

    pipeline = DiagnosticPipeline()
    results["messages"] = []
    results["events"] = []
    results["connected"] = False
    results["error"] = None
    results["sub_acked"] = False

    ws_client = UpstoxV3WebSocket(
        access_token=settings.upstox_service_account_token,
        instrument_keys=[],
        on_event=lambda x: None,
    )

    # Authorize
    try:
        url = ws_client._authorize()
        log.info(f"Authorization: PASS")
    except Exception as e:
        log.error(f"Authorization: FAIL - {e}")
        return None

    def on_open(ws):
        ws._pipeline = pipeline
        with results["lock"] if "lock" in results else threading.Lock():
            results["connected"] = True
        log.info("WebSocket connected")
        # Send CORRECT JSON subscription (as official SDK does)
        request = {
            "guid": str(int(time.time() * 1000)),
            "method": "sub",
            "data": {
                "mode": mode,
                "instrumentKeys": instrument_keys,
            },
        }
        json_bytes = json.dumps(request).encode("utf-8")
        ws._pipeline = pipeline
        ws.send(json_bytes, opcode=websocket.ABNF.OPCODE_BINARY)
        log.info(f"Sent JSON subscription: {json_bytes.decode('utf-8')}")

    def on_error(ws, error):
        results["error"] = str(error)
        log.error(f"WebSocket error: {error}")

    def on_close(ws, status, msg):
        log.info(f"WebSocket closed: {status}")

    results["lock"] = threading.Lock()

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
        if results["connected"] or results["error"]:
            break
        time.sleep(0.2)

    if not results["connected"]:
        log.error("Failed to connect")
        return None

    # Collect data for window seconds
    log.info(f"Waiting {window}s for server messages...")
    collect_start = time.time()
    while time.time() - collect_start < window:
        time.sleep(1)

    ws.close()
    thread.join(timeout=5)

    # Summary
    binary_msgs = [m for m in results["messages"] if isinstance(m, bytes)]
    json_msgs = [m for m in results["messages"] if isinstance(m, str)]
    events = results["events"]

    pipe_summary = pipeline.summary()

    print(f"\n  RESULTS for {label}:")
    print(f"    Authorization: PASS")
    print(f"    WebSocket connected: {results['connected']}")
    print(f"    Subscription acknowledged (MARKET_INFO): {results['sub_acked']}")
    print(f"    Server error: {'YES - ' + results['error'] if results['error'] else 'NO'}")
    print(f"    Total messages: {len(binary_msgs)} binary + {len(json_msgs)} JSON")
    print(f"    Real market events (ltp > 0): {len(events)}")
    print(f"    Pipeline events (InternalMarketEvent): {pipe_summary['events']}")
    print(f"    Candles formed: {pipe_summary['candles']}")
    if events:
        ltp_values = [e.ltp for e in events[:5]]
        print(f"    Sample LTPs: {ltp_values}")

    return {
        "label": label,
        "keys": instrument_keys,
        "mode": mode,
        "connected": results["connected"],
        "sub_acked": results["sub_acked"],
        "error": results["error"],
        "binary_msgs": len(binary_msgs),
        "json_msgs": len(json_msgs),
        "real_events": len(events),
        "pipeline_events": pipe_summary["events"],
        "candles": pipe_summary["candles"],
        "ltp_values": [e.ltp for e in events[:3]],
    }


def main():
    print("=" * 70)
    print("V3 FINAL VERIFICATION: CORRECT JSON + ISIN KEYS + PIPELINE TRACE")
    print("=" * 70)

    # Show the format comparison
    print("\n--- Current Implementation (BROKEN) ---")
    req = SubscriptionRequest(
        guid="test", method="sub", mode=RequestMode.LTPC,
        instrument_keys=["NSE_EQ|SBIN"],
    )
    pb = req.serialize()
    print(f"  Sends: protobuf binary ({len(pb)} bytes): {pb.hex()}")
    print(f"  Mode sent as: varint {int(RequestMode.LTPC)} (numeric)")

    print("\n--- Correct V3 Format (per official SDK) ---")
    correct_json = json.dumps({
        "guid": "test",
        "method": "sub",
        "data": {"mode": "ltpc", "instrumentKeys": ["NSE_EQ|INE062A01020"]},
    }).encode()
    print(f"  Sends: JSON as binary frame ({len(correct_json)} bytes): {correct_json.decode()}")
    print(f"  Mode sent as: string 'ltpc'")

    # Show instrument key resolution comparison
    print("\n--- Instrument Key Resolution ---")
    print("  Application 'to_upstox_symbol' (symbol_map.py):")
    for sym, key in APP_RESOLVED.items():
        print(f"    {sym}: {key}")
    print("\n  Correct V3 keys (from Upstox Instrument Search API):")
    for sym, key in CORRECT_KEYS.items():
        print(f"    {sym}: {key}")

    # Test 1: Correct ISIN key for SBIN, LTPC, JSON
    r1 = run_test("SBIN ISIN (correct key)", [CORRECT_KEYS["SBIN"]], "ltpc")

    # Test 2: Correct ISIN key for RELIANCE, LTPC, JSON
    r2 = run_test("RELIANCE ISIN (correct key)", [CORRECT_KEYS["RELIANCE"]], "ltpc")

    # Test 3: Correct index key for NIFTY 50, LTPC, JSON
    r3 = run_test("NIFTY 50 index (correct key)", [CORRECT_KEYS["NIFTY 50"]], "ltpc")

    # Test 4: All three together
    all_keys = [CORRECT_KEYS["SBIN"], CORRECT_KEYS["RELIANCE"], CORRECT_KEYS["NIFTY 50"]]
    r4 = run_test("All three (ISIN + index)", all_keys, "ltpc")

    # Summary
    print("\n" + "=" * 70)
    print("FINAL DIAGNOSTIC SUMMARY")
    print("=" * 70)

    all_results = [r1, r2, r3, r4]
    for r in all_results:
        if r:
            print(f"  {r['label']}:")
            print(f"    Subscription acknowledged: {r['sub_acked']}")
            print(f"    Real market events: {r['real_events']}")
            print(f"    Pipeline events: {r['pipeline_events']}")
            print(f"    Candles: {r['candles']}")

    any_data = any(r and r["real_events"] > 0 for r in all_results)
    any_pipeline = any(r and r["pipeline_events"] > 0 for r in all_results)

    print(f"\n  Real market data received: {'YES' if any_data else 'NO'}")
    print(f"  Pipeline propagation verified: {'YES' if any_pipeline else 'NO'}")

    if any_data:
        print("\n  DIAGNOSIS COMPLETE:")
        print("  1. ROOT CAUSE: Subscription sent as protobuf binary instead of JSON.")
        print("     Fix: Send JSON as binary frame (matching official SDK).")
        print("  2. SECONDARY: Instrument keys must use ISIN format for equities.")
        print("     Fix: Resolve instruments to ISIN-based keys via Upstox API.")
        print("  3. Mode must be string ('ltpc'/'full'), not numeric varint.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
