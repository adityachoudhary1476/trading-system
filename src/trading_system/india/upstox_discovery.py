"""Upstox v2 option-chain instrument discovery (provider-specific, isolated).

This is the ONLY place that queries the Upstox ``/option-chain`` endpoint for
current derivative contract discovery. It is DATA-ONLY (read-only; never places
orders) and never fabricates contracts — if auth is unavailable or the request
fails, discovery returns an empty list.

This mirrors :class:`trading_system.india.instrument_discovery.FyersInstrumentDiscovery`
but for the Upstox REST API v2 instead of FYERS' SDK ``optionchain`` call.
"""
from __future__ import annotations

from typing import List, Optional

from .instruments import (
    Instrument,
    InstrumentType,
    OptionType,
)
from .instrument_repository import InstrumentRepository
from .upstox import UpstoxMarketDataProvider


class UpstoxInstrumentDiscovery:
    """Discover derivative contracts via the Upstox v2 option-chain endpoint."""

    def __init__(
        self,
        provider: UpstoxMarketDataProvider,
        repo: Optional[InstrumentRepository] = None,
    ) -> None:
        self._provider = provider
        self.repo = repo or InstrumentRepository()

    def index_symbol(self, underlying: str) -> str:
        """Upstox instrument key for the option-chain endpoint.

        Index underlyings use the ``NSE_INDEX`` segment; equities use ``NSE_EQ``.
        The option-chain endpoint is called with this key as the path segment.
        """
        u = underlying.upper()
        _INDICES = {"NIFTY", "NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "MIDCAP"}
        if u in _INDICES:
            return f"NSE_INDEX|{u}"
        return f"NSE_EQ|{u}"

    def discover_options(
        self,
        underlying: str,
        strikecount: int = 20,
    ) -> List[Instrument]:
        """Fetch the option chain for an underlying and register normalized contracts.

        Returns the list of discovered ``Instrument`` objects (empty on failure /
        auth error). No orders are placed; the only network call is read-only.
        """
        if not self._provider.is_authenticated:
            return []

        key = self.index_symbol(underlying)
        path = f"/option-chain/{key}"

        try:
            resp = self._provider._get(path, params={"strikecount": strikecount})
        except Exception:
            return []

        if not isinstance(resp, dict):
            return []

        data = resp.get("data") or resp
        if not isinstance(data, dict):
            return []

        chain = (
            data.get("optionChain")
            or data.get("option_chain")
            or data.get("records")
        )
        if not isinstance(chain, list):
            # Some Upstox responses are dicts keyed by strike
            if isinstance(chain, dict):
                chain = list(chain.values())
            else:
                return []

        discovered: list[Instrument] = []
        for entry in chain:
            if not isinstance(entry, dict):
                continue
            for leg_key, ot in (("ce", OptionType.CE), ("pe", OptionType.PE)):
                leg = entry.get(leg_key)
                if leg is None and entry.get("callOption") is not None:
                    leg = entry.get("callOption") if leg_key == "ce" else entry.get("putOption")
                if not isinstance(leg, dict):
                    continue
                instr = self._entry_to_instrument(underlying, entry, ot)
                if instr is not None:
                    self.repo.register(instr)
                    discovered.append(instr)
        return discovered

    def _entry_to_instrument(
        self, underlying: str, entry: dict, option_type: OptionType
    ) -> Optional[Instrument]:
        """Convert one option-chain row into a normalized Instrument."""
        strike_raw = entry.get("strikePrice") or entry.get("strike")
        if strike_raw is None:
            return None
        try:
            strike_f = float(strike_raw)
        except (TypeError, ValueError):
            return None

        expiry = entry.get("expiry") or entry.get("expiryDate") or entry.get("expiry_date")
        if expiry is None:
            # Try the call/put sub-records for an expiry
            sub = entry.get("ce") or entry.get("pe") or entry.get("callOption") or entry.get("putOption")
            if isinstance(sub, dict):
                expiry = sub.get("expiry") or sub.get("expiryDate")
        if expiry is None:
            return None

        expiry_iso = self._coerce_iso(expiry)
        if expiry_iso is None:
            return None

        ot = "CE" if option_type == OptionType.CE else "PE"
        # Derive the Upstox instrument key if present
        provider_key = None
        sub = entry.get("ce") if option_type == OptionType.CE else entry.get("pe")
        if not isinstance(sub, dict):
            sub = entry.get("callOption") if option_type == OptionType.CE else entry.get("putOption")
        if isinstance(sub, dict):
            provider_key = sub.get("instrument_key") or sub.get("instrumentKey")

        instr = Instrument.option(
            "NSE", underlying.upper(), expiry_iso, strike_f, ot,
            provider_symbol=provider_key,
        )
        if provider_key:
            instr.provider_symbol = provider_key
        return instr

    @staticmethod
    def _coerce_iso(value) -> Optional[str]:
        """Best-effort coercion of an expiry value to ISO YYYY-MM-DD."""
        if isinstance(value, (int, float)):
            import datetime as dt
            try:
                return dt.datetime.fromtimestamp(int(value), dt.timezone.utc).date().isoformat()
            except (ValueError, OSError):
                return None
        if isinstance(value, str):
            s = value.strip()
            for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y", "%Y%m%d", "%d-%M-%Y"):
                try:
                    import datetime as dt
                    return dt.datetime.strptime(s, fmt).date().isoformat()
                except ValueError:
                    continue
            return None
        return None
