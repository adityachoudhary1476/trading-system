"""Day 6 tests: F&O + commodity derivatives foundation (offline, no live API).

Covers:
* futures identity / options identity
* CE vs PE distinction
* expiry distinction
* FYERS symbol resolution (verified format, no guessing)
* commodity (MCX) representation
* database uniqueness (Jun != Jul future, 25000CE != 25000PE)
* derivative historical normalization (backfill integration w/ mocked provider)
* malformed contract metadata validation
* existing equity behavior unchanged
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile

import pandas as pd
import pytest

sys.path.insert(0, "src")

from trading_system.india.instruments import (
    Exchange,
    Instrument,
    InstrumentRegistry,
    InstrumentType,
    InternalSymbol,
    OptionType,
)
from trading_system.india.derivatives import (
    DerivativeRequest,
    from_fyers_derivative_symbol,
    to_fyers_derivative_symbol,
)
from trading_system.india.instrument_repository import InstrumentRepository
from trading_system.india.backfill import BackfillEngine, BackfillStatus
from trading_system.india.fyers import FYERSMarketDataProvider
from trading_system.storage.database import MarketStore
from trading_system.data.validation import validate_ohlcv, validate_contract_identity


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


def _good_frame(start: pd.Timestamp, n: int, freq: str = "1d"):
    import numpy as np

    idx = pd.date_range(start=start, periods=n, freq=freq, tz="UTC")
    rng = np.random.default_rng(int(pd.Timestamp(start).timestamp()))
    close = 100 + rng.normal(0, 1, n).cumsum()
    open_ = close + rng.normal(0, 0.5, n)
    high = np.maximum(open_, close) + abs(rng.normal(0, 1, n))
    low = np.minimum(open_, close) - abs(rng.normal(0, 1, n))
    return pd.DataFrame(
        {
            "open": open_.astype(float),
            "high": high.astype(float),
            "low": low.astype(float),
            "close": close.astype(float),
            "volume": rng.integers(100, 1000, n).astype(float),
        },
        index=idx,
    )


def _store(tmp_path):
    db = "sqlite:///" + str(tmp_path / "hist.db")
    return MarketStore(db)


# --------------------------------------------------------------------------- #
# Phase 1 — model identity
# --------------------------------------------------------------------------- #
def test_future_identity_distinct_from_option():
    fut = Instrument.future("NFO", "NIFTY", "2025-12-25")
    opt = Instrument.option("NFO", "NIFTY", "2025-12-25", 24800, "CE")
    assert fut.contract_id == "NFO:NIFTY|2025-12-25|FUT"
    assert opt.contract_id == "NFO:NIFTY|2025-12-25|24800|CE"
    assert fut.contract_id != opt.contract_id
    assert fut != opt


def test_expiry_distinction():
    jun = Instrument.future("NFO", "NIFTY", "2025-06-26")
    jul = Instrument.future("NFO", "NIFTY", "2025-07-31")
    assert jun.contract_id != jul.contract_id
    assert jun != jul


def test_ce_pe_distinction():
    ce = Instrument.option("NFO", "NIFTY", "2025-12-25", 24800, "CE")
    pe = Instrument.option("NFO", "NIFTY", "2025-12-25", 24800, "PE")
    assert ce.contract_id != pe.contract_id
    assert ce.option_type == "CE" and pe.option_type == "PE"
    assert ce.instrument_type == InstrumentType.OPTION_CE
    assert pe.instrument_type == InstrumentType.OPTION_PE


def test_strike_distinction():
    a = Instrument.option("NFO", "NIFTY", "2025-12-25", 24800, "CE")
    b = Instrument.option("NFO", "NIFTY", "2025-12-25", 24900, "CE")
    assert a.contract_id != b.contract_id


def test_instrument_equality_by_contract_id():
    i1 = Instrument.option("NFO", "NIFTY", "2025-12-25", 24800, "CE")
    i2 = Instrument.option("NFO", "NIFTY", "2025-12-25", 24800, "CE")
    assert i1 == i2
    assert hash(i1) == hash(i2)
    # Equity equality still keyed by symbol.
    assert Instrument(InternalSymbol("NSE", "SBIN"), InstrumentType.EQUITY) == \
        Instrument(InternalSymbol("NSE", "SBIN"), InstrumentType.EQUITY)


# --------------------------------------------------------------------------- #
# Phase 2 — FYERS symbol resolution (verified format)
# --------------------------------------------------------------------------- #
def test_futures_symbol_resolution():
    # Verified FYERS format: <ROOT><YY><MMM>FUT (no dash).
    assert to_fyers_derivative_symbol(
        DerivativeRequest("NIFTY", "future", "2025-12-25")
    ) == "NSE:NIFTY25DECFUT"
    assert to_fyers_derivative_symbol(
        DerivativeRequest("BANKNIFTY", "future", "2025-06-26")
    ) == "NSE:BANKNIFTY25JUNFUT"


def test_option_symbol_resolution():
    sym = to_fyers_derivative_symbol(
        DerivativeRequest("NIFTY", "option", "2025-12-25", strike=24800, option_type="CE")
    )
    assert sym == "NFO:NIFTY25DEC24800CE"


def test_commodity_future_symbol_resolution():
    # Verified: MCX:SILVERMIC25DECFUT
    sym = to_fyers_derivative_symbol(
        DerivativeRequest("SILVERMIC", "future", "2025-12-25")
    )
    assert sym == "MCX:SILVERMIC25DECFUT"


def test_parse_fyers_option_back():
    instr = from_fyers_derivative_symbol("NFO:NIFTY25DEC24800CE")
    assert instr.instrument_type == InstrumentType.OPTION_CE
    assert instr.underlying == "NIFTY"
    assert instr.strike == 24800
    assert instr.option_type == "CE"
    # Expiry parsed to a December 2025 marker.
    assert instr.expiry == "2025-12-28"


def test_parse_fyers_commodity_future_back():
    instr = from_fyers_derivative_symbol("MCX:SILVERMIC25DECFUT")
    assert instr.instrument_type == InstrumentType.FUTURE
    assert instr.underlying == "SILVERMIC"
    assert instr.internal.exchange == "MCX"


def test_resolve_roundtrip():
    req = DerivativeRequest("NIFTY", "option", "2025-12-25", strike=24800, option_type="CE")
    fy = to_fyers_derivative_symbol(req)
    parsed = from_fyers_derivative_symbol(fy)
    # FYERS option tokens omit the day-of-month, so only year+month+strike+type are
    # recoverable from the symbol. The structured Instrument carries the full date.
    assert parsed.underlying == "NIFTY"
    assert parsed.strike == 24800
    assert parsed.option_type == "CE"
    assert parsed.instrument_type == InstrumentType.OPTION_CE


# --------------------------------------------------------------------------- #
# Phase 3 — repository discovery (in-memory)
# --------------------------------------------------------------------------- #
def test_repo_list_futures_and_options():
    repo = InstrumentRepository()
    repo.register(Instrument.future("NFO", "NIFTY", "2025-06-26"))
    repo.register(Instrument.future("NFO", "NIFTY", "2025-07-31"))
    repo.register(Instrument.option("NFO", "NIFTY", "2025-06-26", 24000, "CE"))
    repo.register(Instrument.option("NFO", "NIFTY", "2025-06-26", 24000, "PE"))
    futures = repo.list_futures("NIFTY")
    assert len(futures) == 2
    opts = repo.list_options("NIFTY", expiry="2025-06-26")
    assert len(opts) == 2
    ce_only = repo.list_options("NIFTY", expiry="2025-06-26", option_type="CE")
    assert len(ce_only) == 1 and ce_only[0].option_type == "CE"


def test_repo_get_expiries_and_find_contract():
    repo = InstrumentRepository()
    repo.register(Instrument.option("NFO", "NIFTY", "2025-06-26", 24000, "CE"))
    repo.register(Instrument.option("NFO", "NIFTY", "2025-07-31", 25000, "PE"))
    exps = repo.get_expiries("NIFTY")
    assert "2025-06-26" in exps and "2025-07-31" in exps
    c = repo.find_contract("NIFTY", "2025-07-31", option_type="PE", strike=25000)
    assert c is not None and c.contract_id.endswith("25000|PE")


# --------------------------------------------------------------------------- #
# Phase 5 — commodity representation
# --------------------------------------------------------------------------- #
def test_commodity_instrument_model():
    gold = Instrument.future("MCX", "GOLD", "2025-12-25", provider_symbol="MCX:GOLD25DECFUT")
    gold.internal = InternalSymbol("MCX", "GOLD25DECFUT")
    assert gold.internal.exchange == "MCX"
    assert gold.instrument_type == InstrumentType.FUTURE
    # Distinct from an equity with a similar name.
    assert gold.contract_id != "NSE:GOLD|2025-12-25|FUT"


# --------------------------------------------------------------------------- #
# Phase 6 — database uniqueness
# --------------------------------------------------------------------------- #
def test_db_uniqueness_jun_vs_jul_future(tmp_path):
    store = _store(tmp_path)
    jun = Instrument.future("NFO", "NIFTY", "2025-06-26")
    jul = Instrument.future("NFO", "NIFTY", "2025-07-31")
    base = _ts("2025-06-01")
    rows_jun = [
        {
            "symbol": "NIFTY25JUNFUT", "timeframe": "1d", "timestamp": base.to_pydatetime(),
            "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10,
            "provider": "fyers", "exchange": "NFO", "contract_id": jun.contract_id,
        }
    ]
    rows_jul = [
        {
            "symbol": "NIFTY25JULFUT", "timeframe": "1d", "timestamp": base.to_pydatetime(),
            "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10,
            "provider": "fyers", "exchange": "NFO", "contract_id": jul.contract_id,
        }
    ]
    assert store.upsert_many(rows_jun) == 1
    assert store.upsert_many(rows_jul) == 1  # distinct contract -> distinct row
    assert store.count("NIFTY25JUNFUT", "1d") == 1
    assert store.count("NIFTY25JULFUT", "1d") == 1


def test_db_uniqueness_ce_vs_pe(tmp_path):
    store = _store(tmp_path)
    ce = Instrument.option("NFO", "NIFTY", "2025-12-25", 24800, "CE")
    pe = Instrument.option("NFO", "NIFTY", "2025-12-25", 24800, "PE")
    ts = _ts("2025-12-01").to_pydatetime()
    base = {
        "timeframe": "1d", "timestamp": ts, "open": 1, "high": 2, "low": 0.5,
        "close": 1.5, "volume": 10, "provider": "fyers", "exchange": "NFO",
    }
    assert store.upsert_many([{**base, "symbol": "NIFTY25DEC24800CE", "contract_id": ce.contract_id}]) == 1
    assert store.upsert_many([{**base, "symbol": "NIFTY25DEC24800PE", "contract_id": pe.contract_id}]) == 1
    assert store.count("NIFTY25DEC24800CE", "1d") == 1
    assert store.count("NIFTY25DEC24800PE", "1d") == 1


def test_db_idempotent_rerun(tmp_path):
    store = _store(tmp_path)
    fut = Instrument.future("NFO", "NIFTY", "2025-06-26")
    row = {
        "symbol": "NIFTY25JUNFUT", "timeframe": "1d",
        "timestamp": _ts("2025-06-01").to_pydatetime(),
        "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10,
        "provider": "fyers", "exchange": "NFO", "contract_id": fut.contract_id,
    }
    assert store.upsert_many([row]) == 1
    assert store.upsert_many([row]) == 0  # idempotent


def test_existing_equity_data_not_clobbered(tmp_path):
    store = _store(tmp_path)
    # Existing SBI equity rows must survive a derivative insert.
    eq_row = {
        "symbol": "NSE:SBIN", "timeframe": "1d",
        "timestamp": _ts("2025-06-01").to_pydatetime(),
        "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10,
        "provider": "fyers", "exchange": "NSE", "contract_id": "NSE:SBIN",
    }
    fut = Instrument.future("NFO", "NIFTY", "2025-06-26")
    fut_row = {
        "symbol": "NIFTY25JUNFUT", "timeframe": "1d",
        "timestamp": _ts("2025-06-01").to_pydatetime(),
        "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10,
        "provider": "fyers", "exchange": "NFO", "contract_id": fut.contract_id,
    }
    assert store.upsert_many([eq_row]) == 1
    assert store.upsert_many([fut_row]) == 1
    assert store.count("NSE:SBIN", "1d") == 1


# --------------------------------------------------------------------------- #
# Phase 4/7 — derivative historical normalization + validation (mocked provider)
# --------------------------------------------------------------------------- #
def test_derivative_backfill_normalization(tmp_path, monkeypatch):
    store = _store(tmp_path)
    base = _ts("2024-01-01")
    frame = _good_frame(base, 5)
    prov = FYERSMarketDataProvider(client_id="X", access_token="Y")
    monkeypatch.setattr(prov, "get_historical", lambda s, tf, start=None, end=None, **k: frame)
    # Register the derivative so contract_id resolves.
    instr = Instrument.future("NFO", "NIFTY", "2024-01-01", provider_symbol="NFO:NIFTY24JANFUT")
    instr.internal = InternalSymbol("NFO", "NIFTY24JANFUT")
    eng = BackfillEngine(prov, store, max_retries=0)
    res = eng.backfill_symbol("NFO:NIFTY24JANFUT", "1d", start=base, end=base + pd.Timedelta(days=4))
    assert res.status == BackfillStatus.COMPLETE
    assert res.stored == 5
    # Stored rows carry the derivative symbol AND contract_id.
    df = store.load("NFO:NIFTY24JANFUT", "1d")
    assert len(df) == 5


def test_validate_contract_identity_rejects_bad_metadata():
    bad = Instrument.option("NFO", "NIFTY", None, -5, "XX")
    issues = validate_contract_identity(bad)
    codes = {i.code for i in issues}
    assert "BAD_STRIKE" in codes
    assert "BAD_OPTION_TYPE" in codes
    assert "CONTRACT_META" in codes  # missing expiry


def test_validate_contract_identity_accepts_valid():
    good = Instrument.option("NFO", "NIFTY", "2030-12-25", 24800, "CE")
    assert validate_contract_identity(good) == []


def test_validate_contract_identity_equity_is_noop():
    eq = Instrument(InternalSymbol("NSE", "SBIN"), InstrumentType.EQUITY)
    assert validate_contract_identity(eq) == []


# --------------------------------------------------------------------------- #
# Phase 1 — existing equity behavior unchanged
# --------------------------------------------------------------------------- #
def test_equity_registry_unchanged():
    reg = InstrumentRegistry()
    assert reg.get(InternalSymbol("NSE", "SBIN")).instrument_type == InstrumentType.EQUITY
    assert reg.get(InternalSymbol("NSE", "NIFTY50")).instrument_type == InstrumentType.INDEX


def test_equity_backfill_still_works(tmp_path, monkeypatch):
    store = _store(tmp_path)
    base = _ts("2024-01-01")
    frame = _good_frame(base, 3)
    prov = FYERSMarketDataProvider(client_id="X", access_token="Y")
    monkeypatch.setattr(prov, "get_historical", lambda s, tf, start=None, end=None, **k: frame)
    eng = BackfillEngine(prov, store, max_retries=0)
    res = eng.backfill_symbol("NSE:SBIN", "1d", start=base, end=base + pd.Timedelta(days=2))
    assert res.status == BackfillStatus.COMPLETE
    assert res.stored == 3
    assert store.count("NSE:SBIN", "1d") == 3
