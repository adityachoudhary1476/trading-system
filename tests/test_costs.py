"""Tests for the India transaction-cost model (Day 10.5).

Enforces: brokerage cap/floor, futures STT sell-side turnover, options premium STT
sell-side, GST only on taxable components (STT/CTT + stamp EXCLUDED), SEBI turnover
fee, effective-date rate selection (2026-03-31 vs 2026-04-01 differ for F&O STT),
and full determinism.
"""
from __future__ import annotations

from datetime import date

import pytest

from trading_system.research.costs import (
    IndiaTransactionCostModel, TradeSpec, Segment, CostSide, CostNotConfigured,
)


def _trade(seg, side, price, qty, d):
    return TradeSpec(segment=seg, side=side, price=price, quantity=qty, trade_date=d)


def test_brokerage_futures_cap_and_floor():
    m = IndiaTransactionCostModel()
    # Large notional: 0.03% of 1,000,000 = 300 > 20 cap -> 20.
    bd = m.estimate(_trade(Segment.EQUITY_FUTURE, CostSide.BUY, 1000.0, 1000, date(2026, 5, 1)))
    assert bd.brokerage == 20.0
    # Small notional: 0.03% of 10,000 = 3 < 20 cap -> 3.
    bd2 = m.estimate(_trade(Segment.EQUITY_FUTURE, CostSide.BUY, 100.0, 100, date(2026, 5, 1)))
    assert bd2.brokerage == pytest.approx(3.0)


def test_options_brokerage_per_order_flat():
    m = IndiaTransactionCostModel()
    bd = m.estimate(_trade(Segment.EQUITY_OPTION, CostSide.BUY, 50.0, 75, date(2026, 5, 1)))
    assert bd.brokerage == 20.0  # per executed order, not per lot


def test_futures_stt_sell_side_only():
    m = IndiaTransactionCostModel()
    sell = m.estimate(_trade(Segment.EQUITY_FUTURE, CostSide.SELL, 100.0, 100, date(2026, 5, 1)))
    buy = m.estimate(_trade(Segment.EQUITY_FUTURE, CostSide.BUY, 100.0, 100, date(2026, 5, 1)))
    # turnover = 10,000. New STT 0.0005 -> 5.0 on sell only.
    assert sell.stt_ctt == pytest.approx(5.0)
    assert buy.stt_ctt == pytest.approx(0.0)


def test_options_stt_premium_basis_sell():
    m = IndiaTransactionCostModel()
    sell = m.estimate(_trade(Segment.EQUITY_OPTION, CostSide.SELL, 100.0, 50, date(2026, 5, 1)))
    buy = m.estimate(_trade(Segment.EQUITY_OPTION, CostSide.BUY, 100.0, 50, date(2026, 5, 1)))
    # premium turnover = 5,000; new STT 0.0015 -> 7.5 on sell only.
    assert sell.stt_ctt == pytest.approx(7.5)
    assert buy.stt_ctt == pytest.approx(0.0)


def test_gst_excludes_stt_and_stamp():
    m = IndiaTransactionCostModel()
    bd = m.estimate(_trade(Segment.EQUITY_FUTURE, CostSide.SELL, 100.0, 100, date(2026, 5, 1)))
    taxable = bd.brokerage + bd.exchange_charges + bd.sebi_fee + bd.ipft
    expected_gst = 0.18 * taxable
    assert bd.gst == pytest.approx(expected_gst)
    # GST must not be loaded onto STT or stamp duty.
    assert bd.gst < bd.stt_ctt  # by construction here (stt is large, gst small)
    # Explicit: stt contribution to gst is zero.
    assert 0.18 * bd.stt_ctt not in (bd.gst,)  # stylized; the key check is below
    # The clean check: recompute with stt zeroed should give same gst.
    taxable_no_stt = taxable  # stt never in taxable anyway
    assert bd.gst == pytest.approx(0.18 * taxable_no_stt)


def test_sebi_turnover_fee():
    m = IndiaTransactionCostModel()
    bd = m.estimate(_trade(Segment.EQUITY_FUTURE, CostSide.BUY, 100.0, 100, date(2026, 5, 1)))
    turnover = 100.0 * 100.0
    assert bd.sebi_fee == pytest.approx(1e-6 * turnover)


def test_effective_date_stt_change():
    m = IndiaTransactionCostModel()
    # 2026-03-31 (old schedule) vs 2026-04-01 (new schedule) must differ.
    old = m.applicable_stt_rate(Segment.EQUITY_FUTURE, CostSide.SELL, date(2026, 3, 31))
    new = m.applicable_stt_rate(Segment.EQUITY_FUTURE, CostSide.SELL, date(2026, 4, 1))
    assert old != new
    assert old == pytest.approx(0.0002)   # pre-2026-04-01 futures STT
    assert new == pytest.approx(0.0005)   # post-2026-04-01 futures STT
    # Options too.
    old_o = m.applicable_stt_rate(Segment.EQUITY_OPTION, CostSide.SELL, date(2026, 3, 31))
    new_o = m.applicable_stt_rate(Segment.EQUITY_OPTION, CostSide.SELL, date(2026, 4, 1))
    assert old_o == pytest.approx(0.00125)
    assert new_o == pytest.approx(0.0015)


def test_missing_rate_raises_not_zero():
    # A segment with no configured rates should raise, not return zero costs.
    m = IndiaTransactionCostModel(rates=[])
    with pytest.raises(CostNotConfigured):
        m.estimate(_trade(Segment.EQUITY_FUTURE, CostSide.BUY, 100.0, 100, date(2026, 5, 1)))


def test_determinism():
    m = IndiaTransactionCostModel()
    a = m.estimate(_trade(Segment.EQUITY_OPTION, CostSide.SELL, 100.0, 50, date(2026, 5, 1)))
    b = m.estimate(_trade(Segment.EQUITY_OPTION, CostSide.SELL, 100.0, 50, date(2026, 5, 1)))
    assert a == b
    for f in ("brokerage", "stt_ctt", "exchange_charges", "sebi_fee", "stamp_duty", "gst", "ipft", "total"):
        assert getattr(a, f) == getattr(b, f)


def test_total_equals_sum_components():
    m = IndiaTransactionCostModel()
    bd = m.estimate(_trade(Segment.EQUITY_FUTURE, CostSide.SELL, 100.0, 100, date(2026, 5, 1)))
    s = bd.brokerage + bd.stt_ctt + bd.exchange_charges + bd.sebi_fee + bd.stamp_duty + bd.gst + bd.ipft
    assert bd.total == pytest.approx(s)
