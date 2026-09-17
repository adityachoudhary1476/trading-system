"""Market data providers. Provider-specific code lives only here."""
from .base import MarketDataProvider
from .binance import BinanceProvider
from .stooq import StooqProvider
from ..india.upstox import UpstoxMarketDataProvider

__all__ = [
    "MarketDataProvider",
    "BinanceProvider",
    "StooqProvider",
    "UpstoxMarketDataProvider",
]


def get_provider(name: str, **kwargs) -> MarketDataProvider:
    """Factory: build a provider by name. Keeps callers decoupled from classes."""
    name = (name or "binance").lower()
    if name == "binance":
        return BinanceProvider(**kwargs)
    if name == "stooq":
        return StooqProvider(**kwargs)
    if name in ("upstox", "india", "fyers"):
        # "india" and the legacy "fyers" alias now resolve to Upstox — the
        # sole Indian market-data provider. No Fyers credentials or SDK
        # remain in the system.
        return UpstoxMarketDataProvider(**kwargs)
    raise ValueError(f"Unknown data provider: {name}")
