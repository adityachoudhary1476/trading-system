"""Per-strike greeks and aggregates across a full option chain.

The position-level greeks in :mod:`autonomous.greeks` answer "is the trade I am
holding still the trade I meant to open?". This module answers the wider
question: "what does the whole chain look like right now?" — the strike-by-strike
surface a trader actually reads, where delta, IV, open interest and their changes
sit side by side around the money.

Two design decisions are load-bearing:

**The window is bounded and centred on the money.** A NIFTY weekly chain runs to
several hundred strikes, and the wings are close to worthless: their greeks are
numerically valid but economically meaningless, and solving implied volatility
for each one costs real time for a number nobody will act on. So the default is
a symmetric window around the ATM strike, with the full chain available by
raising the bound. The reported window is always explicit, never implicit.

**Unknown is unknown.** Every field is ``Optional`` and ``None`` means "could not
be computed", never zero. The aggregates report how many rows actually
contributed, so a net delta built from half the window cannot be mistaken for a
complete one. A greeks table that quietly rendered 0.00 for an unsolvable strike
would look identical to a genuinely zero-delta strike, and those two mean
opposite things.

Pure and deterministic: no network, no clock beyond an injectable ``now``, and
never raises on bad provider data — it degrades to unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .greeks import greeks_from_market_inputs, normalise_iv, years_to_expiry

__all__ = [
    "StrikeGreeks",
    "ChainSummary",
    "ChainAnalytics",
    "DEFAULT_WINDOW_STRIKES",
    "MAX_WINDOW_STRIKES",
    "analyse_chain",
]

# Strikes either side of ATM to include by default. Enough to show the shape of
# the smile/skew and the OI build-up without paying for the wings.
DEFAULT_WINDOW_STRIKES = 10

# Hard ceiling on the requested window. Bounds the implied-vol solves per request
# so a wide "give me everything" cannot turn into an unbounded amount of work in
# a request handler.
MAX_WINDOW_STRIKES = 100

# Underlying assumed when a caller does not name one. NSE index options are the
# default chain surface in this system; anything else must be asked for
# explicitly rather than silently substituted.
DEFAULT_UNDERLYING = "NIFTY"

_IV_SOURCE_QUOTE = "quote"
_IV_SOURCE_SOLVED = "solved"


@dataclass(frozen=True)
class StrikeGreeks:
    """Greeks and market data for one strike on one side.

    ``iv_source`` records how ``implied_vol`` was obtained, because the two ways
    are not equally trustworthy: ``"quote"`` is the market's own bid/ask IV and
    carries the real skew and liquidity, while ``"solved"`` is volatility
    inverted out of a single mid premium and is only as good as that mid.
    """

    strike: float
    option_type: str  # "CE" or "PE"
    instrument_key: Optional[str] = None
    ltp: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    oi: Optional[int] = None
    change_oi: Optional[int] = None
    implied_vol: Optional[float] = None
    iv_source: Optional[str] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    rho: Optional[float] = None
    moneyness: Optional[str] = None  # "ITM" | "ATM" | "OTM"

    @property
    def has_greeks(self) -> bool:
        return self.delta is not None


@dataclass(frozen=True)
class ChainSummary:
    """Aggregates over the analysed window.

    Every total is accompanied by the count that produced it. Without that, a
    net delta of 120.0 computed from 6 of 40 rows would be indistinguishable from
    one computed from all 40.
    """

    atm_strike: Optional[float] = None
    lower_strike: Optional[float] = None
    upper_strike: Optional[float] = None
    rows_total: int = 0
    rows_with_greeks: int = 0
    call_delta_total: Optional[float] = None
    put_delta_total: Optional[float] = None
    net_delta: Optional[float] = None
    gamma_total: Optional[float] = None
    theta_total: Optional[float] = None
    vega_total: Optional[float] = None
    call_oi: Optional[int] = None
    put_oi: Optional[int] = None
    put_call_oi_ratio: Optional[float] = None

    @property
    def coverage(self) -> Optional[float]:
        """Share of rows that produced greeks, or None if there are no rows."""
        if self.rows_total <= 0:
            return None
        return self.rows_with_greeks / self.rows_total


@dataclass(frozen=True)
class ChainAnalytics:
    underlying: str
    expiry: str
    as_of: str
    spot_price: Optional[float] = None
    strike_interval: Optional[float] = None
    rows: list[StrikeGreeks] = field(default_factory=list)
    summary: ChainSummary = field(default_factory=ChainSummary)
    notes: list[str] = field(default_factory=list)
    window_truncated: bool = False


def _moneyness(spot: float, strike: float, option_type: str) -> str:
    if option_type == "CE":
        return "ITM" if strike < spot else ("OTM" if strike > spot else "ATM")
    return "ITM" if strike > spot else ("OTM" if strike < spot else "ATM")


def _mid_iv(quote: Any) -> Optional[float]:
    """Decimal IV from a chain quote's bid/ask pair.

    The chain provider already normalised these to decimals. Passing them
    through :func:`normalise_iv` again is deliberately harmless: a decimal
    volatility is below the percent cutoff, so normalising is idempotent and a
    chain assembled by any other route stays safe.
    """
    bid_iv = normalise_iv(getattr(quote, "bid_iv", None))
    ask_iv = normalise_iv(getattr(quote, "ask_iv", None))
    if bid_iv is not None and ask_iv is not None:
        # A crossed pair is a bad payload, not a real quote.
        if ask_iv < bid_iv:
            return None
        return (bid_iv + ask_iv) / 2.0
    return bid_iv if bid_iv is not None else ask_iv


def _ltp_of(quote: Any) -> Optional[float]:
    """Last traded price, preferring LTP then the bid/ask mid.

    The bid/ask mid is a real price when there is no trade, which is common in
    illiquid wings, and beats reporting nothing at all. It is never preferred
    over an actual trade.
    """
    for attr in ("ltp", "last"):
        value = getattr(quote, attr, None)
        if value:
            try:
                price = float(value)
            except (TypeError, ValueError):
                continue
            if price > 0.0:
                return price
    bid = getattr(quote, "bid", None)
    ask = getattr(quote, "ask", None)
    try:
        bid_f = float(bid) if bid else 0.0
        ask_f = float(ask) if ask else 0.0
    except (TypeError, ValueError):
        return None
    if bid_f > 0.0 and ask_f > 0.0:
        return (bid_f + ask_f) / 2.0
    return None


def _row_for_quote(
    quote: Any,
    *,
    spot: float,
    expiry: str,
    now: Optional[datetime],
) -> StrikeGreeks:
    """Greeks for one chain quote, degrading to unknown rather than guessing."""
    strike = float(getattr(quote, "strike", 0.0) or 0.0)
    option_type = (getattr(quote, "option_type", None) or "").upper()
    ltp = _ltp_of(quote)
    iv = _mid_iv(quote)
    iv_source = _IV_SOURCE_QUOTE if iv is not None else None

    greeks = greeks_from_market_inputs(
        spot=spot,
        strike=strike,
        expiry=expiry,
        option_type=option_type,
        ltp=ltp,
        quote_iv=iv,
        now=now,
    )
    if greeks is not None and iv is None:
        # Volatility was recovered from the premium, not taken from the quote.
        iv = greeks.implied_vol
        iv_source = _IV_SOURCE_SOLVED

    return StrikeGreeks(
        strike=strike,
        option_type=option_type,
        instrument_key=getattr(quote, "instrument_key", None),
        ltp=ltp,
        bid=getattr(quote, "bid", None),
        ask=getattr(quote, "ask", None),
        oi=getattr(quote, "open_interest", None),
        change_oi=getattr(quote, "change_oi", None),
        implied_vol=iv,
        iv_source=iv_source,
        delta=greeks.delta if greeks else None,
        gamma=greeks.gamma if greeks else None,
        theta=greeks.theta if greeks else None,
        vega=greeks.vega if greeks else None,
        rho=greeks.rho if greeks else None,
        moneyness=_moneyness(spot, strike, option_type) if spot > 0 else None,
    )


def _sum_optional(rows: list[StrikeGreeks], attr: str) -> Optional[float]:
    """Total of *attr* over rows that have it, or None if none do."""
    values = [
        float(getattr(r, attr)) for r in rows if getattr(r, attr) is not None
    ]
    if not values:
        return None
    return sum(values)


def _sum_oi(rows: list[StrikeGreeks]) -> Optional[int]:
    values = [r.oi for r in rows if r.oi is not None]
    if not values:
        return None
    return sum(values)


def analyse_chain(
    chain: Any,
    *,
    window_strikes: int = DEFAULT_WINDOW_STRIKES,
    now: Optional[datetime] = None,
) -> ChainAnalytics:
    """Compute per-strike greeks and window aggregates for an option chain.

    Args:
        chain: an ``OptionsChain`` (anything with ``underlying``, ``expiry``,
            ``spot_price``, ``strikes``, ``call_quotes`` and ``put_quotes``).
        window_strikes: strikes either side of ATM to include. Clamped to
            ``[0, MAX_WINDOW_STRIKES]``; ``0`` means ATM only.
        now: evaluation time, injectable so the result is deterministic in
            tests. Defaults to the current UTC time.

    Returns:
        A :class:`ChainAnalytics`. Never raises: an unusable chain comes back
        with no rows and a note explaining why, so the caller renders "unknown"
        rather than a table of zeros.
    """
    as_of_dt = now or datetime.now(timezone.utc)
    as_of = as_of_dt.isoformat()

    def _empty(notes: list[str]) -> ChainAnalytics:
        return ChainAnalytics(
            underlying=str(getattr(chain, "underlying", "") or ""),
            expiry=str(getattr(chain, "expiry", "") or ""),
            as_of=as_of,
            spot_price=None,
            strike_interval=None,
            rows=[],
            summary=ChainSummary(),
            notes=notes,
        )

    underlying = str(getattr(chain, "underlying", "") or "")
    expiry = str(getattr(chain, "expiry", "") or "")
    if not underlying or not expiry:
        return _empty(["chain is missing an underlying or expiry"])

    try:
        spot = float(getattr(chain, "spot_price", 0.0) or 0.0)
    except (TypeError, ValueError):
        spot = 0.0
    if spot <= 0.0:
        # Without a spot there is no moneyness and no d1/d2, so every row would
        # be unknown. Say so once instead of emitting 200 empty rows.
        return _empty(
            [
                "underlying spot price is unavailable, so no greeks could be "
                "computed for any strike"
            ]
        )

    strikes = sorted({float(s) for s in (getattr(chain, "strikes", None) or []) if s})
    if not strikes:
        strikes = sorted(
            {
                float(s)
                for s in (
                    list((getattr(chain, "call_quotes", None) or {}).keys())
                    + list((getattr(chain, "put_quotes", None) or {}).keys())
                )
                if s
            }
        )
    if not strikes:
        return _empty(["chain contains no strikes"])

    try:
        window = int(window_strikes)
    except (TypeError, ValueError):
        window = DEFAULT_WINDOW_STRIKES
    if window < 0:
        window = 0
    truncated = False
    if window > MAX_WINDOW_STRIKES:
        window = MAX_WINDOW_STRIKES
        truncated = True

    atm = min(strikes, key=lambda s: abs(s - spot))
    lower_idx = max(0, strikes.index(atm) - window)
    upper_idx = min(len(strikes) - 1, strikes.index(atm) + window)
    selected = strikes[lower_idx : upper_idx + 1]

    notes: list[str] = []
    if len(selected) < len(strikes):
        notes.append(
            f"showing {len(selected)} of {len(strikes)} strikes, "
            f"{window} either side of ATM {atm:g}"
        )
    if truncated:
        notes.append(
            f"window clamped to the maximum of {MAX_WINDOW_STRIKES} strikes"
        )

    years = years_to_expiry(expiry, now=as_of_dt)
    if years is None or years <= 0.0:
        notes.append(
            f"expiry {expiry} is not in the future, so all greeks are unknown"
        )

    call_quotes = getattr(chain, "call_quotes", None) or {}
    put_quotes = getattr(chain, "put_quotes", None) or {}

    rows: list[StrikeGreeks] = []
    for strike in selected:
        for side, quotes in (("CE", call_quotes), ("PE", put_quotes)):
            quote = quotes.get(strike)
            if quote is None:
                continue
            rows.append(
                _row_for_quote(quote, spot=spot, expiry=expiry, now=as_of_dt)
            )

    with_greeks = [r for r in rows if r.has_greeks]
    if rows and not with_greeks:
        notes.append(
            "no strike produced greeks: the chain carried neither a usable "
            "implied volatility nor a premium with time value"
        )

    calls = [r for r in rows if r.option_type == "CE"]
    puts = [r for r in rows if r.option_type == "PE"]

    call_delta = _sum_optional(calls, "delta")
    put_delta = _sum_optional(puts, "delta")
    net_delta = None
    if call_delta is not None or put_delta is not None:
        # A missing side contributes zero to a *net* figure, which is arithmetically
        # right but can overstate conviction, so the partial nature is reported.
        net_delta = (call_delta or 0.0) + (put_delta or 0.0)

    call_oi = _sum_oi(calls)
    put_oi = _sum_oi(puts)
    put_call_ratio = None
    if call_oi is not None and put_oi is not None and call_oi > 0:
        put_call_ratio = put_oi / call_oi

    if call_delta is None or put_delta is None:
        notes.append(
            "net delta is based on the incomplete side of the chain; treat it "
            "as a partial figure"
        )

    interval = getattr(chain, "strike_interval", None)
    try:
        interval_f = float(interval) if interval else None
    except (TypeError, ValueError):
        interval_f = None

    return ChainAnalytics(
        underlying=underlying,
        expiry=expiry,
        as_of=as_of,
        spot_price=spot,
        strike_interval=interval_f,
        rows=rows,
        summary=ChainSummary(
            atm_strike=atm,
            lower_strike=selected[0] if selected else None,
            upper_strike=selected[-1] if selected else None,
            rows_total=len(rows),
            rows_with_greeks=len(with_greeks),
            call_delta_total=call_delta,
            put_delta_total=put_delta,
            net_delta=net_delta,
            gamma_total=_sum_optional(rows, "gamma"),
            theta_total=_sum_optional(rows, "theta"),
            vega_total=_sum_optional(rows, "vega"),
            call_oi=call_oi,
            put_oi=put_oi,
            put_call_oi_ratio=put_call_ratio,
        ),
        notes=notes,
        window_truncated=truncated,
    )