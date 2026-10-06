"""Tests for Phase 3 risk-based sizing (decision D1 + the unit trap).

The sizing function must be safe to call on anything: it fails OPEN to ``None``
(the caller's legacy fixed size) rather than raising or refusing a trade, and
every cap it reads is sanitised so a typo can only shrink the position. The
other load-bearing guarantee is the unit conversion -- greeks price is per
option *unit* while risk is per *contract* -- pinned here with
``contract_multiplier=100`` because getting it wrong is a 100x sizing error
outside India.

Pure in-memory arithmetic: no I/O, no clock, no controller. Fake verdicts are
plain ``SimpleNamespace`` objects; only ``available`` and ``greeks.price`` are
consulted, which is exactly the contract the function pins.
"""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from trading_system.autonomous.greeks_sizing import (
    BOUND_BY_MAX_CONTRACTS,
    BOUND_BY_NOTIONAL,
    BOUND_BY_RISK_BUDGET,
    SizingResult,
    size_for_risk_budget,
)

BOUND_LABELS = (BOUND_BY_RISK_BUDGET, BOUND_BY_NOTIONAL, BOUND_BY_MAX_CONTRACTS)


def fake_verdict(price=10.0, *, available=True, greeks=...):
    """Tiny stand-in for GreeksVerdict: only availability and price matter."""
    if greeks is ...:
        greeks = SimpleNamespace(price=price)
    return SimpleNamespace(available=available, greeks=greeks)


def size(
    *,
    verdict=...,
    budget=10_000.0,
    notional=10_000.0,
    maxc=1_000.0,
    mult=1,
    price=10.0,
):
    if verdict is ...:
        verdict = fake_verdict(price)
    return size_for_risk_budget(
        verdict=verdict,
        risk_budget=budget,
        notional_cap=notional,
        max_contracts=maxc,
        contract_multiplier=mult,
    )


class TestBudgetRespected:
    def test_budget_binds_and_quantity_stays_within_it(self):
        result = size(price=10.0, budget=95.0, notional=10_000.0, maxc=100.0)
        assert result == SizingResult(
            quantity=9,
            bound_by=BOUND_BY_RISK_BUDGET,
            risk_per_contract=10.0,
        )
        assert result.quantity * result.risk_per_contract <= 95.0

    def test_one_more_contract_would_break_the_budget(self):
        result = size(price=10.0, budget=95.0, notional=10_000.0, maxc=100.0)
        assert (result.quantity + 1) * result.risk_per_contract > 95.0

    def test_risk_per_contract_is_returned_on_success(self):
        result = size(price=13.5, budget=1_000.0)
        assert result.risk_per_contract == 13.5
        assert result.quantity == 74


class TestBoundByReachable:
    def test_max_contracts_is_the_binding_clamp(self):
        result = size(price=10.0, budget=10_000.0, notional=10_000.0, maxc=7.0)
        assert result.quantity == 7
        assert result.bound_by == BOUND_BY_MAX_CONTRACTS

    def test_notional_cap_is_the_binding_clamp(self):
        result = size(price=10.0, budget=10_000.0, notional=45.0, maxc=50.0)
        assert result.quantity == 4
        assert result.bound_by == BOUND_BY_NOTIONAL

    def test_risk_budget_is_the_binding_clamp(self):
        result = size(price=10.0, budget=35.0, notional=10_000.0, maxc=50.0)
        assert result.quantity == 3
        assert result.bound_by == BOUND_BY_RISK_BUDGET

    def test_tie_between_all_three_reports_max_contracts(self):
        result = size(price=10.0, budget=70.0, notional=70.0, maxc=7.0)
        assert result.quantity == 7
        assert result.bound_by == BOUND_BY_MAX_CONTRACTS

    def test_tie_between_budget_and_notional_reports_notional(self):
        result = size(price=10.0, budget=70.0, notional=70.0, maxc=50.0)
        assert result.quantity == 7
        assert result.bound_by == BOUND_BY_NOTIONAL

    def test_every_label_is_one_of_the_pinned_constants(self):
        seen = {
            size(price=10.0, budget=35.0).bound_by,
            size(price=10.0, notional=45.0).bound_by,
            size(price=10.0, maxc=7.0).bound_by,
        }
        assert seen == set(BOUND_LABELS)


class TestMonotonicity:
    @pytest.mark.parametrize(
        "notional,maxc",
        [
            (10_000.0, 1_000.0),
            (200.0, 12.0),
            (57.0, 1_000.0),
        ],
    )
    def test_larger_budget_never_yields_fewer_contracts(self, notional, maxc):
        quantities = [
            size(price=8.0, budget=b, notional=notional, maxc=maxc).quantity
            for b in (0.0, 1.0, 7.0, 8.0, 9.0, 16.0, 40.0, 64.0, 96.0, 10_000.0)
        ]
        assert quantities == sorted(quantities)

    def test_monotonicity_holds_across_the_notional_clamp_too(self):
        quantities = [
            size(price=7.5, budget=b, notional=1_000.0, maxc=1_000.0).quantity
            for b in (0.0, 7.0, 15.0, 23.0, 30.0, 75.0, 150.0, 1_000.0)
        ]
        assert quantities == sorted(quantities)
        assert quantities[-1] == 133


class TestIntegerFlooring:
    def test_budget_of_2_9_contracts_yields_2(self):
        result = size(price=10.0, budget=29.0, notional=10_000.0, maxc=100.0)
        assert result.quantity == 2
        assert result.bound_by == BOUND_BY_RISK_BUDGET

    def test_never_rounds_up_past_the_notional_cap(self):
        result = size(price=10.0, budget=1_000_000.0, notional=29.0, maxc=100.0)
        assert result.quantity == 2
        assert result.bound_by == BOUND_BY_NOTIONAL
        assert result.quantity * result.risk_per_contract <= 29.0

    def test_never_rounds_up_past_the_contract_cap(self):
        result = size(price=10.0, budget=1_000_000.0, notional=1_000_000.0, maxc=2.9)
        assert result.quantity == 2
        assert result.bound_by == BOUND_BY_MAX_CONTRACTS

    @pytest.mark.parametrize("budget", [1.0, 15.0, 63.0, 80.0, 999.0])
    @pytest.mark.parametrize("notional", [31.0, 64.0, 500.0])
    def test_floored_quantity_never_exceeds_either_cap(self, budget, notional):
        result = size(price=8.0, budget=budget, notional=notional, maxc=5.0)
        assert result.quantity * result.risk_per_contract <= notional
        assert result.quantity <= 5
        assert result.quantity >= 0


class TestUnitConversion:
    def test_multiplier_100_reports_price_times_100(self):
        result = size(price=2.5, mult=100, budget=5_000.0)
        assert result.risk_per_contract == 250.0
        assert result.risk_per_contract == 2.5 * 100

    def test_multiplier_100_sizes_one_hundredth_of_multiplier_1(self):
        per_unit = size(price=2.5, mult=1, budget=5_000.0, notional=1e9, maxc=1e6)
        per_contract = size(price=2.5, mult=100, budget=5_000.0, notional=1e9, maxc=1e6)
        assert per_unit.quantity == 2_000
        assert per_contract.quantity == 20
        assert per_contract.quantity == per_unit.quantity // 100

    def test_default_multiplier_treats_price_as_per_contract(self):
        result = size(price=4.0, budget=100.0, notional=100.0, maxc=100.0)
        assert result.risk_per_contract == 4.0
        assert result.quantity == 25


class TestFailOpen:
    @pytest.mark.parametrize(
        "verdict",
        [
            None,
            fake_verdict(available=False),
            fake_verdict(greeks=None),
            fake_verdict(price=None),
            fake_verdict(price=0.0),
            fake_verdict(price=-5.0),
            fake_verdict(price=math.nan),
            fake_verdict(price=math.inf),
            fake_verdict(price=-math.inf),
        ],
    )
    def test_unusable_input_returns_none(self, verdict):
        assert (
            size_for_risk_budget(
                verdict=verdict,
                risk_budget=1_000.0,
                notional_cap=1_000.0,
                max_contracts=10.0,
            )
            is None
        )

    def test_verdict_without_price_attribute_fails_open(self):
        assert size(verdict=fake_verdict(greeks=SimpleNamespace())) is None

    def test_hostile_verdict_never_raises(self):
        class Hostile:
            @property
            def available(self):
                raise RuntimeError("boom")

        assert size(verdict=Hostile()) is None

    def test_garbage_cap_never_raises_and_never_enlarges(self):
        result = size(price=10.0, notional="not a number")
        assert result.quantity == 0
        assert result.bound_by == BOUND_BY_NOTIONAL


class TestSanitisedCaps:
    @pytest.mark.parametrize("bad", [math.nan, -1.0, -math.inf, math.inf])
    def test_bad_notional_cap_yields_zero_not_a_huge_number(self, bad):
        result = size(price=10.0, budget=10_000.0, notional=bad, maxc=100.0)
        assert result.quantity == 0
        assert result.bound_by == BOUND_BY_NOTIONAL

    @pytest.mark.parametrize("bad", [math.nan, -1.0, -math.inf, math.inf])
    def test_bad_risk_budget_yields_zero(self, bad):
        result = size(price=10.0, budget=bad, notional=10_000.0, maxc=100.0)
        assert result.quantity == 0
        assert result.bound_by == BOUND_BY_RISK_BUDGET

    @pytest.mark.parametrize("bad", [math.nan, -1.0, -math.inf, math.inf])
    def test_bad_max_contracts_yields_zero(self, bad):
        result = size(price=10.0, budget=10_000.0, notional=10_000.0, maxc=bad)
        assert result.quantity == 0
        assert result.bound_by == BOUND_BY_MAX_CONTRACTS

    def test_sanitised_caps_never_exceed_the_original_intent(self):
        result = size(price=10.0, budget=55.0, notional=35.0, maxc=100.0)
        assert result.quantity == 3
        assert result.quantity * result.risk_per_contract <= 35.0


class TestDeterminism:
    def test_identical_inputs_produce_identical_results(self):
        first = size(price=10.0, budget=95.0, notional=45.0, maxc=7.0)
        second = size(price=10.0, budget=95.0, notional=45.0, maxc=7.0)
        assert first == second
        assert first is not second

    def test_result_is_an_immutable_sizing_result(self):
        result = size(price=10.0, budget=95.0)
        assert isinstance(result, SizingResult)
        with pytest.raises(Exception):
            result.quantity = 0
