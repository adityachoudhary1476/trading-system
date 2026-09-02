"""Try V2 quote endpoint to find ISINs."""
import requests, sys, os

os.chdir(r"C:\Users\Owner\OneDrive\Desktop\trading-system\backend")
sys.path.insert(0, r"C:\Users\Owner/OneDrive/Desktop/trading-system")
sys.path.insert(0, r"C:\Users\Owner/OneDrive/Desktop/trading-system/backend" if False else r"C:\Users\Owner/OneDrive/Desktop/trading-system/backend")

from config import Settings
s = Settings()
headers = {"Authorization": f"Bearer {s.upstox_service_account_token}"}

# Try V2 quote endpoint
for key in ["NSE_EQ|INE002A59102", "NSE_EQ|INE020B01018"]:
    url = "https://api.upstox.com/v2/market-quote/quotes"
    resp = requests.get(url, headers=headers, params={"symbol": key}, timeout=15)
    data = resp.json()
    syms = data.get("data", {})
    quote = syms.get(key, {})
    print(f"V2 {key}: status={data.get('status')}, last_price={quote.get('last_price','?')}, tradingsymbol={quote.get('tradingsymbol','?')}, instrument_key={quote.get('instrument_key','?')}")

# Try V2 LTP endpoint
for key in ["NSE_EQ|INE002A59102", "NSE_EQ|INE020B01018"]:
    url = "https://api.upstox.com/v2/market-quote/quotes"
    resp = requests.get(url, headers=headers, params={"symbol": key}, timeout=15)
    data = resp.json()
    print(f"V2 {key}: {json.dumps(data)[:300]}")

import json
