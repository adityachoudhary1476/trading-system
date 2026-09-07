"""F0-D Structure Bridge — pure validation/preparation layer for Phase E option structures.

This module bridges the Phase E structure model with the rest of the autonomous
stack WITHOUT performing any execution. It is a read-only validator that
enforces the paper-only safety boundary.

Hard rules:
  * NEVER calls PaperBroker.submit_order
  * NEVER calls PaperTradingControlCenter.submit_order_intent
  * NEVER calls AutonomousController.execute_option_order
  * NEVER mutates positions, account balances, scheduler state
  * NEVER executes live API calls
  * NEVER introduces synthetic production contracts
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from trading_system.autonomous.options.structure import (
    OptionLeg,
    OptionsStructure,
    OptionsStructurePlan,
    StructureType,
    StructureValidationResult,
    build_plan,
    validate_structure,
)
from trading_system.execution.orders import Side


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BridgeValidationError:
    code: str
    message: str
    leg_index: Optional[int] = None
    detail: Optional[str] = None


@dataclass(frozen=True)
class BridgeValidationResult:
    is_valid: bool
    errors: tuple[BridgeValidationError, ...] = ()
    plan: Optional[OptionsStructurePlan] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "errors": [
                {
                    "code": e.code,
                    "message": e.message,
                    "leg_index": e.leg_index,
                    "detail": e.detail,
                }
                for e in self.errors
            ],
            "plan": self.plan.to_dict() if self.plan is not None else None,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _option_type_to_str(option_type: Any) -> Optional[str]:
    if option_type is None:
        return None
    if hasattr(option_type, "value"):
        return str(option_type.value)
    return str(option_type)


def _side_to_str(side: Any) -> Optional[str]:
    if side is None:
        return None
    if hasattr(side, "value"):
        return str(side.value)
    return str(side)


# ---------------------------------------------------------------------------
# Core validation
# ---------------------------------------------------------------------------
def validate_plan_structure(structure: OptionsStructure) -> BridgeValidationResult:
    """Validate an OptionsStructure for Phase E execution readiness.

    Pure validation: no orders, no broker calls, no side effects.
    """
    errors: list[BridgeValidationError] = []

    # --- Phase E structure validator ---
    ev: StructureValidationResult = validate_structure(structure)
    for err in ev.errors:
        errors.append(BridgeValidationError(
            code="structure_invalid",
            message=err.message,
            leg_index=getattr(err, "leg_index", None),
            detail=getattr(err, "detail", None),
        ))

    # --- Canonical contract identity ---
    for idx, leg in enumerate(structure.legs):
        cid = getattr(leg, "instrument_id", None)
        if not cid:
            errors.append(BridgeValidationError(
                code="missing_contract_identity",
                message="option leg is missing canonical contract identity",
                leg_index=idx,
            ))

    # --- Underlying consistency ---
    underlyings = {getattr(leg, "underlying", None) for leg in structure.legs}
    underlyings.discard(None)
    if len(underlyings) > 1:
        errors.append(BridgeValidationError(
            code="inconsistent_underlying",
            message=f"structure mixes multiple underlyings: {underlyings}",
        ))

    # --- Expiry consistency ---
    expiries = {getattr(leg, "expiry", None) for leg in structure.legs}
    expiries.discard(None)
    if len(expiries) > 1:
        errors.append(BridgeValidationError(
            code="inconsistent_expiry",
            message=f"structure mixes multiple expiries: {expiries}",
        ))

    # --- Option type per leg ---
    for idx, leg in enumerate(structure.legs):
        ot = _option_type_to_str(getattr(leg, "option_type", None))
        if ot not in ("CE", "PE"):
            errors.append(BridgeValidationError(
                code="invalid_option_type",
                message=f"leg {idx} has invalid option_type={ot!r}; expected CE or PE",
                leg_index=idx,
            ))

    # --- BUY/SELL semantics ---
    for idx, leg in enumerate(structure.legs):
        side_str = _side_to_str(getattr(leg, "side", None))
        if side_str not in ("BUY", "SELL"):
            errors.append(BridgeValidationError(
                code="invalid_side",
                message=f"leg {idx} has invalid side={side_str!r}; expected BUY or SELL",
                leg_index=idx,
            ))

    # --- Quantity ---
    for idx, leg in enumerate(structure.legs):
        qty = getattr(leg, "contracts", None)
        if qty is None or qty <= 0:
            errors.append(BridgeValidationError(
                code="invalid_quantity",
                message=f"leg {idx} has non-positive quantity={qty!r}",
                leg_index=idx,
            ))

    # --- Duplicate/conflicting legs ---
    seen: set[str] = set()
    for idx, leg in enumerate(structure.legs):
        cid = getattr(leg, "instrument_id", "")
        side_str = _side_to_str(getattr(leg, "side", None))
        qty = getattr(leg, "contracts", 0)
        key = (cid, side_str, qty)
        if key in seen:
            errors.append(BridgeValidationError(
                code="duplicate_leg",
                message=f"duplicate leg at index {idx}: {key}",
                leg_index=idx,
            ))
        seen.add(key)

    # --- Structure type compatibility ---
    st = getattr(structure, "structure_type", None)
    if st is None:
        errors.append(BridgeValidationError(
            code="missing_structure_type",
            message="structure is missing structure_type",
        ))
    else:
        supported = {
            StructureType.SINGLE,
            StructureType.VERTICAL_CALL_SPREAD,
            StructureType.VERTICAL_PUT_SPREAD,
            StructureType.STRADDLE,
            StructureType.STRANGLE,
        }
        if st not in supported:
            errors.append(BridgeValidationError(
                code="unsupported_structure_type",
                message=f"structure type {st.value} is not supported for Phase E",
            ))

    is_valid = len(errors) == 0
    return BridgeValidationResult(is_valid=is_valid, errors=tuple(errors))


def plan_to_intents(structure: OptionsStructure) -> BridgeValidationResult:
    """Build the execution plan for a structure.

    Returns a structured result with the plan (if valid) or errors.
    Pure: does not submit anything.
    """
    vr = validate_plan_structure(structure)
    if not vr.is_valid:
        return vr
    try:
        plan = build_plan(structure)
    except Exception as exc:  # noqa: BLE001
        return BridgeValidationResult(
            is_valid=False,
            errors=(BridgeValidationError(
                code="plan_build_failed",
                message=f"failed to build plan: {type(exc).__name__}: {exc}",
            ),),
        )
    return BridgeValidationResult(
        is_valid=True,
        plan=plan,
        metadata={"leg_count": len(plan.intents), "net_premium": plan.net_premium},
    )


def validate_plan_compatibility(structure: OptionsStructure) -> BridgeValidationResult:
    """Validate that the structure is compatible with the existing autonomous stack.

    Checks:
      * All legs have canonical contract identity
      * Underlying consistency across legs
      * Expiry consistency across legs
      * Option types are CE or PE
      * Sides are BUY or SELL
      * Quantities are positive
      * No duplicate/conflicting legs
      * Structure type is one of the supported types
    """
    return validate_plan_structure(structure)


def is_structure_type_compatible(structure_type: Any) -> bool:
    """Return True if the structure type is supported by the bridge."""
    supported = {
        StructureType.SINGLE,
        StructureType.VERTICAL_CALL_SPREAD,
        StructureType.VERTICAL_PUT_SPREAD,
        StructureType.STRADDLE,
        StructureType.STRANGLE,
    }
    if isinstance(structure_type, StructureType):
        return structure_type in supported
    try:
        return StructureType(structure_type) in supported
    except (ValueError, TypeError):
        return False


__all__ = [
    "BridgeValidationError",
    "BridgeValidationResult",
    "is_structure_type_compatible",
    "plan_to_intents",
    "validate_plan_compatibility",
    "validate_plan_structure",
]
