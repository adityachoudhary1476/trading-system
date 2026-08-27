"""Market data providers. Provider-specific code lives only here."""
from .base import MarketDataProvider
from .binance import BinanceProvider
from .stooq import StooqProvider

__all__ = ["MarketDataProvider", "BinanceProvider", "StooqProvider"]


def get_provider(name: str, **kwargs) -> MarketDataProvider:
    """Factory: build a provider by name. Keeps callers decoupled from classes."""
    name = (name or "binance").lower()
    if name == "binance":
        return BinanceProvider(**kwargs)
    if name == "stooq":
        return StooqProvider(**kwargs)
    raise ValueError(f"Unknown data provider: {name}")
