"""ModelProvider abstraction — keeps the app decoupled from any AI vendor.

The AI analyst only produces a MarketView from a MarketSnapshot. It never trades,
sizes positions, or touches risk limits. Implementations: local (offline rule
based, tested) and OpenAI-compatible (real client, untested here — no creds).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from .snapshot import MarketSnapshot
from .market_view import MarketView


class ModelProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def analyze(self, snapshot: MarketSnapshot) -> MarketView:
        """Turn a structured snapshot into a validated MarketView."""
        ...

    @property
    def is_available(self) -> bool:
        """Whether this provider can actually run in the current environment."""
        return True


class ModelProviderError(RuntimeError):
    """Raised when a provider fails or returns unparseable output."""
