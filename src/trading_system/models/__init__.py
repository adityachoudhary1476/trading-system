"""AI analyst model interface. NOT implemented on Day 1.

Day 1 only defines the *contract* for the future AI analyst. The AI is an
analyst / decision-support component: it receives structured market context
and returns a structured MarketView. It never has direct control over orders.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MarketView:
    """Structured analysis output returned by a future AI analyst.

    This is the machine-readable contract the rest of the system will consume.
    No AI code is wired on Day 1 — this is the data shape to build against.
    """

    symbol: str
    timeframe: str
    market_view: str  # e.g. 'bullish' | 'bearish' | 'neutral' | 'choppy'
    confidence: float  # 0..1
    reasoning_summary: str = ""
    bullish_factors: list[str] = field(default_factory=list)
    bearish_factors: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    invalidating_conditions: list[str] = field(default_factory=list)
    model: str = ""


class ModelProvider:
    """Abstract interface for the future AI analyst provider."""

    name: str = "abstract"

    def analyze(self, context: dict) -> MarketView:
        raise NotImplementedError("AI analyst is a Day 2+ component.")
