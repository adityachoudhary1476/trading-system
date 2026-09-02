"""Resolve correct Upstox V3 instrument keys using Instrument Search API."""
import requests, sys, os, json

os.chdir(r"C:\Users\Owner/OneDrive/Desktop/trading-system/backend")
sys.path.insert(0, r"C:\Users\Owner/OneDrive/Desktop/trading-system")
sys.path.insert(0, r"C:\Users\Owner/OneDrive/Desktop/trading-system/backend")

from config import Settings
s = Settings()
headers = {"Authorization": f"Bearer {s.upstox_service_account_token}", "Accept": "application/json"}

# Use Upstox V2 Instrument Search API to find correct ISIN-based keys
search_url = "https://api.upstox.com/v2/instruments/search"

for query in ["SBIN", "RELIANCE", "NIFTY 50", "NIFTY50"]:
    resp = requests.get(
        search_url,
        headers=headers,
        params={"query": query, "exchanges": "NSE", "segments": "EQ,INDEX", "records": 5},
        timeout=20,
    )
    data = resp.json()
    if data.get("status") == "success":
        results = data.get("data", [])
        print(f"Query '{query}': {len(results)} results")
        for r in results[:5]:
            print(f"  isin={r.get('isin')}")
            print(f"  instrument_key={r.get('instrument_key')}")
            print(f"  trading_symbol={r.get('trading_symbol')}")
            print(f"  name={r.get('name')}")
            print(f"  segment={r.get('segment')}")
            print()
    else:
        print(f"Query '{query}': status={data.get('status')}, errors={data.get('errors', data.get('error_message', ''))}")
        print()
