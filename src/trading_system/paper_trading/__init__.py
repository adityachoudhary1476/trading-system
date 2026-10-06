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
from datetime import datetime, timezone
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

    # --- Age tracking ---
    # When this position most recently went from flat to non-flat, as an ISO
    # timestamp. Maintained by PaperBroker on open/increase and cleared on
    # close. Without it no time-based exit is possible: a position held for one
    # bar and one held for a thousand look identical to every exit rule, so
    # capital can be stranded indefinitely by a thesis that simply stops
    # playing out.
    opened_at: Optional[str] = None

    # --- Greeks anchors (optional) ---
    # Delta at entry, and the most recently computed delta. Their ratio is what
    # tells you whether the position still expresses the view it was opened
    # for: a 0.50-delta call that has decayed to 0.10 no longer carries the
    # directional exposure that justified buying it, and a percentage stop
    # cannot see that because premium loss alone does not distinguish ordinary
    # decay from a broken thesis.
    #
    # ``entry_delta`` is the anchor and must be captured at or near the fill;
    # recomputing it later from today's inputs would compare today's delta to
    # itself and always report no decay. Both stay None when the inputs needed
    # to solve volatility were unavailable, which callers must treat as
    # "greeks unknown" rather than as zero exposure.
    #
    # Gamma, theta and vega are captured from the same quote as delta and held
    # for the same reason: a net-exposure cap must be measured against what the
    # book actually carries, and recomputing on read would use a later market
    # than the position was priced at. They follow the same None-means-unknown
    # contract as delta.
    entry_delta: Optional[float] = None
    last_delta: Optional[float] = None
    entry_iv: Optional[float] = None
    last_iv: Optional[float] = None
    entry_gamma: Optional[float] = None
    last_gamma: Optional[float] = None
    entry_theta: Optional[float] = None
    last_theta: Optional[float] = None
    entry_vega: Optional[float] = None
    last_vega: Optional[float] = None
    greeks_as_of: Optional[str] = None

    def has_greeks(self) -> bool:
        """True when both the entry anchor and a current reading exist.

        A delta-decay test needs two points. With only one, any comparison
        silently becomes either "no decay" or a ratio against zero, so the
        honest answer is that the test cannot be evaluated.
        """
        return self.entry_delta is not None and self.last_delta is not None

    def delta_retention(self) -> Optional[float]:
        """|current delta| / |entry delta|, or None if unavailable.

        1.0 means the position still carries its original directional exposure;
        0.5 means half of it has decayed away. Returns None — never 0.0 — when
        either point is missing, so an unmeasurable position is never confused
        with one whose exposure has fully collapsed.
        """
        if not self.has_greeks():
            return None
        anchor = abs(float(self.entry_delta))
        if anchor <= 0.0:
            return None
        return abs(float(self.last_delta)) / anchor

    def holding_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        """Seconds this position has been open, or None if unknown.

        Returns None rather than 0.0 when ``opened_at`` is missing, so callers
        can tell "just opened" apart from "age unknown" and refuse to treat an
        unknown age as a satisfied time stop.
        """
        if not self.opened_at:
            return None
        reference = now or datetime.now(timezone.utc)
        try:
            opened = datetime.fromisoformat(str(self.opened_at))
        except (TypeError, ValueError):
            return None
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        return max(0.0, (reference - opened).total_seconds())

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
        if self.opened_at is not None:
            d["opened_at"] = self.opened_at
        for field_name in (
            "entry_delta", "last_delta", "entry_iv", "last_iv",
            "entry_gamma", "last_gamma", "entry_theta", "last_theta",
            "entry_vega", "last_vega", "greeks_as_of",
        ):
            value = getattr(self, field_name)
            if value is not None:
                d[field_name] = value
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
