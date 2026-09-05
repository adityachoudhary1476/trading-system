"""Parameter schema for the Strategy Factory.

Strategies declare their configurable parameters as a declarative, serializable
schema rather than hardcoding them into constructor signatures. This schema is
machine-readable and supports:

  * UI generation (a future generator can render inputs from the schema)
  * parameter sweeps / grid search (min/max/step)
  * AI strategy generation (a generator emits a schema to be validated)
  * validation (ranges, types, enums, duplicate names)

Supported parameter types: integer, float, boolean, enum, string.

Every parameter type carries a ``default``. Parameters without a meaningful
default should use ``required=True`` (default value ignored by callers until the
parameter is supplied).
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .exceptions import StrategyValidationError


class ParameterType(str, Enum):
    """Controlled vocabulary of parameter value types."""

    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    ENUM = "enum"
    STRING = "string"


_PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class ParameterDefinition(BaseModel):
    """One declared parameter of a strategy."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: ParameterType
    description: str = Field(default="", max_length=500)
    default: Any = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    step: Optional[float] = None
    enum_values: list[str] = Field(default_factory=list)
    required: bool = False

    @field_validator("name")
    @classmethod
    def _name_shape(cls, v: str) -> str:
        if not _PARAM_NAME_RE.fullmatch(v):
            raise ValueError(
                "parameter name must be 1-64 chars of lowercase letters/digits/"
                "underscore, starting with a lowercase letter"
            )
        return v

    @field_validator("enum_values")
    @classmethod
    def _enum_unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("enum_values must not contain duplicates")
        return v

    @model_validator(mode="after")
    def _type_consistency(self) -> "ParameterDefinition":
        t = self.type
        d = self.default
        if self.required and d is None:
            return self
        has_min_max = self.minimum is not None or self.maximum is not None

        if t == ParameterType.INTEGER:
            if isinstance(d, bool) or not isinstance(d, int):
                if not (isinstance(d, float) and d.is_integer()):
                    raise ValueError("integer parameter 'default' must be an integer")
                object.__setattr__(self, "default", int(d))
            if self.minimum is not None and int(self.minimum) != self.minimum:
                raise ValueError("integer parameter 'minimum' must be an integer")
            if self.maximum is not None and int(self.maximum) != self.maximum:
                raise ValueError("integer parameter 'maximum' must be an integer")
            if self.step is not None and int(self.step) != self.step:
                raise ValueError("integer parameter 'step' must be an integer")
            if self.enum_values:
                raise ValueError("integer parameter must not declare enum_values")

        elif t == ParameterType.FLOAT:
            if isinstance(d, bool) or not isinstance(d, (int, float)):
                raise ValueError("float parameter 'default' must be a number")
            if self.enum_values:
                raise ValueError("float parameter must not declare enum_values")

        elif t == ParameterType.BOOLEAN:
            if not isinstance(d, bool):
                raise ValueError("boolean parameter 'default' must be a boolean")
            if has_min_max or self.step is not None or self.enum_values:
                raise ValueError("boolean parameter must not declare range/enum")

        elif t == ParameterType.ENUM:
            if not self.enum_values:
                raise ValueError("enum parameter requires non-empty enum_values")
            if d is not None and d not in self.enum_values:
                raise ValueError(
                    f"enum parameter 'default' {d!r} must be one of {self.enum_values}"
                )

        elif t == ParameterType.STRING:
            if d is not None and not isinstance(d, str):
                raise ValueError("string parameter 'default' must be a string")
            if has_min_max or self.step is not None or self.enum_values:
                raise ValueError("string parameter must not declare range/enum")

        if self.minimum is not None and self.maximum is not None:
            if self.minimum > self.maximum:
                raise ValueError(
                    f"minimum ({self.minimum}) must be <= maximum ({self.maximum})"
                )
        if has_min_max and d is not None:
            lo = self.minimum if self.minimum is not None else float("-inf")
            hi = self.maximum if self.maximum is not None else float("inf")
            if not (lo <= d <= hi):
                raise ValueError(
                    f"default {d!r} is outside [{self.minimum}, {self.maximum}]"
                )
        return self

    def validate_value(self, value: Any) -> Any:
        """Validate and coerce a concrete value against this definition.

        Fills in type coercion and range/enum checks. Raises
        ``StrategyValidationError`` on any mismatch.
        """
        if value is None:
            if self.required and self.default is None:
                raise StrategyValidationError(
                    errors=[f"parameter {self.name!r} is required and has no default"]
                )
            return self.default

        if self.type == ParameterType.INTEGER:
            if isinstance(value, bool) or not isinstance(value, int):
                if not (isinstance(value, float) and value.is_integer()):
                    raise StrategyValidationError(
                        errors=[f"parameter {self.name!r} must be an integer; got {value!r}"]
                    )
                value = int(value)
            value = self._in_range(value)
        elif self.type == ParameterType.FLOAT:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise StrategyValidationError(
                    errors=[f"parameter {self.name!r} must be a number; got {value!r}"]
                )
            value = self._in_range(float(value))
        elif self.type == ParameterType.BOOLEAN:
            if not isinstance(value, bool):
                raise StrategyValidationError(
                    errors=[f"parameter {self.name!r} must be a boolean; got {value!r}"]
                )
        elif self.type == ParameterType.ENUM:
            if value not in self.enum_values:
                raise StrategyValidationError(
                    errors=[
                        f"parameter {self.name!r} must be one of {self.enum_values}; "
                        f"got {value!r}"
                    ]
                )
        elif self.type == ParameterType.STRING:
            if not isinstance(value, str):
                raise StrategyValidationError(
                    errors=[f"parameter {self.name!r} must be a string; got {value!r}"]
                )
        return value

    def _in_range(self, value: Union[float, int]) -> Union[float, int]:
        if self.minimum is not None and value < self.minimum:
            raise StrategyValidationError(
                errors=[f"parameter {self.name!r} value {value} must be at or above minimum {self.minimum}"]
            )
        if self.maximum is not None and value > self.maximum:
            raise StrategyValidationError(
                errors=[f"parameter {self.name!r} value {value} must be at or below maximum {self.maximum}"]
            )
        return value

    def to_schema_dict(self) -> dict[str, Any]:
        """JSON-ready representation suitable for UI generation."""
        out: dict[str, Any] = {
            "name": self.name,
            "type": self.type.value,
            "default": self.default,
            "description": self.description,
            "required": self.required,
        }
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.step is not None:
            out["step"] = self.step
        if self.enum_values:
            out["enum_values"] = list(self.enum_values)
        return out


class ParameterSchema(BaseModel):
    """Ordered, validated collection of parameter definitions."""

    model_config = ConfigDict(extra="forbid")

    parameters: list[ParameterDefinition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> "ParameterSchema":
        names = [p.name for p in self.parameters]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate parameter names: {sorted(dupes)}")
        return self

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.parameters]

    def lookup(self, name: str) -> Optional[ParameterDefinition]:
        for p in self.parameters:
            if p.name == name:
                return p
        return None

    def defaults(self) -> dict[str, Any]:
        return {p.name: p.default for p in self.parameters}

    def validate_values(self, values: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Validate concrete parameter values, filling defaults for missing keys.

        Rejects unknown keys. Raises ``StrategyValidationError`` on any problem.
        """
        provided = dict(values) if values else {}
        out: dict[str, Any] = {}
        errors: list[str] = []

        for pdef in self.parameters:
            if pdef.name in provided:
                try:
                    out[pdef.name] = pdef.validate_value(provided.pop(pdef.name))
                except StrategyValidationError as e:
                    errors.extend(e.errors)
            else:
                out[pdef.name] = pdef.default

        unexpected = sorted(provided)
        if unexpected:
            errors.append(f"unknown parameter(s): {unexpected}")

        if errors:
            raise StrategyValidationError(errors=errors)
        return out

    def to_schema_dict(self) -> list[dict[str, Any]]:
        return [p.to_schema_dict() for p in self.parameters]

    def is_empty(self) -> bool:
        return len(self.parameters) == 0
