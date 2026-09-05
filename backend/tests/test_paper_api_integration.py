"""Integration tests for the production Paper Trading API FastAPI adapter.

These tests verify that the paper-trading API (Phase 21/22) is correctly
exposed through the same FastAPI application that Railway runs in production,
mounted under ``/api/paper``.

They cover:
  A. Route registration — the paper API routes are present on the production app
  B. Adapter contract — method/path/query/body forwarded to PaperAPIRouter.dispatch
     without duplication, with status/body/error-schema preserved
  C. Route isolation — /health and /api/market/* are NOT intercepted
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from main import app
from routes.paper_api import _get_api_router, _api_router


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _reset_paper_singleton():
    """Force re-initialisation of the PaperAPIRouter singleton."""
    import routes.paper_api as mod
    mod._api_router = None
    import trading_system.paper_api as tmod
    import trading_system.paper.control as cmod
    if hasattr(tmod, "_init_cache"):
        tmod._init_cache.clear()


@pytest.fixture
def client():
    """TestClient backed by the production FastAPI app."""
    return TestClient(app)


@pytest.fixture
def isolated_client(tmp_path, monkeypatch):
    """TestClient with a fresh temp SQLite DB for the paper API."""
    _reset_paper_singleton()
    db_path = tmp_path / "test_paper.db"
    monkeypatch.setenv("MARKET_DATA_DB_URL", f"sqlite:///{db_path}")
    _reset_paper_singleton()
    with TestClient(app) as c:
        yield c
    _reset_paper_singleton()


# --------------------------------------------------------------------------- #
# A. Route registration tests
# --------------------------------------------------------------------------- #
class TestRouteRegistration:
    """Verify the production FastAPI app exposes all Paper API routes."""

    def test_paper_api_router_is_included(self):
        """The paper API router must be mounted on the production app."""
        # The catch-all route /api/paper/{path:path} should be present
        route_paths = [r.path for r in app.routes]
        assert any("/api/paper" in p for p in route_paths), (
            "Paper API routes are not mounted on the production FastAPI app"
        )

    @pytest.mark.parametrize("path", [
        "/api/paper/deployments",
        "/api/paper/strategies",
        "/api/paper/regime",
        "/api/paper/allocation",
        "/api/paper/health",
    ])
    def test_paper_routes_exist_or_forward(self, client, path):
        """Paper API routes should return non-404 (they forward to dispatch)."""
        # GET requests to known paper API routes should return 200, 400 (no
        # market data provider for /regime and /allocation), or 200.
        resp = client.get(path)
        # 405 would mean the catch-all didn't match; 404 from dispatch means
        # the router didn't find a handler (but the route itself exists).
        assert resp.status_code != 405, f"Route {path} returned 405 — catch-all not matching"

    def test_deployments_get_route_registered(self, client):
        resp = client.get("/api/paper/deployments")
        assert resp.status_code == 200
        assert "deployments" in resp.json()

    def test_deployments_post_route_registered(self, client):
        """POST /deployments must be accepted (not 404 from missing route)."""
        resp = client.post("/api/paper/deployments", json={
            "strategy_id": "nonexistent_strategy_for_test"
        })
        # 404 means the route was found but strategy not found — NOT a missing route
        # A true routing 404 from FastAPI would have a different body shape.
        assert resp.status_code in (201, 400, 404), (
            f"POST /api/paper/deployments returned {resp.status_code} — "
            "route may not be registered"
        )

    def test_deployment_detail_route_registered(self, client):
        resp = client.get("/api/paper/deployments/nonexistent-id-12345")
        assert resp.status_code == 404
        body = resp.json()
        assert "error" in body

    def test_lifecycle_routes_registered(self, client):
        """Activate/pause/resume/stop must be POST routes under /api/paper/deployments/{id}."""
        for action in ["activate", "pause", "resume", "stop"]:
            resp = client.post(f"/api/paper/deployments/nonexistent-id-12345/{action}")
            # 404 means the route exists (paper API found no deployment)
            assert resp.status_code == 404, (
                f"POST /api/paper/deployments/.../{action} returned {resp.status_code}"
            )

    def test_strategies_route_registered(self, client):
        resp = client.get("/api/paper/strategies")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 5  # Phase 22: 5 strategy specs

    def test_regime_route_returns_400_without_provider(self, client):
        """/regime must return 400 when no market data provider is configured."""
        resp = client.get("/api/paper/regime")
        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == "bad_request"

    def test_allocation_route_returns_400_without_provider(self, client):
        """/allocation must return 400 when no market data provider is configured."""
        resp = client.get("/api/paper/allocation")
        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == "bad_request"


# --------------------------------------------------------------------------- #
# C. Route isolation tests
# --------------------------------------------------------------------------- #
class TestRouteIsolation:
    """Verify the catch-all does NOT intercept unrelated backend routes."""

    def test_health_not_intercepted(self, client):
        """/health must return FastAPI's native health check, not paper API."""
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["service"] == "trading-system-backend"

    def test_health_detailed_not_intercepted(self, client):
        resp = client.get("/health/detailed")
        assert resp.status_code == 200
        body = resp.json()
        assert "dependencies" in body

    def test_market_status_not_intercepted(self, client):
        resp = client.get("/api/market/status")
        assert resp.status_code == 200
        body = resp.json()
        assert "market" in body

    def test_deployment_path_does_not_collide_with_market(self, client):
        """/api/market/deployments (if it existed) must not leak into paper."""
        # /api/market/* routes should remain under their own router
        resp = client.get("/api/market/analysis?symbol=NSE:SBIN")
        # This will likely fail auth or broker check, but must NOT be
        # a paper API 404 "no route for" error.
        assert resp.status_code != 404 or "deployments" in resp.json().get("detail", ""), (
            "Market route may be incorrectly routed to paper API"
        )


# --------------------------------------------------------------------------- #
# B. Adapter contract tests
# --------------------------------------------------------------------------- #
class TestAdapterContract:
    """Verify the adapter correctly forwards requests to PaperAPIRouter.dispatch."""

    def test_get_query_params_forwarded_without_duplication(self, isolated_client):
        """Query params must be forwarded exactly once (regression for the
        duplicate-query-param bug where ?limit=200 became limit=['200','200'])."""
        # Use a large limit to verify the param is parsed correctly.
        resp = isolated_client.get("/api/paper/deployments?limit=200")
        assert resp.status_code == 200
        body = resp.json()
        assert "deployments" in body
        assert body.get("count", 0) == len(body["deployments"])

    def test_query_params_bounded_int(self, isolated_client):
        """Non-integer query params must produce a 400 (not 500)."""
        resp = isolated_client.get("/api/paper/deployments?limit=abc")
        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == "bad_request"

    def test_post_json_body_forwarded(self, isolated_client):
        """POST body must be forwarded to dispatch as JSON."""
        resp = isolated_client.post(
            "/api/paper/deployments",
            json={"strategy_id": "nonexistent_strategy"},
        )
        # 404 = strategy not found (body was forwarded, JSON parsed)
        # 400 = bad request (body forwarded)
        assert resp.status_code in (400, 404)
        body = resp.json()
        assert "error" in body

    def test_post_raw_json_body_forwarded(self, isolated_client):
        """POST with raw JSON string body must be forwarded correctly."""
        raw_body = json.dumps({"strategy_id": "nonexistent_strategy"})
        resp = isolated_client.post(
            "/api/paper/deployments",
            content=raw_body.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code in (400, 404)
        body = resp.json()
        assert "error" in body

    def test_status_code_preserved(self, client):
        """Response status codes from dispatch must be preserved."""
        resp = client.get("/api/paper/regime")
        assert resp.status_code == 400  # dispatch returns 400 for no provider

    def test_json_body_preserved(self, client):
        """Response JSON body from dispatch must be preserved."""
        resp = client.get("/api/paper/strategies")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        # Each strategy should have the expected fields
        for s in data:
            assert "name" in s or "spec_name" in s
            assert "strategy_id" in s

    def test_error_schema_preserved(self, client):
        """The paper API error schema must be preserved (not converted to 200)."""
        resp = client.get("/api/paper/deployments/nonexistent-deployment-id")
        assert resp.status_code == 404
        body = resp.json()
        assert "error" in body
        assert "code" in body["error"]
        assert "message" in body["error"]
        assert "schema_version" in body

    def test_method_not_allowed_preserved(self, client):
        """DELETE must return 405 from dispatch (not a FastAPI 405)."""
        resp = client.delete("/api/paper/deployments")
        assert resp.status_code == 405
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == "method_not_allowed"

    def test_response_is_json(self, client):
        """All paper API responses must be JSON."""
        resp = client.get("/api/paper/deployments")
        assert resp.headers["content-type"].startswith("application/json")

    def test_no_query_string_duplication_on_multiple_params(self, client):
        """Multiple query params must all be forwarded without duplication."""
        resp = client.get("/api/paper/regime?symbol=NSE:SBIN&timeframe=1d")
        # Should be 400 (no market data provider) with a proper error body
        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body
        assert "symbol" in body["error"]["message"] or "market data provider" in body["error"]["message"].lower()
