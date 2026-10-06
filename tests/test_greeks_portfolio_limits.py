"""Tests for Phase 4 portfolio greek aggregation and limit checks.

Two guarantees are load-bearing and pinned here. First (D7): unknown exposure
is never folded into a net as zero -- a book holding one unreadable position
reports ``None``, not a number built from the rest, and ``known``/``unknown_keys``
say exactly which positions a number came from. Second (D1): limits fail open
on anything unmeasurable while refusing only *increases* in absolute exposure,
so reducing a position, or flipping it within the cap, always passes.

Pure in-memory arithmetic: fixtures are ``SimpleNamespace`` positions and the
real ``Greeks`` dataclass (plus one real paper-book ``Position`` to pin the
field names the aggregator reads). No clock, no I/O, no market data.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from trading_system.autonomous.greeks import Greeks
from trading_system.autonomous.greeks_metrics import GreeksMetrics
from trading_system.autonomous.portfolio_greeks import (
    PortfolioGreeks,
    PortfolioGreeksLimits,
    PortfolioLimitBreach,
    PositionGreeks,
    aggregate_positions,
    check_portfolio_limits,
)
from trading_system.paper_trading import Position


def pos(key="NSE:TEST", qty=1.0, *, contract_size=1.0, **fields):
    payload = {
        "options_contract_id": key,
        "qty": qty,
        "contract_size": contract_size,
    }
    payload.update(fields)
    return SimpleNamespace(**payload)


def greeks(delta=0.5, gamma=0.02, theta=-1.0, vega=2.0):
    return Greeks(
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        rho=0.05,
        price=100.0,
        implied_vol=0.18,
    )


def portfolio(
    net_delta=0.0,
    net_gamma=0.0,
    net_theta=0.0,
    net_vega=0.0,
    *,
    known=0,
    unknown_keys=(),
    positions=(),
):
    return PortfolioGreeks(
        net_delta=net_delta,
        net_gamma=net_gamma,
        net_theta=net_theta,
        net_vega=net_vega,
        known=known,
        unknown_keys=tuple(unknown_keys),
        positions=tuple(positions),
    )


def limits(delta=None, gamma=None, theta=None, vega=None):
    return PortfolioGreeksLimits(
        max_net_delta=delta,
        max_net_gamma=gamma,
        max_net_theta=theta,
        max_net_vega=vega,
    )


def candidate(*, delta=None, gamma=None, theta=None, vega=None, qty=1.0,
              contract_size=1.0):
    return PositionGreeks(
        instrument_key="candidate",
        quantity=qty,
        contract_size=contract_size,
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
    )


def check(current, cand, caps):
    return check_portfolio_limits(
        current=current, candidate=cand, limits=caps
    )


class TestEmptyBook:
    def test_empty_book_reports_zero_nets_and_no_unknowns(self):
        result = aggregate_positions([])
        assert result.net_delta == 0.0
        assert result.net_gamma == 0.0
        assert result.net_theta == 0.0
        assert result.net_vega == 0.0
        assert result.known == 0
        assert result.unknown_keys == ()
        assert result.positions == ()

    def test_empty_book_nets_are_zero_not_none(self):
        result = aggregate_positions(())
        for value in (
            result.net_delta,
            result.net_gamma,
            result.net_theta,
            result.net_vega,
        ):
            assert value is not None
            assert value == 0.0

    def test_degenerate_inputs_degrade_to_an_empty_book(self):
        empty_generator = aggregate_positions(item for item in [])
        uniterable = aggregate_positions(None)
        assert empty_generator == aggregate_positions([])
        assert uniterable == aggregate_positions([])


class TestNetting:
    def test_long_and_short_delta_net_to_zero(self):
        result = aggregate_positions(
            [pos("A", 10.0, last_delta=0.5), pos("B", -10.0, last_delta=0.5)]
        )
        assert result.net_delta == 0.0
        assert result.known == 2

    def test_two_long_positions_sum(self):
        result = aggregate_positions(
            [pos("A", 10.0, last_delta=0.25), pos("B", 20.0, last_delta=0.5)]
        )
        assert result.net_delta == 12.5

    def test_short_quantity_flips_exposure_sign_both_ways(self):
        short_long_delta = aggregate_positions([pos("A", -10.0, last_delta=0.5)])
        short_short_delta = aggregate_positions([pos("A", -10.0, last_delta=-0.5)])
        assert short_long_delta.net_delta == -5.0
        assert short_short_delta.net_delta == 5.0

    def test_positions_preserve_input_order_and_fields(self):
        result = aggregate_positions(
            [
                pos("Z", 3.0, contract_size=50.0, last_delta=0.4),
                pos("A", 1.0, last_delta=0.2),
                pos("M", 1.0, last_delta=0.3),
            ]
        )
        assert [row.instrument_key for row in result.positions] == ["Z", "A", "M"]
        assert result.positions[0].quantity == 3.0
        assert result.positions[0].contract_size == 50.0
        assert result.positions[0].delta_exposure == pytest.approx(60.0)


class TestUnknownExposure:
    def test_missing_delta_yields_none_net_and_unknown_key(self):
        result = aggregate_positions([pos("A", 10.0)])
        assert result.net_delta is None
        assert result.known == 0
        assert result.unknown_keys == ("A",)
        assert len(result.positions) == 1

    def test_one_unknown_delta_makes_the_whole_net_unknown(self):
        result = aggregate_positions(
            [pos("A", 10.0, last_delta=0.5), pos("B", 5.0)]
        )
        assert result.net_delta is None
        assert result.known == 1
        assert result.unknown_keys == ("B",)

    def test_gamma_theta_vega_stay_unknown_without_greeks_by_key(self):
        result = aggregate_positions([pos("A", 10.0, last_delta=0.5)])
        assert result.net_delta == 5.0
        assert result.net_gamma is None
        assert result.net_theta is None
        assert result.net_vega is None

    def test_unknown_keys_are_sorted_and_deduplicated(self):
        result = aggregate_positions([pos("Z"), pos("SAME"), pos("A"), pos("SAME")])
        assert result.unknown_keys == ("A", "SAME", "Z")

    def test_delta_prefers_last_reading_then_entry_anchor(self):
        both = aggregate_positions(
            [pos("A", 10.0, entry_delta=0.1, last_delta=0.5)]
        )
        anchor_only = aggregate_positions([pos("A", 10.0, entry_delta=0.25)])
        assert both.net_delta == 5.0
        assert anchor_only.net_delta == 2.5

    def test_non_finite_delta_counts_as_unknown(self):
        result = aggregate_positions(
            [pos("A", 10.0, last_delta=float("nan")), pos("B", 10.0, last_delta=0.5)]
        )
        assert result.net_delta is None
        assert result.unknown_keys == ("A",)
        assert result.known == 1

    def test_book_of_only_unreadable_positions_reports_none_not_zero(self):
        result = aggregate_positions([pos("A", "many")])
        assert result.net_delta is None
        assert result.positions == ()
        assert result.known == 0
        assert result.unknown_keys == ("A",)


class TestGreeksByKey:
    def test_supplied_greeks_fill_every_metric(self):
        supplied = greeks(delta=0.6, gamma=0.02, theta=-1.5, vega=3.5)
        result = aggregate_positions(
            [pos("A", 10.0, last_delta=0.1)], greeks_by_key={"A": supplied}
        )
        assert result.net_delta == pytest.approx(6.0)
        assert result.net_gamma == pytest.approx(0.2)
        assert result.net_theta == pytest.approx(-15.0)
        assert result.net_vega == pytest.approx(35.0)
        assert result.known == 1
        assert result.unknown_keys == ()

    def test_supplied_delta_takes_precedence_over_stored_delta(self):
        supplied = greeks(delta=0.2, gamma=0.0)
        result = aggregate_positions(
            [pos("A", 10.0, entry_delta=0.9, last_delta=0.9)],
            greeks_by_key={"A": supplied},
        )
        assert result.net_delta == pytest.approx(2.0)
        assert result.net_gamma == 0.0

    def test_partial_mapping_leaves_uncovered_positions_unknown(self):
        supplied = greeks(delta=0.4, gamma=0.01)
        result = aggregate_positions(
            [pos("A", 10.0, last_delta=0.5), pos("B", 10.0, last_delta=0.25)],
            greeks_by_key={"A": supplied},
        )
        assert result.net_delta == pytest.approx(6.5)
        assert result.net_gamma is None
        assert result.unknown_keys == ()

    def test_supplied_unknown_delta_overrides_stored_delta(self):
        supplied = SimpleNamespace(delta=None, gamma=0.01, theta=-1.0, vega=2.0)
        result = aggregate_positions(
            [pos("A", 10.0, last_delta=0.7)], greeks_by_key={"A": supplied}
        )
        assert result.net_delta is None
        assert result.net_gamma == pytest.approx(0.1)
        assert result.unknown_keys == ("A",)

    def test_non_finite_supplied_greek_is_unknown(self):
        supplied = SimpleNamespace(
            delta=0.5, gamma=float("inf"), theta=-1.0, vega=2.0
        )
        result = aggregate_positions(
            [pos("A", 10.0, last_delta=0.7)], greeks_by_key={"A": supplied}
        )
        assert result.net_delta == pytest.approx(5.0)
        assert result.net_gamma is None

    def test_unusable_mapping_falls_back_to_stored_delta(self):
        class HostileMapping:
            def __getitem__(self, key):
                raise RuntimeError("no lookups today")

        explicit_none = aggregate_positions(
            [pos("A", 10.0, last_delta=0.5)], greeks_by_key={"A": None}
        )
        hostile = aggregate_positions(
            [pos("A", 10.0, last_delta=0.5)], greeks_by_key=HostileMapping()
        )
        assert explicit_none.net_delta == 5.0
        assert hostile.net_delta == 5.0


class TestContractSize:
    def test_contract_size_scales_exposure(self):
        result = aggregate_positions(
            [pos("A", 2.0, contract_size=100.0, last_delta=0.5)]
        )
        assert result.net_delta == pytest.approx(100.0)

    def test_contract_size_defaults_to_one_when_absent(self):
        position = SimpleNamespace(options_contract_id="A", qty=2.0, last_delta=0.5)
        result = aggregate_positions([position])
        assert result.positions[0].contract_size == 1.0
        assert result.net_delta == 1.0

    @pytest.mark.parametrize(
        "bad_size", [None, "big", 0.0, -5.0, float("nan")]
    )
    def test_unusable_contract_size_falls_back_to_one(self, bad_size):
        position = SimpleNamespace(
            options_contract_id="A",
            qty=2.0,
            contract_size=bad_size,
            last_delta=0.5,
        )
        result = aggregate_positions([position])
        assert result.positions[0].contract_size == 1.0
        assert result.net_delta == 1.0


class TestStoredWiderGreeks:
    """The book persists gamma/theta/vega per position, so aggregation reads
    them the same way it reads delta: ``last_<greek>`` first, then the
    ``entry_<greek>`` anchor, each independent so one missing metric does not
    hide the others (D7).
    """

    def test_stored_gamma_theta_vega_are_aggregated(self):
        result = aggregate_positions(
            [pos("A", 2.0, last_delta=0.5, last_gamma=0.02,
                 last_theta=-3.0, last_vega=1.5)]
        )
        assert result.net_delta == pytest.approx(1.0)
        assert result.net_gamma == pytest.approx(0.04)
        assert result.net_theta == pytest.approx(-6.0)
        assert result.net_vega == pytest.approx(3.0)

    def test_each_wider_greek_falls_back_to_its_entry_anchor(self):
        result = aggregate_positions(
            [pos("A", 2.0, entry_gamma=0.01, entry_theta=-2.0, entry_vega=1.0)]
        )
        assert result.net_gamma == pytest.approx(0.02)
        assert result.net_theta == pytest.approx(-4.0)
        assert result.net_vega == pytest.approx(2.0)

    def test_last_reading_wins_over_the_anchor_per_metric(self):
        result = aggregate_positions(
            [pos("A", 1.0, entry_gamma=0.01, last_gamma=0.05)]
        )
        assert result.net_gamma == pytest.approx(0.05)

    def test_one_missing_wider_greek_keeps_only_that_net_unknown(self):
        result = aggregate_positions(
            [pos("A", 2.0, last_delta=0.5, last_gamma=0.02, last_theta=-3.0)]
        )
        assert result.net_delta == pytest.approx(1.0)
        assert result.net_gamma == pytest.approx(0.04)
        assert result.net_theta == pytest.approx(-6.0)
        assert result.net_vega is None

    def test_supplied_greeks_override_stored_wider_greeks(self):
        supplied = greeks(delta=0.1, gamma=0.5, theta=-1.0, vega=9.0)
        result = aggregate_positions(
            [pos("A", 10.0, last_delta=0.5, last_gamma=0.02,
                 last_theta=-3.0, last_vega=1.5)],
            greeks_by_key={"A": supplied},
        )
        assert result.net_delta == pytest.approx(1.0)
        assert result.net_gamma == pytest.approx(5.0)
        assert result.net_theta == pytest.approx(-10.0)
        assert result.net_vega == pytest.approx(90.0)

    def test_real_paper_position_exposes_stored_wider_greeks(self):
        position = Position(
            symbol="NSE:SBIN",
            qty=2.0,
            last_delta=0.5,
            last_gamma=0.02,
            last_theta=-3.0,
            last_vega=1.5,
            contract_size=1,
        )
        result = aggregate_positions([position])
        assert result.net_delta == pytest.approx(1.0)
        assert result.net_gamma == pytest.approx(0.04)
        assert result.net_theta == pytest.approx(-6.0)
        assert result.net_vega == pytest.approx(3.0)


class TestHostilePositions:
    def test_raising_key_attribute_falls_back_to_symbol(self):
        class PartiallyHostile:
            symbol = "NSE:FALLBACK"
            qty = 4.0
            last_delta = 0.5

            @property
            def options_contract_id(self):
                raise RuntimeError("boom")

        result = aggregate_positions([PartiallyHostile()])
        assert result.net_delta == 2.0
        assert result.unknown_keys == ()

    def test_unreadable_quantity_lands_key_in_unknown_keys(self):
        class RaisingQty:
            options_contract_id = "NSE:BAD"
            last_delta = 0.5

            @property
            def qty(self):
                raise RuntimeError("boom")

        raising = aggregate_positions([RaisingQty()])
        non_numeric = aggregate_positions([pos("A", "ten")])
        for result in (raising, non_numeric):
            assert result.net_delta is None
            assert result.positions == ()
            assert result.known == 0
        assert raising.unknown_keys == ("NSE:BAD",)
        assert non_numeric.unknown_keys == ("A",)

    def test_fully_unreadable_position_is_skipped_without_a_key(self):
        class FullyHostile:
            @property
            def options_contract_id(self):
                raise RuntimeError("boom")

            @property
            def symbol(self):
                raise RuntimeError("boom")

            @property
            def qty(self):
                raise RuntimeError("boom")

        result = aggregate_positions([FullyHostile()])
        assert result.positions == ()
        assert result.unknown_keys == ()
        assert result.net_delta is None
        assert result.known == 0

    def test_good_and_hostile_positions_never_sum_to_a_number(self):
        class Hostile:
            options_contract_id = "BAD"

            @property
            def qty(self):
                raise RuntimeError("boom")

        result = aggregate_positions(
            [pos("A", 10.0, last_delta=0.5), Hostile()]
        )
        assert result.net_delta is None
        assert result.known == 1
        assert result.unknown_keys == ("BAD",)
        assert len(result.positions) == 1

    def test_iterable_that_raises_midway_keeps_what_it_yielded(self):
        def generator():
            yield pos("A", 10.0, last_delta=0.5)
            raise RuntimeError("iterator blew up")

        result = aggregate_positions(generator())
        assert result.net_delta == 5.0
        assert result.known == 1
        assert result.positions[0].instrument_key == "A"


class TestDeterminism:
    def test_repeated_aggregation_of_the_same_book_is_identical(self):
        items = [pos("A", 10.0, last_delta=0.5), pos("B", 5.0)]
        assert aggregate_positions(items) == aggregate_positions(items)

    def test_iterator_and_list_inputs_agree(self):
        items = [pos("A", 10.0, last_delta=0.5), pos("B", -3.0, last_delta=0.25)]
        assert aggregate_positions(iter(items)) == aggregate_positions(items)

    def test_real_paper_position_uses_stored_delta(self):
        position = Position(
            symbol="NSE:SBIN",
            qty=100.0,
            entry_delta=0.4,
            last_delta=0.42,
            contract_size=1,
        )
        result = aggregate_positions([position])
        assert result.known == 1
        assert result.unknown_keys == ()
        assert result.net_delta == pytest.approx(42.0)

    def test_real_paper_position_without_greeks_is_unknown(self):
        position = Position(symbol="NSE:SBIN", qty=100.0)
        result = aggregate_positions([position])
        assert result.net_delta is None
        assert result.unknown_keys == ("NSE:SBIN",)
        assert result.known == 0


class TestLimitBlocking:
    def test_delta_increase_beyond_cap_blocks(self):
        breach = check(
            portfolio(net_delta=0.75), candidate(delta=0.5), limits(delta=1.0)
        )
        assert breach == PortfolioLimitBreach(
            limit_name="max_net_delta",
            metric="net_delta",
            current=0.75,
            projected=1.25,
            limit=1.0,
        )

    def test_gamma_blocked_independently(self):
        breach = check(
            portfolio(net_gamma=0.25), candidate(gamma=1.0), limits(gamma=0.5)
        )
        assert breach is not None
        assert breach.limit_name == "max_net_gamma"
        assert breach.metric == "net_gamma"
        assert breach.projected == 1.25

    def test_theta_blocked_for_long_premium(self):
        breach = check(
            portfolio(net_theta=-60.0),
            candidate(theta=-0.5, qty=100.0),
            limits(theta=100.0),
        )
        assert breach is not None
        assert breach.limit_name == "max_net_theta"
        assert breach.current == -60.0
        assert breach.projected == -110.0
        assert breach.limit == 100.0

    def test_vega_blocked_with_exact_breach_fields(self):
        breach = check(
            portfolio(net_vega=30.0),
            candidate(vega=2.5, qty=10.0),
            limits(vega=50.0),
        )
        assert breach == PortfolioLimitBreach(
            limit_name="max_net_vega",
            metric="net_vega",
            current=30.0,
            projected=55.0,
            limit=50.0,
        )

    def test_breaches_resolve_in_delta_gamma_theta_vega_order(self):
        breach = check(
            portfolio(
                net_delta=0.9, net_gamma=0.9, net_theta=-90.0, net_vega=90.0
            ),
            candidate(delta=0.5, gamma=0.5, theta=-50.0, vega=50.0),
            limits(delta=1.0, gamma=1.0, theta=100.0, vega=100.0),
        )
        assert breach is not None
        assert breach.limit_name == "max_net_delta"

    def test_only_capped_metrics_are_evaluated(self):
        breach = check(
            portfolio(net_delta=0.1, net_vega=9999.0),
            candidate(delta=0.05, vega=9999.0),
            limits(delta=1.0),
        )
        assert breach is None

    def test_projected_equal_to_the_cap_passes_exactly(self):
        at_cap = check(
            portfolio(net_delta=0.5), candidate(delta=0.5), limits(delta=1.0)
        )
        increase_inside_cap = check(
            portfolio(net_delta=0.25), candidate(delta=0.5), limits(delta=1.0)
        )
        assert at_cap is None
        assert increase_inside_cap is None

    def test_flat_candidate_on_an_over_cap_book_passes(self):
        breach = check(
            portfolio(net_delta=1.5), candidate(delta=0.0), limits(delta=1.0)
        )
        assert breach is None


class TestReduceAndFlip:
    def test_reduction_inside_the_cap_passes(self):
        breach = check(
            portfolio(net_delta=0.75), candidate(delta=-0.5), limits(delta=1.0)
        )
        assert breach is None

    def test_flip_to_the_other_side_inside_the_cap_passes(self):
        breach = check(
            portfolio(net_delta=0.75), candidate(delta=-1.0), limits(delta=1.0)
        )
        assert breach is None

    def test_flip_beyond_the_cap_blocks(self):
        breach = check(
            portfolio(net_delta=0.75), candidate(delta=-2.5), limits(delta=1.0)
        )
        assert breach is not None
        assert breach.projected == -1.75

    def test_over_cap_reduction_passes_even_when_still_over(self):
        breach = check(
            portfolio(net_delta=1.5), candidate(delta=-0.25), limits(delta=1.0)
        )
        assert breach is None

    def test_over_cap_increase_blocks(self):
        breach = check(
            portfolio(net_delta=1.5), candidate(delta=0.25), limits(delta=1.0)
        )
        assert breach is not None
        assert breach.projected == 1.75

    def test_negative_net_grows_only_when_it_grows(self):
        shrinking = check(
            portfolio(net_delta=-0.75), candidate(delta=0.5), limits(delta=1.0)
        )
        growing = check(
            portfolio(net_delta=-0.75), candidate(delta=-0.5), limits(delta=1.0)
        )
        assert shrinking is None
        assert growing is not None
        assert growing.projected == -1.25


class TestFailOpen:
    def test_no_limits_never_blocks(self):
        breach = check(
            portfolio(net_delta=99.0, net_vega=99.0),
            candidate(delta=99.0, vega=99.0),
            limits(),
        )
        assert breach is None

    @pytest.mark.parametrize(
        "unusable",
        [float("nan"), float("inf"), float("-inf"), -1.0, -0.001],
    )
    def test_non_finite_and_negative_limits_are_unset(self, unusable):
        breach = check(
            portfolio(net_delta=5.0),
            candidate(delta=5.0),
            limits(delta=unusable),
        )
        assert breach is None

    def test_zero_limit_is_a_real_cap(self):
        breach = check(
            portfolio(net_delta=0.0),
            candidate(delta=0.01),
            limits(delta=0.0),
        )
        flat = check(
            portfolio(net_delta=0.0), candidate(delta=0.0), limits(delta=0.0)
        )
        assert breach is not None
        assert breach.limit == 0.0
        assert flat is None

    def test_unknown_side_of_the_arithmetic_skips_the_metric(self):
        unknown_current = check(
            portfolio(net_delta=None), candidate(delta=5.0), limits(delta=1.0)
        )
        unknown_candidate = check(
            portfolio(net_delta=5.0), candidate(delta=None), limits(delta=1.0)
        )
        assert unknown_current is None
        assert unknown_candidate is None

    def test_unknown_metric_never_blocks_on_a_cap(self):
        breach = check(
            portfolio(net_gamma=None),
            candidate(gamma=50.0),
            limits(gamma=0.1),
        )
        assert breach is None


class TestMetricsIntegration:
    def test_breach_limit_name_buckets_in_greeks_metrics(self):
        breach = check(
            portfolio(net_delta=0.75), candidate(delta=0.5), limits(delta=1.0)
        )
        assert breach is not None
        metrics = GreeksMetrics()
        metrics.record_limit_blocked(breach.limit_name)
        assert metrics.snapshot()["greek_limit_blocked"] == {"max_net_delta": 1}
