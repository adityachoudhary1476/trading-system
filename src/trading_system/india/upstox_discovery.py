"""Upstox v2 option-chain instrument discovery (provider-specific, isolated).

This is the ONLY place that queries the Upstox ``/option-chain`` endpoint for
current derivative contract discovery. It is DATA-ONLY (read-only; never places
orders) and never fabricates contracts — if auth is unavailable or the request
fails, discovery returns an empty list.

This mirrors the provider-independent ``InstrumentRepository`` discovery pattern
but for the Upstox REST API v2 instead of a previous provider's SDK ``optionchain`` call.
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


# Canonical Upstox index instrument keys. Upstox identifies indices by a
# human-readable name (``NSE_INDEX|Nifty 50``), NOT by the trading symbol
# (``NSE_INDEX|NIFTY``). Using the trading symbol returns an empty payload /
# HTTP 404, so the mapping is explicit and verified against the live API.
_INDEX_INSTRUMENT_KEYS = {
    "NIFTY": "Nifty 50",
    "NIFTY50": "Nifty 50",
    "NIFTY 50": "Nifty 50",
    "BANKNIFTY": "Nifty Bank",
    "NIFTYBANK": "Nifty Bank",
    "NIFTY BANK": "Nifty Bank",
    "FINNIFTY": "Nifty Fin Service",
    "NIFTY FIN SERVICE": "Nifty Fin Service",
    "MIDCPNIFTY": "NIFTY MID SELECT",
    "MIDCAP": "NIFTY MID SELECT",
}


class UpstoxInstrumentDiscovery:
    """Discover derivative contracts via the Upstox v2 option-chain endpoint."""

    def __init__(
        self,
        provider: UpstoxMarketDataProvider,
        repo: Optional[InstrumentRepository] = None,
    ) -> None:
        self._provider = provider
        self.repo = repo or InstrumentRepository()
        # instrument_key -> lot_size, learned from /option/contract
        self._lot_sizes: dict[str, int] = {}

    def index_symbol(self, underlying: str) -> str:
        """Upstox instrument key for index/equity option-chain lookups.

        Index underlyings use the canonical ``NSE_INDEX|<Index Name>`` key;
        equities use ``NSE_EQ|<SYMBOL>``.
        """
        u = underlying.upper().strip()
        index_name = _INDEX_INSTRUMENT_KEYS.get(u)
        if index_name is not None:
            return f"NSE_INDEX|{index_name}"
        return f"NSE_EQ|{u}"

    # ------------------------------------------------------------------ #
    # Expiry + chain retrieval
    # ------------------------------------------------------------------ #
    def list_expiries(self, underlying: str) -> List[str]:
        """Expiry dates (ISO, ascending) currently listed for an underlying.

        Uses the read-only ``/option/contract`` endpoint. Returns an empty
        list on any failure — never fabricates an expiry.
        """
        if not self._provider.is_authenticated:
            return []
        key = self.index_symbol(underlying)
        try:
            resp = self._provider._get("/option/contract", params={"instrument_key": key})
        except Exception:
            return []
        if not isinstance(resp, dict):
            return []
        rows = resp.get("data")
        if not isinstance(rows, list):
            return []
        expiries: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            iso = self._coerce_iso(row.get("expiry"))
            if iso:
                expiries.add(iso)
            # learn lot sizes while we are here (chain rows omit them)
            ik = row.get("instrument_key")
            lot = row.get("lot_size")
            if isinstance(ik, str) and lot is not None:
                try:
                    self._lot_sizes[ik] = int(lot)
                except (TypeError, ValueError):
                    pass
        return sorted(expiries)

    def fetch_chain_rows(self, underlying: str, expiry: str) -> List[dict]:
        """Raw ``/option/chain`` rows for one expiry (empty on failure)."""
        if not self._provider.is_authenticated:
            return []
        key = self.index_symbol(underlying)
        try:
            resp = self._provider._get(
                "/option/chain",
                params={"instrument_key": key, "expiry_date": expiry},
            )
        except Exception:
            return []
        if not isinstance(resp, dict):
            return []
        data = resp.get("data")
        if isinstance(data, dict):
            data = data.get("optionChain") or data.get("option_chain") or data.get("records")
        if not isinstance(data, list):
            return []
        return [r for r in data if isinstance(r, dict)]

    def discover_options(
        self,
        underlying: str,
        strikecount: int = 20,
        expiries: int = 2,
    ) -> List[Instrument]:
        """Fetch live option chains and register normalized contracts.

        Uses the read-only ``/option/chain`` endpoint (which requires an
        ``expiry_date``) for the nearest ``expiries`` listed expiries.
        Returns the list of discovered ``Instrument`` objects (empty on
        failure / auth error). No orders are placed.
        """
        if not self._provider.is_authenticated:
            return []

        expiry_list = self.list_expiries(underlying)
        if not expiry_list:
            return []

        discovered: list[Instrument] = []
        seen: set[tuple] = set()
        for expiry in expiry_list[: max(1, int(expiries))]:
            rows = self.fetch_chain_rows(underlying, expiry)
            for entry in rows:
                for option_type, leg in self.iter_chain_legs(entry):
                    instr = self._entry_to_instrument(underlying, entry, option_type, leg=leg)
                    if instr is None:
                        continue
                    identity = (
                        (instr.underlying or "").upper(),
                        instr.expiry,
                        instr.strike,
                        instr.option_type,
                    )
                    if identity in seen:
                        continue
                    seen.add(identity)
                    self.repo.register(instr)
                    discovered.append(instr)
        return discovered

    @staticmethod
    def iter_chain_legs(entry: dict):
        """Yield ``(OptionType, leg_dict)`` for every leg present in a chain row.

        Handles the current Upstox v2 shape (``call_options`` / ``put_options``)
        as well as the legacy ``ce`` / ``pe`` and ``callOption`` / ``putOption``
        shapes, so callers never depend on one provider revision.
        """
        pairs = (
            ("ce", "pe", OptionType.CE, OptionType.PE),
            ("callOption", "putOption", OptionType.CE, OptionType.PE),
            ("call_options", "put_options", OptionType.CE, OptionType.PE),
        )
        for call_key, put_key, ce_type, pe_type in pairs:
            for key, otype in ((call_key, ce_type), (put_key, pe_type)):
                leg = entry.get(key)
                if isinstance(leg, dict):
                    yield otype, leg

    @staticmethod
    def leg_field(leg: dict, *names):
        """Read the first non-None field from a leg, including nested market data.

        Upstox v2 nests quoted values under ``market_data`` /
        ``option_greeks``; legacy shapes put them at the top level.
        """
        nested = [
            leg.get("market_data"),
            leg.get("marketData"),
            leg.get("option_greeks"),
            leg.get("optionGreeks"),
        ]
        for name in names:
            if leg.get(name) is not None:
                return leg.get(name)
            for sub in nested:
                if isinstance(sub, dict) and sub.get(name) is not None:
                    return sub.get(name)
        return None

    def _entry_to_instrument(
        self,
        underlying: str,
        entry: dict,
        option_type: OptionType,
        leg: Optional[dict] = None,
    ) -> Optional[Instrument]:
        """Convert one option-chain row into a normalized Instrument."""
        strike_raw = entry.get("strikePrice") or entry.get("strike_price") or entry.get("strike")
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
        if leg is None:
            sub = entry.get("ce") if option_type == OptionType.CE else entry.get("pe")
            if not isinstance(sub, dict):
                sub = entry.get("callOption") if option_type == OptionType.CE else entry.get("putOption")
            leg = sub if isinstance(sub, dict) else None
        if isinstance(leg, dict):
            provider_key = self.leg_field(leg, "instrument_key", "instrumentKey", "key")
            if provider_key is None and isinstance(leg.get("market_data"), dict):
                provider_key = leg["market_data"].get("instrument_key")

        instr = Instrument.option(
            "NSE", underlying.upper(), expiry_iso, strike_f, ot,
            provider_symbol=provider_key,
        )
        if provider_key:
            instr.provider_symbol = provider_key
        # Exchange lot size (Phase D) — chain rows omit it, so fall back to the
        # value learned from /option/contract for the same instrument key.
        lot_size_raw = (
            entry.get("lotSize")
            or entry.get("lot_size")
            or entry.get("lot size")
            or (self._lot_sizes.get(provider_key) if provider_key else None)
        )
        if lot_size_raw is not None:
            try:
                instr.lot_size = int(lot_size_raw)
            except (ValueError, TypeError):
                pass
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
