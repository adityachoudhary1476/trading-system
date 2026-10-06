"""Shadow-mode greeks availability policy for the decision layer.

Phase 0 does not decide anything: it evaluates the greeks of the contract the
resolver would pick and reports whether those greeks are *usable*. That verdict
is the trust predicate every later phase is built on (GREEKS_DECISION_LAYER.md
section 5, "The trust predicate").

Three decisions are load-bearing:

**Fail open (D1).** ``evaluate_candidate`` never raises and never refuses a
trade. Its only authority is the ``available`` flag: a caller that cannot use
the greeks falls through to today's behaviour unchanged. The module carries no
refusal logic, because the alternative -- refusing a trade because a quote was
missing one field -- would let unverified vendor data halt the bot.

**Per-field availability (D7).** The verdict does not collapse to a single
``greeks_ok = True/False``. A quote with no IV still yields a usable delta when
volatility can be recovered from the premium, and a missing unrequested greek
must not disable a working delta. ``missing`` names exactly which fields were
unavailable, and a computed ``None`` is preserved, never coerced to ``0.0``.

**Provenance.** ``iv_source`` records how volatility was obtained, mirroring
``chain_analytics``: ``"quote"`` is the market's own IV, ``"solved"`` is IV
inverted out of a single premium. Later phases build on the solved path (D3)
because the live IV field's names and scale are unverified.

Pure and deterministic: the chain/quote passed in is all that is consulted (no
fetching, no module state), and ``now`` is injected rather than read from the
clock (D5).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from .greeks import (
    DEFAULT_RISK_FREE_RATE,
    Greeks,
    calculate_greeks,
    implied_vol_from_price,
    normalise_iv,
    normalise_option_type,
    years_to_expiry,
)

__all__ = [
    "GreeksVerdict",
    "evaluate_candidate",
    "shadow_decision",
    "IV_SOURCE_QUOTE",
    "IV_SOURCE_SOLVED",
    "SPOT_MAX_DIVERGENCE",
    "DEFAULT_MIN_GREEKS",
    "DEFAULT_RISK_FREE_RATE",
    "Greeks",
    "FALLBACK_REASONS",
    "FALLBACK_NO_QUOTE",
    "FALLBACK_BAD_STRIKE",
    "FALLBACK_BAD_OPTION_TYPE",
    "FALLBACK_NO_SPOT",
    "FALLBACK_SPOT_MISMATCH",
    "FALLBACK_NO_REFERENCE_TIME",
    "FALLBACK_EXPIRED",
    "FALLBACK_NO_VOLATILITY",
    "FALLBACK_BAD_SPREAD",
    "FALLBACK_SOLVER_FAILED",
    "FALLBACK_GREEKS_NONE",
    "FALLBACK_EVALUATION_ERROR",
]

IV_SOURCE_QUOTE = "quote"
IV_SOURCE_SOLVED = "solved"

# Mirrors the resolver's own spot-sanity guard at options_contract.py:709 so
# the decision layer and the resolver cannot disagree about what "wildly
# inconsistent" means.
SPOT_MAX_DIVERGENCE = 0.15

# Phases 2-4 key off delta, so a caller that names no minimum gets delta.
DEFAULT_MIN_GREEKS = ("delta",)

# Closed set of reasons why greeks could not be trusted. Exported as a tuple
# so tests can prove the whole taxonomy is reachable and nothing else appears.
FALLBACK_NO_QUOTE = "no_quote"
FALLBACK_BAD_STRIKE = "bad_strike"
FALLBACK_BAD_OPTION_TYPE = "bad_option_type"
FALLBACK_NO_SPOT = "no_spot"
FALLBACK_SPOT_MISMATCH = "spot_mismatch"
FALLBACK_NO_REFERENCE_TIME = "no_reference_time"
FALLBACK_EXPIRED = "expired"
FALLBACK_NO_VOLATILITY = "no_volatility"
FALLBACK_BAD_SPREAD = "bad_spread"
FALLBACK_SOLVER_FAILED = "solver_failed"
FALLBACK_GREEKS_NONE = "greeks_none"
FALLBACK_EVALUATION_ERROR = "evaluation_error"

FALLBACK_REASONS = (
    FALLBACK_NO_QUOTE,
    FALLBACK_BAD_STRIKE,
    FALLBACK_BAD_OPTION_TYPE,
    FALLBACK_NO_SPOT,
    FALLBACK_SPOT_MISMATCH,
    FALLBACK_NO_REFERENCE_TIME,
    FALLBACK_EXPIRED,
    FALLBACK_NO_VOLATILITY,
    FALLBACK_BAD_SPREAD,
    FALLBACK_SOLVER_FAILED,
    FALLBACK_GREEKS_NONE,
    FALLBACK_EVALUATION_ERROR,
)


@dataclass(frozen=True)
class GreeksVerdict:
    """Whether one contract's greeks are usable, and why not if they are not.

    ``available`` is the fail-open pivot: ``False`` means the caller runs the
    legacy path unchanged, never that the trade is refused. ``greeks`` may still
    be populated when ``available`` is ``False`` (a computed set that missed one
    *requested* greek), and ``missing`` then names the exact fields that were
    unavailable so the loss is visible rather than folded into a boolean.
    """

    available: bool
    greeks: Optional[Greeks]
    iv_source: Optional[str]
    missing: tuple[str, ...]
    fallback_reason: Optional[str]
    instrument_key: Optional[str]


def _verdict(
    reason: Optional[str],
    missing: tuple[str, ...],
    *,
    key: Optional[str] = None,
    greeks: Optional[Greeks] = None,
    iv_source: Optional[str] = None,
    available: bool = False,
) -> GreeksVerdict:
    return GreeksVerdict(
        available=available,
        greeks=greeks,
        iv_source=iv_source,
        missing=tuple(missing),
        fallback_reason=reason,
        instrument_key=key,
    )


def _safe_attr(obj: Any, name: str) -> Any:
    """Getattr that cannot raise: a hostile object must degrade, not abort."""
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _positive_float(value: Any) -> Optional[float]:
    """Finite strictly-positive number, or None. Guards appear whenever a
    numeric input comes from optional provider data."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= 0.0:
        return None
    return f


def _positive_attr(obj: Any, name: str) -> Optional[float]:
    return _positive_float(_safe_attr(obj, name))


def _option_type_of(instrument: Any, quote: Any) -> Optional[str]:
    """Normalised ``"CE"``/``"PE"``, preferring the instrument over the quote.

    Both are consulted because either may carry it; the enum spelling used by
    the book is unpacked via ``.value`` before ``normalise_option_type`` sees it.
    """
    raw = _safe_attr(instrument, "option_type")
    if raw is None:
        raw = _safe_attr(quote, "option_type")
    if raw is None:
        return None
    return normalise_option_type(getattr(raw, "value", raw))


def _chain_spot_of(chain: Any) -> Optional[float]:
    if chain is None:
        return None
    return _positive_attr(chain, "spot_price")


def _quote_iv(quote: Any) -> Optional[float]:
    """Decimal IV from the quote, in preference order: bid/ask pair, a single
    side, then a whole-quote ``implied_vol`` field. The bid/ask pair carries
    the market's own skew; a crossed pair is a corrupt payload, not a quote.
    """
    bid_iv = normalise_iv(_safe_attr(quote, "bid_iv"))
    ask_iv = normalise_iv(_safe_attr(quote, "ask_iv"))
    if bid_iv is not None and ask_iv is not None:
        if ask_iv < bid_iv:
            return None
        return (bid_iv + ask_iv) / 2.0
    value = bid_iv if bid_iv is not None else ask_iv
    if value is not None:
        return value
    return normalise_iv(_safe_attr(quote, "implied_vol"))


def _premium_of(quote: Any) -> tuple[Optional[float], bool]:
    """A premium to solve volatility from, preferring LTP over the bid/ask mid.

    The second element is True only when the mid had to be used and the spread
    was zero-width or inverted: such a pair invalidates the mid itself, which
    is a distinct pathology from having no price at all.
    """
    for attr in ("ltp", "last"):
        price = _positive_float(_safe_attr(quote, attr))
        if price is not None:
            return price, False
    try:
        bid = float(_safe_attr(quote, "bid")) if _safe_attr(quote, "bid") else None
        ask = float(_safe_attr(quote, "ask")) if _safe_attr(quote, "ask") else None
    except (TypeError, ValueError):
        return None, False
    if bid is None or ask is None:
        return None, False
    if not math.isfinite(bid) or not math.isfinite(ask):
        return None, False
    if bid > 0.0 and ask > 0.0:
        if ask <= bid:
            return None, True
        return (bid + ask) / 2.0, False
    return None, False


def _usable(greeks: Greeks, field: str) -> bool:
    """True when the named greek is a real finite number, never a 0.0 stand-in.

    D7 made this an explicit check: a ``None`` that reaches a comparison is a
    bug, and a non-finite value is just as unusable as a missing one.
    """
    value = _safe_attr(greeks, field)
    if value is None:
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def evaluate_candidate(
    *,
    instrument: Any,
    quote: Any,
    chain: Any,
    spot: Optional[float] = None,
    now: Optional[datetime] = None,
    rate: float = DEFAULT_RISK_FREE_RATE,
    min_greeks: tuple[str, ...] = DEFAULT_MIN_GREEKS,
) -> GreeksVerdict:
    """Evaluate one candidate contract's greeks for the Phase 0 shadow log.

    The only question answered is "are the minimum required greeks usable, and
    if not, why". It is deliberately not "should we trade", which is what keeps
    this fail-open: a caller that sees ``available is False`` falls through to
    the unchanged legacy path, and this function carries no refusal logic.

    Volatility is taken from the quote's own IV when present (``iv_source``
    ``"quote"``) and otherwise recovered from the premium by inversion
    (``"solved"``, the path D3 says later phases must build on). The greeks are
    computed from the chain snapshot's ``spot_price``, which is the causal
    market anchor as-of the decision (D5); the ``spot`` argument exists only to
    reproduce the resolver's divergence guard, so an absent decision spot
    degrades open rather than blocking the chain's own number.

    Degrades to unavailable, never to an exception: ``None`` quote, unusable
    strike, missing spot, an expired contract, no volatility source, a failed
    solve, and any unexpected input error each map to one of the closed
    ``FALLBACK_*`` reasons.
    """
    key = _safe_attr(instrument, "instrument_key")
    try:
        return _evaluate(
            instrument, quote, chain, spot, now, rate, min_greeks, key
        )
    except Exception:
        return _verdict(
            FALLBACK_EVALUATION_ERROR, ("evaluation",), key=key
        )


def _evaluate(
    instrument: Any,
    quote: Any,
    chain: Any,
    spot: Optional[float],
    now: Optional[datetime],
    rate: float,
    min_greeks: tuple[str, ...],
    key: Optional[str],
) -> GreeksVerdict:
    if quote is None:
        return _verdict(FALLBACK_NO_QUOTE, ("quote",), key=key)

    strike = _positive_attr(instrument, "strike")
    if strike is None:
        return _verdict(FALLBACK_BAD_STRIKE, ("strike",), key=key)

    option_type = _option_type_of(instrument, quote)
    if option_type is None:
        return _verdict(FALLBACK_BAD_OPTION_TYPE, ("option_type",), key=key)

    chain_spot = _chain_spot_of(chain)
    if chain_spot is None:
        return _verdict(FALLBACK_NO_SPOT, ("chain_spot",), key=key)

    decision_spot = _positive_float(spot)
    if decision_spot is not None:
        if abs(chain_spot - decision_spot) / decision_spot > SPOT_MAX_DIVERGENCE:
            return _verdict(FALLBACK_SPOT_MISMATCH, ("spot",), key=key)

    if now is None:
        # D5 forbids reading the clock; without an injected reference there is
        # no deterministic way to know the expiry is still ahead of us.
        return _verdict(FALLBACK_NO_REFERENCE_TIME, ("now",), key=key)

    years = years_to_expiry(_safe_attr(chain, "expiry"), now)
    if years is None or years <= 0.0:
        return _verdict(FALLBACK_EXPIRED, ("expiry",), key=key)

    iv = _quote_iv(quote)
    iv_source = IV_SOURCE_QUOTE if iv is not None else None

    if iv is None:
        premium, bad_spread = _premium_of(quote)
        if bad_spread:
            return _verdict(FALLBACK_BAD_SPREAD, ("bid", "ask"), key=key)
        if premium is None:
            return _verdict(
                FALLBACK_NO_VOLATILITY, ("implied_volatility", "premium"), key=key
            )
        iv = implied_vol_from_price(
            price=premium,
            spot=chain_spot,
            strike=strike,
            years_to_expiry=years,
            rate=rate,
            option_type=option_type,
        )
        if iv is None:
            return _verdict(
                FALLBACK_SOLVER_FAILED, ("implied_volatility",), key=key
            )
        iv_source = IV_SOURCE_SOLVED

    calculated = calculate_greeks(
        spot=chain_spot,
        strike=strike,
        years_to_expiry=years,
        rate=rate,
        sigma=iv,
        option_type=option_type,
    )
    if calculated is None:
        missing = tuple(min_greeks) if min_greeks else ("greeks",)
        return _verdict(FALLBACK_GREEKS_NONE, missing, key=key)

    unavailable = tuple(
        f for f in tuple(min_greeks) if not _usable(calculated, f)
    )
    if unavailable:
        return _verdict(
            FALLBACK_GREEKS_NONE,
            unavailable,
            key=key,
            greeks=calculated,
            iv_source=iv_source,
        )

    return _verdict(
        None, (), key=key, greeks=calculated, iv_source=iv_source, available=True
    )


def shadow_decision(verdict: GreeksVerdict, cfg: Any) -> dict:
    """Plain shadow-mode payload describing what later phases would have done.

    Phase 0 logs this dict instead of acting on it. It reports availability
    (the Phase 1 trust predicate), the provenance of any IV, and whether the
    target-delta/risk-sizing phases (2-3) would have had a delta to key off.
    ``cfg`` is carried for the phase flag gate that later phases introduce; it
    is not consulted yet, so the output is a pure function of ``verdict`` and
    never mutates it.
    """
    delta = None
    if verdict.greeks is not None and verdict.greeks.delta is not None:
        delta = verdict.greeks.delta
    usable = bool(verdict.available and delta is not None)
    return {
        "phase": 0,
        "shadow": True,
        "instrument_key": verdict.instrument_key,
        "greeks_available": bool(verdict.available),
        "fallback_reason": verdict.fallback_reason,
        "iv_source": verdict.iv_source,
        "missing": list(verdict.missing),
        "delta": delta,
        "phase2_target_delta_would_run": usable,
        "phase3_risk_sizing_would_run": usable,
        "would_fallback_to_legacy": bool(not verdict.available),
    }