"""Risk-based position sizing for the greeks decision layer (Phase 3).

Replaces the fixed ``qty = cfg.max_contracts_per_leg`` with a quantity derived
from how much loss one leg is allowed to carry. Three clamps compete for every
sizing call: the per-leg currency risk budget, the currency notional cap
(``capital * max_position_allocation_pct``), and the existing
``max_contracts_per_leg`` count limit. ``SizingResult.bound_by`` reports which
one actually bound, in the exact label spelling ``GreeksMetrics.record_sizing``
buckets.

Two decisions are load-bearing.

**Fail open (D1).** ``None`` means "the caller falls back to the legacy fixed
size", never "refuse the trade". A verdict with ``available`` not ``True``, no
greeks, or an unusable option price (missing, non-finite, non-positive) all
degrade to ``None``, and the whole body is exception-guarded so even a hostile
verdict object cannot raise out of a sizing call.

**Unit trap.** ``Greeks.price`` is quoted per *option unit*, but risk is per
*contract*: one contract covers ``contract_multiplier`` units (1 for India, 100
for US). ``risk_per_contract = abs(price) * contract_multiplier`` is therefore
the max loss of one long contract. Reading the per-unit price as per-contract
risk is a 1x sizing error in India and a 100x error elsewhere.

A cap that is NaN, non-finite or negative is sanitised to ``0.0`` before use,
so a typo'd or unbounded cap can only ever shrink the position, never enlarge
it. Pure and deterministic: no I/O, no clock, no module state -- identical
arguments always produce an identical ``SizingResult``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "BOUND_BY_MAX_CONTRACTS",
    "BOUND_BY_NOTIONAL",
    "BOUND_BY_RISK_BUDGET",
    "SizingResult",
    "size_for_risk_budget",
]

BOUND_BY_RISK_BUDGET = "risk_budget"
BOUND_BY_NOTIONAL = "max_position_allocation_pct"
BOUND_BY_MAX_CONTRACTS = "max_contracts_per_leg"


@dataclass(frozen=True)
class SizingResult:
    """One successful sizing answer: how many contracts, and why not more.

    ``quantity`` is a floored contract count (>= 0), ``bound_by`` names the
    binding clamp using one of the three module constants, and
    ``risk_per_contract`` is the per-contract max loss the arithmetic used
    (always populated on a returned result, so callers can re-derive or log
    the risk they took on).
    """

    quantity: int
    bound_by: str
    risk_per_contract: Optional[float]


def _safe_attr(obj: Any, name: str) -> Any:
    """getattr that cannot raise: a hostile object must degrade, not abort."""
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _finite_float(value: Any) -> Optional[float]:
    """Finite float interpretation of ``value``, or None if there isn't one."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _sanitised_cap(value: Any) -> float:
    """A cap value that can never enlarge the position.

    NaN, +/-inf and negatives all become 0.0: an unbounded or typo'd cap must
    clamp to nothing rather than pass through as "no limit".
    """
    number = _finite_float(value)
    if number is None or number < 0.0:
        return 0.0
    return number


def size_for_risk_budget(
    *,
    verdict: Any,
    risk_budget: float,
    notional_cap: float,
    max_contracts: float,
    contract_multiplier: int = 1,
) -> Optional[SizingResult]:
    """Contracts to trade for this leg, or None to use the legacy fixed size.

    ``risk_budget`` is the currency max loss allowed for the leg, ``notional_cap``
    is ``capital * max_position_allocation_pct`` in currency, and
    ``max_contracts`` is ``cfg.max_contracts_per_leg`` (possibly fractional).
    Each is floored into a contract count and the minimum wins; ties resolve to
    ``max_contracts``, then ``notional_cap``, then ``risk_budget``, which is the
    precedence ``bound_by`` reports.

    ``contract_multiplier`` converts per-unit greeks price into per-contract
    risk. Never raises: any unusable input (see module docstring) yields None,
    and any unexpected error is swallowed into the same fail-open answer.
    """
    try:
        return _size(
            verdict,
            risk_budget,
            notional_cap,
            max_contracts,
            contract_multiplier,
        )
    except Exception:
        return None


def _size(
    verdict: Any,
    risk_budget: float,
    notional_cap: float,
    max_contracts: float,
    contract_multiplier: int,
) -> Optional[SizingResult]:
    if verdict is None:
        return None
    if _safe_attr(verdict, "available") is not True:
        return None
    greeks = _safe_attr(verdict, "greeks")
    if greeks is None:
        return None
    price = _finite_float(_safe_attr(greeks, "price"))
    if price is None or price <= 0.0:
        return None
    multiplier = _finite_float(contract_multiplier)
    if multiplier is None or multiplier <= 0.0:
        return None

    risk_per_contract = abs(price) * multiplier

    budget = _sanitised_cap(risk_budget)
    notional = _sanitised_cap(notional_cap)
    cap = math.floor(_sanitised_cap(max_contracts))

    by_budget = math.floor(budget / risk_per_contract)
    by_notional = math.floor(notional / risk_per_contract)

    quantity = max(0, min(by_budget, by_notional, cap))

    if cap <= by_budget and cap <= by_notional:
        bound_by = BOUND_BY_MAX_CONTRACTS
    elif by_notional <= by_budget:
        bound_by = BOUND_BY_NOTIONAL
    else:
        bound_by = BOUND_BY_RISK_BUDGET

    return SizingResult(
        quantity=quantity,
        bound_by=bound_by,
        risk_per_contract=risk_per_contract,
    )
