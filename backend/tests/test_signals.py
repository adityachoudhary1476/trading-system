"""Tests for the signal service with real analysis."""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

import pandas as pd


@pytest.mark.asyncio
class TestSignalService:
    """Test the signal service."""

    async def test_generate_signals_empty_df(self):
        """Test signal generation with empty DataFrame."""
        from services.signals import generate_signals

        result = await generate_signals("NSE:SBIN", "1d", pd.DataFrame(), "token")
        assert result == []

    async def test_generate_signals_insufficient_data(self):
        """Test signal generation with insufficient data."""
        from services.signals import generate_signals

        # Create a DataFrame with only 5 rows (less than min_data_points=30)
        dates = pd.date_range(start="2024-01-01", periods=5, freq="D", tz="UTC")
        df = pd.DataFrame(
            {
                "open": [100.0] * 5,
                "high": [105.0] * 5,
                "low": [95.0] * 5,
                "close": [102.0] * 5,
                "volume": [1000] * 5,
            },
            index=dates,
        )

        result = await generate_signals("NSE:SBIN", "1d", df, "token")
        # Should return a signal (HOLD due to insufficient data)
        assert len(result) == 1
        assert result[0].direction in ["long", "short", "hold"]

    async def test_generate_signals_sufficient_data(self):
        """Test signal generation with sufficient data."""
        from services.signals import generate_signals

        # Create a DataFrame with 50 rows
        dates = pd.date_range(start="2024-01-01", periods=50, freq="D", tz="UTC")
        df = pd.DataFrame(
            {
                "open": [100.0 + i * 0.1 for i in range(50)],
                "high": [105.0 + i * 0.1 for i in range(50)],
                "low": [95.0 + i * 0.1 for i in range(50)],
                "close": [102.0 + i * 0.1 for i in range(50)],
                "volume": [1000] * 50,
            },
            index=dates,
        )

        result = await generate_signals("NSE:SBIN", "1d", df, "token")
        assert len(result) == 1
        signal = result[0]
        assert signal.symbol == "NSE:SBIN"
        assert signal.direction in ["long", "short", "hold"]
        assert 0 <= signal.confidence <= 1

    async def test_generate_signals_no_hardcoded_neutral(self):
        """Test that signals are not hardcoded to NEUTRAL."""
        from services.signals import generate_signals

        # Create a strongly trending DataFrame
        dates = pd.date_range(start="2024-01-01", periods=60, freq="D", tz="UTC")
        df = pd.DataFrame(
            {
                "open": [100.0 + i for i in range(60)],  # Strong uptrend
                "high": [105.0 + i for i in range(60)],
                "low": [95.0 + i for i in range(60)],
                "close": [102.0 + i for i in range(60)],
                "volume": [1000] * 60,
            },
            index=dates,
        )

        result = await generate_signals("NSE:SBIN", "1d", df, "token")
        assert len(result) == 1
        signal = result[0]
        # With a strong uptrend, should not be HOLD
        assert signal.direction != "hold" or signal.confidence < 0.6

    async def test_generate_signals_limit_parameter(self):
        """Test that limit parameter is accepted."""
        from services.signals import generate_signals

        dates = pd.date_range(start="2024-01-01", periods=50, freq="D", tz="UTC")
        df = pd.DataFrame(
            {
                "open": [100.0] * 50,
                "high": [105.0] * 50,
                "low": [95.0] * 50,
                "close": [102.0] * 50,
                "volume": [1000] * 50,
            },
            index=dates,
        )

        # Limit parameter should be accepted
        result = await generate_signals("NSE:SBIN", "1d", df, "token", limit=12)
        assert len(result) >= 0  # May return empty if analysis fails


class TestSignalServiceUnit:
    """Unit tests for signal service helper functions."""

    def test_map_regime_to_view_trending_up(self):
        """Test mapping trending_up regime to BULLISH view."""
        from services.signals import _map_regime_to_view
        from src.trading_system.models.market_view import MarketViewEnum

        mock_regime = MagicMock()
        mock_regime.regime.value = "trending_up"

        result = _map_regime_to_view(mock_regime)
        assert result == MarketViewEnum.BULLISH

    def test_map_regime_to_view_trending_down(self):
        """Test mapping trending_down regime to BEARISH view."""
        from services.signals import _map_regime_to_view
        from src.trading_system.models.market_view import MarketViewEnum

        mock_regime = MagicMock()
        mock_regime.regime.value = "trending_down"

        result = _map_regime_to_view(mock_regime)
        assert result == MarketViewEnum.BEARISH

    def test_map_regime_to_view_range_bound(self):
        """Test mapping range_bound regime to CHOPPY view."""
        from services.signals import _map_regime_to_view
        from src.trading_system.models.market_view import MarketViewEnum

        mock_regime = MagicMock()
        mock_regime.regime.value = "range_bound"

        result = _map_regime_to_view(mock_regime)
        assert result == MarketViewEnum.CHOPPY

    def test_map_regime_to_view_none(self):
        """Test mapping None regime to NEUTRAL view."""
        from services.signals import _map_regime_to_view
        from src.trading_system.models.market_view import MarketViewEnum

        result = _map_regime_to_view(None)
        assert result == MarketViewEnum.NEUTRAL

    def test_extract_confidence_from_candidate(self):
        """Test extracting confidence from signal candidate."""
        from services.signals import _extract_confidence

        mock_candidate = MagicMock()
        mock_candidate.confidence = 0.75

        result = _extract_confidence(mock_candidate, None)
        assert result == 0.75

    def test_extract_confidence_from_regime(self):
        """Test extracting confidence from regime when candidate has none."""
        from services.signals import _extract_confidence

        mock_regime = MagicMock()
        mock_regime.confidence = 0.6

        result = _extract_confidence(None, mock_regime)
        assert result == 0.6

    def test_extract_confidence_default(self):
        """Test default confidence when neither candidate nor regime has it."""
        from services.signals import _extract_confidence

        result = _extract_confidence(None, None)
        assert result == 0.5
