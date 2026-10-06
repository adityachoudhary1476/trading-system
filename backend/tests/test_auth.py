"""Tests for authentication."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from main import app


@pytest.fixture
def client():
    """Create a test client."""
    return TestClient(app)


class TestAuthentication:
    """Test authentication endpoints."""

    def test_missing_token_returns_401(self, client):
        """Test that missing token returns 401."""
        response = client.get("/api/market/analysis?symbol=NSE:SBIN")
        assert response.status_code == 401
        assert "Authentication required" in response.json()["detail"]

    def test_invalid_token_returns_401(self, client):
        """Test that invalid token returns 401."""
        with patch("auth._validate_supabase_jwt", return_value=None):
            response = client.get(
                "/api/market/analysis?symbol=NSE:SBIN",
                headers={"Authorization": "Bearer invalid_token"},
            )
            assert response.status_code == 401

    def test_valid_token_returns_user(self, client, monkeypatch):
        """Test that valid token returns authenticated user."""
        mock_user = MagicMock()
        mock_user.user_id = "test-user-id"
        mock_user.email = "test@example.com"

        # ``get_current_user`` short-circuits to 401 when SUPABASE_URL is
        # unset, before it ever calls the validator. Configure a URL for the
        # duration of the test so the patched validator is actually reached,
        # and clear the settings cache so the value is picked up.
        from config import get_settings

        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        get_settings.cache_clear()
        try:
            with patch("auth._validate_supabase_jwt", return_value=mock_user):
                # This will fail at the broker stage, but auth should pass
                response = client.get(
                    "/api/market/analysis?symbol=NSE:SBIN",
                    headers={"Authorization": "Bearer valid_token"},
                )
                # Should not be 401
                assert response.status_code != 401
        finally:
            get_settings.cache_clear()
