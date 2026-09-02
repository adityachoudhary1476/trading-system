"""Tests for the pipeline service with shared runtime state."""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from runtime import get_trading_runtime, reset_trading_runtime


@pytest.mark.asyncio
class TestPipelineService:
    """Test the pipeline service."""

    def setup_method(self):
        """Reset runtime before each test."""
        reset_trading_runtime()

    def teardown_method(self):
        """Clean up after each test."""
        reset_trading_runtime()

    async def test_pipeline_status_not_running(self):
        """Test pipeline status when runtime is not running."""
        from services.pipeline import get_pipeline_status

        result = await get_pipeline_status("user123", "token")

        # Should return honest degraded state
        assert len(result) >= 1
        assert any(s.id == "live-pipeline" for s in result)
        live_stage = next(s for s in result if s.id == "live-pipeline")
        assert live_stage.status == "disconnected"

    async def test_pipeline_status_running(self):
        """Test pipeline status when runtime is running."""
        from services.pipeline import get_pipeline_status
        from runtime import get_trading_runtime, RuntimeStateEnum

        runtime = get_trading_runtime()

        # Mock the runtime state - set to CONNECTED
        runtime._state.state = RuntimeStateEnum.CONNECTED
        runtime._state.connected = True
        runtime._state.events_received = 100
        runtime._state.candles_generated = 50

        mock_monitor = MagicMock()
        mock_monitor.snapshot.return_value = {
            "status": "healthy",
            "connected": True,
            "events_received": 100,
            "events_rejected": 0,
            "candles_generated": 50,
            "latest_event_ts": 1704067200.0,
            "latest_closed_candle": 1704067200.0,
        }
        runtime._health_monitor = mock_monitor

        result = await get_pipeline_status("user123", "token")

        # Should return actual pipeline state
        assert len(result) == 4
        connection_stage = next(s for s in result if s.id == "upstox-connection")
        assert connection_stage.status == "healthy"

    async def test_pipeline_status_no_per_request_monitor(self):
        """Test that pipeline service doesn't create a new monitor per request."""
        from services.pipeline import get_pipeline_status
        from runtime import get_trading_runtime

        runtime = get_trading_runtime()

        # First call
        result1 = await get_pipeline_status("user123", "token")

        # Get the monitor reference
        monitor1 = runtime.health_monitor

        # Second call
        result2 = await get_pipeline_status("user456", "token")

        # Monitor should be the same (not created per request)
        monitor2 = runtime.health_monitor
        assert monitor1 is monitor2

    async def test_pipeline_status_user_isolation(self):
        """Test that pipeline status doesn't expose user-specific data."""
        from services.pipeline import get_pipeline_status

        # Both users should get the same pipeline status (shared runtime)
        result1 = await get_pipeline_status("user1", "token1")
        result2 = await get_pipeline_status("user2", "token2")

        # Status should be the same (shared pipeline)
        assert len(result1) == len(result2)
        for s1, s2 in zip(result1, result2):
            assert s1.status == s2.status

    async def test_pipeline_status_error_handling(self):
        """Test pipeline service error handling."""
        from services.pipeline import get_pipeline_status
        from runtime import get_trading_runtime

        runtime = get_trading_runtime()

        # Set up a failing monitor
        runtime._state.running = True
        mock_monitor = MagicMock()
        mock_monitor.snapshot.side_effect = Exception("Monitor error")
        runtime._health_monitor = mock_monitor

        # Should not raise, should return minimal status
        result = await get_pipeline_status("user123", "token")
        assert len(result) >= 1
        assert result[0].id == "service"
