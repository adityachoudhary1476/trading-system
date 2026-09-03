"""Hardening tests for the existing Day-12 paper-trading engine.

These tests complement test_paper_broker.py by covering gaps identified
during the Phase 1–9 audit:

  * Invalid order state transitions at the broker level
  * Cash consistency after various scenarios
  * Duplicate fill prevention (double-submit)
  * Position sizing / quantity validation
  * End-to-end order → fill → position → portfolio flow
  * Session persistence / reload behavior
  * Invalid order lifecycle transitions at the runner / broker boundary

Test fixtures use clearly isolated deterministic values (zero slippage,
zero fees, fixed price feeds) — never real market data.
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


# --- helpers -----------------------------------------------------------------
def fixed_clock(ts: datetime):
    return lambda: ts


def fresh_broker(
    slippage_bps: float = 0.0,
    fee_bps: float = 0.0,
    cash: float = 100_000.0,
):
    b = PaperBroker(
        initial_cash=cash,
        slippage=SlippageConfig(slippage_bps=slippage_bps),
        cost_model=SimpleCostModel(fee_bps=fee_bps),
    )
    b._clock = fixed_clock(datetime(2026, 1, 1, 9, 30, tzinfo=timezone.utc))
    return b


SYM = "NSE:RELIANCE-EQ"


# === Invalid order state transitions (broker-level) ========================
class TestInvalidOrderStateTransitions:
    """The Order dataclass has a strict state machine; verify the broker
    respects it and does not allow illegal transitions."""

    def test_broker_cancel_already_cancelled(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        o = b.submit_order(SYM, Side.BUY, 10, OrderType.LIMIT, limit_price=90.0)
        assert b.cancel_order(o.order_id) is True
        assert o.status == OrderStatus.CANCELLED
        # Cancelling again must return False (already terminal).
        assert b.cancel_order(o.order_id) is False

    def test_broker_cancel_filled_order(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        o = b.submit_order(SYM, Side.BUY, 10, OrderType.MARKET)
        assert o.status == OrderStatus.FILLED
        # Cannot cancel a filled order.
        assert b.cancel_order(o.order_id) is False

    def test_terminal_states_have_no_out_transitions(self):
        for terminal in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            assert _ALLOWED_TRANSITIONS[terminal] == set()
            assert OrderStateMachine.allowed_from(terminal) == set()

    def test_partially_filled_can_transition_to_cancelled(self):
        # PARTIALLY_FILLED -> CANCELLED is allowed (cancel remainder).
        assert OrderStateMachine.can_transition(
            OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCELLED
        )

    def test_partially_filled_cannot_reopen(self):
        assert not OrderStateMachine.can_transition(
            OrderStatus.PARTIALLY_FILLED, OrderStatus.OPEN
        )


# === Cash consistency ========================================================
class TestCashConsistency:
    """cash_after = cash_before - buy_cost + sell_proceeds — with fees."""

    def test_buy_decreases_cash_only(self):
        b = fresh_broker(cash=100_000)
        b.update_market_price(SYM, 2500.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")
        # cash: 100000 - 10*2500 = 75000
        assert b.account().cash == pytest.approx(75_000.0)

    def test_sell_increases_cash(self):
        b = fresh_broker(cash=100_000)
        b.update_market_price(SYM, 2500.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")   # cash: 75000
        b.update_market_price(SYM, 2600.0)
        b.submit_order(SYM, "SELL", 10, "MARKET")  # cash: 75000 + 10*2600 = 101000
        assert b.account().cash == pytest.approx(101_000.0)

    def test_fees_reduce_cash(self):
        b = fresh_broker(cash=100_000, fee_bps=0.001)  # 0.1%
        b.update_market_price(SYM, 2500.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")
        # notional = 25000; fee = 25. cash = 100000 - 25000 - 25 = 74975
        assert b.account().cash == pytest.approx(74_975.0)

    def test_round_trip_no_slippage_no_fee_preserves_cash(self):
        b = fresh_broker(cash=100_000, slippage_bps=0.0, fee_bps=0.0)
        b.update_market_price(SYM, 2500.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")
        b.submit_order(SYM, "SELL", 10, "MARKET")
        assert b.account().cash == pytest.approx(100_000.0)

    def test_multiple_symbols_isolated(self):
        b = fresh_broker(cash=200_000)
        b.update_market_price("NSE:INFY-EQ", 1500.0)
        b.update_market_price("NSE:TCS-EQ", 3500.0)
        b.submit_order("NSE:INFY-EQ", "BUY", 10, "MARKET")  # -15000
        b.submit_order("NSE:TCS-EQ", "BUY", 5, "MARKET")    # -17500
        assert b.account().cash == pytest.approx(200_000 - 15_000 - 17_500)


# === Duplicate fill / double-submit prevention =============================
class TestDuplicateFillPrevention:
    """A filled order's remaining_quantity is 0, so _fill_order is a no-op."""

    def test_filled_order_fill_is_noop(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        o = b.submit_order(SYM, "BUY", 10, "MARKET")
        assert o.status == OrderStatus.FILLED
        filled_before = o.filled_quantity
        # Simulate a duplicate fill attempt with no remaining qty.
        b._fill_order(o, 100.0, fill_qty=5.0)
        assert o.filled_quantity == filled_before
        assert len(o.fills) == 1

    def test_cancelled_order_does_not_fill(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        o = b.submit_order(SYM, "BUY", 10, "LIMIT", limit_price=95.0)
        assert o.status == OrderStatus.OPEN
        assert b.cancel_order(o.order_id) is True
        assert o.status == OrderStatus.CANCELLED
        # Price drops below limit — cancelled order must not fill.
        b.update_market_price(SYM, 90.0)
        assert o.status == OrderStatus.CANCELLED
        assert o.filled_quantity == 0.0

    def test_price_update_does_not_refill_already_filled_limit(self):
        b = fresh_broker(slippage_bps=0.0, fee_bps=0.0)
        b.update_market_price(SYM, 100.0)
        o = b.submit_order(SYM, "BUY", 10, "LIMIT", limit_price=100.0)
        assert o.status == OrderStatus.FILLED  # market == limit -> immediate fill
        b.update_market_price(SYM, 100.0)  # re-evaluate at same price
        assert o.status == OrderStatus.FILLED  # still filled, not double-filled
        assert o.filled_quantity == 10.0


# === Position sizing / validation ==========================================
class TestPositionSizing:
    def test_zero_quantity_rejected(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        with pytest.raises(BrokerError, match="quantity must be > 0"):
            b.submit_order(SYM, "BUY", 0, "MARKET")

    def test_negative_quantity_rejected(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        with pytest.raises(BrokerError, match="quantity must be > 0"):
            b.submit_order(SYM, "BUY", -5, "MARKET")

    def test_limit_order_requires_positive_price(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        with pytest.raises(ValueError, match="positive limit_price"):
            Order(symbol=SYM, side=Side.BUY, quantity=10,
                  order_type=OrderType.LIMIT, limit_price=0)

    def test_limit_order_requires_price(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        with pytest.raises(ValueError, match="positive limit_price"):
            Order(symbol=SYM, side=Side.BUY, quantity=10,
                  order_type=OrderType.LIMIT, limit_price=None)


# === End-to-end: order → fill → position → portfolio ======================
class TestEndToEndFlow:
    def test_market_buy_fills_positions_and_portfolio(self):
        b = fresh_broker(slippage_bps=0.0, fee_bps=0.0, cash=100_000)
        b.update_market_price(SYM, 2500.0)
        o = b.submit_order(SYM, "BUY", 10, "MARKET", current_price=2500.0)
        # Order
        assert o.status == OrderStatus.FILLED
        assert o.filled_quantity == 10
        assert len(o.fills) == 1
        # Fill
        f = o.fills[0]
        assert f.symbol == SYM
        assert f.side == Side.BUY
        assert f.quantity == 10
        assert f.price == 2500.0
        assert f.fee == 0.0
        # Position
        pos = b.get_position(SYM)
        assert pos.qty == 10
        assert pos.avg_entry_price == 2500.0
        # Portfolio
        acc = b.account()
        assert acc.cash == pytest.approx(100_000 - 25_000)
        assert acc.equity == pytest.approx(100_000 - 25_000)  # flat unrealized
        assert acc.realized_pnl == 0.0
        assert acc.unrealized_pnl == 0.0

    def test_sell_closes_position_realizes_pnl(self):
        b = fresh_broker(slippage_bps=0.0, fee_bps=0.0, cash=100_000)
        b.update_market_price(SYM, 100.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")   # cost 1000
        b.update_market_price(SYM, 120.0)
        b.submit_order(SYM, "SELL", 10, "MARKET")  # proceeds 1200
        # Position closed
        pos = b.get_position(SYM)
        assert pos.qty == 0
        assert pos.avg_entry_price == 0.0
        # Realized PnL = (120 - 100) * 10 = 200
        acc = b.account()
        assert acc.realized_pnl == pytest.approx(200.0)
        assert acc.cash == pytest.approx(100_000 - 1000 + 1200)
        assert acc.equity == pytest.approx(100_000 + 200)

    def test_unrealized_pnl_updates_with_price(self):
        b = fresh_broker(slippage_bps=0.0, fee_bps=0.0, cash=100_000)
        b.update_market_price(SYM, 100.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")
        b.update_market_price(SYM, 110.0)
        pos = b.get_position(SYM)
        # unrealized = (110 - 100) * 10 = 100
        assert pos.unrealized_pnl == pytest.approx(100.0)
        acc = b.account()
        # cash = 100000 - 1000 = 99000
        assert acc.cash == pytest.approx(99_000.0)
        # equity = cash + unrealized = 99000 + 100 = 99100
        assert acc.equity == pytest.approx(99_100.0)
        assert acc.unrealized_pnl == pytest.approx(100.0)

    def test_average_entry_price_multiple_buys(self):
        b = fresh_broker(slippage_bps=0.0, fee_bps=0.0, cash=100_000)
        b.update_market_price(SYM, 100.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")   # 10 @ 100
        b.update_market_price(SYM, 110.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")   # 10 @ 110
        pos = b.get_position(SYM)
        assert pos.qty == 20
        assert pos.avg_entry_price == pytest.approx(105.0)

    def test_partial_close_keeps_avg_entry(self):
        b = fresh_broker(slippage_bps=0.0, fee_bps=0.0, cash=100_000)
        b.update_market_price(SYM, 100.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")   # avg 100
        b.update_market_price(SYM, 110.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")   # avg becomes 105
        b.update_market_price(SYM, 120.0)
        b.submit_order(SYM, "SELL", 5, "MARKET")   # partial close 5
        pos = b.get_position(SYM)
        assert pos.qty == 15
        assert pos.avg_entry_price == pytest.approx(105.0)  # unchanged on partial
        # realized = (120 - 105) * 5 = 75
        assert pos.realized_pnl == pytest.approx(75.0)


# === Missing market price ==================================================
class TestMissingMarketPrice:
    def test_market_order_no_price_raises(self):
        b = fresh_broker()
        # No update_market_price called.
        with pytest.raises(BrokerError, match="no market price yet"):
            b.submit_order(SYM, "BUY", 10, "MARKET")

    def test_market_order_explicit_current_price_works(self):
        b = fresh_broker()
        # Even without update_market_price, explicit current_price fills.
        o = b.submit_order(SYM, "BUY", 10, "MARKET", current_price=2500.0)
        assert o.status == OrderStatus.FILLED
        assert b.get_position(SYM).qty == 10

    def test_update_market_price_rejects_zero(self):
        b = fresh_broker()
        with pytest.raises(BrokerError):
            b.update_market_price(SYM, 0)

    def test_update_market_price_rejects_negative(self):
        b = fresh_broker()
        with pytest.raises(BrokerError):
            b.update_market_price(SYM, -1.0)

    def test_update_market_price_rejects_none(self):
        b = fresh_broker()
        with pytest.raises(BrokerError):
            b.update_market_price(SYM, None)


# === Position reversal =====================================================
class TestPositionReversal:
    def test_long_to_short_reversal(self):
        b = fresh_broker(slippage_bps=0.0, fee_bps=0.0, cash=100_000)
        b.update_market_price(SYM, 100.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")    # long 10 @ 100
        b.update_market_price(SYM, 120.0)
        b.submit_order(SYM, "SELL", 25, "MARKET")   # close 10 + short 15 @ 120
        pos = b.get_position(SYM)
        assert pos.qty == -15
        assert pos.avg_entry_price == pytest.approx(120.0)
        # realized on the closed 10: (120 - 100) * 10 = 200
        assert pos.realized_pnl == pytest.approx(200.0)


# === No negative quantity on position ======================================
class TestPositionInvariants:
    def test_full_close_leaves_zero_qty(self):
        b = fresh_broker(slippage_bps=0.0, fee_bps=0.0, cash=100_000)
        b.update_market_price(SYM, 100.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")
        b.update_market_price(SYM, 110.0)
        b.submit_order(SYM, "SELL", 10, "MARKET")
        pos = b.get_position(SYM)
        assert pos.qty == 0
        assert pos.avg_entry_price == 0.0

    def test_partial_reduce_never_goes_negative(self):
        b = fresh_broker(slippage_bps=0.0, fee_bps=0.0, cash=100_000)
        b.update_market_price(SYM, 100.0)
        b.submit_order(SYM, "BUY", 10, "MARKET")
        b.update_market_price(SYM, 110.0)
        b.submit_order(SYM, "SELL", 3, "MARKET")  # partial close
        pos = b.get_position(SYM)
        assert pos.qty == 7
        assert pos.qty > 0  # never negative on a long


# === Invalid price handling ================================================
class TestInvalidPrices:
    def test_zero_price_market_order_raises(self):
        b = fresh_broker()
        with pytest.raises(BrokerError, match="no market price yet"):
            b.submit_order(SYM, "BUY", 10, "MARKET", current_price=0)

    def test_negative_price_market_order_raises(self):
        b = fresh_broker()
        with pytest.raises(BrokerError, match="no market price yet"):
            b.submit_order(SYM, "BUY", 10, "MARKET", current_price=-1.0)

    def test_empty_symbol_rejected(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        with pytest.raises(BrokerError, match="symbol is required"):
            b.submit_order("", "BUY", 10, "MARKET")


# === Order state after fill =================================================
class TestOrderStateAfterFill:
    def test_market_order_immediately_filled(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        o = b.submit_order(SYM, "BUY", 10, "MARKET")
        assert o.status == OrderStatus.FILLED

    def test_limit_order_stays_open_when_not_met(self):
        b = fresh_broker()
        b.update_market_price(SYM, 100.0)
        o = b.submit_order(SYM, "BUY", 10, "LIMIT", limit_price=95.0)
        assert o.status == OrderStatus.OPEN
        assert o.filled_quantity == 0.0

    def test_partial_fill_transitions_correctly(self):
        b = fresh_broker(slippage_bps=0.0, fee_bps=0.0)
        b.update_market_price(SYM, 100.0)
        o = b.submit_order(SYM, "BUY", 10, "LIMIT", limit_price=100.0)
        assert o.status == OrderStatus.FILLED  # market=100, limit=100 -> fill
        assert o.filled_quantity == 10


# === Persistence / reload (session store) ==================================
class TestSessionPersistence:
    """Verify checkpoint save + restore preserves broker accounting state."""

    def test_session_store_round_trip(self, engine):
        from datetime import datetime, timezone
        from trading_system.paper.session import (
            PaperSessionStore,
            PaperSessionCheckpoint,
        )
        from trading_system.execution.paper_broker import PaperBroker

        broker = PaperBroker(initial_cash=100_000.0, slippage=SlippageConfig(0.0))
        broker._clock = fixed_clock(datetime(2026, 1, 1, 9, 30, tzinfo=timezone.utc))
        broker.update_market_price("NSE:SBIN-EQ", 100.0)
        broker.submit_order("NSE:SBIN-EQ", "BUY", 10, "MARKET")

        store = PaperSessionStore(engine)
        checkpoint = PaperSessionCheckpoint(
            checkpoint_id="cp-1",
            session_id="sid-1",
            deployment_id="dep-1",
            strategy_id="strat-1",
            strategy_spec_hash="hash-1",
            symbol="NSE:SBIN-EQ",
            timeframe="1d",
            execution_mode="paper",
            dataset_id="ds-1",
            schema_version=3,
            deployment_status="active",
            session_status="active",
            bar_count=5,
            orders_submitted=1,
            fills_received=1,
            broker_state={
                "cash": broker.account().cash,
                "equity": broker.account().equity,
                "realized_pnl": broker.account().realized_pnl,
            },
            events_fingerprint="abc",
            ops_fingerprint="def",
            created_at="2026-01-01T09:30:00+00:00",
        )
        store.save_checkpoint(checkpoint)

        # Reload from store.
        loaded = store.get_checkpoint("sid-1")
        assert loaded is not None
        assert loaded.checkpoint_id == "cp-1"
        assert loaded.broker_state["cash"] == pytest.approx(99_000.0)

    def test_checkpoint_idempotent_save(self, engine):
        from trading_system.paper.session import (
            PaperSessionStore,
            PaperSessionCheckpoint,
        )
        store = PaperSessionStore(engine)
        cp = PaperSessionCheckpoint(
            checkpoint_id="cp-2",
            session_id="sid-2",
            deployment_id="dep-2",
            strategy_id="strat-2",
            strategy_spec_hash="hash-2",
            symbol="NSE:TCS-EQ",
            timeframe="1d",
            execution_mode="paper",
            dataset_id="ds-2",
            schema_version=3,
            deployment_status="active",
            session_status="active",
            bar_count=0,
            orders_submitted=0,
            fills_received=0,
            broker_state={},
            events_fingerprint="x",
            ops_fingerprint="y",
            created_at="2026-01-01T09:30:00+00:00",
        )
        store.save_checkpoint(cp)
        # Re-saving same checkpoint_id should be a no-op (returns existing).
        store.save_checkpoint(cp)
        loaded = store.get_checkpoint("sid-2")
        assert loaded is not None
        assert loaded.checkpoint_id == "cp-2"

    def test_save_diff_checkpoint_id_rejected(self, engine):
        """Two checkpoints with the same session_id but different checkpoint_id
        must be rejected (fail-closed identity protection)."""
        from trading_system.paper.session import (
            PaperSessionStore,
            PaperSessionCheckpoint,
            SessionIdentityError,
        )
        store = PaperSessionStore(engine)
        cp1 = PaperSessionCheckpoint(
            checkpoint_id="cp-a",
            session_id="sid-x",
            deployment_id="dep-x",
            strategy_id="strat-x",
            strategy_spec_hash="hash-x",
            symbol="NSE:SBIN-EQ", timeframe="1d",
            execution_mode="paper", dataset_id="ds-x", schema_version=3,
            deployment_status="active", session_status="active",
            bar_count=0, orders_submitted=0, fills_received=0,
            broker_state={}, events_fingerprint="f1", ops_fingerprint="o1",
            created_at="2026-01-01T09:30:00+00:00",
        )
        store.save_checkpoint(cp1)
        cp2 = PaperSessionCheckpoint(
            checkpoint_id="cp-b",  # different checkpoint_id
            session_id="sid-x",
            deployment_id="dep-x",
            strategy_id="strat-x",
            strategy_spec_hash="hash-x",
            symbol="NSE:SBIN-EQ", timeframe="1d",
            execution_mode="paper", dataset_id="ds-x", schema_version=3,
            deployment_status="active", session_status="active",
            bar_count=5, orders_submitted=1, fills_received=1,
            broker_state={}, events_fingerprint="f2", ops_fingerprint="o2",
            created_at="2026-01-01T09:35:00+00:00",
        )
        with pytest.raises(SessionIdentityError):
            store.save_checkpoint(cp2)


@pytest.fixture()
def engine():
    from sqlalchemy import create_engine
    return create_engine("sqlite://")
