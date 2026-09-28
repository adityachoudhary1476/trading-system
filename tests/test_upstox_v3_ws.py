"""Upstox V3 WebSocket tests (offline + deterministic).

The V3 transport delivers binary protobuf ``FeedResponse`` frames, not the v2
JSON dicts. These tests drive the real deserialize -> normalize -> on_event
path with synthetic protobuf frames built by the fixtures, so the decode logic
is covered without a network or a real socket.
"""
from __future__ import annotations

import pytest

from trading_system.india.upstox_v3_pb import FeedResponse, RequestMode
from trading_system.india.upstox_v3_ws import UpstoxV3WebSocket
from tests.fixtures.india_fixtures import (
    V3_NIFTY_KEY,
    V3_SBIN_KEY,
    v3_feed_bytes,
    v3_feed_response,
    v3_feed_response_full,
    v3_feed_response_no_price,
)


def _socket(on_event=None, symbol_map=None):
    return UpstoxV3WebSocket(
        access_token="tok",
        instrument_keys=[V3_SBIN_KEY],
        on_event=on_event if on_event is not None else (lambda e: None),
        symbol_map=symbol_map if symbol_map is not None else {V3_SBIN_KEY: "NSE:SBIN"},
    )


def test_v3_ltpc_frame_normalizes_to_internal_event():
    received = []
    sock = _socket(received.append)
    sock._handle_message_from_ws(None, v3_feed_bytes())
    assert len(received) == 1
    ev = received[0]
    assert ev.symbol == "NSE:SBIN"
    assert ev.ltp == 123.45
    assert ev.provider_symbol == V3_SBIN_KEY
    assert ev.exchange == "NSE"


def test_v3_timestamp_is_taken_from_feed_not_wall_clock():
    received = []
    sock = _socket(received.append)
    sock._handle_message_from_ws(None, v3_feed_bytes(ltt_ms=1_700_000_000_000))
    ev = received[0]
    # 2023-11-14T22:13:20Z - the last-traded time on the wire, not "now".
    assert ev.timestamp.timestamp() == 1_700_000_000


def test_v3_full_feed_carries_ohlc_and_volume():
    received = []
    sock = _socket(received.append)
    sock._handle_message_from_ws(None, v3_feed_response_full().serialize())
    assert len(received) == 1
    ev = received[0]
    assert ev.open == 120.0
    assert ev.high == 125.0
    assert ev.low == 119.0
    assert ev.volume == 98765.0


def test_v3_unknown_instrument_key_passes_through_verbatim():
    """An unmapped key must not be dropped or silently renamed."""
    received = []
    sock = _socket(received.append, symbol_map={})
    sock._handle_message_from_ws(None, v3_feed_bytes())
    assert len(received) == 1
    assert received[0].symbol == V3_SBIN_KEY


def test_v3_index_instrument_key_maps_to_internal_symbol():
    received = []
    sock = _socket(received.append, symbol_map={V3_NIFTY_KEY: "NSE:NIFTY50"})
    sock._handle_message_from_ws(None, v3_feed_bytes(instrument_key=V3_NIFTY_KEY))
    assert len(received) == 1
    assert received[0].symbol == "NSE:NIFTY50"
    assert received[0].exchange == "NSE"


def test_v3_frame_without_price_emits_no_event():
    received = []
    sock = _socket(received.append)
    sock._handle_message_from_ws(None, v3_feed_response_no_price().serialize())
    assert received == []


def test_v3_corrupt_frame_does_not_crash_the_socket():
    """A corrupt binary frame must not raise or emit a bogus event.

    The hand-rolled protobuf decoder is lenient: undecodable bytes yield an
    empty FeedResponse rather than raising, so the socket stays alive and
    simply produces nothing for that frame.
    """
    received = []
    sock = _socket(received.append)
    for payload in (b"\xff\xff\xff\xff not a protobuf frame", b"", b"\x0a\x7f"):
        sock._handle_message_from_ws(None, payload)  # must not raise
    assert received == []


def test_v3_json_control_message_is_ignored():
    """V3 also emits JSON control frames (e.g. errors); they carry no feed."""
    received = []
    sock = _socket(received.append)
    sock._handle_message_from_ws(None, '{"type":"error","code":"401"}')
    assert received == []


def test_v3_multi_instrument_frame_emits_one_event_per_feed():
    response = FeedResponse.deserialize(v3_feed_bytes())
    other = "NSE_EQ|INE467B01029"
    response.feeds[other] = response.feeds[V3_SBIN_KEY]
    received = []
    sock = _socket(
        received.append,
        symbol_map={V3_SBIN_KEY: "NSE:SBIN", other: "NSE:INFY"},
    )
    sock._handle_message_from_ws(None, response.serialize())
    assert sorted(ev.symbol for ev in received) == ["NSE:INFY", "NSE:SBIN"]


def test_v3_lifecycle_callbacks_fire():
    events = []
    sock = _socket()
    sock.on_connect_cb(lambda: events.append("connect"))
    sock.on_disconnect_cb(lambda: events.append("disconnect"))
    sock.on_error_cb(lambda e: events.append("error"))
    sock.on_invalid_cb(lambda e: events.append("invalid"))
    sock.on_auth_error_cb(lambda: events.append("auth_error"))

    sock._handle_open(None)
    assert events == ["connect"]

    sock._handle_error(None, RuntimeError("boom"))
    assert events == ["connect", "error"]

    # An unexpected drop is reported as a disconnect...
    sock._handle_close(None, 1006, "abnormal")
    assert events == ["connect", "error", "disconnect"]

    # ...but a deliberate close() is not.
    sock._closed = True
    sock._handle_close(None, 1000, "bye")
    assert events == ["connect", "error", "disconnect"]

    sock._notify_auth_error()
    assert events == ["connect", "error", "disconnect", "auth_error"]


def test_v3_open_subscribes_and_sends_auth_frame():
    sent = []

    class _FakeWS:
        def send(self, payload):
            sent.append(payload)

    sock = _socket()
    sock._subscribe = lambda: sent.append("<subscribed>")
    sock._handle_open(_FakeWS())
    assert sent == ["<subscribed>"]


def test_v3_request_mode_lite_sends_ltpc_only():
    sock = UpstoxV3WebSocket(
        access_token="tok",
        instrument_keys=[V3_SBIN_KEY],
        on_event=lambda e: None,
        mode=RequestMode.LTPC,
    )
    payload = sock._build_json_subscription(
        method="sub",
        instrument_keys=[V3_SBIN_KEY],
        mode=RequestMode.LTPC,
    )
    assert isinstance(payload, bytes)
    text = payload.decode("utf-8")
    assert V3_SBIN_KEY in text
    assert '"ltpc"' in text


def test_v3_full_d5_mode_requests_full_feed():
    sock = UpstoxV3WebSocket(
        access_token="tok",
        instrument_keys=[V3_SBIN_KEY],
        on_event=lambda e: None,
        mode=RequestMode.FULL_D5,
    )
    payload = sock._build_json_subscription(
        method="sub",
        instrument_keys=[V3_SBIN_KEY],
        mode=RequestMode.FULL_D5,
    )
    text = payload.decode("utf-8")
    assert V3_SBIN_KEY in text
    # Upstox v3 names the 5-day full-feed mode "full" and the 30-day one
    # "full_d30"; they are distinct on the wire.
    assert '"mode":"full"' in text


def test_v3_full_d30_mode_is_distinct_from_full_d5():
    sock = _socket()
    d5 = sock._build_json_subscription(
        method="sub", instrument_keys=[V3_SBIN_KEY], mode=RequestMode.FULL_D5
    ).decode("utf-8")
    d30 = sock._build_json_subscription(
        method="sub", instrument_keys=[V3_SBIN_KEY], mode=RequestMode.FULL_D30
    ).decode("utf-8")
    assert d5 != d30
    assert '"mode":"full_d30"' in d30


def test_v3_rejects_unknown_subscription_method():
    sock = _socket()
    with pytest.raises(ValueError):
        sock._build_json_subscription(
            method="subscribe", instrument_keys=[V3_SBIN_KEY]
        )
