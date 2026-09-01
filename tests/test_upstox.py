"""Tests for Upstox market-data adapter (no network, no real credentials)."""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from trading_system.india.upstox import (
    UpstoxMarketDataProvider,
    UpstoxDataSocket,
    UpstoxAuthError,
    UpstoxAPIError,
    UpstoxNetworkError,
    UpstoxRateLimitError,
)
from trading_system.data.provider_exports import get_provider
from trading_system.data.validation import validate_ohlcv


def _upstox_history_response():
    epoch = 1609459200  # 2021-01-01
    return {
        "status": "success",
        "data": {
            "candles": [
                [epoch, 100.0, 102.0, 99.0, 101.0, 1000.0, 5000.0],
                [epoch + 86400, 101.0, 103.0, 100.0, 102.0, 1100.0, 5100.0],
            ]
        }
    }


def test_upstox_is_a_marketdataprovider():
    prov = get_provider("upstox")
    assert isinstance(prov, UpstoxMarketDataProvider)
    assert prov.name == "upstox"


def test_upstox_historical_normalization_shape(monkeypatch):
    prov = UpstoxMarketDataProvider(client_id="X-100", access_token="tok")
    monkeypatch.setattr(prov, "_get", lambda path, params=None: _upstox_history_response())
    df = prov.get_historical("NSE:SBIN", "1d", 2)
    assert len(df) == 2
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is not None
    report = validate_ohlcv(df, "1d")
    assert report.ok


def test_upstox_requires_auth_for_live(monkeypatch):
    monkeypatch.delenv("UPSTOX_CLIENT_ID", raising=False)
    monkeypatch.delenv("UPSTOX_ACCESS_TOKEN", raising=False)

    prov = UpstoxMarketDataProvider()
    assert not prov.is_authenticated

    with pytest.raises(RuntimeError):
        prov.connect_live(["NSE:SBIN"], on_event=lambda e: None)


def test_upstox_symbol_resolution_without_creds():
    prov = UpstoxMarketDataProvider()
    assert prov._upstox_symbol("NSE:SBIN") == "NSE_EQ|SBIN"
    assert prov._upstox_symbol("NSE:NIFTY50") == "NSE_INDEX|NIFTY50"


def test_upstox_ws_message_normalization(monkeypatch):
    prov = UpstoxMarketDataProvider(client_id="X-100", access_token="tok")
    sock = object.__new__(UpstoxDataSocket)
    sock._up_to_internal = {"NSE_EQ|SBIN": "NSE:SBIN"}
    sock.provider = prov
    sock.on_event = None
    msg = {"symbol": "NSE_EQ|SBIN", "last_price": 555.5, "open_price": 550.0,
           "high_price": 560.0, "low_price": 548.0, "volume": 123.0}
    ev = sock._normalize(msg)
    assert ev is not None
    assert ev.symbol == "NSE:SBIN"
    assert ev.ltp == 555.5


def test_upstox_ws_control_frame_skipped():
    prov = UpstoxMarketDataProvider(client_id="X-100", access_token="tok")
    sock = object.__new__(UpstoxDataSocket)
    sock._up_to_internal = {"NSE_EQ|SBIN": "NSE:SBIN"}
    sock.provider = prov
    sock.on_event = None
    assert sock._normalize({"type": "ack"}) is None
    assert sock._normalize("garbage") is None
