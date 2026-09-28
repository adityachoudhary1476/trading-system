"""Upstox V3 Market Data Feed protobuf message wrappers.

This module provides Python wrappers for the Upstox V3 protobuf messages.
The official .proto file is at:
https://assets.upstox.com/feed/market-data-feed/v3/MarketDataFeed.proto

This implementation uses manual serialization/deserialization to avoid
requiring the protoc compiler at build time.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional


# Upstox V3 protobuf message types
class FeedType(IntEnum):
    INITIAL_FEED = 0
    LIVE_FEED = 1
    MARKET_INFO = 2


class RequestMode(IntEnum):
    LTPC = 0
    FULL_D5 = 1
    OPTION_GREEKS = 2
    FULL_D30 = 3


# Protobuf wire types
VARINT = 0
FIXED64 = 1
LEN_DELIMITED = 2
FIXED32 = 5

# A 64-bit varint is at most 10 bytes. Anything longer is corrupt, and without
# this bound a run of continuation bytes shifts an int past any limit.
_MAX_VARINT_BYTES = 10


class ProtobufDecodeError(ValueError):
    """Raised when a byte sequence is not a valid protobuf message.

    The decoders used to be lenient: a truncated or corrupt frame decoded to a
    default-constructed message, so a garbled feed frame was indistinguishable
    from a legitimately empty one. That made corruption invisible - the feed
    simply went quiet with nothing to alert on. Malformed input now raises, and
    the socket reports it via its invalid-frame callback.
    """


def _encode_varint(value: int) -> bytes:
    """Encode an integer as a protobuf varint."""
    result = []
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Decode a protobuf varint. Returns (value, new_offset)."""
    result = 0
    shift = 0
    start = offset
    while True:
        if offset >= len(data):
            raise ProtobufDecodeError(
                f"truncated varint at offset {start}"
            )
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            break
        shift += 7
        if offset - start >= _MAX_VARINT_BYTES:
            raise ProtobufDecodeError(
                f"varint at offset {start} exceeds {_MAX_VARINT_BYTES} bytes"
            )
    return result, offset


def _encode_tag(field_number: int, wire_type: int) -> bytes:
    """Encode a protobuf field tag."""
    return _encode_varint((field_number << 3) | wire_type)


def _encode_string(field_number: int, value: str) -> bytes:
    """Encode a string field."""
    encoded = value.encode("utf-8")
    return _encode_tag(field_number, LEN_DELIMITED) + _encode_varint(len(encoded)) + encoded


def _encode_varint_field(field_number: int, value: int) -> bytes:
    """Encode a varint field."""
    return _encode_tag(field_number, VARINT) + _encode_varint(value)


def _encode_double(field_number: int, value: float) -> bytes:
    """Encode a double field (64-bit)."""
    return _encode_tag(field_number, FIXED64) + struct.pack("<d", value)


def _encode_int64(field_number: int, value: int) -> bytes:
    """Encode an int64 field as varint."""
    return _encode_tag(field_number, VARINT) + _encode_varint(value)


def _read_length_delimited(data: bytes, offset: int) -> tuple[bytes, int]:
    """Read a length-delimited payload, rejecting a length that overruns."""
    length, offset = _decode_varint(data, offset)
    if offset + length > len(data):
        raise ProtobufDecodeError(
            f"length-delimited field at offset {offset} claims {length} bytes, "
            f"{len(data) - offset} available"
        )
    return data[offset:offset + length], offset + length


def _decode_string(data: bytes, offset: int) -> tuple[str, int]:
    """Decode a string field. Returns (value, new_offset)."""
    raw, offset = _read_length_delimited(data, offset)
    try:
        return raw.decode("utf-8"), offset
    except UnicodeDecodeError as exc:
        raise ProtobufDecodeError("invalid utf-8 in string field") from exc


def _decode_bytes(data: bytes, offset: int) -> tuple[bytes, int]:
    """Decode a length-delimited bytes field. Returns (value, new_offset)."""
    return _read_length_delimited(data, offset)


def _decode_double(data: bytes, offset: int) -> tuple[float, int]:
    """Decode a double field (64-bit). Returns (value, new_offset)."""
    if offset + 8 > len(data):
        raise ProtobufDecodeError(f"truncated 64-bit field at offset {offset}")
    return struct.unpack("<d", data[offset:offset + 8])[0], offset + 8


def _decode_int64(data: bytes, offset: int) -> tuple[int, int]:
    """Decode an int64 field as varint. Returns (value, new_offset)."""
    return _decode_varint(data, offset)


def _skip_field(data: bytes, offset: int, wire_type: int, field_number: int) -> int:
    """Advance past an unknown field.

    Unknown fields are skipped rather than rejected: the wire format does not
    forbid a server from sending fields this client predates, and failing here
    would break the feed on any Upstox schema addition. Only genuinely
    unparseable bytes raise.
    """
    if wire_type == VARINT:
        _, offset = _decode_varint(data, offset)
    elif wire_type == LEN_DELIMITED:
        _, offset = _read_length_delimited(data, offset)
    elif wire_type == FIXED64:
        if offset + 8 > len(data):
            raise ProtobufDecodeError(f"truncated unknown 64-bit field {field_number}")
        offset += 8
    elif wire_type == FIXED32:
        if offset + 4 > len(data):
            raise ProtobufDecodeError(f"truncated unknown 32-bit field {field_number}")
        offset += 4
    else:
        # Wire types 3/4 (groups) and 6/7 are not valid in proto3.
        raise ProtobufDecodeError(
            f"field {field_number} has unsupported wire type {wire_type}"
        )
    return offset


def _read_tag(data: bytes, offset: int) -> tuple[int, int, int]:
    """Read a field tag. Returns (field_number, wire_type, new_offset)."""
    tag, offset = _decode_varint(data, offset)
    field_number = tag >> 3
    if field_number == 0:
        raise ProtobufDecodeError("field number 0 is not valid")
    return field_number, tag & 0x07, offset


def _decode_utf8(raw: bytes, what: str) -> str:
    """Decode a length-delimited string payload."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtobufDecodeError(f"invalid utf-8 in {what}") from exc



@dataclass
class LTPC:
    """Latest Trading Price and Close price."""
    ltp: float = 0.0
    ltt: int = 0  # Last traded time (milliseconds)
    ltq: int = 0  # Last traded quantity
    cp: float = 0.0  # Close price

    def serialize(self) -> bytes:
        """Serialize to protobuf bytes."""
        result = b""
        if self.ltp:
            result += _encode_double(1, self.ltp)
        if self.ltt:
            result += _encode_int64(2, self.ltt)
        if self.ltq:
            result += _encode_int64(3, self.ltq)
        if self.cp:
            result += _encode_double(4, self.cp)
        return result

    @classmethod
    def deserialize(cls, data: bytes) -> "LTPC":
        """Deserialize from protobuf bytes."""
        ltpc = cls()
        offset = 0
        while offset < len(data):
            field_number, wire_type, offset = _read_tag(data, offset)

            if field_number == 1:  # ltp
                if wire_type != FIXED64:
                    raise ProtobufDecodeError(
                        f"LTPC.ltp has wire type {wire_type}, expected {FIXED64}"
                    )
                ltpc.ltp, offset = _decode_double(data, offset)
            elif field_number == 2:  # ltt
                if wire_type != VARINT:
                    raise ProtobufDecodeError(
                        f"LTPC.ltt has wire type {wire_type}, expected {VARINT}"
                    )
                ltpc.ltt, offset = _decode_varint(data, offset)
            elif field_number == 3:  # ltq
                if wire_type != VARINT:
                    raise ProtobufDecodeError(
                        f"LTPC.ltq has wire type {wire_type}, expected {VARINT}"
                    )
                ltpc.ltq, offset = _decode_varint(data, offset)
            elif field_number == 4:  # cp
                if wire_type != FIXED64:
                    raise ProtobufDecodeError(
                        f"LTPC.cp has wire type {wire_type}, expected {FIXED64}"
                    )
                ltpc.cp, offset = _decode_double(data, offset)
            else:
                offset = _skip_field(data, offset, wire_type, field_number)
        return ltpc


@dataclass
class OHLC:
    """OHLC candle data."""
    interval: str = ""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    vol: int = 0
    ts: int = 0

    def serialize(self) -> bytes:
        """Serialize to protobuf bytes."""
        result = b""
        if self.interval:
            result += _encode_string(1, self.interval)
        if self.open:
            result += _encode_double(2, self.open)
        if self.high:
            result += _encode_double(3, self.high)
        if self.low:
            result += _encode_double(4, self.low)
        if self.close:
            result += _encode_double(5, self.close)
        if self.vol:
            result += _encode_int64(6, self.vol)
        if self.ts:
            result += _encode_int64(7, self.ts)
        return result

    @classmethod
    def deserialize(cls, data: bytes) -> "OHLC":
        """Deserialize from protobuf bytes."""
        ohlc = cls()
        offset = 0
        while offset < len(data):
            field_number, wire_type, offset = _read_tag(data, offset)

            if field_number == 1:  # interval
                if wire_type != LEN_DELIMITED:
                    raise ProtobufDecodeError(
                        f"OHLC.interval has wire type {wire_type}, expected "
                        f"{LEN_DELIMITED}"
                    )
                ohlc.interval, offset = _decode_string(data, offset)
            elif field_number in (2, 3, 4, 5):  # open/high/low/close
                if wire_type != FIXED64:
                    raise ProtobufDecodeError(
                        f"OHLC field {field_number} has wire type {wire_type}, "
                        f"expected {FIXED64}"
                    )
                value, offset = _decode_double(data, offset)
                if field_number == 2:
                    ohlc.open = value
                elif field_number == 3:
                    ohlc.high = value
                elif field_number == 4:
                    ohlc.low = value
                else:
                    ohlc.close = value
            elif field_number in (6, 7):  # vol, ts
                if wire_type != VARINT:
                    raise ProtobufDecodeError(
                        f"OHLC field {field_number} has wire type {wire_type}, "
                        f"expected {VARINT}"
                    )
                value, offset = _decode_varint(data, offset)
                if field_number == 6:
                    ohlc.vol = value
                else:
                    ohlc.ts = value
            else:
                offset = _skip_field(data, offset, wire_type, field_number)
        return ohlc


@dataclass
class MarketFullFeed:
    """Full market data feed."""
    ltpc: Optional[LTPC] = None
    market_level: Optional[bytes] = None  # Simplified for now
    option_greeks: Optional[bytes] = None  # Simplified for now
    market_ohlc: Optional[list[OHLC]] = field(default_factory=list)
    atp: float = 0.0
    vtt: int = 0
    oi: float = 0.0
    iv: float = 0.0
    tbq: float = 0.0
    tsq: float = 0.0

    def serialize(self) -> bytes:
        """Serialize to protobuf bytes."""
        result = b""
        if self.ltpc:
            ltpc_bytes = self.ltpc.serialize()
            result += _encode_tag(1, LEN_DELIMITED) + _encode_varint(len(ltpc_bytes)) + ltpc_bytes
        for ohlc in self.market_ohlc:
            ohlc_bytes = ohlc.serialize()
            result += _encode_tag(4, LEN_DELIMITED) + _encode_varint(len(ohlc_bytes)) + ohlc_bytes
        if self.atp:
            result += _encode_double(5, self.atp)
        if self.vtt:
            result += _encode_int64(6, self.vtt)
        if self.oi:
            result += _encode_double(7, self.oi)
        if self.iv:
            result += _encode_double(8, self.iv)
        if self.tbq:
            result += _encode_double(9, self.tbq)
        if self.tsq:
            result += _encode_double(10, self.tsq)
        return result

    @classmethod
    def deserialize(cls, data: bytes) -> "MarketFullFeed":
        """Deserialize from protobuf bytes."""
        feed = cls()
        offset = 0
        while offset < len(data):
            field_number, wire_type, offset = _read_tag(data, offset)

            if wire_type == LEN_DELIMITED:
                field_data, offset = _read_length_delimited(data, offset)

                if field_number == 1:  # ltpc
                    feed.ltpc = LTPC.deserialize(field_data)
                elif field_number == 4:  # market_ohlc
                    feed.market_ohlc.append(OHLC.deserialize(field_data))
                # Other length-delimited fields are skipped by the read above.
            elif wire_type == VARINT:
                if field_number == 6:  # vtt
                    feed.vtt, offset = _decode_varint(data, offset)
                else:
                    _, offset = _decode_varint(data, offset)
            elif wire_type == FIXED64:
                if field_number == 5:  # atp
                    feed.atp, offset = _decode_double(data, offset)
                elif field_number == 7:  # oi
                    feed.oi, offset = _decode_double(data, offset)
                elif field_number == 8:  # iv
                    feed.iv, offset = _decode_double(data, offset)
                elif field_number == 9:  # tbq
                    feed.tbq, offset = _decode_double(data, offset)
                elif field_number == 10:  # tsq
                    feed.tsq, offset = _decode_double(data, offset)
                else:
                    if offset + 8 > len(data):
                        raise ProtobufDecodeError(
                            f"truncated unknown 64-bit field {field_number}"
                        )
                    offset += 8
            else:
                raise ProtobufDecodeError(
                    f"field {field_number} has unsupported wire type {wire_type}"
                )
        return feed


@dataclass
class Feed:
    """A single feed entry for an instrument."""
    ltpc: Optional[LTPC] = None
    full_feed: Optional[MarketFullFeed] = None
    request_mode: RequestMode = RequestMode.LTPC

    def serialize(self) -> bytes:
        """Serialize to protobuf bytes."""
        result = b""
        if self.ltpc:
            ltpc_bytes = self.ltpc.serialize()
            result += _encode_tag(1, LEN_DELIMITED) + _encode_varint(len(ltpc_bytes)) + ltpc_bytes
        if self.full_feed:
            ff_bytes = self.full_feed.serialize()
            result += _encode_tag(2, LEN_DELIMITED) + _encode_varint(len(ff_bytes)) + ff_bytes
        if self.request_mode != RequestMode.LTPC:
            result += _encode_varint_field(4, self.request_mode)
        return result

    @classmethod
    def deserialize(cls, data: bytes) -> "Feed":
        """Deserialize from protobuf bytes."""
        feed = cls()
        offset = 0
        while offset < len(data):
            field_number, wire_type, offset = _read_tag(data, offset)

            if wire_type == LEN_DELIMITED:
                field_data, offset = _read_length_delimited(data, offset)

                if field_number == 1:  # ltpc
                    feed.ltpc = LTPC.deserialize(field_data)
                elif field_number == 2:  # full_feed
                    feed.full_feed = MarketFullFeed.deserialize(field_data)
            elif wire_type == VARINT:
                if field_number == 4:  # request_mode
                    mode_val, offset = _decode_varint(data, offset)
                    feed.request_mode = RequestMode(mode_val)
                else:
                    _, offset = _decode_varint(data, offset)
            else:
                offset = _skip_field(data, offset, wire_type, field_number)
        return feed


@dataclass
class FeedResponse:
    """Top-level feed response message."""
    type: FeedType = FeedType.LIVE_FEED
    feeds: dict[str, Feed] = field(default_factory=dict)
    current_ts: int = 0
    # Populated when an individual instrument entry in the feeds map could not
    # be parsed. A single corrupt instrument must not cost every other
    # instrument in the frame, so those entries are collected and reported
    # rather than aborting the whole decode. Not part of the wire format.
    decode_errors: list = field(default_factory=list)

    def serialize(self) -> bytes:
        """Serialize to protobuf bytes."""
        result = b""
        if self.type != FeedType.LIVE_FEED:
            result += _encode_varint_field(1, self.type)
        for key, feed in self.feeds.items():
            feed_bytes = feed.serialize()
            # Encode as map entry (field 2)
            map_entry = _encode_string(1, key) + _encode_tag(2, LEN_DELIMITED) + _encode_varint(len(feed_bytes)) + feed_bytes
            result += _encode_tag(2, LEN_DELIMITED) + _encode_varint(len(map_entry)) + map_entry
        if self.current_ts:
            result += _encode_int64(3, self.current_ts)
        return result

    @classmethod
    def deserialize(cls, data: bytes) -> "FeedResponse":
        """Deserialize from protobuf bytes."""
        response = cls()
        offset = 0
        while offset < len(data):
            field_number, wire_type, offset = _read_tag(data, offset)

            if wire_type == VARINT:
                if field_number == 1:  # type
                    type_val, offset = _decode_varint(data, offset)
                    response.type = FeedType(type_val)
                elif field_number == 3:  # current_ts
                    response.current_ts, offset = _decode_varint(data, offset)
                else:
                    _, offset = _decode_varint(data, offset)
            elif wire_type == LEN_DELIMITED:
                entry_data, offset = _read_length_delimited(data, offset)
                if field_number == 2:  # feeds map
                    try:
                        key, feed = _decode_map_entry(entry_data)
                    except ProtobufDecodeError as e:
                        # Isolate the bad instrument; the rest of the frame
                        # stays usable.
                        response.decode_errors.append(e)
                        continue
                    response.feeds[key] = feed
            else:
                offset = _skip_field(data, offset, wire_type, field_number)
        return response


def _decode_map_entry(data: bytes) -> tuple[str, Feed]:
    """Decode a map entry (key, Feed)."""
    key = ""
    feed = Feed()
    offset = 0
    while offset < len(data):
        field_number, wire_type, offset = _read_tag(data, offset)

        if wire_type == LEN_DELIMITED:
            field_data, offset = _read_length_delimited(data, offset)

            if field_number == 1:  # key
                try:
                    key = field_data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ProtobufDecodeError("invalid utf-8 in map key") from exc
            elif field_number == 2:  # value (Feed)
                feed = Feed.deserialize(field_data)
        else:
            offset = _skip_field(data, offset, wire_type, field_number)
    return key, feed


@dataclass
class SubscriptionRequest:
    """V3 subscription request message."""
    guid: str = ""
    method: str = "sub"  # sub, unsub, change_mode
    mode: RequestMode = RequestMode.LTPC
    instrument_keys: list[str] = field(default_factory=list)

    def serialize(self) -> bytes:
        """Serialize to binary format for WebSocket."""
        # Build the data part
        data = b""
        if self.mode != RequestMode.LTPC:
            data += _encode_varint_field(1, self.mode)
        for key in self.instrument_keys:
            data += _encode_string(2, key)

        # Build the full request
        result = b""
        if self.guid:
            result += _encode_string(1, self.guid)
        if self.method:
            result += _encode_string(2, self.method)
        if data:
            result += _encode_tag(3, LEN_DELIMITED) + _encode_varint(len(data)) + data
        return result

    @classmethod
    def deserialize(cls, data: bytes) -> "SubscriptionRequest":
        """Deserialize from binary format."""
        request = cls()
        offset = 0
        while offset < len(data):
            field_number, wire_type, offset = _read_tag(data, offset)

            if wire_type == LEN_DELIMITED:
                field_data, offset = _read_length_delimited(data, offset)

                if field_number == 1:  # guid
                    request.guid = _decode_utf8(field_data, "guid")
                elif field_number == 2:  # method
                    request.method = _decode_utf8(field_data, "method")
                elif field_number == 3:  # data
                    # Parse data fields
                    data_offset = 0
                    while data_offset < len(field_data):
                        sub_field, sub_wire, data_offset = _read_tag(
                            field_data, data_offset
                        )

                        if sub_wire == VARINT and sub_field == 1:  # mode
                            mode_val, data_offset = _decode_varint(field_data, data_offset)
                            request.mode = RequestMode(mode_val)
                        elif sub_wire == LEN_DELIMITED and sub_field == 2:  # instrumentKeys
                            key_data, data_offset = _read_length_delimited(
                                field_data, data_offset
                            )
                            request.instrument_keys.append(
                                _decode_utf8(key_data, "instrument key")
                            )
                        else:
                            data_offset = _skip_field(
                                field_data, data_offset, sub_wire, sub_field
                            )
            else:
                offset = _skip_field(data, offset, wire_type, field_number)
        return request
