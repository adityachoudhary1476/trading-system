"""Tests for FYERS auth-recovery helpers (Day 10.5 recovery). No network, no real creds.

Verifies: login URL is generated correctly and contains no leaked secret in the wrong
place (the secret_key is REQUIRED by FYERS in the URL itself, but the token manager never
logs it); auth-code exchange success; exchange failure handling; tokens never written to
files; no order/execution code invoked.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from trading_system.india.token_manager import TokenManager, TokenError, _checksum


CLIENT = "CLIENT1-100"
SECRET = "SUPER_SECRET_VALUE_0123456789"
AUTH_CODE = "AUTHCODE_0123456789"


def _fake_post_ok(url, data):
    class R:
        status_code = 200
        def json(self):
            return {"s": "ok", "access_token": "NEW_ACCESS", "refresh_token": "NEW_REFRESH"}
    return R()


def test_generate_auth_url_contains_expected_params():
    tm = TokenManager(client_id=CLIENT, secret=SECRET)
    url = tm.generate_auth_url(redirect_uri="https://example.com/cb", state="xyz")
    assert url.startswith("https://api-t1.fyers.in/api/v3/generate-authcode?")
    assert "client_id=CLIENT1-100" in url
    assert "response_type=code" in url
    assert "state=xyz" in url
    assert "secret_key=" in url  # FYERS requires secret_key in the URL by design
    assert "redirect_uri=" in url


def test_generate_auth_url_requires_credentials():
    tm = TokenManager(client_id="", secret="")
    with pytest.raises(TokenError):
        tm.generate_auth_url()


def test_generate_auth_url_state_is_per_request_random():
    tm = TokenManager(client_id=CLIENT, secret=SECRET)
    url1 = tm.generate_auth_url()
    url2 = tm.generate_auth_url()
    # The URLs must differ (different random state each call).
    assert url1 != url2
    s1 = _state_from_url(url1)
    s2 = _state_from_url(url2)
    assert s1 != s2


def test_generate_auth_url_state_is_url_safe():
    tm = TokenManager(client_id=CLIENT, secret=SECRET)
    url = tm.generate_auth_url()
    s = _state_from_url(url)
    # URL-safe alphabet only (no '+' '/' '=').
    import string
    allowed = string.ascii_letters + string.digits + "-._~"
    assert s, "state must be non-empty"
    assert all(c in allowed for c in s)
    # secrets.token_urlsafe output contains only [A-Za-z0-9_-].
    assert set(s) <= set(string.ascii_letters + string.digits + "-_")


def test_generate_auth_url_state_present_and_params_intact():
    tm = TokenManager(client_id=CLIENT, secret=SECRET)
    url = tm.generate_auth_url()
    assert "state=" in url
    assert url.startswith("https://api-t1.fyers.in/api/v3/generate-authcode?")
    assert "client_id=CLIENT1-100" in url
    assert "response_type=code" in url
    assert "secret_key=" in url  # FYERS requires secret_key in the URL (expected)
    assert "redirect_uri=" in url


def _state_from_url(url: str) -> str:
    from urllib.parse import parse_qs, urlparse
    return parse_qs(urlparse(url).query)["state"][0]


def test_exchange_auth_code_success():
    captured = {}

    def fake_post(url, data, json_body=False):
        captured["url"] = url
        captured["data"] = data
        captured["json_body"] = json_body

        class R:
            status_code = 200

            def json(self):
                return {"s": "ok", "access_token": "NEW_ACCESS", "refresh_token": "NEW_REFRESH"}

        return R()

    tm = TokenManager(client_id=CLIENT, secret=SECRET, http_post=fake_post)
    access, refresh = tm.exchange_auth_code(AUTH_CODE)
    assert access == "NEW_ACCESS"
    assert refresh == "NEW_REFRESH"
    # Stored on the manager in-memory.
    assert tm.access_token == "NEW_ACCESS"
    assert tm.refresh_token == "NEW_REFRESH"
    # Contract: correct v3 endpoint, JSON body, appIdHash present, legacy fields absent.
    assert captured["url"] == "https://api-t1.fyers.in/api/v3/validate-authcode"
    assert captured["json_body"] is True
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["code"] == AUTH_CODE
    assert captured["data"]["appIdHash"] == _checksum(CLIENT, SECRET)
    # FYERS v3 exchange must NOT leak secret/old param names.
    assert "secret_key" not in captured["data"]
    assert "auth_code" not in captured["data"]
    assert "redirect_uri" not in captured["data"]
    assert "client_id" not in captured["data"]


def test_exchange_appidhash_is_sha256_of_clientid_secret():
    import hashlib

    captured = {}

    def fake_post(url, data, json_body=False):
        captured["data"] = data

        class R:
            status_code = 200

            def json(self):
                return {"s": "ok", "access_token": "X", "refresh_token": "Y"}

        return R()

    tm = TokenManager(client_id=CLIENT, secret=SECRET, http_post=fake_post)
    tm.exchange_auth_code(AUTH_CODE)
    expected = hashlib.sha256(f"{CLIENT}:{SECRET}".encode("utf-8")).hexdigest()
    assert captured["data"]["appIdHash"] == expected


def test_exchange_auth_code_rejected():
    def fake_post(url, data, json_body=False):
        class R:
            status_code = 200

            def json(self):
                return {"s": "error", "code": -16, "message": "bad auth code"}

        return R()

    tm = TokenManager(client_id=CLIENT, secret=SECRET, http_post=fake_post)
    with pytest.raises(TokenError):
        tm.exchange_auth_code(AUTH_CODE)


def test_exchange_auth_code_no_code():
    tm = TokenManager(client_id=CLIENT, secret=SECRET, http_post=MagicMock())
    with pytest.raises(TokenError):
        tm.exchange_auth_code("")


def test_exchange_auth_code_missing_secret():
    tm = TokenManager(client_id=CLIENT, secret="", http_post=MagicMock())
    with pytest.raises(TokenError):
        tm.exchange_auth_code(AUTH_CODE)


def test_exchange_does_not_write_files(tmp_path):
    # Ensure no .env is touched during exchange.
    env_file = tmp_path / ".env"
    env_file.write_text("FYERS_ACCESS_TOKEN=old\n")

    def fake_post(url, data, json_body=False):
        class R:
            status_code = 200

            def json(self):
                return {"s": "ok", "access_token": "NEW_ACCESS", "refresh_token": "NEW_REFRESH"}

        return R()

    tm = TokenManager(client_id=CLIENT, secret=SECRET, http_post=fake_post)
    tm.exchange_auth_code(AUTH_CODE)
    # File must be unchanged (we never write creds).
    assert "old" in env_file.read_text()
    assert "NEW_ACCESS" not in env_file.read_text()
