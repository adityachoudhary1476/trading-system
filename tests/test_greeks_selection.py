"""Phase 2 — target-delta strike selection (the pure selection engine).

Every chain here is built from the deterministic in-memory provider with a
fixed ``reference_date`` and every call gets an injected ``now``, so the
expected numbers are properties of the synthetic pricing model rather than of
the day the suite happens to run (D5: no clock reads).

The load-bearing invariants are the ones a caller depends on before trusting
the fallback: a selection is always inside the configured band, always
deterministic, and never produced by raising — malformed input yields ``None``
and the legacy path decides (D1, fail open).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from trading_system.autonomous.greeks_policy import evaluate_candidate
from trading_system.autonomous.greeks_selection import (
    StrikeSelection,
    select_by_target_delta,
)
from trading_system.autonomous.options_contract import (
    InMemoryOptionsChainProvider,
    OptionsChain,
)
from trading_system.india.instruments import OptionType

UTC = timezone.utc
SYMBOL = "NSE:SBIN"
SPOT = 100.0
REF_DATE = date(2024, 1, 10)
NOW = datetime(2024, 1, 10, 10, 30, tzinfo=UTC)
TARGET_030 = 0.30
TOL_05 = 0.05


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

def _chain(spot: float = SPOT) -> OptionsChain:
    provider = InMemoryOptionsChainProvider(reference_date=REF_DATE)
    provider.set_spot(SYMBOL, spot)
    chain = provider.get_chain(SYMBOL)
    assert chain is not None
    return chain


def _select(
    chain,
    *,
    target: float = TARGET_030,
    tol: float = TOL_05,
    option_type=OptionType.CE,
    now=NOW,
):
    return select_by_target_delta(
        chain=chain,
        option_type=option_type,
        target_delta=target,
        delta_tolerance=tol,
        now=now,
    )


def _delta_at(
    chain: OptionsChain,
    strike: float,
    option_type: OptionType = OptionType.CE,
    now=NOW,
) -> float:
    """Independent oracle: one strike's delta via the pinned Phase 0 predicate."""
    verdict = evaluate_candidate(
        instrument=SimpleNamespace(
            strike=strike,
            option_type=option_type,
            instrument_key=f"test|{strike}",
        ),
        quote=chain.get_quote(strike, option_type),
        chain=chain,
        now=now,
    )
    assert verdict.available
    assert verdict.greeks is not None
    assert verdict.greeks.delta is not None
    return verdict.greeks.delta


def _distances(
    chain: OptionsChain,
    target: float,
    option_type: OptionType = OptionType.CE,
) -> dict[float, float]:
    return {
        strike: abs(abs(_delta_at(chain, strike, option_type)) - abs(target))
        for strike in chain.strikes
    }


# --------------------------------------------------------------------------- #
# 1. ATM target selects the strike nearest the money
# --------------------------------------------------------------------------- #

def test_atm_target_selects_strike_nearest_the_money() -> None:
    chain = _chain()
    sel = _select(chain, target=0.50, tol=0.10)

    assert isinstance(sel, StrikeSelection)
    nearest = min(chain.strikes, key=lambda s: abs(s - SPOT))
    assert sel.strike == nearest
    assert sel.strike == SPOT
    assert abs(sel.delta - 0.50) < 0.10


# --------------------------------------------------------------------------- #
# 2. OTM target returns the closest in-band strike
# --------------------------------------------------------------------------- #

def test_target_030_returns_closest_in_band_strike() -> None:
    chain = _chain()
    sel = _select(chain, target=TARGET_030, tol=TOL_05)

    assert sel is not None
    assert sel.distance <= TOL_05
    assert sel.strike in chain.strikes

    ordered = sorted(chain.strikes)
    i = ordered.index(sel.strike)
    for j in (i - 1, i + 1):
        if 0 <= j < len(ordered):
            neighbour = abs(abs(_delta_at(chain, ordered[j])) - TARGET_030)
            assert sel.distance < neighbour

    distances = _distances(chain, TARGET_030)
    closest = min(distances, key=lambda s: (distances[s], s))
    assert sel.strike == closest
    assert sel.distance == pytest.approx(distances[closest])


# --------------------------------------------------------------------------- #
# 3. Band edges: a tolerance that just includes vs just excludes a strike
# --------------------------------------------------------------------------- #

def test_band_edge_tolerance_just_includes_then_just_excludes() -> None:
    chain = _chain()
    distances = _distances(chain, 0.50)
    edge = distances[SPOT]
    off_band = min(v for s, v in distances.items() if s != SPOT)

    # The chosen tolerances genuinely straddle the ATM strike's distance, and
    # every other strike sits well outside the wider one.
    assert 0.02 < edge < 0.05
    assert off_band > 0.05

    included = _select(chain, target=0.50, tol=0.05)
    assert included is not None
    assert included.strike == SPOT
    assert included.distance <= 0.05

    excluded = _select(chain, target=0.50, tol=0.02)
    assert excluded is None


# --------------------------------------------------------------------------- #
# 4. Very small tolerance admits nothing
# --------------------------------------------------------------------------- #

def test_no_strike_qualifies_returns_none() -> None:
    chain = _chain()
    smallest = min(_distances(chain, 0.50).values())
    assert smallest > 1e-6

    assert _select(chain, target=0.50, tol=1e-6) is None


# --------------------------------------------------------------------------- #
# 5. now=None → greeks unavailable → fail open
# --------------------------------------------------------------------------- #

def test_now_none_yields_none() -> None:
    assert _select(_chain(), target=TARGET_030, tol=TOL_05, now=None) is None


# --------------------------------------------------------------------------- #
# 6. Put side: negative deltas, matched on magnitude
# --------------------------------------------------------------------------- #

def test_put_side_matches_on_delta_magnitude() -> None:
    chain = _chain()
    sel = _select(chain, target=TARGET_030, tol=TOL_05, option_type=OptionType.PE)

    assert sel is not None
    assert sel.delta < 0
    assert sel.strike in chain.strikes
    assert sel.distance <= TOL_05
    assert sel.distance == pytest.approx(abs(abs(sel.delta) - TARGET_030))


# --------------------------------------------------------------------------- #
# 7. Determinism
# --------------------------------------------------------------------------- #

def test_identical_calls_return_equal_selections() -> None:
    chain = _chain()
    first = _select(chain, target=TARGET_030, tol=TOL_05)
    second = _select(chain, target=TARGET_030, tol=TOL_05)

    assert isinstance(first, StrikeSelection)
    assert isinstance(second, StrikeSelection)
    assert first == second
    assert first == StrikeSelection(
        strike=first.strike,
        delta=first.delta,
        iv_source=first.iv_source,
        distance=first.distance,
    )


def test_negative_target_is_treated_as_a_magnitude() -> None:
    chain = _chain()
    positive = _select(chain, target=TARGET_030, tol=TOL_05)
    negative = _select(chain, target=-TARGET_030, tol=TOL_05)

    assert positive is not None
    assert negative is not None
    assert positive == negative


# --------------------------------------------------------------------------- #
# 8. Malformed input → None, never a raise
# --------------------------------------------------------------------------- #

def test_malformed_input_returns_none_without_raising() -> None:
    chain = _chain()

    assert _select(None) is None
    assert _select(object()) is None
    assert _select(SimpleNamespace()) is None
    assert _select(SimpleNamespace(strikes=[SPOT])) is None
    assert (
        _select(SimpleNamespace(strikes=[SPOT], get_quote=lambda s, o: None)) is None
    )

    empty = OptionsChain(
        underlying=SYMBOL,
        expiry=chain.expiry,
        spot_price=SPOT,
        strike_interval=2.5,
        strikes=[],
    )
    assert _select(empty) is None

    assert _select(chain, option_type="XX") is None
    assert _select(chain, option_type=None) is None
    assert _select(chain, target=None) is None
    assert _select(chain, tol=None) is None


# --------------------------------------------------------------------------- #
# 9. Property: any returned selection is in-band with a real delta
# --------------------------------------------------------------------------- #

def test_property_returned_selection_is_in_band_with_unit_delta() -> None:
    chain = _chain()
    tolerances = (0.01, 0.05, 0.20)
    targets = (0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90)

    checked = 0
    for option_type in (OptionType.CE, OptionType.PE):
        for target in targets:
            for tol in tolerances:
                sel = _select(chain, target=target, tol=tol, option_type=option_type)
                if sel is None:
                    continue
                checked += 1
                assert 0.0 < abs(sel.delta) < 1.0
                assert sel.distance <= tol
                assert sel.distance == pytest.approx(abs(abs(sel.delta) - target))
                assert sel.strike in chain.strikes

    assert checked > 0
