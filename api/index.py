"""Vercel-hosted Upstox OAuth endpoints.

Routes:
  GET /api/upstox/auth
  GET /api/upstox/callback
  GET /api/upstox/status
  POST /api/upstox/disconnect

The Upstox client secret and access token never enter frontend JavaScript.
The callback exchanges the one-time authorization code server-side and stores
an encrypted access-token session in an HttpOnly cookie. This is suitable for
this single-user deployment; a multi-user product should replace the cookie
session with encrypted per-user server-side persistence.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from trading_system.india.token_manager import (
    UPSTOX_AUTH_URL,
    UPSTOX_PROFILE_URL,
    UPSTOX_TOKEN_URL,
    UpstoxTokenManager,
)

app = FastAPI(title="Trading System API", docs_url=None, redoc_url=None)

_SESSION_SECRET_ENV = "UPSTOX_SESSION_SECRET"
_STATE_TTL = 10 * 60
_TOKEN_COOKIE = "upstox_session"
_STATE_COOKIE = "upstox_oauth_state"


def _secret() -> str:
    value = os.getenv(_SESSION_SECRET_ENV, "").strip()
    if not value:
        raise RuntimeError("UPSTOX_SESSION_SECRET is not configured")
    return value


def _key() -> bytes:
    return hashlib.sha256(_secret().encode("utf-8")).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign_state(payload: dict) -> str:
    raw = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_secret().encode("utf-8"), raw.encode("ascii"), hashlib.sha256).digest()
    return f"{raw}.{_b64(sig)}"


def _verify_state(value: str) -> dict | None:
    try:
        raw, supplied = value.split(".", 1)
        expected = hmac.new(_secret().encode("utf-8"), raw.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(supplied), expected):
            return None
        payload = json.loads(_unb64(raw).decode("utf-8"))
        if not isinstance(payload, dict) or int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _encrypt_token(token: str) -> str:
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(_key()).encrypt(nonce, token.encode("utf-8"), None)
    return _b64(nonce + ciphertext)


def _decrypt_token(value: str) -> str | None:
    try:
        blob = _unb64(value)
        nonce, ciphertext = blob[:12], blob[12:]
        return AESGCM(_key()).decrypt(nonce, ciphertext, None).decode("utf-8")
    except Exception:
        return None


def _manager(token: str = "") -> UpstoxTokenManager:
    return UpstoxTokenManager(
        access_token=token,
        client_id=os.getenv("UPSTOX_CLIENT_ID", ""),
        client_secret=os.getenv("UPSTOX_CLIENT_SECRET", ""),
        redirect_uri=os.getenv("UPSTOX_REDIRECT_URI", ""),
    )


def _token_from_request(request: Request) -> str:
    return _decrypt_token(request.cookies.get(_TOKEN_COOKIE, "")) or ""


def _cookie_kwargs() -> dict:
    return {
        "httponly": True,
        "secure": os.getenv("VERCEL", "").lower() == "1" or os.getenv("VERCEL_ENV") == "production",
        "samesite": "lax",
        "path": "/",
    }


@app.get("/api/upstox/auth")
def upstox_auth(response: JSONResponse | None = None):
    manager = _manager()
    state = secrets.token_urlsafe(24)
    signed = _sign_state({"nonce": state, "exp": int(time.time()) + _STATE_TTL})
    url = manager.build_authorization_url(state=state)
    result = RedirectResponse(url=url, status_code=302)
    result.set_cookie(_STATE_COOKIE, signed, max_age=_STATE_TTL, **_cookie_kwargs())
    return result


@app.get("/api/upstox/callback")
def upstox_callback(request: Request):
    error = request.query_params.get("error")
    if error:
        return RedirectResponse(url="/?upstox=denied", status_code=303)

    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    signed_state = request.cookies.get(_STATE_COOKIE, "")
    payload = _verify_state(signed_state)
    if not code or not state or not payload or not hmac.compare_digest(state, str(payload.get("nonce", ""))):
        return JSONResponse({"error": "invalid_oauth_state"}, status_code=400)

    manager = _manager()
    try:
        token = manager.exchange_auth_code(code)
        verified = manager.verify_authentication()
    except Exception:
        return RedirectResponse(url="/?upstox=exchange_failed", status_code=303)

    if verified.status.value != "auth_ok":
        return RedirectResponse(url="/?upstox=verification_failed", status_code=303)

    result = RedirectResponse(url="/?upstox=connected", status_code=303)
    result.set_cookie(_TOKEN_COOKIE, _encrypt_token(token), max_age=24 * 60 * 60, **_cookie_kwargs())
    result.delete_cookie(_STATE_COOKIE, path="/")
    return result


@app.get("/api/upstox/status")
def upstox_status(request: Request):
    token = _token_from_request(request)
    if not token:
        # Local development may still use UPSTOX_ACCESS_TOKEN from .env.
        token = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    if not token:
        return {"connected": False, "status": "access_token_expired", "message": "Upstox is not connected."}

    try:
        state = _manager(token).verify_authentication()
    except Exception:
        return {"connected": False, "status": "auth_failed", "message": "Unable to verify Upstox connection."}
    return {
        "connected": state.status.value == "auth_ok",
        "status": state.status.value,
        "message": state.message,
    }


@app.post("/api/upstox/disconnect")
def upstox_disconnect():
    result = JSONResponse({"connected": False, "status": "disconnected"})
    result.delete_cookie(_TOKEN_COOKIE, path="/")
    return result


@app.get("/api/health")
def health():
    return {"ok": True}
