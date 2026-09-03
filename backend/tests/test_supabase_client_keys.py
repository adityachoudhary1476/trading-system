"""Regression tests for supabase-py client construction with modern API keys.

The Supabase platform now issues two API key formats:
  * Legacy JWTs shaped like `eyJ...` (three dot-separated base64url segments)
  * Modern non-JWT keys shaped like `sb_secret_...` or `sb_publishable_...`

Earlier versions of supabase-py (<2.18.0) validated the key against a JWT
regex inside `SyncClient.__init__` and raised `SupabaseException("Invalid API
key")` for any non-JWT value. This module pins that behavior so we never
silently regress to a version that rejects the modern key shape used in
production.
"""
from __future__ import annotations

import pytest


FAKE_SECRET_KEY = "sb_secret_FAKE_REDACTED_NOT_A_REAL_KEY_xxxxxxxxxxxxxxxxxxxx"
FAKE_PUBLISHABLE_KEY = "sb_publishable_FAKE_REDACTED_NOT_A_REAL_KEY_xxxxxxxxxxxxx"


def test_create_client_accepts_sb_secret_key():
    """The constructor must not reject modern sb_secret_ keys."""
    from supabase import create_client

    client = create_client("https://example.supabase.co", FAKE_SECRET_KEY)
    assert client is not None


def test_create_client_accepts_sb_publishable_key():
    """The constructor must not reject modern sb_publishable_ keys."""
    from supabase import create_client

    client = create_client("https://example.supabase.co", FAKE_PUBLISHABLE_KEY)
    assert client is not None


def test_create_client_accepts_legacy_jwt_key():
    """Legacy JWT keys must continue to work."""
    from supabase import create_client

    legacy_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.signature"
    client = create_client("https://example.supabase.co", legacy_jwt)
    assert client is not None


def test_create_client_does_not_raise_supabase_exception_for_modern_key():
    """Specifically guard against the historical `Invalid API key` error."""
    from supabase import create_client
    from supabase._sync.client import SupabaseException

    try:
        create_client("https://example.supabase.co", FAKE_SECRET_KEY)
    except SupabaseException as exc:  # pragma: no cover - regression guard
        pytest.fail(
            "supabase client rejected a modern sb_secret_ key. "
            f"This is the regression we are guarding against. Error: {exc}"
        )
