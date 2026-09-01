"""Phase 19 — Paper performance snapshot.

A deterministic, serializable point-in-time performance snapshot for a paper
deployment. All values are read from the broker's public ``AccountSnapshot``
and ``Position`` views — never independently recomputed — so the snapshot
reflects exactly one accounting system.

Metrics the broker cannot supply (win rate, profit factor, exposure) are left
``None`` rather than fabricated.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from ..execution.broker import AccountSnapshot
from ..paper_trading import Position
from .operations import position_dict


class PaperPerformanceSnapshot(BaseModel):
    """Deterministic paper performance snapshot. JSON-safe."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    timestamp: Optional[str] = None
    deployment_id: str
    strategy_id: str
    equity: Optional[float] = None
    realized_pnl: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    total_pnl: Optional[float] = None
    return_: Optional[float] = Field(default=None, alias="return")
    drawdown: Optional[float] = None
    position: Optional[dict] = None
    trade_count: int = 0
    win_rate: Optional[float] = None
    profit_factor: Optional[float] = None
    exposure: Optional[float] = None
    health_status: str = "healthy"


def build_snapshot(
    *,
    deployment_id: str,
    strategy_id: str,
    timestamp: Optional[str],
    account: AccountSnapshot,
    position: Optional[Position],
    starting_equity: Optional[float],
    max_drawdown: Optional[float],
    trade_count: int = 0,
    win_rate: Optional[float] = None,
    profit_factor: Optional[float] = None,
    health_status: str = "healthy",
) -> PaperPerformanceSnapshot:
    """Build a snapshot from broker views + derived operational metrics.

    ``return_`` is (equity - starting) / starting when both are available.
    """
    equity = float(account.equity)
    realized = float(account.realized_pnl)
    unrealized = float(account.unrealized_pnl)
    total_pnl = equity - starting_equity if starting_equity is not None else None
    ret: Optional[float] = None
    if starting_equity is not None and starting_equity > 0:
        ret = (equity - starting_equity) / starting_equity

    exposure: Optional[float] = None
    if account.equity > 0 and position is not None and position.is_open:
        exposure = abs(position.market_value) / account.equity

    return PaperPerformanceSnapshot(
        timestamp=timestamp,
        deployment_id=deployment_id,
        strategy_id=strategy_id,
        equity=equity,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        total_pnl=total_pnl,
        return_=ret,
        drawdown=max_drawdown,
        position=position_dict(position),
        trade_count=trade_count,
        win_rate=win_rate,
        profit_factor=profit_factor,
        exposure=exposure,
        health_status=health_status,
    )
