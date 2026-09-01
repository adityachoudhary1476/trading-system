"""PaperBroker — a simulation-only broker (NO real orders, NO live broker calls).

Implements the `Broker` contract for fully in-memory paper trading. Designed to be
fed prices via `update_market_price` from either the historical backtest engine or a
future live market-data engine. Strategies never talk to this directly for *decisions*;
they emit signals, risk approves, then an Order is submitted.

Execution model (documented, deterministic):
  * MARKET orders fill immediately at the supplied/last market price, with slippage
    applied adversely (BUY fills slightly above, SELL slightly below).
  * LIMIT orders are validated and parked OPEN. Each `update_market_price` re-evaluates
    them: BUY LIMIT fills when market <= limit; SELL LIMIT fills when market >= limit.
  * All fills update cash, the position (avg-entry / realized PnL), and fees.
  * Position accounting supports open / average / partial close / full close / reverse.
  * Fees come from an injected `CostModel` (default: simple bps model; the repo's
    `IndiaTransactionCostModel` can be passed in for realistic India charges).

This module imports NOTHING from `india.fyers` or `india.token_manager`. It cannot
place a real order even by accident.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from .broker import AccountSnapshot, Broker, BrokerError, CostModel
from .orders import Fill, Order, OrderStatus, OrderType, Side

# Default simple cost model parameters (overridable). Kept here so the broker has
# a sane default without forcing a specific real-broker schedule.
_DEFAULT_FEE_BPS = 0.0          # flat per-fill fee basis (fraction of notional)
_DEFAULT_MIN_FEE = 0.0           # minimum fee per fill (INR)


class SlippageConfig:
    """Configurable slippage in basis points.

    Applied adversely: BUY executes above market, SELL below. Deterministic — the
    same inputs always produce the same execution price (no randomness).
    """

    def __init__(self, slippage_bps: float = 5.0) -> None:
        if slippage_bps < 0:
            raise ValueError("slippage_bps must be >= 0")
        self.slippage_bps = float(slippage_bps)

    def apply(self, side: Side, market_price: float) -> float:
        """Return the execution price after adverse slippage."""
        if market_price <= 0:
            return market_price
        adj = self.slippage_bps / 10_000.0
        if side == Side.BUY:
            return market_price * (1.0 + adj)
        return market_price * (1.0 - adj)


class SimpleCostModel:
    """Provider-independent default cost model.

    fee = max(min_fee, fee_bps * notional). Kept intentionally simple; the repo's
    real `IndiaTransactionCostModel` satisfies the same `CostModel` protocol and can
    be injected for realistic statutory charges. THIS model is NOT FYERS-specific.
    """

    def __init__(self, fee_bps: float = _DEFAULT_FEE_BPS, min_fee: float = _DEFAULT_MIN_FEE) -> None:
        self.fee_bps = float(fee_bps)
        self.min_fee = float(min_fee)

    def estimate_fill_fee(self, symbol: str, side: Side, price: float, quantity: float) -> float:
        notional = abs(price) * abs(quantity)
        fee = self.fee_bps * notional
        return max(self.min_fee, fee)


class _Clock:
    """Pluggable timestamp source (deterministic in tests)."""
    def __init__(self, now: Optional[Callable[[], datetime]] = None) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc))

    def now(self) -> datetime:
        return self._now()


@dataclass
class PaperBroker(Broker):
    """In-memory simulation broker. No I/O, no live calls, fully deterministic."""
    initial_cash: float = 100_000.0
    slippage: SlippageConfig = field(default_factory=SlippageConfig)
    cost_model: CostModel = field(default_factory=SimpleCostModel)
    # Simple cash-only margin model: margin_used is 0 by default (no leverage).
    # Real FYERS margin rules are explicitly OUT of scope (paper accounting only).
    _cash: float = field(init=False)
    _realized_pnl: float = field(init=False, default=0.0)
    _positions: dict[str, "Position"] = field(init=False, default_factory=dict)
    _orders: dict[str, Order] = field(init=False, default_factory=dict)
    _last_price: dict[str, float] = field(init=False, default_factory=dict, repr=False)
    # Pluggable timestamp source (deterministic in tests). A plain callable
    # returning a datetime; default uses wall-clock UTC.
    _clock: Callable[[], datetime] = field(
        init=False, default_factory=lambda: (lambda: datetime.now(timezone.utc))
    )

    def __post_init__(self) -> None:
        self._cash = float(self.initial_cash)
        self._realized_pnl = 0.0
        self._positions = {}
        self._orders = {}
        self._last_price = {}

    # -- internal helpers -----------------------------------------------------
    def _now(self) -> datetime:
        return self._clock()

    def _position(self, symbol: str):
        from ..paper_trading import Position
        pos = self._positions.get(symbol)
        if pos is None:
            pos = Position(symbol=symbol)
            self._positions[symbol] = pos
        return pos

    def _unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self._positions.values())

    def _equity(self) -> float:
        # cash already reflects realized PnL; equity = cash + unrealized.
        return self._cash + self._unrealized_pnl()

    def _available_cash(self) -> float:
        # Simple model: no margin reserved, so available == cash.
        return self._cash

    # -- order submission -----------------------------------------------------
    def submit_order(
        self,
        symbol: str,
        side: Side | str,
        quantity: float,
        order_type: OrderType | str = OrderType.MARKET,
        limit_price: Optional[float] = None,
        current_price: Optional[float] = None,
    ) -> Order:
        side = Side(side) if not isinstance(side, Side) else side
        order_type = OrderType(order_type) if not isinstance(order_type, OrderType) else order_type
        if quantity is None or quantity <= 0:
            raise BrokerError("order quantity must be > 0")
        if not symbol:
            raise BrokerError("symbol is required")

        # Resolve the reference market price for immediate (MARKET) fills.
        ref_price = current_price if current_price is not None else self._last_price.get(symbol)
        if order_type == OrderType.MARKET and (ref_price is None or ref_price <= 0):
            raise BrokerError(
                f"MARKET order for {symbol} has no market price yet; call "
                f"update_market_price(symbol, price) first or pass current_price."
            )

        order = Order(
            symbol=symbol,
            side=side,
            quantity=float(quantity),
            order_type=order_type,
            limit_price=(float(limit_price) if limit_price is not None else None),
        )
        # PENDING -> OPEN
        order.transition_to(OrderStatus.OPEN)
        self._orders[order.order_id] = order

        if order_type == OrderType.MARKET:
            self._fill_order(order, ref_price, fill_qty=order.quantity)
        else:
            # LIMIT: evaluate now in case the condition is already met.
            self._evaluate_limit_order(order, ref_price)
        return order

    # -- cancellation ---------------------------------------------------------
    def cancel_order(self, order_id: str) -> bool:
        order = self._orders.get(order_id)
        if order is None or not order.is_active:
            return False
        order.transition_to(OrderStatus.CANCELLED)
        return True

    def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    # -- market price feed ----------------------------------------------------
    def update_market_price(self, symbol: str, price: float) -> None:
        if price is None or price <= 0:
            raise BrokerError(f"invalid market price for {symbol}: {price}")
        self._last_price[symbol] = float(price)
        pos = self._positions.get(symbol)
        if pos is not None:
            pos.current_price = float(price)
        # Evaluate any resting LIMIT orders for this symbol.
        for order in list(self._orders.values()):
            if order.symbol == symbol and order.is_active and order.order_type == OrderType.LIMIT:
                self._evaluate_limit_order(order, price)

    # -- limit evaluation -----------------------------------------------------
    def _evaluate_limit_order(self, order: Order, market_price: Optional[float]) -> None:
        if market_price is None or market_price <= 0:
            return
        lp = order.limit_price
        if lp is None:
            return
        buy_ok = order.side == Side.BUY and market_price <= lp
        sell_ok = order.side == Side.SELL and market_price >= lp
        if buy_ok or sell_ok:
            self._fill_order(order, market_price, fill_qty=order.quantity)

    # -- fill engine (core accounting) ---------------------------------------
    def _fill_order(self, order: Order, market_price: float, fill_qty: float) -> None:
        if fill_qty <= 0 or order.remaining_quantity <= 0:
            return
        fill_qty = min(fill_qty, order.remaining_quantity)

        exec_price = self.slippage.apply(order.side, market_price)
        fee = self.cost_model.estimate_fill_fee(order.symbol, order.side, exec_price, fill_qty)
        fill = Fill(
            fill_id=uuid.uuid4().hex,
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=fill_qty,
            price=exec_price,
            timestamp=self._now(),
            fee=fee,
            note="paper fill (simulation)",
        )
        order.fills.append(fill)
        order.filled_quantity += fill_qty
        # rolling average fill price
        if order.filled_quantity > 0:
            prev = order.avg_fill_price * (order.filled_quantity - fill_qty)
            order.avg_fill_price = (prev + exec_price * fill_qty) / order.filled_quantity

        self._apply_fill_to_book(order.symbol, order.side, fill_qty, exec_price, fee)

        # Order status transition
        if order.remaining_quantity <= 1e-12:
            order.transition_to(OrderStatus.FILLED)
        else:
            order.transition_to(OrderStatus.PARTIALLY_FILLED)

    def _apply_fill_to_book(
        self, symbol: str, side: Side, qty: float, price: float, fee: float
    ) -> None:
        """Update cash + position with one fill. Signed qty: BUY +, SELL -."""
        signed = qty if side == Side.BUY else -qty
        pos = self._position(symbol)

        # Cash effect: buying spends cash + fee; selling receives cash - fee.
        cash_delta = -signed * price - (fee if side == Side.BUY else fee)
        # For SELL, fee reduces proceeds: cash += qty*price - fee.
        if side == Side.SELL:
            cash_delta = qty * price - fee
        else:
            cash_delta = -(qty * price) - fee
        self._cash += cash_delta

        # Position update + realized PnL on reducing/closing/reversing.
        old_qty = pos.qty
        old_avg = pos.avg_entry_price

        if old_qty == 0 or (old_qty > 0 and signed > 0) or (old_qty < 0 and signed < 0):
            # Open or increase in the same direction.
            new_qty = old_qty + signed
            if new_qty != 0:
                pos.avg_entry_price = (old_avg * abs(old_qty) + price * abs(signed)) / abs(new_qty)
            else:
                pos.avg_entry_price = 0.0
            pos.qty = new_qty
        else:
            # Reducing / closing / reversing.
            closing_qty = min(abs(signed), abs(old_qty))
            # Realized PnL: for a LONG (old_qty>0) being reduced by a SELL:
            #   pnl = (sell_price - avg_entry) * closing_qty
            # For a SHORT (old_qty<0) being reduced by a BUY:
            #   pnl = (avg_entry - buy_price) * closing_qty
            direction_sign = 1.0 if old_qty > 0 else -1.0
            realized = direction_sign * (price - old_avg) * closing_qty
            self._realized_pnl += realized
            pos.realized_pnl += realized

            remaining = old_qty + signed
            if abs(remaining) < 1e-12:
                # fully closed
                pos.qty = 0.0
                pos.avg_entry_price = 0.0
            elif (old_qty > 0 and remaining < 0) or (old_qty < 0 and remaining > 0):
                # reversed: leftover is opposite side at the new price
                pos.qty = remaining
                pos.avg_entry_price = price
            else:
                # partial reduce, same side remains
                pos.qty = remaining
                # avg_entry unchanged when reducing

        pos.current_price = price

    # -- portfolio views -----------------------------------------------------
    def get_position(self, symbol: str):
        return self._positions.get(symbol)

    def positions(self) -> dict:
        return dict(self._positions)

    def account(self) -> AccountSnapshot:
        return AccountSnapshot(
            initial_cash=self.initial_cash,
            cash=self._cash,
            equity=self._equity(),
            margin_used=0.0,
            available_cash=self._available_cash(),
            realized_pnl=self._realized_pnl,
            unrealized_pnl=self._unrealized_pnl(),
            positions={s: p for s, p in self._positions.items() if p.is_open},
        )
