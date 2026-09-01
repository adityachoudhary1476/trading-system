"""Broker abstraction (provider-independent execution interface).

A Broker EXECUTES orders. It does not decide what to trade — that is the
strategy + risk layer's job. Concrete brokers (PaperBroker now; FyersBroker /
UpstoxBroker later) implement this contract so strategies stay agnostic to the
underlying execution venue.

Safety: this module and its concrete implementations must NEVER place real
orders or import a live broker client. PaperBroker is simulation-only.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Protocol

from .orders import Fill, Order, OrderStatus, OrderType, Side


class BrokerError(RuntimeError):
    """Base class for broker execution errors (insufficient cash, invalid qty...)."""


class CostModel(Protocol):
    """Provider-independent cost estimator.

    Implementations return a total fee (INR) for one fill. The repo's
    `research.costs.IndiaTransactionCostModel` satisfies this contract and can be
    injected for realistic India charges; a trivial flat/bps model is the default.
    """

    def estimate_fill_fee(self, symbol: str, side: Side, price: float, quantity: float) -> float:
        ...


@dataclass
class AccountSnapshot:
    """A point-in-time view of the paper account (no live data, no secrets)."""
    initial_cash: float
    cash: float
    equity: float
    margin_used: float
    available_cash: float
    realized_pnl: float
    unrealized_pnl: float
    positions: dict[str, "object"]  # symbol -> Position (kept generic here)

    def as_dict(self) -> dict:
        return {
            "initial_cash": self.initial_cash,
            "cash": round(self.cash, 2),
            "equity": round(self.equity, 2),
            "margin_used": round(self.margin_used, 2),
            "available_cash": round(self.available_cash, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "positions": {s: p.as_dict() for s, p in self.positions.items()},
        }


class Broker(ABC):
    """Contract every broker implementation must satisfy."""

    # -- order lifecycle ------------------------------------------------------
    @abstractmethod
    def submit_order(
        self,
        symbol: str,
        side: Side | str,
        quantity: float,
        order_type: OrderType | str = OrderType.MARKET,
        limit_price: Optional[float] = None,
        current_price: Optional[float] = None,
    ) -> Order:
        """Submit an order. MARKET orders are filled immediately against
        `current_price` (if provided) or the last known market price; LIMIT orders
        remain OPEN until their condition is met via `update_market_price`."""
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True if cancelled, False if not found/terminal."""
        ...

    @abstractmethod
    def get_order(self, order_id: str) -> Optional[Order]:
        ...

    # -- market data feed -----------------------------------------------------
    @abstractmethod
    def update_market_price(self, symbol: str, price: float) -> None:
        """Push a market price into the broker: mark positions, evaluate limit orders.
        This is the single entry point the historical/live data engines will feed."""
        ...

    # -- portfolio views ------------------------------------------------------
    @abstractmethod
    def get_position(self, symbol: str):
        ...

    @abstractmethod
    def positions(self) -> dict:
        ...

    @abstractmethod
    def account(self) -> AccountSnapshot:
        ...
