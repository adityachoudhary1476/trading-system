"""Unit tests for the market_data.fetch_quote helper.

These pin the field mapping (ohlc.* / cp / average_price / volume /
timestamp) and verify that fabricated / non-finite values are
explicitly rejected (never zeroed).
"""
from __future__ import annotations

import math
from unittest.mock import MagicMock, patch

import pytest
import requests

from services import market_data
from services.market_data import (
    UpstoxBadResponseError,
    UpstoxMalformedError,
    UpstoxNetworkError,
    fetch_quote,
)


FAKE_TOKEN = "fake-redacted-bearer-token-not-a-real-credential"


def _mock_response(status_code: int, body, headers=None) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.headers = headers or {}
    if isinstance(body, (dict, list)):
        resp.json.return_value = body
    else:
        resp.json.side_effect = ValueError("not json")
    return resp


@pytest.mark.asyncio
async def test_normalizes_full_quote():
    upstox_body = {
        "status": "success",
        "data": {
            "NSE_EQ|INE062A01020": {
                "ohlc": {"open": 800, "high": 860, "low": 795, "close": 805},
                "depth": {"buy": [], "sell": []},
                "timestamp": "2024-01-05T03:45:30+00:00",
                "instrument_token": 12345,
                "last_price": 850.5,
                "volume": 1234567,
                "average_price": 820.25,
            }
        },
    }
    mock_resp = _mock_response(200, upstox_body)
    with patch("services.market_data.requests.get", return_value=mock_resp):
        q = await fetch_quote("NSE:SBIN", FAKE_TOKEN)
    assert q is not None
    assert q["last_price"] == 850.5
    assert q["open_price"] == 800.0
    assert q["high_price"] == 860.0
    assert q["low_price"] == 795.0
    assert q["prev_close"] == 805.0
    assert q["volume"] == 1234567
    assert q["average_price"] == 820.25
    assert q["ohlc"]["open"] == 800.0
    assert q["ohlc"]["high"] == 860.0
    assert q["instrument_token"] == 12345
    assert q["symbol"] == "NSE_EQ|INE062A01020"


@pytest.mark.asyncio
async def test_missing_last_price_returns_none():
    body = {
        "status": "success",
        "data": {"NSE_EQ|X": {"ohlc": {}, "last_price": None, "volume": 0}},
    }
    with patch("services.market_data.requests.get", return_value=_mock_response(200, body)):
        assert await fetch_quote("NSE:SBIN", FAKE_TOKEN) is None


@pytest.mark.asyncio
async def test_non_finite_last_price_returns_none():
    body = {
        "status": "success",
        "data": {"NSE_EQ|X": {"ohlc": {}, "last_price": float("nan"), "volume": 0}},
    }
    with patch("services.market_data.requests.get", return_value=_mock_response(200, body)):
        assert await fetch_quote("NSE:SBIN", FAKE_TOKEN) is None


@pytest.mark.asyncio
async def test_missing_optional_fields_are_none_not_zero():
    body = {
        "status": "success",
        "data": {
            "NSE_EQ|INE062A01020": {
                "ohlc": {},
                "last_price": 100.0,
                # no volume, no average_price, no prev_close
            }
        },
    }
    with patch("services.market_data.requests.get", return_value=_mock_response(200, body)):
        q = await fetch_quote("NSE:SBIN", FAKE_TOKEN)
    assert q is not None
    assert q["last_price"] == 100.0
    assert q["prev_close"] is None
    assert q["volume"] is None
    assert q["average_price"] is None
    assert q["open_price"] is None


@pytest.mark.asyncio
async def test_non_success_body_raises():
    body = {"status": "error", "error_message": "Rate limit exceeded"}
    with patch("services.market_data.requests.get", return_value=_mock_response(200, body)):
        with pytest.raises(UpstoxBadResponseError):
            await fetch_quote("NSE:SBIN", FAKE_TOKEN)


@pytest.mark.asyncio
async def test_non_json_body_raises_malformed():
    mock_resp = _mock_response(200, "not-json-as-string")
    with patch("services.market_data.requests.get", return_value=mock_resp):
        with pytest.raises(UpstoxMalformedError):
            await fetch_quote("NSE:SBIN", FAKE_TOKEN)


@pytest.mark.asyncio
async def test_network_failure_raises_network_error():
    with patch(
        "services.market_data.requests.get",
        side_effect=requests.ConnectionError("refused"),
    ):
        with pytest.raises(UpstoxNetworkError):
            await fetch_quote("NSE:SBIN", FAKE_TOKEN)


@pytest.mark.asyncio
async def test_missing_symbol_key_returns_none():
    body = {
        "status": "success",
        "data": {"SOME_OTHER_KEY": {"last_price": 100.0}},
    }
    with patch("services.market_data.requests.get", return_value=_mock_response(200, body)):
        # The function looks for the *Upstox-formatted* key. When the
        # requested symbol's instrument is missing from the response, the
        # helper returns None rather than fabricating a quote.
        result = await fetch_quote("NSE:SBIN", FAKE_TOKEN)
        assert result is None


@pytest.mark.asyncio
async def test_bearer_token_is_attached():
    mock_resp = _mock_response(
        200,
        {
            "status": "success",
            "data": {"NSE_EQ|INE062A01020": {"last_price": 1.0, "ohlc": {}}},
        },
    )
    with patch("services.market_data.requests.get", return_value=mock_resp) as mock_get:
        await fetch_quote("NSE:SBIN", FAKE_TOKEN)
    headers = mock_get.call_args.kwargs["headers"]
    assert headers["Authorization"] == f"Bearer {FAKE_TOKEN}"


@pytest.mark.asyncio
async def test_quote_dto_populates_market_timestamp_from_upstox_ts():
    """Phase 2: QuoteDTO.from_upstox_quote must set marketTimestamp
    from the Upstox exchange timestamp (epoch seconds → epoch ms)."""
    from schemas.market import QuoteDTO

    upstox_quote = {
        "last_price": 850.5,
        "prev_close": 805.0,
        "average_price": 820.25,
        "open_price": 800.0,
        "high_price": 860.0,
        "low_price": 795.0,
        "volume": 1234567,
        "instrument_token": 12345,
        "timestamp": 1704412800,  # epoch seconds
    }
    ts_ms = 1704412800 * 1000
    dto = QuoteDTO.from_upstox_quote(
        symbol="NSE:SBIN",
        quote=upstox_quote,
        session_state="REGULAR",
        fallback_timestamp_ms=ts_ms + 1,
    )
    assert dto.timestamp == ts_ms
    assert dto.marketTimestamp == ts_ms
    assert dto.fetchedAt == ts_ms + 1


@pytest.mark.asyncio
async def test_quote_dto_falls_back_when_timestamp_missing():
    """Phase 2: when Upstox does not supply a timestamp, marketTimestamp
    falls back to fetchedAt (the server wall-clock)."""
    from schemas.market import QuoteDTO

    upstox_quote = {
        "last_price": 100.0,
        "prev_close": 99.0,
    }
    fallback = 1704412800000
    dto = QuoteDTO.from_upstox_quote(
        symbol="NSE:SBIN",
        quote=upstox_quote,
        session_state="REGULAR",
        fallback_timestamp_ms=fallback,
    )
    assert dto.timestamp == fallback
    assert dto.marketTimestamp == fallback
    assert dto.fetchedAt == fallback
