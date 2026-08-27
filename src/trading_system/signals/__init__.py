"""Signal generation. NOT implemented on Day 1.

This package is a placeholder for the future signal-generation component.
The contract below documents the intended machine-readable output shape so
downstream (risk, paper trading, notifications) can be built against it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


@dataclass
class Signal:
    """Machine-readable trading signal (future component)."""

    symbol: str
    timestamp: object
    direction: SignalDirection
    confidence: float  # 0..1
    source: str = "rule"
    reason: str = ""


def generate_signal(*args, **kwargs) -> Signal:
    raise NotImplementedError("Signal generation is a Day 2+ component.")
