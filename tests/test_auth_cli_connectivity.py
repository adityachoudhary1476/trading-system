"""CLI-level tests for auth-status connectivity and malformed auth-code handling (Day 11).

No network, no real credentials. UpstoxTokenManager + its http_get probe are faked so
we can assert the CLI reports CONNECTED / NOT CONNECTED based on the LIVE probe, not
merely on token presence, and that malformed input aborts cleanly (rc=2) without
contacting Upstox.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
from unittest.mock import patch

from trading_system import __main__ as cli
from trading_system.india.token_manager import TokenState, AuthStatus


ACCESS = "FAKE_ACCESS_TOKEN_0123456789"
FAKE_ENV = "STALE_ENV_CODE_0000000000"
MANUAL = "MANUAL_FRESH_CODE_1111111111"


class _FakeTM:
    """Configurable fake UpstoxTokenManager for CLI tests."""
    def __init__(self, *a, **k):
        self.client_id = "X"
        self.secret = "Y"
        self.redirect_uri = "https://127.0.0.1:8080/callback"
        self._access = ""
        self.probe_state = None
        self.exchanged = None
        self.captured_code = None

    def token_status(self):
        present = bool(self._access)
        return TokenState(
            AuthStatus.AUTH_OK if present else AuthStatus.ACCESS_TOKEN_EXPIRED,
            present, False, "x",
        )

    def verify_authentication(self):
        return self.probe_state

    def exchange_auth_code(self, code, redirect_uri=None):
        self.captured_code = code
        self._access = ACCESS
        return ACCESS

    def save_access_token(self, token):
        import tempfile, os
        from pathlib import Path
        p = Path(tempfile.gettempdir()) / ".env.test"
        p.write_text(f"UPSTOX_ACCESS_TOKEN={token}\n")
        return p


def _make_tm_class(probe_status_value, access_present=False):
    class _T(_FakeTM):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._access = ACCESS if access_present else ""
            self.probe_state = TokenState(probe_status_value, access_present, False, "probe msg")
    return _T


def _run(args, tm_class):
    buf = io.StringIO()
    with patch("trading_system.india.token_manager.UpstoxTokenManager", tm_class), \
         contextlib.redirect_stdout(buf):
        rc = cli._cmd_auth_status(args)
    return rc, buf.getvalue()


def test_auth_status_connected_only_when_probe_ok():
    cls = _make_tm_class(AuthStatus.AUTH_OK, access_present=True)
    rc, out = _run(argparse.Namespace(), cls)
    assert rc == 0
    assert "UPSTOX CONNECTIVITY    : CONNECTED" in out
    assert "CONNECTED — access token accepted by Upstox." in out


def test_auth_status_not_connected_on_token_presence_alone():
    # Token present but probe says rejected -> must NOT print CONNECTED.
    cls = _make_tm_class(AuthStatus.ACCESS_TOKEN_EXPIRED, access_present=True)
    rc, out = _run(argparse.Namespace(), cls)
    assert rc == 2
    assert "UPSTOX CONNECTIVITY    : NOT CONNECTED" in out
    assert "CONNECTED — access token accepted" not in out


def test_auth_status_missing_credentials_exits_cleanly():
    class _T(_FakeTM):
        def token_status(self):
            return TokenState(AuthStatus.ACCESS_TOKEN_EXPIRED, False, False, "no token")

    rc, out = _run(argparse.Namespace(), _T)
    assert rc == 2
    assert "CREDENTIALS MISSING" in out
    assert "UPSTOX CONNECTIVITY" not in out  # no probe attempted


def test_auth_status_network_error_reported():
    cls = _make_tm_class(AuthStatus.NETWORK_ERROR, access_present=True)
    rc, out = _run(argparse.Namespace(), cls)
    assert rc == 2
    assert "NETWORK ERROR" in out


# --- malformed auth input to auth-exchange --------------------------------------

def _run_exchange(args, manual_input, tm_class):
    buf = io.StringIO()
    with patch("trading_system.india.token_manager.UpstoxTokenManager", tm_class), \
         patch("getpass.getpass", return_value=manual_input), \
         contextlib.redirect_stdout(buf):
        rc = cli._cmd_auth_exchange(args)
    return rc, buf.getvalue()


def test_extract_malformed_url_passed_through_not_silent():
    # A non-empty string without auth_code= is NOT silently dropped; it is passed through
    # unchanged (so a real Upstox rejection surfaces instead of a false "ok").
    assert cli._extract_auth_code("https://example.com/foo") == "https://example.com/foo"


def test_auth_exchange_empty_input_aborts_cleanly():
    cls = _make_tm_class(AuthStatus.AUTH_OK, access_present=True)
    with patch.dict(os.environ, {}, clear=False):
        rc, out = _run_exchange(argparse.Namespace(auth_code=None), "", cls)
    assert rc == 2
    assert "No auth_code provided" in out
    assert cls().captured_code is None
