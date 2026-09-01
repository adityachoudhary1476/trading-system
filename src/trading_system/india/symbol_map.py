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


def to_upstox_symbol(instrument: Instrument) -> str:
    """Map a normalized Instrument to its Upstox symbol string.

    Upstox instrument keys use the form EXCHANGE_SEGMENT|SYMBOL, e.g.:
      * Equity  : NSE_EQ|SBIN
      * Index   : NSE_INDEX|NIFTY50
      * Futures : NSE_FUT|SBIN25DECFUT
      * Options : NSE_OPT|SBIN25DEC400CE
    """
    ex = instrument.internal.exchange.upper()
    t = instrument.instrument_type
    base = instrument.internal.symbol

    if t == InstrumentType.INDEX:
        segment = "INDEX"
        return f"{ex}_{segment}|{base}"
    if t == InstrumentType.EQUITY:
        segment = "EQ"
        return f"{ex}_{segment}|{base}"
    if t in (InstrumentType.OPTION_CE, InstrumentType.OPTION_PE):
        segment = "OPT"
        if not (instrument.underlying and instrument.expiry and instrument.strike):
            raise ValueError("Options need underlying/expiry/strike for Upstox symbol")
        import datetime as dt
        exp = dt.date.fromisoformat(instrument.expiry)
        yy = f"{exp.year % 100:02d}"
        mm = exp.strftime("%b").upper()
        ot = "CE" if t == InstrumentType.OPTION_CE else "PE"
        strike = int(instrument.strike)
        token = f"{instrument.underlying}{yy}{mm}{strike}{ot}"
        return f"{ex}_{segment}|{token}"
    if t == InstrumentType.FUTURE:
        segment = "FUT"
        return f"{ex}_{segment}|{base}"
    return f"{ex}_EQ|{base}"


def from_upstox_symbol(upstox_symbol: str) -> Instrument:
    """Best-effort parse of an Upstox symbol back to an Instrument."""
    if "|" not in upstox_symbol:
        raise ValueError(f"Not an Upstox symbol: {upstox_symbol!r}")
    prefix, body = upstox_symbol.split("|", 1)
    ex = prefix.split("_")[0] if "_" in prefix else prefix
    segment = prefix.split("_")[1] if "_" in prefix else "EQ"
    if segment == "INDEX":
        return Instrument(
            instrument_type=InstrumentType.INDEX,
            internal=_internal(ex, body),
            provider_symbol=upstox_symbol,
        )
    if segment == "EQ":
        return Instrument(
            instrument_type=InstrumentType.EQUITY,
            internal=_internal(ex, body),
            provider_symbol=upstox_symbol,
        )
    if segment == "OPT":
        instr = Instrument(
            instrument_type=InstrumentType.EQUITY,
            internal=_internal(ex, body),
            provider_symbol=upstox_symbol,
        )
        try:
            import re
            m = re.match(r"^([A-Z]+?)(\d{2})([A-Z]{3})(\d+)(CE|PE)$", body)
            if m:
                root, yy, mmm, strike_str, ot = m.groups()
                year = 2000 + int(yy)
                month = {
                    "JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                    "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12,
                }[mmm]
                expiry = f"{year:04d}-{month:02d}-28"
                instr = Instrument.option(
                    ex, root, expiry, float(strike_str), ot, provider_symbol=upstox_symbol
                )
                instr.internal = _internal(ex, body)
        except Exception:
            pass
        return instr
    if segment == "FUT":
        return Instrument(
            instrument_type=InstrumentType.FUTURE,
            internal=_internal(ex, body),
            provider_symbol=upstox_symbol,
        )
    return Instrument(
        instrument_type=InstrumentType.EQUITY,
        internal=_internal(ex, body),
        provider_symbol=upstox_symbol,
    )
