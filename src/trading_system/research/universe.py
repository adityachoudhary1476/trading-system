"""Research data universe (Day 10.5) — configurable, no fabricated constituents.

A `ResearchUniverse` is a NAMED, EXPLICIT basket of instruments for bulk backfill /
coverage reporting. Constituents are supplied via configuration (a JSON file or an
in-memory list) — they are NEVER invented by business logic.

If you need NIFTY50 / NIFTY100 / LIQUID_FNO official constituents, paste the official
constituent list into a universe config; this module will not guess them. The shipped
`universes.example.json` contains only the instruments already present in the
`InstrumentRegistry` defaults (safe, real, known symbols) plus clearly-marked scaffold
entries you must populate.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from ..india.instruments import InstrumentRegistry, DEFAULT_INSTRUMENTS, InstrumentType


class Segment(str, Enum):
    EQUITY = "equity"
    INDEX = "index"
    FNO = "fno"          # NSE futures & options
    MCX = "mcx"         # commodities
    CDS = "cds"         # currency


@dataclass
class ResearchUniverse:
    name: str
    symbols: list[str]
    segment: str = Segment.EQUITY.value
    description: str = ""
    # When True, the symbol list is a placeholder and must be populated before use.
    requires_constituents: bool = False

    def __post_init__(self) -> None:
        # De-duplicate while preserving order.
        seen: set[str] = set()
        self.symbols = [s for s in self.symbols if not (s in seen or seen.add(s))]

    @property
    def size(self) -> int:
        return len(self.symbols)

    def validate(self) -> list[str]:
        """Return a list of problems (empty if OK)."""
        problems: list[str] = []
        if not self.symbols:
            problems.append(f"universe {self.name!r} has no symbols")
        if self.requires_constituents:
            problems.append(
                f"universe {self.name!r} requires official constituents before use"
            )
        return problems


class UniverseRegistry:
    """Holds research universes. Loads from an explicit config file (no guessing)."""

    def __init__(self, universes: Optional[dict[str, ResearchUniverse]] = None) -> None:
        self._universes: dict[str, ResearchUniverse] = dict(universes or {})

    def register(self, u: ResearchUniverse) -> None:
        self._universes[u.name] = u

    def get(self, name: str) -> Optional[ResearchUniverse]:
        return self._universes.get(name)

    def names(self) -> list[str]:
        return list(self._universes.keys())

    @classmethod
    def from_config_file(cls, path: str | Path) -> "UniverseRegistry":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"universe config not found: {p}")
        raw = json.loads(p.read_text(encoding="utf-8"))
        reg = cls()
        for name, spec in raw.items():
            reg.register(ResearchUniverse(
                name=name,
                symbols=list(spec.get("symbols", [])),
                segment=spec.get("segment", Segment.EQUITY.value),
                description=spec.get("description", ""),
                requires_constituents=bool(spec.get("requires_constituents", False)),
            ))
        return reg

    def to_config_file(self, path: str | Path) -> None:
        out = {
            name: {
                "symbols": u.symbols,
                "segment": u.segment,
                "description": u.description,
                "requires_constituents": u.requires_constituents,
            }
            for name, u in self._universes.items()
        }
        Path(path).write_text(json.dumps(out, indent=2), encoding="utf-8")


def default_universe_registry() -> UniverseRegistry:
    """A registry seeded ONLY with the real instruments already in the registry.

    No NIFTY50/NIFTY100 constituents are fabricated here. Callers that need those
    must load a config file populated with the official list.
    """
    reg = UniverseRegistry()
    reg.register(ResearchUniverse(
        name="DEFAULT_BASKET",
        symbols=[i.key for i in DEFAULT_INSTRUMENTS if i.instrument_type == InstrumentType.EQUITY],
        segment=Segment.EQUITY.value,
        description="Equity instruments already present in the InstrumentRegistry defaults.",
    ))
    reg.register(ResearchUniverse(
        name="INDICES",
        symbols=[i.key for i in DEFAULT_INSTRUMENTS if i.instrument_type == InstrumentType.INDEX],
        segment=Segment.INDEX.value,
        description="Index instruments in the registry defaults.",
    ))
    # Scaffolds that MUST be populated with the official constituent list before use.
    reg.register(ResearchUniverse(
        name="NIFTY50", symbols=[], segment=Segment.FNO.value,
        description="NIFTY 50 constituents — populate from official NSE list.",
        requires_constituents=True,
    ))
    reg.register(ResearchUniverse(
        name="NIFTY100", symbols=[], segment=Segment.FNO.value,
        description="NIFTY 100 constituents — populate from official NSE list.",
        requires_constituents=True,
    ))
    reg.register(ResearchUniverse(
        name="LIQUID_FNO", symbols=[], segment=Segment.FNO.value,
        description="Liquid F&O underlyings — populate from official NSE F&O list.",
        requires_constituents=True,
    ))
    reg.register(ResearchUniverse(
        name="MCX_RESEARCH", symbols=[], segment=Segment.MCX.value,
        description="MCX liquid commodities — populate from official MCX list.",
        requires_constituents=True,
    ))
    return reg
