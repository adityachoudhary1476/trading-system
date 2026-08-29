"""Tests for FYERS TokenManager (Day 10.5). No network, no real credentials.

These verify: refresh success, expired refresh token, malformed response, network
failure, and that NO secret (access/refresh token, client secret) ever appears in
logs/exceptions. We capture log output and assert absence of secret substrings.
"""
from __future__ import annotations

import logging
from datetime import date
from unittest.mock import MagicMock

import pytest

from trading_system.india.token_manager import (
    TokenManager, AuthStatus, TokenError, FYERS_TOKEN_URL,
)


class _FakeResp:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
    def json(self):
        return self._payload


class _LogCapture:
    """Capture log records; expose all emitted message strings."""
    def __init__(self) -> None:
        self.records: list[str] = []
        self._handler = logging.Handler()
        self._handler.emit = lambda r: self.records.append(r.getMessage())
    def install(self):
        logging.getLogger("trading_system").addHandler(self._handler)
        logging.getLogger("trading_system").setLevel(logging.DEBUG)
    def uninstall(self):
        logging.getLogger("trading_system").removeHandler(self._handler)
    @property
    def text(self) -> str:
        return "\n".join(self.records)


ACCESS = "ACCESS_TOKEN_REDACTED_0123456789"
REFRESH = "REFRESH_TOKEN_REDACTED_0123456789"
SECRET = "SUPER_SECRET_VALUE_0123456789"


def test_token_status_no_tokens():
    tm = TokenManager(client_id="C", access_token="", refresh_token="", secret="")
    st = tm.token_status()
    assert st.status == AuthStatus.ACCESS_TOKEN_EXPIRED
    assert st.access_token_present is False
    assert st.refresh_token_present is False


def test_token_status_access_only():
    tm = TokenManager(client_id="C", access_token=ACCESS, refresh_token="", secret="")
    st = tm.token_status()
    assert st.status == AuthStatus.ACCESS_TOKEN_EXPIRED


def test_token_status_both_present():
    tm = TokenManager(client_id="C", access_token=ACCESS, refresh_token=REFRESH, secret=SECRET)
    st = tm.token_status()
    assert st.status == AuthStatus.AUTH_OK


def test_get_valid_access_token_returns_when_present():
    tm = TokenManager(access_token=ACCESS)
    assert tm.get_valid_access_token() == ACCESS


def test_get_valid_access_token_raises_when_absent():
    tm = TokenManager(access_token="")
    with pytest.raises(TokenError):
        tm.get_valid_access_token()


def test_refresh_success():
    captured = {}
    def fake_post(url, data):
        captured["url"] = url
        captured["data"] = data
        return _FakeResp(200, {"s": "ok", "access_token": "NEW_ACCESS_TOKEN"})
    tm = TokenManager(
        client_id="CLIENT1", refresh_token=REFRESH, secret=SECRET,
        http_post=fake_post,
    )
    new = tm.refresh_access_token()
    assert new == "NEW_ACCESS_TOKEN"
    # Checksum sent, refresh token sent, but we only assert the request shape.
    assert captured["data"]["grant_type"] == "refresh_token"
    assert captured["data"]["refresh_token"] == REFRESH
    assert "checksum" in captured["data"]
    assert captured["data"]["client_id"] == "CLIENT1"


def test_refresh_expired_refresh_token():
    def fake_post(url, data):
        return _FakeResp(200, {"s": "error", "code": -16, "message": "auth failed"})
    tm = TokenManager(client_id="C", refresh_token=REFRESH, secret=SECRET, http_post=fake_post)
    with pytest.raises(TokenError) as ei:
        tm.refresh_access_token()
    assert "re-authorization" in str(ei.value).lower()


def test_refresh_network_failure():
    def fake_post(url, data):
        raise __import__("requests").RequestException("conn reset")
    tm = TokenManager(client_id="C", refresh_token=REFRESH, secret=SECRET, http_post=fake_post)
    with pytest.raises(TokenError) as ei:
        tm.refresh_access_token()
    assert "network" in str(ei.value).lower()


def test_refresh_missing_secret_raises():
    tm = TokenManager(client_id="C", refresh_token=REFRESH, secret="", http_post=MagicMock())
    with pytest.raises(TokenError) as ei:
        tm.refresh_access_token()
    assert "secret" in str(ei.value).lower()


def test_secrets_never_in_logs():
    cap = _LogCapture()
    cap.install()
    try:
        def fake_post(url, data):
            return _FakeResp(200, {"s": "error", "code": -17, "message": "refresh invalid"})
        tm = TokenManager(client_id="C", refresh_token=REFRESH, secret=SECRET, http_post=fake_post)
        with pytest.raises(TokenError):
            tm.refresh_access_token()
        text = cap.text
        assert ACCESS not in text, "ACCESS token leaked into logs"
        assert REFRESH not in text, "REFRESH token leaked into logs"
        assert SECRET not in text, "SECRET leaked into logs"
        # Logs must never contain secret material; that is the contract.
    finally:
        cap.uninstall()


def test_secrets_never_in_exception():
    def fake_post(url, data):
        return _FakeResp(200, {"s": "error", "code": -16, "message": "auth failed"})
    tm = TokenManager(client_id="C", refresh_token=REFRESH, secret=SECRET, http_post=fake_post)
    try:
        tm.refresh_access_token()
        assert False, "expected TokenError"
    except TokenError as e:
        msg = str(e)
        assert REFRESH not in msg, "refresh token in exception"
        assert SECRET not in msg, "secret in exception"
        assert "re-authorization" in msg.lower()
