"""Phase 8 — Autonomous Options Contract Selection.

Deterministic, paper-only single-leg options contract selection that bridges
the gap between an equity-style directional decision and a specific resolved
option Instrument.

The canonical internal representation is the existing ``Instrument`` model
(``trading_system.india.instruments.Instrument``) and its stable ``contract_id``.
No parallel domain model is introduced.
"""
from __future__ import annotations

from trading_system.india.instruments import Instrument, InstrumentType, OptionType

__all__ = [
    "OptionDirection",
    "OptionsContractSelection",
    "StrikeSelectionPolicy",
    "ExpirySelectionPolicy",
    "OptionsContractSelector",
]


from .model import (
    OptionDirection,
    OptionsContractSelection,
    StrikeSelectionPolicy,
    ExpirySelectionPolicy,
)
from .selector import OptionsContractSelector
