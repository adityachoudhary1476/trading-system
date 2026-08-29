"""Tests for ResearchUniverse / UniverseRegistry (Day 10.5)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from trading_system.research.universe import (
    ResearchUniverse, UniverseRegistry, default_universe_registry, Segment,
)


def test_default_registry_seeded_with_real_instruments():
    reg = default_universe_registry()
    basket = reg.get("DEFAULT_BASKET")
    assert basket is not None
    assert "NSE:SBIN" in basket.symbols
    assert "NSE:RELIANCE" in basket.symbols
    # Scaffold universes must NOT contain fabricated constituents.
    n50 = reg.get("NIFTY50")
    assert n50.requires_constituents is True
    assert n50.symbols == []


def test_requires_constituents_blocks_run():
    u = ResearchUniverse(name="X", symbols=[], segment=Segment.FNO.value,
                         requires_constituents=True)
    assert u.validate()  # non-empty problems list


def test_dedup_symbols():
    u = ResearchUniverse(name="Y", symbols=["NSE:A", "NSE:A", "NSE:B"])
    assert u.symbols == ["NSE:A", "NSE:B"]
    assert u.size == 2


def test_config_roundtrip():
    reg = default_universe_registry()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "unis.json"
        reg.to_config_file(p)
        reloaded = UniverseRegistry.from_config_file(p)
        assert set(reloaded.names()) == set(reg.names())
        assert reloaded.get("DEFAULT_BASKET").symbols == reg.get("DEFAULT_BASKET").symbols


def test_from_config_file_validates_no_fabrication():
    data = {
        "REAL": {"symbols": ["NSE:TCS"], "segment": "equity", "requires_constituents": False},
        "FAKE": {"symbols": ["NSE:Z1", "NSE:Z2"], "segment": "fno", "requires_constituents": True},
    }
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "u.json"
        p.write_text(json.dumps(data))
        reg = UniverseRegistry.from_config_file(p)
        assert reg.get("REAL").validate() == []
        assert reg.get("FAKE").validate()  # blocked: requires_constituents
