"""Execution layer — broker-agnostic order/position machinery.

This package defines provider-independent order, fill, and broker abstractions so
that strategies and the paper engine never depend on a specific broker (FYERS,
Upstox, ...). Execution is strictly simulation-capable: the concrete `PaperBroker`
places NO real orders and never imports any live broker client.

Architecture (intended, per Day 12):
    Market Data -> Strategy -> Signal -> Risk Manager -> Order -> Broker -> Fill -> Portfolio

The Broker only EXECUTES orders; it never decides whether a trade should happen.
"""
from __future__ import annotations

from .orders import (
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Side,
    OrderStateMachine,
    InvalidOrderTransition,
)
from .broker import Broker, BrokerError
from .paper_broker import (
    PaperBroker,
    SlippageConfig,
    SimpleCostModel,
    AccountSnapshot,
)

__all__ = [
    "Fill",
    "Order",
    "OrderStatus",
    "OrderType",
    "Side",
    "OrderStateMachine",
    "InvalidOrderTransition",
    "Broker",
    "BrokerError",
    "PaperBroker",
    "SlippageConfig",
    "SimpleCostModel",
    "AccountSnapshot",
]
