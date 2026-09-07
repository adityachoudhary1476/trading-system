"""Phase D — lot-size / contract-size resolution for option accounting.

The exchange defines a lot size (a.k.a. contract size / market lot) for every
listed option. The paper trading stack MUST resolve this from canonical
instrument metadata before any premium-based cash movement is computed.

This module is the single, deterministic resolver:

    * the source of truth is ``Instrument.lot_size``;
    * lot_size ``None`` on an option instrument is treated as UNKNOWN and the
      caller (the ``OptionAccountingGate``) is expected to reject execution
      rather than silently default to 1;
    * for cash equities (NSE/BSE delivery, indices treated as cash) the
      accounting layer uses ``contract_size = 1`` implicitly — this matches
      pre-Phase D behaviour and is the documented back-compat semantic.

A separate ``OptionMetadata`` helper exposes ``is_option``, ``is_expired``,
and ``can_value_for_mtm`` flags so downstream code does not need to inspect
free-form strings.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from ..india.instruments import Instrument, InstrumentType, OptionType


# A reasonable positive integer upper bound — catches accidentally negative
# lot sizes or absurd values (e.g. from a corrupted provider feed). 1,000,000
# units per contract is more than enough for any real listed instrument.
MAX_LOT_SIZE = 1_000_000


@dataclass(frozen=True)
class ResolvedContractSize:
    """The outcome of resolving ``contract_size`` for one instrument.

    ``contract_size`` is the number of underlying units per *one* contract.
    For cash equities this is always ``1``. For options this is
    ``Instrument.lot_size`` when supplied, otherwise ``None`` (which means
    "unknown — do NOT compute financially meaningful P&L").

    ``is_option`` distinguishes an option from an equity so the broker can
    choose to apply different accounting without checking strings.
    """

    contract_size: Optional[int]
    is_option: bool
    is_expired: bool

    @property
    def has_lot_size(self) -> bool:
        """True when ``contract_size`` is known and > 0."""
        return self.contract_size is not None and self.contract_size > 0

    def __bool__(self) -> bool:  # convenience: True iff has_lot_size
        return self.has_lot_size


def _parse_expiry(expiry: Optional[str]) -> Optional[date]:
    if not expiry:
        return None
    try:
        # Accept both bare "YYYY-MM-DD" and full ISO datetimes.
        s = str(expiry).strip()
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        return date.fromisoformat(s[:10])
    except Exception:
        return None


def is_option_instrument(instrument: Instrument) -> bool:
    return instrument.instrument_type in (
        InstrumentType.OPTION_CE,
        InstrumentType.OPTION_PE,
    )


def resolve_contract_size(instrument: Instrument, *, as_of: Optional[date] = None) -> ResolvedContractSize:
    """Resolve the contract size for an instrument, plus its option / expiry flags.

    For non-option instruments the contract_size is always ``1`` (cash
    equities, indices treated as cash, futures are not handled here).

    For option instruments the contract size is whatever the instrument
    metadata carries — ``None`` if the metadata does not include a lot size.
    The caller is responsible for rejecting financially-meaningful execution
    when ``has_lot_size`` is False (see :class:`OptionAccountingGate`).
    """
    is_option = is_option_instrument(instrument)
    if not is_option:
        # Equity / index / future: contract_size is implicitly 1 unit per
        # "share/contract". Back-compat with the pre-Phase D codebase.
        return ResolvedContractSize(
            contract_size=1, is_option=False, is_expired=False
        )

    raw_lot = instrument.lot_size
    contract_size: Optional[int]
    if raw_lot is None:
        contract_size = None
    else:
        try:
            lot_int = int(raw_lot)
        except (TypeError, ValueError):
            contract_size = None
        else:
            if lot_int <= 0 or lot_int > MAX_LOT_SIZE:
                contract_size = None
            else:
                contract_size = lot_int

    today = as_of or datetime.now(timezone.utc).date()
    exp = _parse_expiry(instrument.expiry)
    expired = exp is not None and exp < today

    return ResolvedContractSize(
        contract_size=contract_size, is_option=True, is_expired=expired
    )


__all__ = [
    "MAX_LOT_SIZE",
    "ResolvedContractSize",
    "is_option_instrument",
    "resolve_contract_size",
]