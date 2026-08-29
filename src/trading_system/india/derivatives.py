"""FYERS derivative symbol resolution (provider-specific, isolated).

This module is the ONLY place that knows how FYERS encodes F&O / commodity
contracts on the wire. Everything else in the system works with the
provider-independent `Instrument` / `DerivativeRequest` model.

Verified FYERS symbol conventions (from the installed ``fyers_apiv3`` SDK source,
not guessed):
  * Index         : ``NSE:NIFTY50-INDEX``            (note: NIFTY50, not NIFTY)
  * Equity        : ``NSE:SBIN-EQ``
  * Index/Equity future-ish token: ``<UNDERLYING><YY><MMM><STRIKE?><CE|PE|FUT>``
  * Commodity FUT : ``MCX:SILVERMIC25DECFUT``          (no dash; abbreviated root)
                    ``MCX:SILVERMIC20NOVFUT``
  * Option chain  : discovered via the ``optionchain(symbol, timestamp,
                    strikecount)`` endpoint (symbol = the underlying index/stock
                    FYERS symbol, e.g. ``NSE:NIFTY50-INDEX``).

Rules encoded here:
  * Compact token = ``<ROOT><YY><MMM><STRIKE><CE|PE>`` for options, with no dash.
    ``ROOT`` is the FYERS contract root (e.g. ``NIFTY``, ``BANKNIFTY``,
    ``SILVERMIC``). ``YY`` = 2-digit year, ``MMM`` = 3-letter uppercase month.
  * Futures append ``FUT`` with **no strike** (FYERS future token has no strike).
  * We never fabricate today's specific contract: the caller supplies the expiry;
    this module only formats it.

The discovery endpoint (option chain) is intentionally NOT re-implemented here;
see ``instrument_repository.py`` for the provider-independent discovery surface,
which uses the live ``optionchain`` call through the FYERS model object.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Optional

from .instruments import (
    Exchange,
    Instrument,
    InstrumentType,
    InternalSymbol,
    OptionType,
)

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_MONTHS_REV = {v: k for k, v in _MONTHS.items()}

# FYERS uses short root codes for some underlyings. Map the human underlying to
# the FYERS contract root. Keep this small and explicit; extend as needed.
_ROOT_ALIASES = {
    "NIFTY": "NIFTY",
    "NIFTY50": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
    "SILVERMIC": "SILVERMIC",
    "GOLD": "GOLD",
    "CRUDEOIL": "CRUDEOIL",
    "CRUDEOILM": "CRUDEOILM",
}

# FYERS exchange for an underlying asset class.
_EXCHANGE_BY_KIND = {
    "index": "NSE",
    "equity": "NSE",
    "commodity": "MCX",
}


@dataclass
class DerivativeRequest:
    """Provider-independent description of a derivative contract to resolve.

    This is what the CLI / user supplies. It is NOT a FYERS string.
    """

    underlying: str                  # "NIFTY", "BANKNIFTY", "SILVERMIC", "SBIN"
    kind: str = "future"            # "future" | "option" | "option_ce" | "option_pe"
    expiry: Optional[str] = None    # ISO date "2025-12-25"
    strike: Optional[float] = None  # for options
    option_type: Optional[str] = None  # "CE"/"PE" (derived from kind if omitted)

    def __post_init__(self) -> None:
        if self.kind in ("option", "option_ce"):
            self.kind = "option"
            self.option_type = self.option_type or "CE"
        elif self.kind in ("option_pe",):
            self.kind = "option"
            self.option_type = "PE"
        if self.kind == "option" and self.option_type is None:
            raise ValueError("option_type (CE/PE) required for option requests")
        if self.kind == "option" and self.strike is None:
            raise ValueError("strike required for option requests")
        if self.expiry is None:
            raise ValueError("expiry required for derivative requests")
        # Normalize expiry to ISO.
        self.expiry = dt.date.fromisoformat(self.expiry).isoformat()


def _root_for(underlying: str) -> str:
    u = underlying.upper().strip()
    return _ROOT_ALIASES.get(u, u)


def _expiry_to_token(expiry: str) -> tuple[str, str]:
    """Return (yy, mmm) compact tokens for a YYYY-MM-DD expiry."""
    d = dt.date.fromisoformat(expiry)
    return f"{d.year % 100:02d}", d.strftime("%b").upper()


def to_fyers_derivative_symbol(req: DerivativeRequest) -> str:
    """Build the FYERS wire symbol for a derivative request.

    Format (verified): ``<EXCHANGE>:<ROOT><YY><MMM><STRIKE?><CE|PE|FUT>``.
    Equity/Index options use NFO; commodity futures use MCX.
    """
    root = _root_for(req.underlying)
    yy, mmm = _expiry_to_token(req.expiry)
    kind = req.kind
    if kind == "future":
        exch = _EXCHANGE_BY_KIND.get("commodity" if root in ("SILVERMIC", "GOLD", "CRUDEOIL", "CRUDEOILM") else "equity")
        token = f"{root}{yy}{mmm}FUT"
        return f"{exch}:{token}"
    if kind == "option":
        ot = req.option_type or "CE"
        strike = int(req.strike)
        exch = "NFO"
        token = f"{root}{yy}{mmm}{strike}{ot}"
        return f"{exch}:{token}"
    raise ValueError(f"Unsupported derivative kind: {kind}")


# Pre-compiled pattern for parsing a FYERS derivative token back into components.
# Examples: NIFTY25DEC24800CE, BANKNIFTY25DEC54000PE, SILVERMIC25DECFUT
_DERIV_TOKEN = re.compile(
    r"^(?P<root>[A-Z]+?)(?P<yy>\d{2})(?P<mmm>[A-Z]{3})(?P<rest>(?:\d+)?(?:CE|PE|FUT))$"
)


def from_fyers_derivative_symbol(fyers_symbol: str) -> Instrument:
    """Parse a FYERS derivative wire symbol into a normalized Instrument.

    Raises ValueError if the symbol does not look like a FYERS derivative token.
    The exchange prefix (``NFO:`` / ``MCX:`` / ``NSE:``) is preserved.
    """
    if ":" not in fyers_symbol:
        raise ValueError(f"Not a FYERS symbol: {fyers_symbol!r}")
    exch, body = fyers_symbol.split(":", 1)
    m = _DERIV_TOKEN.match(body)
    if not m:
        raise ValueError(f"Unrecognized FYERS derivative token: {fyers_symbol!r}")
    root = m.group("root")
    yy = int(m.group("yy"))
    mmm = m.group("mmm")
    rest = m.group("rest")
    if mmm not in _MONTHS:
        raise ValueError(f"Bad month in FYERS token: {fyers_symbol!r}")
    month = _MONTHS[mmm]
    # Infer a plausible expiry year. FYERS 2-digit year is ambiguous across
    # centuries; we assume 2000+ (valid for any contract trading this decade).
    year = 2000 + yy
    # Expiry day: FYERS option futures expire on a known weekday; we don't guess
    # the exact day, so we record the last day of the month as the contract month
    # marker. Discovery (optionchain) is the authoritative source for the exact
    # expiry timestamp.
    expiry = f"{year:04d}-{month:02d}-28"
    if rest.endswith("FUT"):
        # Commodity roots are MCX; others NFO.
        exchange = "MCX" if root in ("SILVERMIC", "GOLD", "CRUDEOIL", "CRUDEOILM") else exch
        instr = Instrument.future(exchange, root, expiry, provider_symbol=fyers_symbol)
        instr.internal = InternalSymbol(exchange=exchange, symbol=body)
        return instr
    # Option: rest = "<strike><CE|PE>"
    ot = rest[-2:]
    strike_str = rest[:-2]
    strike = float(strike_str) if strike_str else 0.0
    instr = Instrument.option(
        "NFO", root, expiry, strike, ot, provider_symbol=fyers_symbol
    )
    instr.internal = InternalSymbol(exchange="NFO", symbol=body)
    return instr


def build_instrument(req: DerivativeRequest) -> Instrument:
    """Resolve a DerivativeRequest into a normalized Instrument + FYERS symbol."""
    fy = to_fyers_derivative_symbol(req)
    if req.kind == "future":
        exch = fy.split(":", 1)[0]
        instr = Instrument.future(exch, req.underlying.upper(), req.expiry, provider_symbol=fy)
    else:
        instr = Instrument.option(
            "NFO", req.underlying.upper(), req.expiry, req.strike, req.option_type,
            provider_symbol=fy,
        )
    return instr
