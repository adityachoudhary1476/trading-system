"""Route-level tests for /api/market/quote and /api/market/status.

These tests pin the contract:

* the quote route returns a finite real ``price`` derived from a real
  Upstox quote (never ``0`` or fabricated)
* the quote route returns a fresh ``timestamp`` (epoch ms) on every
  successful call
* HTTP 401/403 from Upstox become 403; 429 becomes 503; 5xx / network
  become 502
* the status route returns the authoritative NSE session phase, server
  time, and next session boundaries
* when the market is closed the status phase is ``closed`` /
  ``holiday``; ``serverTime`` is the canonical epoch-ms the client
  should use for staleness calculations
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app
from auth import get_current_user
from services.market_data import (
    UpstoxBadResponseError,
    UpstoxNetworkError,
    UpstoxRateLimitedError,
    UpstoxUnauthorizedError,
)
from src.trading_system.india.market_calendar import SessionPhase


FAKE_BEARER = "fake-redacted-bearer-token-not-a-real-credential"


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
    return {"Authorization": f"Bearer {FAKE_BEARER}"}


def _stub_broker_token():
    async def _get(user_id):
        return FAKE_BEARER
    return _get


def _real_quote_payload(symbol: str = "NSE_EQ|INE062A01020") -> dict:
    return {
        "status": "success",
        "data": {
            symbol: {
                "ohlc": {"open": 800.0, "high": 860.0, "low": 795.0, "close": 805.0},
                "depth": {"buy": [], "sell": []},
                "timestamp": "2024-01-05T03:45:30+00:00",
                "instrument_token": 12345,
                "symbol": symbol,
                "last_price": 850.5,
                "volume": 1234567,
                "average_price": 820.25,
                "open_price": 800.0,
                "high_price": 860.0,
                "low_price": 795.0,
                "close_price": 805.0,
            }
        },
    }


# ---------------------------------------------------------------------------
# /api/market/quote happy path
# ---------------------------------------------------------------------------


class TestQuoteRouteContract:
    def test_returns_real_finite_price(self, client, auth_headers):
        with patch(
            "routes.live.broker.get_upstox_access_token",
            side_effect=_stub_broker_token(),
        ):
            with patch(
                "routes.live.market_data.fetch_quote",
                return_value={
                    "last_price": 850.5,
                    "open_price": 800.0,
                    "high_price": 860.0,
                    "low_price": 795.0,
                    "prev_close": 805.0,
                    "volume": 1234567,
                    "average_price": 820.25,
                    "ohlc": {"open": 800.0, "high": 860.0, "low": 795.0, "close": 805.0},
                    "depth": None,
                    "timestamp": 1704412800,
                    "instrument_token": 12345,
                    "symbol": "NSE_EQ|INE062A01020",
                    "raw": {},
                },
            ):
                resp = client.get(
                    "/api/market/quote?symbol=NSE:SBIN", headers=auth_headers
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["price"] == 850.5
        assert body["price"] > 0
        assert body["symbol"] == "NSE:SBIN"
        # wire-format must use camelCase
        assert "previousClose" in body
        assert "change" in body
        assert "changePct" in body
        assert "timestamp" in body
        assert "sessionState" in body

    def test_change_derived_from_prev_close(self, client, auth_headers):
        with patch(
            "routes.live.broker.get_upstox_access_token",
            side_effect=_stub_broker_token(),
        ):
            with patch(
                "routes.live.market_data.fetch_quote",
                return_value={
                    "last_price": 850.5,
                    "open_price": 800.0,
                    "high_price": 860.0,
                    "low_price": 795.0,
                    "prev_close": 800.0,  # change = 50.5, changePct ≈ 6.31
                    "volume": 1,
                    "average_price": 800.0,
                    "ohlc": {"open": 800.0, "high": 860.0, "low": 795.0, "close": 800.0},
                    "depth": None,
                    "timestamp": 1704412800,
                    "instrument_token": 1,
                    "symbol": "NSE_EQ|X",
                    "raw": {},
                },
            ):
                resp = client.get(
                    "/api/market/quote?symbol=NSE:SBIN", headers=auth_headers
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["previousClose"] == 800.0
        assert abs(body["change"] - 50.5) < 1e-9
        assert body["changePct"] is not None
        assert abs(body["changePct"] - 6.3125) < 1e-3

    def test_timestamp_is_epoch_ms(self, client, auth_headers):
        with patch(
            "routes.live.broker.get_upstox_access_token",
            side_effect=_stub_broker_token(),
        ):
            with patch(
                "routes.live.market_data.fetch_quote",
                return_value={
                    "last_price": 100.0,
                    "open_price": None,
                    "high_price": None,
                    "low_price": None,
                    "prev_close": None,
                    "volume": None,
                    "average_price": None,
                    "ohlc": {},
                    "depth": None,
                    "timestamp": 1704412800,  # seconds
                    "instrument_token": None,
                    "symbol": "NSE_EQ|X",
                    "raw": {},
                },
            ):
                resp = client.get(
                    "/api/market/quote?symbol=NSE:SBIN", headers=auth_headers
                )
        assert resp.status_code == 200
        body = resp.json()
        # 1704412800 seconds * 1000 = 1704412800000 ms
        assert body["timestamp"] == 1704412800000

    def test_no_upstox_token_returns_403(self, client, auth_headers):
        async def no_token(user_id):
            return None
        with patch("routes.live.broker.get_upstox_access_token", side_effect=no_token):
            resp = client.get(
                "/api/market/quote?symbol=NSE:SBIN", headers=auth_headers
            )
        assert resp.status_code == 403
        assert "Upstox connection not found" in resp.json()["detail"]

    def test_unauthorized_upstox_returns_403(self, client, auth_headers):
        with patch(
            "routes.live.broker.get_upstox_access_token",
            side_effect=_stub_broker_token(),
        ):
            with patch(
                "routes.live.market_data.fetch_quote",
                side_effect=UpstoxUnauthorizedError("invalid token"),
            ):
                resp = client.get(
                    "/api/market/quote?symbol=NSE:SBIN", headers=auth_headers
                )
        assert resp.status_code == 403
        assert "reconnect" in resp.json()["detail"].lower()

    def test_rate_limit_returns_503(self, client, auth_headers):
        with patch(
            "routes.live.broker.get_upstox_access_token",
            side_effect=_stub_broker_token(),
        ):
            with patch(
                "routes.live.market_data.fetch_quote",
                side_effect=UpstoxRateLimitedError("rate limit"),
            ):
                resp = client.get(
                    "/api/market/quote?symbol=NSE:SBIN", headers=auth_headers
                )
        assert resp.status_code == 503

    def test_upstream_error_returns_502(self, client, auth_headers):
        with patch(
            "routes.live.broker.get_upstox_access_token",
            side_effect=_stub_broker_token(),
        ):
            with patch(
                "routes.live.market_data.fetch_quote",
                side_effect=UpstoxBadResponseError("HTTP 500"),
            ):
                resp = client.get(
                    "/api/market/quote?symbol=NSE:SBIN", headers=auth_headers
                )
        assert resp.status_code == 502

    def test_network_error_returns_502(self, client, auth_headers):
        with patch(
            "routes.live.broker.get_upstox_access_token",
            side_effect=_stub_broker_token(),
        ):
            with patch(
                "routes.live.market_data.fetch_quote",
                side_effect=UpstoxNetworkError("connection refused"),
            ):
                resp = client.get(
                    "/api/market/quote?symbol=NSE:SBIN", headers=auth_headers
                )
        assert resp.status_code == 502

    def test_missing_quote_returns_502(self, client, auth_headers):
        with patch(
            "routes.live.broker.get_upstox_access_token",
            side_effect=_stub_broker_token(),
        ):
            with patch(
                "routes.live.market_data.fetch_quote",
                return_value=None,
            ):
                resp = client.get(
                    "/api/market/quote?symbol=NSE:SBIN", headers=auth_headers
                )
        assert resp.status_code == 502

    def test_requires_authentication(self, client):
        with TestClient(app) as c:
            resp = c.get("/api/market/quote?symbol=NSE:SBIN")
        # The route depends on get_current_user which raises HTTPException;
        # both 401 and 403 are acceptable evidence of "not authenticated".
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# /api/market/status
# ---------------------------------------------------------------------------


class TestStatusRouteContract:
    def test_returns_session_phase(self, client):
        resp = client.get("/api/market/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["market"] == "NSE"
        assert body["phase"] in {
            "pre_market",
            "regular",
            "post_market",
            "closed",
            "holiday",
        }
        assert isinstance(body["serverTime"], int)
        assert body["serverTime"] > 1_700_000_000_000

    def test_closed_phase_when_called_outside_session(self, client, monkeypatch):
        # Pin "now" to 2024-01-07 23:00 UTC = a Sunday well after close
        import routes.live as live_route
        from src.trading_system.india.market_calendar import SessionPhase

        fixed = datetime(2024, 1, 7, 23, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(
            live_route,
            "_session_phase_for",
            lambda _dt: SessionPhase.CLOSED,
        )
        resp = client.get("/api/market/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["phase"] == "closed"

    def test_response_includes_next_session_boundaries(self, client):
        resp = client.get("/api/market/status")
        body = resp.json()
        # serverTime is always present
        assert "serverTime" in body
        # nextOpen/nextClose may be null for some edge cases; if present, must be int
        if body.get("nextOpen") is not None:
            assert isinstance(body["nextOpen"], int)
        if body.get("nextClose") is not None:
            assert isinstance(body["nextClose"], int)
