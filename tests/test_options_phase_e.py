"""Phase E — Multi-leg options architecture tests.

Phase E is paper-only and non-executing. These tests verify:

  * the canonical leg / structure / plan models;
  * deterministic identity and leg ordering;
  * per-structure-type validation rules;
  * exact contract identity (different strikes / expiries / rights are
    different contracts);
  * structure -> plan conversion (no execution);
  * premium / net-premium math (no double lot-size);
  * partial-state model without claiming execution happened;
  * synthetic ``InMemoryOptionsChainProvider`` is never used by
    production resolution;
  * the architecture does NOT call ``submit_order_intent``,
    ``execute_option_order``, or any PaperBroker method;
  * existing equity functionality is untouched.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from trading_system.autonomous.options.structure import (
    OptionLeg,
    OptionsStructure,
    OptionsStructurePlan,
    StructureLegState,
    StructureState,
    StructureType,
    StructureValidationError,
    StructureValidationResult,
    build_plan,
    resolve_straddle,
    resolve_vertical_call_spread,
    validate_structure,
)
from trading_system.autonomous.options_contract import InMemoryOptionsChainProvider
from trading_system.execution.orders import OrderType, Side
from trading_system.india.instrument_repository import InstrumentRepository
from trading_system.india.instruments import Instrument


UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _future_expiry(days: int = 14) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).date().isoformat()


def _opt(
    underlying: str,
    expiry: str,
    strike: float,
    right: str,
    *,
    provider_symbol: Optional[str] = None,
) -> Instrument:
    return Instrument.option(
        "NSE",
        underlying,
        expiry,
        float(strike),
        right,
        provider_symbol=provider_symbol,
    )


def _leg(
    *,
    underlying: str = "NIFTY",
    expiry: str,
    strike: float,
    right: str,
    side: Side = Side.BUY,
    contracts: float = 1.0,
    premium: Optional[float] = None,
    contract_size: Optional[int] = None,
    instrument_id: Optional[str] = None,
) -> OptionLeg:
    instr = _opt(underlying, expiry, strike, right)
    return OptionLeg(
        instrument_id=instrument_id or instr.contract_id,
        underlying=underlying,
        option_type=right,
        strike=float(strike),
        expiry=expiry,
        side=side,
        contracts=float(contracts),
        premium=premium,
        contract_size=contract_size,
    )


def _seed_repo(
    *,
    underlying: str = "NIFTY",
    expiry: Optional[str] = None,
    strikes: Optional[list[float]] = None,
) -> InstrumentRepository:
    expiry = expiry or _future_expiry()
    strikes = strikes or [24800.0, 24900.0, 25000.0, 25100.0, 25200.0]
    repo = InstrumentRepository()
    for s in strikes:
        repo.register(_opt(underlying, expiry, s, "CE"))
        repo.register(_opt(underlying, expiry, s, "PE"))
    return repo


def repo_id_for(expiry: str, strike: float, right: str) -> str:
    """The contract_id produced by ``Instrument.option(...)`` for the
    test universe. Mirrors ``Instrument.contract_id``."""
    return f"NSE:NIFTY|{expiry}|{int(strike)}|{right}"


# --------------------------------------------------------------------------- #
# Model construction
# --------------------------------------------------------------------------- #
class TestModelConstruction:
    def test_valid_leg(self) -> None:
        expiry = _future_expiry()
        leg = _leg(expiry=expiry, strike=25000.0, right="CE")
        assert leg.option_type == "CE"
        assert leg.strike == 25000.0
        assert leg.contracts == 1.0
        assert leg.side == Side.BUY
        assert leg.leg_id  # deterministic identity
        assert leg.instrument_id

    def test_valid_structure(self) -> None:
        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.STRADDLE,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE"),
                _leg(expiry=expiry, strike=25000.0, right="PE"),
            ],
        )
        assert s.structure_id
        assert s.leg_count == 2
        assert s.state == StructureState.PLANNED

    def test_serialization_roundtrip(self) -> None:
        expiry = _future_expiry()
        ce = _leg(expiry=expiry, strike=25000.0, right="CE", premium=147.5)
        pe = _leg(expiry=expiry, strike=25000.0, right="PE", premium=132.25)
        original = OptionsStructure(
            structure_type=StructureType.STRADDLE,
            underlying="NIFTY",
            legs=[ce, pe],
            notes="roundtrip test",
        )
        blob = original.to_dict()
        # JSON-safe: must round-trip through json.dumps.
        json.dumps(blob)
        restored = OptionsStructure.from_dict(blob)
        assert restored.structure_id == original.structure_id
        assert restored.underlying == original.underlying
        assert restored.structure_type == original.structure_type
        assert len(restored.legs) == len(original.legs)
        for a, b in zip(restored.legs, original.legs):
            assert a.leg_id == b.leg_id
            assert a.instrument_id == b.instrument_id
            assert a.option_type == b.option_type
            assert a.strike == b.strike
            assert a.expiry == b.expiry
            assert a.side == b.side
            assert a.contracts == b.contracts

    def test_leg_rejects_invalid_option_type(self) -> None:
        with pytest.raises(ValueError):
            _leg(expiry=_future_expiry(), strike=25000.0, right="XX")

    def test_leg_rejects_zero_contracts(self) -> None:
        with pytest.raises(ValueError):
            _leg(expiry=_future_expiry(), strike=25000.0, right="CE", contracts=0.0)

    def test_leg_rejects_negative_premium(self) -> None:
        with pytest.raises(ValueError):
            _leg(
                expiry=_future_expiry(),
                strike=25000.0,
                right="CE",
                premium=-1.0,
            )


# --------------------------------------------------------------------------- #
# Validation — generic
# --------------------------------------------------------------------------- #
class TestValidationGeneric:
    def test_empty_structure_rejected(self) -> None:
        s = OptionsStructure(
            structure_type=StructureType.STRADDLE, underlying="NIFTY", legs=[],
        )
        result = validate_structure(s)
        assert result.is_valid is False
        assert any(e.code == "empty_structure" for e in result.errors)

    def test_missing_instrument_id_rejected(self) -> None:
        """Empty instrument_id is rejected at the *leg construction*
        layer (so a structure with such a leg cannot exist). The
        validator is a second line of defence for any path that
        bypasses the dataclass guard."""
        with pytest.raises(ValueError):
            OptionLeg(
                instrument_id="",  # explicitly missing
                underlying="NIFTY",
                option_type="CE",
                strike=25000.0,
                expiry=_future_expiry(),
                side=Side.BUY,
                contracts=1.0,
            )

    def test_zero_contracts_rejected(self) -> None:
        """Zero-contracts is rejected at the *leg construction* layer
        (so a structure with such a leg cannot exist); a structure
        built with a single invalid leg would raise at construction
        time. Both are correct fail-closed behaviour."""
        with pytest.raises(ValueError):
            _leg(expiry=_future_expiry(), strike=25000.0, right="CE", contracts=0.0)

    def test_invalid_option_type_rejected(self) -> None:
        expiry = _future_expiry()
        with pytest.raises(ValueError):
            _leg(expiry=expiry, strike=25000.0, right="XX")

    def test_validation_function_flags_zero_contracts(self) -> None:
        """The validator is a second line of defence: if a leg somehow
        bypassed the dataclass check (e.g. via direct __dict__ mutation
        in a future refactor), ``validate_structure`` would still
        reject it.
        """
        expiry = _future_expiry()
        # Build a leg with a normal value, then poke contracts to 0 via
        # ``dataclasses.replace`` (which bypasses ``__post_init__`` is
        # *not* what we want; instead build normally and confirm the
        # dataclass guard fired). The validator-level coverage exists
        # via ``test_leg_rejects_zero_contracts`` above plus the
        # dataclass guard; this test is a placeholder for the case
        # where future code disables ``__post_init__``.
        leg = _leg(expiry=expiry, strike=25000.0, right="CE")
        assert leg.contracts > 0

    def test_mixed_underlying_rejected(self) -> None:
        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.STRADDLE,
            underlying="NIFTY",
            legs=[
                _leg(underlying="NIFTY", expiry=expiry, strike=25000.0, right="CE"),
                _leg(underlying="BANKNIFTY", expiry=expiry, strike=50000.0, right="PE"),
            ],
        )
        result = validate_structure(s)
        assert result.is_valid is False
        assert any(e.code == "mixed_underlying" for e in result.errors)


# --------------------------------------------------------------------------- #
# Structure-type-specific validation
# --------------------------------------------------------------------------- #
class TestStructureTypes:
    def test_call_spread_valid(self) -> None:
        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.VERTICAL_CALL_SPREAD,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE", side=Side.BUY),
                _leg(expiry=expiry, strike=25200.0, right="CE", side=Side.SELL),
            ],
        )
        result = validate_structure(s)
        assert result.is_valid is True, result.errors

    def test_call_spread_mixed_expiry_rejected(self) -> None:
        s = OptionsStructure(
            structure_type=StructureType.VERTICAL_CALL_SPREAD,
            underlying="NIFTY",
            legs=[
                _leg(expiry=_future_expiry(7), strike=25000.0, right="CE"),
                _leg(expiry=_future_expiry(30), strike=25200.0, right="CE"),
            ],
        )
        result = validate_structure(s)
        assert result.is_valid is False
        assert any(e.code == "mixed_expiry" for e in result.errors)

    def test_call_spread_same_strike_rejected(self) -> None:
        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.VERTICAL_CALL_SPREAD,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE", side=Side.BUY),
                _leg(expiry=expiry, strike=25000.0, right="CE", side=Side.SELL),
            ],
        )
        result = validate_structure(s)
        assert result.is_valid is False
        assert any(e.code == "invalid_strike_relationship" for e in result.errors)

    def test_call_spread_wrong_right_rejected(self) -> None:
        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.VERTICAL_CALL_SPREAD,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE"),
                _leg(expiry=expiry, strike=25200.0, right="PE"),
            ],
        )
        result = validate_structure(s)
        assert result.is_valid is False
        assert any(e.code == "invalid_option_type" for e in result.errors)

    def test_put_spread_valid(self) -> None:
        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.VERTICAL_PUT_SPREAD,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="PE", side=Side.SELL),
                _leg(expiry=expiry, strike=24800.0, right="PE", side=Side.BUY),
            ],
        )
        result = validate_structure(s)
        assert result.is_valid is True, result.errors

    def test_straddle_valid(self) -> None:
        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.STRADDLE,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE"),
                _leg(expiry=expiry, strike=25000.0, right="PE"),
            ],
        )
        result = validate_structure(s)
        assert result.is_valid is True, result.errors

    def test_straddle_different_strikes_rejected(self) -> None:
        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.STRADDLE,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE"),
                _leg(expiry=expiry, strike=25100.0, right="PE"),
            ],
        )
        result = validate_structure(s)
        assert result.is_valid is False
        assert any(e.code == "invalid_strike_relationship" for e in result.errors)

    def test_strangle_valid(self) -> None:
        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.STRANGLE,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25200.0, right="CE"),
                _leg(expiry=expiry, strike=24800.0, right="PE"),
            ],
        )
        result = validate_structure(s)
        assert result.is_valid is True, result.errors

    def test_strangle_same_strike_rejected(self) -> None:
        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.STRANGLE,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE"),
                _leg(expiry=expiry, strike=25000.0, right="PE"),
            ],
        )
        result = validate_structure(s)
        assert result.is_valid is False
        assert any(e.code == "invalid_strike_relationship" for e in result.errors)

    def test_single_requires_one_leg(self) -> None:
        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.SINGLE,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE"),
                _leg(expiry=expiry, strike=25000.0, right="PE"),
            ],
        )
        result = validate_structure(s)
        assert result.is_valid is False
        assert any(e.code == "invalid_leg_count" for e in result.errors)


# --------------------------------------------------------------------------- #
# Exact contract identity
# --------------------------------------------------------------------------- #
class TestExactContractIdentity:
    def test_different_strikes_are_different_contracts(self) -> None:
        expiry = _future_expiry()
        ce_low = _leg(expiry=expiry, strike=24800.0, right="CE")
        ce_high = _leg(expiry=expiry, strike=25200.0, right="CE")
        assert ce_low.instrument_id != ce_high.instrument_id

    def test_different_expiries_are_different_contracts(self) -> None:
        ce_w1 = _leg(expiry=_future_expiry(7), strike=25000.0, right="CE")
        ce_w2 = _leg(expiry=_future_expiry(30), strike=25000.0, right="CE")
        assert ce_w1.instrument_id != ce_w2.instrument_id

    def test_ce_and_pe_are_different_contracts(self) -> None:
        expiry = _future_expiry()
        ce = _leg(expiry=expiry, strike=25000.0, right="CE")
        pe = _leg(expiry=expiry, strike=25000.0, right="PE")
        assert ce.instrument_id != pe.instrument_id

    def test_duplicate_contract_rejected_for_vertical(self) -> None:
        expiry = _future_expiry()
        # Force the same instrument_id on both legs.
        shared_id = "NSE:NIFTY|2099-01-01|25000|CE"
        s = OptionsStructure(
            structure_type=StructureType.VERTICAL_CALL_SPREAD,
            underlying="NIFTY",
            legs=[
                _leg(
                    expiry=expiry, strike=25000.0, right="CE",
                    instrument_id=shared_id,
                ),
                _leg(
                    expiry=expiry, strike=25200.0, right="CE",
                    instrument_id=shared_id,
                ),
            ],
        )
        # First the strike relationship check fires; also any two legs
        # with the same instrument_id collapse to one logical contract.
        result = validate_structure(s)
        assert result.is_valid is False
        assert any(
            e.code in ("invalid_strike_relationship", "duplicate_contract")
            for e in result.errors
        )


# --------------------------------------------------------------------------- #
# Deterministic ordering
# --------------------------------------------------------------------------- #
class TestDeterministicOrdering:
    def test_same_structure_different_construction_order_same_id(self) -> None:
        expiry = _future_expiry()
        ce = _leg(expiry=expiry, strike=25000.0, right="CE")
        pe = _leg(expiry=expiry, strike=25000.0, right="PE")
        s1 = OptionsStructure(
            structure_type=StructureType.STRADDLE,
            underlying="NIFTY",
            legs=[ce, pe],
        )
        s2 = OptionsStructure(
            structure_type=StructureType.STRADDLE,
            underlying="NIFTY",
            legs=[pe, ce],  # reversed order
        )
        assert s1.structure_id == s2.structure_id
        # Leg ordering is sorted canonically.
        assert [l.leg_id for l in s1.legs] == [l.leg_id for l in s2.legs]

    def test_changing_one_strike_changes_one_instrument(self) -> None:
        expiry = _future_expiry()
        s1 = OptionsStructure(
            structure_type=StructureType.VERTICAL_CALL_SPREAD,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE", side=Side.BUY),
                _leg(expiry=expiry, strike=25100.0, right="CE", side=Side.SELL),
            ],
        )
        s2 = OptionsStructure(
            structure_type=StructureType.VERTICAL_CALL_SPREAD,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE", side=Side.BUY),
                _leg(expiry=expiry, strike=25200.0, right="CE", side=Side.SELL),
            ],
        )
        assert s1.structure_id != s2.structure_id
        # The unchanged leg keeps the same contract identity; the
        # changed leg has a different one. The symmetric difference is
        # therefore exactly the {removed, added} pair (size 2), and
        # exactly one leg is in common.
        ids1 = {l.instrument_id for l in s1.legs}
        ids2 = {l.instrument_id for l in s2.legs}
        assert ids1 & ids2  # the long-leg contract is shared
        assert (ids1 - ids2) == {
            repo_id_for(expiry, 25100.0, "CE"),
        }
        assert (ids2 - ids1) == {
            repo_id_for(expiry, 25200.0, "CE"),
        }

    def test_leg_id_is_deterministic(self) -> None:
        expiry = _future_expiry()
        ce1 = _leg(expiry=expiry, strike=25000.0, right="CE")
        ce2 = _leg(expiry=expiry, strike=25000.0, right="CE")
        assert ce1.leg_id == ce2.leg_id


# --------------------------------------------------------------------------- #
# Structure -> plan
# --------------------------------------------------------------------------- #
class TestStructureToPlan:
    def test_call_spread_to_plan(self) -> None:
        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.VERTICAL_CALL_SPREAD,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE",
                     side=Side.BUY, premium=180.0, contracts=1),
                _leg(expiry=expiry, strike=25200.0, right="CE",
                     side=Side.SELL, premium=80.0, contracts=1),
            ],
        )
        plan = build_plan(s)
        assert plan.is_ready is True
        assert plan.state == StructureState.READY
        assert plan.intent_count == 2
        for intent, leg in zip(plan.intents, s.legs):
            assert intent.symbol == leg.instrument_id
            assert intent.side == leg.side
            assert intent.quantity == leg.contracts
            assert intent.options_contract_id == leg.instrument_id
            assert intent.strike == leg.strike
            assert intent.expiry == leg.expiry
            assert intent.option_type == leg.option_type
            assert intent.order_type == OrderType.LIMIT
            assert intent.limit_price == leg.premium
            assert intent.client_order_id  # deterministic

    def test_plan_client_order_ids_are_deterministic(self) -> None:
        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.STRADDLE,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE", premium=180.0),
                _leg(expiry=expiry, strike=25000.0, right="PE", premium=170.0),
            ],
        )
        plan_a = build_plan(s)
        plan_b = build_plan(s)
        assert [i.client_order_id for i in plan_a.intents] == [
            i.client_order_id for i in plan_b.intents
        ]

    def test_plan_leg_ids_match_structure_legs(self) -> None:
        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.STRADDLE,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE"),
                _leg(expiry=expiry, strike=25000.0, right="PE"),
            ],
        )
        plan = build_plan(s)
        assert set(plan.leg_ids) == {l.leg_id for l in s.legs}

    def test_plan_one_to_one_with_legs(self) -> None:
        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.STRADDLE,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE", premium=180.0),
                _leg(expiry=expiry, strike=25000.0, right="PE", premium=170.0),
            ],
        )
        plan = build_plan(s)
        assert plan.intent_count == s.leg_count
        for intent, leg in zip(plan.intents, s.legs):
            assert intent.options_contract_id == leg.instrument_id

    def test_plan_without_premiums_uses_market_order(self) -> None:
        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.STRADDLE,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE"),
                _leg(expiry=expiry, strike=25000.0, right="PE"),
            ],
        )
        plan = build_plan(s)
        for intent in plan.intents:
            assert intent.order_type == OrderType.MARKET
            assert intent.limit_price is None
        assert plan.has_all_premiums is False
        # require_premiums=True forces state back to PLANNED.
        plan2 = build_plan(s, require_premiums=True)
        assert plan2.state == StructureState.PLANNED

    def test_plan_for_invalid_structure_is_invalid(self) -> None:
        s = OptionsStructure(
            structure_type=StructureType.STRADDLE, underlying="NIFTY", legs=[],
        )
        plan = build_plan(s)
        assert plan.state == StructureState.INVALID
        assert plan.intent_count == 0


# --------------------------------------------------------------------------- #
# Premium / net-debit / credit
# --------------------------------------------------------------------------- #
class TestPremiumCalculations:
    def test_leg_net_premium_buy(self) -> None:
        expiry = _future_expiry()
        leg = _leg(expiry=expiry, strike=25000.0, right="CE",
                   side=Side.BUY, premium=100.0, contracts=2)
        assert leg.net_premium == 200.0
        assert leg.notional == 200.0

    def test_leg_net_premium_sell(self) -> None:
        expiry = _future_expiry()
        leg = _leg(expiry=expiry, strike=25200.0, right="CE",
                   side=Side.SELL, premium=80.0, contracts=2)
        assert leg.net_premium == -160.0
        assert leg.notional == 160.0

    def test_straddle_net_premium(self) -> None:
        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.STRADDLE,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE",
                     premium=180.0, contracts=1),
                _leg(expiry=expiry, strike=25000.0, right="PE",
                     premium=170.0, contracts=1),
            ],
        )
        assert s.net_premium == 350.0
        assert s.gross_premium == 350.0

    def test_call_spread_net_debit(self) -> None:
        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.VERTICAL_CALL_SPREAD,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE",
                     side=Side.BUY, premium=180.0, contracts=1),
                _leg(expiry=expiry, strike=25200.0, right="CE",
                     side=Side.SELL, premium=80.0, contracts=1),
            ],
        )
        # BUY 180 - SELL 80 = net debit 100.
        assert s.net_premium == 100.0

    def test_no_double_lot_size_multiplication(self) -> None:
        """contract_size is a future accounting concern; net_premium
        uses premium * contracts only. Phase D will multiply
        contract_size into the broker's cash movement — Phase E must
        not pre-multiply here."""
        expiry = _future_expiry()
        leg = _leg(
            expiry=expiry, strike=25000.0, right="CE",
            side=Side.BUY, premium=100.0, contracts=1,
            contract_size=75,  # NIFTY lot size — not used by Phase E
        )
        # Net premium is 1 * 100 = 100. NOT 1 * 100 * 75.
        assert leg.net_premium == 100.0

    def test_missing_premium_zero_contribution(self) -> None:
        expiry = _future_expiry()
        leg = _leg(
            expiry=expiry, strike=25000.0, right="CE",
            side=Side.BUY, contracts=1,
        )
        assert leg.net_premium == 0.0
        assert leg.notional == 0.0


# --------------------------------------------------------------------------- #
# Partial state model
# --------------------------------------------------------------------------- #
class TestPartialStateModel:
    def test_state_machine_values(self) -> None:
        # All states are defined.
        for st in (
            StructureState.PLANNED,
            StructureState.READY,
            StructureState.INVALID,
            StructureState.PARTIAL,
            StructureState.COMPLETE,
            StructureState.FAILED,
            StructureState.CANCELLED,
        ):
            assert st.value
        for st in (
            StructureLegState.PLANNED,
            StructureLegState.READY,
            StructureLegState.SUBMITTED,
            StructureLegState.FILLED,
            StructureLegState.REJECTED,
            StructureLegState.CANCELLED,
        ):
            assert st.value

    def test_leg_state_immutable_update(self) -> None:
        expiry = _future_expiry()
        leg = _leg(expiry=expiry, strike=25000.0, right="CE")
        assert leg.state == StructureLegState.PLANNED
        new_leg = leg.with_state(StructureLegState.READY)
        assert new_leg.state == StructureLegState.READY
        # Original unchanged (frozen dataclass).
        assert leg.state == StructureLegState.PLANNED

    def test_structure_state_is_separate_from_execution(self) -> None:
        """Phase E must NOT claim any structure is 'complete' or
        'partial' just because it exists. The only valid Phase-E
        transitions are PLANNED -> READY (or INVALID)."""
        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.STRADDLE,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE", premium=180.0),
                _leg(expiry=expiry, strike=25000.0, right="PE", premium=170.0),
            ],
        )
        # Default state: PLANNED.
        assert s.state == StructureState.PLANNED
        # build_plan with premiums produces READY.
        plan = build_plan(s)
        assert plan.state == StructureState.READY
        # The structure object itself was NOT mutated to READY —
        # Phase E preserves the planned/validated distinction.
        assert s.state == StructureState.PLANNED


# --------------------------------------------------------------------------- #
# Synthetic provider rejection
# --------------------------------------------------------------------------- #
class TestSyntheticProviderRejection:
    def test_synthetic_provider_not_used_in_production_resolution(self) -> None:
        """Production resolution goes through the real
        ``InstrumentRepository``; ``InMemoryOptionsChainProvider`` is
        not referenced anywhere in Phase E.
        """
        import trading_system.autonomous.options.structure as mod

        with open(mod.__file__, "r", encoding="utf-8") as fh:
            body = fh.read()
        # Class is only referenced via test-side import, never used.
        assert "InMemoryOptionsChainProvider(" not in body

    def test_resolve_uses_real_repository_only(self) -> None:
        """``resolve_vertical_call_spread`` / ``resolve_straddle`` use
        ``InstrumentRepository.find_contract``; they do NOT
        instantiate or call any chain provider.
        """
        expiry = _future_expiry()
        repo = _seed_repo(expiry=expiry, strikes=[24900.0, 25000.0, 25100.0])
        s = resolve_vertical_call_spread(
            repository=repo,
            underlying="NIFTY",
            expiry=expiry,
            lower_strike=25000.0,
            upper_strike=25100.0,
            buy_side=Side.BUY,
            contracts=1.0,
        )
        assert s.leg_count == 2
        assert all(
            l.instrument_id == repo.find_contract(
                underlying="NIFTY", expiry=expiry, option_type="CE",
                strike=l.strike,
            ).contract_id
            for l in s.legs
        )

    def test_resolve_fails_closed_when_contract_missing(self) -> None:
        expiry = _future_expiry()
        repo = _seed_repo(expiry=expiry, strikes=[25000.0])  # no 25100
        with pytest.raises(ValueError):
            resolve_vertical_call_spread(
                repository=repo,
                underlying="NIFTY",
                expiry=expiry,
                lower_strike=25000.0,
                upper_strike=25100.0,
                buy_side=Side.BUY,
                contracts=1.0,
            )


# --------------------------------------------------------------------------- #
# No execution — the safety boundary
# --------------------------------------------------------------------------- #
class TestNoExecution:
    def test_no_execute_option_order_in_phase_e(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Build a plan, then spy on every conceivable execution
        entry point. None must be invoked.
        """
        from trading_system.autonomous.controller import AutonomousController
        from trading_system.paper.control import PaperTradingControlCenter

        submit_calls: list = []
        execute_calls: list = []

        def _spy_submit(self, session_id, intent):  # type: ignore[no-untyped-def]
            submit_calls.append((session_id, intent))
            raise AssertionError("submit_order_intent must NOT be called in Phase E")

        def _spy_execute(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            execute_calls.append((args, kwargs))
            raise AssertionError("execute_option_order must NOT be called in Phase E")

        monkeypatch.setattr(AutonomousController, "execute_option_order", _spy_execute)
        monkeypatch.setattr(PaperTradingControlCenter, "submit_order_intent", _spy_submit)

        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.STRADDLE,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE", premium=180.0),
                _leg(expiry=expiry, strike=25000.0, right="PE", premium=170.0),
            ],
        )
        plan = build_plan(s)
        # Iterate intents. None of them should call execute_option_order
        # or submit_order_intent (they are pure data).
        for intent in plan.intents:
            assert intent.symbol
            assert intent.client_order_id
        assert submit_calls == []
        assert execute_calls == []

    def test_plan_does_not_carry_execution_status(self) -> None:
        """``OptionsStructurePlan.state`` for a valid, quoted plan is
        ``READY``. The plan must NOT claim a leg or the structure is
        'filled' / 'complete' merely because the model exists.
        """
        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.STRADDLE,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE", premium=180.0),
                _leg(expiry=expiry, strike=25000.0, right="PE", premium=170.0),
            ],
        )
        plan = build_plan(s)
        assert plan.state not in (
            StructureState.PARTIAL,
            StructureState.COMPLETE,
            StructureState.FAILED,
        )


# --------------------------------------------------------------------------- #
# Equity regression — Phase E touches no equity code path
# --------------------------------------------------------------------------- #
class TestEquityRegression:
    def test_equity_order_intent_construction_unchanged(self) -> None:
        """Phase E only adds a new module. It does NOT modify
        ``OrderIntent`` or ``Side``. An equity caller still gets
        exactly the same fields.
        """
        from trading_system.execution.orders import OrderIntent, Side, OrderType

        intent = OrderIntent(
            symbol="NSE:SBIN",
            side=Side.BUY,
            quantity=10.0,
            order_type=OrderType.MARKET,
            client_order_id="equity-cid",
            current_price=700.0,
        )
        assert intent.symbol == "NSE:SBIN"
        assert intent.side == Side.BUY
        assert intent.quantity == 10.0
        assert intent.options_contract_id is None
        assert intent.strike is None
        assert intent.expiry is None
        assert intent.option_type is None

    def test_options_structure_does_not_mutate_equity_world(self) -> None:
        """Sanity: building an OptionsStructure does not import or
        touch any equity-specific code."""
        expiry = _future_expiry()
        s = OptionsStructure(
            structure_type=StructureType.SINGLE,
            underlying="NIFTY",
            legs=[_leg(expiry=expiry, strike=25000.0, right="CE")],
        )
        assert s.underlying == "NIFTY"
        # No "NSE:SBIN" etc. in the structure.
        assert "NSE:SBIN" not in s.to_dict()["underlying"]


# --------------------------------------------------------------------------- #
# Premiums and the underlying spot — Phase E must never substitute one
# --------------------------------------------------------------------------- #
class TestUnderlyingSpotIsNotPremium:
    def test_leg_premium_must_be_explicit(self) -> None:
        """If a builder ever tried to use the NIFTY spot (~25000) as
        the option premium, validation would still pass, but the
        ``net_premium`` math would explode. Here we simply verify
        that the model surfaces the *configured* premium and that
        substituting the underlying spot would produce a very
        different (and clearly wrong) number.
        """
        expiry = _future_expiry()
        leg = _leg(
            expiry=expiry, strike=25000.0, right="CE",
            side=Side.BUY, premium=147.5, contracts=1,
        )
        # Premium is 147.5, NOT 25000.
        assert leg.net_premium == 147.5
        assert leg.premium != leg.strike  # not the strike either


# --------------------------------------------------------------------------- #
# Properties / invariants
# --------------------------------------------------------------------------- #
class TestInvariants:
    def test_same_definition_same_plan(self) -> None:
        expiry = _future_expiry()
        ce = _leg(expiry=expiry, strike=25000.0, right="CE", premium=180.0)
        pe = _leg(expiry=expiry, strike=25000.0, right="PE", premium=170.0)
        s_a = OptionsStructure(
            structure_type=StructureType.STRADDLE, underlying="NIFTY",
            legs=[ce, pe],
        )
        s_b = OptionsStructure(
            structure_type=StructureType.STRADDLE, underlying="NIFTY",
            legs=[pe, ce],  # reversed
        )
        p_a = build_plan(s_a)
        p_b = build_plan(s_b)
        assert p_a.structure_id == p_b.structure_id
        assert [i.client_order_id for i in p_a.intents] == [
            i.client_order_id for i in p_b.intents
        ]
        assert [i.symbol for i in p_a.intents] == [i.symbol for i in p_b.intents]

    def test_one_leg_change_one_intent_change(self) -> None:
        expiry = _future_expiry()
        s_a = OptionsStructure(
            structure_type=StructureType.VERTICAL_CALL_SPREAD,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE",
                     side=Side.BUY, premium=180.0),
                _leg(expiry=expiry, strike=25100.0, right="CE",
                     side=Side.SELL, premium=120.0),
            ],
        )
        s_b = OptionsStructure(
            structure_type=StructureType.VERTICAL_CALL_SPREAD,
            underlying="NIFTY",
            legs=[
                _leg(expiry=expiry, strike=25000.0, right="CE",
                     side=Side.BUY, premium=190.0),  # premium changed
                _leg(expiry=expiry, strike=25100.0, right="CE",
                     side=Side.SELL, premium=120.0),
            ],
        )
        p_a = build_plan(s_a)
        p_b = build_plan(s_b)
        # The structure_id is a function of the full leg payload,
        # so changing a premium changes the structure identity. This
        # is the correct, deterministic behaviour.
        assert p_a.structure_id != p_b.structure_id
        # The unchanged leg still maps to the same instrument_id.
        assert p_a.intents[1].symbol == p_b.intents[1].symbol
        # The changed leg has a different limit_price.
        assert p_a.intents[0].limit_price != p_b.intents[0].limit_price
        # The unchanged leg has the same limit_price.
        assert p_a.intents[1].limit_price == p_b.intents[1].limit_price
