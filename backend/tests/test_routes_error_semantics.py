"""Route-level tests for analysis and signals error semantics.

These tests pin the *new* behaviour: a total Upstox failure must NOT
collapse to a 200 with an empty list, and a 401/403 from Upstox must
surface as a 403 to the frontend, not a 404.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from main import app
from auth import get_current_user
from services.market_data import (
    UpstoxBadResponseError,
    UpstoxMarketDataError,
    UpstoxNetworkError,
    UpstoxRateLimitedError,
    UpstoxUnauthorizedError,
)


@pytest.fixture
def client():
    async def mock_get_current_user():
        return MagicMock(user_id="test-user", email="test@example.com")

    app.dependency_overrides[get_current_user] = mock_get_current_user
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer fake-token"}


def _stub_token():
    with patch("routes.analysis.broker.get_upstox_access_token") as m:
        m.return_value = "fake-bearer-not-real"
        yield m


class TestAnalysisErrorSemantics:
    def test_upstox_401_surfaces_as_403(self, client, auth_headers):
        with patch("routes.analysis.broker.get_upstox_access_token") as mock_token:
            mock_token.return_value = "fake-bearer"
            with patch(
                "routes.analysis.market_data.fetch_ohlcv",
                side_effect=UpstoxUnauthorizedError("invalid token"),
            ):
                response = client.get(
                    "/api/market/analysis?symbol=NSE:NIFTY50", headers=auth_headers
                )
        assert response.status_code == 403
        assert "reconnect" in response.json()["detail"].lower()

    def test_upstox_429_surfaces_as_503(self, client, auth_headers):
        with patch("routes.analysis.broker.get_upstox_access_token") as mock_token:
            mock_token.return_value = "fake-bearer"
            with patch(
                "routes.analysis.market_data.fetch_ohlcv",
                side_effect=UpstoxRateLimitedError("rate limit"),
            ):
                response = client.get(
                    "/api/market/analysis?symbol=NSE:NIFTY50", headers=auth_headers
                )
        assert response.status_code == 503

    def test_upstox_500_surfaces_as_502(self, client, auth_headers):
        with patch("routes.analysis.broker.get_upstox_access_token") as mock_token:
            mock_token.return_value = "fake-bearer"
            with patch(
                "routes.analysis.market_data.fetch_ohlcv",
                side_effect=UpstoxBadResponseError("HTTP 500 upstream"),
            ):
                response = client.get(
                    "/api/market/analysis?symbol=NSE:NIFTY50", headers=auth_headers
                )
        assert response.status_code == 502

    def test_upstox_malformed_surfaces_as_502(self, client, auth_headers):
        with patch("routes.analysis.broker.get_upstox_access_token") as mock_token:
            mock_token.return_value = "fake-bearer"
            with patch(
                "routes.analysis.market_data.fetch_ohlcv",
                side_effect=UpstoxNetworkError("connection error"),
            ):
                response = client.get(
                    "/api/market/analysis?symbol=NSE:NIFTY50", headers=auth_headers
                )
        assert response.status_code == 502

    def test_empty_upstox_response_still_returns_404(self, client, auth_headers):
        # Upstox success body with empty candles -> 404 (no data for symbol)
        with patch("routes.analysis.broker.get_upstox_access_token") as mock_token:
            mock_token.return_value = "fake-bearer"
            with patch(
                "routes.analysis.market_data.fetch_ohlcv",
                return_value=None,
            ):
                response = client.get(
                    "/api/market/analysis?symbol=NSE:NIFTY50", headers=auth_headers
                )
        assert response.status_code == 404


class TestSignalsErrorSemantics:
    def _settings_universe(self, monkeypatch, value: str):
        monkeypatch.setenv("SIGNAL_UNIVERSE", value)
        from config import get_settings
        get_settings.cache_clear()

    def test_total_upstox_failure_surfaces_as_502(self, client, auth_headers, monkeypatch):
        self._settings_universe(monkeypatch, "NSE:NIFTY50")
        with patch("routes.signals.broker.get_upstox_access_token") as mock_token:
            mock_token.return_value = "fake-bearer"
            with patch(
                "routes.signals.market_data.fetch_ohlcv",
                side_effect=UpstoxBadResponseError("upstream down"),
            ):
                response = client.get("/api/market/signals?limit=1", headers=auth_headers)
        assert response.status_code == 502

    def test_total_upstox_unauthorized_surfaces_as_403(
        self, client, auth_headers, monkeypatch
    ):
        self._settings_universe(monkeypatch, "NSE:NIFTY50")
        with patch("routes.signals.broker.get_upstox_access_token") as mock_token:
            mock_token.return_value = "fake-bearer"
            with patch(
                "routes.signals.market_data.fetch_ohlcv",
                side_effect=UpstoxUnauthorizedError("invalid token"),
            ):
                response = client.get("/api/market/signals?limit=1", headers=auth_headers)
        assert response.status_code == 403

    def test_partial_upstox_failure_still_returns_200(
        self, client, auth_headers, monkeypatch
    ):
        # Two symbols, one fails, one succeeds -> 200 with the one signal
        self._settings_universe(monkeypatch, "NSE:NIFTY50,NSE:SBIN")

        async def fake_fetch(symbol, timeframe, token, bars=60):
            if symbol == "NSE:NIFTY50":
                raise UpstoxBadResponseError("upstream down for nifty")
            dates = pd.date_range(start="2024-01-01", periods=60, freq="D", tz="UTC")
            return pd.DataFrame(
                {
                    "open": [100.0 + i for i in range(60)],
                    "high": [105.0 + i for i in range(60)],
                    "low": [95.0 + i for i in range(60)],
                    "close": [102.0 + i for i in range(60)],
                    "volume": [1000] * 60,
                },
                index=dates,
            )

        with patch("routes.signals.broker.get_upstox_access_token") as mock_token:
            mock_token.return_value = "fake-bearer"
            with patch(
                "routes.signals.market_data.fetch_ohlcv", side_effect=fake_fetch
            ):
                response = client.get("/api/market/signals?limit=2", headers=auth_headers)
        assert response.status_code == 200
        # At least one signal returned (from SBIN)
        body = response.json()
        assert isinstance(body, list)
        assert any(s["symbol"] == "NSE:SBIN" for s in body)
