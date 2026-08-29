"""Normalized Indian instrument model + provider symbol mapping.

The rest of the system works with `InternalSymbol` (exchange:symbol) and an
`Instrument` (rich metadata). Provider-specific identifiers (FYERS uses
"NSE:SBIN-EQ", "NSE:NIFTY50-INDEX", "MCX:SILVERMIC25DECFUT",
"NSE:NIFTY25DEC24800CE") stay inside the FYERS adapter (see `derivatives.py`).
This keeps Angel One / Upstox swappable later.
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


class OptionType(str, Enum):
    """Option right. Mirrors the FYERS CE/PE token but provider-independent."""

    CE = "CE"
    PE = "PE"


@dataclass(frozen=True)
class InternalSymbol:
    """Provider-agnostic symbol key used throughout the app.

    For derivatives the raw trading symbol is the FULL contract token (e.g.
    "NIFTY25DEC24800CE"), so two contracts with different expiries/strikes are
    naturally distinct keys. The `Instrument` carries the structured metadata
    (underlying/expiry/strike/option_type) needed to reason about contracts.
    """

    exchange: str          # "NSE" / "NFO" / "MCX"
    symbol: str            # "RELIANCE" (equity) or "NIFTY25DEC24800CE" (derivative)

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
    """Normalized instrument descriptor (day-count-agnostic).

    `contract_id` is a canonical, *stable* identity that disambiguates contracts
    even when the raw `symbol` token would otherwise collide across providers or
    asset classes. Two instruments compare equal iff their `contract_id` matches.
    """

    internal: InternalSymbol
    instrument_type: InstrumentType
    name: Optional[str] = None
    # Provider token/identifier, e.g. FYERS symbol string. Populated by adapters.
    provider_symbol: Optional[str] = None
    underlying: Optional[str] = None        # for F&O / index-based derivatives
    expiry: Optional[str] = None            # ISO date for F&O ("2025-12-25")
    strike: Optional[float] = None          # for options
    option_type: Optional[str] = None       # "CE"/"PE"
    exchange_full: Optional[str] = None     # e.g. "NSE F&O" / "MCX COMM"

    @property
    def key(self) -> str:
        return self.internal.key

    @property
    def contract_id(self) -> str:
        """Stable, unique identity for the contract.

        * Equity / index -> "EXCHANGE:SYMBOL" (the normal key).
        * Future        -> "EXCHANGE:UNDERLYING|EXPIRY|FUT"
        * Option        -> "EXCHANGE:UNDERLYING|EXPIRY|STRIKE|CE|PE"

        NIFTY June future and NIFTY July future therefore produce different ids;
        NIFTY 25000 CE and NIFTY 25000 PE likewise never collide.
        """
        if self.instrument_type == InstrumentType.FUTURE:
            return f"{self.internal.exchange}:{self.underlying}|{self.expiry}|FUT"
        if self.instrument_type in (InstrumentType.OPTION_CE, InstrumentType.OPTION_PE):
            ot = (self.option_type or ("CE" if self.instrument_type == InstrumentType.OPTION_CE else "PE"))
            strike = int(self.strike) if self.strike is not None else 0
            return f"{self.internal.exchange}:{self.underlying}|{self.expiry}|{strike}|{ot}"
        return self.key

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Instrument):
            return NotImplemented
        return self.contract_id == other.contract_id

    def __hash__(self) -> int:
        return hash(self.contract_id)

    @classmethod
    def future(
        cls,
        exchange: str,
        underlying: str,
        expiry: str,
        name: Optional[str] = None,
        provider_symbol: Optional[str] = None,
    ) -> "Instrument":
        return cls(
            internal=InternalSymbol(exchange=exchange, symbol=provider_symbol or underlying),
            instrument_type=InstrumentType.FUTURE,
            name=name or f"{underlying} {expiry} FUT",
            provider_symbol=provider_symbol,
            underlying=underlying,
            expiry=expiry,
            exchange_full=exchange,
        )

    @classmethod
    def option(
        cls,
        exchange: str,
        underlying: str,
        expiry: str,
        strike: float,
        option_type: OptionType | str,
        name: Optional[str] = None,
        provider_symbol: Optional[str] = None,
    ) -> "Instrument":
        ot = option_type.value if isinstance(option_type, OptionType) else str(option_type).upper()
        itype = InstrumentType.OPTION_CE if ot == "CE" else InstrumentType.OPTION_PE
        return cls(
            internal=InternalSymbol(exchange=exchange, symbol=provider_symbol or underlying),
            instrument_type=itype,
            name=name or f"{underlying} {expiry} {int(strike)} {ot}",
            provider_symbol=provider_symbol,
            underlying=underlying,
            expiry=expiry,
            strike=float(strike),
            option_type=ot,
            exchange_full=exchange,
        )


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
        self._by_contract: dict[str, Instrument] = {}
        for i in (instruments or DEFAULT_INSTRUMENTS):
            self.register(i)

    def register(self, instrument: Instrument) -> None:
        # Index by both the user-facing key and the stable contract identity so
        # derivatives (whose symbol == full token) and equities both resolve.
        self._by_key[instrument.key] = instrument
        self._by_contract[instrument.contract_id] = instrument

    def get(self, internal: InternalSymbol) -> Optional[Instrument]:
        # Direct key lookup first, then by contract_id (covers derivatives whose
        # internal.symbol is the full token), then by bare symbol.
        return (
            self._by_key.get(internal.key)
            or self._by_contract.get(internal.key)
            or self._by_key.get(internal.symbol)
        )

    def resolve(self, symbol: str) -> Instrument:
        """Resolve an InternalSymbol string to a registered/default Instrument."""
        internal = InternalSymbol.parse(symbol)
        existing = self._by_key.get(internal.key) or self._by_key.get(internal.symbol)
        if existing:
            return existing
        # Unknown but well-formed -> create a minimal equity placeholder.
        return Instrument(internal, InstrumentType.EQUITY)

