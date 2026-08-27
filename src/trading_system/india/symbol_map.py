"""Provider-specific symbol mapping (FYERS).

FYERS symbol conventions (from current v3 docs / official SDK samples):
  * Equity  : "NSE:SBIN-EQ"
  * Index   : "NSE:NIFTY50-INDEX"  (note: NIFTY50, not NIFTY)
  * Futures : "NSE:SBIN25DEC400CE"  (example option; futures similarly)
This module is the ONLY place that knows these strings.
"""
from __future__ import annotations

from .instruments import Instrument, InstrumentType


def to_fyers_symbol(instrument: Instrument) -> str:
    """Map a normalized Instrument to its FYERS symbol string."""
    ex = instrument.internal.exchange
    sym = instrument.internal.symbol
    t = instrument.instrument_type

    if t == InstrumentType.INDEX:
        suffix = "INDEX"
        body = sym
        # FYERS index names: NIFTY50, NIFTYBANK, FINNIFTY, etc.
        return f"{ex}:{body}-{suffix}"

    if t == InstrumentType.EQUITY:
        return f"{ex}:{sym}-EQ"

    if t in (InstrumentType.OPTION_CE, InstrumentType.OPTION_PE):
        # Requires underlying/expiry/strike from the instrument.
        if not (instrument.underlying and instrument.expiry and instrument.strike):
            raise ValueError("Options need underlying/expiry/strike for FYERS symbol")
        # FYERS option format e.g. NSE:SBIN25DEC400CE (no spaces, year omitted,
        # month as 3-letter, strike integer-ish).
        import datetime as dt

        exp = dt.date.fromisoformat(instrument.expiry)
        yy = f"{exp.year % 100:02d}"
        mm = exp.strftime("%b").upper()
        ot = "CE" if t == InstrumentType.OPTION_CE else "PE"
        strike = int(instrument.strike)
        return f"{ex}:{instrument.underlying}{yy}{mm}{strike}{ot}"

    # Futures / other: fall back to equity-style with -FUT if known.
    return f"{ex}:{sym}-FUT"


def from_fyers_symbol(fyers_symbol: str) -> Instrument:
    """Best-effort parse of a FYERS symbol back to an Instrument.

    Not all FYERS symbols are unambiguously reversible; this handles the common
    equity/index forms. Returns a minimal Instrument with provider_symbol set.
    """
    if ":" not in fyers_symbol:
        raise ValueError(f"Not a FYERS symbol: {fyers_symbol!r}")
    ex, body = fyers_symbol.split(":", 1)
    if body.endswith("-INDEX"):
        raw = body[: -len("-INDEX")]
        return Instrument(
            instrument_type=InstrumentType.INDEX,
            internal=__import__("typing").cast(
                "InternalSymbolType", None
            ) if False else _internal(ex, raw),
            provider_symbol=fyers_symbol,
        )
    if body.endswith("-EQ"):
        raw = body[: -len("-EQ")]
        return Instrument(
            instrument_type=InstrumentType.EQUITY,
            internal=_internal(ex, raw),
            provider_symbol=fyers_symbol,
        )
    # Unknown suffix: treat as generic equity-ish.
    return Instrument(
        instrument_type=InstrumentType.EQUITY,
        internal=_internal(ex, body),
        provider_symbol=fyers_symbol,
    )


def _internal(ex: str, sym: str):
    from .instruments import InternalSymbol

    return InternalSymbol(exchange=ex.upper(), symbol=sym)
