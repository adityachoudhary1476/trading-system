"""Strict StrategySpec validation (Phase 13, Step 5).

Two layers:

1. STRUCTURAL — enforced by pydantic at parse time (spec.py + dsl.py): unknown
   indicators, unsupported operators, malformed/nested conditions, code-like
   strings, invalid numerics, impossible stops/TPs, invalid position sizes,
   invalid long/short configuration, unknown fields. A malformed document can
   never even become a StrategySpec object.

2. SEMANTIC — :func:`validate_spec` cross-checks a *parsed* spec:
   * every indicator referenced by a condition resolves to a declared indicator;
   * indicator params are within registry bounds (re-checked, defense in depth);
   * the timeframe is one the research layer understands;
   * optionally, the spec matches a dataset (symbol/timeframe) and the dataset
     has enough bars for the declared indicators (insufficient historical data).

Errors are returned as a list of specific, human-readable strings so a caller
(and the AI) can see exactly what was wrong. ``require_valid`` raises
:class:`StrategyValidationError` carrying all messages.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ...analysis.quant import TRADING_PERIODS
from ..dataset import HistoricalDataset
from .dsl import (
    collect_indicator_refs,
    is_known_indicator,
    validate_indicator_params,
    warmup_bars_for,
)
from .spec import StrategySpec

# The largest warmup any single declared indicator may demand.
MAX_WARMUP_BARS = 500


@dataclass
class StrategyValidationError(ValueError):
    """Raised when a spec fails semantic validation (all messages attached)."""

    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return "StrategySpec validation failed: " + "; ".join(self.errors)


def _max_warmup(spec: StrategySpec) -> tuple[int, str]:
    """(max warmup across declared indicators, key of that indicator)."""
    worst, worst_key = 0, ""
    for ind in spec.indicators:
        w = warmup_bars_for(ind.name.value, ind.params)
        if w > worst:
            worst, worst_key = w, ind.key
    return worst, worst_key


def validate_spec(
    spec: StrategySpec,
    dataset: Optional[HistoricalDataset] = None,
    *,
    allowed_symbols: Optional[set[str]] = None,
    min_bars: int = 0,
) -> list[str]:
    """Semantic validation of a parsed StrategySpec. Returns a list of errors.

    An empty list means the spec is valid for the (optional) dataset.
    """
    errors: list[str] = []

    # --- indicator declarations re-checked against the registry -----------
    for ind in spec.indicators:
        name = ind.name.value
        if not is_known_indicator(name):
            # pydantic already guarantees this; re-check for defense in depth.
            errors.append(f"unknown indicator {name!r}")
            continue
        try:
            validate_indicator_params(name, ind.params)
        except ValueError as e:
            errors.append(f"indicator {ind.key!r}: {e}")

    # --- every referenced operand resolves to a declared indicator --------
    for cond_name, cond in (
        ("entry", spec.entry),
        ("entry_short", spec.entry_short),
        ("exit", spec.exit),
    ):
        if cond is None:
            continue
        for ref in collect_indicator_refs(cond):
            try:
                spec.resolve_indicator_key(ref)
            except ValueError as e:
                errors.append(f"{cond_name}: {e}")

    # --- direction sanity (defense in depth; pydantic also checks) --------
    if spec.risk.allow_short and not spec.entry_short:
        errors.append("allow_short is true but entry_short condition is missing")

    # --- timeframe known to the research layer ----------------------------
    if spec.timeframe not in TRADING_PERIODS:
        errors.append(
            f"timeframe {spec.timeframe!r} is not supported "
            f"(supported: {sorted(TRADING_PERIODS)})"
        )

    # --- instrument allowlist ----------------------------------------------
    if allowed_symbols is not None and spec.symbol not in allowed_symbols:
        errors.append(
            f"symbol {spec.symbol!r} is not in the allowed instrument set "
            f"({len(allowed_symbols)} symbols)"
        )

    # --- dataset consistency + sufficiency ---------------------------------
    if dataset is not None:
        if dataset.symbol != spec.symbol:
            errors.append(
                f"spec symbol {spec.symbol!r} does not match dataset symbol "
                f"{dataset.symbol!r}"
            )
        if dataset.timeframe != spec.timeframe:
            errors.append(
                f"spec timeframe {spec.timeframe!r} does not match dataset "
                f"timeframe {dataset.timeframe!r}"
            )
        rows = 0 if dataset.data is None else len(dataset.data)
        if rows < max(min_bars, 2):
            errors.append(
                f"insufficient historical data: {rows} rows available, "
                f"{max(min_bars, 2)} required"
            )
        worst, worst_key = _max_warmup(spec)
        if worst > MAX_WARMUP_BARS:
            errors.append(
                f"indicator {worst_key!r} needs {worst} warmup bars, which exceeds "
                f"the maximum allowed ({MAX_WARMUP_BARS})"
            )
        elif rows and worst >= rows:
            errors.append(
                f"insufficient historical data for indicator {worst_key!r}: "
                f"needs {worst} bars, dataset has {rows}"
            )

    return errors


def require_valid(
    spec: StrategySpec,
    dataset: Optional[HistoricalDataset] = None,
    *,
    allowed_symbols: Optional[set[str]] = None,
    min_bars: int = 0,
) -> None:
    """Raise StrategyValidationError if the spec has any semantic errors."""
    errors = validate_spec(
        spec, dataset, allowed_symbols=allowed_symbols, min_bars=min_bars
    )
    if errors:
        raise StrategyValidationError(errors=errors)


def required_warmup_bars(spec: StrategySpec) -> int:
    """Maximum indicator warm-up (in bars) any declared indicator in ``spec`` needs.

    Used by the walk-forward layer to guarantee a fold's warm-up context is large
    enough for every indicator the spec may evaluate (Phase 14).
    """
    worst, _ = _max_warmup(spec)
    return worst

