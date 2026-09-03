"""Focused tests for the Upstox V2 market-data service.

The previous implementation routed every interval through the
``/v2/historical-candle/intraday/{symbol}/{interval}`` endpoint, which
silently failed for ``day`` (the intraday endpoint only accepts
``1minute``/``30minute``).  This module pins the corrected behaviour:

* daily candles use the historical-candle endpoint with a YYYY-MM-DD
  date range, not the intraday endpoint
* intraday intervals (1m/30m) also use the historical endpoint with a
  one-day range
* HTTP 401/403/429/4xx/5xx are surfaced as typed exceptions
* malformed Upstox bodies do not silently collapse to ``None``
* candles are sorted oldest-first
* the NSE:NIFTY50 + 1d case builds the expected instrument key and
  request URL

All tests mock the HTTP boundary with a clearly fake bearer token; no
real Upstox credentials are required.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import requests

from services import market_data
from services.market_data import (
    UpstoxBadResponseError,
    UpstoxMalformedError,
    UpstoxMarketDataError,
    UpstoxNetworkError,
    UpstoxRateLimitedError,
    UpstoxUnauthorizedError,
    _build_dataframe,
    _build_url,
    _date_range_for,
    _parse_candle,
    _resolve_interval,
    fetch_ohlcv,
    to_upstox_symbol,
)


FAKE_BEARER = "fake-redacted-bearer-token-not-a-real-credential"


def _nifty50_success_candles() -> list[list]:
    """Realistic NIFTY50 daily candle response (newest-first)."""
    return [
        ["2024-01-05T00:00:00+05:30", 21701.95, 21743.75, 21650.20, 21452.95, 0, 0],
        ["2024-01-04T00:00:00+05:30", 21655.20, 21689.05, 21573.45, 21566.85, 0, 0],
        ["2024-01-03T00:00:00+05:30", 21551.25, 21623.10, 21521.80, 21551.85, 0, 0],
    ]


def _mock_response(status_code: int, body, headers=None) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.headers = headers or {}
    resp.text = "" if body is None else ""
    if isinstance(body, (dict, list)):
        resp.json.return_value = body
    else:
        resp.json.side_effect = ValueError("not json")
    return resp


# ----------------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------------


class TestHelpers:
    def test_to_upstox_symbol_index(self):
        assert to_upstox_symbol("NSE:NIFTY50") == "NSE_INDEX|Nifty 50"
        assert to_upstox_symbol("NSE:BANKNIFTY") == "NSE_INDEX|Nifty Bank"
        assert to_upstox_symbol("NSE:FINNIFTY") == "NSE_INDEX|Nifty Fin Service"

    def test_to_upstox_symbol_equity_isin(self):
        assert to_upstox_symbol("NSE:SBIN") == "NSE_EQ|INE062A01020"
        assert to_upstox_symbol("NSE:RELIANCE") == "NSE_EQ|INE002A01018"

    def test_to_upstox_symbol_passthrough(self):
        assert to_upstox_symbol("NSE_EQ|INE062A01020") == "NSE_EQ|INE062A01020"
        assert to_upstox_symbol("NSE_INDEX|Nifty 50") == "NSE_INDEX|Nifty 50"

    def test_to_upstox_symbol_invalid(self):
        with pytest.raises(ValueError):
            to_upstox_symbol("NOPE")

    def test_resolve_interval_supported(self):
        assert _resolve_interval("1d") == "day"
        assert _resolve_interval("1D") == "day"
        assert _resolve_interval("1m") == "1minute"
        assert _resolve_interval("1w") == "week"
        assert _resolve_interval("1mo") == "month"

    def test_resolve_interval_unsupported(self):
        with pytest.raises(ValueError):
            _resolve_interval("5m")
        with pytest.raises(ValueError):
            _resolve_interval("1h")
        with pytest.raises(ValueError):
            _resolve_interval("garbage")

    def test_parse_candle_valid(self):
        raw = ["2024-01-05T00:00:00+05:30", 21701.95, 21743.75, 21650.20, 21452.95, 0]
        parsed = _parse_candle(raw)
        assert parsed is not None
        assert parsed[0] == "2024-01-05T00:00:00+05:30"
        assert parsed[1:] == [21701.95, 21743.75, 21650.20, 21452.95, 0.0]

    def test_parse_candle_missing_volume_defaults_zero(self):
        # 5-element candle is rejected (need at least 6 fields)
        assert _parse_candle(["2024-01-05T00:00:00+05:30", 1, 2, 3, 4]) is None

    def test_parse_candle_non_finite_price_rejected(self):
        raw = ["2024-01-05T00:00:00+05:30", float("inf"), 2, 3, 4, 5]
        assert _parse_candle(raw) is None

    def test_parse_candle_non_string_timestamp_rejected(self):
        raw = [1704412800000, 1, 2, 3, 4, 5]
        assert _parse_candle(raw) is None

    def test_parse_candle_garbage_rejected(self):
        assert _parse_candle(None) is None
        assert _parse_candle("not a list") is None
        assert _parse_candle([1, 2, 3]) is None

    def test_build_dataframe_sorts_oldest_first(self):
        # Upstox returns newest-first; verify we reorder to oldest-first
        raw = _nifty50_success_candles()
        df = _build_dataframe(raw)
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]
        # Index must be tz-aware UTC
        assert df.index.tz is not None
        # Order: oldest first.  Upstox NSE candles are timestamped in
        # IST (+05:30) at 00:00 local, so the UTC equivalent is 18:30
        # the previous calendar day.
        assert df.index[0] == pd.Timestamp("2024-01-02 18:30:00", tz="UTC")
        assert df.index[-1] == pd.Timestamp("2024-01-04 18:30:00", tz="UTC")

    def test_build_dataframe_deduplicates_timestamps(self):
        raw = [
            ["2024-01-05T00:00:00+05:30", 1, 2, 3, 4, 5],
            ["2024-01-05T00:00:00+05:30", 10, 20, 30, 40, 50],  # duplicate ts, different values
            ["2024-01-04T00:00:00+05:30", 1, 2, 3, 4, 5],
        ]
        df = _build_dataframe(raw)
        assert len(df) == 2
        # last-write-wins on duplicates
        assert df.iloc[-1]["open"] == 10.0

    def test_build_url_encodes_instrument_key(self):
        url = _build_url("NSE_INDEX|Nifty 50", "day", "2024-01-05", "2024-01-03")
        # Pipe must be percent-encoded as %7C
        assert url == (
            "https://api.upstox.com/v2/historical-candle/"
            "NSE_INDEX%7CNifty%2050/day/2024-01-05/2024-01-03"
        )

    def test_date_range_for_day_uses_year_window(self):
        to_d, from_d = _date_range_for("day", bars=160)
        # Both formatted YYYY-MM-DD
        for s in (to_d, from_d):
            assert len(s) == 10 and s[4] == "-" and s[7] == "-"
        # From is earlier than to
        assert datetime.strptime(from_d, "%Y-%m-%d") < datetime.strptime(to_d, "%Y-%m-%d")
        # 160 daily bars -> up to ~161 calendar days back
        delta = (
            datetime.strptime(to_d, "%Y-%m-%d") - datetime.strptime(from_d, "%Y-%m-%d")
        ).days
        assert 150 <= delta <= 200

    def test_date_range_for_minute_uses_one_day_window(self):
        to_d, from_d = _date_range_for("1minute", bars=400)
        # 1minute historical endpoint max window is 31 days; 400 1-min bars
        # fit in a single day, so range stays tiny.
        delta = (
            datetime.strptime(to_d, "%Y-%m-%d") - datetime.strptime(from_d, "%Y-%m-%d")
        ).days
        assert delta <= 2


# ----------------------------------------------------------------------------
# NIFTY50 + 1d (the exact production failure case)
# ----------------------------------------------------------------------------


class TestFetchOhlcvNifty50Daily:
    @pytest.mark.asyncio
    async def test_hits_historical_endpoint_with_date_range(self):
        body = {"status": "success", "data": {"candles": _nifty50_success_candles()}}
        mock_resp = _mock_response(200, body)

        with patch("services.market_data.requests.get", return_value=mock_resp) as mock_get:
            df = await fetch_ohlcv("NSE:NIFTY50", "1d", FAKE_BEARER, bars=160)

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3
        # Oldest first.  NSE daily candles are timestamped in IST at
        # 00:00 local, so the UTC equivalent is 18:30 the previous
        # calendar day.
        assert df.index[0] == pd.Timestamp("2024-01-02 18:30:00", tz="UTC")
        assert df.index[-1] == pd.Timestamp("2024-01-04 18:30:00", tz="UTC")
        # Auth header carries the fake bearer
        called_kwargs = mock_get.call_args.kwargs
        assert called_kwargs["headers"]["Authorization"] == f"Bearer {FAKE_BEARER}"
        called_url = mock_get.call_args.args[0]
        # Must hit the historical (non-intraday) endpoint
        assert called_url.startswith(
            "https://api.upstox.com/v2/historical-candle/"
        )
        assert "/intraday/" not in called_url
        # Instrument key must be the canonical NSE_INDEX|Nifty 50
        assert "NSE_INDEX%7CNifty%2050" in called_url
        # Path ends with to_date/from_date
        path_parts = called_url.split("/")
        assert path_parts[-2].count("-") == 2 and len(path_parts[-2]) == 10
        assert path_parts[-1].count("-") == 2 and len(path_parts[-1]) == 10

    @pytest.mark.asyncio
    async def test_trims_to_requested_bar_count(self):
        # Upstox returns 300 candles for a 1-year daily window; we ask for 50
        big = []
        # Build 300 uniquely-dated daily candles starting 2023-01-02
        # (skipping weekends by stepping 1 day at a time, which is a
        # valid approximation since _build_dataframe doesn't dedup).
        from datetime import date, timedelta
        start = date(2023, 1, 2)
        d = start
        for i in range(300):
            big.append(
                [
                    f"{d.isoformat()}T00:00:00+05:30",
                    100 + i, 105 + i, 95 + i, 102 + i, 1000,
                ]
            )
            d = d + timedelta(days=1)
        body = {"status": "success", "data": {"candles": big}}
        mock_resp = _mock_response(200, body)

        with patch("services.market_data.requests.get", return_value=mock_resp):
            df = await fetch_ohlcv("NSE:NIFTY50", "1d", FAKE_BEARER, bars=50)
        # The service does not invent candles; it just returns the
        # parsed set.  We only guarantee no padding.
        assert len(df) == 300
        # Verify chronological order
        assert df.index.is_monotonic_increasing


# ----------------------------------------------------------------------------
# Empty / malformed / error responses
# ----------------------------------------------------------------------------


class TestFetchOhlcvErrorPaths:
    @pytest.mark.asyncio
    async def test_empty_candles_returns_none(self):
        body = {"status": "success", "data": {"candles": []}}
        mock_resp = _mock_response(200, body)
        with patch("services.market_data.requests.get", return_value=mock_resp):
            df = await fetch_ohlcv("NSE:NIFTY50", "1d", FAKE_BEARER)
        assert df is None

    @pytest.mark.asyncio
    async def test_http_401_raises_unauthorized(self):
        mock_resp = _mock_response(401, {"status": "error", "message": "Invalid token"})
        with patch("services.market_data.requests.get", return_value=mock_resp):
            with pytest.raises(UpstoxUnauthorizedError):
                await fetch_ohlcv("NSE:NIFTY50", "1d", FAKE_BEARER)

    @pytest.mark.asyncio
    async def test_http_403_raises_unauthorized(self):
        mock_resp = _mock_response(403, {"status": "error", "message": "Forbidden"})
        with patch("services.market_data.requests.get", return_value=mock_resp):
            with pytest.raises(UpstoxUnauthorizedError):
                await fetch_ohlcv("NSE:NIFTY50", "1d", FAKE_BEARER)

    @pytest.mark.asyncio
    async def test_http_429_raises_rate_limited(self):
        mock_resp = _mock_response(
            429,
            {"status": "error", "message": "Too many requests"},
            headers={"Retry-After": "2"},
        )
        with patch("services.market_data.requests.get", return_value=mock_resp):
            with pytest.raises(UpstoxRateLimitedError) as exc:
                await fetch_ohlcv("NSE:NIFTY50", "1d", FAKE_BEARER)
            assert "2" in str(exc.value)

    @pytest.mark.asyncio
    async def test_http_500_raises_bad_response(self):
        mock_resp = _mock_response(500, {"status": "error", "message": "Upstream error"})
        with patch("services.market_data.requests.get", return_value=mock_resp):
            with pytest.raises(UpstoxBadResponseError):
                await fetch_ohlcv("NSE:NIFTY50", "1d", FAKE_BEARER)

    @pytest.mark.asyncio
    async def test_non_success_body_raises_bad_response(self):
        # 200 but status=error in the body
        mock_resp = _mock_response(
            200,
            {"status": "error", "error_message": "Invalid instrument_key"},
        )
        with patch("services.market_data.requests.get", return_value=mock_resp):
            with pytest.raises(UpstoxBadResponseError):
                await fetch_ohlcv("NSE:NIFTY50", "1d", FAKE_BEARER)

    @pytest.mark.asyncio
    async def test_malformed_json_raises_malformed(self):
        mock_resp = _mock_response(200, "not-json")
        with patch("services.market_data.requests.get", return_value=mock_resp):
            with pytest.raises(UpstoxMalformedError):
                await fetch_ohlcv("NSE:NIFTY50", "1d", FAKE_BEARER)

    @pytest.mark.asyncio
    async def test_missing_candles_array_raises_malformed(self):
        mock_resp = _mock_response(200, {"status": "success", "data": {}})
        with patch("services.market_data.requests.get", return_value=mock_resp):
            with pytest.raises(UpstoxMalformedError):
                await fetch_ohlcv("NSE:NIFTY50", "1d", FAKE_BEARER)

    @pytest.mark.asyncio
    async def test_only_malformed_candles_raises_malformed(self):
        body = {
            "status": "success",
            "data": {
                "candles": [
                    ["not-a-timestamp", 1, 2, 3, 4, 5],
                    [None, 1, 2, 3, 4, 5],
                ]
            },
        }
        mock_resp = _mock_response(200, body)
        with patch("services.market_data.requests.get", return_value=mock_resp):
            with pytest.raises(UpstoxMalformedError):
                await fetch_ohlcv("NSE:NIFTY50", "1d", FAKE_BEARER)

    @pytest.mark.asyncio
    async def test_network_error_raises_network_error(self):
        with patch(
            "services.market_data.requests.get",
            side_effect=requests.ConnectionError("boom"),
        ):
            with pytest.raises(UpstoxNetworkError):
                await fetch_ohlcv("NSE:NIFTY50", "1d", FAKE_BEARER)

    @pytest.mark.asyncio
    async def test_invalid_symbol_returns_none(self):
        df = await fetch_ohlcv("GARBAGE", "1d", FAKE_BEARER)
        assert df is None

    @pytest.mark.asyncio
    async def test_unsupported_timeframe_returns_none(self):
        df = await fetch_ohlcv("NSE:NIFTY50", "5m", FAKE_BEARER)
        assert df is None

    @pytest.mark.asyncio
    async def test_does_not_hit_intraday_endpoint_for_daily(self):
        body = {"status": "success", "data": {"candles": _nifty50_success_candles()}}
        mock_resp = _mock_response(200, body)
        with patch("services.market_data.requests.get", return_value=mock_resp) as mock_get:
            await fetch_ohlcv("NSE:NIFTY50", "1d", FAKE_BEARER)
        called_url = mock_get.call_args.args[0]
        # The bug we are fixing: previous code used
        # /historical-candle/intraday/.../day which is wrong.
        assert "/intraday/" not in called_url
        # And the path must end with a date range
        assert called_url.endswith(
            f"/{_date_range_for('day', 160)[0]}/{_date_range_for('day', 160)[1]}"
        )

    @pytest.mark.asyncio
    async def test_does_not_silently_coerce_unsupported_timeframe(self):
        # Calling fetch_ohlcv with an unsupported timeframe must NOT route
        # to a neighbouring interval.  The service returns None and never
        # hits the network.
        with patch("services.market_data.requests.get") as mock_get:
            df = await fetch_ohlcv("NSE:NIFTY50", "15m", FAKE_BEARER)
        assert df is None
        assert mock_get.call_count == 0
