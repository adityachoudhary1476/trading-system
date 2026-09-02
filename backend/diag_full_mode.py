"""Quick FULL mode test with correct ISIN key."""
import os, sys, json, time, threading, logging
import websocket

os.chdir(r"C:\Users\Owner/OneDrive/Desktop/trading-system/backend")
sys.path.insert(0, r"C:\Users/OneDrive/Desktop/trading-system")
sys.path.insert(0, r"C:\Users/OneDrive/Desktop/trading-system/backend")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("fulltest")

from config import Settings
from src.trading_system.india.upstox_v3_ws import UpstoxV3WebSocket
from src.trading_system.india.upstox_v3_pb import FeedResponse

s = Settings()

ws_client = UpstoxV3WebSocket(
    access_token=s.upstox_service_account_token,
    instrument_keys=[],
    on_event=lambda x: None,
)
url = ws_client._authorize()
log.info("Authorized (URI sanitized)")

messages = []
results = {"connected": False}

def on_open(ws):
    results["connected"] = True
    req = {"guid": str(int(time.time()*1000)), "method": "sub", "data": {"mode": "full", "instrumentKeys": ["NSE_EQ|INE062A01020"]}}
    ws.send(json.dumps(req).encode("utf-8"), opcode=websocket.ABNF.OPCODE_BINARY)
    log.info("Sent FULL mode subscription for NSE_EQ|INE062A01020")

def on_message(ws, msg):
    messages.append(msg)
    if isinstance(msg, bytes):
        try:
            resp = FeedResponse.deserialize(msg)
            desc = f"type={resp.type.name}"
            if resp.feeds:
                for k, f in resp.feeds.items():
                    info = []
                    if f.ltpc:
                        info.append(f"ltpc.ltp={f.ltpc.ltp}")
                    if f.full_feed:
                        info.append("fullFeed(has data)")
                    desc += f" feeds[{k}]={','.join(info)}"
            log.info(f"  BINARY ({len(msg)}b): {desc}")
        except Exception as e:
            log.info(f"  BINARY ({len(msg)}b): decode error: {e}")
    else:
        log.info(f"  JSON: {msg[:200]}")

def on_error(ws, e):
    log.error(f"WS error: {e}")

def on_close(ws, status, msg):
    log.info(f"WS closed: {status}")

ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
t = threading.Thread(target=ws.run_forever, daemon=True)
t.start()
time.sleep(20)
ws.close()
t.join(timeout=5)
log.info(f"Total messages: {len(messages)}")
log.info(f"Connected: {results['connected']}")
