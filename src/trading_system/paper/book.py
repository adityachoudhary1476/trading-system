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
import logging
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

# v2 added the runner bar watermark (``last_processed_bar_timestamp``,
# ``bar_count``) and per-position ``opened_at``. v1 books are still readable:
# a missing watermark is reported as "unknown", never as "nothing processed".
BOOK_SCHEMA_VERSION = 2

logger = logging.getLogger(__name__)


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
    # The runner's bar watermark. It lives here, not only on the checkpoint,
    # because the checkpoint is immutable: ``save_checkpoint`` refuses a new
    # ``checkpoint_id`` for an existing session, so the checkpoint's cursor is
    # frozen at whatever it was when the session was first written (usually
    # ``None``). The book is the only record that stays current, so it is the
    # only place a resumable cursor can live. Losing it makes a restarted
    # runner treat every replayed bar as new and re-enter live positions.
    last_processed_bar_timestamp: Optional[str] = None
    bar_count: int = 0
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
                    "last_processed_bar_timestamp": self.last_processed_bar_timestamp,
                    "bar_count": self.bar_count,
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
        "opened_at": position.opened_at,
        # Greeks anchors. Held in the live book rather than recomputed on read,
        # because ``entry_delta`` is a one-time measurement taken at the fill:
        # reconstructing it from later market data would compare today's delta
        # against itself and report no decay ever.
        "entry_delta": position.entry_delta,
        "last_delta": position.last_delta,
        "entry_iv": position.entry_iv,
        "last_iv": position.last_iv,
        "greeks_as_of": position.greeks_as_of,
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
    *,
    broker: "PaperBroker",
    session_id: str,
    deployment_id: str,
    last_processed_bar: Any = None,
    bar_count: int = 0,
) -> PaperBook:
    """Snapshot the broker's **entire** book, not just one position.

    ``last_processed_bar`` is the runner's bar watermark, passed in because it
    lives on the runner rather than the broker. It is part of the resumable
    state, so it must be saved with the book or a restart re-trades history.
    """
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
        last_processed_bar_timestamp=(
            last_processed_bar.isoformat()
            if isinstance(last_processed_bar, datetime)
            else (str(last_processed_bar) if last_processed_bar is not None else None)
        ),
        bar_count=int(bar_count or 0),
    )
    book.state_hash = book.compute_hash()
    return book


# --------------------------------------------------------------------------- #
# Book -> broker
# --------------------------------------------------------------------------- #
def _parse_dt(value: Any) -> datetime:
    """Parse a persisted timestamp, refusing to invent one.

    The previous fallback returned ``now()`` for anything unparseable, which
    silently rewrote history: a corrupted fill timestamp became "just now",
    making an old fill look recent and defeating any age-based logic that reads
    the ledger. An unparseable value now raises, and the caller drops that one
    row rather than fabricating a plausible one.

    Naive values are localized to UTC so restored timestamps are always
    comparable with freshly generated ones.
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))  # raises on garbage
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_book_cursor(value: Any) -> Optional[datetime]:
    """Best-effort parse of a persisted bar watermark; ``None`` if unusable."""
    if value is None:
        return None
    try:
        return _parse_dt(value)
    except (TypeError, ValueError):
        return None


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
        # Absent on books written before age tracking existed; a position with
        # unknown age is reported as unknown rather than as brand new, so a
        # time stop can refuse to fire on a guess.
        opened_at=data.get("opened_at"),
        # Likewise absent on books written before greeks tracking existed. The
        # defaults are None, which reads as "greeks unknown" and makes any
        # delta-based exit decline to fire rather than guess.
        entry_delta=data.get("entry_delta"),
        last_delta=data.get("last_delta"),
        entry_iv=data.get("entry_iv"),
        last_iv=data.get("last_iv"),
        greeks_as_of=data.get("greeks_as_of"),
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
    dropped = 0
    for payload in book.orders:
        try:
            order = _order_from_payload(payload)
        except Exception:  # noqa: BLE001
            # One malformed ledger row must not cost the whole book: the
            # positions and cash above are what protect capital. The row is
            # dropped rather than restored with a fabricated timestamp, so the
            # loss is counted and logged instead of being invisible.
            dropped += 1
            continue
        orders[order.order_id] = order
    broker._orders = orders
    if dropped:
        logger.warning(
            "dropped %d unparseable order row(s) while restoring the book for "
            "session %s; the ledger is incomplete for those orders",
            dropped,
            book.session_id,
        )

    return len(positions)
