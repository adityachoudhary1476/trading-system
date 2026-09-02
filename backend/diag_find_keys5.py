"""Find correct ISINs using NSE_EQ|SYMBOL format via V2 API."""
import requests, sys, os, json

os.chdir(r"C:\Users\Owner/OneDrive/Desktop/trading-system/backend")
sys.path.insert(0, r"C:\Users\Owner/OneDrive/Desktop/trading-system")
sys.path.insert(0, r"C:\Users\Owner/OneDrive/Desktop/trading-system/backend")

from config import Settings
s = Settings()
headers = {"Authorization": f"Bearer {s.upstox_service_account_token}"}

# V2 quote API - try trading symbol format
for key in ["NSE_EQ|SBIN", "NSE_EQ|RELIANCE", "NSE_EQ|INE020B01018"]:
    url = "https://api.upstox.com/v2/market-quote/quotes"
    resp = requests.get(url, headers=headers, params={"symbol": key}, timeout=15)
    data = resp.json()
    syms = data.get("data", {})
    if syms:
        for k, v in syms.items():
            print(f"  Input: {key} -> Resolved: {k}")
            print(f"    instrument_key: {v.get('instrument_key')}")
            print(f"    tradingsymbol: {v.get('tradingsymbol')}")
            print(f"    last_price: {v.get('last_price')}")
            print(f"    isin: {v.get('isin', 'N/A')}")
            print()
    else:
        print(f"  {key}: status={data.get('status')}, errors={data.get('errors', data.get('error_message'))}")
        print()
