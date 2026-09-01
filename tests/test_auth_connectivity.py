"""Tests for Upstox live-connectivity probe (OAuth 2.0 migration).

No network, no real credentials. HTTP is injected via fakes. The probe uses a
separate GET callable (``http_get``) so it can be mocked independently from
the token-exchange POST.

Proves:
* verify_authentication() maps a real Upstox success → CONNECTED (AUTH_OK)
* rejected/expired token → ACCESS_TOKEN_EXPIRED (distinct from "no token")
* Upstox non-auth error → AUTH_FAILED
* network failure → NETWORK_ERROR (handled cleanly, no crash)
* auth_status_callback (B4) is invoked with the resolved status
* a successful exchange is NOT undone by a later failed probe (token retained in memory)
"""
from __future__ import annotations

import pytest

from trading_system import __main__ as cli
from trading_system.india.token_manager import (
    TokenManager,
    UpstoxTokenManager,
    AuthStatus,
    TokenError,
    TokenState,
    UPSTOX_PROFILE_URL,
    UPSTOX_TOKEN_URL,
)


CLIENT = "CLIENT1-100"
SECRET = "SUPER_SECRET_VALUE_0123456789"
ACCESS = "FAKE_ACCESS_TOKEN_0123456789"
CODE = "AUTHCODE_0123456789"
REDIRECT = "https://127.0.0.1:8080/callback"


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


# --- token-presence vs connectivity separation --------------------------------

def test_verify_no_token_no_network():
    """No access token -> presence-only state, no GET attempted."""
    calls = []

    def fake_get(url, headers):
        calls.append((url, headers))
        return _FakeResp(200, {"status": "success"})

    tm = UpstoxTokenManager(client_id=CLIENT, access_token="", secret=SECRET,
                            redirect_uri=REDIRECT, http_get=fake_get)
    st = tm.verify_authentication()
    assert st.status == AuthStatus.ACCESS_TOKEN_EXPIRED
    assert calls == []  # never probes when there is nothing to probe


def test_verify_connected():
    def fake_get(url, headers):
        assert url == UPSTOX_PROFILE_URL
        # Authorization header must be Bearer (never logged/printed).
        assert headers.get("Authorization") == f"Bearer {ACCESS}"
        return _FakeResp(200, {"status": "success", "data": {"user_id": "u1"}})

    tm = UpstoxTokenManager(client_id=CLIENT, access_token=ACCESS, secret=SECRET,
                            redirect_uri=REDIRECT, http_get=fake_get)
    st = tm.verify_authentication()
    assert st.status == AuthStatus.AUTH_OK
    assert st.access_token_present is True


def test_verify_token_rejected_expired_401():
    def fake_get(url, headers):
        return _FakeResp(401, {"status": "error", "errors": [{"code": "invalid_token", "message": "expired"}]})

    tm = UpstoxTokenManager(client_id=CLIENT, access_token=ACCESS, secret=SECRET,
                            redirect_uri=REDIRECT, http_get=fake_get)
    st = tm.verify_authentication()
    # Token present but Upstox refused it: explicit ACCESS_TOKEN_EXPIRED.
    assert st.status == AuthStatus.ACCESS_TOKEN_EXPIRED


def test_verify_token_rejected_expired_403():
    def fake_get(url, headers):
        return _FakeResp(403, {"status": "error", "errors": [{"code": "forbidden", "message": "nope"}]})

    tm = UpstoxTokenManager(client_id=CLIENT, access_token=ACCESS, secret=SECRET,
                            redirect_uri=REDIRECT, http_get=fake_get)
    st = tm.verify_authentication()
    assert st.status == AuthStatus.ACCESS_TOKEN_EXPIRED


def test_verify_other_error():
    def fake_get(url, headers):
        return _FakeResp(500, {"status": "error", "errors": [{"code": "internal", "message": "server error"}]})

    tm = UpstoxTokenManager(client_id=CLIENT, access_token=ACCESS, secret=SECRET,
                            redirect_uri=REDIRECT, http_get=fake_get)
    st = tm.verify_authentication()
    assert st.status == AuthStatus.AUTH_FAILED


def test_verify_network_error_is_clean():
    def fake_get(url, headers):
        raise __import__("requests").RequestException("conn reset")

    tm = UpstoxTokenManager(client_id=CLIENT, access_token=ACCESS, secret=SECRET,
                            redirect_uri=REDIRECT, http_get=fake_get)
    st = tm.verify_authentication()
    assert st.status == AuthStatus.NETWORK_ERROR
    assert st.access_token_present is True


def test_verify_invokes_auth_status_callback():
    seen = {}

    def cb(status_str):
        seen["status"] = status_str

    def fake_get(url, headers):
        return _FakeResp(200, {"status": "success"})

    tm = UpstoxTokenManager(client_id=CLIENT, access_token=ACCESS, secret=SECRET,
                            redirect_uri=REDIRECT, http_get=fake_get,
                            auth_status_callback=cb)
    st = tm.verify_authentication()
    assert st.status == AuthStatus.AUTH_OK
    assert seen.get("status") == AuthStatus.AUTH_OK.value


# --- auth-code rejection (exchange) ------------------------------------------

def test_exchange_auth_code_http_error_does_not_store():
    def fake_post(url, data):
        return _FakeResp(400, {"status": "error", "errors": [{"code": "invalid_grant", "message": "bad code"}]})

    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT, http_post=fake_post,
    )
    with pytest.raises(TokenError):
        tm.exchange_auth_code(CODE)
    # Nothing stored on rejection.
    assert not tm.has_access_token()


def test_exchange_auth_code_network_error_does_not_store():
    def fake_post(url, data):
        raise __import__("requests").RequestException("conn reset")

    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT, http_post=fake_post,
    )
    with pytest.raises(TokenError):
        tm.exchange_auth_code(CODE)
    assert not tm.has_access_token()


def test_exchange_success_then_probe_failure_keeps_token():
    """A failed probe must not wipe the freshly exchanged token in memory."""
    def fake_post(url, data):
        return _FakeResp(200, {"access_token": ACCESS})

    def fake_get(url, headers):
        return _FakeResp(401, {"status": "error", "errors": [{"code": "invalid_token", "message": "expired"}]})

    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT,
        http_post=fake_post, http_get=fake_get,
    )
    tm.exchange_auth_code(CODE)
    assert tm.access_token == ACCESS
    st = tm.verify_authentication()
    # Probe failure reports connectivity problem but the token remains stored.
    assert st.status == AuthStatus.ACCESS_TOKEN_EXPIRED
    assert tm.access_token == ACCESS
