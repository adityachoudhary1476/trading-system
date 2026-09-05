"""Built-in reference strategies for the Strategy Factory.

These are small, deterministic, fully-specified strategies provided as
architectural fixtures (Phase 1). They are registered into the discovery
catalog via the ``@register_strategy`` decorator applied in each module and
re-registered by name here so that ``discover(..., reload=True)`` can repopulate
the catalog after ``clear_discovery`` without creating new class objects. Importing
them pulls in only ``trading_system.indicators`` (numpy/pandas only) -- never a
broker or network.
"""
from __future__ import annotations

from ..discovery import register_strategy
from .ema_crossover import EMACrossoverStrategy
from .rsi_mean_reversion import RSIMeanReversionStrategy

register_strategy(EMACrossoverStrategy)
register_strategy(RSIMeanReversionStrategy)

__all__ = [
    "EMACrossoverStrategy",
    "RSIMeanReversionStrategy",
]
