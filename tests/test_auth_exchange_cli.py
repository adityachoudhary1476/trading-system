"""Regression tests for the FYERS auth-exchange CLI precedence fix (Day 10.5).

Verifies:
* Manual hidden-prompt input takes precedence over a stale FYERS_AUTH_CODE env var.
* FYERS_AUTH_CODE is used ONLY when manual input is empty (fallback).
* The environment value is NEVER printed to stdout/logging.
* Explicit --auth-code still wins over prompting.
No network, no real credentials. TokenManager is faked.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
from unittest.mock import patch

from trading_system import __main__ as cli


def test_extract_bare_token():
    assert cli._extract_auth_code("eyJabc123") == "eyJabc123"


def test_extract_with_surrounding_whitespace():
    assert cli._extract_auth_code("  eyJabc123  ") == "eyJabc123"


def test_extract_quoted():
    assert cli._extract_auth_code('"eyJabc123"') == "eyJabc123"
    assert cli._extract_auth_code("'eyJabc123'") == "eyJabc123"


def test_extract_auth_code_equals_form():
    assert cli._extract_auth_code("auth_code=eyJabc123") == "eyJabc123"


def test_extract_auth_code_with_state():
    assert cli._extract_auth_code("auth_code=eyJabc123&state=sample") == "eyJabc123"


def test_extract_full_redirect_url():
    url = "https://trade.fyers.in/api-login/redirect-uri/index.html?auth_code=eyJabc123&state=sample"
    assert cli._extract_auth_code(url) == "eyJabc123"


def test_extract_empty():
    assert cli._extract_auth_code("") == ""
    assert cli._extract_auth_code("   ") == ""


FAKE_ENV = "STALE_ENV_CODE_0000000000"
MANUAL = "MANUAL_FRESH_CODE_1111111111"
CLI = "CLI_EXPLICIT_CODE_2222222222"
ACCESS = "FAKE_ACCESS_TOKEN"
REFRESH = "FAKE_REFRESH_TOKEN"

TM_PATH = "trading_system.india.token_manager.TokenManager"


class _FakeTM:
    """Records the code passed to exchange_auth_code; returns fake tokens."""
    def __init__(self, *a, **k):
        self.client_id = "X"
        self.secret = "Y"
        self.captured = None

    def exchange_auth_code(self, code, redirect_uri=None):
        self.captured = code
        return ACCESS, REFRESH


def _args(auth_code=None):
    return argparse.Namespace(auth_code=auth_code)


def _run(args, manual_input):
    """Run the CLI with a faked TokenManager + faked getpass; return (rc, tm, out)."""
    tm = _FakeTM()
    buf = io.StringIO()
    with patch(TM_PATH, lambda *a, **k: tm), \
         patch("getpass.getpass", return_value=manual_input):
        with contextlib.redirect_stdout(buf):
            rc = cli._cmd_auth_exchange(args)
    return rc, tm, buf.getvalue()


def test_manual_input_overrides_stale_env():
    with patch.dict(os.environ, {"FYERS_AUTH_CODE": FAKE_ENV}, clear=False):
        rc, tm, out = _run(_args(), MANUAL)
    assert rc == 0
    assert tm.captured == MANUAL      # manual wins over env
    assert tm.captured != FAKE_ENV


def test_env_used_only_when_manual_empty():
    with patch.dict(os.environ, {"FYERS_AUTH_CODE": FAKE_ENV}, clear=False):
        rc, tm, out = _run(_args(), "")  # empty manual -> fallback to env
    assert rc == 0
    assert tm.captured == FAKE_ENV


def test_env_value_never_printed():
    with patch.dict(os.environ, {"FYERS_AUTH_CODE": FAKE_ENV}, clear=False):
        rc, tm, out = _run(_args(), MANUAL)
    assert FAKE_ENV not in out  # env value must never appear in output
    assert "WARNING: FYERS_AUTH_CODE environment variable is set; manual input takes precedence." in out


def test_explicit_cli_auth_code_wins():
    with patch.dict(os.environ, {"FYERS_AUTH_CODE": FAKE_ENV}, clear=False):
        rc, tm, out = _run(_args(auth_code=CLI), "SHOULD_NOT_BE_USED")
    assert rc == 0
    assert tm.captured == CLI        # explicit --auth-code wins, prompt skipped
