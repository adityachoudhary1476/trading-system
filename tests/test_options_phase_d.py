"""Phase D — option lot-size, P&L, cash, and expiry accounting tests.

PAPER ONLY. Deterministic. No live broker, no network, no scheduler, no
strategy logic.

Coverage matrix:

  1.  Basic lot-size accounting (1 contract, explicit lot, premium)
  2.  Multiple contracts × lot_size × premium
  3.  Unrealized profit — (120 - 100) × 50 × 2 = +2000
  4.  Unrealized loss   — (100 - 120) × 50 × 2 = -2000
  5.  Realized profit   — open, mark up, close
  6.  Realized loss     — open, mark down, close
  7.  Cash debit on BUY — premium × lot × contracts
  8.  Cash credit on SELL — premium × lot × contracts
  9.  Risk limit (rupee-denominated max_position_size)
 10.  CE full lifecycle
 11.  PE full lifecycle
 12.  Contract identity — CE/PE/strike/expiry do not collide
 13.  Missing lot size — fail closed
 14.  Historical contract_size — preserved even if instrument lot changes
 15.  Equity regression — exact back-compat
 16.  Expired option — refuse fresh MTM, refuse fresh orders
 17.  No double-multiplication — invariant: P&L(delta) == delta × cs × qty
 18.  Economic value invariant — premium × cs × contracts
 19.  Production-shaped deterministic test — full lifecycle on a deterministic
      instrument with lot_size = 50.

Every test uses deterministic test doubles (no real instrument resolution).
The InstrumentResolver is an explicit callable, not the live registry.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pytest

from trading_system.execution.broker import BrokerError
from trading_system.execution.orders import (
    Fill,
    OrderIntent,
    OrderStatus,
    OrderType,
    Side,
)
from trading_system.execution.paper_broker import (
    PaperBroker,
    SimpleCostModel,
    SlippageConfig,
)
from trading_system.india.instruments import Instrument, InstrumentType
from trading_system.paper_trading import Position
from trading_system.paper_trading.lot_size import resolve_contract_size
from trading_system.paper_trading.option_accounting import (
    OptionAccountingDecision,
    evaluate_option_accounting,
)


UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def fixed_clock():
    return lambda: datetime(2026, 1, 1, 9, 30, tzinfo=UTC)


def future_expiry(days: int = 14) -> str:
    return (datetime.now(UTC).date() + timedelta(days=days)).isoformat()


def past_expiry(days: int = 1) -> str:
    return (datetime.now(UTC).date() - timedelta(days=days)).isoformat()


def make_option(
    *,
    lot_size: Optional[int] = None,
    strike: float = 25000.0,
    expiry: Optional[str] = None,
    option_type: str = "CE",
    underlying: str = "NIFTY",
    exchange: str = "NSE",
) -> Instrument:
    return Instrument.option(
        exchange=exchange,
        underlying=underlying,
        expiry=expiry or future_expiry(14),
        strike=float(strike),
        option_type=option_type,
        provider_symbol=f"NSE_FO|{underlying}{expiry or future_expiry(14)}{int(strike)}{option_type}",
        lot_size=lot_size,
    )


def make_resolver(instruments: dict[str, Instrument]):
    def _resolve(symbol: str) -> Optional[Instrument]:
        return instruments.get(symbol)
    return _resolve


def fresh_broker(
    *,
    cash: float = 1_000_000.0,
    instrument_resolver=None,
    fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
) -> PaperBroker:
    b = PaperBroker(
        initial_cash=cash,
        slippage=SlippageConfig(slippage_bps=slippage_bps),
        cost_model=SimpleCostModel(fee_bps=fee_bps),
        instrument_resolver=instrument_resolver,
    )
    b._clock = fixed_clock()
    return b


# --------------------------------------------------------------------------- #
# 1. Basic lot-size accounting
# --------------------------------------------------------------------------- #
class TestBasicLotSize:
    def test_one_contract_one_lot_size_cash(self):
        instr = make_option(lot_size=50, strike=25000.0, option_type="CE")
        resolver = make_resolver({"NSE:NIFTY25DEC25000CE": instr})
        b = fresh_broker(cash=1_000_000.0, instrument_resolver=resolver)

        b.update_market_price("NSE:NIFTY25DEC25000CE", 100.0)
        b.submit_order(
            "NSE:NIFTY25DEC25000CE", Side.BUY, 1, OrderType.MARKET,
            current_price=100.0,
            options_contract_id=instr.contract_id,
            strike=25000.0, expiry=instr.expiry, option_type="CE",
        )
        # Cash debit = 100 * 50 * 1 = 5000
        assert b.account().cash == pytest.approx(1_000_000.0 - 5_000.0)
        pos = b.get_position("NSE:NIFTY25DEC25000CE")
        assert pos.qty == 1
        assert pos.contract_size == 50

    def test_multiple_contracts_cash(self):
        instr = make_option(lot_size=50, strike=25000.0)
        resolver = make_resolver({"X": instr})
        b = fresh_broker(cash=1_000_000.0, instrument_resolver=resolver)
        b.update_market_price("X", 100.0)
        b.submit_order("X", Side.BUY, 2, OrderType.MARKET, current_price=100.0,
                       options_contract_id=instr.contract_id, strike=25000.0,
                       expiry=instr.expiry, option_type="CE")
        # 2 contracts * 100 * 50 = 10,000
        assert b.account().cash == pytest.approx(1_000_000.0 - 10_000.0)


# --------------------------------------------------------------------------- #
# 2-4. Unrealized P&L
# --------------------------------------------------------------------------- #
class TestUnrealizedPnL:
    def _open_two_contracts(self, *, premium=100.0, lot_size=50):
        instr = make_option(lot_size=lot_size)
        resolver = make_resolver({"X": instr})
        b = fresh_broker(cash=1_000_000.0, instrument_resolver=resolver)
        b.update_market_price("X", premium)
        b.submit_order("X", Side.BUY, 2, OrderType.MARKET, current_price=premium,
                       options_contract_id=instr.contract_id,
                       strike=instr.strike, expiry=instr.expiry,
                       option_type=instr.option_type)
        return b, instr

    def test_unrealized_profit(self):
        b, _ = self._open_two_contracts(premium=100.0)
        b.update_market_price("X", 120.0)
        # (120 - 100) * 50 * 2 = +2000
        assert b.account().unrealized_pnl == pytest.approx(2_000.0)
        assert b.get_position("X").unrealized_pnl == pytest.approx(2_000.0)

    def test_unrealized_loss(self):
        b, _ = self._open_two_contracts(premium=120.0)
        b.update_market_price("X", 100.0)
        # (100 - 120) * 50 * 2 = -2000
        assert b.account().unrealized_pnl == pytest.approx(-2_000.0)
        assert b.get_position("X").unrealized_pnl == pytest.approx(-2_000.0)

    def test_market_value_includes_contract_size(self):
        b, _ = self._open_two_contracts(premium=100.0)
        b.update_market_price("X", 120.0)
        # 2 * 120 * 50 = 12,000
        assert b.get_position("X").market_value == pytest.approx(12_000.0)


# --------------------------------------------------------------------------- #
# 5-6. Realized P&L
# --------------------------------------------------------------------------- #
class TestRealizedPnL:
    def _open(self, premium, lot_size=50):
        instr = make_option(lot_size=lot_size)
        resolver = make_resolver({"X": instr})
        b = fresh_broker(cash=1_000_000.0, instrument_resolver=resolver)
        b.update_market_price("X", premium)
        b.submit_order("X", Side.BUY, 2, OrderType.MARKET, current_price=premium,
                       options_contract_id=instr.contract_id,
                       strike=instr.strike, expiry=instr.expiry,
                       option_type=instr.option_type)
        return b

    def test_realized_profit_full_close(self):
        b = self._open(100.0)
        b.update_market_price("X", 120.0)
        b.submit_order("X", Side.SELL, 2, OrderType.MARKET, current_price=120.0,
                       options_contract_id="cid", strike=25000.0,
                       expiry="2026-01-01", option_type="CE")
        # (120 - 100) * 50 * 2 = 2000
        assert b.account().realized_pnl == pytest.approx(2_000.0)
        assert b.get_position("X").qty == 0

    def test_realized_loss_full_close(self):
        b = self._open(120.0)
        b.update_market_price("X", 100.0)
        b.submit_order("X", Side.SELL, 2, OrderType.MARKET, current_price=100.0,
                       options_contract_id="cid", strike=25000.0,
                       expiry="2026-01-01", option_type="CE")
        # (100 - 120) * 50 * 2 = -2000
        assert b.account().realized_pnl == pytest.approx(-2_000.0)

    def test_partial_close(self):
        b = self._open(100.0)
        b.update_market_price("X", 130.0)
        b.submit_order("X", Side.SELL, 1, OrderType.MARKET, current_price=130.0,
                       options_contract_id="cid", strike=25000.0,
                       expiry="2026-01-01", option_type="CE")
        # (130 - 100) * 50 * 1 = 1500 realized on the closed contract
        assert b.account().realized_pnl == pytest.approx(1_500.0)
        assert b.get_position("X").qty == 1
        # Remaining contract: avg entry still 100, current 130, lot 50 → +1500
        assert b.get_position("X").unrealized_pnl == pytest.approx(1_500.0)


# --------------------------------------------------------------------------- #
# 7-8. Cash movement
# --------------------------------------------------------------------------- #
class TestCashMovement:
    def test_cash_debit_includes_lot_size(self):
        instr = make_option(lot_size=50)
        resolver = make_resolver({"X": instr})
        b = fresh_broker(cash=1_000_000.0, instrument_resolver=resolver)
        b.update_market_price("X", 200.0)
        b.submit_order("X", Side.BUY, 1, OrderType.MARKET, current_price=200.0,
                       options_contract_id=instr.contract_id,
                       strike=instr.strike, expiry=instr.expiry,
                       option_type=instr.option_type)
        # 200 * 50 * 1 = 10,000
        assert b.account().cash == pytest.approx(990_000.0)

    def test_cash_credit_includes_lot_size(self):
        instr = make_option(lot_size=50)
        resolver = make_resolver({"X": instr})
        b = fresh_broker(cash=1_000_000.0, instrument_resolver=resolver)
        b.update_market_price("X", 100.0)
        b.submit_order("X", Side.BUY, 1, OrderType.MARKET, current_price=100.0,
                       options_contract_id=instr.contract_id,
                       strike=instr.strike, expiry=instr.expiry,
                       option_type=instr.option_type)
        b.update_market_price("X", 150.0)
        b.submit_order("X", Side.SELL, 1, OrderType.MARKET, current_price=150.0,
                       options_contract_id=instr.contract_id,
                       strike=instr.strike, expiry=instr.expiry,
                       option_type=instr.option_type)
        # cash after: 1_000_000 - (100*50) + (150*50) = 1_000_000 + 2_500
        assert b.account().cash == pytest.approx(1_002_500.0)

    def test_fees_apply_once(self):
        instr = make_option(lot_size=50)
        resolver = make_resolver({"X": instr})
        b = fresh_broker(cash=1_000_000.0, instrument_resolver=resolver,
                         fee_bps=0.001)  # 0.1% of notional
        b.update_market_price("X", 100.0)
        b.submit_order("X", Side.BUY, 1, OrderType.MARKET, current_price=100.0,
                       options_contract_id=instr.contract_id,
                       strike=instr.strike, expiry=instr.expiry,
                       option_type=instr.option_type)
        # notional = 100 * 1 * 50 = 5000, fee = 0.001 * 5000 = 5.0
        assert b.get_position("X").realized_pnl == 0.0
        assert b.account().cash == pytest.approx(1_000_000.0 - 5_000.0 - 5.0)


# --------------------------------------------------------------------------- #
# 9. Risk limit (rupee-denominated)
# --------------------------------------------------------------------------- #
class TestRiskLimits:
    def test_max_position_size_includes_lot_size(self):
        """A NIFTY option position cannot bypass a rupee-denominated limit
        merely because the code sees ``quantity = 1`` instead of the actual
        lot size."""
        from trading_system.paper.risk import PaperRiskGuard, PaperRiskConfig
        instr = make_option(lot_size=50)
        resolver = make_resolver({"X": instr})
        b = fresh_broker(cash=1_000_000.0, instrument_resolver=resolver)
        b.update_market_price("X", 100.0)
        b.submit_order("X", Side.BUY, 2, OrderType.MARKET, current_price=100.0,
                       options_contract_id=instr.contract_id,
                       strike=instr.strike, expiry=instr.expiry,
                       option_type=instr.option_type)
        b.update_market_price("X", 100.0)
        # Economic value = 2 * 100 * 50 = 10,000 (NOT 200).
        pos = b.get_position("X")
        assert pos.market_value == pytest.approx(10_000.0)
        # With equity 1_000_000 - 10_000 + 0 = 990_000, exposure = 10000/990000
        equity = b.account().equity
        assert equity == pytest.approx(990_000.0)
        guard = PaperRiskGuard(PaperRiskConfig(max_position_value_pct=0.005))
        decision, reason = guard.check(
            max_drawdown=0.0, equity=equity, position=pos,
            rejected_orders=0, consecutive_errors=0,
        )
        # 10000/990000 ~= 0.0101 > 0.005 → halt
        assert decision.value == "halt"
        assert reason and "exposure" in reason


# --------------------------------------------------------------------------- #
# 10-11. CE / PE lifecycle
# --------------------------------------------------------------------------- #
class TestCEandPELifecycle:
    def _lifecycle(self, option_type: str):
        instr = make_option(lot_size=50, option_type=option_type)
        resolver = make_resolver({"X": instr})
        b = fresh_broker(cash=1_000_000.0, instrument_resolver=resolver)
        b.update_market_price("X", 100.0)
        # BUY 1 contract
        b.submit_order("X", Side.BUY, 1, OrderType.MARKET, current_price=100.0,
                       options_contract_id=instr.contract_id,
                       strike=instr.strike, expiry=instr.expiry,
                       option_type=instr.option_type)
        pos = b.get_position("X")
        assert pos.qty == 1
        assert pos.option_type == option_type
        assert pos.contract_size == 50
        # Cash debit includes lot size
        assert b.account().cash == pytest.approx(1_000_000.0 - 5_000.0)
        # Mark up
        b.update_market_price("X", 120.0)
        assert pos.unrealized_pnl == pytest.approx(1_000.0)
        # Close
        b.submit_order("X", Side.SELL, 1, OrderType.MARKET, current_price=120.0,
                       options_contract_id=instr.contract_id,
                       strike=instr.strike, expiry=instr.expiry,
                       option_type=instr.option_type)
        assert b.account().realized_pnl == pytest.approx(1_000.0)
        assert b.get_position("X").qty == 0

    def test_ce_lifecycle(self):
        self._lifecycle("CE")

    def test_pe_lifecycle(self):
        self._lifecycle("PE")


# --------------------------------------------------------------------------- #
# 12. Contract identity
# --------------------------------------------------------------------------- #
class TestContractIdentity:
    def test_ce_pe_different_strike_expiry_do_not_collide(self):
        ce_25k = make_option(strike=25000.0, option_type="CE")
        pe_25k = make_option(strike=25000.0, option_type="PE")
        ce_25100 = make_option(strike=25100.0, option_type="CE")
        ce_next = make_option(strike=25000.0, option_type="CE",
                              expiry=future_expiry(30))
        # contract_id is the canonical identity — different params ≠ same id
        assert ce_25k.contract_id != pe_25k.contract_id
        assert ce_25k.contract_id != ce_25100.contract_id
        assert ce_25k.contract_id != ce_next.contract_id

    def test_broker_keeps_positions_separate(self):
        # Two different strikes of the same underlying remain two positions.
        ce1 = make_option(lot_size=50, strike=25000.0, option_type="CE")
        ce2 = make_option(lot_size=50, strike=25100.0, option_type="CE")
        # Use different symbols because the broker keys positions by symbol.
        b = fresh_broker(
            cash=1_000_000.0,
            instrument_resolver=make_resolver({"CE_25K": ce1, "CE_25100": ce2}),
        )
        b.update_market_price("CE_25K", 100.0)
        b.submit_order("CE_25K", Side.BUY, 1, OrderType.MARKET,
                       current_price=100.0,
                       options_contract_id=ce1.contract_id,
                       strike=25000.0, expiry=ce1.expiry, option_type="CE")
        b.update_market_price("CE_25100", 80.0)
        b.submit_order("CE_25100", Side.BUY, 1, OrderType.MARKET,
                       current_price=80.0,
                       options_contract_id=ce2.contract_id,
                       strike=25100.0, expiry=ce2.expiry, option_type="CE")
        assert len([p for p in b.positions().values() if p.is_open]) == 2


# --------------------------------------------------------------------------- #
# 13. Missing lot size — fail closed
# --------------------------------------------------------------------------- #
class TestMissingLotSize:
    def test_option_without_lot_size_rejected(self):
        instr = make_option(lot_size=None)
        resolver = make_resolver({"X": instr})
        b = fresh_broker(cash=1_000_000.0, instrument_resolver=resolver)
        b.update_market_price("X", 100.0)
        with pytest.raises(BrokerError) as ei:
            b.submit_order("X", Side.BUY, 1, OrderType.MARKET,
                           current_price=100.0,
                           options_contract_id=instr.contract_id,
                           strike=instr.strike, expiry=instr.expiry,
                           option_type=instr.option_type)
        assert "lot_size" in str(ei.value).lower()

    def test_equity_without_resolver_uses_default_1(self):
        # No resolver → back-compat path → contract_size = 1 (equity semantics).
        b = fresh_broker(cash=1_000_000.0)
        b.update_market_price("NSE:RELIANCE", 2500.0)
        b.submit_order("NSE:RELIANCE", Side.BUY, 10, OrderType.MARKET,
                       current_price=2500.0)
        pos = b.get_position("NSE:RELIANCE")
        assert pos.contract_size == 1
        assert pos.market_value == pytest.approx(10 * 2500.0)

    def test_explicit_contract_size_overrides_resolver(self):
        # If the caller (e.g. OrderIntent) supplies contract_size, that wins
        # even if the resolver returns None.
        b = fresh_broker(
            cash=1_000_000.0,
            instrument_resolver=make_resolver({}),  # nothing resolved
        )
        b.update_market_price("X", 100.0)
        b.submit_order(
            "X", Side.BUY, 1, OrderType.MARKET, current_price=100.0,
            options_contract_id="cid", strike=25000.0,
            expiry=future_expiry(14), option_type="CE",
            contract_size=75,
        )
        assert b.get_position("X").contract_size == 75
        assert b.account().cash == pytest.approx(1_000_000.0 - 100.0 * 75.0)


# --------------------------------------------------------------------------- #
# 14. Historical contract_size is durable
# --------------------------------------------------------------------------- #
class TestDurableContractSize:
    def test_position_keeps_original_lot_size(self):
        # Open a position with lot_size=50; then change the registry to
        # lot_size=75; the existing position must still report 50.
        instr = make_option(lot_size=50)
        resolver = make_resolver({"X": instr})
        b = fresh_broker(cash=1_000_000.0, instrument_resolver=resolver)
        b.update_market_price("X", 100.0)
        b.submit_order("X", Side.BUY, 1, OrderType.MARKET,
                       current_price=100.0,
                       options_contract_id=instr.contract_id,
                       strike=instr.strike, expiry=instr.expiry,
                       option_type=instr.option_type)
        assert b.get_position("X").contract_size == 50
        # Exchange revises lot size to 75
        instr.lot_size = 75  # type: ignore[misc]
        # Mark to market; existing position still uses 50.
        b.update_market_price("X", 120.0)
        assert b.get_position("X").contract_size == 50
        # Unrealized = (120 - 100) * 1 * 50 = 1000 (not 1500)
        assert b.get_position("X").unrealized_pnl == pytest.approx(1_000.0)


# --------------------------------------------------------------------------- #
# 15. Equity regression
# --------------------------------------------------------------------------- #
class TestEquityRegression:
    """Existing equity behavior must remain unchanged."""

    def test_equity_position_market_value(self):
        b = fresh_broker(cash=100_000.0)
        b.update_market_price("NSE:RELIANCE", 2500.0)
        b.submit_order("NSE:RELIANCE", Side.BUY, 10, OrderType.MARKET,
                       current_price=2500.0)
        # Back-compat: market_value = qty * price (contract_size=1)
        assert b.get_position("NSE:RELIANCE").market_value == pytest.approx(25_000.0)

    def test_equity_unrealized_pnl(self):
        b = fresh_broker(cash=100_000.0)
        b.update_market_price("NSE:RELIANCE", 100.0)
        b.submit_order("NSE:RELIANCE", Side.BUY, 10, OrderType.MARKET,
                       current_price=100.0)
        b.update_market_price("NSE:RELIANCE", 110.0)
        assert b.get_position("NSE:RELIANCE").unrealized_pnl == pytest.approx(100.0)

    def test_equity_realized_pnl_round_trip(self):
        b = fresh_broker(cash=100_000.0)
        b.update_market_price("NSE:RELIANCE", 100.0)
        b.submit_order("NSE:RELIANCE", Side.BUY, 10, OrderType.MARKET,
                       current_price=100.0)
        b.update_market_price("NSE:RELIANCE", 150.0)
        b.submit_order("NSE:RELIANCE", Side.SELL, 10, OrderType.MARKET,
                       current_price=150.0)
        # (150 - 100) * 10 = 500
        assert b.account().realized_pnl == pytest.approx(500.0)
        assert b.get_position("NSE:RELIANCE").qty == 0


# --------------------------------------------------------------------------- #
# 16. Expired option safety
# --------------------------------------------------------------------------- #
class TestExpiredOptionSafety:
    def test_expired_option_rejects_fresh_mtm(self):
        instr = make_option(lot_size=50, expiry=past_expiry(1))
        resolver = make_resolver({"X": instr})
        b = fresh_broker(cash=1_000_000.0, instrument_resolver=resolver)
        b.update_market_price("X", 100.0)
        b.submit_order("X", Side.BUY, 1, OrderType.MARKET, current_price=100.0,
                       options_contract_id=instr.contract_id,
                       strike=instr.strike, expiry=instr.expiry,
                       option_type=instr.option_type)
        # A second fresh quote for the expired contract is rejected.
        with pytest.raises(BrokerError) as ei:
            b.update_market_price("X", 110.0)
        assert "expired" in str(ei.value).lower()

    def test_unexpired_option_accepts_mtm(self):
        instr = make_option(lot_size=50, expiry=future_expiry(14))
        resolver = make_resolver({"X": instr})
        b = fresh_broker(cash=1_000_000.0, instrument_resolver=resolver)
        b.update_market_price("X", 100.0)
        b.submit_order("X", Side.BUY, 1, OrderType.MARKET, current_price=100.0,
                       options_contract_id=instr.contract_id,
                       strike=instr.strike, expiry=instr.expiry,
                       option_type=instr.option_type)
        # Future expiry → MTM is allowed.
        b.update_market_price("X", 110.0)
        assert b.get_position("X").unrealized_pnl == pytest.approx(500.0)

    def test_evaluate_option_accounting_rejects_expired(self):
        instr = make_option(lot_size=50, expiry=past_expiry(2))
        verdict = evaluate_option_accounting(instr, operation="mark_to_market")
        assert verdict.decision == OptionAccountingDecision.REJECT_EXPIRED
        assert verdict.is_expired is True

    def test_evaluate_option_accounting_rejects_unknown_lot(self):
        instr = make_option(lot_size=None)
        verdict = evaluate_option_accounting(instr, operation="execute")
        assert verdict.decision == OptionAccountingDecision.REJECT_UNKNOWN_LOT_SIZE


# --------------------------------------------------------------------------- #
# 17-18. No double multiplication / property invariants
# --------------------------------------------------------------------------- #
class TestInvariants:
    def test_no_double_multiplication_of_lot_size(self):
        """Property invariant: for a long option position,
            P&L(premium + delta) - P&L(premium) == delta * contract_size * qty.
        """
        for premium in (50.0, 100.0, 150.0, 250.0):
            for delta in (1.0, 5.0, -3.0, 10.0):
                for lot_size in (25, 50, 75):
                    for qty in (1, 2, 5):
                        instr = make_option(lot_size=lot_size)
                        resolver = make_resolver({"X": instr})
                        b = fresh_broker(cash=10_000_000.0,
                                         instrument_resolver=resolver)
                        b.update_market_price("X", premium)
                        b.submit_order(
                            "X", Side.BUY, qty, OrderType.MARKET,
                            current_price=premium,
                            options_contract_id=instr.contract_id,
                            strike=instr.strike, expiry=instr.expiry,
                            option_type=instr.option_type,
                        )
                        p0 = b.get_position("X").unrealized_pnl
                        b.update_market_price("X", premium + delta)
                        p1 = b.get_position("X").unrealized_pnl
                        expected_delta = delta * lot_size * qty
                        assert (p1 - p0) == pytest.approx(expected_delta), (
                            f"premium={premium} delta={delta} lot={lot_size} "
                            f"qty={qty}: P&L delta={p1-p0} != {expected_delta}"
                        )

    def test_economic_value_invariant(self):
        """For any open option position,
            market_value == current_price * contract_size * qty."""
        instr = make_option(lot_size=50)
        resolver = make_resolver({"X": instr})
        b = fresh_broker(cash=10_000_000.0, instrument_resolver=resolver)
        b.update_market_price("X", 100.0)
        b.submit_order("X", Side.BUY, 3, OrderType.MARKET,
                       current_price=100.0,
                       options_contract_id=instr.contract_id,
                       strike=instr.strike, expiry=instr.expiry,
                       option_type=instr.option_type)
        for px in (90.0, 100.0, 130.0, 200.0):
            b.update_market_price("X", px)
            assert b.get_position("X").market_value == pytest.approx(
                3 * px * 50
            )

    def test_cash_effect_invariant(self):
        """Cash change on BUY = price × lot × contracts; on SELL the reverse."""
        instr = make_option(lot_size=50)
        resolver = make_resolver({"X": instr})
        b = fresh_broker(cash=10_000_000.0, instrument_resolver=resolver)
        b.update_market_price("X", 80.0)
        cash_before = b.account().cash
        b.submit_order("X", Side.BUY, 4, OrderType.MARKET,
                       current_price=80.0,
                       options_contract_id=instr.contract_id,
                       strike=instr.strike, expiry=instr.expiry,
                       option_type=instr.option_type)
        assert (cash_before - b.account().cash) == pytest.approx(80.0 * 50 * 4)
        b.update_market_price("X", 100.0)
        cash_before2 = b.account().cash
        b.submit_order("X", Side.SELL, 4, OrderType.MARKET,
                       current_price=100.0,
                       options_contract_id=instr.contract_id,
                       strike=instr.strike, expiry=instr.expiry,
                       option_type=instr.option_type)
        assert (b.account().cash - cash_before2) == pytest.approx(100.0 * 50 * 4)


# --------------------------------------------------------------------------- #
# 19. Production-shaped deterministic test
# --------------------------------------------------------------------------- #
class TestProductionShaped:
    def test_full_option_lifecycle_deterministic(self):
        """Single end-to-end test:

            instrument (lot_size=50)
                → BUY option via PaperBroker
                → Position.contract_size populated
                → cash debited correctly
                → premium changes
                → unrealized P&L correct
                → close option
                → cash credited correctly
                → realized P&L correct
        """
        # 1. Instrument with deterministic lot_size
        instr = make_option(lot_size=50, strike=25000.0, option_type="CE",
                            underlying="NIFTY")
        resolver = make_resolver({"NSE:NIFTY25DEC25000CE": instr})
        b = fresh_broker(cash=1_000_000.0, instrument_resolver=resolver)
        assert b.account().initial_cash == 1_000_000.0

        # 2. BUY 2 contracts at premium 100
        b.update_market_price("NSE:NIFTY25DEC25000CE", 100.0)
        order = b.submit_order(
            "NSE:NIFTY25DEC25000CE", Side.BUY, 2, OrderType.MARKET,
            current_price=100.0,
            options_contract_id=instr.contract_id,
            strike=25000.0, expiry=instr.expiry, option_type="CE",
        )
        assert order.status == OrderStatus.FILLED
        pos = b.get_position("NSE:NIFTY25DEC25000CE")

        # 3. Position.contract_size populated
        assert pos.contract_size == 50
        assert pos.is_option is True
        assert pos.option_type == "CE"
        assert pos.strike == 25000.0
        assert pos.qty == 2

        # 4. Cash debited correctly: 2 * 100 * 50 = 10,000
        assert b.account().cash == pytest.approx(990_000.0)

        # 5. Premium changes to 130
        b.update_market_price("NSE:NIFTY25DEC25000CE", 130.0)
        # 6. Unrealized P&L correct: (130 - 100) * 50 * 2 = 3000
        assert pos.unrealized_pnl == pytest.approx(3_000.0)
        assert pos.market_value == pytest.approx(2 * 130 * 50)
        # Equity = cash + unrealized
        assert b.account().equity == pytest.approx(993_000.0)

        # 7. Close option at 140 (SELL 2 contracts)
        b.update_market_price("NSE:NIFTY25DEC25000CE", 140.0)
        close = b.submit_order(
            "NSE:NIFTY25DEC25000CE", Side.SELL, 2, OrderType.MARKET,
            current_price=140.0,
            options_contract_id=instr.contract_id,
            strike=25000.0, expiry=instr.expiry, option_type="CE",
        )
        assert close.status == OrderStatus.FILLED

        # 8. Cash credited correctly: cash = 990_000 + 140 * 50 * 2 = 1,004,000
        assert b.account().cash == pytest.approx(1_004_000.0)

        # 9. Realized P&L correct: (140 - 100) * 50 * 2 = 4000
        assert b.account().realized_pnl == pytest.approx(4_000.0)
        assert pos.qty == 0
        assert pos.unrealized_pnl == 0.0


# --------------------------------------------------------------------------- #
# Lot-size resolver tests (independent of broker)
# --------------------------------------------------------------------------- #
class TestLotSizeResolver:
    def test_equity_resolves_to_1(self):
        instr = Instrument(
            internal=type("I", (), {"exchange": "NSE", "symbol": "SBIN", "key": "NSE:SBIN"})(),
            instrument_type=InstrumentType.EQUITY,
        )
        resolved = resolve_contract_size(instr)
        assert resolved.contract_size == 1
        assert resolved.is_option is False

    def test_option_with_lot_size(self):
        instr = make_option(lot_size=50)
        resolved = resolve_contract_size(instr)
        assert resolved.contract_size == 50
        assert resolved.is_option is True

    def test_option_without_lot_size(self):
        instr = make_option(lot_size=None)
        resolved = resolve_contract_size(instr)
        assert resolved.contract_size is None
        assert resolved.has_lot_size is False

    def test_abnormal_lot_size_rejected(self):
        instr = make_option(lot_size=0)
        resolved = resolve_contract_size(instr)
        assert resolved.contract_size is None

        instr2 = make_option(lot_size=-5)
        resolved2 = resolve_contract_size(instr2)
        assert resolved2.contract_size is None

    def test_far_future_expiry_not_expired(self):
        instr = make_option(lot_size=50, expiry=future_expiry(30))
        resolved = resolve_contract_size(instr)
        assert resolved.is_expired is False

    def test_past_expiry_is_expired(self):
        instr = make_option(lot_size=50, expiry=past_expiry(5))
        resolved = resolve_contract_size(instr)
        assert resolved.is_expired is True


# --------------------------------------------------------------------------- #
# Safety: no live broker / network references in Phase D modules
# --------------------------------------------------------------------------- #
class TestPhaseDSafety:
    def test_no_live_imports_in_lot_size(self):
        import inspect
        import trading_system.paper_trading.lot_size as ls
        src = inspect.getsource(ls)
        assert "fyers" not in src.lower()
        assert "orders/sync" not in src
        assert "validate-authcode" not in src

    def test_no_live_imports_in_option_accounting(self):
        import inspect
        import trading_system.paper_trading.option_accounting as oa
        src = inspect.getsource(oa)
        assert "fyers" not in src.lower()
        assert "orders/sync" not in src
        assert "validate-authcode" not in src