"""Instrument repository: normalized lookup + provider-agnostic parsing.

The repository keeps a normalized `Instrument` per `InternalSymbol` and supports
queries (get/ search/ equities/ indices/ derivatives). Provider-specific symbol
masters (e.g. FYERS CSV) are parsed into the normalized model by the importer, so
the rest of the system never depends on a broker's raw master format.

Design note: the system must eventually *discover* instruments dynamically from a
provider master rather than rely on a hand-coded list. `import_master_rows` accepts
already-parsed rows (or fixture data) and merges them in. Live master download is
out of scope for Day 4 (requires auth); the integration is a thin step later.
"""
from __future__ import annotations

import csv
import io
from datetime import date
from typing import Iterable, Optional

from .instruments import (
    Exchange,
    Instrument,
    InstrumentRegistry,
    InstrumentType,
    InternalSymbol,
)
from .symbol_map import from_fyers_symbol, to_fyers_symbol


# Map FYERS instrument-master 'option_type' / suffix tokens to our enum.
_SUFFIX_TO_TYPE = {
    "EQ": InstrumentType.EQUITY,
    "INDEX": InstrumentType.INDEX,
    "CE": InstrumentType.OPTION_CE,
    "PE": InstrumentType.OPTION_PE,
    "FUT": InstrumentType.FUTURE,
}


class InstrumentRepository:
    """Normalized instrument store with rich queries."""

    def __init__(self, registry: Optional[InstrumentRegistry] = None) -> None:
        self.registry = registry or InstrumentRegistry()

    # --- normalized queries ----------------------------------------------
    def get_instrument(self, symbol: str) -> Optional[Instrument]:
        return self.registry.get(InternalSymbol.parse(symbol))

    def register(self, instrument: Instrument) -> None:
        self.registry.register(instrument)

    def search_instruments(self, query: str) -> list[Instrument]:
        q = query.upper()
        return [i for i in self.registry._by_key.values() if q in i.key or q in (i.name or "").upper()]

    def get_equities(self, exchange: str | None = None) -> list[Instrument]:
        return self._filter(InstrumentType.EQUITY, exchange)

    def get_indices(self, exchange: str | None = None) -> list[Instrument]:
        return self._filter(InstrumentType.INDEX, exchange)

    def get_derivatives(self, exchange: str | None = None) -> list[Instrument]:
        derivs = (InstrumentType.OPTION_CE, InstrumentType.OPTION_PE, InstrumentType.FUTURE)
        out = [i for i in self.registry._by_key.values() if i.instrument_type in derivs]
        if exchange:
            out = [i for i in out if i.internal.exchange == exchange.upper()]
        return out

    def get_expiring_derivatives(self, as_of: date, within_days: int = 7) -> list[Instrument]:
        out = []
        for i in self.get_derivatives():
            if i.expiry:
                exp = date.fromisoformat(i.expiry)
                if 0 <= (exp - as_of).days <= within_days:
                    out.append(i)
        return out

    def _filter(self, itype: InstrumentType, exchange: str | None) -> list[Instrument]:
        out = [i for i in self.registry._by_key.values() if i.instrument_type == itype]
        if exchange:
            out = [i for i in out if i.internal.exchange == exchange.upper()]
        return out

    # --- importing parsed rows (provider-agnostic) ------------------------
    def import_master_rows(self, rows: Iterable[dict]) -> int:
        """Merge parsed master rows into the registry. Returns count added."""
        added = 0
        for r in rows:
            instr = _row_to_instrument(r)
            if instr is not None:
                existing = self.registry.get(instr.internal)
                if existing is None or not existing.provider_symbol:
                    self.registry.register(instr)
                    added += 1
        return added

    @classmethod
    def from_fyers_csv(cls, csv_text: str) -> "InstrumentRepository":
        """Parse a FYERS symbol-master CSV (documented column layout) into the repo.

        Does NOT download anything. Feed it fixture/auth-fetched text only.
        """
        repo = cls()
        reader = csv.DictReader(io.StringIO(csv_text))
        repo.import_master_rows(reader)
        return repo


def _row_to_instrument(row: dict) -> Optional[Instrument]:
    """Convert one parsed master row into a normalized Instrument.

    Expected keys (FYERS-documented): Symbol, Exch, Token, Instrument, Expiry,
    StrikePrice, OptionType, LotSize. Provider keys are normalized here only.

    We build the InternalSymbol from the master's own fields (not by reversing the
    FYERS symbol string) so derivatives — whose FYERS symbol embeds expiry/strike —
    resolve correctly.
    """
    fy_sym = (row.get("symbol") or row.get("Symbol") or "").strip()
    if not fy_sym:
        return None
    exch = (row.get("exch") or row.get("Exch") or "NSE").strip().upper()
    option_type = (row.get("option_type") or row.get("OptionType") or "").strip().upper()
    strike_raw = (row.get("strike") or row.get("StrikePrice") or "").strip()
    expiry = (row.get("expiry") or row.get("Expiry") or "").strip()
    underlying = (row.get("instrument") or row.get("Instrument") or "").strip()

    itype = _SUFFIX_TO_TYPE.get(option_type, InstrumentType.EQUITY)
    # Fall back to the FYERS symbol suffix when master metadata lacks OptionType
    # (equities use -EQ, indices use -INDEX; derivatives carry CE/PE/FUT in
    # OptionType but also encode it in the suffix).
    if itype == InstrumentType.EQUITY and "-" in fy_sym:
        suffix = fy_sym.rsplit("-", 1)[-1].upper()
        if suffix in _SUFFIX_TO_TYPE:
            itype = _SUFFIX_TO_TYPE[suffix]
    # Derive the internal symbol from master fields.
    if itype == InstrumentType.INDEX:
        sym = underlying or fy_sym.split("-")[0]
    elif itype in (InstrumentType.OPTION_CE, InstrumentType.OPTION_PE, InstrumentType.FUTURE):
        # Internal symbol keeps the full FYERS-style token for uniqueness, e.g.
        # SBIN25DEC400CE -> NSE:SBIN25DEC400CE.
        sym = fy_sym.split("-")[0] if "-" in fy_sym else fy_sym
    else:
        sym = underlying or fy_sym.split("-")[0]

    instr = Instrument(
        internal=InternalSymbol(exchange=exch, symbol=sym),
        instrument_type=itype,
        name=underlying or sym,
    )
    instr.provider_symbol = fy_sym
    instr.exchange_full = exch
    if strike_raw:
        try:
            instr.strike = float(strike_raw)
        except ValueError:
            pass
    if expiry:
        instr.expiry = expiry
    if itype in (InstrumentType.OPTION_CE, InstrumentType.OPTION_PE, InstrumentType.FUTURE):
        instr.underlying = underlying
    return instr
