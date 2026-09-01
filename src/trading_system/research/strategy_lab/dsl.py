"""Controlled strategy DSL for AI-proposed strategies (Phase 13).

This module defines the ONLY vocabulary an AI (or human) may use to express a
research strategy. Every primitive is deterministically evaluable from
historical OHLCV bars, so a specification can never encode hidden behavior:

Values    : open / high / low / close / volume fields, declared indicators,
            numeric constants.
Operators : >, <, >=, <=, ==, crosses_above, crosses_below.
Logic     : AND, OR, NOT.

There is deliberately NO arithmetic between series, NO function calls, NO
references to anything outside this registry, and NO way to express code.
Anything not enumerated here fails validation (see spec.py / validation.py).

Indicator reuse policy: this DSL exposes ONLY indicators already implemented in
``trading_system.indicators`` (pure, causal). No new indicator math is invented
here — the interpreter simply calls the existing functions.
"""
from __future__ import annotations

import math
import re
from enum import Enum
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# A canonical indicator reference, e.g. "sma_20", "macd_12_26_9", "bb_upper_20_2".
INDICATOR_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class PriceField(str, Enum):
    """Raw OHLCV columns available as operands."""

    OPEN = "open"
    HIGH = "high"
    LOW = "low"
    CLOSE = "close"
    VOLUME = "volume"


class OperandKind(str, Enum):
    FIELD = "field"
    INDICATOR = "indicator"
    CONSTANT = "constant"


class Operand(BaseModel):
    """One side of a comparison: a price field, an indicator, or a constant."""

    model_config = ConfigDict(extra="forbid")

    kind: OperandKind
    field: Optional[PriceField] = None
    indicator: Optional[str] = None
    constant: Optional[float] = None

    @property
    def is_series(self) -> bool:
        return self.kind in (OperandKind.FIELD, OperandKind.INDICATOR)

    @field_validator("indicator")
    @classmethod
    def _indicator_key_shape(cls, v):
        if v is not None and not INDICATOR_KEY_RE.fullmatch(v):
            raise ValueError(
                f"indicator reference {v!r} is not a valid indicator key "
                "(lowercase letters/digits/underscore)"
            )
        return v

    @model_validator(mode="after")
    def _consistent(self) -> "Operand":
        if self.kind == OperandKind.FIELD and self.field is None:
            raise ValueError("field operand requires 'field' to be set")
        if self.kind == OperandKind.INDICATOR:
            if not self.indicator:
                raise ValueError("indicator operand requires 'indicator' key")
            if self.field is not None or self.constant is not None:
                raise ValueError("indicator operand must not set 'field' or 'constant'")
        if self.kind == OperandKind.CONSTANT:
            if self.constant is None:
                raise ValueError("constant operand requires 'constant' value")
            if not math.isfinite(self.constant):
                raise ValueError("constant operand must be a finite number")
            if self.field is not None or self.indicator is not None:
                raise ValueError("constant operand must not set 'field' or 'indicator'")
        return self


def close_field() -> Operand:
    return Operand(kind=OperandKind.FIELD, field=PriceField.CLOSE)


def indicator_ref(key: str) -> Operand:
    return Operand(kind=OperandKind.INDICATOR, indicator=key)


def const(value: float) -> Operand:
    return Operand(kind=OperandKind.CONSTANT, constant=float(value))


class ComparisonOp(str, Enum):
    GT = ">"
    LT = "<"
    GTE = ">="
    LTE = "<="
    EQ = "=="
    CROSSES_ABOVE = "crosses_above"
    CROSSES_BELOW = "crosses_below"


class Comparison(BaseModel):
    """left <op> right. Crosses require a series on the LEFT side."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["comparison"] = "comparison"
    left: Operand
    op: ComparisonOp
    right: Operand

    @model_validator(mode="after")
    def _crosses_need_series_left(self) -> "Comparison":
        if self.op in (ComparisonOp.CROSSES_ABOVE, ComparisonOp.CROSSES_BELOW):
            if not self.left.is_series:
                raise ValueError(
                    f"{self.op.value} requires a series (field/indicator) on the left side"
                )
            if not (self.right.is_series or self.right.kind == OperandKind.CONSTANT):
                raise ValueError(f"{self.op.value} right side must be a series or constant")
        if not self.left.is_series and not self.right.is_series:
            raise ValueError(
                "comparison between two constants is not a meaningful condition"
            )
        return self


class LogicOp(str, Enum):
    AND = "AND"
    OR = "OR"


class LogicNode(BaseModel):
    """N-ary AND / OR over sub-conditions (min 2, max 8)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["logic"] = "logic"
    op: LogicOp
    conditions: list["Condition"] = Field(min_length=2, max_length=8)


class NotNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["not"] = "not"
    condition: "Condition"


Condition = Annotated[
    Union[Comparison, LogicNode, NotNode],
    Field(discriminator="type"),
]

LogicNode.model_rebuild()
NotNode.model_rebuild()


# --------------------------------------------------------------------------- #
# Indicator registry — ONLY indicators that already exist in the codebase.
# --------------------------------------------------------------------------- #
class IndicatorName(str, Enum):
    SMA = "sma"
    EMA = "ema"
    RSI = "rsi"
    ATR = "atr"
    MACD = "macd"
    MACD_SIGNAL = "macd_signal"
    MACD_HISTOGRAM = "macd_histogram"
    BOLLINGER_UPPER = "bb_upper"
    BOLLINGER_MIDDLE = "bb_middle"
    BOLLINGER_LOWER = "bb_lower"
    MOMENTUM = "momentum"


# param -> (type, inclusive_min, inclusive_max, default)
_PARAM_SPECS: dict[str, dict[str, tuple[type, float, float, float]]] = {
    IndicatorName.SMA.value: {"window": (int, 2, 500, 20)},
    IndicatorName.EMA.value: {"window": (int, 2, 500, 12)},
    IndicatorName.RSI.value: {"window": (int, 2, 100, 14)},
    IndicatorName.ATR.value: {"window": (int, 2, 100, 14)},
    IndicatorName.MACD.value: {
        "fast": (int, 2, 200, 12), "slow": (int, 3, 500, 26), "signal": (int, 2, 200, 9),
    },
    IndicatorName.MACD_SIGNAL.value: {
        "fast": (int, 2, 200, 12), "slow": (int, 3, 500, 26), "signal": (int, 2, 200, 9),
    },
    IndicatorName.MACD_HISTOGRAM.value: {
        "fast": (int, 2, 200, 12), "slow": (int, 3, 500, 26), "signal": (int, 2, 200, 9),
    },
    IndicatorName.BOLLINGER_UPPER.value: {
        "window": (int, 2, 500, 20), "num_std": (float, 0.1, 10.0, 2.0),
    },
    IndicatorName.BOLLINGER_MIDDLE.value: {
        "window": (int, 2, 500, 20), "num_std": (float, 0.1, 10.0, 2.0),
    },
    IndicatorName.BOLLINGER_LOWER.value: {
        "window": (int, 2, 500, 20), "num_std": (float, 0.1, 10.0, 2.0),
    },
    IndicatorName.MOMENTUM.value: {"window": (int, 2, 500, 10)},
}


def param_spec(name: str) -> dict[str, tuple[type, float, float, float]]:
    """Parameter spec for an indicator name; empty dict if unknown."""
    return {k: v for k, v in _PARAM_SPECS.get(name, {}).items()}


def is_known_indicator(name: str) -> bool:
    return name in _PARAM_SPECS


def validate_indicator_params(name: str, params: dict) -> dict:
    """Validate + normalize params for an indicator; returns canonical params.

    Raises ValueError with a specific message for: unknown params, wrong types,
    out-of-range values, and structurally impossible combinations (macd fast >= slow).
    """
    if name not in _PARAM_SPECS:
        raise ValueError(f"unknown indicator {name!r}")
    spec = _PARAM_SPECS[name]
    unknown = set(params) - set(spec)
    if unknown:
        raise ValueError(
            f"indicator {name!r} does not accept parameter(s) "
            f"{sorted(unknown)}; allowed: {sorted(spec)}"
        )
    out: dict = {}
    for pname, (ptype, lo, hi, default) in spec.items():
        raw = params.get(pname, default)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"indicator {name!r} parameter {pname!r} must be a number")
        if ptype is int and int(raw) != raw:
            raise ValueError(f"indicator {name!r} parameter {pname!r} must be an integer")
        val = ptype(raw)
        if not (lo <= val <= hi):
            raise ValueError(
                f"indicator {name!r} parameter {pname!r} must be in [{lo}, {hi}]; got {raw}"
            )
        out[pname] = val
    if name in (IndicatorName.MACD.value, IndicatorName.MACD_SIGNAL.value,
                IndicatorName.MACD_HISTOGRAM.value) and out["fast"] >= out["slow"]:
        raise ValueError(
            f"indicator {name!r} requires fast < slow (got fast={out['fast']}, slow={out['slow']})"
        )
    return out


def indicator_key(name: str, params: dict) -> str:
    """Canonical, human-readable key used in condition operands.

    e.g. sma/20 -> "sma_20"; macd/12,26,9 -> "macd_12_26_9";
    bb_upper/20,2.0 -> "bb_upper_20_2" (trailing .0 stripped on floats).
    """
    parts = [name]
    for v in params.values():
        if isinstance(v, float) and v.is_integer():
            parts.append(str(int(v)))
        else:
            parts.append(str(v))
    key = "_".join(parts)
    if not INDICATOR_KEY_RE.fullmatch(key):  # pragma: no cover - registry-guarded
        raise ValueError(f"generated indicator key {key!r} is invalid")
    return key


def warmup_bars_for(name: str, params: dict) -> int:
    """Minimum bars before this indicator produces its first defined value."""
    if name in (IndicatorName.MACD.value, IndicatorName.MACD_SIGNAL.value,
                IndicatorName.MACD_HISTOGRAM.value):
        return int(params["slow"]) + int(params["signal"])
    return int(params.get("window", 0))


# --------------------------------------------------------------------------- #
# Condition tree utilities
# --------------------------------------------------------------------------- #
def iter_conditions(condition):
    """Yield every node in a condition tree (depth-first)."""
    yield condition
    if isinstance(condition, LogicNode):
        for sub in condition.conditions:
            yield from iter_conditions(sub)
    elif isinstance(condition, NotNode):
        yield from iter_conditions(condition.condition)


def collect_indicator_refs(condition) -> list[str]:
    """All indicator keys referenced anywhere in a condition tree."""
    refs: list[str] = []
    for node in iter_conditions(condition):
        if isinstance(node, Comparison):
            for side in (node.left, node.right):
                if side.kind == OperandKind.INDICATOR and side.indicator:
                    refs.append(side.indicator)
    return refs

