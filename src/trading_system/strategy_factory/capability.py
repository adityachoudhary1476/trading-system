"""Capability declarations and compatibility checking for the Strategy Factory.

A strategy's *capabilities* are what it requires to operate: the market-data
granularity it consumes, the timeframes it is valid for, and the minimum history
it needs. This module provides a typed, validated, serializable view over the
existing ``StrategyMetadata`` fields and a pure, side-effect-free compatibility
checker.

This layer describes **what data a strategy requires**, never where that data
comes from. It is intentionally independent of any broker, market-data client,
or execution layer (-- no network, no broker, no live data fetching).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..analysis.quant import TRADING_PERIODS
from .metadata import StrategyMetadata


class DataRequirement(str, Enum):
    """Canonical market-data requirement a strategy needs to evaluate."""

    OHLCV = "ohlcv"
    OHLC = "ohlc"
    PRICE = "price"
    VOLUME = "volume"


def _coerce_data(values: Any) -> list[DataRequirement]:
    """Normalize a sequence of data requirements into ``DataRequirement`` values."""
    if not values:
        return []
    out: list[DataRequirement] = []
    for item in values:
        if isinstance(item, DataRequirement):
            out.append(item)
        else:
            out.append(DataRequirement(item))
    return out


def _normalize_str_list(values: Any) -> list[str]:
    """Strip/dedup/sort a sequence of strings into a canonical list."""
    if not values:
        return []
    names = [str(x).strip() for x in values]
    names = [n for n in names if n]
    return sorted(set(names))


class Capabilities(BaseModel):
    """Typed, validated, serializable declaration of a strategy's requirements.

    Derived from (not duplicating) ``StrategyMetadata``. A strategy requires:
      * ``required_data``    -- market-data bundles it consumes (typed enum).
      * ``required_indicators`` -- indicator names it consumes (normalized).
      * ``supported_timeframes`` -- timeframes it is valid for (validated set).
      * ``minimum_bars``     -- minimum historical bars before evaluation.
    """

    model_config = ConfigDict(extra="forbid")

    required_data: list[DataRequirement] = Field(default_factory=list)
    required_indicators: list[str] = Field(default_factory=list)
    supported_timeframes: list[str] = Field(default_factory=list)
    minimum_bars: int = Field(default=0, ge=0)

    @field_validator("required_data", mode="before")
    @classmethod
    def _coerce_required_data(cls, v: Any) -> list[DataRequirement]:
        return _coerce_data(v)

    @field_validator("required_indicators", mode="before")
    @classmethod
    def _normalize_required_indicators(cls, v: Any) -> list[str]:
        return _normalize_str_list(v)

    @field_validator("supported_timeframes", mode="before")
    @classmethod
    def _validate_supported_timeframes(cls, v: Any) -> list[str]:
        frames = _normalize_str_list(v)
        unknown = [t for t in frames if t not in TRADING_PERIODS]
        if unknown:
            raise ValueError(
                f"unsupported timeframe(s): {unknown}; supported: {sorted(TRADING_PERIODS)}"
            )
        return frames

    @classmethod
    def from_metadata(cls, meta: StrategyMetadata) -> "Capabilities":
        """Build the capability view from a ``StrategyMetadata`` instance."""
        return cls(
            required_data=list(meta.required_data),
            required_indicators=list(meta.required_indicators),
            supported_timeframes=list(meta.timeframes),
            minimum_bars=int(meta.minimum_history),
        )

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class MarketContext(BaseModel):
    """What a market/environment supplies for a compatibility check.

    Supplied by the caller (the environment, not the strategy); never fetched by
    the Factory. ``instrument`` and ``timeframe`` identify the context,
    ``bar_count`` is the number of closed bars available, and ``data_available``
    is the data the environment can provide.
    """

    model_config = ConfigDict(extra="forbid")

    instrument: str
    timeframe: str
    bar_count: int = Field(default=0, ge=0)
    data_available: list[DataRequirement] = Field(default_factory=list)

    @field_validator("instrument")
    @classmethod
    def _instrument_ok(cls, v: str) -> str:
        s = str(v).strip()
        if not s:
            raise ValueError("instrument must be a non-empty identifier")
        return s

    @field_validator("timeframe")
    @classmethod
    def _timeframe_ok(cls, v: str) -> str:
        s = str(v).strip()
        if s not in TRADING_PERIODS:
            raise ValueError(
                f"timeframe {s!r} is not a recognised market timeframe; "
                f"supported: {sorted(TRADING_PERIODS)}"
            )
        return s

    @field_validator("data_available", mode="before")
    @classmethod
    def _coerce_data_available(cls, v: Any) -> list[DataRequirement]:
        return _coerce_data(v)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


@dataclass(frozen=True)
class CompatibilityReport:
    """Result of a capability compatibility check.

    ``compatible`` is True when there are no reasons; ``reasons`` lists every
    unmet requirement (empty when compatible).
    """

    compatible: bool
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"compatible": self.compatible, "reasons": list(self.reasons)}


CapabilitySource = Union[Capabilities, StrategyMetadata, Any]


def capabilities_from(source: CapabilitySource) -> Capabilities:
    """Extract typed ``Capabilities`` from a strategy, metadata, or Capabilities."""
    if isinstance(source, Capabilities):
        return source
    if isinstance(source, StrategyMetadata):
        return Capabilities.from_metadata(source)
    if hasattr(source, "metadata"):
        return Capabilities.from_metadata(source.metadata)
    raise TypeError(
        "cannot derive Capabilities from an object without StrategyMetadata; "
        "expected a Strategy, StrategyMetadata, or Capabilities"
    )


def is_compatible(
    strategy_or_capabilities: CapabilitySource,
    market_context: Union[MarketContext, dict, Any],
) -> CompatibilityReport:
    """Pure, side-effect-free compatibility check between a strategy and a context.

    A strategy is compatible when, for the supplied ``MarketContext``:
      * every required data bundle is available;
      * the context timeframe is within the strategy's supported timeframes;
      * the available bar count meets the strategy's minimum history.
    """
    caps = capabilities_from(strategy_or_capabilities)
    if isinstance(market_context, MarketContext):
        ctx = market_context
    elif isinstance(market_context, dict):
        ctx = MarketContext(**market_context)
    else:
        raise TypeError("market_context must be a MarketContext or a dict of its fields")

    reasons: list[str] = []

    required = set(caps.required_data)
    available = set(ctx.data_available)
    missing = required - available
    if missing:
        names = sorted(req.value for req in missing)
        reasons.append(f"requires data {names} not available in context")

    if ctx.timeframe not in caps.supported_timeframes:
        reasons.append(
            f"timeframe {ctx.timeframe!r} not supported by strategy "
            f"(supported: {caps.supported_timeframes})"
        )

    if ctx.bar_count < caps.minimum_bars:
        reasons.append(
            f"insufficient history: strategy requires >= {caps.minimum_bars} bars, "
            f"context has {ctx.bar_count}"
        )

    return CompatibilityReport(compatible=not reasons, reasons=reasons)


__all__ = [
    "DataRequirement",
    "Capabilities",
    "MarketContext",
    "CompatibilityReport",
    "is_compatible",
    "capabilities_from",
]