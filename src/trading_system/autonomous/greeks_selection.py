"""Phase 2 of the greeks decision layer — target-delta strike selection.

Phases 0-1 only *report* whether a contract's greeks are usable; Phase 2 spends
that verdict. It walks the chain, evaluates every strike through the pinned
``evaluate_candidate`` trust predicate, and returns the strike whose computed
delta lands closest to ``target_delta`` inside ``delta_tolerance``. Four
decisions pin the behaviour down:

**Fail open (D1).** The result is ``Optional[StrikeSelection]``. ``None`` means
"nothing in the band" — or "no usable greeks", or "malformed input" — and the
caller keeps today's legacy selection unchanged. Nothing here refuses a trade
and nothing here raises: every input path degrades to ``None``, because a
selection engine that throws on a corrupt chain would turn unverified vendor
data into an outage.

**Band, not near miss.** A strike outside the band is never returned, even when
it is the closest thing on the chain. A tolerance that admits nothing is the
caller saying the greeks do not support this trade, which is exactly when the
legacy path must take over; returning the least-bad strike would silently
widen the band the caller configured.

**Magnitude, sign preserved.** Calls carry positive deltas and puts negative
ones, so the target is matched on ``abs(delta)`` (a caller passing ``-0.30``
means the same band as ``0.30``), while the signed value is carried through in
``StrikeSelection.delta`` so the caller never has to re-derive which side it is
on.

**Deterministic (D5).** No clock is read — ``now`` is injected — no I/O, no
module state. Ties break to the lower strike, so the same chain and the same
arguments always produce the same answer, which is what makes the caller's
fallback decision reproducible too.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from trading_system.autonomous.greeks import DEFAULT_RISK_FREE_RATE, normalise_option_type
from trading_system.autonomous.greeks_policy import evaluate_candidate
from trading_system.india.instruments import OptionType

__all__ = [
    "StrikeSelection",
    "select_by_target_delta",
]


@dataclass(frozen=True)
class StrikeSelection:
    """One in-band strike and the delta evidence that selected it.

    ``delta`` is signed exactly as computed (calls positive, puts negative) and
    ``distance`` is ``abs(abs(delta) - abs(target_delta))`` — kept alongside so
    a caller can log how much room the selection had without recomputing it.
    """

    strike: float
    delta: float
    iv_source: Optional[str]
    distance: float


@dataclass(frozen=True)
class _Candidate:
    """Minimal instrument view ``evaluate_candidate`` reads.

    The policy layer only ever touches ``strike``, ``option_type`` and
    ``instrument_key`` on the instrument, so a tiny local record is enough and
    the heavyweight options-contract model stays out of the import graph.
    """

    strike: Any
    option_type: OptionType
    instrument_key: Optional[str]


def _safe_attr(obj: Any, name: str) -> Any:
    """getattr that cannot raise: a hostile object must degrade, not abort."""
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _normalised_side(option_type: Any) -> Optional[str]:
    """``"CE"``/``"PE"``, or None when the side cannot be normalised.

    The enum spelling is unpacked via ``.value`` first because ``str()`` of a
    non-string enum is ``"OptionType.CE"``, which no alias table recognises.
    """
    try:
        return normalise_option_type(getattr(option_type, "value", option_type))
    except Exception:
        return None


def _chain_usable(chain: Any) -> bool:
    """True when the duck-typed chain has everything a sweep needs.

    Checked once up front so a half-built chain fails before any per-strike
    work rather than producing a partial sweep that looks like "no candidate".
    """
    if chain is None:
        return False
    if _safe_attr(chain, "strikes") is None:
        return False
    if not callable(_safe_attr(chain, "get_quote")):
        return False
    if _safe_attr(chain, "spot_price") is None:
        return False
    if _safe_attr(chain, "expiry") is None:
        return False
    return True


def _usable_delta(verdict: Any) -> Optional[float]:
    """Signed delta of an *available* verdict, or None if it cannot be trusted.

    Re-checks what the Phase 0 trust predicate already checked (availability,
    a non-None delta, finiteness) rather than assuming it, and additionally
    rejects a delta pinned at exactly 0 or +/-1: those are the degenerate tails
    of the distribution, they carry no discriminating information for a target
    match, and allowing them would break the ``0 < abs(delta) < 1`` guarantee
    the selection contract makes whenever it returns a strike.
    """
    if verdict is None or not _safe_attr(verdict, "available"):
        return None
    greeks = _safe_attr(verdict, "greeks")
    if greeks is None:
        return None
    raw = _safe_attr(greeks, "delta")
    if raw is None:
        return None
    try:
        delta = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(delta):
        return None
    if not 0.0 < abs(delta) < 1.0:
        return None
    return delta


def _instrument_key(chain: Any, strike: Any, side: str) -> Optional[str]:
    """Deterministic identity for the candidate, for the verdict's audit trail."""
    underlying = _safe_attr(chain, "underlying")
    expiry = _safe_attr(chain, "expiry")
    return f"{underlying}|{expiry}|{strike}|{side}"


def select_by_target_delta(
    *,
    chain: Any,
    option_type: Any,
    target_delta: float,
    delta_tolerance: float,
    now: Optional[datetime],
    rate: float = DEFAULT_RISK_FREE_RATE,
) -> Optional[StrikeSelection]:
    """Strike whose computed delta lands closest to ``target_delta`` in band.

    ``target_delta`` is a magnitude (a negative value is accepted and compared
    on ``abs``); ``delta_tolerance`` is the half-width of the acceptable band
    around it. Only strikes whose verdict is ``available`` with a usable delta
    are considered; among those that fall inside the band the smallest
    ``abs(abs(delta) - abs(target_delta))`` wins, ties breaking to the lower
    strike.

    Returns ``None`` — never raises — when nothing qualifies, when the greeks
    are unavailable (``now=None``, expired contract, no volatility source, ...),
    or when the input is malformed. The caller then falls back to legacy
    selection; a strike outside the band is never returned.
    """
    try:
        return _select(chain, option_type, target_delta, delta_tolerance, now, rate)
    except Exception:
        return None


def _select(
    chain: Any,
    option_type: Any,
    target_delta: float,
    delta_tolerance: float,
    now: Optional[datetime],
    rate: float,
) -> Optional[StrikeSelection]:
    side = _normalised_side(option_type)
    if side is None:
        return None
    if not _chain_usable(chain):
        return None

    try:
        magnitude = abs(float(target_delta))
        tolerance = float(delta_tolerance)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(magnitude) or not math.isfinite(tolerance):
        return None

    strikes = _safe_attr(chain, "strikes")
    if strikes is None:
        return None
    wanted = OptionType.CE if side == "CE" else OptionType.PE

    best_key: Optional[tuple[float, float]] = None
    best_strike = 0.0
    best_delta = 0.0
    best_iv_source: Optional[str] = None

    for strike in strikes:
        try:
            quote = chain.get_quote(strike, wanted)
            verdict = evaluate_candidate(
                instrument=_Candidate(
                    strike=strike,
                    option_type=wanted,
                    instrument_key=_instrument_key(chain, strike, side),
                ),
                quote=quote,
                chain=chain,
                now=now,
                rate=rate,
            )
            delta = _usable_delta(verdict)
            if delta is None:
                continue
            strike_key = float(strike)
        except Exception:
            continue

        distance = abs(abs(delta) - magnitude)
        if not (distance <= tolerance):
            continue
        key = (distance, strike_key)
        if best_key is None or key < best_key:
            best_key = key
            best_strike = strike_key
            best_delta = delta
            best_iv_source = _safe_attr(verdict, "iv_source")

    if best_key is None:
        return None
    return StrikeSelection(
        strike=best_strike,
        delta=best_delta,
        iv_source=best_iv_source,
        distance=best_key[0],
    )
