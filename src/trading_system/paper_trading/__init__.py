"""Paper-trading account primitives (replaces the Day 1 placeholder).

These dataclasses hold the *accounting* state of a simulated portfolio. They are
deliberately separate from any real broker's margin rules — this is PAPER
accounting only. The `PaperBroker` in `execution.paper_broker` owns mutation;
these are plain data holders with convenience views.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Position:
    """A single instrument position in the paper book.

    Tracks signed quantity via `qty` (positive long, negative short) and a
    running average entry price. `realized_pnl` accumulates on reducing/closing
    legs; `unrealized_pnl` is computed against `current_price`.
    """
    symbol: str
    qty: float = 0.0
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0
    current_price: float = 0.0

    # -- views ----------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self.qty != 0.0

    @property
    def side(self) -> str:
        if self.qty > 0:
            return "LONG"
        if self.qty < 0:
            return "SHORT"
        return "FLAT"

    @property
    def market_value(self) -> float:
        return self.qty * self.current_price

    @property
    def unrealized_pnl(self) -> float:
        if self.qty == 0.0 or self.avg_entry_price == 0.0:
            return 0.0
        return (self.current_price - self.avg_entry_price) * self.qty

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "qty": self.qty,
            "side": self.side,
            "avg_entry_price": round(self.avg_entry_price, 4),
            "current_price": round(self.current_price, 4),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "market_value": round(self.market_value, 2),
        }


@dataclass
class PaperAccount:
    """Cash + equity ledger for the paper portfolio (no real broker margin)."""
    initial_cash: float = 0.0
    cash: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    margin_used: float = 0.0  # simple book-keeping only; NOT real FYERS margin

    @property
    def equity(self) -> float:
        # equity = cash + marked-to-market value of all positions.
        # `unrealized_pnl` already nets (current - avg_entry) * qty against cash,
        # so equity == cash + sum(position.market_value). We expose both forms.
        return self.cash + self.unrealized_pnl

    @property
    def available_cash(self) -> float:
        return self.cash - self.margin_used

    def as_dict(self) -> dict:
        return {
            "initial_cash": self.initial_cash,
            "cash": round(self.cash, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "equity": round(self.equity, 2),
            "margin_used": round(self.margin_used, 2),
            "available_cash": round(self.available_cash, 2),
        }
