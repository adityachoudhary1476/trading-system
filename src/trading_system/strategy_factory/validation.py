"""Deterministic validation for the Strategy Factory.

Validation is the choke point: a strategy must pass ``require_valid_strategy``
before it is accepted by the registry. Validation is split into four
independent, independently-testable concerns:

  1. metadata         -- ``validate_metadata``
  2. parameter schema -- ``validate_parameter_schema``
  3. parameter values -- ``validate_parameter_values``
  4. signal           -- ``validate_signal``

Each returns a list of human-readable error strings (empty == valid), mirroring
the ``validate_spec`` convention in ``strategy_lab.validation``. ``require_*``
helpers raise ``StrategyValidationError`` carrying all messages.

This validation never touches a broker, network, or database.
"""
from __future__ import annotations

from typing import Any, Optional

from ..research.strategy_lab.spec import assert_no_code_payload
from .contract import Strategy, StrategySignal
from .exceptions import StrategyValidationError
from .metadata import StrategyFamily, StrategyMetadata, StrategyReference
from .parameters import ParameterSchema


def _scan_text(value: Any, field: str) -> list[str]:
    """Scan a free-text field for code-like payloads; returns error list."""
    if not isinstance(value, str) or not value:
        return []
    try:
        assert_no_code_payload(value, field)
    except ValueError as exc:
        return [str(exc)]
    return []


def validate_metadata(meta: StrategyMetadata) -> list[str]:
    """Validate a ``StrategyMetadata`` instance (deep, beyond pydantic)."""
    errors: list[str] = []

    if not isinstance(meta, StrategyMetadata):
        errors.append("metadata is not a StrategyMetadata instance")
        return errors

    try:
        StrategyReference.parse(str(meta.reference))
    except ValueError as exc:
        errors.append(f"metadata identity: {exc}")

    if not meta.name or not meta.name.strip():
        errors.append("metadata 'name' is required")
    if not meta.description or not meta.description.strip():
        errors.append("metadata 'description' is required")

    if not meta.required_data:
        errors.append("metadata 'required_data' must declare at least one data requirement")
    if not meta.required_indicators:
        errors.append("metadata 'required_indicators' must declare at least one indicator")
    if meta.minimum_history < 0:
        errors.append("metadata 'minimum_history' must be >= 0")

    errors.extend(validate_parameter_schema(meta.parameter_schema))

    allowed = {f.value for f in StrategyFamily}
    fam = meta.family.value if hasattr(meta.family, "value") else meta.family
    if fam not in allowed:
        errors.append(f"metadata family {fam!r} is not a recognised StrategyFamily")

    for field_name in ("description", "author", "provenance"):
        value = meta.__dict__.get(field_name, "")
        errors.extend(_scan_text(value, field_name))

    return errors


def validate_parameter_schema(schema: ParameterSchema) -> list[str]:
    """Validate a parameter schema's internal consistency."""
    errors: list[str] = []

    if not isinstance(schema, ParameterSchema):
        errors.append("parameter_schema is not a ParameterSchema instance")
        return errors

    names = schema.names
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        errors.append(f"duplicate parameter names: {sorted(dupes)}")

    for pdef in schema.parameters:
        if pdef.minimum is not None and pdef.maximum is not None:
            if pdef.minimum > pdef.maximum:
                errors.append(
                    f"parameter {pdef.name!r}: minimum ({pdef.minimum}) > "
                    f"maximum ({pdef.maximum})"
                )
        if pdef.step is not None and pdef.step <= 0:
            errors.append(f"parameter {pdef.name!r}: step must be > 0")

    return errors


def validate_parameter_values(
    values: Optional[dict[str, Any]], schema: ParameterSchema
) -> list[str]:
    """Validate concrete parameter values against a schema (does not mutate)."""
    errors: list[str] = []
    if values is None:
        values = {}
    if not isinstance(values, dict):
        errors.append("parameter values must be a mapping")
        return errors

    declared = set(schema.names)
    unknown = set(values.keys()) - declared
    if unknown:
        errors.append(f"unknown parameter(s): {sorted(unknown)}")

    missing_required = [
        p.name for p in schema.parameters
        if p.name not in values and p.default is None and p.required
    ]
    if missing_required:
        errors.append(
            f"required parameter(s) without value or default: {sorted(missing_required)}"
        )

    try:
        schema.validate_values(values)
    except StrategyValidationError as exc:
        errors.extend(exc.errors)

    return errors


def validate_signal(signal: StrategySignal) -> list[str]:
    """Validate a ``StrategySignal`` is well-formed and self-consistent."""
    errors: list[str] = []

    if not isinstance(signal, StrategySignal):
        errors.append("signal is not a StrategySignal instance")
        return errors

    valid_actions = {"buy", "sell", "exit", "hold"}
    action_val = signal.action.value if hasattr(signal.action, "value") else signal.action
    if action_val not in valid_actions:
        errors.append(f"signal action {signal.action!r} is not a valid SignalAction")

    if not signal.strategy_id:
        errors.append("signal 'strategy_id' is required")
    if signal.reference_price <= 0:
        errors.append("signal 'reference_price' must be > 0")
    if not (0.0 <= signal.confidence <= 1.0):
        errors.append("signal 'confidence' must be in [0.0, 1.0]")
    if signal.timestamp.tzinfo is None:
        errors.append("signal 'timestamp' must be timezone-aware")

    if signal.target_position is not None and signal.target_position not in (-1, 0, 1):
        errors.append(
            f"signal target_position {signal.target_position!r} must be in {{-1, 0, 1}}"
        )

    errors.extend(_scan_text(signal.reason, "reason"))

    return errors


def validate_strategy(strategy: Strategy) -> list[str]:
    """Validate a complete strategy instance: ABC conformance + metadata + params.

    This is the registry gate. Signal validity is checked separately via
    ``validate_signal`` since a strategy without market state cannot produce one.
    """
    errors: list[str] = []

    if not isinstance(strategy, Strategy):
        errors.append("object is not a Strategy Factory Strategy instance")
        return errors

    try:
        meta = strategy.metadata
    except Exception as exc:  # pragma: no cover - defensive
        errors.append(f"metadata access failed: {exc}")
        return errors

    if not isinstance(meta, StrategyMetadata):
        errors.append("metadata is not a StrategyMetadata instance")
        return errors

    errors.extend(validate_metadata(meta))

    try:
        params = strategy.parameters
    except Exception as exc:  # pragma: no cover - defensive
        errors.append(f"parameters access failed: {exc}")
        params = {}

    if not isinstance(params, dict):
        errors.append("parameters must be a dict")
    else:
        errors.extend(validate_parameter_values(params, meta.parameter_schema))

    return errors


def require_valid_strategy(strategy: Strategy) -> None:
    """Raise ``StrategyValidationError`` if the strategy is invalid."""
    errors = validate_strategy(strategy)
    if errors:
        raise StrategyValidationError(errors=errors)


def require_valid_metadata(meta: StrategyMetadata) -> None:
    errors = validate_metadata(meta)
    if errors:
        raise StrategyValidationError(errors=errors)


def require_valid_signal(signal: StrategySignal) -> None:
    errors = validate_signal(signal)
    if errors:
        raise StrategyValidationError(errors=errors)


__all__ = [
    "validate_metadata",
    "validate_parameter_schema",
    "validate_parameter_values",
    "validate_signal",
    "validate_strategy",
    "require_valid_strategy",
    "require_valid_metadata",
    "require_valid_signal",
]