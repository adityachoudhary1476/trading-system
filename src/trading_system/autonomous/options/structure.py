"""Phase E — Multi-leg options architecture (representation, validation, plan).

This module establishes a canonical, paper-only, deterministic representation
for multi-leg option structures. It deliberately does NOT execute anything:

  * No ``OrderIntent`` is submitted.
  * No ``PaperBroker`` is touched.
  * No scheduler hook is added.
  * No live broker is referenced.

Phase E is the architecture for future multi-leg execution; it is NOT the
execution itself. A future multi-leg execution phase will consume the
:class:`OptionsStructure` and :class:`OptionsStructurePlan` produced here.

Design constraints (mirrors the project-wide multi-leg spec):

  * Each leg carries an explicit ``side`` and ``option_type`` — no
    ``BUY -> CALL`` / ``SELL -> PUT`` inference. The model can represent
    every combination, but the side of a leg is independent of its right.
  * Each leg is anchored to the canonical ``Instrument`` (``contract_id``
    is the stable identity). Two different strikes / expiries / option
    types are always different contracts.
  * Determinism: legs are sorted in a canonical order before any
    structure-level identity is computed. The same structure definition
    always produces the same ``structure_id`` and the same leg ordering.
  * Structure type is a small explicit enum (single, vertical call spread,
    vertical put spread, straddle, strangle). Mixed-expiry / exotic
    strategies are NOT supported.
  * Validation is fail-closed. A structure with empty legs, missing
    instrument identity, zero contracts, or an invalid strike relationship
    is rejected and never becomes "ready".
  * Premiums are carried per leg; net premium is computed deterministically
    and is the *theoretical* structure cost, not realized P&L.
  * State model: ``PLANNED`` / ``READY`` / ``PARTIAL`` / ``COMPLETE`` /
    ``FAILED`` / ``CANCELLED``. Phase E only writes ``PLANNED`` and
    ``READY``. Future execution phases write the rest.
  * The :class:`OptionsStructure` model is independent of the older
    ``OptionsTradePlan`` so it is safe to land alongside Phase C / D
    without touching shared files.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Iterable, Optional

from trading_system.execution.orders import OrderIntent, OrderType, Side
from trading_system.india.instruments import (
    Instrument,
    InstrumentType,
    OptionType,
)


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class StructureType(str, Enum):
    """Explicit, finite set of multi-leg structures Phase E supports.

    The enum is small and explicit on purpose. New structures require a
    new validator method and a new identity mixin; they do not silently
    fall under a generic "custom" bucket.
    """

    SINGLE = "single"
    VERTICAL_CALL_SPREAD = "vertical_call_spread"
    VERTICAL_PUT_SPREAD = "vertical_put_spread"
    STRADDLE = "straddle"
    STRANGLE = "strangle"


class StructureLegState(str, Enum):
    """Per-leg execution state.

    Phase E only ever writes ``PLANNED`` (default) and ``READY`` (when
    the structure has been fully validated and quotes are present). The
    other states are reserved for a future execution phase.
    """

    PLANNED = "planned"
    READY = "ready"
    SUBMITTED = "submitted"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class StructureState(str, Enum):
    """Structure-level execution state for future atomicity tracking.

    Phase E writes only ``PLANNED`` (default), ``READY`` (validated, no
    quotes missing) and ``INVALID`` (validation failed). The other
    states are reserved for a future execution phase.
    """

    PLANNED = "planned"
    READY = "ready"
    INVALID = "invalid"
    PARTIAL = "partial"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


# --------------------------------------------------------------------------- #
# OptionLeg — one leg of a multi-leg structure
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OptionLeg:
    """One leg of a multi-leg options structure.

    A leg is *exactly*:

      * one underlying
      * one expiry
      * one strike
      * one option right (CE / PE)
      * one side (BUY / SELL)
      * a positive number of contracts
      * an optional reference premium (per-contract)

    The leg carries a canonical ``instrument_id`` (``Instrument.contract_id``)
    so a downstream execution layer can resolve the contract through the
    repository without re-parsing free-form strings. ``instrument_id`` is
    the stable identity; the other fields are denormalized projections
    used for sorting and validation.

    The model does NOT:

      * infer side from option_type (BUY != CALL);
      * infer option_type from side (SELL != PUT);
      * silently fall back to the underlying spot as a premium.
    """

    instrument_id: str
    underlying: str
    option_type: str            # "CE" / "PE"
    strike: float
    expiry: str                 # ISO YYYY-MM-DD
    side: Side                  # BUY / SELL
    contracts: float            # > 0
    premium: Optional[float] = None
    contract_size: Optional[int] = None
    state: StructureLegState = StructureLegState.PLANNED
    leg_id: str = ""

    def __post_init__(self) -> None:
        if not self.instrument_id:
            raise ValueError("OptionLeg.instrument_id is required")
        if self.option_type not in ("CE", "PE"):
            raise ValueError(
                f"OptionLeg.option_type must be 'CE' or 'PE', got {self.option_type!r}"
            )
        if self.strike <= 0:
            raise ValueError(f"OptionLeg.strike must be > 0, got {self.strike!r}")
        if not self.expiry:
            raise ValueError("OptionLeg.expiry is required (ISO date)")
        if float(self.contracts) <= 0:
            raise ValueError(
                f"OptionLeg.contracts must be > 0, got {self.contracts!r}"
            )
        if self.premium is not None and float(self.premium) < 0:
            raise ValueError(
                f"OptionLeg.premium must be >= 0, got {self.premium!r}"
            )
        if self.contract_size is not None and int(self.contract_size) <= 0:
            raise ValueError(
                f"OptionLeg.contract_size must be > 0 when set, got {self.contract_size!r}"
            )
        # leg_id is a deterministic identity (pure function of the leg
        # payload) computed once at construction.
        if not self.leg_id:
            object.__setattr__(self, "leg_id", self._compute_leg_id())

    # -- identity --------------------------------------------------------- #
    def _compute_leg_id(self) -> str:
        payload = {
            "instrument_id": str(self.instrument_id),
            "underlying": str(self.underlying),
            "option_type": str(self.option_type),
            "strike": float(self.strike),
            "expiry": str(self.expiry),
            "side": self.side.value,
            "contracts": float(self.contracts),
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(("phase-e-leg:" + blob).encode("utf-8")).hexdigest()[:24]

    # -- accounting primitives ------------------------------------------- #
    @property
    def signed_contracts(self) -> float:
        """Signed contract count. BUY = +, SELL = -."""
        return float(self.contracts) if self.side == Side.BUY else -float(self.contracts)

    @property
    def net_premium(self) -> float:
        """Theoretical net premium contribution of this leg.

        Positive = the leg adds cost to the structure (BUY).
        Negative = the leg adds proceeds (SELL).
        Returns 0.0 when no premium is known.
        """
        if self.premium is None:
            return 0.0
        sign = 1.0 if self.side == Side.BUY else -1.0
        return sign * float(self.premium) * float(self.contracts)

    @property
    def notional(self) -> float:
        """Premium * contracts (unsigned). 0.0 when premium unknown."""
        if self.premium is None:
            return 0.0
        return float(self.premium) * float(self.contracts)

    def with_state(self, state: StructureLegState) -> "OptionLeg":
        """Return a new leg with the given state (immutable update)."""
        return OptionLeg(
            instrument_id=self.instrument_id,
            underlying=self.underlying,
            option_type=self.option_type,
            strike=self.strike,
            expiry=self.expiry,
            side=self.side,
            contracts=self.contracts,
            premium=self.premium,
            contract_size=self.contract_size,
            state=state,
            leg_id=self.leg_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "leg_id": self.leg_id,
            "instrument_id": self.instrument_id,
            "underlying": self.underlying,
            "option_type": self.option_type,
            "strike": float(self.strike),
            "expiry": self.expiry,
            "side": self.side.value,
            "contracts": float(self.contracts),
            "premium": (float(self.premium) if self.premium is not None else None),
            "contract_size": (
                int(self.contract_size) if self.contract_size is not None else None
            ),
            "state": self.state.value,
            "signed_contracts": self.signed_contracts,
            "net_premium": self.net_premium,
            "notional": self.notional,
        }


# --------------------------------------------------------------------------- #
# Validation result
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StructureValidationError:
    """One specific reason a structure failed validation."""

    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class StructureValidationResult:
    """The outcome of validating an :class:`OptionsStructure`."""

    is_valid: bool
    errors: tuple[StructureValidationError, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "errors": [e.to_dict() for e in self.errors],
        }


# --------------------------------------------------------------------------- #
# OptionsStructure — canonical multi-leg structure
# --------------------------------------------------------------------------- #
@dataclass
class OptionsStructure:
    """A canonical, validated multi-leg options structure.

    The structure is the source of truth for:

      * the underlying (e.g. ``"NIFTY"``);
      * the explicit structure type (single, vertical call spread, …);
      * a stable ``structure_id`` derived purely from the leg payload;
      * the list of legs (sorted deterministically);
      * the structure-level state (PLANNED / READY / INVALID / …).

    The model deliberately does NOT assume the structure is executable;
    the ``state`` field is the authoritative signal a future execution
    layer will read.
    """

    structure_type: StructureType
    underlying: str
    legs: list[OptionLeg] = field(default_factory=list)
    structure_id: str = ""
    state: StructureState = StructureState.PLANNED
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.underlying:
            raise ValueError("OptionsStructure.underlying is required")
        if not isinstance(self.structure_type, StructureType):
            raise ValueError(
                f"structure_type must be a StructureType, got {self.structure_type!r}"
            )
        # Sort legs deterministically so the structure_id is invariant
        # under construction-time ordering.
        self.legs = sorted(self.legs, key=_leg_sort_key)
        if not self.structure_id:
            self.structure_id = self._compute_structure_id()

    # -- identity --------------------------------------------------------- #
    def _compute_structure_id(self) -> str:
        payload = {
            "structure_type": self.structure_type.value,
            "underlying": str(self.underlying),
            "legs": [leg.to_dict() for leg in self.legs],
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(
            ("phase-e-structure:" + blob).encode("utf-8")
        ).hexdigest()[:32]

    # -- accessors -------------------------------------------------------- #
    @property
    def is_empty(self) -> bool:
        return len(self.legs) == 0

    @property
    def leg_count(self) -> int:
        return len(self.legs)

    def leg_by_id(self, leg_id: str) -> Optional[OptionLeg]:
        for leg in self.legs:
            if leg.leg_id == leg_id:
                return leg
        return None

    # -- premium / exposure --------------------------------------------- #
    @property
    def net_premium(self) -> float:
        """Sum of per-leg net premium (BUY +, SELL -).

        Returns the *theoretical* structure cost. 0.0 when any leg has
        no premium. No realized P&L is calculated.
        """
        return float(sum(leg.net_premium for leg in self.legs))

    @property
    def gross_premium(self) -> float:
        """Sum of per-leg unsigned notional. 0.0 when any leg has no premium."""
        return float(sum(leg.notional for leg in self.legs))

    @property
    def has_all_premiums(self) -> bool:
        return all(leg.premium is not None for leg in self.legs)

    @property
    def unique_instrument_ids(self) -> set[str]:
        return {leg.instrument_id for leg in self.legs}

    # -- (de)serialisation ---------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        return {
            "structure_id": self.structure_id,
            "structure_type": self.structure_type.value,
            "underlying": self.underlying,
            "state": self.state.value,
            "notes": self.notes,
            "legs": [leg.to_dict() for leg in self.legs],
            "net_premium": self.net_premium,
            "gross_premium": self.gross_premium,
            "has_all_premiums": self.has_all_premiums,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OptionsStructure":
        legs = [
            _leg_from_dict(leg) for leg in data.get("legs", [])
        ]
        return cls(
            structure_type=StructureType(data["structure_type"]),
            underlying=data["underlying"],
            legs=legs,
            structure_id=data.get("structure_id", ""),
            state=StructureState(data.get("state", StructureState.PLANNED.value)),
            notes=data.get("notes", ""),
        )


# --------------------------------------------------------------------------- #
# OptionsStructurePlan — deterministic execution plan (no execution)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OptionsStructurePlan:
    """Deterministic execution plan for an :class:`OptionsStructure`.

    The plan is the exact ordered list of leg-level ``OrderIntent``
    objects a future execution phase will submit to the control center.
    The plan does NOT submit anything — it only *describes* what would
    be submitted.

    The plan also carries:

      * the source structure_id (so a future ``PaperSessionStore`` can
        dedupe on structure identity, not per-leg);
      * the structure-level state (``PLANNED`` / ``READY`` by default);
      * the per-leg leg_ids in the same order as the intents;
      * the deterministic net premium (theoretical).

    Reconstructing the same structure must always produce the same plan
    with the same ordered intents and the same client_order_ids.
    """

    structure_id: str
    structure_type: StructureType
    underlying: str
    leg_ids: tuple[str, ...]
    intents: tuple[OrderIntent, ...]
    net_premium: float
    state: StructureState
    has_all_premiums: bool
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "structure_id": self.structure_id,
            "structure_type": self.structure_type.value,
            "underlying": self.underlying,
            "state": self.state.value,
            "leg_ids": list(self.leg_ids),
            "intents": [_intent_to_dict(i) for i in self.intents],
            "net_premium": self.net_premium,
            "has_all_premiums": self.has_all_premiums,
            "notes": self.notes,
        }

    @property
    def intent_count(self) -> int:
        return len(self.intents)

    @property
    def is_ready(self) -> bool:
        return self.state == StructureState.READY


# --------------------------------------------------------------------------- #
# Validation entry point
# --------------------------------------------------------------------------- #
def validate_structure(structure: OptionsStructure) -> StructureValidationResult:
    """Run the structure-specific validator. Pure, side-effect free.

    Returns a :class:`StructureValidationResult` listing every reason
    the structure was rejected. An empty ``errors`` tuple means the
    structure is valid for its declared type.
    """
    errors: list[StructureValidationError] = []
    _check_empty(structure, errors)
    _check_leg_basics(structure, errors)
    _check_universal_invariants(structure, errors)
    _check_structure_type_specific(structure, errors)
    is_valid = len(errors) == 0
    return StructureValidationResult(
        is_valid=is_valid,
        errors=tuple(errors),
    )


def _check_empty(
    structure: OptionsStructure,
    errors: list[StructureValidationError],
) -> None:
    if structure.is_empty:
        errors.append(
            StructureValidationError(
                code="empty_structure",
                message="structure must contain at least one leg",
            )
        )


def _check_leg_basics(
    structure: OptionsStructure,
    errors: list[StructureValidationError],
) -> None:
    for i, leg in enumerate(structure.legs):
        if not leg.instrument_id:
            errors.append(
                StructureValidationError(
                    code="missing_instrument_id",
                    message=f"leg[{i}] is missing instrument_id",
                )
            )
        if leg.option_type not in ("CE", "PE"):
            errors.append(
                StructureValidationError(
                    code="invalid_option_type",
                    message=(
                        f"leg[{i}] option_type must be 'CE' or 'PE', "
                        f"got {leg.option_type!r}"
                    ),
                )
            )
        if float(leg.contracts) <= 0:
            errors.append(
                StructureValidationError(
                    code="invalid_contracts",
                    message=f"leg[{i}] contracts must be > 0",
                )
            )
        if leg.strike <= 0:
            errors.append(
                StructureValidationError(
                    code="invalid_strike",
                    message=f"leg[{i}] strike must be > 0",
                )
            )
        if not leg.expiry:
            errors.append(
                StructureValidationError(
                    code="missing_expiry",
                    message=f"leg[{i}] is missing expiry",
                )
            )


def _check_universal_invariants(
    structure: OptionsStructure,
    errors: list[StructureValidationError],
) -> None:
    # All legs must belong to the same underlying.
    bad_underlyings = {
        leg.underlying for leg in structure.legs
        if leg.underlying and leg.underlying != structure.underlying
    }
    if bad_underlyings:
        errors.append(
            StructureValidationError(
                code="mixed_underlying",
                message=(
                    f"all legs must share underlying {structure.underlying!r}; "
                    f"got {sorted(bad_underlyings)!r}"
                ),
            )
        )


def _check_structure_type_specific(
    structure: OptionsStructure,
    errors: list[StructureValidationError],
) -> None:
    if structure.is_empty:
        # Empty already reported.
        return
    if structure.structure_type == StructureType.SINGLE:
        _check_single(structure, errors)
    elif structure.structure_type == StructureType.VERTICAL_CALL_SPREAD:
        _check_vertical(structure, errors, option_type="CE")
    elif structure.structure_type == StructureType.VERTICAL_PUT_SPREAD:
        _check_vertical(structure, errors, option_type="PE")
    elif structure.structure_type == StructureType.STRADDLE:
        _check_straddle(structure, errors)
    elif structure.structure_type == StructureType.STRANGLE:
        _check_strangle(structure, errors)
    else:
        errors.append(
            StructureValidationError(
                code="unsupported_structure_type",
                message=f"structure_type {structure.structure_type!r} is not supported",
            )
        )


def _check_single(
    structure: OptionsStructure,
    errors: list[StructureValidationError],
) -> None:
    if len(structure.legs) != 1:
        errors.append(
            StructureValidationError(
                code="invalid_leg_count",
                message=(
                    f"SINGLE requires exactly 1 leg, got {len(structure.legs)}"
                ),
            )
        )


def _check_vertical(
    structure: OptionsStructure,
    errors: list[StructureValidationError],
    *,
    option_type: str,
) -> None:
    if len(structure.legs) != 2:
        errors.append(
            StructureValidationError(
                code="invalid_leg_count",
                message=(
                    f"vertical {option_type} spread requires exactly 2 legs, "
                    f"got {len(structure.legs)}"
                ),
            )
        )
        return
    a, b = structure.legs
    if a.option_type != option_type or b.option_type != option_type:
        errors.append(
            StructureValidationError(
                code="invalid_option_type",
                message=(
                    f"vertical {option_type} spread must have both legs as {option_type}"
                ),
            )
        )
    if a.expiry != b.expiry:
        errors.append(
            StructureValidationError(
                code="mixed_expiry",
                message=(
                    "vertical spread requires a common expiry; "
                    f"got {a.expiry!r} and {b.expiry!r}"
                ),
            )
        )
    if a.strike == b.strike:
        errors.append(
            StructureValidationError(
                code="invalid_strike_relationship",
                message=(
                    f"vertical {option_type} spread requires two distinct strikes; "
                    f"both legs have strike={a.strike}"
                ),
            )
        )
    if a.instrument_id and b.instrument_id and a.instrument_id == b.instrument_id:
        errors.append(
            StructureValidationError(
                code="duplicate_contract",
                message=(
                    f"vertical {option_type} spread legs must not share the same "
                    f"contract_id={a.instrument_id!r}"
                ),
            )
        )


def _check_straddle(
    structure: OptionsStructure,
    errors: list[StructureValidationError],
) -> None:
    if len(structure.legs) != 2:
        errors.append(
            StructureValidationError(
                code="invalid_leg_count",
                message=(
                    f"STRADDLE requires exactly 2 legs, got {len(structure.legs)}"
                ),
            )
        )
        return
    a, b = structure.legs
    if a.expiry != b.expiry:
        errors.append(
            StructureValidationError(
                code="mixed_expiry",
                message=(
                    "STRADDLE requires a common expiry; "
                    f"got {a.expiry!r} and {b.expiry!r}"
                ),
            )
        )
    if a.strike != b.strike:
        errors.append(
            StructureValidationError(
                code="invalid_strike_relationship",
                message=(
                    "STRADDLE requires both legs to share the same strike; "
                    f"got CE strike={a.strike} PE strike={b.strike}"
                ),
            )
        )
    rights = {a.option_type, b.option_type}
    if rights != {"CE", "PE"}:
        errors.append(
            StructureValidationError(
                code="invalid_right_combo",
                message=(
                    f"STRADDLE requires one CE and one PE leg; got {sorted(rights)!r}"
                ),
            )
        )
    if a.instrument_id and b.instrument_id and a.instrument_id == b.instrument_id:
        errors.append(
            StructureValidationError(
                code="duplicate_contract",
                message=(
                    "STRADDLE legs must not share the same contract_id="
                    f"{a.instrument_id!r}"
                ),
            )
        )


def _check_strangle(
    structure: OptionsStructure,
    errors: list[StructureValidationError],
) -> None:
    if len(structure.legs) != 2:
        errors.append(
            StructureValidationError(
                code="invalid_leg_count",
                message=(
                    f"STRANGLE requires exactly 2 legs, got {len(structure.legs)}"
                ),
            )
        )
        return
    a, b = structure.legs
    if a.expiry != b.expiry:
        errors.append(
            StructureValidationError(
                code="mixed_expiry",
                message=(
                    "STRANGLE requires a common expiry; "
                    f"got {a.expiry!r} and {b.expiry!r}"
                ),
            )
        )
    if a.strike == b.strike:
        errors.append(
            StructureValidationError(
                code="invalid_strike_relationship",
                message=(
                    "STRANGLE requires CE and PE strikes to differ; "
                    f"both legs have strike={a.strike}"
                ),
            )
        )
    rights = {a.option_type, b.option_type}
    if rights != {"CE", "PE"}:
        errors.append(
            StructureValidationError(
                code="invalid_right_combo",
                message=(
                    f"STRANGLE requires one CE and one PE leg; got {sorted(rights)!r}"
                ),
            )
        )
    if a.instrument_id and b.instrument_id and a.instrument_id == b.instrument_id:
        errors.append(
            StructureValidationError(
                code="duplicate_contract",
                message=(
                    "STRANGLE legs must not share the same contract_id="
                    f"{a.instrument_id!r}"
                ),
            )
        )


# --------------------------------------------------------------------------- #
# Conversion: structure -> plan
# --------------------------------------------------------------------------- #
def build_plan(
    structure: OptionsStructure,
    *,
    require_premiums: bool = False,
) -> OptionsStructurePlan:
    """Build a deterministic execution plan for a validated structure.

    Behaviour:

      * If ``structure`` fails validation, the returned plan has
        ``state = INVALID`` and an empty ``intents`` list.
      * If ``require_premiums`` is True and any leg is missing a
        premium, the returned plan has ``state = PLANNED`` (not READY)
        so a future execution phase can wait for quotes.
      * Otherwise the plan is ``state = READY``.

    Each leg is converted to an ``OrderIntent`` whose
    ``client_order_id`` is a deterministic sha256 of
    ``structure_id + leg_id + side``. A future ``PaperSessionStore``
    can dedupe on that key.
    """
    result = validate_structure(structure)
    if not result.is_valid:
        return OptionsStructurePlan(
            structure_id=structure.structure_id,
            structure_type=structure.structure_type,
            underlying=structure.underlying,
            leg_ids=tuple(leg.leg_id for leg in structure.legs),
            intents=(),
            net_premium=0.0,
            state=StructureState.INVALID,
            has_all_premiums=False,
            notes=(
                "structure failed validation: "
                + "; ".join(e.code for e in result.errors)
            ),
        )

    intents: list[OrderIntent] = []
    for leg in structure.legs:
        has_price = leg.premium is not None and float(leg.premium) > 0
        order_type = OrderType.LIMIT if has_price else OrderType.MARKET
        cid = _leg_client_order_id(structure.structure_id, leg.leg_id, leg.side)
        intents.append(
            OrderIntent(
                symbol=leg.instrument_id,
                side=leg.side,
                quantity=float(leg.contracts),
                order_type=order_type,
                limit_price=(float(leg.premium) if has_price else None),
                client_order_id=cid,
                current_price=(float(leg.premium) if has_price else None),
                options_contract_id=leg.instrument_id,
                strike=leg.strike,
                expiry=leg.expiry,
                option_type=leg.option_type,
            )
        )

    has_all_premiums = structure.has_all_premiums
    state = StructureState.READY
    notes = ""
    if require_premiums and not has_all_premiums:
        state = StructureState.PLANNED
        notes = "structure ready but awaiting premiums for one or more legs"

    return OptionsStructurePlan(
        structure_id=structure.structure_id,
        structure_type=structure.structure_type,
        underlying=structure.underlying,
        leg_ids=tuple(leg.leg_id for leg in structure.legs),
        intents=tuple(intents),
        net_premium=structure.net_premium,
        state=state,
        has_all_premiums=has_all_premiums,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# Resolution helper — turn a high-level shape into legs using the repository
# --------------------------------------------------------------------------- #
def resolve_vertical_call_spread(
    *,
    repository,                                # InstrumentRepository
    underlying: str,
    expiry: str,
    lower_strike: float,
    upper_strike: float,
    buy_side: Side,                            # which strike is bought
    contracts: float = 1.0,
) -> OptionsStructure:
    """Build a deterministic vertical call spread from a repository.

    Resolves the two exact ``Instrument`` objects through
    ``repository.find_contract``. Fails closed if either leg cannot be
    resolved — no synthetic contract is ever constructed.
    """
    if lower_strike == upper_strike:
        raise ValueError("vertical call spread requires lower_strike != upper_strike")
    if buy_side not in (Side.BUY, Side.SELL):
        raise ValueError("buy_side must be BUY or SELL")
    long_strike, short_strike = (
        (lower_strike, upper_strike)
        if buy_side == Side.BUY
        else (upper_strike, lower_strike)
    )
    long_instr = repository.find_contract(
        underlying=underlying,
        expiry=expiry,
        option_type="CE",
        strike=float(long_strike),
    )
    short_instr = repository.find_contract(
        underlying=underlying,
        expiry=expiry,
        option_type="CE",
        strike=float(short_strike),
    )
    if long_instr is None or short_instr is None:
        raise ValueError(
            "vertical call spread could not resolve both legs through the "
            f"repository: long={long_instr} short={short_instr}"
        )
    long_leg = _leg_from_instrument(
        instrument=long_instr, side=Side.BUY, contracts=contracts,
    )
    short_leg = _leg_from_instrument(
        instrument=short_instr, side=Side.SELL, contracts=contracts,
    )
    return OptionsStructure(
        structure_type=StructureType.VERTICAL_CALL_SPREAD,
        underlying=underlying,
        legs=[long_leg, short_leg],
    )


def resolve_straddle(
    *,
    repository,                                # InstrumentRepository
    underlying: str,
    expiry: str,
    strike: float,
    contracts: float = 1.0,
) -> OptionsStructure:
    """Build a deterministic straddle from a repository (long straddle)."""
    call = repository.find_contract(
        underlying=underlying,
        expiry=expiry,
        option_type="CE",
        strike=float(strike),
    )
    put = repository.find_contract(
        underlying=underlying,
        expiry=expiry,
        option_type="PE",
        strike=float(strike),
    )
    if call is None or put is None:
        raise ValueError(
            "straddle could not resolve both legs through the repository: "
            f"call={call} put={put}"
        )
    call_leg = _leg_from_instrument(instrument=call, side=Side.BUY, contracts=contracts)
    put_leg = _leg_from_instrument(instrument=put, side=Side.BUY, contracts=contracts)
    return OptionsStructure(
        structure_type=StructureType.STRADDLE,
        underlying=underlying,
        legs=[call_leg, put_leg],
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _leg_sort_key(leg: OptionLeg) -> tuple:
    """Deterministic, total ordering for legs.

    Sorted by (option_type, strike, expiry, side, instrument_id,
    contracts) — all six are total. Two legs of the same structure
    never compare equal under this key unless every field matches
    (which would itself be a duplicate contract, rejected by validation).
    """
    return (
        str(leg.option_type),
        float(leg.strike),
        str(leg.expiry),
        leg.side.value,
        str(leg.instrument_id),
        float(leg.contracts),
    )


def _leg_client_order_id(structure_id: str, leg_id: str, side: Side) -> str:
    payload = f"{structure_id}:{leg_id}:{side.value}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:48]


def _leg_from_dict(data: dict[str, Any]) -> OptionLeg:
    return OptionLeg(
        instrument_id=data["instrument_id"],
        underlying=data["underlying"],
        option_type=data["option_type"],
        strike=float(data["strike"]),
        expiry=data["expiry"],
        side=Side(data["side"]),
        contracts=float(data["contracts"]),
        premium=(float(data["premium"]) if data.get("premium") is not None else None),
        contract_size=(
            int(data["contract_size"]) if data.get("contract_size") is not None else None
        ),
        state=StructureLegState(data.get("state", StructureLegState.PLANNED.value)),
        leg_id=data.get("leg_id", ""),
    )


def _leg_from_instrument(
    *,
    instrument: Instrument,
    side: Side,
    contracts: float,
) -> OptionLeg:
    """Build an :class:`OptionLeg` from a resolved repository ``Instrument``."""
    if instrument.instrument_type not in (
        InstrumentType.OPTION_CE,
        InstrumentType.OPTION_PE,
    ):
        raise ValueError(
            f"instrument is not an option: {instrument.instrument_type}"
        )
    if not instrument.underlying:
        raise ValueError("instrument.underlying is required to build a leg")
    ot = instrument.option_type or (
        "CE" if instrument.instrument_type == InstrumentType.OPTION_CE else "PE"
    )
    if ot not in ("CE", "PE"):
        raise ValueError(f"instrument has invalid option_type={ot!r}")
    if instrument.strike is None or float(instrument.strike) <= 0:
        raise ValueError("instrument.strike is required to build a leg")
    if not instrument.expiry:
        raise ValueError("instrument.expiry is required to build a leg")
    return OptionLeg(
        instrument_id=instrument.contract_id,
        underlying=instrument.underlying,
        option_type=ot,
        strike=float(instrument.strike),
        expiry=instrument.expiry,
        side=side,
        contracts=float(contracts),
    )


def _intent_to_dict(intent: OrderIntent) -> dict[str, Any]:
    return {
        "symbol": intent.symbol,
        "side": intent.side.value,
        "quantity": intent.quantity,
        "order_type": intent.order_type.value,
        "limit_price": intent.limit_price,
        "client_order_id": intent.client_order_id,
        "current_price": intent.current_price,
        "options_contract_id": intent.options_contract_id,
        "strike": intent.strike,
        "expiry": intent.expiry,
        "option_type": intent.option_type,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #
__all__ = [
    "StructureType",
    "StructureState",
    "StructureLegState",
    "OptionLeg",
    "OptionsStructure",
    "OptionsStructurePlan",
    "StructureValidationError",
    "StructureValidationResult",
    "validate_structure",
    "build_plan",
    "resolve_vertical_call_spread",
    "resolve_straddle",
]
