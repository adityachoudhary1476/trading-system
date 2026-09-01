"""Order, fill, and order-lifecycle primitives (provider-independent).

These types are shared by every broker implementation (PaperBroker today;
FyersBroker / UpstoxBroker later). They deliberately contain NO broker-specific
logic and NO execution behavior — only data and a strict state machine.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class InvalidOrderTransition(ValueError):
    """Raised when an order state change violates the lifecycle."""


# Allowed transitions. An order may only move along these edges. Any other move
# (e.g. CANCELLED -> FILLED, or filling a REJECTED order) is rejected.
_ALLOWED_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING: {OrderStatus.OPEN, OrderStatus.REJECTED, OrderStatus.CANCELLED},
    OrderStatus.OPEN: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
    },
    OrderStatus.FILLED: set(),          # terminal
    OrderStatus.CANCELLED: set(),       # terminal
    OrderStatus.REJECTED: set(),        # terminal
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Fill:
    """A single execution against an order."""
    fill_id: str
    order_id: str
    symbol: str
    side: Side
    quantity: float
    price: float           # execution price (after slippage)
    timestamp: datetime
    fee: float = 0.0       # transaction cost charged on this fill (INR)
    note: str = ""

    @property
    def notional(self) -> float:
        return abs(self.quantity) * self.price


@dataclass
class Order:
    """A broker order. Immutable identity; mutable lifecycle state.

    The broker owns mutation of `status`, `filled_quantity`, and `fills`.
    Stratgies/risk produce the order; they must not mutate its lifecycle.
    """
    symbol: str
    side: Side
    quantity: float
    order_type: OrderType
    order_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    limit_price: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: float = 0.0
    avg_fill_price: float = 0.0
    fills: list[Fill] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    reject_reason: str = ""

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("order quantity must be positive")
        if self.order_type == OrderType.LIMIT and (
            self.limit_price is None or self.limit_price <= 0
        ):
            raise ValueError("LIMIT orders require a positive limit_price")

    # -- convenience views ----------------------------------------------------
    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)

    @property
    def is_active(self) -> bool:
        """True if the order can still transition (not terminal)."""
        return self.status not in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        )

    @property
    def signed_filled_quantity(self) -> float:
        """Filled qty in signed form (negative for SELL)."""
        return -self.filled_quantity if self.side == Side.SELL else self.filled_quantity

    def transition_to(self, new_status: OrderStatus) -> None:
        """Validate and apply a lifecycle transition (raises on invalid)."""
        if new_status == self.status:
            return
        allowed = _ALLOWED_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise InvalidOrderTransition(
                f"cannot move order {self.order_id} from {self.status.value} "
                f"to {new_status.value}"
            )
        self.status = new_status
        self.updated_at = _now()


class OrderStateMachine:
    """Stateless helper exposing the allowed-transition table for tests/UI."""

    @staticmethod
    def can_transition(frm: OrderStatus, to: OrderStatus) -> bool:
        if frm == to:
            return True
        return to in _ALLOWED_TRANSITIONS.get(frm, set())

    @staticmethod
    def allowed_from(frm: OrderStatus) -> set[OrderStatus]:
        return set(_ALLOWED_TRANSITIONS.get(frm, set()))

    @staticmethod
    def terminal_states() -> set[OrderStatus]:
        return {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}
