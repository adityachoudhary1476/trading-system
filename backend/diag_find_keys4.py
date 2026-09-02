"""Find correct ISINs for SBIN and RELIANCE via V2 API."""
import requests, sys, os, json

os.chdir(r"C:\Users\Owner/OneDrive/Desktop/trading-system/backend")
sys.path.insert(0, r"C:\Users\Owner/OneDrive/Desktop/trading-system")
sys.path.insert(0, r"C:\Users\Owner/OneDrive/Desktop/trading-system/backend")

from config import Settings
s = Settings()
headers = {"Authorization": f"Bearer {s.upstox_service_account_token}"}

# Try V2 quote with trading symbol format
for key in ["NSE:SBIN", "NSE:RELIANCE"]:
    url = "https://api.upstox.com/v2/market-quote/quotes"
    resp = requests.get(url, headers=headers, params={"symbol": key}, timeout=15)
    data = resp.json()
    syms = data.get("data", {})
    if syms:
        for k, v in syms.items():
            print(f"  {k}:")
            print(f"    instrument_key: {v.get('instrument_key')}")
            print(f"    tradingsymbol: {v.get('tradingsymbol')}")
            print(f"    last_price: {v.get('last_price')}")
            print(f"    isin: {v.get('isin', 'N/A')}")
            print()
    else:
        print(f"  {key}: empty data. Full: {json.dumps(data)[:400]}")
        print()

# Try V2 LTP endpoint
print("--- V2 LTP endpoint ---")
for key in ["NSE:SBIN", "NSE:RELIANCE"]:
    url = "https://api.upstox.com/v2/market-quote/ltp"
    resp = requests.get(url, headers=headers, params={"symbol": key}, timeout=15)
    data = resp.json()
    print(f"  {key}: {json.dumps(data)[:400]}")
    print()
