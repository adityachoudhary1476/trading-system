"""Deterministic configuration fingerprinting and stable contract serialization.

* ``configuration_fingerprint`` -- a deterministic identity for a strategy
  configuration derived from canonicalized JSON of
  ``{strategy_id, version, normalized parameters}``. Uses the repository's
  sha256 convention (``json.dumps(..., sort_keys=True, default=str)`` +
  ``hashlib.sha256``), matching ``research.evidence.strategy_identity``.

* ``contract_schema`` -- a stable, JSON-safe, API-ready serialization of a
  strategy's contract (id/version/family/name/description/timeframes/data/
  indicators/bars + per-parameter schema).

* ``strategy_identity`` -- canonical machine-readable identity for a strategy
  (derived from strategy_id + version only; excludes parameters).

  Both configuration functions are pure: they do not mutate their input and
  contain no network, broker, or execution logic.
  """
from __future__ import annotations

import hashlib
import json
from typing import Any

from .metadata import StrategyMetadata
from .parameters import ParameterSchema


def _require_metadata(strategy: Any) -> StrategyMetadata:
    """Return ``strategy.metadata`` if it is a ``StrategyMetadata``, else raise."""
    meta = strategy.metadata
    if not isinstance(meta, StrategyMetadata):
        raise TypeError("strategy.metadata must be a StrategyMetadata instance")
    return meta


def _normalize_params(params: Any, schema: Any) -> dict[str, Any]:
    """Return a canonical parameter dict for fingerprinting.

    Schema-backed strategies are normalized through
    ``ParameterSchema.validate_values`` so semantically-equivalent
    representations (e.g. integer ``12`` vs float ``12.0``) collapse before
    hashing. Unparameterized strategies are canonicalized via a *strict* stable
    JSON round-trip (tuple/list equivalence, key ordering, nested dict
    normalization). Unsupported/arbitrary objects fail explicitly and
    deterministically -- they are never stringified into an unstable
    (e.g. address-bearing) hash.
    """
    if not isinstance(params, dict):
        raise TypeError("strategy.parameters must be a dict")
    if isinstance(schema, ParameterSchema) and not schema.is_empty():
        return schema.validate_values(dict(params))
    # Strict canonicalization: reject non-JSON-native values rather than
    # silently stringifying them (which can embed memory addresses).
    try:
        return json.loads(json.dumps(dict(params), sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise TypeError(f"unsupported parameter type in strategy parameters: {exc}") from exc


def strategy_identity(strategy: Any) -> str:
    """Canonical machine-readable identity for a *strategy* (not a configuration).

    Deterministically derived from ``strategy_id`` and ``version`` only:

      * stable across processes
      * stable across dictionary ordering
      * independent of parameter values
      * independent of memory address, filesystem paths, timestamps, and
        environment-dependent values
      * no random UUIDs

    Renaming a parameter value or binding a different configuration produces a
    *different* :func:`configuration_fingerprint` but the *same*
    :func:`strategy_identity`. The latter identifies the strategy definition;
    the former identifies a concrete configuration.
    """
    meta = _require_metadata(strategy)
    payload = {"strategy_id": meta.strategy_id, "version": meta.version}
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:64]


def configuration_fingerprint(strategy: Any) -> str:
    """Deterministic fingerprint of a strategy's configuration.

    Derived from ``strategy_id``, ``version``, and **normalized** parameters
    (canonicalized through ``ParameterSchema.validate_values`` so that equivalent
    representations -- e.g. integer ``12`` vs float ``12.0`` -- collapse to the
    same value before hashing). Dictionary insertion order does not affect the
    result (``sort_keys=True``).

    The result is a 64-char sha256 hex digest; it never embeds memory addresses,
    timestamps, random values, or environment-specific data, and it contains no
    secrets or credentials.
    """
    meta = _require_metadata(strategy)
    params = strategy.parameters
    normalized = _normalize_params(params, meta.parameter_schema)
    payload = {
        "strategy_id": meta.strategy_id,
        "version": meta.version,
        "parameters": normalized,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:64]


def contract_schema(strategy: Any) -> dict[str, Any]:
    """Stable, JSON-safe serialization of a strategy's contract for API consumption.

    Deterministic key ordering; no Python class objects, memory addresses, or
    callables. Does not mutate the source model.
    """
    meta = _require_metadata(strategy)
    family_val = meta.family.value if hasattr(meta.family, "value") else meta.family
    parameters: dict[str, Any] = {}
    for pdef in meta.parameter_schema.parameters:
        entry: dict[str, Any] = {
            "type": pdef.type.value,
            "required": pdef.required,
        }
        if pdef.default is not None:
            entry["default"] = pdef.default
        if pdef.minimum is not None:
            entry["minimum"] = pdef.minimum
        if pdef.maximum is not None:
            entry["maximum"] = pdef.maximum
        if pdef.step is not None:
            entry["step"] = pdef.step
        if pdef.enum_values:
            entry["enum_values"] = list(pdef.enum_values)
        if pdef.description:
            entry["description"] = pdef.description
        parameters[pdef.name] = entry
    required_data = [
        item.value if hasattr(item, "value") else str(item)
        for item in meta.required_data
    ]
    return {
        "strategy_id": meta.strategy_id,
        "version": meta.version,
        "name": meta.name,
        "family": family_val,
        "description": meta.description,
        "timeframes": list(meta.timeframes),
        "supported_instruments": list(meta.supported_instruments),
        "minimum_bars": meta.minimum_history,
        "required_data": required_data,
        "required_indicators": list(meta.required_indicators),
        "parameters": parameters,
    }


__all__ = [
    "strategy_identity",
    "configuration_fingerprint",
    "contract_schema",
]