"""Tests for Upstox TokenManager (OAuth 2.0 migration). No network, no real creds.

These verify: authorization-URL generation, auth-code exchange (form-encoded),
token-expiration at 3:30 AM IST, connectivity probe, and that NO secret
(access token, client secret, auth code) ever appears in logs/exceptions.

HTTP is injected via fake callables so the full parsing logic is exercised
without any network.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from trading_system.india.token_manager import (
    UpstoxTokenManager,
    TokenManager,
    AuthStatus,
    TokenError,
    TokenState,
    UPSTOX_AUTH_URL,
    UPSTOX_TOKEN_URL,
    UPSTOX_PROFILE_URL,
)


class _FakeResp:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _LogCapture:
    """Capture log records emitted by the trading_system logger."""

    def __init__(self) -> None:
        self.records: list[str] = []
        self._handler = logging.Handler()
        self._handler.emit = lambda r: self.records.append(r.getMessage())

    def install(self):
        root = logging.getLogger("trading_system")
        root.addHandler(self._handler)
        root.setLevel(logging.DEBUG)

    def uninstall(self):
        logging.getLogger("trading_system").removeHandler(self._handler)

    @property
    def text(self) -> str:
        return "\n".join(self.records)


CLIENT = "upstox_client_123"
SECRET = "UPSTOX_SECRET_VALUE_9999999999"
REDIRECT = "https://127.0.0.1:8080/callback"
ACCESS = "ACCESS_TOKEN_REDACTED_0123456789"
CODE = "AUTHCODE_0123456789abcdef"


# --------------------------------------------------------------------------- #
# Backward-compat alias
# --------------------------------------------------------------------------- #
def test_token_manager_alias_is_upstox():
    assert TokenManager is UpstoxTokenManager


# --------------------------------------------------------------------------- #
# token_status
# --------------------------------------------------------------------------- #
def test_token_status_no_tokens():
    tm = UpstoxTokenManager(client_id="C", access_token="", secret="", redirect_uri=REDIRECT)
    st = tm.token_status()
    assert st.status == AuthStatus.ACCESS_TOKEN_EXPIRED
    assert st.access_token_present is False


def test_token_status_valid_token():
    tm = UpstoxTokenManager(
        client_id=CLIENT, access_token=ACCESS, secret=SECRET, redirect_uri=REDIRECT,
    )
    # No obtained_at → not locally expired, so presence-based AUTH_OK.
    st = tm.token_status()
    assert st.status == AuthStatus.AUTH_OK
    assert st.access_token_present is True


def test_token_status_expired_by_time():
    from trading_system.india.token_manager import _UTC
    tm = UpstoxTokenManager(
        client_id=CLIENT, access_token=ACCESS, secret=SECRET, redirect_uri=REDIRECT,
    )
    # Set obtained_at to a time far in the past → token should be expired.
    tm._obtained_at = datetime(2020, 1, 1, tzinfo=_UTC)
    st = tm.token_status()
    assert st.status == AuthStatus.ACCESS_TOKEN_EXPIRED
    assert st.access_token_present is True


# --------------------------------------------------------------------------- #
# get_valid_access_token / is_authenticated
# --------------------------------------------------------------------------- #
def test_get_valid_access_token_returns_when_present():
    tm = UpstoxTokenManager(access_token=ACCESS)
    assert tm.get_valid_access_token() == ACCESS


def test_get_valid_access_token_raises_when_absent():
    tm = UpstoxTokenManager(access_token="")
    with pytest.raises(TokenError):
        tm.get_valid_access_token()


def test_is_authenticated_true_when_token_present():
    tm = UpstoxTokenManager(access_token=ACCESS)
    assert tm.is_authenticated() is True


def test_is_authenticated_false_when_no_token():
    tm = UpstoxTokenManager(access_token="")
    assert tm.is_authenticated() is False


# --------------------------------------------------------------------------- #
# build_authorization_url / generate_auth_url
# --------------------------------------------------------------------------- #
def test_auth_url_correct_endpoint_and_params():
    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT,
    )
    url = tm.build_authorization_url()
    assert url.startswith(UPSTOX_AUTH_URL + "?")
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(url).query)
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == [CLIENT]
    assert qs["redirect_uri"] == [REDIRECT]
    assert "state" in qs
    # Secret must NOT appear in the URL.
    assert "secret" not in url.lower()
    assert "client_secret" not in url.lower()


def test_auth_url_alias_generate_auth_url():
    tm = UpstoxTokenManager(client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT)
    url = tm.generate_auth_url()
    assert url.startswith(UPSTOX_AUTH_URL)


def test_auth_url_requires_client_id():
    tm = UpstoxTokenManager(client_id="", secret=SECRET, redirect_uri=REDIRECT)
    with pytest.raises(TokenError):
        tm.build_authorization_url()


def test_auth_url_requires_redirect_uri():
    tm = UpstoxTokenManager(client_id=CLIENT, secret=SECRET, redirect_uri="")
    with pytest.raises(TokenError):
        tm.build_authorization_url()


def test_auth_url_state_is_per_request_random():
    tm = UpstoxTokenManager(client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT)
    url1 = tm.build_authorization_url()
    url2 = tm.build_authorization_url()
    assert url1 != url2
    from urllib.parse import urlparse, parse_qs
    s1 = parse_qs(urlparse(url1).query)["state"][0]
    s2 = parse_qs(urlparse(url2).query)["state"][0]
    assert s1 != s2


def test_auth_url_state_is_url_safe():
    tm = UpstoxTokenManager(client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT)
    url = tm.build_authorization_url()
    from urllib.parse import urlparse, parse_qs
    s = parse_qs(urlparse(url).query)["state"][0]
    import string
    allowed = string.ascii_letters + string.digits + "-._~"
    assert s
    assert all(c in allowed for c in s)


def test_auth_url_explicit_state_and_redirect():
    tm = UpstoxTokenManager(client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT)
    url = tm.build_authorization_url(redirect_uri="https://example.com/cb", state="xyz123")
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(url).query)
    assert qs["redirect_uri"] == ["https://example.com/cb"]
    assert qs["state"] == ["xyz123"]


def test_auth_url_has_only_allowed_params():
    tm = UpstoxTokenManager(client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT)
    url = tm.build_authorization_url(state="xyz")
    from urllib.parse import urlparse, parse_qs
    params = parse_qs(urlparse(url).query, keep_blank_values=True)
    assert set(params.keys()) == {"client_id", "redirect_uri", "response_type", "state"}
    assert "secret" not in urlparse(url).query.lower()


def test_auth_url_proper_url_encoding():
    """Redirect URI with special chars must be properly encoded in the URL."""
    tm = UpstoxTokenManager(
        client_id="client with space", secret=SECRET,
        redirect_uri="https://example.com/cb?foo=bar&baz=qux",
    )
    url = tm.build_authorization_url()
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(url).query)
    assert qs["redirect_uri"] == ["https://example.com/cb?foo=bar&baz=qux"]
    assert qs["client_id"] == ["client with space"]


# --------------------------------------------------------------------------- #
# exchange_auth_code
# --------------------------------------------------------------------------- #
def test_exchange_success_returns_access_token():
    captured = {}

    def fake_post(url, data):
        captured["url"] = url
        captured["data"] = data

        class R:
            status_code = 200

            def json(self):
                return {"access_token": ACCESS}

        return R()

    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT, http_post=fake_post,
    )
    access = tm.exchange_auth_code(CODE)
    assert access == ACCESS
    assert tm.access_token == ACCESS
    # Form-encoded body at the correct endpoint.
    assert captured["url"] == UPSTOX_TOKEN_URL
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["code"] == CODE
    assert captured["data"]["client_id"] == CLIENT
    assert captured["data"]["client_secret"] == SECRET
    assert captured["data"]["redirect_uri"] == REDIRECT


def test_exchange_success_stores_obtained_at():
    def fake_post(url, data):
        return _FakeResp(200, {"access_token": ACCESS})

    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT, http_post=fake_post,
    )
    tm.exchange_auth_code(CODE)
    assert tm._obtained_at is not None


def test_exchange_no_code_raises():
    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT, http_post=MagicMock(),
    )
    with pytest.raises(TokenError):
        tm.exchange_auth_code("")


def test_exchange_missing_secret_raises():
    tm = UpstoxTokenManager(
        client_id=CLIENT, secret="", redirect_uri=REDIRECT, http_post=MagicMock(),
    )
    with pytest.raises(TokenError):
        tm.exchange_auth_code(CODE)


def test_exchange_http_error_raises():
    def fake_post(url, data):
        return _FakeResp(400, {"status": "error", "errors": [{"code": "invalid_grant", "message": "bad code"}]})

    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT, http_post=fake_post,
    )
    with pytest.raises(TokenError) as ei:
        tm.exchange_auth_code(CODE)
    assert "rejected" in str(ei.value).lower() or "400" in str(ei.value)


def test_exchange_network_failure_raises():
    def fake_post(url, data):
        raise __import__("requests").RequestException("conn reset")

    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT, http_post=fake_post,
    )
    with pytest.raises(TokenError):
        tm.exchange_auth_code(CODE)


def test_exchange_malformed_json_raises():
    class BadResp:
        status_code = 200

        def json(self):
            raise ValueError("not json")

    def fake_post(url, data):
        return BadResp()

    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT, http_post=fake_post,
    )
    with pytest.raises(TokenError) as ei:
        tm.exchange_auth_code(CODE)
    assert ACCESS not in str(ei.value)


def test_exchange_missing_access_token_raises():
    def fake_post(url, data):
        return _FakeResp(200, {"some": "other_field"})

    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT, http_post=fake_post,
    )
    with pytest.raises(TokenError):
        tm.exchange_auth_code(CODE)


def test_exchange_invalid_credentials():
    def fake_post(url, data):
        return _FakeResp(401, {"status": "error", "errors": [{"code": "invalid_client", "message": "bad secret"}]})

    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT, http_post=fake_post,
    )
    with pytest.raises(TokenError):
        tm.exchange_auth_code(CODE)


def test_exchange_stores_token_and_notifies():
    """exchange_auth_code stores the token in memory."""
    def fake_post(url, data):
        return _FakeResp(200, {"access_token": ACCESS})

    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT, http_post=fake_post,
    )
    tm.exchange_auth_code(CODE)
    assert tm.access_token == ACCESS
    assert tm.has_access_token()


def test_exchange_does_not_print_token(capsys):
    def fake_post(url, data):
        return _FakeResp(200, {"access_token": ACCESS})

    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT, http_post=fake_post,
    )
    tm.exchange_auth_code(CODE)
    captured = capsys.readouterr()
    assert ACCESS not in captured.out
    assert ACCESS not in captured.err


# --------------------------------------------------------------------------- #
# verify_authentication (connectivity probe)
# --------------------------------------------------------------------------- #
def test_verify_no_token_no_probing():
    calls = []

    def fake_get(url, headers):
        calls.append(url)
        return _FakeResp(200, {"status": "success"})

    tm = UpstoxTokenManager(
        client_id=CLIENT, access_token="", secret=SECRET, redirect_uri=REDIRECT, http_get=fake_get,
    )
    st = tm.verify_authentication()
    assert st.status == AuthStatus.ACCESS_TOKEN_EXPIRED
    assert st.access_token_present is False
    assert calls == []  # no network call when token absent


def test_verify_connected():
    def fake_get(url, headers):
        assert url == UPSTOX_PROFILE_URL
        # Must use Bearer, not client_id:token.
        assert headers.get("Authorization") == f"Bearer {ACCESS}"
        return _FakeResp(200, {"status": "success", "data": {"user_id": "user123"}})

    tm = UpstoxTokenManager(
        client_id=CLIENT, access_token=ACCESS, secret=SECRET, redirect_uri=REDIRECT,
        http_get=fake_get,
    )
    st = tm.verify_authentication()
    assert st.status == AuthStatus.AUTH_OK
    assert st.access_token_present is True


def test_verify_token_rejected_401():
    def fake_get(url, headers):
        return _FakeResp(401, {"status": "error", "errors": [{"code": "invalid_token", "message": "expired"}]})

    tm = UpstoxTokenManager(
        client_id=CLIENT, access_token=ACCESS, secret=SECRET, redirect_uri=REDIRECT,
        http_get=fake_get,
    )
    st = tm.verify_authentication()
    assert st.status == AuthStatus.ACCESS_TOKEN_EXPIRED


def test_verify_token_rejected_403():
    def fake_get(url, headers):
        return _FakeResp(403, {"status": "error"})

    tm = UpstoxTokenManager(
        client_id=CLIENT, access_token=ACCESS, secret=SECRET, redirect_uri=REDIRECT,
        http_get=fake_get,
    )
    st = tm.verify_authentication()
    assert st.status == AuthStatus.ACCESS_TOKEN_EXPIRED


def test_verify_other_error():
    def fake_get(url, headers):
        return _FakeResp(500, {"status": "error", "errors": [{"code": "internal", "message": "server error"}]})

    tm = UpstoxTokenManager(
        client_id=CLIENT, access_token=ACCESS, secret=SECRET, redirect_uri=REDIRECT,
        http_get=fake_get,
    )
    st = tm.verify_authentication()
    assert st.status == AuthStatus.AUTH_FAILED


def test_verify_network_error_clean():
    def fake_get(url, headers):
        raise __import__("requests").RequestException("conn reset")

    tm = UpstoxTokenManager(
        client_id=CLIENT, access_token=ACCESS, secret=SECRET, redirect_uri=REDIRECT,
        http_get=fake_get,
    )
    st = tm.verify_authentication()
    assert st.status == AuthStatus.NETWORK_ERROR
    assert st.access_token_present is True


def test_verify_invokes_auth_status_callback():
    seen = {}

    def cb(status_str):
        seen["status"] = status_str

    def fake_get(url, headers):
        return _FakeResp(200, {"status": "success"})

    tm = UpstoxTokenManager(
        client_id=CLIENT, access_token=ACCESS, secret=SECRET, redirect_uri=REDIRECT,
        http_get=fake_get, auth_status_callback=cb,
    )
    tm.verify_authentication()
    assert seen.get("status") == AuthStatus.AUTH_OK.value


def test_exchange_success_then_probe_failure_keeps_token():
    """A failed probe must not wipe the freshly exchanged token in memory."""
    def fake_post(url, data):
        return _FakeResp(200, {"access_token": ACCESS})

    def fake_get(url, headers):
        return _FakeResp(401, {"status": "error", "errors": [{"code": "expired", "message": "expired"}]})

    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT,
        http_post=fake_post, http_get=fake_get,
    )
    tm.exchange_auth_code(CODE)
    assert tm.access_token == ACCESS
    st = tm.verify_authentication()
    assert st.status == AuthStatus.ACCESS_TOKEN_EXPIRED
    assert tm.access_token == ACCESS  # token retained


# --------------------------------------------------------------------------- #
# Token expiration (3:30 AM IST next day)
# --------------------------------------------------------------------------- #
def test_token_expiration_after_330am_expires_next_day():
    """Token obtained at 10:00 IST → expires at 3:30 AM IST the next day."""
    from trading_system.india.token_manager import _next_expiry, _IST, _UTC
    obtained = datetime(2026, 9, 1, 4, 30, tzinfo=_UTC)  # 10:00 IST
    expiry = _next_expiry(obtained)
    # 3:30 AM IST on Sep 2 = 3:30 - 5:30 = 2026-09-01 22:00 UTC
    expected = datetime(2026, 9, 1, 22, 0, tzinfo=_UTC)
    assert expiry == expected


def test_token_expiration_before_330am_expires_same_day():
    """Token obtained at 2:00 IST → expires at 3:30 AM IST same day."""
    from trading_system.india.token_manager import _next_expiry, _IST, _UTC
    obtained = datetime(2026, 9, 1, 1, 30, tzinfo=_UTC)  # 7:00 IST... wait
    # 2:00 IST = 2026-09-01 20:00 UTC (previous day UTC)
    # Actually let me compute: 2:00 IST = 2:00 - 5:30 = 20:00 UTC previous day
    # So in UTC it's 2026-08-31 20:30 UTC
    obtained = datetime(2026, 8, 31, 20, 30, tzinfo=_UTC)  # 2:00 AM Sep 1 IST
    expiry = _next_expiry(obtained)
    # 3:30 AM IST Sep 1 = 3:30 - 5:30 = 2026-08-31 22:00 UTC
    expected = datetime(2026, 8, 31, 22, 0, tzinfo=_UTC)
    assert expiry == expected


def test_is_expired_returns_false_when_no_obtained_at():
    tm = UpstoxTokenManager(access_token=ACCESS)
    assert tm._is_expired() is False


def test_is_expired_returns_true_when_past_expiry():
    from trading_system.india.token_manager import _UTC
    tm = UpstoxTokenManager(access_token=ACCESS)
    tm._obtained_at = datetime(2020, 1, 1, tzinfo=_UTC)
    assert tm._is_expired() is True


def test_is_expired_returns_false_within_window():
    from trading_system.india.token_manager import _UTC
    tm = UpstoxTokenManager(access_token=ACCESS)
    # Obtained now → not yet expired (3:30 AM hasn't passed).
    tm._obtained_at = datetime.now(_UTC)
    assert tm._is_expired() is False


# --------------------------------------------------------------------------- #
# Secrets safety
# --------------------------------------------------------------------------- #
def test_secret_never_in_logs():
    cap = _LogCapture()
    cap.install()
    try:
        def fake_post(url, data):
            return _FakeResp(200, {"access_token": ACCESS})

        tm = UpstoxTokenManager(
            client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT, http_post=fake_post,
        )
        tm.exchange_auth_code(CODE)
        text = cap.text
        assert ACCESS not in text
        assert SECRET not in text
        assert CODE not in text
    finally:
        cap.uninstall()


def test_secret_never_in_exception():
    def fake_post(url, data):
        return _FakeResp(400, {"status": "error", "errors": [{"code": "bad", "message": "nope"}]})

    tm = UpstoxTokenManager(
        client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT, http_post=fake_post,
    )
    try:
        tm.exchange_auth_code(CODE)
        assert False, "expected TokenError"
    except TokenError as e:
        msg = str(e)
        assert SECRET not in msg
        assert CODE not in msg
        assert ACCESS not in msg


# --------------------------------------------------------------------------- #
# save / load access token
# --------------------------------------------------------------------------- #
def test_save_access_token_writes_env(tmp_path, monkeypatch):
    from trading_system.config import settings
    monkeypatch.setattr(settings, "project_root", tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("# existing\nUPSTOX_CLIENT_ID=abc\n\n")

    tm = UpstoxTokenManager(client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT)
    tm.save_access_token(ACCESS)
    content = env_file.read_text()
    assert "UPSTOX_ACCESS_TOKEN=" + ACCESS in content
    # Existing line untouched.
    assert "UPSTOX_CLIENT_ID=abc" in content


def test_save_access_token_updates_existing(tmp_path, monkeypatch):
    from trading_system.config import settings
    monkeypatch.setattr(settings, "project_root", tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("UPSTOX_ACCESS_TOKEN=old_token\n")

    tm = UpstoxTokenManager(client_id=CLIENT, secret=SECRET, redirect_uri=REDIRECT)
    tm.save_access_token(ACCESS)
    content = env_file.read_text()
    assert "UPSTOX_ACCESS_TOKEN=" + ACCESS in content
    assert "old_token" not in content
