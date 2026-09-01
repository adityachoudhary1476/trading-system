"""StrategySpec — the ONLY structure an AI may use to propose a strategy.

A StrategySpec is a strictly validated, serializable, declarative description of
a research strategy. It contains NO code and CANNOT contain code: every field is
a constrained pydantic model, unknown fields are rejected, and free-text fields
are scanned for code-like payloads. The AI never produces Python; it produces
this document, and the deterministic interpreter (interpreter.py) decides what
it means.

Serialization: ``model_dump(mode="json")`` / ``StrategySpec.model_validate_json``
round-trip losslessly (covered by tests).

Safety invariants (enforced here and re-checked in validation.py):
  * extra="forbid" — unknown fields (including anything that looks like code)
    are rejected at parse time.
  * name / description / provenance strings are length- and charset-bounded and
    scanned for executable-looking payloads (import/eval/exec/dunder/lambda/...).
  * numeric parameters are bounded; stops/TPs must be in (0, 1); position
    allocation must be in (0, 1]; leverage is not exposed at all.
  * long/short permissions are explicit; a short-enabled spec must carry an
    explicit short entry condition.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .dsl import (
    Condition,
    IndicatorName,
    collect_indicator_refs,
    indicator_key,
    validate_indicator_params,
)

SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_\-.]{0,31}$")
TIMEFRAME_RE = re.compile(r"^[0-9]{1,4}[smhdwM]$")

# Code-payload patterns for free-text fields. Word-shape aware so that prose
# like "clean trend execution" or "momentum is important" is allowed, while
# actual code payloads ("import os", "eval(...)", "lambda: ...") are rejected.
_FORBIDDEN_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bimport\b",           # import statements / __import__ (not "important")
        r"\beval\s*\(",          # eval(...)
        r"\bexec\s*\(",          # exec(...)
        r"\bcompile\s*\(",
        r"\bgetattr\s*\(",
        r"\bsetattr\s*\(",
        r"\bglobals\s*\(",
        r"\blocals\s*\(",
        r"\blambda\b",           # lambda expressions
        r"\bopen\s*\(",
        r"\bsubprocess\b",
        r"\bos\.system\b",
        r"\bsys\.",
        r"\bbase64\b",
        r"\bchr\s*\(",
        r"\bord\s*\(",
        r"__\w+__",              # dunder access (e.g. __import__, __builtins__)
        r"\bbytecode\b",
        r"\bmarshal\b",
    )
)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-]{0,63}$")


def assert_no_code_payload(value: str, field: str) -> str:
    """Raise ValueError if a free-text field looks like an attempted code payload."""
    for pattern in _FORBIDDEN_PATTERNS:
        match = pattern.search(value)
        if match:
            raise ValueError(
                f"{field} contains a forbidden code-like token ({match.group(0)!r}); "
                "a StrategySpec is data, not code"
            )
    return value


class IndicatorDef(BaseModel):
    """One declared indicator instance, e.g. {name: 'sma', params: {window: 20}}."""

    model_config = ConfigDict(extra="forbid")

    name: IndicatorName
    params: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_params(self) -> "IndicatorDef":
        # Validate + normalize params in place (canonical ints/floats).
        object.__setattr__(
            self, "params", validate_indicator_params(self.name.value, self.params)
        )
        return self

    @property
    def key(self) -> str:
        return indicator_key(self.name.value, self.params)


class PositionSizing(BaseModel):
    """Explicit, bounded position sizing (mapped onto RiskConfig by the engine)."""

    model_config = ConfigDict(extra="forbid")

    max_allocation_pct: float = Field(default=1.0, gt=0.0, le=1.0)
    max_position_size: Optional[float] = Field(default=None, gt=0.0)


class RiskParams(BaseModel):
    """Per-trade risk exits. All fractional values must be in (0, 1).

    A stop or take-profit of 0 (instant) or >= 1 (100% adverse move) is
    impossible/nonsensical and rejected here.
    """

    model_config = ConfigDict(extra="forbid")

    stop_loss_pct: Optional[float] = Field(default=None, gt=0.0, lt=1.0)
    take_profit_pct: Optional[float] = Field(default=None, gt=0.0, lt=1.0)
    allow_short: bool = False
    max_loss_per_trade_pct: Optional[float] = Field(default=None, gt=0.0, lt=1.0)


class StrategySpec(BaseModel):
    """Validated, serializable strategy specification (LLM-safe)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = Field(default="", max_length=2000)
    symbol: str
    timeframe: str

    # Declared indicator instances (canonical keys are derived from these).
    indicators: list[IndicatorDef] = Field(default_factory=list, max_length=12)

    # Entry applies to LONGS; entry_short applies to SHORTS; exit applies to any
    # open position. Conditions come from the controlled DSL (dsl.Condition).
    entry: Condition
    entry_short: Optional[Condition] = None
    exit: Optional[Condition] = None

    allow_long: bool = True

    position_sizing: PositionSizing = Field(default_factory=PositionSizing)
    risk: RiskParams = Field(default_factory=RiskParams)

    # Provenance / audit: which provider produced this spec.
    generated_by: str = Field(default="unknown", max_length=64)

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v: str) -> str:
        v = v.strip()
        if not _NAME_RE.fullmatch(v):
            raise ValueError(
                "name must be 1-64 chars of letters/digits/space/underscore/dash, "
                "starting with a letter or digit"
            )
        return assert_no_code_payload(v, "name")

    @field_validator("description")
    @classmethod
    def _description_ok(cls, v: str) -> str:
        return assert_no_code_payload(v, "description")

    @field_validator("symbol")
    @classmethod
    def _symbol_ok(cls, v: str) -> str:
        if not SYMBOL_RE.fullmatch(v):
            raise ValueError(f"symbol {v!r} is not a valid instrument identifier")
        return v

    @field_validator("timeframe")
    @classmethod
    def _timeframe_ok(cls, v: str) -> str:
        if not TIMEFRAME_RE.fullmatch(v):
            raise ValueError(
                f"timeframe {v!r} is invalid; expected e.g. '1m', '5m', '1h', '1d', '1w'"
            )
        return v

    @field_validator("generated_by")
    @classmethod
    def _provenance_ok(cls, v: str) -> str:
        return assert_no_code_payload(v, "generated_by")

    @model_validator(mode="after")
    def _directions_and_conditions(self) -> "StrategySpec":
        if not (self.allow_long or self.risk.allow_short):
            raise ValueError(
                "spec must allow at least one direction (allow_long / risk.allow_short)"
            )
        if self.risk.allow_short and self.entry_short is None:
            raise ValueError(
                "short trading is enabled but no 'entry_short' condition was provided"
            )
        if not self.allow_long and self.entry_short is None:
            raise ValueError(
                "long trading is disabled but no 'entry_short' condition was provided"
            )
        # Duplicate indicator keys are redundant/malformed.
        keys = [ind.key for ind in self.indicators]
        dupes = {k for k in keys if keys.count(k) > 1}
        if dupes:
            raise ValueError(f"duplicate indicator declarations: {sorted(dupes)}")
        return self

    # ------------------------------------------------------------------ #
    # Indicator reference resolution
    # ------------------------------------------------------------------ #
    def indicator_keys(self) -> list[str]:
        return [ind.key for ind in self.indicators]

    def resolve_indicator_key(self, ref: str) -> str:
        """Resolve an operand's indicator reference to a declared canonical key.

        Accepts either an exact canonical key ("sma_20") or a bare indicator
        name ("sma") when exactly one indicator of that family is declared.
        Raises ValueError otherwise (never guesses).
        """
        keys = self.indicator_keys()
        if ref in keys:
            return ref
        matches = [k for k in keys if k == ref or k.split("_")[0] == ref]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(
                f"condition references undeclared indicator {ref!r}; "
                f"declared: {keys or 'none'}"
            )
        raise ValueError(
            f"ambiguous indicator reference {ref!r}; matches {matches}. "
            "Use the full canonical key."
        )

    def referenced_indicators(self) -> list[str]:
        """Canonical keys referenced by entry/entry_short/exit conditions."""
        refs: list[str] = []
        for cond in (self.entry, self.entry_short, self.exit):
            if cond is not None:
                refs.extend(collect_indicator_refs(cond))
        resolved = [self.resolve_indicator_key(r) for r in refs]
        seen: set[str] = set()
        return [r for r in resolved if not (r in seen or seen.add(r))]

    def unused_indicators(self) -> list[str]:
        """Declared indicators never referenced by any condition."""
        used = set(self.referenced_indicators())
        return [k for k in self.indicator_keys() if k not in used]

    # ------------------------------------------------------------------ #
    # Untrusted-JSON choke point (mirrors MarketView.from_model_json)
    # ------------------------------------------------------------------ #
    @classmethod
    def from_model_json(cls, data: dict, model: str = "unknown") -> "StrategySpec":
        """Parse untrusted model JSON into a validated StrategySpec.

        Raises pydantic.ValidationError (or TypeError) on malformed/partial
        output. Single choke point that keeps arbitrary LLM text out of the
        research engine.
        """
        if not isinstance(data, dict):
            raise TypeError("model output must be a JSON object")
        data = dict(data)
        data.setdefault("generated_by", model)
        return cls(**data)

    def to_json(self) -> str:
        """Lossless JSON serialization."""
        return self.model_dump_json(indent=2)

    @classmethod
    def from_json(cls, payload: str) -> "StrategySpec":
        return cls.model_validate_json(payload)


# --------------------------------------------------------------------------- #
# JSON-ready builder helpers (used by providers / tests)
# --------------------------------------------------------------------------- #
def field_operand(field: str) -> dict:
    return {"kind": "field", "field": field}


def indicator_operand(key: str) -> dict:
    return {"kind": "indicator", "indicator": key}


def const_operand(value: float) -> dict:
    return {"kind": "constant", "constant": value}


def make_condition(left: dict, op: str, right: dict) -> dict:
    """JSON-ready comparison node."""
    return {"type": "comparison", "left": left, "op": op, "right": right}


def logic(op: str, *conditions: dict) -> dict:
    """JSON-ready AND/OR node."""
    return {"type": "logic", "op": op, "conditions": list(conditions)}


def not_(condition: dict) -> dict:
    """JSON-ready NOT node."""
    return {"type": "not", "condition": condition}


class SpecStatus(str, Enum):
    """Lifecycle of a research candidate (used by the research engine)."""

    GENERATED = "generated"
    VALIDATED = "validated"
    INVALID = "invalid"
    BACKTESTED = "backtested"
    EVALUATED = "evaluated"
    BACKTEST_FAILED = "backtest_failed"
    PROVIDER_ERROR = "provider_error"



