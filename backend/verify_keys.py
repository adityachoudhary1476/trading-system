"""Verify instrument keys via Upstox quote API."""
import os, sys
os.chdir(r"C:\Users\Owner\OneDrive\Desktop\trading-system\backend")
sys.path.insert(0, r"C:\Users\Owner\OneDrive\Desktop\trading-system")
sys.path.insert(0, r"C:\Users\Owner\OneDrive\Desktop\trading-system\backend")

from config import Settings
s = Settings()

import requests
headers = {"Authorization": f"Bearer {s.upstox_service_account_token}"}

keys = [
    "NSE_EQ|INE020B01018",   # SBIN ISIN
    "NSE_EQ|INE002A59102",   # RELIANCE ISIN (correct)
    "NSE_EQ|INE020A59102",   # wrong ISIN (what was tested)
    "NSE_EQ|SBIN",            # SBIN trading symbol
    "NSE_INDEX|NIFTY 50",     # NIFTY index with space
    "NSE_INDEX|NIFTY50",      # NIFTY index without space
]

for key in keys:
    url = f"https://api.upstox.com/v3/market-quote/quotes"
    resp = requests.get(url, headers=headers, params={"symbol": key}, timeout=15)
    data = resp.json()
    syms = data.get("data", {})
    quote = syms.get(key, {})
    lt = quote.get("last_price", "N/A")
    ts = quote.get("tradingsymbol", "N/A")
    err = data.get("error_message", "") if data.get("status") != "success" else ""
    errs = data.get("errors", [])
    err_detail = errs[0].get("message", "") if errs else ""
    print(f"{key}: status={data.get('status')}, last_price={lt}, tradingsymbol={ts}, error={err or err_detail}")
