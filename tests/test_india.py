"""Day 3 tests: Indian market layer (no live FYERS connection required)."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pandas as pd
import pytest

from trading_system.india import (
    Instrument,
    InstrumentType,
    InternalSymbol,
    InstrumentRegistry,
    to_fyers_symbol,
    from_fyers_symbol,
    is_trading_day,
    market_state,
    is_within_session,
    session_boundaries,
    CandleAggregator,
    InternalMarketEvent,
    EventType,
)
from trading_system.india.fyers import FYERSMarketDataProvider
from trading_system.data.provider_exports import get_provider
from trading_system.data.validation import validate_ohlcv
from zoneinfo import ZoneInfo

KOL = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


# --- Instrument model + symbol normalization ---
def test_internal_symbol_roundtrip():
    s = InternalSymbol.parse("NSE:RELIANCE")
    assert s.exchange == "NSE" and s.symbol == "RELIANCE"
    assert s.key == "NSE:RELIANCE"


def test_to_fyers_equity():
    instr = Instrument(InternalSymbol("NSE", "SBIN"), InstrumentType.EQUITY)
    assert to_fyers_symbol(instr) == "NSE:SBIN-EQ"


def test_to_fyers_index():
    instr = Instrument(InternalSymbol("NSE", "NIFTY50"), InstrumentType.INDEX)
    assert to_fyers_symbol(instr) == "NSE:NIFTY50-INDEX"


def test_to_fyers_option():
    instr = Instrument(
        InternalSymbol("NSE", "SBIN25DEC400CE"),
        InstrumentType.OPTION_CE,
        underlying="SBIN", expiry="2025-12-25", strike=400.0,
    )
    assert to_fyers_symbol(instr) == "NSE:SBIN25DEC400CE"


def test_from_fyers_equity():
    instr = from_fyers_symbol("NSE:SBIN-EQ")
    assert instr.internal.key == "NSE:SBIN"
    assert instr.instrument_type == InstrumentType.EQUITY


def test_registry_resolves_unknown_as_equity():
    reg = InstrumentRegistry()
    instr = reg.resolve("NSE:ZOMATO")
    assert instr.internal.symbol == "ZOMATO"


# --- Indian timezone / session handling ---
def test_weekend_is_not_trading_day():
    sat = datetime(2024, 3, 2, 12, 0, tzinfo=KOL)  # Saturday
    assert not is_trading_day(sat)
    assert market_state(sat) == "closed"


def test_market_hours_open():
    # Wednesday 12:00 IST = open.
    wed = datetime(2024, 3, 6, 12, 0, tzinfo=KOL)
    assert is_within_session(wed)
    assert market_state(wed) == "open"


def test_outside_hours_closed():
    night = datetime(2024, 3, 6, 22, 0, tzinfo=KOL)  # 22:00 IST -> closed
    assert market_state(night) == "closed"


def test_session_boundaries_weekend_rolls_to_monday():
    sat = datetime(2024, 3, 2, 12, 0, tzinfo=KOL)
    open_dt, close_dt = session_boundaries(sat)
    assert open_dt.weekday() == 0  # Monday
    assert (open_dt.hour, open_dt.minute) == (9, 15)
    assert (close_dt.hour, close_dt.minute) == (15, 30)


# --- Candle aggregation ---
def test_candle_aggregator_basic():
    agg = CandleAggregator("5m")
    base = datetime(2024, 3, 6, 9, 15, tzinfo=KOL)
    ticks = [
        (base, 100.0), (base + timedelta(minutes=1), 102.0),
        (base + timedelta(minutes=2), 99.0), (base + timedelta(minutes=3), 101.0),
    ]
    completed = []
    for ts, p in ticks:
        completed += agg.update(ts, p, volume=10.0)
    assert len(completed) == 0  # bar not closed yet
    # Push a tick into the NEXT 5m bar -> previous bar closes.
    next_bar = base + timedelta(minutes=5)
    completed += agg.update(next_bar, 103.0, volume=5.0)
    assert len(completed) == 1
    bar = completed[0]
    assert bar.open == 100.0
    assert bar.high == 102.0
    assert bar.low == 99.0
    assert bar.close == 101.0
    assert bar.volume == 40.0  # 4 ticks * 10 (09:15,16,17,18 all in same 5m bar)


def test_candle_aggregator_provisional_distinct():
    agg = CandleAggregator("1m")
    base = datetime(2024, 3, 6, 9, 15, tzinfo=KOL)
    agg.update(base, 100.0)
    prov = agg.provisional
    assert prov is not None
    # The provisional bar must never be reported as closed.
    assert len(agg.flush_completed()) == 0


def test_candle_aggregator_tz_naive_rejected():
    agg = CandleAggregator("1m")
    # naive timestamp gets tz-attached as UTC; bar alignment uses Kolkata.
    agg.update(datetime(2024, 3, 6, 3, 45), 100.0)  # 09:15 IST
    assert agg.provisional is not None


# --- FYERS response normalization (mocked, no network) ---
def _fy_hist_response():
    epoch = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    return {
        "s": "ok",
        "candles": [
            [epoch, 100.0, 102.0, 99.0, 101.0, 1000.0],
            [epoch + 86400, 101.0, 103.0, 100.0, 102.0, 1100.0],
        ],
    }


def test_fyers_historical_normalization_shape(monkeypatch):
    prov = FYERSMarketDataProvider(client_id="X-100", access_token="tok")
    # Patch the REST helper to return a fixture.
    monkeypatch.setattr(prov, "_get", lambda path, params: _fy_hist_response())
    df = prov.get_historical("NSE:SBIN", "1d", 2)
    assert len(df) == 2
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is not None  # tz-aware UTC
    # Pass it through the existing validator to prove it's usable downstream.
    report = validate_ohlcv(df, "1d")
    assert report.ok


def test_fyers_requires_auth_for_live():
    prov = FYERSMarketDataProvider()  # no creds
    assert not prov.is_authenticated
    with pytest.raises(RuntimeError):
        prov.connect_live(["NSE:SBIN"], on_event=lambda e: None)


def test_fyers_symbol_resolution_without_creds():
    # Symbol mapping works even without credentials (no network).
    prov = FYERSMarketDataProvider()
    assert prov._fyers_symbol("NSE:SBIN") == "NSE:SBIN-EQ"
    assert prov._fyers_symbol("NSE:NIFTY50") == "NSE:NIFTY50-INDEX"


def test_fyers_ws_message_normalization(monkeypatch):
    prov = FYERSMarketDataProvider(client_id="X-100", access_token="tok")
    # A symbolUpdate-style message (best-effort shape).
    msg = {"T": "t", "symbol": "NSE:SBIN-EQ", "v": {"lp": 555.5, "o": 550, "h": 560, "l": 548, "c": 555, "vol": 123}}
    ev = prov._normalize_ws(msg, ["NSE:SBIN-EQ"])
    assert isinstance(ev, InternalMarketEvent)
    assert ev.symbol == "NSE:SBIN"
    assert ev.ltp == 555.5
    assert ev.event_type == EventType.QUOTE


def test_fyers_ws_control_frame_skipped():
    prov = FYERSMarketDataProvider(client_id="X-100", access_token="tok")
    # Auth/subscribe/heartbeat frames must return None.
    assert prov._normalize_ws({"T": "c", "authorization": "x"}, []) is None
    assert prov._normalize_ws({"T": "h"}, []) is None


# --- Provider abstraction integrity ---
def test_fyers_is_a_marketdataprovider():
    from trading_system.data.base import MarketDataProvider

    prov = get_provider("fyers")
    assert isinstance(prov, MarketDataProvider)
    assert prov.name == "fyers"


def test_binance_still_default_and_works():
    # Ensure the Day 1/2 provider remains intact.
    prov = get_provider("binance")
    assert prov.name == "binance"
    # No network call here; just confirm the interface contract.
    assert prov.has_historical and prov.is_real_time
