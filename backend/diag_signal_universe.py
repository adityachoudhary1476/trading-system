"""Verify Upstox Instrument Search API returns expected ISINs."""
import os, sys, json
os.chdir(r"C:\Users\Owner/OneDrive/Desktop/trading-system/backend")
sys.path.insert(0, r"C:\Users\OneDrive/Desktop/trading-system")
sys.path.insert(0, r"C:\Users\OneDrive/Desktop/trading-system/backend")

from config import Settings
s = Settings()
headers = {"Authorization": f"Bearer {s.upstox_service_account_token}", "Accept": "application/json"}

queries = ["SBIN", "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "KOTAKBANK", "AXISBANK", "LT", "WIPRO"]
for q in queries:
    resp = requests.get(
        "https://api.upstox.com/v2/instruments/search",
        headers=headers,
        params={"query": q, "exchanges": "NSE", "segments": "EQ", "records": 3},
        timeout=10,
    ) if False else __import__("requests").get(
        "https://api.upstox.com/v2/instruments/search",
        headers=headers,
        params={"query": q, "exchanges": "NSE", "segments": "EQ", "records": 3},
        timeout=10,
    )
    data = resp.json()
    items = data.get("data", [])
    if items:
        r = items[0]
        print(f"{q:12} -> {r.get('instrument_key')} (isin={r.get('isin')}, ts={r.get('trading_symbol')})")
    else:
        print(f"{q:12} -> NOT FOUND")

import requests
