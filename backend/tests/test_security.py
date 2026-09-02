"""Security tests to ensure no secrets are exposed."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from main import app


@pytest.fixture
def client():
    """Create a test client."""
    return TestClient(app)


class TestSecurity:
    """Security tests."""

    def test_no_token_in_error_responses(self, client):
        """Verify that error responses don't contain tokens."""
        response = client.get("/api/market/analysis?symbol=NSE:SBIN")
        assert response.status_code == 401
        # Response should not contain any token-like strings
        response_text = response.text
        assert "Bearer" not in response_text
        assert "access_token" not in response_text.lower()

    def test_health_endpoint_no_secrets(self, client):
        """Verify health endpoint doesn't expose secrets."""
        response = client.get("/health")
        assert response.status_code == 200
        response_text = response.text
        # Should not contain any secret patterns
        assert "key" not in response_text.lower()
        assert "secret" not in response_text.lower()
        assert "token" not in response_text.lower()

    def test_analysis_error_no_broker_details(self, client):
        """Verify analysis errors don't expose broker details."""
        with patch("routes.analysis.get_current_user") as mock_user:
            mock_user.return_value = MagicMock(user_id="test-user")
            response = client.get(
                "/api/market/analysis?symbol=NSE:SBIN",
                headers={"Authorization": "Bearer test"},
            )
            # Should not expose internal broker details
            response_text = response.text.lower()
            assert "decrypt" not in response_text
            assert "encrypt" not in response_text
