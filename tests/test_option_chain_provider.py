"""Phase 1 — Tests for option-chain provider (no network, no real token).

All tests use mock responses that mirror the real Upstox v2 response shape.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from trading_system.india.option_chain_provider import (
    UpstoxOptionChainProvider,
    NormalizedOptionRow,
    OptionChainSnapshot,
    _extract,
    _to_float,
    _to_int,
    _normalize_expiry,
    _infer_strike_interval,
)
from trading_system.india.instruments import OptionType


# --------------------------------------------------------------------------- #
# Mock helpers
# --------------------------------------------------------------------------- #
SAMPLE_V2_CHAIN = {
    "status": "success",
    "data": {
        "spotPrice": 18500.5,
        "optionChain": [
            {
                "strikePrice": 18400.0,
                "expiry": "2025-01-30",
                "ce": {
                    "instrumentKey": "NSE_OPTIDX_NIFTY_30JAN2025_18400.00_CE",
                    "ltp": 145.3,
                    "bidPrice": 145.0,
                    "askPrice": 145.6,
                    "volume": 1000,
                    "oi": 5000,
                    "changeOI": 100,
                    "bidIv": 15.2,
                    "askIv": 16.1,
                },
                "pe": {
                    "instrumentKey": "NSE_OPTIDX_NIFTY_30JAN2025_18400.00_PE",
                    "ltp": 55.7,
                    "bidPrice": 55.5,
                    "askPrice": 56.0,
                    "volume": 800,
                    "oi": 3000,
                    "changeOI": 50,
                    "bidIv": 14.8,
                    "askIv": 15.5,
                },
            },
            {
                "strikePrice": 18500.0,
                "expiry": "2025-01-30",
                "ce": {
                    "instrumentKey": "NSE_OPTIDX_NIFTY_30JAN2025_18500.00_CE",
                    "ltp": 80.2,
                    "bidPrice": 80.0,
                    "askPrice": 80.5,
                    "volume": 2000,
                    "oi": 8000,
                    "changeOI": 200,
                    "bidIv": 14.5,
                    "askIv": 15.3,
                },
                "pe": {
                    "instrumentKey": "NSE_OPTIDX_NIFTY_30JAN2025_18500.00_PE",
                    "ltp": 95.1,
                    "bidPrice": 95.0,
                    "askPrice": 95.5,
                    "volume": 1500,
                    "oi": 6000,
                    "changeOI": 150,
                    "bidIv": 15.0,
                    "askIv": 15.8,
                },
            },
        ],
    },
}


def make_provider(raw_response=None, fetch_side_effect=None):
    """Build a provider with a fully-mocked UpstoxInstrumentDiscovery."""
    discovery = MagicMock()
    discovery._provider._get = MagicMock(return_value=raw_response)
    if fetch_side_effect is not None:
        discovery._provider._get = MagicMock(side_effect=fetch_side_effect)
    discovery._parse_symbol = MagicMock(return_value=("nse", "NIFTY", "index"))
    discovery.index_symbol = MagicMock(return_value="NSE_INDEX|NIFTY")
    return UpstoxOptionChainProvider(discovery=discovery)


def make_mock_session():
    """Return a mock session that works as a context manager."""
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session_factory = MagicMock(return_value=session)
    return session, session_factory


# --------------------------------------------------------------------------- #
# Provider tests
# --------------------------------------------------------------------------- #
class TestGetChainBasic:
    def test_returns_valid_chain(self):
        provider = make_provider(raw_response=SAMPLE_V2_CHAIN)
        chain = provider.get_chain("NIFTY", "2025-01-30")
        assert chain is not None
        assert chain.underlying == "NIFTY"
        assert chain.expiry == "2025-01-30"
        assert chain.spot_price == 18500.5
        assert 18400.0 in chain.call_quotes
        assert 18400.0 in chain.put_quotes
        assert len(chain.all_quotes) == 4  # 2 strikes x 2 types

    def test_preserves_null_fields(self):
        """Fields unavailable from provider are None, not fabricated."""
        response = {
            "status": "success",
            "data": {
                "spotPrice": 18500.0,
                "optionChain": [
                    {
                        "strikePrice": 18500.0,
                        "expiry": "2025-01-30",
                        "ce": {
                            "instrumentKey": "KEY_CE",
                            "ltp": 80.0,
                            "bidPrice": 79.5,
                            "askPrice": 80.5,
                            # no volume, oi, changeOI, bidIv, askIv
                        },
                        "pe": {
                            "instrumentKey": "KEY_PE",
                            "ltp": 95.0,
                            "bidPrice": 94.5,
                            "askPrice": 95.5,
                        },
                    },
                ],
            },
        }
        provider = make_provider(raw_response=response)
        chain = provider.get_chain("NIFTY", "2025-01-30")
        assert chain is not None
        ce = chain.call_quotes[18500.0]
        assert ce.ltp == 80.0
        assert ce.bid_iv is None
        assert ce.ask_iv is None
        assert ce.change_oi is None
        assert ce.volume == 0  # defaults to 0
        assert ce.open_interest == 0

    def test_strike_interval_inferred(self):
        provider = make_provider(raw_response=SAMPLE_V2_CHAIN)
        chain = provider.get_chain("NIFTY", "2025-01-30")
        assert chain.strike_interval == 100.0  # 18400, 18500


class TestExpiryFiltering:
    def test_filters_by_expiry(self):
        response = {
            "status": "success",
            "data": {
                "spotPrice": 18500.0,
                "optionChain": [
                    {"strikePrice": 18400.0, "expiry": "2025-01-30",
                     "ce": {"instrumentKey": "CE1", "ltp": 1.0, "bidPrice": 0.9, "askPrice": 1.1}},
                    {"strikePrice": 18400.0, "expiry": "2025-02-27",
                     "ce": {"instrumentKey": "CE2", "ltp": 2.0, "bidPrice": 1.9, "askPrice": 2.1}},
                ],
            },
        }
        provider = make_provider(raw_response=response)
        chain = provider.get_chain("NIFTY", "2025-01-30")
        assert chain is not None
        assert len(chain.all_quotes) == 1
        assert chain.all_quotes[0].instrument_key == "CE1"

    def test_empty_expiry_uses_first(self):
        response = {
            "status": "success",
            "data": {
                "spotPrice": 18500.0,
                "optionChain": [
                    {"strikePrice": 18400.0, "expiry": "2025-01-30",
                     "ce": {"instrumentKey": "CE1", "ltp": 1.0, "bidPrice": 0.9, "askPrice": 1.1}},
                    {"strikePrice": 18400.0, "expiry": "2025-02-27",
                     "ce": {"instrumentKey": "CE2", "ltp": 2.0, "bidPrice": 1.9, "askPrice": 2.1}},
                ],
            },
        }
        provider = make_provider(raw_response=response)
        chain = provider.get_chain("NIFTY", "")
        assert chain is not None
        assert chain.expiry == "2025-01-30"


class TestFailClosed:
    def test_auth_failure_returns_none(self):
        provider = make_provider(
            raw_response={"status": "Unauthorized", "errors": ["token expired"]}
        )
        assert provider.get_chain("NIFTY", "2025-01-30") is None

    def test_malformed_response_returns_none(self):
        provider = make_provider(raw_response={"status": "success", "data": {}})
        assert provider.get_chain("NIFTY", "2025-01-30") is None

    def test_empty_response_returns_none(self):
        provider = make_provider(raw_response=None)
        assert provider.get_chain("NIFTY", "2025-01-30") is None

    def test_no_option_chain_returns_none(self):
        provider = make_provider(raw_response={"status": "success", "data": {"optionChain": []}})
        assert provider.get_chain("NIFTY", "2025-01-30") is None

    def test_exception_in_fetch_returns_none(self):
        provider = make_provider(fetch_side_effect=ConnectionError("network"))
        assert provider.get_chain("NIFTY", "2025-01-30") is None


class TestValidation:
    def test_negative_oi_degraded_not_invalid(self):
        """Negative OI -> error recorded, chain still returned (degraded)."""
        response = {
            "status": "success",
            "data": {
                "spotPrice": 18500.0,
                "optionChain": [
                    {"strikePrice": 18400.0, "expiry": "2025-01-30",
                     "ce": {"instrumentKey": "CE1", "ltp": 1.0, "bidPrice": 0.9,
                            "askPrice": 1.1, "oi": -100}},
                ],
            },
        }
        provider = make_provider(raw_response=response)
        chain = provider.get_chain("NIFTY", "2025-01-30")
        assert chain is not None  # degraded, not invalid

    def test_negative_volume_degraded(self):
        response = {
            "status": "success",
            "data": {
                "spotPrice": 18500.0,
                "optionChain": [
                    {"strikePrice": 18400.0, "expiry": "2025-01-30",
                     "ce": {"instrumentKey": "CE1", "ltp": 1.0, "bidPrice": 0.9,
                            "askPrice": 1.1, "volume": -50}},
                ],
            },
        }
        provider = make_provider(raw_response=response)
        chain = provider.get_chain("NIFTY", "2025-01-30")
        assert chain is not None  # degraded

    def test_bid_gt_ask_recorded(self):
        response = {
            "status": "success",
            "data": {
                "spotPrice": 18500.0,
                "optionChain": [
                    {"strikePrice": 18400.0, "expiry": "2025-01-30",
                     "ce": {"instrumentKey": "CE1", "ltp": 1.0, "bidPrice": 1.5,
                            "askPrice": 1.1}},
                ],
            },
        }
        provider = make_provider(raw_response=response)
        chain = provider.get_chain("NIFTY", "2025-01-30")
        assert chain is not None  # degraded

    def test_missing_instrument_key_skips_row(self):
        response = {
            "status": "success",
            "data": {
                "spotPrice": 18500.0,
                "optionChain": [
                    {"strikePrice": 18400.0, "expiry": "2025-01-30",
                     "ce": {"ltp": 1.0, "bidPrice": 0.9, "askPrice": 1.1}},  # no instrumentKey
                    {"strikePrice": 18500.0, "expiry": "2025-01-30",
                     "ce": {"instrumentKey": "CE2", "ltp": 2.0, "bidPrice": 1.9, "askPrice": 2.1}},
                ],
            },
        }
        provider = make_provider(raw_response=response)
        chain = provider.get_chain("NIFTY", "2025-01-30")
        assert chain is not None
        assert 18500.0 in chain.call_quotes  # only the valid row
        assert 18400.0 not in chain.call_quotes  # missing instrument_key -> skipped


class TestV3Response:
    def test_v3_format_parsed(self):
        """V3 API uses strike_price, callOption/putOption, openInterest."""
        response = {
            "status": "OK",
            "data": {
                "spotPrice": 18500.0,
                "optionChain": [
                    {
                        "strike_price": 18400.0,
                        "expiry": "2025-01-30",
                        "callOption": {
                            "instrumentKey": "CE1",
                            "ltp": 145.3,
                            "bidPrice": 145.0,
                            "askPrice": 145.6,
                            "volume": 1000,
                            "openInterest": 5000,
                            "changeOI": 100,
                            "bidIv": 15.2,
                            "askIv": 16.1,
                        },
                        "putOption": {
                            "instrumentKey": "PE1",
                            "ltp": 55.7,
                            "bidPrice": 55.5,
                            "askPrice": 56.0,
                            "volume": 800,
                            "openInterest": 3000,
                            "changeOI": 50,
                            "bidIv": 14.8,
                            "askIv": 15.5,
                        },
                    },
                ],
            },
        }
        provider = make_provider(raw_response=response)
        chain = provider.get_chain("NIFTY", "2025-01-30")
        assert chain is not None
        assert 18400.0 in chain.call_quotes
        assert 18400.0 in chain.put_quotes
        assert chain.call_quotes[18400.0].open_interest == 5000  # openInterest -> oi
        assert chain.put_quotes[18400.0].open_interest == 3000


class TestPersistence:
    def test_snapshot_persisted(self):
        """When session_factory provided, raw_json + rows are persisted."""
        session, sf = make_mock_session()
        discovery = MagicMock()
        discovery._provider._get = MagicMock(return_value=SAMPLE_V2_CHAIN)
        discovery._parse_symbol = MagicMock(return_value=("nse", "NIFTY", "index"))
        discovery.index_symbol = MagicMock(return_value="NSE_INDEX|NIFTY")

        provider = UpstoxOptionChainProvider(discovery=discovery, session_factory=sf)
        chain = provider.get_chain("NIFTY", "2025-01-30")

        assert chain is not None
        assert sf.called  # session_factory was called
        assert session.add.call_count >= 2  # snapshot + at least one row
        session.commit.assert_called_once()

    def test_raw_json_preserved(self):
        """The raw provider response is preserved in the snapshot."""
        session, sf = make_mock_session()
        discovery = MagicMock()
        discovery._provider._get = MagicMock(return_value=SAMPLE_V2_CHAIN)
        discovery._parse_symbol = MagicMock(return_value=("nse", "NIFTY", "index"))
        discovery.index_symbol = MagicMock(return_value="NSE_INDEX|NIFTY")

        provider = UpstoxOptionChainProvider(discovery=discovery, session_factory=sf)
        provider.get_chain("NIFTY", "2025-01-30")

        added = [call.args[0] for call in session.add.call_args_list]
        from trading_system.paper.option_chain_models import OptionChainSnapshotRecord
        snapshot_records = [a for a in added if isinstance(a, OptionChainSnapshotRecord)]
        assert len(snapshot_records) == 1
        parsed = json.loads(snapshot_records[0].raw_json)
        assert parsed["status"] == "success"


# --------------------------------------------------------------------------- #
# Helper function tests
# --------------------------------------------------------------------------- #
class TestHelpers:
    def test_extract(self):
        d = {"a": 1, "b": None, "c": 3}
        assert _extract(d, "x", "a") == 1
        assert _extract(d, "y", "z") is None
        assert _extract(d, "b", "c") == 3  # skips None

    def test_to_float(self):
        assert _to_float(123) == 123.0
        assert _to_float("123.45") == 123.45
        assert _to_float(None) is None
        assert _to_float("abc") is None

    def test_to_int(self):
        assert _to_int(123) == 123
        assert _to_int("123") == 123
        assert _to_int(None) is None
        assert _to_int("abc") is None

    def test_normalize_expiry(self):
        assert _normalize_expiry("2025-01-30") == "2025-01-30"
        assert _normalize_expiry("30-Jan-2025") == "2025-01-30"
        assert _normalize_expiry("") == ""

    def test_infer_strike_interval(self):
        assert _infer_strike_interval([18400, 18500, 18600]) == 100.0
        assert _infer_strike_interval([18400]) == 0.0
        assert _infer_strike_interval([]) == 0.0
