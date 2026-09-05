"""Exception hierarchy for the Strategy Factory.

These errors are pure data/validation failures. They never carry, embed, or
trigger execution: no Broker, no PaperBroker, no network, no live trading.
"""
from __future__ import annotations

from typing import Optional


class StrategyFactoryError(Exception):
    """Base class for all Strategy Factory errors."""


class StrategyValidationError(StrategyFactoryError, ValueError):
    """Raised when a strategy definition fails validation.

    Carries the full list of human-readable error messages so a caller (or a
    future AI generator) can see exactly what was wrong. Mirrors the
    ``StrategyValidationError`` convention used by ``strategy_lab``.
    """

    def __init__(self, errors: Optional[list[str]] = None, message: str = "") -> None:
        self.errors: list[str] = list(errors) if errors else []
        if message and not self.errors:
            self.errors = [message]
        super().__init__("; ".join(self.errors) if self.errors else (message or "invalid strategy"))

    def __str__(self) -> str:
        if self.errors:
            return "Strategy Factory validation failed: " + "; ".join(self.errors)
        return "Strategy Factory validation failed"


class DuplicateStrategyError(StrategyFactoryError):
    """Raised when a strategy identity is already registered."""


class InsufficientHistoryError(StrategyFactoryError):
    """Raised when a MarketState carries too few bars for a strategy to evaluate."""
