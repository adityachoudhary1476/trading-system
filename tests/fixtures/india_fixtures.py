"""Offline fixtures for Indian-market (Upstox) testing.

CRITICAL: These are SYNTHETIC, deterministic fixtures that mirror the *documented*
Upstox v2 response shapes (see docs/UPSTOX.md). They are for testing normalization,
parsing, and pipeline logic only. They are NOT live market data and must never be
presented as such.
"""
from __future__ import annotations

from datetime import datetime, timezone


def upstox_history_response(symbol: str = "NSE:SBIN", bars: list | None = None) -> dict:
    """A /historical-candle response shaped exactly like the documented Upstox payload.

    candles: [epoch, open, high, low, close, volume, oi]
    """
    if bars is None:
        base = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
        bars = [
            [base, 100.0, 102.0, 99.0, 101.0, 1000.0, 5000.0],
            [base + 86400, 101.0, 103.0, 100.0, 102.0, 1100.0, 5100.0],
            [base + 2 * 86400, 102.0, 104.0, 101.0, 103.5, 980.0, 5050.0],
        ]
    return {
        "status": "success",
        "data": {"candles": bars, "symbol": symbol},
    }


def upstox_history_empty(symbol: str = "NSE:SBIN") -> dict:
    return {"status": "success", "data": {"candles": [], "symbol": symbol}}


def upstox_history_error() -> dict:
    # Upstox uses {"status": "error", "errors": [{"code":..., "message":...}]} on failure.
    return {"status": "error", "errors": [{"code": -1, "message": "Invalid symbol"}]}


def upstox_ws_symbol_update(symbol: str = "NSE:SBIN-EQ", last_price: float = 123.45) -> dict:
    """Full-mode market dict as decoded by the Upstox v2 WebSocket feed.

    Keys: symbol, last_price, open_price, high_price, low_price, volume, ...
    """
    return {
        "symbol": symbol,
        "last_price": last_price,
        "open_price": 121.0,
        "high_price": 124.0,
        "low_price": 120.5,
        "volume": 98765.0,
        "prev_close_price": 122.0,
        "type": "sf",
    }


def upstox_ws_lite(symbol: str = "NSE:SBIN-EQ", last_price: float = 123.45) -> dict:
    """Lite (LTP-only) dict as received from the Upstox feed."""
    return {"symbol": symbol, "last_price": last_price, "type": "sf"}


def upstox_ws_heartbeat() -> dict:
    # Control/response frames carry no "symbol" -> skipped by _normalize.
    return {"type": "ack"}


def upstox_ws_auth_ack() -> dict:
    return {"type": "auth_ack"}


def upstox_ws_malformed() -> dict:
    # Decoded dict missing the price/control frame -> skipped (no crash).
    return {"type": "subscribed"}


def upstox_ws_unknown_type() -> dict:
    return {"foo": "bar"}


# --- Upstox V3 protobuf feed fixtures ---------------------------------------
# V3 delivers binary protobuf FeedResponse frames, not the v2 JSON dicts above.
# These build real serializable protobuf messages so the decode path is
# exercised end to end rather than stubbed out.

V3_SBIN_KEY = "NSE_EQ|INE020B01018"
V3_NIFTY_KEY = "NSE_INDEX|NIFTY 50"


def v3_feed_response(
    instrument_key: str = V3_SBIN_KEY,
    ltp: float = 123.45,
    ltt_ms: int = 1_700_000_000_000,
    current_ts: int = 1_700_000_000_000,
) -> "object":
    """A single-instrument LTPC FeedResponse message."""
    from trading_system.india.upstox_v3_pb import Feed, FeedResponse, LTPC

    return FeedResponse(
        feeds={
            instrument_key: Feed(
                ltpc=LTPC(ltp=ltp, ltt=ltt_ms, cp=ltp),
            )
        },
        current_ts=current_ts,
    )


def v3_feed_response_full(
    instrument_key: str = V3_SBIN_KEY,
    ltp: float = 123.45,
    ltt_ms: int = 1_700_000_000_000,
) -> "object":
    """A FeedResponse carrying a full feed (LTPC + OHLC)."""
    from trading_system.india.upstox_v3_pb import Feed, FeedResponse, LTPC, MarketFullFeed, OHLC

    return FeedResponse(
        feeds={
            instrument_key: Feed(
                ltpc=LTPC(ltp=ltp, ltt=ltt_ms, cp=ltp),
                full_feed=MarketFullFeed(
                    ltpc=LTPC(ltp=ltp, ltt=ltt_ms, cp=ltp),
                    market_ohlc=[OHLC(open=120.0, high=125.0, low=119.0, close=123.45, vol=98765)],
                ),
            )
        },
        current_ts=ltt_ms,
    )


def v3_feed_response_no_price(instrument_key: str = V3_SBIN_KEY) -> "object":
    """A FeedResponse whose feed carries no LTPC - must be skipped, not crash."""
    from trading_system.india.upstox_v3_pb import Feed, FeedResponse, LTPC

    return FeedResponse(
        feeds={instrument_key: Feed(ltpc=LTPC(ltp=0.0, ltt=0, cp=0.0))},
        current_ts=1_700_000_000_000,
    )


def v3_feed_bytes(*args, **kwargs) -> bytes:
    """Serialized V3 FeedResponse frame, as delivered by websocket."""
    return v3_feed_response(*args, **kwargs).serialize()


# --- Instrument master fixtures (provider-independent) ----------------------
# Instrument master is a CSV/JSON with columns like:
#   Symbol, Exch, Token, Instrument, Expiry, StrikePrice, OptionType, LotSize
# We model the rows we need; the parser must not assume this is exhaustive.
INSTRUMENT_MASTER_ROWS = [
    # equity
    {"symbol": "RELIANCE-EQ", "exch": "NSE", "token": "2885", "instrument": "RELIANCE",
     "expiry": "", "strike": "", "option_type": "", "lot_size": "1"},
    {"symbol": "SBIN-EQ", "exch": "NSE", "token": "3045", "instrument": "SBIN",
     "expiry": "", "strike": "", "option_type": "", "lot_size": "1"},
    {"symbol": "INFY-EQ", "exch": "NSE", "token": "1594", "instrument": "INFY",
     "expiry": "", "strike": "", "option_type": "", "lot_size": "1"},
    # index
    {"symbol": "NIFTY50-INDEX", "exch": "NSE", "token": "99926000", "instrument": "NIFTY50",
     "expiry": "", "strike": "", "option_type": "", "lot_size": "75"},
    {"symbol": "NIFTYBANK-INDEX", "exch": "NSE", "token": "99926009", "instrument": "NIFTYBANK",
     "expiry": "", "strike": "", "option_type": "", "lot_size": "25"},
    # option (CE)
    {"symbol": "SBIN25DEC400CE", "exch": "NSE", "token": "99999123", "instrument": "SBIN",
     "expiry": "2025-12-25", "strike": "400", "option_type": "CE", "lot_size": "3000"},
    # option (PE)
    {"symbol": "SBIN25DEC400PE", "exch": "NSE", "token": "99999124", "instrument": "SBIN",
     "expiry": "2025-12-25", "strike": "400", "option_type": "PE", "lot_size": "3000"},
    # future
    {"symbol": "SBIN25DECFUT", "exch": "NSE", "token": "99999200", "instrument": "SBIN",
     "expiry": "2025-12-25", "strike": "", "option_type": "FUT", "lot_size": "3000"},
]


def instrument_master_csv() -> str:
    """Render the fixture rows as the documented CSV layout."""
    header = "Symbol,Exch,Token,Instrument,Expiry,StrikePrice,OptionType,LotSize"
    lines = [header]
    for r in INSTRUMENT_MASTER_ROWS:
        lines.append(
            f"{r['symbol']},{r['exch']},{r['token']},{r['instrument']},"
            f"{r['expiry']},{r['strike']},{r['option_type']},{r['lot_size']}"
        )
    return "\n".join(lines)
