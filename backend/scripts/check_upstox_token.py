"""Pre-deploy check: verify the Upstox service account token is valid.

Run locally before `railway up` to fail fast on bad tokens:
    python backend/scripts/check_upstox_token.py

Or run on the Railway service to check the live env var:
    railway ssh --service finova-autonomous-scheduler "python3 -" < backend/scripts/check_upstox_token.py
"""
import os
import sys

import requests

token = os.environ.get("UPSTOX_SERVICE_ACCOUNT_TOKEN", "")
if not token or len(token) < 20:
    print("FAIL: UPSTOX_SERVICE_ACCOUNT_TOKEN is not set or too short (<20 chars)")
    sys.exit(1)

# Probe Upstox with a lightweight API call
try:
    resp = requests.get(
        "https://api.upstox.com/v3/user/profile",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=10,
    )
    if resp.status_code == 200:
        print("OK: Upstox token is valid (HTTP 200)")
        sys.exit(0)
    else:
        print(f"FAIL: Upstox token rejected (HTTP {resp.status_code})")
        print(f"   Response: {resp.text[:200]}")
        sys.exit(1)
except requests.exceptions.ConnectionError:
    print("WARN: Could not reach Upstox API (network issue) — skipping token check")
    sys.exit(0)
except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)
