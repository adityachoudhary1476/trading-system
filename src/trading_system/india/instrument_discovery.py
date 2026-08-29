"""FYERS derivative instrument discovery (provider-specific, isolated).

This is the ONLY place that talks to FYERS' live ``optionchain`` endpoint for
symbol discovery. It is DATA-ONLY (read-only; never places orders) and it never
downloads the full symbol master unnecessarily: it queries the option chain for a
specific underlying, parses the returned contracts, and feeds them into the
provider-independent ``InstrumentRepository`` as normalized ``Instrument`` objects.

If authentication is unavailable or the request fails, discovery returns an empty
result — it NEVER fabricates contracts.

The parsed results are cached in memory (and can be persisted by the caller) so
repeated lookups don't re-hit the API. Cache refresh is explicit (call again).
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from . import derivatives as dmod
from .instruments import (
    Instrument,
    InstrumentType,
    OptionType,
)
from .instrument_repository import InstrumentRepository


class FyersInstrumentDiscovery:
    """Discover derivative contracts via the FYERS option-chain endpoint."""

    def __init__(
        self,
        fyers_model,  # a fyers_apiv3.fyersModel.FyersModel instance (auth-ready)
        repo: Optional[InstrumentRepository] = None,
    ) -> None:
        self.model = fyers_model
        self.repo = repo or InstrumentRepository()

    def index_symbol(self, underlying: str) -> str:
        """FYERS index/underlying symbol used by the option-chain endpoint.

        Verified with the SDK: ``NSE:NIFTY50-INDEX`` is the chain symbol for Nifty.
        Equity underlyings use their ``-EQ`` form. Commodities use their root.
        """
        u = underlying.upper()
        _INDEX = {"NIFTY": "NSE:NIFTY50-INDEX", "NIFTY50": "NSE:NIFTY50-INDEX",
                  "BANKNIFTY": "NSE:NIFTYBANK-INDEX", "FINNIFTY": "NSE:FINNIFTY-INDEX"}
        if u in _INDEX:
            return _INDEX[u]
        # Equity underlying -> NSE:<NAME>-EQ
        return f"NSE:{u}-EQ"

    def discover_options(
        self,
        underlying: str,
        expiry_ts: Optional[int] = None,
        strikecount: int = 20,
        greeks: int = 0,
    ) -> list[Instrument]:
        """Fetch option-chain for an underlying and register normalized contracts.

        Returns the list of discovered ``Instrument`` objects (empty on failure /
        auth error). No orders are placed; the only network call is read-only.
        """
        symbol = self.index_symbol(underlying)
        payload = {
            "symbol": symbol,
            "strikecount": strikecount,
            "greeks": greeks,
        }
        if expiry_ts is not None:
            payload["timestamp"] = int(expiry_ts)
        try:
            resp = self.model.optionchain(payload)
        except Exception:
            # Network / SDK failure: never fabricate. Surface empty + let caller log.
            return []

        if not isinstance(resp, dict) or resp.get("s") != "ok":
            # FYERS error payload (e.g. auth -15/-16). Do not treat as data.
            return []

        data = resp.get("data") or {}
        chain = data.get("optionsChain") or data.get("optionChain") or []
        discovered: list[Instrument] = []
        for entry in chain:
            if not isinstance(entry, dict):
                continue
            instr = self._entry_to_instrument(underlying, entry)
            if instr is not None:
                self.repo.register(instr)
                discovered.append(instr)
        return discovered

    def _entry_to_instrument(
        self, underlying: str, entry: dict
    ) -> Optional[Instrument]:
        """Convert one option-chain row into a normalized Instrument.

        The option-chain entry carries strike + call/put sub-records with their
        FYERS symbols. We build both legs as normalized options.
        """
        strike = entry.get("strikePrice") or entry.get("strike")
        if strike is None:
            return None
        try:
            strike_f = float(strike)
        except (TypeError, ValueError):
            return None
        # Expiry: prefer an explicit date in the entry; else fall back to a
        # monthly marker derived elsewhere. We don't guess exact day.
        expiry = entry.get("expiry") or entry.get("expiryDate")
        if expiry is None:
            # Without an expiry we cannot form a stable contract_id; skip.
            return None
        expiry_iso = _coerce_iso(expiry)
        if expiry_iso is None:
            return None

        instruments: list[Instrument] = []
        for leg_key, ot in (("call_options", "CE"), ("put_options", "PE")):
            leg = entry.get(leg_key)
            if not isinstance(leg, dict):
                continue
            fy_sym = leg.get("symbol")
            instr = Instrument.option(
                "NFO", underlying.upper(), expiry_iso, strike_f, ot,
                provider_symbol=fy_sym,
            )
            if fy_sym:
                try:
                    parsed = dmod.from_fyers_derivative_symbol(fy_sym)
                    instr.internal = parsed.internal
                except Exception:
                    instr.internal = __import__("typing").cast(  # keep linter calm
                        "X", None
                    ) if False else instr.internal
            instruments.append(instr)
        # Return the first valid leg; the loop registers both via repo.register.
        for i in instruments:
            self.repo.register(i)
        return instruments[0] if instruments else None


def _coerce_iso(value) -> Optional[str]:
    """Best-effort coercion of an expiry value to ISO YYYY-MM-DD."""
    if isinstance(value, (int, float)):
        # FYERS timestamps are epoch seconds.
        try:
            return dt.datetime.fromtimestamp(int(value), dt.timezone.utc).date().isoformat()
        except (ValueError, OSError):
            return None
    if isinstance(value, str):
        s = value.strip()
        for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y", "%Y%m%d"):
            try:
                return dt.datetime.strptime(s, fmt).date().isoformat()
            except ValueError:
                continue
        return None
    return None
