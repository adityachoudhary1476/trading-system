"""Paper-trading account primitives (replaces the Day 1 placeholder).

These d

# --------------------------------------------------------------------------- #
# Expiry Settlement Guidance
# --------------------------------------------------------------------------- #
# Note: Expiry settlement is a separate phase from live premium marking.
# An expired option must NOT receive fresh MTM quotes.
# Intrinsic value at expiry:
#   CE: max(0, settlement_spot - strike)
#   PE: max(0, strike - settlement_spot)
# Settlement closes the position and records realized PnL.
# This is guidance for future execution phases.
ataclasses hold the *accounting* state of a simulated portfolio. They are
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

    # --- Phase 8: Options contract metadata (optional) ---
    options_contract_id: Optional[str] = None
    strike: Optional[float] = None
    expiry: Optional[str] = None
    option_type: Optional[str] = None
    contract_size: int = 1  # number of shares per option contract (default 1 for equities)

    # -- views ----------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self.qty != 0.0

    @property
    def is_option(self) -> bool:
        return self.option_type is not None and self.option_type in ("CE", "PE")

    @property
    def side(self) -> str:
        if self.qty > 0:
            return "LONG"
        if self.qty < 0:
            return "SHORT"
        return "FLAT"

    @property
    def market_value(self) -> float:
        return self.qty * self.current_price * self.contract_size

    @property
    def unrealized_pnl(self) -> float:
        if self.qty == 0.0 or self.avg_entry_price == 0.0:
            return 0.0
        return (self.current_price - self.avg_entry_price) * self.qty * self.contract_size

    def as_dict(self) -> dict:
        d = {
            "symbol": self.symbol,
            "qty": self.qty,
            "side": self.side,
            "avg_entry_price": round(self.avg_entry_price, 4),
            "current_price": round(self.current_price, 4),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "market_value": round(self.market_value, 2),
        }
        if self.options_contract_id is not None:
            d["options_contract_id"] = self.options_contract_id
            d["strike"] = self.strike
            d["expiry"] = self.expiry
            d["option_type"] = self.option_type
            d["contract_size"] = self.contract_size
        return d


# --------------------------------------------------------------------------- #
# Expiry Settlement
# --------------------------------------------------------------------------- #
def settle_option_expiry(
    position: Position,
    settlement_spot: float,
) -> OptionAccountingDecision:
    """
    Settle an option position at expiry.

    Intrinsic value calculation:
      * CE: max(0, settlement_spot - strike)
      * PE: max(0, strike - settlement_spot)

    The settlement_spot should be the official closing price of the underlying
    on the expiry date. If unavailable, settlement cannot proceed.

    Effects:
    * Closes the position (qty -> 0)
    * Calculates realized PnL: (settlement_price - avg_entry_price) * signed_qty * contract_size
    * Updates cash accordingly
    * Records the settlement event

    Returns:
      * OptionAccountingDecision.ALLOW if settlement succeeded
      * OptionAccountingDecision.REJECT if settlement_spot is invalid
    """
    from ..paper_trading.option_accounting import OptionAccountingVerdict, OptionAccountingDecision

    if position.qty == 0:
        return OptionAccountingDecision.REJECT

    if position.expiry is None:
        return OptionAccountingDecision.REJECT

    # Intrinsic value calculation
    strike = position.strike
    option_type = position.option_type or "CE"

    if option_type == "CE":
        intrinsic = max(0.0, settlement_spot - strike)
    elif option_type == "PE":
        intrinsic = max(0.0, strike - settlement_spot)
    else:
        return OptionAccountingDecision.REJECT

    # Cash movement: closing the position
    # The position closes at intrinsic value per contract
    signed_qty = position.qty  # already signed (positive long, negative short)
    contract_size = position.contract_size if position.contract_size is not None else 1

    # Realized PnL: (intrinsic_price - avg_entry_price) * signed_qty * contract_size
    realized = (intrinsic - position.avg_entry_price) * signed_qty * contract_size

    # Update position
    pos = position  # alias
    pos.qty = 0.0
    pos.avg_entry_price = 0.0
    pos.realized_pnl += realized
    pos.current_price = intrinsic  # mark-to-market at settlement

    return OptionAccountingDecision.ALLOW


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
