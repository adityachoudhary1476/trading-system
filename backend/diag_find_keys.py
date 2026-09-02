"""Find correct ISIN keys via Upstox API."""
import requests, sys, os

os.chdir(r"C:\Users\Owner\OneDrive\Desktop\trading-system\backend")
sys.path.insert(0, r"C:\Users\Owner\OneDrive\Desktop\trading-system")
sys.path.insert(0, r"C:\Users\Owner\OneDrive\Desktop\trading-system\backend")

from config import Settings
s = Settings()
headers = {"Authorization": f"Bearer {s.upstox_service_account_token}"}

# Try the Upstox V3 instruments endpoint
# This returns a master CSV of all instruments
urls_to_try = [
    "https://api.upstox.com/v3/instruments?exchange=NSE&segment=EQ",
    "https://api.upstox.com/v3/instruments/?exchange=NSE&segment=EQ",
    "https://api.upstox.com/v2/instruments?exchange=NSE",
    "https://api.upstox.com/v2/instruments",
]

for url in urls_to_try:
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        print(f"URL: {url}")
        print(f"  Status: {resp.status_code}")
        ct = resp.headers.get("Content-Type", "unknown")
        print(f"  Content-Type: {ct}")
        if resp.status_code == 200 and len(resp.content) > 0:
            print(f"  Content length: {len(resp.content)}")
            if "csv" in ct or resp.text[0:50].find("name") >= 0 or resp.text[0:50].find("symbol") >= 0:
                content = resp.text
                # Search for RELIANCE and SBIN
                for sym in ["SBIN", "RELIANCE"]:
                    matches = [l for l in content.split("\n") if sym in l.upper()]
                    print(f"  Matches for {sym}: {len(matches)}")
                    for m in matches[:3]:
                        print(f"    {m[:300]}")
            else:
                print(f"  First 500 chars: {resp.text[:500]}")
        else:
            print(f"  Response: {resp.text[:300]}")
        break  # Stop if we got a successful response
    except Exception as e:
        print(f"URL: {url} -> Error: {e}")

# Also try direct lookup via quote endpoint
print("\n--- Try V3 quote endpoint ---")
for key in ["NSE_EQ|INE002A59102", "NSE_EQ|RELIANCE", "NSE_EQ|INE020B01018"]:
    url = "https://api.upstox.com/v3/market-quote/quotes"
    resp = requests.get(url, headers=headers, params={"symbol": key}, timeout=15)
    data = resp.json()
    print(f"  {key}: HTTP {resp.status_code}, status={data.get('status')}, errors={data.get('errors', data.get('error_message', ''))}")
