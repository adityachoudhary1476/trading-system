"""Normalized Indian instrument model + provider symbol mapping.

The rest of the system works with `InternalSymbol` (exchange:symbol) and an
`Instrument` (rich metadata). Provider-specific identifiers (FYERS uses
"NSE:SBIN-EQ", "NSE:NIFTY50-INDEX", "NSE:SBIN25DEC400CE") stay inside the
FYERS adapter. This keeps Angel One / Upstox swappable later.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Exchange(str, Enum):
    NSE = "NSE"
    BSE = "BSE"
    NFO = "NFO"   # NSE F&O
    MCX = "MCX"   # commodities
    CDS = "CDS"   # currency


class InstrumentType(str, Enum):
    EQUITY = "equity"
    INDEX = "index"
    FUTURE = "future"
    OPTION_CE = "option_ce"
    OPTION_PE = "option_pe"


@dataclass(frozen=True)
class InternalSymbol:
    """Provider-agnostic symbol key used throughout the app."""

    exchange: str          # "NSE"
    symbol: str            # "RELIANCE"  (raw trading symbol, no suffix)

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.symbol}"

    @classmethod
    def parse(cls, s: str) -> "InternalSymbol":
        if ":" in s:
            ex, sym = s.split(":", 1)
            return cls(exchange=ex.upper(), symbol=sym)
        raise ValueError(f"InternalSymbol must be 'EXCHANGE:SYMBOL', got {s!r}")


@dataclass
class Instrument:
    """Normalized instrument descriptor (day-count-agnostic)."""

    internal: InternalSymbol
    instrument_type: InstrumentType
    name: Optional[str] = None
    # Provider token/identifier, e.g. FYERS symbol string. Populated by adapters.
    provider_symbol: Optional[str] = None
    underlying: Optional[str] = None        # for F&O
    expiry: Optional[str] = None            # ISO date for F&O
    strike: Optional[float] = None          # for options
    option_type: Optional[str] = None       # "CE"/"PE"
    exchange_full: Optional[str] = None     # e.g. "NSE" / "NSE F&O"

    @property
    def key(self) -> str:
        return self.internal.key


# Curated default instruments (NOT a hard-coded allow-list; the system accepts any
# valid InternalSymbol, these are just convenient defaults for CLI/tests).
DEFAULT_INSTRUMENTS: list[Instrument] = [
    Instrument(InternalSymbol("NSE", "RELIANCE"), InstrumentType.EQUITY, "Reliance Industries"),
    Instrument(InternalSymbol("NSE", "TCS"), InstrumentType.EQUITY, "Tata Consultancy Services"),
    Instrument(InternalSymbol("NSE", "INFY"), InstrumentType.EQUITY, "Infosys"),
    Instrument(InternalSymbol("NSE", "HDFCBANK"), InstrumentType.EQUITY, "HDFC Bank"),
    Instrument(InternalSymbol("NSE", "ICICIBANK"), InstrumentType.EQUITY, "ICICI Bank"),
    Instrument(InternalSymbol("NSE", "SBIN"), InstrumentType.EQUITY, "State Bank of India"),
    Instrument(InternalSymbol("NSE", "NIFTY50"), InstrumentType.INDEX, "Nifty 50"),
    Instrument(InternalSymbol("NSE", "NIFTYBANK"), InstrumentType.INDEX, "Bank Nifty"),
    Instrument(InternalSymbol("NSE", "FINNIFTY"), InstrumentType.INDEX, "Fin Nifty"),
]


class InstrumentRegistry:
    """In-memory registry; later backed by the FYERS symbol master CSV."""

    def __init__(self, instruments: Optional[list[Instrument]] = None) -> None:
        self._by_key: dict[str, Instrument] = {}
        for i in (instruments or DEFAULT_INSTRUMENTS):
            self._by_key[i.key] = i

    def register(self, instrument: Instrument) -> None:
        self._by_key[instrument.key] = instrument

    def get(self, internal: InternalSymbol) -> Optional[Instrument]:
        return self._by_key.get(internal.key)

    def resolve(self, symbol: str) -> Instrument:
        """Resolve an InternalSymbol string to a registered/default Instrument."""
        internal = InternalSymbol.parse(symbol)
        existing = self._by_key.get(internal.key)
        if existing:
            return existing
        # Unknown but well-formed -> create a minimal equity placeholder.
        return Instrument(internal, InstrumentType.EQUITY)
