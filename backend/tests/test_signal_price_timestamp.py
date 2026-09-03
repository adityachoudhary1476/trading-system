"""Regression tests for SignalDTO real price + timestamp contract.

The Signals tab was showing ``₹0.00`` for price and ``—`` for the
timestamp.  Root causes:

1. ``_signal_to_dto`` used ``getattr(signal, "price", 0.0)`` and the
   underlying ``Signal`` dataclass has no ``price`` attribute, so the
   fallback ``0.0`` was always returned.
2. ``_signal_to_dto`` used ``int(time.time() * 1000)`` (wall clock) for
   the timestamp, but the FastAPI route did not emit the camelCase key
   the frontend reads (``s.generatedAt``), so the value was dropped at
   the client.  The user-visible cell then rendered ``—``.

These tests pin the corrected behaviour: a finite non-zero close price
propagates from the latest candle, the timestamp originates from the
source bar (not the wall clock), and missing/invalid values cause the
signal to be dropped instead of fabricated.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from main import app
from auth import get_current_user
from services import signals as signals_service
from services.market_data import (
    UpstoxBadResponseError,
    UpstoxMarketDataError,
)


# ---------------------------------------------------------------------------
# _signal_to_dto unit tests
# ---------------------------------------------------------------------------


def _make_snapshot(
    close: float | None,
    bar_timestamp: datetime,
    market_view: str = "bullish",
    direction_value: str = "long",
    reason: str = "test reason",
    confidence: float = 0.7,
):
    """Build a minimal snapshot-like object for _signal_to_dto."""
    snap = MagicMock()
    snap.latest_price = close
    snap.timestamp = bar_timestamp
    return snap


def _make_signal(direction_value: str = "long", reason: str = "test reason", confidence: float = 0.7):
    sig = MagicMock()
    sig.direction.value = direction_value
    sig.confidence = confidence
    sig.reason = reason
    sig.market_view = "bullish"
    sig.source = "deterministic"
    return sig


class TestSignalToDtoContract:
    def test_price_is_source_candle_close(self):
        bar = datetime(2024, 1, 5, 0, 0, tzinfo=timezone.utc)
        snap = _make_snapshot(close=21701.95, bar_timestamp=bar)
        sig = _make_signal()

        dto = signals_service._signal_to_dto(sig, "NSE:NIFTY50", snap)

        assert dto is not None
        assert dto.price == 21701.95
        assert isinstance(dto.price, float)

    def test_timestamp_is_source_bar_not_wall_clock(self):
        bar = datetime(2024, 1, 5, 0, 0, tzinfo=timezone.utc)
        snap = _make_snapshot(close=21701.95, bar_timestamp=bar)
        sig = _make_signal()

        with patch("time.time", return_value=9999999999.0):
            dto = signals_service._signal_to_dto(sig, "NSE:NIFTY50", snap)

        assert dto is not None
        # epoch ms of 2024-01-05T00:00:00Z = 1704412800000
        assert dto.timestamp == 1704412800000
        # Must NOT equal the wall clock value
        assert dto.timestamp != int(9999999999.0 * 1000)

    def test_naive_timestamp_is_treated_as_utc(self):
        bar = datetime(2024, 1, 5, 0, 0)  # no tzinfo
        snap = _make_snapshot(close=100.0, bar_timestamp=bar)
        sig = _make_signal()

        dto = signals_service._signal_to_dto(sig, "NSE:SBIN", snap)

        assert dto is not None
        assert dto.timestamp == 1704412800000  # treated as UTC

    def test_serializes_as_generatedAt_camel_case(self):
        bar = datetime(2024, 1, 5, 0, 0, tzinfo=timezone.utc)
        snap = _make_snapshot(close=100.0, bar_timestamp=bar)
        sig = _make_signal()

        dto = signals_service._signal_to_dto(sig, "NSE:SBIN", snap)

        assert dto is not None
        dumped = dto.model_dump(by_alias=True)
        assert "generatedAt" in dumped
        assert "timestamp" not in dumped
        assert dumped["generatedAt"] == 1704412800000
        assert dumped["price"] == 100.0

    def test_serializes_with_python_field_name_too(self):
        bar = datetime(2024, 1, 5, 0, 0, tzinfo=timezone.utc)
        snap = _make_snapshot(close=100.0, bar_timestamp=bar)
        sig = _make_signal()

        dto = signals_service._signal_to_dto(sig, "NSE:SBIN", snap)

        assert dto is not None
        dumped = dto.model_dump(by_alias=False)
        assert dumped["timestamp"] == 1704412800000

    def test_missing_price_returns_none_to_drop_signal(self):
        bar = datetime(2024, 1, 5, 0, 0, tzinfo=timezone.utc)
        snap = _make_snapshot(close=None, bar_timestamp=bar)
        sig = _make_signal()

        dto = signals_service._signal_to_dto(sig, "NSE:SBIN", snap)

        assert dto is None

    def test_zero_price_returns_none_to_drop_signal(self):
        bar = datetime(2024, 1, 5, 0, 0, tzinfo=timezone.utc)
        snap = _make_snapshot(close=0.0, bar_timestamp=bar)
        sig = _make_signal()

        dto = signals_service._signal_to_dto(sig, "NSE:SBIN", snap)

        assert dto is None

    def test_negative_price_returns_none_to_drop_signal(self):
        bar = datetime(2024, 1, 5, 0, 0, tzinfo=timezone.utc)
        snap = _make_snapshot(close=-1.0, bar_timestamp=bar)
        sig = _make_signal()

        dto = signals_service._signal_to_dto(sig, "NSE:SBIN", snap)

        assert dto is None

    def test_non_finite_price_returns_none_to_drop_signal(self):
        bar = datetime(2024, 1, 5, 0, 0, tzinfo=timezone.utc)
        snap = _make_snapshot(close=float("nan"), bar_timestamp=bar)
        sig = _make_signal()

        dto = signals_service._signal_to_dto(sig, "NSE:SBIN", snap)

        assert dto is None

    def test_missing_timestamp_returns_none_to_drop_signal(self):
        snap = MagicMock()
        snap.latest_price = 100.0
        snap.timestamp = None
        sig = _make_signal()

        dto = signals_service._signal_to_dto(sig, "NSE:SBIN", snap)

        assert dto is None


# ---------------------------------------------------------------------------
# generate_signals end-to-end through real DataFrame
# ---------------------------------------------------------------------------


def _make_uptrend_df(periods: int = 60) -> pd.DataFrame:
    dates = pd.date_range(start="2024-01-01", periods=periods, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": [100.0 + i * 1.0 for i in range(periods)],
            "high": [105.0 + i * 1.0 for i in range(periods)],
            "low": [99.0 + i * 1.0 for i in range(periods)],
            "close": [102.0 + i * 1.0 for i in range(periods)],
            "volume": [1_000_000] * periods,
        },
        index=dates,
    )


class TestGenerateSignalsEndToEnd:
    @pytest.mark.asyncio
    async def test_returns_real_finite_price(self):
        df = _make_uptrend_df(60)
        result = await signals_service.generate_signals("NSE:SBIN", "1d", df, "fake-bearer")
        assert len(result) == 1
        assert math.isfinite(result[0].price)
        assert result[0].price > 0
        # Must be the last close in the source DataFrame
        assert result[0].price == float(df["close"].iloc[-1])

    @pytest.mark.asyncio
    async def test_returns_source_bar_timestamp_not_wall_clock(self):
        df = _make_uptrend_df(60)
        with patch("time.time", return_value=9999999999.0):
            result = await signals_service.generate_signals(
                "NSE:SBIN", "1d", df, "fake-bearer"
            )
        assert len(result) == 1
        last_ts = df.index[-1]
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize("UTC")
        expected_ms = int(last_ts.timestamp() * 1000)
        assert result[0].timestamp == expected_ms
        assert result[0].timestamp != int(9999999999.0 * 1000)

    @pytest.mark.asyncio
    async def test_dto_has_no_zero_price(self):
        df = _make_uptrend_df(60)
        result = await signals_service.generate_signals("NSE:SBIN", "1d", df, "fake-bearer")
        assert len(result) == 1
        assert result[0].price != 0.0
        assert result[0].price is not None

    @pytest.mark.asyncio
    async def test_dumped_json_uses_generatedAt_camel_case(self):
        df = _make_uptrend_df(60)
        result = await signals_service.generate_signals("NSE:SBIN", "1d", df, "fake-bearer")
        assert len(result) == 1
        dumped = result[0].model_dump(by_alias=True)
        # Must NOT contain the snake_case key
        assert "timestamp" not in dumped or "generatedAt" in dumped
        # Must contain the camelCase key
        assert "generatedAt" in dumped
        assert isinstance(dumped["generatedAt"], int)
        assert dumped["generatedAt"] > 0


# ---------------------------------------------------------------------------
# Route-level: the JSON wire format must use camelCase and contain real data
# ---------------------------------------------------------------------------


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
    return {"Authorization": "Bearer fake-bearer-not-a-real-credential"}


def _settings_universe(monkeypatch, value: str):
    monkeypatch.setenv("SIGNAL_UNIVERSE", value)
    from config import get_settings
    get_settings.cache_clear()


class TestSignalsRouteWireFormat:
    def test_response_uses_generatedAt_camel_case(
        self, client, auth_headers, monkeypatch
    ):
        _settings_universe(monkeypatch, "NSE:SBIN")
        df = _make_uptrend_df(60)

        with patch("routes.signals.broker.get_upstox_access_token") as mock_token:
            mock_token.return_value = "fake-bearer"
            with patch(
                "routes.signals.market_data.fetch_ohlcv", return_value=df
            ):
                response = client.get(
                    "/api/market/signals?limit=1", headers=auth_headers
                )

        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)
        assert len(body) == 1
        signal = body[0]
        # camelCase wire format
        assert "generatedAt" in signal
        assert "timestamp" not in signal
        # finite, non-zero price
        assert isinstance(signal["price"], (int, float))
        assert math.isfinite(signal["price"])
        assert signal["price"] > 0
        # positive timestamp in epoch ms
        assert isinstance(signal["generatedAt"], int)
        assert signal["generatedAt"] > 0

    def test_response_never_contains_zero_price(
        self, client, auth_headers, monkeypatch
    ):
        _settings_universe(monkeypatch, "NSE:SBIN")
        df = _make_uptrend_df(60)

        with patch("routes.signals.broker.get_upstox_access_token") as mock_token:
            mock_token.return_value = "fake-bearer"
            with patch(
                "routes.signals.market_data.fetch_ohlcv", return_value=df
            ):
                response = client.get(
                    "/api/market/signals?limit=1", headers=auth_headers
                )

        assert response.status_code == 200
        for sig in response.json():
            assert sig["price"] != 0, f"signal has zero price: {sig}"

    def test_response_includes_contract_fields(
        self, client, auth_headers, monkeypatch
    ):
        _settings_universe(monkeypatch, "NSE:SBIN")
        df = _make_uptrend_df(60)

        with patch("routes.signals.broker.get_upstox_access_token") as mock_token:
            mock_token.return_value = "fake-bearer"
            with patch(
                "routes.signals.market_data.fetch_ohlcv", return_value=df
            ):
                response = client.get(
                    "/api/market/signals?limit=1", headers=auth_headers
                )

        assert response.status_code == 200
        signal = response.json()[0]
        # All contract fields must be present and non-null
        for field_name in [
            "id", "symbol", "direction", "confidence", "price",
            "bias", "reason", "generatedAt", "source",
        ]:
            assert field_name in signal, f"missing field: {field_name}"
            assert signal[field_name] is not None, f"null field: {field_name}"
