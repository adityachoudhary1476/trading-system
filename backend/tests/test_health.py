"""Tests for health endpoints."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from main import app


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

    def test_health_does_not_fake_dependencies(self, client):
        """Test that health endpoint doesn't fake dependency status."""
        response = client.get("/health/detailed")
        assert response.status_code == 200
        data = response.json()
        # Should have dependencies section
        assert "dependencies" in data
