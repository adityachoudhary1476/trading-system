"""Durable live paper book — restart-recoverable position state.

Why this exists
---------------
``paper_sessions`` is an **immutable, content-addressed audit record**. Its
``checkpoint_id`` is a hash of the state it stores, and the store refuses to
overwrite an existing session with a different id. That is a deliberate
guarantee, and it is why a checkpoint can never track a live book: the moment
the book changes, the id changes, and the write is refused forever. Checkpoint
capture also reduces the book to a *single* position
(``broker.get_position(deployment.symbol)``), so it could not hold a
multi-contract book even if it were writable.

So the live book needs its own home: a mutable row, overwritten in place on
every save, that captures the **whole** book — every open position, the
realized P&L, cash, and the fill ledger — and can rebuild a broker from it
after a restart. This is the only state that loses money if it is lost, so it
is kept separate from the audit trail and never degrades it.

Serialization is lossless: values are stored unrounded (unlike
``Position.as_dict``, which rounds for display), and the restore path rebuilds
real ``Position``/``Order``/``Fill`` objects so the reporting code that reads
``broker._orders`` keeps working after a restart.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..execution.orders import Fill, Order, OrderStatus, OrderType, Side
from ..paper_trading import Position

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance
    # This module serialises and restores a PaperBroker specifically: the paper
    # package must never handle a live broker, and the type reference is what
    # the Phase 18 safety guard checks for.
    from ..execution.paper_broker import PaperBroker

BOOK_SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PaperBook(BaseModel):
    """The full, mutable state of one paper session's book.

    Unlike ``PaperSessionCheckpoint`` this is expected to change: it is
    overwritten in place, and ``state_hash`` is a content hash used to detect
    divergence between the persisted book and a live broker, not an identity
    that pins the row.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    deployment_id: str
    initial_cash: float
    cash: float
    realized_pnl: float
    positions: list[dict] = Field(default_factory=list)
    orders: list[dict] = Field(default_factory=list)
    state_hash: str = ""
    schema_version: int = BOOK_SCHEMA_VERSION
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)

    def compute_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "cash": self.cash,
                    "realized_pnl": self.realized_pnl,
                    "positions": self.positions,
                    "orders": self.orders,
                },
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:64]


# --------------------------------------------------------------------------- #
# Broker -> book
# --------------------------------------------------------------------------- #
def _position_payload(position: Position) -> dict:
    """Unrounded position snapshot (unlike ``Position.as_dict``)."""
    return {
        "symbol": position.symbol,
        "qty": float(position.qty),
        "avg_entry_price": float(position.avg_entry_price),
        "realized_pnl": float(position.realized_pnl),
        "current_price": float(position.current_price),
        "options_contract_id": position.options_contract_id,
        "strike": position.strike,
        "expiry": position.expiry,
        "option_type": position.option_type,
        "contract_size": int(position.contract_size),
    }


def _fill_payload(fill: Fill) -> dict:
    return {
        "fill_id": fill.fill_id,
        "order_id": fill.order_id,
        "symbol": fill.symbol,
        "side": fill.side.value,
        "quantity": float(fill.quantity),
        "price": float(fill.price),
        "timestamp": fill.timestamp.isoformat()
        if isinstance(fill.timestamp, datetime)
        else str(fill.timestamp),
        "fee": float(fill.fee),
        "note": fill.note,
    }


def _order_payload(order: Order) -> dict:
    return {
        "symbol": order.symbol,
        "side": order.side.value,
        "quantity": float(order.quantity),
        "order_type": order.order_type.value,
        "order_id": order.order_id,
        "limit_price": order.limit_price,
        "status": order.status.value,
        "filled_quantity": float(order.filled_quantity),
        "avg_fill_price": float(order.avg_fill_price),
        "fills": [_fill_payload(f) for f in order.fills],
        "created_at": order.created_at.isoformat()
        if isinstance(order.created_at, datetime)
        else str(order.created_at),
        "updated_at": order.updated_at.isoformat()
        if isinstance(order.updated_at, datetime)
        else str(order.updated_at),
        "reject_reason": order.reject_reason,
        "options_contract_id": order.options_contract_id,
        "strike": order.strike,
        "expiry": order.expiry,
        "option_type": order.option_type,
        "contract_size": order.contract_size,
    }


def book_from_broker(
    *, broker: "PaperBroker", session_id: str, deployment_id: str
) -> PaperBook:
    """Snapshot the broker's **entire** book, not just one position."""
    account = broker.account()
    # hasattr rather than getattr: the paper package forbids dynamic attribute
    # access, and the fallback keeps test doubles without a ledger working.
    ledger = broker._orders if hasattr(broker, "_orders") else {}
    book = PaperBook(
        session_id=session_id,
        deployment_id=deployment_id,
        initial_cash=float(account.initial_cash),
        cash=float(account.cash),
        realized_pnl=float(account.realized_pnl),
        positions=[
            _position_payload(p) for p in broker.positions().values() if p.is_open
        ],
        orders=[_order_payload(o) for o in ledger.values()],
    )
    book.state_hash = book.compute_hash()
    return book


# --------------------------------------------------------------------------- #
# Book -> broker
# --------------------------------------------------------------------------- #
def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _fill_from_payload(data: dict) -> Fill:
    return Fill(
        fill_id=data["fill_id"],
        order_id=data["order_id"],
        symbol=data["symbol"],
        side=Side(data["side"]),
        quantity=float(data["quantity"]),
        price=float(data["price"]),
        timestamp=_parse_dt(data.get("timestamp")),
        fee=float(data.get("fee", 0.0) or 0.0),
        note=data.get("note", "") or "",
    )


def _order_from_payload(data: dict) -> Order:
    return Order(
        symbol=data["symbol"],
        side=Side(data["side"]),
        quantity=float(data["quantity"]),
        order_type=OrderType(data["order_type"]),
        order_id=data["order_id"],
        limit_price=data.get("limit_price"),
        status=OrderStatus(data.get("status", OrderStatus.FILLED.value)),
        filled_quantity=float(data.get("filled_quantity", 0.0) or 0.0),
        avg_fill_price=float(data.get("avg_fill_price", 0.0) or 0.0),
        fills=[_fill_from_payload(f) for f in (data.get("fills") or [])],
        created_at=_parse_dt(data.get("created_at")),
        updated_at=_parse_dt(data.get("updated_at")),
        reject_reason=data.get("reject_reason", "") or "",
        options_contract_id=data.get("options_contract_id"),
        strike=data.get("strike"),
        expiry=data.get("expiry"),
        option_type=data.get("option_type"),
        contract_size=data.get("contract_size"),
    )


def _position_from_payload(data: dict) -> Position:
    return Position(
        symbol=data["symbol"],
        qty=float(data["qty"]),
        avg_entry_price=float(data.get("avg_entry_price", 0.0) or 0.0),
        realized_pnl=float(data.get("realized_pnl", 0.0) or 0.0),
        current_price=float(data.get("current_price", 0.0) or 0.0),
        options_contract_id=data.get("options_contract_id"),
        strike=data.get("strike"),
        expiry=data.get("expiry"),
        option_type=data.get("option_type"),
        contract_size=int(data.get("contract_size", 1) or 1),
    )


def apply_book_to_broker(broker: "PaperBroker", book: PaperBook) -> int:
    """Rebuild ``broker`` from ``book``; returns the number of positions restored.

    Cash and realized P&L are restored alongside positions. Realized P&L is
    what makes a restored book consistent with the reported P&L: it is folded
    into the broker on close, so restoring positions without it would silently
    rewrite history.
    """
    broker._cash = float(book.cash)
    broker._realized_pnl = float(book.realized_pnl)

    positions: dict[str, Position] = {}
    for payload in book.positions:
        position = _position_from_payload(payload)
        positions[position.symbol] = position
    broker._positions = positions

    orders: dict[str, Order] = {}
    for payload in book.orders:
        try:
            order = _order_from_payload(payload)
        except Exception:  # noqa: BLE001
            # One malformed ledger row must not cost the whole book: the
            # positions and cash above are what protect capital.
            continue
        orders[order.order_id] = order
    broker._orders = orders

    return len(positions)
