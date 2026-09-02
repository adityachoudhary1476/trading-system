"""Tests for the trading runtime manager."""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from runtime import TradingRuntime, RuntimeState, RuntimeStateEnum, get_trading_runtime, reset_trading_runtime


class TestRuntimeState:
    """Test RuntimeState dataclass."""

    def test_default_state(self):
        state = RuntimeState()
        assert state.state == RuntimeStateEnum.STOPPED
        assert state.connected is False
        assert state.last_event_time is None
        assert state.events_received == 0
        assert state.candles_generated == 0
        assert state.errors == 0
        assert state.reconnect_attempts == 0
        assert state.started_at is None


class TestTradingRuntime:
    """Test TradingRuntime manager."""

    def setup_method(self):
        """Reset runtime before each test."""
        reset_trading_runtime()

    def teardown_method(self):
        """Clean up after each test."""
        reset_trading_runtime()

    def test_singleton(self):
        """Test that get_trading_runtime returns the same instance."""
        runtime1 = get_trading_runtime()
        runtime2 = get_trading_runtime()
        assert runtime1 is runtime2

    def test_initial_state(self):
        """Test initial runtime state."""
        runtime = get_trading_runtime()
        assert runtime.state.state == RuntimeStateEnum.STOPPED
        assert runtime.is_connected is False
        assert runtime.health_monitor is None
        assert runtime.pipeline is None

    def test_get_pipeline_status_not_running(self):
        """Test pipeline status when not running."""
        runtime = get_trading_runtime()
        status = runtime.get_pipeline_status()

        assert status["status"] == "stopped"
        assert status["connected"] is False

    def test_record_event(self):
        """Test recording events."""
        runtime = get_trading_runtime()
        runtime.record_event()

        assert runtime.state.events_received == 1
        assert runtime.state.last_event_time is not None

    def test_record_candle(self):
        """Test recording candles."""
        runtime = get_trading_runtime()
        runtime.record_candle()

        assert runtime.state.candles_generated == 1

    @patch("src.trading_system.india.live_pipeline.LiveMarketPipeline")
    @patch("src.trading_system.india.upstox.UpstoxMarketDataProvider")
    @patch("src.trading_system.india.data_health.DataHealthMonitor")
    @patch("runtime.get_settings")
    def test_start_runtime(self, mock_get_settings, mock_monitor_class, mock_provider_class, mock_pipeline_class):
        """Test starting the runtime."""
        # Mock settings to provide client_id
        mock_settings = MagicMock()
        mock_settings.upstox_client_id = "test_client_id"
        mock_get_settings.return_value = mock_settings

        mock_pipeline = MagicMock()
        mock_pipeline_class.return_value = mock_pipeline

        mock_monitor = MagicMock()
        mock_monitor_class.return_value = mock_monitor

        mock_provider = MagicMock()
        mock_provider_class.return_value = mock_provider

        runtime = get_trading_runtime()

        # Mock the WebSocket thread to avoid actual connection
        with patch.object(runtime, "_start_websocket_thread"):
            runtime.start("test_token", ["NSE:SBIN"], "1d")

        assert runtime.state.state == RuntimeStateEnum.STARTING
        assert runtime.pipeline is not None
        assert runtime.health_monitor is not None

    @patch("src.trading_system.india.live_pipeline.LiveMarketPipeline")
    @patch("src.trading_system.india.upstox.UpstoxMarketDataProvider")
    @patch("src.trading_system.india.data_health.DataHealthMonitor")
    @patch("runtime.get_settings")
    def test_stop_runtime(self, mock_get_settings, mock_monitor_class, mock_provider_class, mock_pipeline_class):
        """Test stopping the runtime."""
        # Mock settings to provide client_id
        mock_settings = MagicMock()
        mock_settings.upstox_client_id = "test_client_id"
        mock_get_settings.return_value = mock_settings

        mock_pipeline = MagicMock()
        mock_pipeline_class.return_value = mock_pipeline

        mock_monitor = MagicMock()
        mock_monitor_class.return_value = mock_monitor

        mock_provider = MagicMock()
        mock_provider_class.return_value = mock_provider

        runtime = get_trading_runtime()

        # Start then stop
        with patch.object(runtime, "_start_websocket_thread"):
            runtime.start("test_token", ["NSE:SBIN"], "1d")

        runtime.stop()

        assert runtime.state.state == RuntimeStateEnum.STOPPED
        mock_pipeline.stop.assert_called_once()

    def test_reset_runtime(self):
        """Test resetting the runtime."""
        runtime = get_trading_runtime()
        reset_trading_runtime()

        # After reset, should get a new instance
        new_runtime = get_trading_runtime()
        assert new_runtime is not runtime

    @patch("runtime.get_settings")
    def test_start_without_client_id(self, mock_get_settings):
        """Test that starting without client_id raises RuntimeError."""
        # Mock settings without client_id
        mock_settings = MagicMock()
        mock_settings.upstox_client_id = ""
        mock_get_settings.return_value = mock_settings

        runtime = get_trading_runtime()

        with pytest.raises(RuntimeError, match="UPSTOX_CLIENT_ID is not configured"):
            runtime.start("test_token", ["NSE:SBIN"], "1d")
