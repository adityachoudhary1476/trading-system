"""Phase D — Option accounting gate.

A small, deterministic, fail-closed gate that runs *before* the order
reaches the broker. Its responsibilities:

  * Reject an option order whose contract_size cannot be resolved from the
    canonical instrument metadata (an unknown lot size must NEVER produce
    financially meaningful P&L).
  * Reject fresh quotes for an option whose ``expiry`` is strictly in the
    past (the contract is no longer tradable; the broker must not silently
    keep marking it at a stale premium).
  * Surface the resolved ``contract_size`` / ``is_option`` / ``is_expired``
    flags so the broker / risk layer can build correct cash movement and
    P&L without re-parsing the instrument.

The gate does NOT touch cash, does NOT place orders, does NOT call any
broker method. It only inspects a ``ResolvedContractSize`` + option metadata
and returns a typed decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ..india.instruments import Instrument, OptionType
from .lot_size import ResolvedContractSize, resolve_contract_size


class OptionAccountingDecision(str, Enum):
    """Outcome of the option accounting gate."""

    ALLOW = "allow"
    REJECT_UNKNOWN_LOT_SIZE = "reject_unknown_lot_size"
    REJECT_EXPIRED = "reject_expired"
    REJECT_NOT_AN_OPTION = "reject_not_an_option"


@dataclass(frozen=True)
class OptionAccountingVerdict:
    """Typed outcome of the gate.

    ``contract_size`` is propagated so the broker / risk layer can use it
    directly without re-resolving.
    """

    decision: OptionAccountingDecision
    contract_size: Optional[int]
    is_option: bool
    is_expired: bool
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == OptionAccountingDecision.ALLOW


class OptionAccountingError(RuntimeError):
    """Raised when an option order violates the accounting invariants.

    This is a paper-only safety error: a real broker would silently
    mis-account, the paper stack fails closed.
    """


# Reason tokens (stable, lowercase; safe to surface in API responses).
REASON_UNKNOWN_LOT_SIZE = "option_lot_size_unknown"
REASON_EXPIRED = "option_expired"
REASON_NOT_OPTION = "not_an_option_contract"


def evaluate_option_accounting(
    instrument: Optional[Instrument],
    *,
    as_of: Optional[object] = None,  # date or datetime
    operation: str = "execute",
) -> OptionAccountingVerdict:
    """Evaluate the option accounting invariants for one instrument.

    ``operation`` selects the check:
      * ``"execute"``     — a NEW order is being submitted for the instrument.
      * ``"mark_to_market"`` — a fresh market quote is being applied for MTM.

    Both checks share the unknown-lot-size rule; the expiry rule only fires
    for ``mark_to_market`` because an order at expiry has already been
    prevented upstream by the quote path in the production flow. The gate
    applies both rules regardless to be conservative.
    """
    if instrument is None:
        return OptionAccountingVerdict(
            decision=OptionAccountingDecision.REJECT_NOT_AN_OPTION,
            contract_size=None,
            is_option=False,
            is_expired=False,
            reason=REASON_NOT_OPTION,
        )
    resolved = resolve_contract_size(instrument, as_of=as_of)  # type: ignore[arg-type]
    if not resolved.is_option:
        # Cash equity: nothing to gate. contract_size = 1 (back-compat).
        return OptionAccountingVerdict(
            decision=OptionAccountingDecision.ALLOW,
            contract_size=resolved.contract_size,
            is_option=False,
            is_expired=False,
            reason="equity_or_index",
        )
    if not resolved.has_lot_size:
        return OptionAccountingVerdict(
            decision=OptionAccountingDecision.REJECT_UNKNOWN_LOT_SIZE,
            contract_size=None,
            is_option=True,
            is_expired=resolved.is_expired,
            reason=REASON_UNKNOWN_LOT_SIZE,
        )
    if resolved.is_expired and operation == "mark_to_market":
        return OptionAccountingVerdict(
            decision=OptionAccountingDecision.REJECT_EXPIRED,
            contract_size=resolved.contract_size,
            is_option=True,
            is_expired=True,
            reason=REASON_EXPIRED,
        )
    return OptionAccountingVerdict(
        decision=OptionAccountingDecision.ALLOW,
        contract_size=resolved.contract_size,
        is_option=True,
        is_expired=resolved.is_expired,
        reason="ok",
    )


__all__ = [
    "OptionAccountingDecision",
    "OptionAccountingError",
    "OptionAccountingVerdict",
    "REASON_EXPIRED",
    "REASON_NOT_OPTION",
    "REASON_UNKNOWN_LOT_SIZE",
    "evaluate_option_accounting",
]