# Superseded authentication script — DO NOT USE

`fyers_auth.py` in this directory is **superseded** by the canonical FYERS
authentication implementation:

- `src/trading_system/india/token_manager.py` (`TokenManager`)
- CLI commands in `src/trading_system/__main__.py`: `auth-login`, `auth-exchange`, `auth-status`

## Why it was quarantined (Day 11 auth hardening)

This script diverged from the canonical flow in three ways that caused
configuration drift (a real contributor to `-437` / "not connected" confusion):

1. It reads a **different** environment variable (`FYERS_SECRET_KEY`) than the rest
   of the system (`FYERS_SECRET`).
2. It uses a **different** redirect URI (`http://127.0.0.1:5000/callback`) than the
   canonical `https://trade.fyers.in/api-login/redirect-uri/index.html` used by
   `generate_auth_url()` — so the login URL it builds will not match the app's
   registered redirect URI.
3. It **auto-opens a browser**, runs a **local callback server**, and **auto-writes
   `.env`**. The canonical design intentionally does NONE of these: the user logs in
   manually, pastes the `auth_code` into `auth-exchange`, and pastes the printed tokens
   into `.env` themselves. Browser automation and automatic secret-writing are out of
   scope and were explicitly disallowed.

Nothing in the repository imports or invokes this script. It is kept here only as a
historical reference, not as a working second authentication path.

## Canonical workflow

```
python -m trading_system auth-login     # prints a FRESH login URL to a temp file
# open the URL, sign in manually, copy the auth_code from the redirect
python -m trading_system auth-exchange  # paste the code; prints tokens + live connectivity
python -m trading_system auth-status    # proves the token is accepted by FYERS
```
