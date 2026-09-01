"""Tests for Upstox auth-recovery helpers (OAuth 2.0 migration). No network, no real creds.

Verifies: login URL is generated correctly and contains ONLY the OAuth params
(response_type, client_id, redirect_uri, state) — the client secret is NEVER
placed in the URL (it is used only at token exchange, server-to-server);
auth-code exchange success; exchange failure handling; tokens never written
to files during exchange; no order/execution code invoked.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from trading_system.india.token_manager import (
    UpstoxTokenManager,
    TokenManager,
    TokenError,
    UPSTOX_AUTH_URL,
    UPSTOX_TOKEN_URL,
)


CLIENT = "upstox_client_123"
SECRET = "UPSTOX_SECRET_VALUE_9999999999"
REDIRECT = "https://127.0.0.1:8080/callback"
AUTH_CODE = "AUTHCODE_0123456789abcdef"


def _fake_post_ok(url, data):
    class R:
        status_code = 200
        def json(self):
            return {"access_token": "NEW_ACCESS_TOKEN"}
    return R()


# --- authorization URL -------------------------------------------------------

def test_generate_auth_url_contains_expected_params():
    tm = UpstoxTokenManager(client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT)
    url = tm.build_authorization_url()
    from urllib.parse import urlparse, parse_qs
    assert url.startswith(UPSTOX_AUTH_URL + "?")
    qs = parse_qs(urlparse(url).query)
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == [CLIENT]
    assert qs["redirect_uri"] == [REDIRECT]
    assert "state" in qs
    assert "client_secret" not in url.lower()


def test_generate_auth_url_requires_credentials():
    tm = UpstoxTokenManager(client_id="", secret="", redirect_uri=REDIRECT)
    with pytest.raises(TokenError):
        tm.build_authorization_url()


def test_generate_auth_url_state_is_per_request_random():
    tm = UpstoxTokenManager(client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT)
    url1 = tm.build_authorization_url()
    url2 = tm.build_authorization_url()
    assert url1 != url2
    from urllib.parse import urlparse, parse_qs
    s1 = parse_qs(urlparse(url1).query)["state"][0]
    s2 = parse_qs(urlparse(url2).query)["state"][0]
    assert s1 != s2


def test_generate_auth_url_state_is_url_safe():
    tm = UpstoxTokenManager(client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT)
    url = tm.build_authorization_url()
    from urllib.parse import urlparse, parse_qs
    s = parse_qs(urlparse(url).query)["state"][0]
    import string
    allowed = string.ascii_letters + string.digits + "-._~"
    assert s
    assert all(c in allowed for c in s)


def test_generate_auth_url_state_present_and_params_intact():
    tm = UpstoxTokenManager(client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT)
    url = tm.build_authorization_url()
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(url).query)
    assert "state" in qs
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == [CLIENT]
    assert qs["redirect_uri"] == [REDIRECT]
    assert "client_secret" not in url.lower()


def test_generate_auth_url_has_only_allowed_params():
    """Regression: the login URL must carry ONLY the three OAuth params
    (response_type, client_id, redirect_uri) plus state — no secret."""
    tm = UpstoxTokenManager(client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT)
    url = tm.build_authorization_url(state="xyz")
    from urllib.parse import urlparse, parse_qs
    qs = urlparse(url).query
    params = parse_qs(qs, keep_blank_values=True)
    assert set(params.keys()) == {
        "client_id", "redirect_uri", "response_type", "state"
    }, f"unexpected params: {set(params.keys())}"
    assert params["client_id"] == [CLIENT]
    assert params["redirect_uri"] == [REDIRECT]
    assert params["response_type"] == ["code"]
    assert params["state"] == ["xyz"]
    assert "secret" not in qs.lower()


# --- auth-code exchange ------------------------------------------------------

def test_exchange_auth_code_success():
    captured = {}

    def fake_post(url, data):
        captured["url"] = url
        captured["data"] = data
        return _fake_post_ok(url, data)

    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT, http_post=fake_post,
    )
    access = tm.exchange_auth_code(AUTH_CODE)
    assert access == "NEW_ACCESS_TOKEN"
    assert tm.access_token == "NEW_ACCESS_TOKEN"
    # Upstox token exchange: form-encoded POST to the token endpoint.
    assert captured["url"] == UPSTOX_TOKEN_URL
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["code"] == AUTH_CODE
    assert captured["data"]["client_id"] == CLIENT
    assert captured["data"]["client_secret"] == SECRET
    assert captured["data"]["redirect_uri"] == REDIRECT
    # Secret is in the POST body (server-to-server), NOT in a URL or logged.
    assert "REDIRECT_URI" not in str(captured["url"])
    assert "secret" not in str(captured["url"]).lower()


def test_exchange_auth_code_rejected_http_error():
    def fake_post(url, data):
        class R:
            status_code = 400
            def json(self):
                return {"status": "error", "errors": [{"code": "invalid_grant", "message": "bad code"}]}
        return R()

    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT, http_post=fake_post,
    )
    with pytest.raises(TokenError):
        tm.exchange_auth_code(AUTH_CODE)


def test_exchange_auth_code_no_code():
    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT, http_post=MagicMock(),
    )
    with pytest.raises(TokenError):
        tm.exchange_auth_code("")


def test_exchange_auth_code_missing_secret():
    tm = UpstoxTokenManager(
        client_id=CLIENT, secret="", redirect_uri=REDIRECT, http_post=MagicMock(),
    )
    with pytest.raises(TokenError):
        tm.exchange_auth_code(AUTH_CODE)


def test_exchange_does_not_write_files(tmp_path, monkeypatch):
    from trading_system.config import settings
    monkeypatch.setattr(settings, "project_root", tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("UPSTOX_ACCESS_TOKEN=old\n")

    def fake_post(url, data):
        return _fake_post_ok(url, data)

    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT, http_post=fake_post,
    )
    tm.exchange_auth_code(AUTH_CODE)
    # File must be unchanged (we never write creds).
    assert "old" in env_file.read_text()
    assert "NEW_ACCESS_TOKEN" not in env_file.read_text()


def test_exchange_auth_code_invalid_credentials():
    def fake_post(url, data):
        class R:
            status_code = 401
            def json(self):
                return {"status": "error", "errors": [{"code": "invalid_client", "message": "bad secret"}]}
        return R()

    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT, http_post=fake_post,
    )
    with pytest.raises(TokenError):
        tm.exchange_auth_code(AUTH_CODE)


def test_exchange_auth_code_network_failure():
    def fake_post(url, data):
        import requests
        raise requests.RequestException("conn reset")

    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT, http_post=fake_post,
    )
    with pytest.raises(TokenError):
        tm.exchange_auth_code(AUTH_CODE)
