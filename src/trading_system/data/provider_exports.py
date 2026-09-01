"""Market data providers. Provider-specific code lives only here."""
from .base import MarketDataProvider
from .binance import BinanceProvider
from .stooq import StooqProvider
from ..india.fyers import FYERSMarketDataProvider
from ..india.upstox import UpstoxMarketDataProvider

__all__ = [
    "MarketDataProvider",
    "BinanceProvider",
    "StooqProvider",
    "FYERSMarketDataProvider",
    "UpstoxMarketDataProvider",
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
    if name == "upstox":
        return UpstoxMarketDataProvider(**kwargs)
    raise ValueError(f"Unknown data provider: {name}")
