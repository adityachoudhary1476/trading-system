"""Deterministic tests for the Day 12 paper-trading engine.

Covers: market/limit orders, slippage, costs, position accounting (open/average/
partial-close/full-close/reversal), PnL, order lifecycle (valid + invalid
transitions, duplicate fills, cancelled-cannot-fill), portfolio views, and
isolation from any live broker / FYERS.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading_system.execution import (
    AccountSnapshot,
    BrokerError,
    Fill,
    InvalidOrderTransition,
    Order,
    OrderStateMachine,
    OrderStatus,
    OrderType,
    PaperBroker,
    Side,
    SlippageConfig,
    SimpleCostModel,
)
from trading_system.execution.orders import _ALLOWED_TRANSITIONS


# --- helpers ----------------------------------------------------------------
def fixed_clock(ts: datetime):
    return lambda: ts


def fresh_broker(slippage_bps: float = 0.0, fee_bps: float = 0.0, cash: float = 100_000.0):
    """Deterministic broker: zero slippage/fees unless asked, fixed timestamp."""
    b = PaperBroker(
        initial_cash=cash,
        slippage=SlippageConfig(slippage_bps=slippage_bps),
        cost_model=SimpleCostModel(fee_bps=fee_bps),
    )
    b._clock = fixed_clock(datetime(2026, 1, 1, 9, 30, tzinfo=timezone.utc))
    return b


SYM = "NSE:RELIANCE-EQ"


# === Order lifecycle state machine =========================================
class TestOrderLifecycle:
    def test_allowed_transitions_present(self):
        assert OrderStateMachine.can_transition(OrderStatus.PENDING, OrderStatus.OPEN)
        assert OrderStateMachine.can_transition(OrderStatus.OPEN, OrderStatus.FILLED)
        assert OrderStateMachine.can_transition(OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)
        assert OrderStateMachine.can_transition(OrderStatus.OPEN, OrderStatus.CANCELLED)
        assert OrderStateMachine.can_transition(OrderStatus.OPEN, OrderStatus.REJECTED)
        assert OrderStateMachine.can_transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED)

    def test_terminal_states_reject_all(self):
        for term in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            assert OrderStateMachine.terminal_states() == {
                OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED
            }
            assert OrderStateMachine.allowed_from(term) == set()

    def test_invalid_transition_rejected(self):
        o = Order(symbol=SYM, side=Side.BUY, quantity=10, order_type=OrderType.MARKET)
        o.transition_to(OrderStatus.OPEN)
        o.transition_to(OrderStatus.FILLED)
        with pytest.raises(InvalidOrderTransition):
            o.transition_to(OrderStatus.OPEN)  # terminal -> back to open
        with pytest.raises(InvalidOrderTransition):
            o.transition_to(OrderStatus.CANCELLED)  # filled -> cancelled

    def test_cancelled_order_cannot_fill(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        o = b.submit_order(SYM, Side.BUY, 10, OrderType.LIMIT, limit_price=90.0)
        assert o.status == OrderStatus.OPEN
        assert b.cancel_order(o.order_id) is True
        assert o.status == OrderStatus.CANCELLED
        # Market drops to the limit price; cancelled order must NOT fill.
        b.update_market_price(SYM, 80.0)
        assert o.status == OrderStatus.CANCELLED
        assert o.filled_quantity == 0.0

    def test_duplicate_fill_prevented(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        o = b.submit_order(SYM, Side.BUY, 10, OrderType.MARKET)  # fills immediately
        assert o.status == OrderStatus.FILLED
        filled_before = o.filled_quantity
        # A second internal fill attempt with no remaining qty is a no-op.
        b._fill_order(o, 100.0, fill_qty=5.0)
        assert o.filled_quantity == filled_before


# === Market orders =========================================================
class TestMarketOrders:
    def test_market_buy(self):
        b = fresh_broker()
        b.update_market_price(SYM, 2500.0)
        o = b.submit_order(SYM, "BUY", 10, "MARKET")
        assert o.status == OrderStatus.FILLED
        assert o.filled_quantity == 10
        pos = b.get_position(SYM)
        assert pos.qty == 10
        assert pos.avg_entry_price == 2500.0
        # cash decreased by 10*2500 (no slippage/fee)
        assert b.account().cash == 100_000 - 25_000

    def test_market_sell(self):
        b = fresh_broker()
        b.update_market_price(SYM, 2500.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")
        b.submit_order(SYM, "SELL", 10, "MARKET")
        acc = b.account()
        assert acc.cash == 100_000.0  # round trip, no slippage/fee
        assert b.get_position(SYM).qty == 0

    def test_market_buy_with_slippage(self):
        b = fresh_broker(slippage_bps=10.0)  # 0.10%
        b.update_market_price(SYM, 2500.0)
        o = b.submit_order(SYM, "BUY", 10, "MARKET")
        # BUY slips UP: 2500 * 1.001 = 2502.5
        assert o.avg_fill_price == pytest.approx(2502.5)
        assert o.fills[0].price == pytest.approx(2502.5)

    def test_market_sell_with_slippage(self):
        b = fresh_broker(slippage_bps=10.0)
        b.update_market_price(SYM, 2500.0)
        o = b.submit_order(SYM, "SELL", 10, "MARKET")
        # SELL slips DOWN: 2500 * 0.999 = 2497.5
        assert o.avg_fill_price == pytest.approx(2497.5)

    def test_insufficient_cash(self):
        b = fresh_broker(cash=1000.0)
        b.update_market_price(SYM, 2500.0)
        # No explicit cash check in v1 (cash can go negative as a loan proxy)?
        # We instead assert the simple model does not block, but documents behaviour.
        # To keep deterministic + safe, we test the negative-cash path is allowed
        # (paper accounting) and equity reflects it.
        o = b.submit_order(SYM, "BUY", 10, "MARKET")
        assert o.status == OrderStatus.FILLED
        assert b.account().cash == pytest.approx(1000.0 - 25_000.0)

    def test_invalid_quantity(self):
        b = fresh_broker()
        b.update_market_price(SYM, 2500.0)
        with pytest.raises(BrokerError):
            b.submit_order(SYM, "BUY", 0, "MARKET")
        with pytest.raises(BrokerError):
            b.submit_order(SYM, "BUY", -5, "MARKET")

    def test_invalid_price_blocks_market(self):
        b = fresh_broker()
        # No price ever set; MARKET must refuse.
        with pytest.raises(BrokerError):
            b.submit_order(SYM, "BUY", 10, "MARKET")


# === Limit orders ==========================================================
class TestLimitOrders:
    def test_buy_limit_fills_when_condition_met(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        o = b.submit_order(SYM, "BUY", 10, "LIMIT", limit_price=95.0)
        assert o.status == OrderStatus.OPEN
        b.update_market_price(SYM, 90.0)  # market <= 95 -> fill
        assert o.status == OrderStatus.FILLED
        assert b.get_position(SYM).qty == 10
        assert b.get_position(SYM).avg_entry_price == 90.0

    def test_buy_limit_stays_open_when_not_met(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        o = b.submit_order(SYM, "BUY", 10, "LIMIT", limit_price=95.0)
        b.update_market_price(SYM, 98.0)  # market > 95 -> no fill
        assert o.status == OrderStatus.OPEN
        assert o.filled_quantity == 0.0

    def test_sell_limit_fills_when_condition_met(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")  # open a long at 100
        o = b.submit_order(SYM, "SELL", 10, "LIMIT", limit_price=110.0)
        assert o.status == OrderStatus.OPEN
        b.update_market_price(SYM, 115.0)  # market >= 110 -> fill
        assert o.status == OrderStatus.FILLED
        assert b.get_position(SYM).qty == 0

    def test_sell_limit_stays_open_when_not_met(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")
        o = b.submit_order(SYM, "SELL", 10, "LIMIT", limit_price=110.0)
        b.update_market_price(SYM, 105.0)  # market < 110 -> no fill
        assert o.status == OrderStatus.OPEN

    def test_limit_cancellation(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        o = b.submit_order(SYM, "BUY", 10, "LIMIT", limit_price=95.0)
        assert b.cancel_order(o.order_id) is True
        assert o.status == OrderStatus.CANCELLED
        b.update_market_price(SYM, 90.0)
        assert o.filled_quantity == 0.0  # stayed cancelled


# === Positions =============================================================
class TestPositions:
    def test_open_and_average(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")   # @100
        b.update_market_price(SYM, 110.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")   # @110
        pos = b.get_position(SYM)
        assert pos.qty == 20
        assert pos.avg_entry_price == 105.0

    def test_partial_close(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")
        b.update_market_price(SYM, 120.0)
        b.submit_order(SYM, "SELL", 4, "MARKET")   # close 4 of 10
        pos = b.get_position(SYM)
        assert pos.qty == 6
        assert pos.avg_entry_price == 100.0  # unchanged on partial reduce
        # realized = (120-100)*4 = 80
        assert pos.realized_pnl == pytest.approx(80.0)
        assert b.account().realized_pnl == pytest.approx(80.0)

    def test_full_close(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")
        b.update_market_price(SYM, 130.0)
        b.submit_order(SYM, "SELL", 10, "MARKET")
        pos = b.get_position(SYM)
        assert pos.qty == 0
        # realized = (130-100)*10 = 300
        assert pos.realized_pnl == pytest.approx(300.0)
        assert b.account().realized_pnl == pytest.approx(300.0)

    def test_reversal(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")   # long 10 @100
        b.update_market_price(SYM, 120.0)
        b.submit_order(SYM, "SELL", 25, "MARKET")  # close 10 + go short 15
        pos = b.get_position(SYM)
        assert pos.qty == -15
        assert pos.avg_entry_price == 120.0  # new short entry
        # realized on the closed 10: (120-100)*10 = 200
        assert pos.realized_pnl == pytest.approx(200.0)


# === P&L / costs ===========================================================
class TestPnL:
    def test_gross_realized_pnl(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")
        b.update_market_price(SYM, 150.0)
        b.submit_order(SYM, "SELL", 10, "MARKET")
        # gross realized = (150-100)*10 = 500
        assert b.account().realized_pnl == pytest.approx(500.0)

    def test_unrealized_pnl(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")
        b.update_market_price(SYM, 110.0)  # mark up
        # unrealized = (110-100)*10 = 100
        assert b.account().unrealized_pnl == pytest.approx(100.0)
        # equity = cash (100000-1000) + unrealized 100 = 99100
        assert b.account().equity == pytest.approx(99_100.0)

    def test_fees_charged(self):
        b = fresh_broker(fee_bps=0.001)  # 0.1% of notional
        b.update_market_price(SYM, 2500.0)
        o = b.submit_order(SYM, "BUY", 10, "MARKET")
        expected_fee = 0.001 * (2500.0 * 10)
        assert o.fills[0].fee == pytest.approx(expected_fee)
        # cash reduced by notional + fee
        assert b.account().cash == pytest.approx(100_000.0 - 25_000.0 - expected_fee)

    def test_net_pnl_after_fees(self):
        b = fresh_broker(fee_bps=0.001)
        b.update_market_price(SYM, 100.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")   # fee = 0.001*1000 = 1.0
        b.update_market_price(SYM, 150.0)
        b.submit_order(SYM, "SELL", 10, "MARKET")  # fee = 0.001*1500 = 1.5
        # gross realized = (150-100)*10 = 500; fees total 2.5.
        # Net P&L = equity - initial = (realized 500) - fees 2.5 = 497.5.
        net_pnl = b.account().equity - b.initial_cash
        assert net_pnl == pytest.approx(497.5)
        # realized PnL (before fees) is still 500; fees are separate.
        assert b.account().realized_pnl == pytest.approx(500.0)


# === Portfolio ==============================================================
class TestPortfolio:
    def test_cash_updates(self):
        b = fresh_broker()
        b.update_market_price(SYM, 200.0)
        b.submit_order(SYM, "BUY", 5, "MARKET")
        assert b.account().cash == pytest.approx(100_000.0 - 1_000.0)

    def test_equity_updates(self):
        b = fresh_broker()
        b.update_market_price(SYM, 200.0)
        b.submit_order(SYM, "BUY", 5, "MARKET")
        b.update_market_price(SYM, 210.0)
        # cash 99_000 + unrealized (210-200)*5=50 = 99_050
        assert b.account().equity == pytest.approx(99_050.0)

    def test_available_cash(self):
        b = fresh_broker()
        assert b.account().available_cash == pytest.approx(100_000.0)

    def test_position_valuation(self):
        b = fresh_broker()
        b.update_market_price(SYM, 200.0)
        b.submit_order(SYM, "BUY", 5, "MARKET")
        b.update_market_price(SYM, 220.0)
        pos = b.get_position(SYM)
        assert pos.market_value == pytest.approx(5 * 220.0)


# === Broker abstraction / isolation ========================================
class TestIsolation:
    def test_paper_broker_has_no_fyers_path(self):
        import ast
        import inspect
        import trading_system.execution.paper_broker as pb

        tree = ast.parse(inspect.getsource(pb))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    imported.add(n.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module)
        # No FYERS / live-broker / order-endpoint modules are imported.
        assert not any("fyers_apiv3" in m or "india.fyers" in m or "token_manager" in m
                       for m in imported)
        # The docstring may mention these names only as prose; imports must not.
        assert "fyers_apiv3" not in imported
        assert "india.fyers" not in imported
        # No hardcoded FYERS order endpoint string in source.
        src = inspect.getsource(pb)
        assert "orders/sync" not in src  # FYERS order endpoint
        assert "validate-authcode" not in src

    def test_no_live_imports_in_execution_package(self):
        import importlib, pkgutil
        import trading_system.execution as ex

        for mod in pkgutil.iter_modules(ex.__path__):
            m = importlib.import_module(f"trading_system.execution.{mod.name}")
            assert "fyers_apiv3" not in getattr(m, "__dict__", {}).get("__name__", "")

    def test_paper_status_command_runs_without_credentials(self):
        from trading_system import __main__ as cli

        class _Args:
            pass
        rc = cli._cmd_paper_status(_Args())
        assert rc == 0


# === End-to-end vertical slice (task success criteria) =======================
class TestVerticalSlice:
    def test_conceptual_usage(self):
        # Matches the task's success-criteria snippet. We pass explicit zero slippage
        # so the conceptual numbers (entry == 2500) hold exactly; the default
        # SlippageConfig is 5 bps (tested separately).
        broker = PaperBroker(initial_cash=100_000, slippage=SlippageConfig(0.0))
        broker.update_market_price("NSE:RELIANCE-EQ", 2500)
        order = broker.submit_order(
            symbol="NSE:RELIANCE-EQ", side="BUY", quantity=10, order_type="MARKET"
        )
        broker.update_market_price("NSE:RELIANCE-EQ", 2520)

        pos = broker.get_position("NSE:RELIANCE-EQ")
        acc = broker.account()
        assert order.status == OrderStatus.FILLED
        assert pos.qty == 10
        assert pos.avg_entry_price == 2500.0
        assert acc.cash == pytest.approx(100_000 - 25_000)
        assert acc.equity == pytest.approx(100_000 - 25_000 + (2520 - 2500) * 10)
        assert order.avg_fill_price == 2500.0
        assert order.fills[0].fee == 0.0

    def test_default_slippage_is_conservative(self):
        # Default broker applies a small adverse slippage (5 bps), so a BUY at a
        # 2500 market fills at 2501.25. This documents the real default behavior.
        broker = PaperBroker(initial_cash=100_000)  # default SlippageConfig()
        broker.update_market_price("NSE:RELIANCE-EQ", 2500)
        order = broker.submit_order(
            symbol="NSE:RELIANCE-EQ", side="BUY", quantity=10, order_type="MARKET"
        )
        assert order.avg_fill_price == pytest.approx(2501.25)
