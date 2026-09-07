"""Phase 8 — Options contract selection data model.

Types the criteria for selecting a single-leg option contract. The resolved
``Instrument`` (existing model) is the canonical source of truth; this model
serializes its key fields for cross-layer propagation.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trading_system.india.instruments import (
    Exchange,
    Instrument,
    InstrumentRegistry,
    InstrumentType,
    InternalSymbol,
    OptionType,
)


class OptionDirection(str, Enum):
    """Directional bias mapped to option right.

    Bullish → CALL (CE), Bearish → PUT (PE).
    """

    CALL = "call"
    PUT = "put"

    @property
    def option_type(self) -> str:
        return "CE" if self is OptionDirection.CALL else "PE"

    @property
    def instrument_type(self) -> InstrumentType:
        return (
            InstrumentType.OPTION_CE
            if self is OptionDirection.CALL
            else InstrumentType.OPTION_PE
        )


class StrikeSelectionPolicy(str, Enum):
    """Deterministic strike-selection policies."""

    ATM = "at_the_money"
    """Nearest strike to the spot price."""

    ITM = "in_the_money"
    """Nearest in-the-money strike (further from spot for the direction)."""

    OTM = "out_of_the_money"
    """Nearest out-of-the-money strike (further from spot for the direction)."""


class ExpirySelectionPolicy(str, Enum):
    """Deterministic expiry-selection policies."""

    NEAREST = "nearest"
    """Nearest non-expired expiry to the as_of date."""

    NEXT = "next"
    """Second nearest non-expired expiry."""


class OptionsContractSelection(BaseModel):
    """Typed representation of a selected single-leg options contract.

    This is the bridge between a directional decision (bullish/bearish, BUY/SELL)
    and a concrete, repository-resolved ``Instrument``. It carries both the
    *criteria* used to select the contract and the *identity* of the resolved
    instrument.

    The canonical identity is ``instrument_id`` (the ``Instrument.contract_id``).
    The other fields are denormalized projections of the same ``Instrument`` so
    the selection can be serialized and displayed without requiring the full
    instrument registry at every layer.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # --- Identity ---
    underlying: str = Field(..., min_length=1, description="Underlying symbol, e.g. 'NIFTY'")
    option_type: str = Field(..., min_length=1, description="Option right: 'CE' or 'PE'")
    strike: float = Field(..., gt=0, description="Strike price")
    expiry: str = Field(..., min_length=1, description="ISO date YYYY-MM-DD")
    instrument_type: str = Field(
        ..., description="InstrumentType value: 'option_ce' or 'option_pe'"
    )
    exchange: str = Field(default="NFO", description="Exchange: NFO, NSE, etc.")

    # --- Resolved instrument identity (canonical) ---
    # The Instrument.contract_id — stable, unique, canonical.
    instrument_id: Optional[str] = Field(
        default=None,
        description="Resolved Instrument.contract_id. None if not yet resolved from the repository.",
    )

    # The full internal symbol key (e.g. "NFO:NIFTY25DEC24800CE").
    symbol: Optional[str] = Field(
        default=None,
        description="InternalSymbol key for the resolved contract.",
    )

    @field_validator("option_type")
    @classmethod
    def _validate_option_type(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in ("CE", "PE"):
            raise ValueError(f"option_type must be 'CE' or 'PE', got {v!r}")
        return v

    @field_validator("instrument_type")
    @classmethod
    def _validate_instrument_type(cls, v: str) -> str:
        if v not in (InstrumentType.OPTION_CE.value, InstrumentType.OPTION_PE.value):
            raise ValueError(
                f"instrument_type must be '{InstrumentType.OPTION_CE.value}' "
                f"or '{InstrumentType.OPTION_PE.value}', got {v!r}"
            )
        return v

    @field_validator("expiry")
    @classmethod
    def _validate_expiry(cls, v: str) -> str:
        d = date.fromisoformat(v)
        return d.isoformat()

    @model_validator(mode="after")
    def _cross_check(self) -> "OptionsContractSelection":
        ot = self.option_type
        expected_itype = (
            InstrumentType.OPTION_CE.value if ot == "CE" else InstrumentType.OPTION_PE.value
        )
        if self.instrument_type != expected_itype:
            raise ValueError(
                f"instrument_type {self.instrument_type!r} must match "
                f"option_type {ot!r} (expected {expected_itype!r})"
            )
        return self

    @property
    def contract_id(self) -> str:
        """Stable, canonical identity — matches ``Instrument.contract_id``."""
        if self.instrument_id:
            return self.instrument_id
        exchange = self.exchange
        return f"{exchange}:{self.underlying}|{self.expiry}|{int(self.strike)}|{self.option_type}"

    @classmethod
    def from_instrument(cls, instr: Instrument) -> "OptionsContractSelection":
        """Build a selection from a resolved Instrument."""
        if instr.instrument_type not in (
            InstrumentType.OPTION_CE,
            InstrumentType.OPTION_PE,
        ):
            raise ValueError(
                f"Instrument must be an option, got {instr.instrument_type}"
            )
        ot = (
            instr.option_type
            or (
                "CE"
                if instr.instrument_type == InstrumentType.OPTION_CE
                else "PE"
            )
        ).upper()
        return cls(
            underlying=instr.underlying or "",
            option_type=ot,
            strike=float(instr.strike or 0.0),
            expiry=instr.expiry or "",
            instrument_type=instr.instrument_type.value,
            exchange=instr.internal.exchange,
            instrument_id=instr.contract_id,
            symbol=instr.key,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "option_type": self.option_type,
            "strike": self.strike,
            "expiry": self.expiry,
            "instrument_type": self.instrument_type,
            "exchange": self.exchange,
            "instrument_id": self.instrument_id,
            "symbol": self.symbol,
            "contract_id": self.contract_id,
        }
