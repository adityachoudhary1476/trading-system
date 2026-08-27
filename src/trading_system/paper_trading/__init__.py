"""Paper trading. NOT implemented on Day 1 (no execution).

Reserved for a simulated broker that consumes signals + risk approvals and
tracks a virtual portfolio. Placeholder only; no live or simulated orders yet.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Position:
    symbol: str
    qty: float
    entry_price: float
    current_price: float = 0.0


@dataclass
class PaperAccount:
    cash: float = 0.0
    positions: list[Position] = field(default_factory=list)
