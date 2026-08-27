"""Offline fixtures for Indian-market (FYERS) testing.

CRITICAL: These are SYNTHETIC, deterministic fixtures that mirror the *documented*
FYERS v3 response shapes (see docs/FYERS.md). They are for testing normalization,
parsing, and pipeline logic only. They are NOT live market data and must never be
presented as such.
"""
from __future__ import annotations

from datetime import datetime, timezone


def fyers_history_response(symbol: str = "NSE:SBIN-EQ", bars: list | None = None) -> dict:
    """A /history response shaped exactly like the documented FYERS payload.

    candles: [epoch, open, high, low, close, volume]
    """
    if bars is None:
        base = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
        bars = [
            [base, 100.0, 102.0, 99.0, 101.0, 1000.0],
            [base + 86400, 101.0, 103.0, 100.0, 102.0, 1100.0],
            [base + 2 * 86400, 102.0, 104.0, 101.0, 103.5, 980.0],
        ]
    return {"s": "ok", "candles": bars, "symbol": symbol}


def fyers_history_empty(symbol: str = "NSE:SBIN-EQ") -> dict:
    return {"s": "ok", "candles": [], "symbol": symbol}


def fyers_history_error() -> dict:
    # FYERS uses {"s":"error","code":...,"message":...} on failure.
    return {"s": "error", "code": -1, "message": "Invalid symbol"}


def fyers_ws_symbol_update(symbol: str = "NSE:SBIN-EQ", ltp: float = 123.45) -> dict:
    """Full-mode market dict as decoded by the official fyers_apiv3 SDK.

    The real FYERS v3 data socket is binary protobuf; the SDK decodes it into a
    dict with precision/multiplier already applied. Keys (full mode):
    symbol, ltp, open_price, high_price, low_price, vol_traded_today, ...
    """
    return {
        "symbol": symbol,
        "ltp": ltp,
        "open_price": 121.0,
        "high_price": 124.0,
        "low_price": 120.5,
        "vol_traded_today": 98765,
        "prev_close_price": 122.0,
        "type": "sf",
    }


def fyers_ws_lite(symbol: str = "NSE:SBIN-EQ", ltp: float = 123.45) -> dict:
    """Lite (LTP-only) dict as decoded by the SDK."""
    return {"symbol": symbol, "ltp": ltp, "type": "sf"}


def fyers_ws_heartbeat() -> dict:
    # SDK control/response frames carry no "symbol" -> skipped by _normalize.
    return {"type": "ack"}


def fyers_ws_auth_ack() -> dict:
    return {"type": "auth_ack"}


def fyers_ws_malformed() -> dict:
    # Decoded dict missing the price/control frame -> skipped (no crash).
    return {"type": "subscribed"}


def fyers_ws_unknown_type() -> dict:
    return {"foo": "bar"}


# --- Instrument master fixtures (documented CSV-style structure) -------------
# FYERS symbol master is a CSV/JSON with columns like:
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
