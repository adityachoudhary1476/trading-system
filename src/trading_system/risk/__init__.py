"""Risk management. NOT implemented on Day 1.

Reserved for position sizing, stop-loss / take-profit logic, exposure limits,
and portfolio-level risk checks. Kept as a typed placeholder so the architecture
is complete and decoupled from the data layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class RiskCheck:
    name: str
    passed: bool
    detail: str = ""


class RiskManager:
    """Placeholder. Real logic lands on Day 2+."""

    def evaluate(self, *args, **kwargs) -> list[RiskCheck]:
        raise NotImplementedError("Risk management is a Day 2+ component.")
