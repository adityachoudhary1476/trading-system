"""Phase 19 — Paper operations state model.

A strongly typed, serializable snapshot of the operational state of one paper
deployment. It is a *view* derived from the runner + broker; it never owns
accounting truth (the ``PaperBroker`` remains the source of cash/equity/P&L).

All metrics here are read from the broker's ``AccountSnapshot`` and ``Position``
views, never recomputed independently, so there is exactly one accounting
system. Unavailable metrics stay ``None``.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict

from ..paper_trading import Position


class HealthStatus(str, Enum):
    """Operational health classification."""

    HEALTHY = "healthy"
    WARNING = "warning"
    HALTED = "halted"


class PaperOperationsState(BaseModel):
    """Point-in-time operational state of a paper deployment.

    Deterministic and JSON-safe. No broker internals, no credentials.
    """

    model_config = ConfigDict(extra="forbid")

    deployment_id: str
    strategy_id: str
    status: str
    started_at: Optional[str] = None
    last_bar_timestamp: Optional[str] = None
    processed_bars: int = 0
    generated_signals: int = 0
    submitted_orders: int = 0
    filled_orders: int = 0
    rejected_orders: int = 0
    current_equity: Optional[float] = None
    starting_equity: Optional[float] = None
    realized_pnl: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    max_drawdown: Optional[float] = None
    current_position: Optional[dict] = None
    last_signal: Optional[str] = None
    last_order: Optional[dict] = None
    last_fill: Optional[dict] = None
    health_status: str = HealthStatus.HEALTHY.value
    halt_reason: Optional[str] = None
    consecutive_errors: int = 0


def position_dict(position: Optional[Position]) -> Optional[dict]:
    """Safe, serializable position view (None when flat/missing)."""
    if position is None or not position.is_open:
        return None
    return {
        "symbol": position.symbol,
        "side": position.side,
        "qty": position.qty,
        "avg_entry_price": position.avg_entry_price,
        "current_price": position.current_price,
        "market_value": position.market_value,
        "unrealized_pnl": position.unrealized_pnl,
        "realized_pnl": position.realized_pnl,
    }
