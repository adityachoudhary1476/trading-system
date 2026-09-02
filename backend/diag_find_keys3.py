"""Debug V2 quote response structure."""
import requests, sys, os, json

os.chdir(r"C:\Users\Owner/OneDrive/Desktop/trading-system/backend")
sys.path.insert(0, r"C:\Users\Owner/OneDrive/Desktop/trading-system")
sys.path.insert(0, r"C:\Users\Owner/OneDrive/Desktop/trading-system/backend")

from config import Settings
s = Settings()
headers = {"Authorization": f"Bearer {s.upstox_service_account_token}"}

for key in ["NSE_EQ|INE020B01018", "NSE_EQ|INE002A59102"]:
    url = "https://api.upstox.com/v2/market-quote/quotes"
    resp = requests.get(url, headers=headers, params={"symbol": key}, timeout=15)
    data = resp.json()
    print(f"Key: {key}")
    print(f"  Full response: {json.dumps(data)[:600]}")
    print()
