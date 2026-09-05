"""Tests for broker.get_upstox_access_token with service-account fallback.

Covers the three cases:
1. User has individual Upstox token → returns user's token.
2. User has no individual token + service-account token configured → returns service token.
3. Neither user token nor service-account token → returns None.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _clean_settings_cache():
    """Clear the settings LRU cache before and after each test."""
    from config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _loop():
    return asyncio.new_event_loop()


class TestBrokerTokenFallback:
    """Test the service-account token fallback in get_upstox_access_token."""

    def test_user_token_found_returns_user_token(self):
        """When the user has an individual Upstox connection, that token is returned."""
        from services.broker import get_upstox_access_token

        mock_sb = MagicMock()
        mock_response = MagicMock()
        mock_response.data = {"access_token_encrypted": "encrypted_blob_here"}
        mock_response.error = None
        mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value = mock_response

        with patch.dict(os.environ, {
            "SUPABASE_URL": "https://test.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "service-role-key-test-only",
            "UPSTOX_SERVICE_ACCOUNT_TOKEN": "service-account-token-test-only",
        }):
            with patch("supabase.create_client", return_value=mock_sb):
                with patch("services.broker.decrypt_token", return_value="user-individual-token"):
                    loop = _loop()
                    token = loop.run_until_complete(
                        get_upstox_access_token("user-123")
                    )
                    loop.close()
                    assert token == "user-individual-token"

    def test_no_user_token_service_token_configured_returns_service_token(self):
        """When user has no individual token but service-account token exists, returns service token."""
        from services.broker import get_upstox_access_token

        mock_sb = MagicMock()
        mock_response = MagicMock()
        mock_response.data = None
        mock_response.error = None
        mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value = mock_response

        with patch.dict(os.environ, {
            "SUPABASE_URL": "https://test.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "service-role-key-test-only",
            "UPSTOX_SERVICE_ACCOUNT_TOKEN": "service-account-token-test-only",
        }):
            with patch("supabase.create_client", return_value=mock_sb):
                loop = _loop()
                token = loop.run_until_complete(
                    get_upstox_access_token("user-456")
                )
                loop.close()
                assert token == "service-account-token-test-only"

    def test_no_user_token_no_service_token_returns_none(self):
        """When neither user token nor service-account token exists, returns None."""
        from services.broker import get_upstox_access_token

        mock_sb = MagicMock()
        mock_response = MagicMock()
        mock_response.data = None
        mock_response.error = None
        mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value = mock_response

        with patch.dict(os.environ, {
            "SUPABASE_URL": "https://test.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "service-role-key-test-only",
            "UPSTOX_SERVICE_ACCOUNT_TOKEN": "",
        }):
            with patch("supabase.create_client", return_value=mock_sb):
                loop = _loop()
                token = loop.run_until_complete(
                    get_upstox_access_token("user-789")
                )
                loop.close()
                assert token is None

    def test_supabase_not_configured_falls_back_to_service_token(self):
        """When Supabase is not configured, still falls back to service token."""
        from services.broker import get_upstox_access_token

        with patch.dict(os.environ, {
            "SUPABASE_URL": "",
            "SUPABASE_SERVICE_ROLE_KEY": "",
            "UPSTOX_SERVICE_ACCOUNT_TOKEN": "service-account-token-test-only",
        }, clear=False):
            loop = _loop()
            token = loop.run_until_complete(
                get_upstox_access_token("user-abc")
            )
            loop.close()
            assert token == "service-account-token-test-only"

    def test_supabase_not_configured_no_service_token_returns_none(self):
        """When nothing is configured, returns None."""
        from services.broker import get_upstox_access_token

        with patch.dict(os.environ, {
            "SUPABASE_URL": "",
            "SUPABASE_SERVICE_ROLE_KEY": "",
            "UPSTOX_SERVICE_ACCOUNT_TOKEN": "",
        }, clear=False):
            loop = _loop()
            token = loop.run_until_complete(
                get_upstox_access_token("user-xyz")
            )
            loop.close()
            assert token is None

    def test_user_token_takes_priority_over_service_token(self):
        """User's individual token takes priority over service account token."""
        from services.broker import get_upstox_access_token

        mock_sb = MagicMock()
        mock_response = MagicMock()
        mock_response.data = {"access_token_encrypted": "encrypted_blob_here"}
        mock_response.error = None
        mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value = mock_response

        with patch.dict(os.environ, {
            "SUPABASE_URL": "https://test.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "service-role-key-test-only",
            "UPSTOX_SERVICE_ACCOUNT_TOKEN": "service-account-token-test-only",
        }):
            with patch("supabase.create_client", return_value=mock_sb):
                with patch("services.broker.decrypt_token", return_value="user-individual-token") as mock_decrypt:
                    loop = _loop()
                    token = loop.run_until_complete(
                        get_upstox_access_token("user-priority-test")
                    )
                    loop.close()
                    assert token == "user-individual-token"
                    # Verify decrypt was called (user path taken, not service fallback)
                    mock_decrypt.assert_called_once()
