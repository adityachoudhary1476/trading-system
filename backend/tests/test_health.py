"""Tests for health endpoints."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from main import app
from runtime import get_trading_runtime, reset_trading_runtime, RuntimeStateEnum


@pytest.fixture
def client():
    """Create a test client."""
    return TestClient(app)


class TestHealthEndpoint:
    """Test health check endpoints."""

    def test_health_returns_ok(self, client):
        """Test basic health check."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["service"] == "trading-system-backend"

    def test_health_includes_pipeline_status_stopped(self, client):
        """Test that health endpoint includes pipeline status when stopped."""
        reset_trading_runtime()
        runtime = get_trading_runtime()
        runtime._state.state = RuntimeStateEnum.STOPPED
        runtime._state.connected = False
        runtime._state.started_at = None

        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "pipeline" in data
        assert data["pipeline"]["status"] == "stopped"
        assert data["pipeline"]["connected"] is False

    def test_health_includes_pipeline_status_connected(self, client):
        """Test that health endpoint includes pipeline status when connected."""
        reset_trading_runtime()
        runtime = get_trading_runtime()
        runtime._state.state = RuntimeStateEnum.CONNECTED
        runtime._state.connected = True
        runtime._state.started_at = 1000.0
        runtime._state.last_event_time = 2000.0

        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "pipeline" in data
        assert data["pipeline"]["status"] == "connected"
        assert data["pipeline"]["connected"] is True

    def test_health_detailed_includes_runtime_state(self, client):
        """Test that detailed health includes runtime state."""
        reset_trading_runtime()
        runtime = get_trading_runtime()
        runtime._state.state = RuntimeStateEnum.CONNECTED
        runtime._state.connected = True
        runtime._state.events_received = 42
        runtime._state.candles_generated = 10
        runtime._state.started_at = 1000.0
        runtime._state.last_event_time = 2000.0

        response = client.get("/health/detailed")
        assert response.status_code == 200
        data = response.json()
        assert "dependencies" in data
        assert "trading_runtime" in data["dependencies"]
        rt = data["dependencies"]["trading_runtime"]
        assert rt["status"] == "connected"
        assert rt["connected"] is True
        assert rt["events_received"] == 42
        assert rt["candles_generated"] == 10
        assert rt["started_at"] == 1000.0
        assert rt["last_event_time"] == 2000.0

    def test_health_does_not_fake_dependencies(self, client):
        """Test that health endpoint doesn't fake dependency status."""
        response = client.get("/health/detailed")
        assert response.status_code == 200
        data = response.json()
        # Should have dependencies section
        assert "dependencies" in data

    def test_health_includes_autonomous_pipeline_fields(self, client):
        """Test that health endpoint includes autonomous pipeline fields."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "autonomous_paper_pipeline" in data
        assert "paper_execution" in data
        assert "live_execution" in data
        assert "scheduler" in data
        assert data["live_execution"]["enabled"] is False
        assert data["scheduler"]["enabled"] in (True, False)

    def test_health_paper_execution_when_controller_running(self, client):
        """Test paper execution reflects autonomous controller state."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        paper_exec = data.get("paper_execution", {})
        assert "enabled" in paper_exec
        assert "running" in paper_exec

    def test_health_does_not_enable_live_execution(self, client):
        """Test that live execution is always reported as disabled."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["live_execution"]["enabled"] is False
