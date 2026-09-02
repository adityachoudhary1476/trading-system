"""Final verification: correct RELIANCE ISIN + pipeline trace."""
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
log = logging.getLogger("v3final")

from config import Settings
settings = Settings()

from src.trading_system.india.upstox_v3_ws import UpstoxV3WebSocket
from src.trading_system.india.upstox_v3_pb import FeedResponse, FeedType, LTPC, Feed

RELIANCE_ISIN = "NSE_EQ|INE002A59102"  # Correct RELIANCE Industries ISIN

results = {"messages": [], "events": [], "connected": False}
lock = threading.Lock()

def on_message(ws, message):
    with lock:
        results["messages"].append(message)
    if isinstance(message, bytes):
        resp = FeedResponse.deserialize(message)
        msg_type = resp.type.name
        for key, feed in resp.feeds.items():
            if feed.ltpc and feed.ltpc.ltp and feed.ltpc.ltp > 0:
                event = ws._impl_ws._normalize(key, feed, resp.current_ts) if hasattr(ws, '_impl_ws') else None
                log.info(f"  REAL DATA: {key} ltp={feed.ltpc.ltp} cp={feed.ltpc.cp} ltt={feed.ltpc.ltt}")
                with lock:
                    results["events"].append({
                        "instrument_key": key,
                        "ltp": feed.ltpc.ltp,
                        "cp": feed.ltpc.cp,
                        "ltt": feed.ltpc.ltt,
                        "type": msg_type,
                        "current_ts": resp.current_ts,
                    })

def on_open(ws):
    with lock:
        results["connected"] = True
    log.info("WebSocket connected")
    request = {
        "guid": str(int(time.time() * 1000)),
        "method": "sub",
        "data": {"mode": "ltpc", "instrumentKeys": [RELIANCE_ISIN]},
    }
    data = json.dumps(request).encode("utf-8")
    ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)
    log.info(f"Sent JSON subscription for {RELIANCE_ISIN}")

def on_error(ws, error):
    log.error(f"WS error: {error}")

def on_close(ws, status, msg):
    log.info(f"WS closed: {status}")

ws_client = UpstoxV3WebSocket(
    access_token=settings.upstox_service_account_token,
    instrument_keys=[],
    on_event=lambda x: None,
)

url = ws_client._authorize()
log.info("Authorized (URI sanitized)")

ws = websocket.WebSocketApp(url, on_open=on_open, on_error=on_error, on_close=on_close, on_message=on_message)
thread = threading.Thread(target=ws.run_forever, daemon=True)
thread.start()

# Wait 30 seconds
log.info("Waiting 30 seconds for real market data...")
start = time.time()
while time.time() - start < 30:
    time.sleep(1)

ws.close()
thread.join(timeout=5)

print(f"\n{'='*60}")
print(f"RELIANCE ISIN ({RELIANCE_ISIN}) VERIFICATION")
print(f"{'='*60}")
print(f"Connected: {results['connected']}")
print(f"Total messages: {len(results['messages'])}")
print(f"Real market events: {len(results['events'])}")
for ev in results["events"][:5]:
    print(f"  {ev}")
if results["events"]:
    print(f"\nReal data confirmed for RELIANCE. LTP values: {[e['ltp'] for e in results['events'][:5]]}")
else:
    print(f"\nNo real data for RELIANCE with ISIN {RELIANCE_ISIN}")
    print("May be due to no trades during this window, or ISIN needs verification.")
