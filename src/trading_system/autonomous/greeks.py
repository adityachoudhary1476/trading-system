"""Black-Scholes pricing and greeks for the options risk path.

Upstox's quote and chain endpoints do not supply greeks, so they are derived
locally from inputs the pipeline already has: the underlying spot, the strike,
time to expiry, a risk-free rate, and an implied volatility taken from the
quote's own bid/ask IV when present.

Why this exists: a percentage stop is the wrong unit for an option. A 3% loss
on a 0.05-delta weekly call is ordinary decay, while a 3% loss on a 0.55-delta
ATM call is a broken thesis. The same threshold cannot mean both things. Delta
gives the exit logic a unit that does.

Conventions, fixed once here so every consumer agrees:
    delta  - price change per 1.00 move in the underlying
    gamma  - delta change per 1.00^2 move in the underlying
    theta  - price change per calendar day (negative for long premium)
    vega   - price change per 1 percentage point of implied volatility
    rho    - price change per 1 percentage point of the interest rate

Everything here is pure and deterministic, and returns ``None`` rather than
guessing when its inputs cannot support a valid answer. A risk rule that cannot
be evaluated must be treated as unknown, never as satisfied.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional, Union

__all__ = [
    "Greeks",
    "DEFAULT_RISK_FREE_RATE",
    "normalise_option_type",
    "normalise_iv",
    "IV_PERCENT_CUTOFF",
    "black_scholes_price",
    "calculate_greeks",
    "implied_vol_from_price",
    "greeks_from_market_inputs",
    "years_to_expiry",
    "SECONDS_PER_YEAR",
]

# Indian index options are deep-ITM-heavy and the market quotes rates well
# above short-term policy, so a single mid-point rate is a reasonable stand-in
# for the curve. It is a constant on purpose: a rate that drifts tick to tick
# would move rho and therefore the implied vol solved from a market price, for
# no modelling benefit.
DEFAULT_RISK_FREE_RATE = 0.065

SECONDS_PER_YEAR = 365.0 * 24.0 * 60.0 * 60.0

_CALL_ALIASES = frozenset({"C", "CE", "CALL"})
_PUT_ALIASES = frozenset({"P", "PE", "PUT"})

# Bisection bounds for the implied-vol solve. The lower bound keeps sigma
# strictly positive so the d1/d2 denominators never collapse; the upper bound
# is well above any tradeable index option and exists only to stop a corrupt
# price from driving the solve into an unbounded loop.
_MIN_SOLVE_VOL = 1e-6
_MAX_SOLVE_VOL = 5.0

# Below this much time value an option is effectively at intrinsic and its
# implied volatility is undefined, so the solve reports unknown instead of
# inventing a number.
_MIN_TIME_VALUE = 1e-8

# Indian option IV above this many percent is not a real quote, so a raw value
# at or above it is unambiguously a percentage and a value below it is
# unambiguously already a decimal. This is what lets the parser accept both
# without a flag.
IV_PERCENT_CUTOFF = 3.0


def normalise_iv(value: Optional[Union[float, str]]) -> Optional[float]:
    """Convert a raw provider implied volatility to a decimal fraction.

    Upstox reports IV in percentage points, while Black-Scholes takes sigma as
    a fraction. Passing ``15.0`` where ``0.15`` is expected does not raise: it
    silently yields 1500% volatility, a delta pinned at 1.0, and greeks that
    look plausible while being fiction. The conversion belongs at the one
    boundary that knows the provider's unit, so it lives here rather than in
    each consumer.

    Providers that already send a decimal pass through untouched, since any
    real volatility is below the cutoff. A missing, unparseable, zero or
    negative value is ``None``: a zero volatility has no meaning and would
    divide by zero downstream.

    This is the single authority for IV units. Both the quote path
    (``india.option_quotes``) and the chain path (``india.option_chain_provider``)
    route through it, so the two cannot drift apart again.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or math.isinf(f):
        return None
    if f <= 0.0:
        return None
    if f > IV_PERCENT_CUTOFF:
        return f / 100.0
    return f


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


@dataclass(frozen=True)
class Greeks:
    """Greeks for one option contract at a point in time.

    ``price`` and ``implied_vol`` are carried alongside so a snapshot is
    self-describing: a stored delta is meaningless without the price and vol it
    was derived from, and holding all three makes that explicit rather than
    implicit.
    """

    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float
    price: float
    implied_vol: float


def normalise_option_type(option_type: Union[str, None]) -> Optional[str]:
    """Return ``"CE"``/``"PE"``, or None if unrecognised.

    Upstox, the chain provider and the position book each spell the option type
    differently ("CE", "C", "CALL"), so normalisation happens once here rather
    than being re-guessed at every call site.
    """
    if option_type is None:
        return None
    token = str(option_type).strip().upper()
    if token in _CALL_ALIASES:
        return "CE"
    if token in _PUT_ALIASES:
        return "PE"
    return None


def _validate_inputs(
    spot: float,
    strike: float,
    years: float,
    sigma: float,
    kind: Optional[str],
) -> bool:
    # None is checked first, explicitly. A bare ``sigma > 0.0`` raises
    # TypeError on None, which would turn "volatility unavailable" into an
    # exception escaping a function documented to return None instead. Every
    # numeric input reaches here from optional provider data, so None is the
    # common case, not an exotic one.
    if spot is None or strike is None or years is None or sigma is None:
        return False
    if kind is None:
        return False
    if not isinstance(spot, (int, float)) or not isinstance(strike, (int, float)):
        return False
    if not isinstance(years, (int, float)) or not isinstance(sigma, (int, float)):
        return False
    if not (spot > 0.0 and strike > 0.0):
        return False
    if not math.isfinite(spot) or not math.isfinite(strike):
        return False
    if not (years > 0.0):
        return False
    if not (sigma > 0.0):
        return False
    return math.isfinite(sigma) and math.isfinite(years)


def black_scholes_price(
    *,
    spot: float,
    strike: float,
    years_to_expiry: float,
    rate: float = DEFAULT_RISK_FREE_RATE,
    sigma: float,
    option_type: Union[str, None],
) -> Optional[float]:
    """European option price under Black-Scholes.

    Returns None when the inputs cannot produce a valid price (non-positive
    spot or strike, non-positive time or volatility, unrecognised option type).
    Callers get ``None``, never 0.0, so a missing price is never mistaken for a
    worthless one.
    """
    kind = normalise_option_type(option_type)
    if not _validate_inputs(spot, strike, years_to_expiry, sigma, kind):
        return None

    sqrt_t = math.sqrt(years_to_expiry)
    d1 = (
        math.log(spot / strike)
        + (rate + 0.5 * sigma * sigma) * years_to_expiry
    ) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    discount = math.exp(-rate * years_to_expiry)

    if kind == "CE":
        return spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
    return strike * discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def calculate_greeks(
    *,
    spot: float,
    strike: float,
    years_to_expiry: float,
    rate: float = DEFAULT_RISK_FREE_RATE,
    sigma: float,
    option_type: Union[str, None],
) -> Optional[Greeks]:
    """Compute price and greeks, or None if the inputs are unusable.

    Unlike :func:`black_scholes_price` this requires strictly positive time to
    expiry. At expiry an option's delta is a step function and its implied
    volatility is undefined, so reporting a number there would be false
    precision. An expired contract is already handled by the portfolio's own
    ``expired`` exit, which fires first.
    """
    kind = normalise_option_type(option_type)
    if not _validate_inputs(spot, strike, years_to_expiry, sigma, kind):
        return None

    price = black_scholes_price(
        spot=spot,
        strike=strike,
        years_to_expiry=years_to_expiry,
        rate=rate,
        sigma=sigma,
        option_type=kind,
    )
    if price is None:
        return None

    sqrt_t = math.sqrt(years_to_expiry)
    d1 = (
        math.log(spot / strike)
        + (rate + 0.5 * sigma * sigma) * years_to_expiry
    ) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    discount = math.exp(-rate * years_to_expiry)
    density = _norm_pdf(d1)

    gamma = density / (spot * sigma * sqrt_t)
    # Vega is the same for calls and puts; only the sign convention differs.
    vega = spot * density * sqrt_t / 100.0

    common_theta = -(spot * density * sigma) / (2.0 * sqrt_t)
    if kind == "CE":
        delta = _norm_cdf(d1)
        theta = (common_theta - rate * strike * discount * _norm_cdf(d2)) / 365.0
        rho = strike * years_to_expiry * discount * _norm_cdf(d2) / 100.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (common_theta + rate * strike * discount * _norm_cdf(-d2)) / 365.0
        rho = -strike * years_to_expiry * discount * _norm_cdf(-d2) / 100.0

    return Greeks(
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        rho=rho,
        price=price,
        implied_vol=sigma,
    )


def implied_vol_from_price(
    *,
    price: float,
    spot: float,
    strike: float,
    years_to_expiry: float,
    rate: float = DEFAULT_RISK_FREE_RATE,
    option_type: Union[str, None],
) -> Optional[float]:
    """Solve for the implied volatility that reproduces ``price``.

    This is what makes delta computable without a separate chain fetch. The
    mark-to-market path already pays for a quote and already holds the option's
    market price, so recovering volatility from that price by bisection gives
    the greeks at zero additional network cost.

    Returns None when no valid solution exists: bad inputs, a price outside the
    no-arbitrage bounds, or an option with no time value left (an at-the-money
    option one minute from expiry, or a deep-ITM option already pinned to
    intrinsic). In every one of those cases the honest answer is "volatility
    unknown", and a caller must not treat it as zero.
    """
    kind = normalise_option_type(option_type)
    if kind is None or not (price > 0.0 and spot > 0.0 and strike > 0.0):
        return None
    if not (years_to_expiry > 0.0):
        return None
    if not all(math.isfinite(v) for v in (price, spot, strike, years_to_expiry)):
        return None

    discount = math.exp(-rate * years_to_expiry)
    lower_bound = max(spot - strike * discount, 0.0) if kind == "CE" else max(
        strike * discount - spot, 0.0
    )
    upper_bound = spot if kind == "CE" else strike * discount

    if price < lower_bound - 1e-8 or price > upper_bound + 1e-8:
        return None
    if price - lower_bound <= _MIN_TIME_VALUE:
        return None

    def _price_at(sigma: float) -> Optional[float]:
        return black_scholes_price(
            spot=spot,
            strike=strike,
            years_to_expiry=years_to_expiry,
            rate=rate,
            sigma=sigma,
            option_type=kind,
        )

    low, high = _MIN_SOLVE_VOL, _MAX_SOLVE_VOL
    low_price = _price_at(low)
    if low_price is None:
        return None
    if price < low_price:
        return None

    for _ in range(200):
        mid = (low + high) / 2.0
        mid_price = _price_at(mid)
        if mid_price is None:
            return None
        if abs(mid_price - price) <= 1e-8:
            return mid
        if mid_price < price:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def greeks_from_market_inputs(
    *,
    spot: Optional[float],
    strike: Optional[float],
    expiry: Union[str, date, datetime, None],
    option_type: Union[str, None],
    ltp: Optional[float] = None,
    quote_iv: Optional[float] = None,
    now: Optional[datetime] = None,
    rate: float = DEFAULT_RISK_FREE_RATE,
) -> Optional[Greeks]:
    """Greeks for a held contract, from whatever a quote happens to carry.

    Volatility is taken from ``quote_iv`` when the provider supplied it, and
    otherwise recovered from ``ltp`` by inversion. The fallback is what makes
    this affordable: the mark-to-market path already pays for a quote and
    already holds the premium, so in the common case where the payload omits
    IV the greeks cost no extra network call rather than requiring a separate
    chain fetch per position per sweep.

    Returns None whenever the contract's greeks are unknowable — no spot, an
    unparseable expiry, an expired contract, no usable volatility. Callers must
    propagate that as "greeks unknown"; treating it as zero exposure would let
    a position be held because the maths could not run.
    """
    if spot is None or strike is None:
        return None
    if not (float(spot) > 0.0 and float(strike) > 0.0):
        return None
    kind = normalise_option_type(option_type)
    if kind is None:
        return None

    years = years_to_expiry(expiry, now)
    if years is None or years <= 0.0:
        return None

    sigma: Optional[float] = None
    if quote_iv is not None and float(quote_iv) > 0.0:
        sigma = float(quote_iv)
    elif ltp is not None and float(ltp) > 0.0:
        sigma = implied_vol_from_price(
            price=float(ltp),
            spot=float(spot),
            strike=float(strike),
            years_to_expiry=years,
            rate=rate,
            option_type=kind,
        )
    if sigma is None:
        return None

    return calculate_greeks(
        spot=float(spot),
        strike=float(strike),
        years_to_expiry=years,
        rate=rate,
        sigma=sigma,
        option_type=kind,
    )


def years_to_expiry(
    expiry: Union[str, date, datetime, None],
    now: Optional[datetime] = None,
) -> Optional[float]:
    """Years remaining until ``expiry``, or None if it cannot be determined.

    Accepts the several shapes an expiry takes in this codebase: an ISO
    datetime string from the book, a plain ``YYYY-MM-DD`` date string from the
    chain provider, or a date/datetime. A bare date is read as end of that day
    UTC, which is how Indian index options actually expire.

    The result is clamped at 0.0 rather than going negative, so a stale or
    mistyped expiry cannot manufacture time value.
    """
    if expiry is None:
        return None

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    parsed: Optional[datetime] = None
    if isinstance(expiry, datetime):
        parsed = expiry
    elif isinstance(expiry, date):
        parsed = datetime(expiry.year, expiry.month, expiry.day, tzinfo=timezone.utc)
        # A bare date means the whole session, not its midnight.
        parsed = parsed.replace(hour=23, minute=59, second=59)
    else:
        raw = str(expiry).strip()
        if not raw:
            return None
        # Order matters. Python 3.11's datetime.fromisoformat accepts a bare
        # "YYYY-MM-DD" and returns midnight, which would silently override the
        # end-of-session reading below, so the date-only form is tried first.
        try:
            bare = date.fromisoformat(raw)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                return None
        else:
            parsed = datetime(
                bare.year, bare.month, bare.day, 23, 59, 59, tzinfo=timezone.utc
            )

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    remaining = (parsed - reference).total_seconds()
    if remaining <= 0.0:
        return 0.0
    return remaining / SECONDS_PER_YEAR
