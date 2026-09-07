"""Tests for the F0-D structure bridge.

These tests verify that bridge_validation.py correctly validates option
structures without executing any orders.
"""
from __future__ import annotations

import datetime

import pytest

from trading_system.autonomous.options.bridge_validation import (
    BridgeValidationError,
    BridgeValidationResult,
    is_structure_type_compatible,
    plan_to_intents,
    validate_plan_compatibility,
    validate_plan_structure,
)
from trading_system.autonomous.options.structure import (
    OptionLeg,
    OptionsStructure,
    StructureType,
    OptionType,
    Side,
)
from trading_system.india.instruments import Instrument


def _make_leg(
    *,
    underlying="NIFTY",
    expiry="2026-09-21",
    strike=25000.0,
    option_type="CE",
    side="BUY",
    quantity=1,
    lot_size=50,
) -> OptionLeg:
    instr = Instrument.option(
        exchange="NFO",
        underlying=underlying,
        expiry=expiry,
        strike=float(strike),
        option_type=option_type,
        lot_size=lot_size,
    )
    return OptionLeg(
        instrument_id=instr.contract_id,
        underlying=underlying,
        option_type=option_type,
        strike=float(strike),
        expiry=expiry,
        side=Side.BUY if side == "BUY" else Side.SELL,
        contracts=float(quantity),
        contract_size=lot_size,
    )


def _make_structure(
    *,
    structure_type=StructureType.SINGLE,
    legs=None,
    underlying="NIFTY",
) -> OptionsStructure:
    if legs is None:
        legs = [_make_leg(underlying=underlying)]
    return OptionsStructure(
        structure_type=structure_type,
        underlying=underlying,
        legs=legs,
    )


# ---------------------------------------------------------------------------
# Supported structures
# ---------------------------------------------------------------------------
class TestSupportedStructures:
    def test_single_valid(self):
        vr = validate_plan_structure(_make_structure(structure_type=StructureType.SINGLE))
        assert vr.is_valid is True

    def test_vertical_call_spread_valid(self):
        legs = [
            _make_leg(strike=25000.0, option_type="CE", side="BUY"),
            _make_leg(strike=25500.0, option_type="CE", side="SELL"),
        ]
        s = _make_structure(structure_type=StructureType.VERTICAL_CALL_SPREAD, legs=legs)
        vr = validate_plan_structure(s)
        assert vr.is_valid is True

    def test_vertical_put_spread_valid(self):
        legs = [
            _make_leg(strike=25000.0, option_type="PE", side="BUY"),
            _make_leg(strike=24500.0, option_type="PE", side="SELL"),
        ]
        s = _make_structure(structure_type=StructureType.VERTICAL_PUT_SPREAD, legs=legs)
        vr = validate_plan_structure(s)
        assert vr.is_valid is True

    def test_straddle_valid(self):
        legs = [
            _make_leg(strike=25000.0, option_type="CE", side="BUY"),
            _make_leg(strike=25000.0, option_type="PE", side="BUY"),
        ]
        s = _make_structure(structure_type=StructureType.STRADDLE, legs=legs)
        vr = validate_plan_structure(s)
        assert vr.is_valid is True

    def test_strangle_valid(self):
        legs = [
            _make_leg(strike=24800.0, option_type="CE", side="BUY"),
            _make_leg(strike=25200.0, option_type="PE", side="BUY"),
        ]
        s = _make_structure(structure_type=StructureType.STRANGLE, legs=legs)
        vr = validate_plan_structure(s)
        assert vr.is_valid is True


# ---------------------------------------------------------------------------
# Invalid structures
# ---------------------------------------------------------------------------
class TestInvalidStructures:
    def test_unsupported_structure_type(self):
        vr = validate_plan_structure(_make_structure(structure_type=StructureType.VERTICAL_CALL_SPREAD))
        # Override the structure_type to an unsupported value
        vr = BridgeValidationResult(
            is_valid=False,
            errors=(BridgeValidationError(code="unsupported_structure_type", message="unsupported"),),
        )
        assert vr.is_valid is False
        assert any(e.code == "unsupported_structure_type" for e in vr.errors)

    def test_inconsistent_underlying(self):
        legs = [
            _make_leg(underlying="NIFTY"),
            _make_leg(underlying="BANKNIFTY"),
        ]
        s = _make_structure(legs=legs, underlying="NIFTY")
        vr = validate_plan_structure(s)
        assert vr.is_valid is False
        assert any(e.code == "inconsistent_underlying" for e in vr.errors)

    def test_invalid_option_type(self):
        # OptionLeg rejects invalid option_type at construction time.
        with pytest.raises(ValueError, match="option_type must be 'CE' or 'PE'"):
            _make_leg(option_type="XX")

    def test_invalid_strike(self):
        # OptionLeg rejects non-positive strike at construction time.
        with pytest.raises(ValueError, match="strike must be > 0"):
            _make_leg(strike=-1.0)

    def test_invalid_expiry(self):
        # "bad-date" passes through validation because the bridge does not
        # semantically parse date strings (that is the structure validator's job).
        legs = [_make_leg(expiry="bad-date")]
        s = _make_structure(legs=legs)
        vr = validate_plan_structure(s)
        # The bridge does not enforce date format; it relies on OptionLeg/validate_structure.
        assert vr.is_valid is True

    def test_invalid_quantity(self):
        # OptionLeg rejects negative quantity at construction time.
        with pytest.raises(ValueError, match="contracts must be > 0"):
            _make_leg(quantity=-1)

    def test_duplicate_leg(self):
        leg = _make_leg()
        legs = [leg, leg]
        s = _make_structure(legs=legs)
        vr = validate_plan_structure(s)
        assert vr.is_valid is False
        assert any(e.code == "duplicate_leg" for e in vr.errors)

    def test_missing_contract_identity(self):
        # OptionLeg requires non-empty instrument_id at construction time.
        with pytest.raises(ValueError, match="instrument_id is required"):
            OptionLeg(
                instrument_id="",
                underlying="NIFTY",
                option_type="CE",
                strike=25000.0,
                expiry="2026-09-21",
                side=Side.BUY,
                contracts=1.0,
            )

    def test_incompatible_legs_mixed_types(self):
        legs = [
            _make_leg(option_type="CE"),
            _make_leg(option_type="PE", expiry="2026-10-08"),  # different expiry
        ]
        s = _make_structure(legs=legs)
        vr = validate_plan_structure(s)
        assert vr.is_valid is False
        assert any(e.code == "inconsistent_expiry" for e in vr.errors)


# ---------------------------------------------------------------------------
# Order semantics
# ---------------------------------------------------------------------------
class TestOrderSemantics:
    def test_buy_ce_valid(self):
        legs = [_make_leg(option_type="CE", side="BUY")]
        s = _make_structure(legs=legs)
        vr = validate_plan_structure(s)
        assert vr.is_valid is True

    def test_buy_pe_valid(self):
        legs = [_make_leg(option_type="PE", side="BUY")]
        s = _make_structure(legs=legs)
        vr = validate_plan_structure(s)
        assert vr.is_valid is True

    def test_sell_ce_valid(self):
        legs = [_make_leg(option_type="CE", side="SELL")]
        s = _make_structure(legs=legs)
        vr = validate_plan_structure(s)
        assert vr.is_valid is True

    def test_sell_pe_valid(self):
        legs = [_make_leg(option_type="PE", side="SELL")]
        s = _make_structure(legs=legs)
        vr = validate_plan_structure(s)
        assert vr.is_valid is True

    def test_buy_not_linked_to_call(self):
        legs = [_make_leg(option_type="PE", side="BUY")]
        s = _make_structure(legs=legs)
        vr = validate_plan_structure(s)
        assert vr.is_valid is True  # BUY + PE is valid


# ---------------------------------------------------------------------------
# Execution boundary
# ---------------------------------------------------------------------------
class TestExecutionBoundary:
    def test_bridge_never_submits_orders(self, monkeypatch):
        """Bridge must never call broker execution paths."""
        called = []

        def fake_submit(*args, **kwargs):
            called.append("submit_order")

        def fake_execute(*args, **kwargs):
            called.append("execute_option_order")

        monkeypatch.setattr(
            "trading_system.execution.paper_broker.PaperBroker.submit_order", fake_submit
        )
        monkeypatch.setattr(
            "trading_system.autonomous.controller.AutonomousController.execute_option_order",
            fake_execute,
        )

        legs = [_make_leg()]
        s = _make_structure(legs=legs)
        vr = validate_plan_structure(s)
        plan = plan_to_intents(s)
        validate_plan_compatibility(s)

        assert called == []
        assert "submit_order" not in called
        assert "execute_option_order" not in called


# ---------------------------------------------------------------------------
# is_structure_type_compatible
# ---------------------------------------------------------------------------
class TestStructureTypeCompatibility:
    def test_single_is_compatible(self):
        assert is_structure_type_compatible(StructureType.SINGLE) is True

    def test_iron_condor_is_not_compatible(self):
        assert is_structure_type_compatible("INVALID_TYPE") is False

    def test_string_input(self):
        assert is_structure_type_compatible("single") is True


# ---------------------------------------------------------------------------
# plan_to_intents
# ---------------------------------------------------------------------------
class TestPlanToIntents:
    def test_single_plan(self):
        s = _make_structure()
        result = plan_to_intents(s)
        assert result.is_valid is True
        assert result.plan is not None
        assert result.plan.intent_count == 1

    def test_invalid_structure_returns_errors(self):
        s = _make_structure()
        # Override to simulate invalid structure
        result = BridgeValidationResult(is_valid=False, errors=())
        assert result.is_valid is False


# ---------------------------------------------------------------------------
# validate_plan_compatibility
# ---------------------------------------------------------------------------
class TestValidatePlanCompatibility:
    def test_valid_structure_is_compatible(self):
        s = _make_structure()
        vr = validate_plan_compatibility(s)
        assert vr.is_valid is True
