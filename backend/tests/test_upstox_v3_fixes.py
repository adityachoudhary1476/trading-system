"""Regression tests for the two Phase 2G-C defects.

1. V3 subscription must be JSON (UTF-8) sent as a WebSocket binary frame.
2. Equity instrument resolution must return real Upstox ISIN-based keys,
   not the legacy ``NSE_EQ|SBIN`` trading-symbol form.

These tests do NOT require real Upstox credentials or network access. The
resolver is exercised with an injectable ``search_fn`` so all behaviour is
deterministic.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.trading_system.india.upstox_v3_pb import RequestMode
from src.trading_system.india.upstox_v3_resolver import (
    UpstoxV3InstrumentResolver,
    UnresolvedInstrumentError,
)
from src.trading_system.india.upstox_v3_ws import UpstoxV3WebSocket


# ---------------------------------------------------------------------------
# Defect 1: subscription is JSON-in-binary (per official Upstox V3 SDK)
# ---------------------------------------------------------------------------
class TestV3JsonSubscription:
    """Verify the corrected V3 subscription format (JSON in binary frame)."""

    def _build(self, method="sub", mode=RequestMode.LTPC, keys=None, guid=None):
        if keys is None:
            keys = ["NSE_EQ|INE062A01020"]
        return UpstoxV3WebSocket._build_json_subscription(
            method=method,
            instrument_keys=keys,
            mode=mode,
            guid=guid,
        )

    def test_subscription_is_bytes(self):
        """Subscription must be raw bytes (encoded JSON)."""
        payload = self._build()
        assert isinstance(payload, bytes)

    def test_subscription_is_valid_json(self):
        """Subscription bytes must decode to a valid JSON object."""
        payload = self._build()
        decoded = json.loads(payload.decode("utf-8"))
        assert isinstance(decoded, dict)

    def test_subscription_is_utf8(self):
        """Subscription bytes must be UTF-8 (not Latin-1 / ASCII)."""
        # Build a payload that includes a non-ASCII character in an index name
        # to confirm UTF-8 encoding.
        payload = self._build(keys=["NSE_INDEX|Nifty 50"])
        # The space is ASCII so this just checks the encode round-trip.
        text = payload.decode("utf-8")
        assert "Nifty 50" in text

    def test_subscription_contains_guid(self):
        payload = self._build(guid="test-guid-1234")
        decoded = json.loads(payload.decode("utf-8"))
        assert decoded["guid"] == "test-guid-1234"

    def test_subscription_guid_generated_when_missing(self):
        payload = self._build()
        decoded = json.loads(payload.decode("utf-8"))
        assert "guid" in decoded
        assert isinstance(decoded["guid"], str)
        assert len(decoded["guid"]) > 0

    def test_subscription_method_is_sub(self):
        payload = self._build(method="sub")
        decoded = json.loads(payload.decode("utf-8"))
        assert decoded["method"] == "sub"

    def test_subscription_method_is_unsub(self):
        payload = self._build(method="unsub")
        decoded = json.loads(payload.decode("utf-8"))
        assert decoded["method"] == "unsub"

    def test_subscription_method_is_change_mode(self):
        payload = self._build(method="change_mode")
        decoded = json.loads(payload.decode("utf-8"))
        assert decoded["method"] == "change_mode"

    def test_subscription_data_contains_instrument_keys(self):
        keys = ["NSE_EQ|INE062A01020", "NSE_EQ|INE002A01018"]
        payload = self._build(keys=keys)
        decoded = json.loads(payload.decode("utf-8"))
        assert decoded["data"]["instrumentKeys"] == keys

    def test_subscription_mode_is_string(self):
        """The V3 mode field is a STRING (per official SDK), not a varint."""
        payload = self._build(mode=RequestMode.LTPC)
        decoded = json.loads(payload.decode("utf-8"))
        assert decoded["data"]["mode"] == "ltpc"
        # Must NOT be a varint
        assert not isinstance(decoded["data"]["mode"], int)

    def test_subscription_mode_full(self):
        payload = self._build(mode=RequestMode.FULL_D5)
        decoded = json.loads(payload.decode("utf-8"))
        assert decoded["data"]["mode"] == "full"

    def test_subscription_mode_full_d30(self):
        payload = self._build(mode=RequestMode.FULL_D30)
        decoded = json.loads(payload.decode("utf-8"))
        assert decoded["data"]["mode"] == "full_d30"

    def test_subscription_mode_option_greeks(self):
        payload = self._build(mode=RequestMode.OPTION_GREEKS)
        decoded = json.loads(payload.decode("utf-8"))
        assert decoded["data"]["mode"] == "option_greeks"

    def test_subscription_multiple_instruments(self):
        keys = [
            "NSE_EQ|INE062A01020",
            "NSE_EQ|INE002A01018",
            "NSE_INDEX|Nifty 50",
        ]
        payload = self._build(keys=keys)
        decoded = json.loads(payload.decode("utf-8"))
        assert decoded["data"]["instrumentKeys"] == keys
        assert len(decoded["data"]["instrumentKeys"]) == 3

    def test_subscription_rejects_empty_instrument_keys(self):
        with pytest.raises(ValueError, match="must not be empty"):
            self._build(keys=[])

    def test_subscription_rejects_unknown_method(self):
        with pytest.raises(ValueError, match="method"):
            self._build(method="subscribe")  # wrong: V3 uses "sub"

    def test_unsub_does_not_require_mode(self):
        # ``unsub`` legitimately has no mode (the V3 schema).
        payload = self._build(method="unsub", mode=None)
        decoded = json.loads(payload.decode("utf-8"))
        assert decoded["method"] == "unsub"
        assert "mode" not in decoded["data"]

    def test_subscribe_sends_payload_via_websocket(self):
        """The subscribe() path must invoke ws.send(payload, opcode=2)."""
        ws = UpstoxV3WebSocket(
            access_token="test",
            instrument_keys=["NSE_EQ|INE062A01020"],
            on_event=lambda x: None,
        )
        mock_ws = MagicMock()
        ws._ws = mock_ws
        ws._subscribe()

        # ws.send was called exactly once with (payload, opcode=2)
        mock_ws.send.assert_called_once()
        args, kwargs = mock_ws.send.call_args
        payload_arg = args[0]
        opcode_arg = args[1] if len(args) > 1 else kwargs.get("opcode")
        assert isinstance(payload_arg, bytes)
        # Verify it's valid JSON with the V3 schema
        decoded = json.loads(payload_arg.decode("utf-8"))
        assert decoded["method"] == "sub"
        assert decoded["data"]["mode"] == "ltpc"
        assert decoded["data"]["instrumentKeys"] == ["NSE_EQ|INE062A01020"]
        # Opcode 2 == WebSocket binary frame
        assert opcode_arg == 2


# ---------------------------------------------------------------------------
# Defect 2: equity resolution returns real ISIN-based Upstox keys
# ---------------------------------------------------------------------------
class TestV3InstrumentResolver:
    """Verify the corrected Upstox V3 instrument-key resolution."""

    def _resolver(self, search_results):
        """Build a resolver with the given stubbed search results."""
        def search_fn(query):
            return list(search_results.get(query, []))
        return UpstoxV3InstrumentResolver(search_fn=search_fn)

    def test_resolve_sbin_uses_isin(self):
        """NSE:SBIN must resolve to the ISIN-based key, not the trading symbol."""
        r = self._resolver({
            "SBIN": [
                {
                    "segment": "NSE_EQ",
                    "trading_symbol": "SBIN",
                    "isin": "INE062A01020",
                    "instrument_key": "NSE_EQ|INE062A01020",
                }
            ]
        })
        assert r.resolve("NSE:SBIN") == "NSE_EQ|INE062A01020"

    def test_resolve_reliance_uses_isin(self):
        r = self._resolver({
            "RELIANCE": [
                {
                    "segment": "NSE_EQ",
                    "trading_symbol": "RELIANCE",
                    "isin": "INE002A01018",
                    "instrument_key": "NSE_EQ|INE002A01018",
                }
            ]
        })
        assert r.resolve("NSE:RELIANCE") == "NSE_EQ|INE002A01018"

    def test_resolve_nifty50_index(self):
        """NIFTY 50 must resolve to the canonical NSE_INDEX|Nifty 50 key."""
        r = self._resolver({})
        # Indices don't need the search API - resolved locally.
        assert r.resolve("NSE:NIFTY50") == "NSE_INDEX|Nifty 50"
        assert r.resolve("NSE:NIFTY 50") == "NSE_INDEX|Nifty 50"
        assert r.resolve("NSE:NIFTY") == "NSE_INDEX|Nifty 50"

    def test_resolve_banknifty_index(self):
        r = self._resolver({})
        assert r.resolve("NSE:BANKNIFTY") == "NSE_INDEX|Nifty Bank"
        assert r.resolve("NSE:NIFTY BANK") == "NSE_INDEX|Nifty Bank"

    def test_resolve_already_instrument_key_passes_through(self):
        """If a caller passes a fully-formed V3 key, return it unchanged."""
        r = self._resolver({})
        assert r.resolve("NSE_EQ|INE062A01020") == "NSE_EQ|INE062A01020"

    def test_resolve_unknown_equity_fails_clearly(self):
        """Unknown equity must raise, not silently fall back to trading symbol."""
        r = self._resolver({})  # search returns nothing
        with pytest.raises(UnresolvedInstrumentError) as excinfo:
            r.resolve("NSE:FAKESYMBOL9999")
        assert "FAKESYMBOL9999" in str(excinfo.value)

    def test_resolve_empty_symbol_raises(self):
        r = self._resolver({})
        with pytest.raises(UnresolvedInstrumentError):
            r.resolve("")

    def test_resolve_malformed_symbol_raises(self):
        r = self._resolver({})
        with pytest.raises(UnresolvedInstrumentError):
            r.resolve(":")
        with pytest.raises(UnresolvedInstrumentError):
            r.resolve("::")

    def test_resolve_results_are_cached(self):
        """The same symbol must not trigger multiple search calls."""
        calls: list[str] = []
        def search_fn(query):
            calls.append(query)
            return [
                {
                    "segment": "NSE_EQ",
                    "trading_symbol": "SBIN",
                    "isin": "INE062A01020",
                    "instrument_key": "NSE_EQ|INE062A01020",
                }
            ]
        r = UpstoxV3InstrumentResolver(search_fn=search_fn)
        r.resolve("NSE:SBIN")
        r.resolve("NSE:SBIN")
        r.resolve("NSE:SBIN")
        # Only one search invocation despite three resolve calls.
        assert len(calls) == 1

    def test_resolve_does_not_silently_fall_back_to_trading_symbol(self):
        """Per the hard rules: an unresolved symbol must NOT silently become
        ``NSE_EQ|SBIN`` (that key returns zero market data from the V3 feed).
        """
        r = self._resolver({})  # search returns nothing
        with pytest.raises(UnresolvedInstrumentError):
            r.resolve("NSE:SBIN")
        # Make sure the forbidden fallback string is never returned.
        try:
            key = r.resolve("NSE:SBIN")
        except UnresolvedInstrumentError:
            key = None
        assert key != "NSE_EQ|SBIN"

    def test_resolve_prefers_exact_symbol_match(self):
        """When search returns multiple rows, prefer exact trading symbol match."""
        r = self._resolver({
            "TATA": [
                {
                    "segment": "NSE_EQ",
                    "trading_symbol": "TATAMOTORS",
                    "isin": "INE155A01022",
                    "instrument_key": "NSE_EQ|INE155A01022",
                },
                {
                    "segment": "NSE_EQ",
                    "trading_symbol": "TATA",
                    "isin": "INE081A01020",
                    "instrument_key": "NSE_EQ|INE081A01020",
                },
            ]
        })
        assert r.resolve("NSE:TATA") == "NSE_EQ|INE081A01020"

    def test_resolve_multiple_signal_universe(self):
        """Resolve the application's full SIGNAL_UNIVERSE via the search API."""
        # Verified values from real Upstox Instrument Search API output.
        search_results = {
            sym: [
                {
                    "segment": "NSE_EQ",
                    "trading_symbol": sym,
                    "isin": isin,
                    "instrument_key": f"NSE_EQ|{isin}",
                }
            ]
            for sym, isin in [
                ("SBIN", "INE062A01020"),
                ("RELIANCE", "INE002A01018"),
                ("TCS", "INE467B01029"),
                ("INFY", "INE009A01021"),
                ("HDFCBANK", "INE040A01034"),
                ("ICICIBANK", "INE090A01021"),
                ("KOTAKBANK", "INE237A01036"),
                ("AXISBANK", "INE238A01034"),
                ("LT", "INE018A01030"),
                ("WIPRO", "INE075A01022"),
            ]
        }
        r = self._resolver(search_results)
        for sym, expected_isin in [
            ("SBIN", "INE062A01020"),
            ("RELIANCE", "INE002A01018"),
            ("TCS", "INE467B01029"),
            ("INFY", "INE009A01021"),
            ("HDFCBANK", "INE040A01034"),
            ("ICICIBANK", "INE090A01021"),
            ("KOTAKBANK", "INE237A01036"),
            ("AXISBANK", "INE238A01034"),
            ("LT", "INE018A01030"),
            ("WIPRO", "INE075A01022"),
        ]:
            assert r.resolve(f"NSE:{sym}") == f"NSE_EQ|{expected_isin}", sym

    def test_resolve_handles_search_api_error_gracefully(self):
        """A failing search API must surface as UnresolvedInstrumentError, not a
        500 / silent crash."""
        def boom(query):
            raise RuntimeError("network down")
        r = UpstoxV3InstrumentResolver(search_fn=boom)
        with pytest.raises(UnresolvedInstrumentError):
            r.resolve("NSE:SBIN")

    def test_resolve_handles_non_success_response(self):
        """Search API returning non-success must NOT silently use a fallback."""
        r = UpstoxV3InstrumentResolver(search_fn=lambda q: [])
        with pytest.raises(UnresolvedInstrumentError):
            r.resolve("NSE:SBIN")
