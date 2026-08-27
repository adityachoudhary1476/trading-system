"""Market data providers. Provider-specific code lives only here."""
from .base import MarketDataProvider
from .binance import BinanceProvider
from .stooq import StooqProvider
from ..india.fyers import FYERSMarketDataProvider

__all__ = [
    "MarketDataProvider",
    "BinanceProvider",
    "StooqProvider",
    "FYERSMarketDataProvider",
]


def get_provider(name: str, **kwargs) -> MarketDataProvider:
    """Factory: build a provider by name. Keeps callers decoupled from classes."""
    name = (name or "binance").lower()
    if name == "binance":
        return BinanceProvider(**kwargs)
    if name == "stooq":
        return StooqProvider(**kwargs)
    if name in ("fyers", "india"):
        return FYERSMarketDataProvider(**kwargs)
    raise ValueError(f"Unknown data provider: {name}")
