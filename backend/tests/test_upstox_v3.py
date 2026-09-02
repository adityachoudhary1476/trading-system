"""Tests for Upstox V3 WebSocket protocol implementation.

Tests cover:
- Protobuf message serialization/deserialization
- V3 authorization flow
- Subscription message format
- Market data message decoding
- Error handling
"""
from __future__ import annotations

import json
import struct
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.trading_system.india.upstox_v3_pb import (
    Feed,
    FeedResponse,
    FeedType,
    LTPC,
    MarketFullFeed,
    OHLC,
    RequestMode,
    SubscriptionRequest,
)
from src.trading_system.india.upstox_v3_ws import (
    UpstoxV3AuthorizationError,
    UpstoxV3WebSocket,
)


class TestProtobufLTPC:
    """Tests for LTPC protobuf message."""

    def test_serialize_basic(self):
        ltpc = LTPC(ltp=100.5, ltt=1700000000000, ltq=100, cp=99.5)
        data = ltpc.serialize()
        assert len(data) > 0

    def test_deserialize_basic(self):
        ltpc = LTPC(ltp=100.5, ltt=1700000000000, ltq=100, cp=99.5)
        data = ltpc.serialize()
        decoded = LTPC.deserialize(data)
        assert decoded.ltp == pytest.approx(100.5)
        assert decoded.ltt == 1700000000000
        assert decoded.ltq == 100
        assert decoded.cp == pytest.approx(99.5)

    def test_roundtrip_empty(self):
        ltpc = LTPC()
        data = ltpc.serialize()
        decoded = LTPC.deserialize(data)
        assert decoded.ltp == 0.0
        assert decoded.ltt == 0

    def test_roundtrip_large_values(self):
        ltpc = LTPC(ltp=99999.99, ltt=9999999999999, ltq=1000000, cp=88888.88)
        data = ltpc.serialize()
        decoded = LTPC.deserialize(data)
        assert decoded.ltp == pytest.approx(99999.99)
        assert decoded.ltt == 9999999999999
        assert decoded.ltq == 1000000


class TestProtobufOHLC:
    """Tests for OHLC protobuf message."""

    def test_serialize_basic(self):
        ohlc = OHLC(interval="1minute", open=100.0, high=105.0, low=99.0, close=102.0, vol=1000, ts=1700000000000)
        data = ohlc.serialize()
        assert len(data) > 0

    def test_deserialize_basic(self):
        ohlc = OHLC(interval="1minute", open=100.0, high=105.0, low=99.0, close=102.0, vol=1000, ts=1700000000000)
        data = ohlc.serialize()
        decoded = OHLC.deserialize(data)
        assert decoded.interval == "1minute"
        assert decoded.open == pytest.approx(100.0)
        assert decoded.high == pytest.approx(105.0)
        assert decoded.low == pytest.approx(99.0)
        assert decoded.close == pytest.approx(102.0)
        assert decoded.vol == 1000
        assert decoded.ts == 1700000000000

    def test_roundtrip_multiple_intervals(self):
        for interval in ["1minute", "5minute", "15minute", "1hour", "day"]:
            ohlc = OHLC(interval=interval, open=100.0, high=105.0, low=99.0, close=102.0)
            data = ohlc.serialize()
            decoded = OHLC.deserialize(data)
            assert decoded.interval == interval


class TestProtobufMarketFullFeed:
    """Tests for MarketFullFeed protobuf message."""

    def test_serialize_with_ltpc(self):
        ltpc = LTPC(ltp=100.5, ltt=1700000000000)
        feed = MarketFullFeed(ltpc=ltpc)
        data = feed.serialize()
        assert len(data) > 0

    def test_deserialize_with_ltpc(self):
        ltpc = LTPC(ltp=100.5, ltt=1700000000000)
        feed = MarketFullFeed(ltpc=ltpc)
        data = feed.serialize()
        decoded = MarketFullFeed.deserialize(data)
        assert decoded.ltpc is not None
        assert decoded.ltpc.ltp == pytest.approx(100.5)
        assert decoded.ltpc.ltt == 1700000000000

    def test_serialize_with_ohlc(self):
        ohlc = OHLC(interval="1minute", open=100.0, high=105.0, low=99.0, close=102.0, vol=1000)
        feed = MarketFullFeed(market_ohlc=[ohlc])
        data = feed.serialize()
        assert len(data) > 0

    def test_deserialize_with_ohlc(self):
        ohlc = OHLC(interval="1minute", open=100.0, high=105.0, low=99.0, close=102.0, vol=1000)
        feed = MarketFullFeed(market_ohlc=[ohlc])
        data = feed.serialize()
        decoded = MarketFullFeed.deserialize(data)
        assert len(decoded.market_ohlc) == 1
        assert decoded.market_ohlc[0].interval == "1minute"
        assert decoded.market_ohlc[0].open == pytest.approx(100.0)

    def test_roundtrip_full(self):
        ltpc = LTPC(ltp=100.5, ltt=1700000000000, ltq=100, cp=99.5)
        ohlc1 = OHLC(interval="1minute", open=100.0, high=105.0, low=99.0, close=102.0, vol=1000)
        ohlc2 = OHLC(interval="5minute", open=99.0, high=106.0, low=98.0, close=103.0, vol=5000)
        feed = MarketFullFeed(ltpc=ltpc, market_ohlc=[ohlc1, ohlc2], atp=101.0, vtt=10000)
        data = feed.serialize()
        decoded = MarketFullFeed.deserialize(data)
        assert decoded.ltpc.ltp == pytest.approx(100.5)
        assert len(decoded.market_ohlc) == 2
        assert decoded.atp == pytest.approx(101.0)
        assert decoded.vtt == 10000


class TestProtobufFeed:
    """Tests for Feed protobuf message."""

    def test_serialize_ltpc_only(self):
        ltpc = LTPC(ltp=100.5, ltt=1700000000000)
        feed = Feed(ltpc=ltpc, request_mode=RequestMode.LTPC)
        data = feed.serialize()
        assert len(data) > 0

    def test_deserialize_ltpc_only(self):
        ltpc = LTPC(ltp=100.5, ltt=1700000000000)
        feed = Feed(ltpc=ltpc, request_mode=RequestMode.LTPC)
        data = feed.serialize()
        decoded = Feed.deserialize(data)
        assert decoded.ltpc is not None
        assert decoded.ltpc.ltp == pytest.approx(100.5)

    def test_serialize_full_feed(self):
        ltpc = LTPC(ltp=100.5, ltt=1700000000000)
        full_feed = MarketFullFeed(ltpc=ltpc)
        feed = Feed(full_feed=full_feed, request_mode=RequestMode.FULL_D5)
        data = feed.serialize()
        assert len(data) > 0

    def test_roundtrip_full_mode(self):
        ltpc = LTPC(ltp=100.5, ltt=1700000000000)
        full_feed = MarketFullFeed(ltpc=ltpc, atp=101.0)
        feed = Feed(ltpc=ltpc, full_feed=full_feed, request_mode=RequestMode.FULL_D5)
        data = feed.serialize()
        decoded = Feed.deserialize(data)
        assert decoded.request_mode == RequestMode.FULL_D5
        assert decoded.ltpc is not None
        assert decoded.full_feed is not None
        assert decoded.full_feed.atp == pytest.approx(101.0)


class TestProtobufFeedResponse:
    """Tests for FeedResponse protobuf message."""

    def test_serialize_single_feed(self):
        ltpc = LTPC(ltp=100.5, ltt=1700000000000)
        feed = Feed(ltpc=ltpc)
        response = FeedResponse(
            type=FeedType.LIVE_FEED,
            feeds={"NSE_EQ|INE020B01018": feed},
            current_ts=1700000000000,
        )
        data = response.serialize()
        assert len(data) > 0

    def test_deserialize_single_feed(self):
        ltpc = LTPC(ltp=100.5, ltt=1700000000000)
        feed = Feed(ltpc=ltpc)
        response = FeedResponse(
            type=FeedType.LIVE_FEED,
            feeds={"NSE_EQ|INE020B01018": feed},
            current_ts=1700000000000,
        )
        data = response.serialize()
        decoded = FeedResponse.deserialize(data)
        assert decoded.type == FeedType.LIVE_FEED
        assert decoded.current_ts == 1700000000000
        assert "NSE_EQ|INE020B01018" in decoded.feeds
        assert decoded.feeds["NSE_EQ|INE020B01018"].ltpc.ltp == pytest.approx(100.5)

    def test_roundtrip_multiple_feeds(self):
        ltpc1 = LTPC(ltp=100.5, ltt=1700000000000)
        ltpc2 = LTPC(ltp=200.5, ltt=1700000000000)
        feed1 = Feed(ltpc=ltpc1)
        feed2 = Feed(ltpc=ltpc2)
        response = FeedResponse(
            type=FeedType.LIVE_FEED,
            feeds={
                "NSE_EQ|INE020B01018": feed1,
                "NSE_EQ|INE030B01019": feed2,
            },
            current_ts=1700000000000,
        )
        data = response.serialize()
        decoded = FeedResponse.deserialize(data)
        assert len(decoded.feeds) == 2
        assert decoded.feeds["NSE_EQ|INE020B01018"].ltpc.ltp == pytest.approx(100.5)
        assert decoded.feeds["NSE_EQ|INE030B01019"].ltpc.ltp == pytest.approx(200.5)

    def test_initial_feed_type(self):
        response = FeedResponse(
            type=FeedType.INITIAL_FEED,
            feeds={},
            current_ts=1700000000000,
        )
        data = response.serialize()
        decoded = FeedResponse.deserialize(data)
        assert decoded.type == FeedType.INITIAL_FEED


class TestSubscriptionRequest:
    """Tests for V3 subscription request message."""

    def test_serialize_basic(self):
        request = SubscriptionRequest(
            guid="test-guid-123",
            method="sub",
            mode=RequestMode.LTPC,
            instrument_keys=["NSE_EQ|INE020B01018"],
        )
        data = request.serialize()
        assert len(data) > 0

    def test_deserialize_basic(self):
        request = SubscriptionRequest(
            guid="test-guid-123",
            method="sub",
            mode=RequestMode.LTPC,
            instrument_keys=["NSE_EQ|INE020B01018"],
        )
        data = request.serialize()
        decoded = SubscriptionRequest.deserialize(data)
        assert decoded.guid == "test-guid-123"
        assert decoded.method == "sub"
        assert decoded.instrument_keys == ["NSE_EQ|INE020B01018"]

    def test_roundtrip_multiple_instruments(self):
        request = SubscriptionRequest(
            guid="test-guid-456",
            method="sub",
            mode=RequestMode.FULL_D5,
            instrument_keys=[
                "NSE_EQ|INE020B01018",
                "NSE_EQ|INE030B01019",
                "NSE_EQ|INE040B01020",
            ],
        )
        data = request.serialize()
        decoded = SubscriptionRequest.deserialize(data)
        assert decoded.guid == "test-guid-456"
        assert decoded.method == "sub"
        assert decoded.mode == RequestMode.FULL_D5
        assert len(decoded.instrument_keys) == 3
        assert decoded.instrument_keys[0] == "NSE_EQ|INE020B01018"
        assert decoded.instrument_keys[1] == "NSE_EQ|INE030B01019"
        assert decoded.instrument_keys[2] == "NSE_EQ|INE040B01020"

    def test_unsubscribe(self):
        request = SubscriptionRequest(
            guid="test-guid-789",
            method="unsub",
            instrument_keys=["NSE_EQ|INE020B01018"],
        )
        data = request.serialize()
        decoded = SubscriptionRequest.deserialize(data)
        assert decoded.method == "unsub"

    def test_change_mode(self):
        request = SubscriptionRequest(
            guid="test-guid-012",
            method="change_mode",
            mode=RequestMode.FULL_D30,
            instrument_keys=["NSE_EQ|INE020B01018"],
        )
        data = request.serialize()
        decoded = SubscriptionRequest.deserialize(data)
        assert decoded.method == "change_mode"
        assert decoded.mode == RequestMode.FULL_D30


class TestUpstoxV3WebSocket:
    """Tests for V3 WebSocket client."""

    def test_init(self):
        ws = UpstoxV3WebSocket(
            access_token="test-token",
            instrument_keys=["NSE_EQ|INE020B01018"],
            on_event=lambda x: None,
        )
        assert ws.access_token == "test-token"
        assert ws.instrument_keys == ["NSE_EQ|INE020B01018"]
        assert ws._closed is False

    def test_authorize_success(self):
        ws = UpstoxV3WebSocket(
            access_token="test-token",
            instrument_keys=["NSE_EQ|INE020B01018"],
            on_event=lambda x: None,
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "success",
            "data": {
                "authorized_redirect_uri": "wss://ws-api.upstox.com/v3/feed/authorized?token=abc123",
            },
        }
        mock_response.raise_for_status = MagicMock()

        with patch("src.trading_system.india.upstox_v3_ws.requests.get", return_value=mock_response):
            url = ws._authorize()
            assert url == "wss://ws-api.upstox.com/v3/feed/authorized?token=abc123"

    def test_authorize_401_error(self):
        ws = UpstoxV3WebSocket(
            access_token="bad-token",
            instrument_keys=["NSE_EQ|INE020B01018"],
            on_event=lambda x: None,
        )
        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch("src.trading_system.india.upstox_v3_ws.requests.get", return_value=mock_response):
            with pytest.raises(UpstoxV3AuthorizationError, match="invalid or expired"):
                ws._authorize()

    def test_authorize_403_error(self):
        ws = UpstoxV3WebSocket(
            access_token="no-permission-token",
            instrument_keys=["NSE_EQ|INE020B01018"],
            on_event=lambda x: None,
        )
        mock_response = MagicMock()
        mock_response.status_code = 403

        with patch("src.trading_system.india.upstox_v3_ws.requests.get", return_value=mock_response):
            with pytest.raises(UpstoxV3AuthorizationError, match="market data permission"):
                ws._authorize()

    def test_authorize_missing_redirect_uri(self):
        ws = UpstoxV3WebSocket(
            access_token="test-token",
            instrument_keys=["NSE_EQ|INE020B01018"],
            on_event=lambda x: None,
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "success",
            "data": {},  # Missing authorized_redirect_uri
        }
        mock_response.raise_for_status = MagicMock()

        with patch("src.trading_system.india.upstox_v3_ws.requests.get", return_value=mock_response):
            with pytest.raises(UpstoxV3AuthorizationError, match="No authorized_redirect_uri"):
                ws._authorize()

    def test_authorize_api_error(self):
        ws = UpstoxV3WebSocket(
            access_token="test-token",
            instrument_keys=["NSE_EQ|INE020B01018"],
            on_event=lambda x: None,
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "error",
            "message": "Internal server error",
        }
        mock_response.raise_for_status = MagicMock()

        with patch("src.trading_system.india.upstox_v3_ws.requests.get", return_value=mock_response):
            with pytest.raises(UpstoxV3AuthorizationError, match="Authorization failed"):
                ws._authorize()

    def test_normalize_ltpc_only(self):
        ws = UpstoxV3WebSocket(
            access_token="test-token",
            instrument_keys=["NSE_EQ|INE020B01018"],
            on_event=lambda x: None,
        )
        ltpc = LTPC(ltp=100.5, ltt=1700000000000, cp=99.5)
        feed = Feed(ltpc=ltpc)

        event = ws._normalize("NSE_EQ|INE020B01018", feed, 1700000000000)

        assert event is not None
        assert event.ltp == 100.5
        assert event.close == 99.5
        assert event.symbol == "NSE_EQ|INE020B01018"
        assert event.exchange == "NSE_EQ"

    def test_normalize_full_feed(self):
        ws = UpstoxV3WebSocket(
            access_token="test-token",
            instrument_keys=["NSE_EQ|INE020B01018"],
            on_event=lambda x: None,
        )
        ltpc = LTPC(ltp=100.5, ltt=1700000000000)
        ohlc = OHLC(interval="1minute", open=100.0, high=105.0, low=99.0, close=102.0, vol=1000)
        full_feed = MarketFullFeed(ltpc=ltpc, market_ohlc=[ohlc])
        feed = Feed(full_feed=full_feed)

        event = ws._normalize("NSE_EQ|INE020B01018", feed, 1700000000000)

        assert event is not None
        assert event.ltp == 100.5
        assert event.open == 100.0
        assert event.high == 105.0
        assert event.low == 99.0
        assert event.close == 102.0
        assert event.volume == 1000

    def test_normalize_no_ltpc(self):
        ws = UpstoxV3WebSocket(
            access_token="test-token",
            instrument_keys=["NSE_EQ|INE020B01018"],
            on_event=lambda x: None,
        )
        feed = Feed()  # No ltpc, no full_feed

        event = ws._normalize("NSE_EQ|INE020B01018", feed, 1700000000000)

        assert event is None

    def test_normalize_timestamp_conversion(self):
        ws = UpstoxV3WebSocket(
            access_token="test-token",
            instrument_keys=["NSE_EQ|INE020B01018"],
            on_event=lambda x: None,
        )
        ltpc = LTPC(ltp=100.5, ltt=1700000000000)  # milliseconds
        feed = Feed(ltpc=ltpc)

        event = ws._normalize("NSE_EQ|INE020B01018", feed, 1700000000000)

        assert event is not None
        # Verify timestamp is correctly converted from milliseconds
        expected_ts = datetime.fromtimestamp(1700000000000 / 1000, tz=timezone.utc)
        assert event.timestamp == expected_ts

    def test_handle_message_json(self):
        ws = UpstoxV3WebSocket(
            access_token="test-token",
            instrument_keys=["NSE_EQ|INE020B01018"],
            on_event=lambda x: None,
        )
        # JSON messages should be logged and skipped
        ws._handle_message('{"type": "error", "message": "test"}')

    def test_handle_message_protobuf(self):
        ws = UpstoxV3WebSocket(
            access_token="test-token",
            instrument_keys=["NSE_EQ|INE020B01018"],
            on_event=lambda x: None,
        )
        # Create a valid protobuf message
        ltpc = LTPC(ltp=100.5, ltt=1700000000000)
        feed = Feed(ltpc=ltpc)
        response = FeedResponse(
            type=FeedType.LIVE_FEED,
            feeds={"NSE_EQ|INE020B01018": feed},
            current_ts=1700000000000,
        )
        data = response.serialize()

        received_events = []
        ws.on_event = lambda e: received_events.append(e)

        ws._handle_message(data)

        assert len(received_events) == 1
        assert received_events[0].ltp == 100.5

    def test_close(self):
        ws = UpstoxV3WebSocket(
            access_token="test-token",
            instrument_keys=["NSE_EQ|INE020B01018"],
            on_event=lambda x: None,
        )
        ws._ws = MagicMock()
        ws.close()
        assert ws._closed is True
        ws._ws.close.assert_called_once()

    def test_close_no_ws(self):
        ws = UpstoxV3WebSocket(
            access_token="test-token",
            instrument_keys=["NSE_EQ|INE020B01018"],
            on_event=lambda x: None,
        )
        ws._ws = None
        ws.close()  # Should not raise
        assert ws._closed is True

    def test_callback_setters(self):
        ws = UpstoxV3WebSocket(
            access_token="test-token",
            instrument_keys=["NSE_EQ|INE020B01018"],
            on_event=lambda x: None,
        )
        cb1 = MagicMock()
        cb2 = MagicMock()
        cb3 = MagicMock()
        cb4 = MagicMock()

        ws.on_connect_cb(cb1)
        ws.on_disconnect_cb(cb2)
        ws.on_auth_error_cb(cb3)
        ws.on_error_cb(cb4)

        assert ws._on_connect_cb == cb1
        assert ws._on_disconnect_cb == cb2
        assert ws._on_auth_error_cb == cb3
        assert ws._on_error_cb == cb4


class TestV3Integration:
    """Integration tests for V3 protocol flow."""

    def test_full_flow_simulation(self):
        """Simulate the full V3 flow: authorize -> connect -> subscribe -> receive data."""
        events_received = []

        ws = UpstoxV3WebSocket(
            access_token="test-token",
            instrument_keys=["NSE_EQ|INE020B01018", "NSE_EQ|INE030B01019"],
            on_event=lambda e: events_received.append(e),
        )

        # Step 1: Authorize
        mock_auth_response = MagicMock()
        mock_auth_response.status_code = 200
        mock_auth_response.json.return_value = {
            "status": "success",
            "data": {
                "authorized_redirect_uri": "wss://ws-api.upstox.com/v3/feed/authorized?token=abc123",
            },
        }
        mock_auth_response.raise_for_status = MagicMock()

        with patch("src.trading_system.india.upstox_v3_ws.requests.get", return_value=mock_auth_response):
            url = ws._authorize()
            assert url == "wss://ws-api.upstox.com/v3/feed/authorized?token=abc123"

        # Step 2: Simulate receiving market data
        ltpc1 = LTPC(ltp=100.5, ltt=1700000000000)
        ltpc2 = LTPC(ltp=200.5, ltt=1700000000000)
        feed1 = Feed(ltpc=ltpc1)
        feed2 = Feed(ltpc=ltpc2)
        response = FeedResponse(
            type=FeedType.LIVE_FEED,
            feeds={
                "NSE_EQ|INE020B01018": feed1,
                "NSE_EQ|INE030B01019": feed2,
            },
            current_ts=1700000000000,
        )
        data = response.serialize()

        ws._handle_message(data)

        assert len(events_received) == 2
        assert events_received[0].symbol == "NSE_EQ|INE020B01018"
        assert events_received[0].ltp == 100.5
        assert events_received[1].symbol == "NSE_EQ|INE030B01019"
        assert events_received[1].ltp == 200.5

    def test_subscription_request_format(self):
        """Verify subscription request matches V3 format."""
        request = SubscriptionRequest(
            guid="1234567890",
            method="sub",
            mode=RequestMode.FULL_D5,
            instrument_keys=["NSE_EQ|INE020B01018", "NSE_EQ|INE030B01019"],
        )
        data = request.serialize()

        # Verify it's binary (not JSON)
        assert isinstance(data, bytes)

        # Verify it can be deserialized
        decoded = SubscriptionRequest.deserialize(data)
        assert decoded.guid == "1234567890"
        assert decoded.method == "sub"
        assert decoded.mode == RequestMode.FULL_D5
        assert len(decoded.instrument_keys) == 2
