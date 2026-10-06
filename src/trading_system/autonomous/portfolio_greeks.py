"""Portfolio greek aggregation and limit checks for the decision layer.

Phases 0-3 judge one trade at a time; this module answers the book-level
question behind Phase 4 of GREEKS_DECISION_LAYER.md: what net greek exposure
does the whole portfolio carry, and would accepting one more position push a
net figure past its cap. Aggregation and arithmetic only -- no clock, no I/O,
no broker, no market-data fetch -- so identical inputs always produce an
identical answer, and the module is as safe inside a decision path as inside a
test.

Three decisions are load-bearing.

**Unknown is unknown, never zero (D7).** The paper book stores each position's
delta, gamma, theta and vega (``last_<greek>`` with ``entry_<greek>`` as
fallback); an externally supplied ``greeks_by_key`` overrides them when the
caller holds fresher values. A net figure is therefore reported only when
*every* position in the book has a usable value for that metric -- otherwise
it is ``None``, and ``known`` plus ``unknown_keys`` say exactly which positions
the number would have been built from. A book written before the wider greeks
were persisted simply reports gamma/theta/vega as unknown. This mirrors
``chain_analytics.ChainSummary``: a total computed from half the book must
never be indistinguishable from a complete one. The empty book is the
deliberate exception: with no positions there is no unknown exposure, so its
nets are ``0.0``.

**A hostile position degrades, it does not abort.** Every attribute read is
exception-guarded, so an object whose properties raise is skipped rather than
propagated; its key, when one can still be obtained, joins ``unknown_keys``,
and its unreadable exposure keeps the nets ``None`` instead of silently
shrinking the denominator.

**Limits fail open and only refuse growth (D1).** A metric is checked only
when it carries a finite, non-negative cap *and* both the current net and the
candidate's exposure are known; anything else is skipped, never guessed. A
breach requires ``abs(projected) > limit`` *and* a strictly larger absolute
net than today, so an order that reduces exposure -- or flips it to the other
side -- inside the cap passes. The cap exists to refuse growing risk, not to
trap a position that can no longer be reduced.

Breach ``limit_name`` values are spelled exactly as
``GreeksMetrics.record_limit_blocked`` buckets them (``"max_net_delta"``), so
a returned breach can be handed straight to the metrics layer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Optional

__all__ = [
    "PositionGreeks",
    "PortfolioGreeks",
    "PortfolioGreeksLimits",
    "PortfolioLimitBreach",
    "aggregate_positions",
    "check_portfolio_limits",
]

_LIMIT_METRICS = ("delta", "gamma", "theta", "vega")


@dataclass(frozen=True)
class PositionGreeks:
    """Greeks and signed exposure for one position in the book.

    ``quantity`` is the signed contract count exactly as the book reports it
    (positive long, negative short) and ``contract_size`` the underlying units
    per contract, so each ``<greek>_exposure`` is the position's contribution
    to the net in the greek's own unit (delta per 1.0 of underlying, gamma per
    1.0 squared, theta per calendar day, vega per IV percentage point). A
    ``None`` greek means "not known"; its exposure mirrors that as ``None``
    and is never folded into a total as zero.
    """

    instrument_key: str
    quantity: float
    contract_size: float = 1.0
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None

    @property
    def delta_exposure(self) -> Optional[float]:
        return _exposure(self.quantity, self.contract_size, self.delta)

    @property
    def gamma_exposure(self) -> Optional[float]:
        return _exposure(self.quantity, self.contract_size, self.gamma)

    @property
    def theta_exposure(self) -> Optional[float]:
        return _exposure(self.quantity, self.contract_size, self.theta)

    @property
    def vega_exposure(self) -> Optional[float]:
        return _exposure(self.quantity, self.contract_size, self.vega)


@dataclass(frozen=True)
class PortfolioGreeks:
    """Net greek exposure across a book, with the coverage behind each net.

    Every ``None`` net is explained by ``known`` (positions with a usable
    delta) and ``unknown_keys`` (sorted keys without one), so a caller can
    tell "no exposure" apart from "exposure not measurable". ``positions``
    preserves input order so two snapshots can be diffed position by position.
    """

    net_delta: Optional[float]
    net_gamma: Optional[float]
    net_theta: Optional[float]
    net_vega: Optional[float]
    known: int
    unknown_keys: tuple[str, ...]
    positions: tuple[PositionGreeks, ...]


@dataclass(frozen=True)
class PortfolioGreeksLimits:
    """Caps on the absolute value of each net greek.

    ``None`` means the metric is not capped at all; ``0.0`` is a real cap of
    "no net exposure", not a missing one. Non-finite and negative values are
    read as unset by the checker rather than trusted, since a cap that cannot
    be evaluated must not silently refuse or permit anything.
    """

    max_net_delta: Optional[float] = None
    max_net_gamma: Optional[float] = None
    max_net_theta: Optional[float] = None
    max_net_vega: Optional[float] = None


@dataclass(frozen=True)
class PortfolioLimitBreach:
    """The first cap a candidate would increase past, with its arithmetic.

    ``limit_name`` is the bucket spelling ``GreeksMetrics.record_limit_blocked``
    expects, ``metric`` names the net figure that breached, and ``current`` /
    ``projected`` / ``limit`` are the three numbers the verdict came from, so a
    caller can log the refusal without recomputing it.
    """

    limit_name: str
    metric: str
    current: Optional[float]
    projected: float
    limit: float


def _exposure(
    quantity: float, contract_size: float, greek: Optional[float]
) -> Optional[float]:
    if greek is None:
        return None
    return quantity * contract_size * greek


def _safe_attr(obj: Any, name: str) -> Any:
    """getattr that cannot raise: a hostile object must degrade, not abort."""
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _finite_float(value: Any) -> Optional[float]:
    """Finite float interpretation of ``value``, or None if there is not one.

    Missing, unparseable and non-finite values all collapse to None, because
    the layer's contract is "unknown", never a number that would compare
    wrongly against a cap.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _instrument_key(obj: Any) -> Optional[str]:
    """Stable key for a position, or None when no key can be obtained.

    The options contract id identifies the exact instrument; the symbol is the
    fallback for equity rows that never had one. Both reads are guarded and
    the conversion is stringified, so an unreadable key yields None rather
    than an exception or a key type nothing else can compare against.
    """
    try:
        raw = _safe_attr(obj, "options_contract_id")
        if raw is None:
            raw = _safe_attr(obj, "symbol")
        if raw is None:
            return None
        text = str(raw).strip()
    except Exception:
        return None
    return text or None


def _quantity_of(obj: Any) -> Optional[float]:
    """Signed contract count, tolerating the ``quantity`` spelling, else None.

    A position whose quantity cannot be read as a finite number is unreadable
    rather than flat: defaulting it to 0.0 would drop real exposure from the
    book while reporting a perfectly healthy net.
    """
    raw = _safe_attr(obj, "qty")
    if raw is None:
        raw = _safe_attr(obj, "quantity")
    return _finite_float(raw)


def _contract_size_of(obj: Any) -> float:
    """Underlying units per contract, falling back to 1.0.

    A missing, unparseable, non-finite or non-positive size cannot be trusted,
    so it falls back to the unit multiplier rather than zeroing or flipping
    the position's exposure.
    """
    size = _finite_float(_safe_attr(obj, "contract_size"))
    if size is None or size <= 0.0:
        return 1.0
    return size


def _stored_greek(obj: Any, name: str) -> Optional[float]:
    """Most recent usable ``name`` greek, falling back to the entry anchor.

    ``last_<name>`` is preferred because it reflects the position as it is
    held; ``entry_<name>`` is the anchor captured at the fill and is the honest
    second choice when no later reading exists. A non-finite reading counts as
    unusable, so a NaN greek lands in ``unknown_keys`` instead of poisoning a
    net. Gamma, theta and vega use the same lookup as delta; a book written
    before they were persisted simply reports them unknown.
    """
    value = _finite_float(_safe_attr(obj, f"last_{name}"))
    if value is not None:
        return value
    return _finite_float(_safe_attr(obj, f"entry_{name}"))


def _supplied_greeks(greeks_by_key: Any, key: str) -> Optional[Any]:
    """Mapping entry for ``key``, or None when nothing usable is supplied.

    Any failure to index -- absent key, hostile mapping, wrong container type
    -- reads as "not supplied", so the stored-delta path still runs instead of
    a lookup error aborting aggregation.
    """
    if greeks_by_key is None:
        return None
    try:
        return greeks_by_key[key]
    except Exception:
        return None


def _greek_fields(supplied: Any) -> tuple:
    """The four gated greeks from a supplied object, non-finite as None."""
    return (
        _finite_float(_safe_attr(supplied, "delta")),
        _finite_float(_safe_attr(supplied, "gamma")),
        _finite_float(_safe_attr(supplied, "theta")),
        _finite_float(_safe_attr(supplied, "vega")),
    )


def _position_greeks(
    obj: Any, key: str, greeks_by_key: Any
) -> Optional[PositionGreeks]:
    """Build one position's greek row, or None if it is unreadable.

    When ``greeks_by_key`` supplies an object for this key it is authoritative
    for all four greeks, replacing the stored values: the caller that supplied
    fresher greeks knows more than the book does, and mixing the two sources
    across one row would produce exposure from no single point in time.
    """
    quantity = _quantity_of(obj)
    if quantity is None:
        return None
    supplied = _supplied_greeks(greeks_by_key, key)
    if supplied is None:
        delta = _stored_greek(obj, "delta")
        gamma = _stored_greek(obj, "gamma")
        theta = _stored_greek(obj, "theta")
        vega = _stored_greek(obj, "vega")
    else:
        delta, gamma, theta, vega = _greek_fields(supplied)
    return PositionGreeks(
        instrument_key=key,
        quantity=quantity,
        contract_size=_contract_size_of(obj),
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
    )


def _safe_iter(items: Any) -> Iterator[Any]:
    """Yield every item of ``items``, stopping cleanly if iteration fails.

    A partially consumable iterable still contributes what it produced; the
    items it never yielded are unknowable, and unknowable is reported as
    whatever the caller can still see rather than invented.
    """
    try:
        iterator = iter(items)
    except Exception:
        return
    while True:
        try:
            yield next(iterator)
        except StopIteration:
            return
        except Exception:
            return


def _net(values: list, *, incomplete: bool) -> Optional[float]:
    """Total of per-position exposures, or None if any position is unknown.

    ``incomplete`` is set when a position could not be read at all; such a
    position is unknown for every metric, so no net survives it. The empty
    book falls through to 0.0: with nothing held there is no unknown exposure.
    """
    if incomplete:
        return None
    total = 0.0
    for value in values:
        if value is None:
            return None
        total += value
    return total


def aggregate_positions(
    positions: Any,
    *,
    greeks_by_key: Optional[Mapping[str, Any]] = None,
) -> PortfolioGreeks:
    """Aggregate a book of positions into net greek exposure and coverage.

    Args:
        positions: any iterable of position-like objects. Each is read
            defensively: ``options_contract_id`` (falling back to ``symbol``)
            for the key, ``qty`` (falling back to ``quantity``) as the signed
            contract count, ``contract_size`` defaulting to 1.0, and each greek
            from ``last_<greek>`` falling back to ``entry_<greek>``. Gamma,
            theta and vega are read this way in addition to delta; a position
            written before they were persisted reports them unknown.

        greeks_by_key: optional mapping of instrument key to a Greeks-like
            object exposing ``delta``/``gamma``/``theta``/``vega``. When a key
            is present its greeks are authoritative for that position and
            replace the stored delta.

    Returns:
        A :class:`PortfolioGreeks` in input order. Never raises and never
        reads a clock or any I/O: an unreadable position is skipped, its key
        (if obtainable) joins ``unknown_keys``, and its exposure keeps every
        net ``None`` rather than counting as zero (D7). An empty book reports
        ``0.0`` nets with no unknowns.
    """
    built: list[PositionGreeks] = []
    unknown_keys: list[str] = []
    unreadable = False

    for obj in _safe_iter(positions):
        key = _instrument_key(obj)
        entry: Optional[PositionGreeks] = None
        if key is not None:
            try:
                entry = _position_greeks(obj, key, greeks_by_key)
            except Exception:
                entry = None
        if entry is None:
            unreadable = True
            if key is not None:
                unknown_keys.append(key)
            continue
        built.append(entry)

    for row in built:
        if row.delta is None:
            unknown_keys.append(row.instrument_key)

    return PortfolioGreeks(
        net_delta=_net([row.delta_exposure for row in built], incomplete=unreadable),
        net_gamma=_net([row.gamma_exposure for row in built], incomplete=unreadable),
        net_theta=_net([row.theta_exposure for row in built], incomplete=unreadable),
        net_vega=_net([row.vega_exposure for row in built], incomplete=unreadable),
        known=sum(1 for row in built if row.delta is not None),
        unknown_keys=tuple(sorted(set(unknown_keys))),
        positions=tuple(built),
    )


def _usable_limit(value: Any) -> Optional[float]:
    """A cap that can be enforced, or None if it cannot be trusted.

    Zero is a real cap ("no net exposure"); negative and non-finite values are
    nonsense for a magnitude threshold and are treated as unset, so a typo'd
    cap can never silently refuse every order or permit every one.
    """
    limit = _finite_float(value)
    if limit is None or limit < 0.0:
        return None
    return limit


def check_portfolio_limits(
    *,
    current: PortfolioGreeks,
    candidate: PositionGreeks,
    limits: PortfolioGreeksLimits,
) -> Optional[PortfolioLimitBreach]:
    """The first cap the candidate would push past, or None to allow it.

    Metrics are evaluated in the fixed order delta, gamma, theta, vega. A
    metric is checked only when it has a usable cap and both sides of
    ``projected = current.net_<metric> + candidate.<metric>_exposure`` are
    known; anything else is skipped, never guessed, so an unmeasurable metric
    fails open (D1) instead of blocking a trade the layer cannot judge.

    A metric breaches only when the candidate *increases* the absolute net:
    ``abs(projected) > limit`` and ``abs(projected) > abs(current)``. An order
    that reduces the net, or flips it to the other side, inside the cap passes
    even when the book is already over the cap -- refusing those would make
    the exposure impossible to shrink.
    """
    for name in _LIMIT_METRICS:
        limit = _usable_limit(_safe_attr(limits, f"max_net_{name}"))
        if limit is None:
            continue
        net = _finite_float(_safe_attr(current, f"net_{name}"))
        exposure = _finite_float(_safe_attr(candidate, f"{name}_exposure"))
        if net is None or exposure is None:
            continue
        projected = net + exposure
        if abs(projected) > limit and abs(projected) > abs(net):
            return PortfolioLimitBreach(
                limit_name=f"max_net_{name}",
                metric=f"net_{name}",
                current=net,
                projected=projected,
                limit=limit,
            )
    return None
