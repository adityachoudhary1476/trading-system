"""Strategy metadata, family taxonomy, and versioning for the Strategy Factory.

This module defines the machine-readable, serializable description every strategy
must carry. It is the canonical metadata contract consumed by the Paper API,
the autonomous orchestrator, the frontend, the backtest engine, and the
AI strategy-generation pipeline.

Design notes
------------
* ``StrategyFamily`` is the *single* controlled family taxonomy for the
  Strategy Factory. The codebase already has two related enums,
  ``research.strategy_library.Category`` and ``research.phase22.StrategyCategory``;
  this Factory taxonomy is the canonical, extensible authority that those
  existing enums map onto, rather than a competing copy.
* ``StrategyMetadata`` is a pydantic model (``model_dump(mode="json")`` is pure
  JSON — never callables, never code). Free-text fields are scanned for
  code-payloads via the existing ``assert_no_code_payload`` helper.
* Versioning is explicit (``"1.0.0"``) so ``ema_crossover@1.0.0`` and
  ``ema_crossover@1.1.0`` are distinct, reproducible identities.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..analysis.quant import TRADING_PERIODS
from ..research.strategy_lab.spec import assert_no_code_payload
from .parameters import ParameterSchema

_STRATEGY_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class StrategyFamily(str, Enum):
    """Controlled strategy-family taxonomy.

    Adding a new family is a one-line enum addition. Existing codebase enums
    map onto these (e.g. research ``Category.TREND_FOLLOWING`` -> TREND).
    """

    TREND = "trend"
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    BREAKOUT = "breakout"
    VOLATILITY = "volatility"
    MARKET_STRUCTURE = "market_structure"


@dataclass(frozen=True)
class StrategyVersion:
    """Semantic version value object for a strategy definition.

    Stored as a string on ``StrategyMetadata`` for clean JSON serialization, but
    parsed/validated through this class so version arithmetic (bumping) and
    parse failures are handled in one place.
    """

    major: int = 0
    minor: int = 0
    patch: int = 1

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @classmethod
    def parse(cls, value: str) -> "StrategyVersion":
        if not isinstance(value, str) or not _VERSION_RE.fullmatch(value):
            raise ValueError(
                f"invalid version {value!r}; expected semantic version 'major.minor.patch'"
            )
        parts = value.split(".")
        return cls(int(parts[0]), int(parts[1]), int(parts[2]))

    def bump_major(self) -> "StrategyVersion":
        return StrategyVersion(self.major + 1, 0, 0)

    def bump_minor(self) -> "StrategyVersion":
        return StrategyVersion(self.major, self.minor + 1, 0)

    def bump_patch(self) -> "StrategyVersion":
        return StrategyVersion(self.major, self.minor, self.patch + 1)

    @property
    def tuple(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)


@dataclass(frozen=True)
class StrategyReference:
    """Canonical strategy identity: ``<strategy_id>@<version>``.

    Two strategies with the same reference are the SAME strategy version. To
    represent a different parametrization, bump the version. A separate
    ``content_hash`` (computed by the registry) pins the exact metadata+parameter
    payload for reproducibility audits.
    """

    strategy_id: str
    version: str

    def __str__(self) -> str:
        return f"{self.strategy_id}@{self.version}"

    @classmethod
    def parse(cls, reference: str) -> "StrategyReference":
        if not isinstance(reference, str) or "@" not in reference:
            raise ValueError(
                f"invalid strategy reference {reference!r}; expected 'id@version'"
            )
        sid, _, ver = reference.rpartition("@")
        if not sid or not ver:
            raise ValueError(
                f"invalid strategy reference {reference!r}; expected 'id@version'"
            )
        StrategyVersion.parse(ver)
        return cls(sid, ver)


class StrategyMetadata(BaseModel):
    """Machine-readable, serializable strategy description.

    This is metadata only — it carries NO executable code and contains NO
    reference to a broker, database, or network. It describes *what* a strategy
    is so it can be discovered, validated, and dispatched.
    """

    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    name: str
    version: str
    family: StrategyFamily
    description: str = Field(default="", max_length=2000)
    timeframes: list[str] = Field(default_factory=list)
    supported_instruments: list[str] = Field(default_factory=list)
    required_data: list[str] = Field(default_factory=list)
    required_indicators: list[str] = Field(default_factory=list)
    parameter_schema: ParameterSchema = Field(default_factory=ParameterSchema)
    author: str = ""
    tags: list[str] = Field(default_factory=list)
    long_short_support: bool = False
    intraday: bool = False
    requires_volume: bool = False
    requires_ohlcv: bool = True
    minimum_history: int = Field(default=0, ge=0)
    provenance: str = ""

    @field_validator("strategy_id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not _STRATEGY_ID_RE.fullmatch(v):
            raise ValueError(
                "strategy_id must be 1-64 chars of lowercase letters/digits/"
                "underscore, starting with a letter"
            )
        return v

    @field_validator("name")
    @classmethod
    def _name_shape(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 128:
            raise ValueError("name must be 1-128 chars")
        return v

    @field_validator("version")
    @classmethod
    def _version_shape(cls, v: str) -> str:
        StrategyVersion.parse(v)
        return v

    @field_validator("timeframes")
    @classmethod
    def _timeframes_known(cls, v: list[str]) -> list[str]:
        for tf in v:
            if tf not in TRADING_PERIODS:
                raise ValueError(
                    f"timeframe {tf!r} is not supported "
                    f"(supported: {sorted(TRADING_PERIODS)})"
                )
        return v

    @field_validator("tags")
    @classmethod
    def _tags_shape(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        for t in v:
            if not isinstance(t, str) or not t.strip():
                raise ValueError("tags must be non-empty strings")
            if t in seen:
                raise ValueError(f"duplicate tag {t!r}")
            seen.add(t)
        return v

    @model_validator(mode="after")
    def _scan_free_text(self) -> "StrategyMetadata":
        for field_name in ("description", "author", "provenance"):
            value = self.__dict__.get(field_name, "")
            if isinstance(value, str) and value:
                assert_no_code_payload(value, field_name)
        return self

    @property
    def reference(self) -> StrategyReference:
        return StrategyReference(self.strategy_id, self.version)

    def content_hash(self) -> str:
        """Deterministic hash of the metadata payload (excludes identity fields).

        Independent of dict ordering / whitespace. Used for reproducibility
        audits: a backtest records the exact definition that produced it.
        """
        import hashlib
        import json

        payload = self.model_dump(mode="json", exclude={"strategy_id", "version"})
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:64]

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


__all__ = [
    "StrategyFamily",
    "StrategyVersion",
    "StrategyReference",
    "StrategyMetadata",
]
