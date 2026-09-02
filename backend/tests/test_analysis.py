"""Tests for market analysis endpoint."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from main import app
from auth import get_current_user


@pytest.fixture
def client():
    """Create a test client with mocked auth."""
    async def mock_get_current_user():
        return MagicMock(user_id="test-user", email="test@example.com")

    app.dependency_overrides[get_current_user] = mock_get_current_user
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def auth_headers():
    """Return headers with valid auth token."""
    return {"Authorization": "Bearer test_token"}


class TestAnalysisEndpoint:
    """Test the analysis endpoint."""

    def test_analysis_requires_auth(self):
        """Test that analysis requires authentication."""
        # Create a client without auth override
        with TestClient(app) as test_client:
            response = test_client.get("/api/market/analysis?symbol=NSE:SBIN")
            assert response.status_code == 401

    def test_analysis_requires_broker_connection(self, client, auth_headers):
        """Test that analysis requires broker connection."""
        with patch("routes.analysis.broker.get_upstox_access_token") as mock_token:
            mock_token.return_value = None

            response = client.get("/api/market/analysis?symbol=NSE:SBIN", headers=auth_headers)
            assert response.status_code == 403
            assert "Upstox connection not found" in response.json()["detail"]

    def test_analysis_returns_real_data(self, client, auth_headers):
        """Test that analysis returns real data."""
        import pandas as pd

        mock_result = {
            "symbol": "NSE:SBIN",
            "timeframe": "1d",
            "bias": "bullish",
            "confidence": 0.75,
            "signal": "long",
            "summary": "Strong bullish trend detected",
            "factors": [],
            "generated_at": 1704067200000,
            "model": "test-model",
        }

        # Create a mock DataFrame
        dates = pd.date_range(start="2024-01-01", periods=50, freq="D", tz="UTC")
        mock_df = pd.DataFrame(
            {
                "open": [100.0] * 50,
                "high": [105.0] * 50,
                "low": [95.0] * 50,
                "close": [102.0] * 50,
                "volume": [1000] * 50,
            },
            index=dates,
        )

        with patch("routes.analysis.broker.get_upstox_access_token") as mock_token:
            with patch("routes.analysis.market_data.fetch_ohlcv") as mock_ohlcv:
                with patch("routes.analysis.analysis.analyze_market") as mock_analyze:
                    mock_token.return_value = "test_access_token"
                    mock_ohlcv.return_value = mock_df
                    mock_analyze.return_value = mock_result

                    response = client.get("/api/market/analysis?symbol=NSE:SBIN", headers=auth_headers)
                    assert response.status_code == 200
                    data = response.json()
                    assert "symbol" in data
                    assert "bias" in data
                    assert "confidence" in data
