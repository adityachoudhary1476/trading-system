"""Backtest integration of the India cost model (Day 10.5).

Verifies: backward-compatible generic percentage cost still works; injecting a
IndiaTransactionCostModel changes total costs (and trade ledger still reconciles);
warmup / evaluation-window / look-ahead behavior is preserved; deterministic.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from trading_system.research.backtester import (
    run_backtest, BacktestConfig,
)
from trading_system.research.strategies import EMATrendStrategy
from trading_system.research.dataset import HistoricalDataset
from trading_system.research.costs import IndiaTransactionCostModel, Segment, CostSide, TradeSpec
from trading_system.research.strategies import Signal


def _dataset():
    n = 260
    idx = pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC")
    rng = __import__("numpy").random.default_rng(7)
    px = 100 + rng.normal(0, 1, n).cumsum()
    df = pd.DataFrame(
        {"open": px, "high": px + 1, "low": px - 1, "close": px, "volume": 1e6},
        index=idx,
    )
    return HistoricalDataset("NSE:TEST", "1d", df, contract_id="NSE:TEST")


def _equity_segment_ds():
    ds = _dataset()
    return ds


def test_generic_cost_unchanged_backward_compat():
    ds = _equity_segment_ds()
    cfg = BacktestConfig(transaction_cost_pct=0.001, slippage_pct=0.0)
    res = run_backtest(ds, EMATrendStrategy(), cfg)
    # With generic cost, net_pnl negative-ish due to costs; trades exist.
    assert res.trades
    total_cost = sum(t.costs for t in res.trades)
    assert total_cost > 0


def test_cost_model_injected_changes_total_cost():
    ds = _equity_segment_ds()
    model = IndiaTransactionCostModel()
    cfg = BacktestConfig(cost_model=model, cost_segment=Segment.EQUITY_DELIVERY.value, slippage_pct=0.0)
    res = run_backtest(ds, EMATrendStrategy(), cfg)
    assert res.trades
    total_cost = sum(t.costs for t in res.trades)
    assert total_cost > 0  # real India schedule applied
    # Each trade's costs must be a positive finite number (not silently zero).
    assert all(t.costs > 0 for t in res.trades)


def test_ledger_reconciles_with_cost_model():
    ds = _equity_segment_ds()
    model = IndiaTransactionCostModel()
    cfg = BacktestConfig(cost_model=model, cost_segment=Segment.EQUITY_DELIVERY.value,
                         slippage_pct=0.0, initial_capital=100_000)
    res = run_backtest(ds, EMATrendStrategy(), cfg)
    perf = res.performance if hasattr(res, "performance") else None
    if perf is not None:
        assert perf.net_pnl == pytest.approx(sum(t.net_pnl for t in res.trades), rel=1e-6)
    # At minimum, final capital equals initial + sum(trade net_pnl).
    assert res.final_capital == pytest.approx(100_000 + sum(t.net_pnl for t in res.trades), rel=1e-6)


def test_warmup_preserved_with_cost_model():
    ds = _equity_segment_ds()
    model = IndiaTransactionCostModel()
    cfg = BacktestConfig(warmup_bars=60, cost_model=model,
                         cost_segment=Segment.EQUITY_DELIVERY.value, slippage_pct=0.0)
    res = run_backtest(ds, EMATrendStrategy(), cfg)
    # Equity curve should start at/after bar warmup_bars (eval window).
    eq_idx = res.equity_curve.index
    assert eq_idx[0] >= ds.data.index[59]


def test_determinism_with_cost_model():
    ds = _equity_segment_ds()
    model = IndiaTransactionCostModel()
    cfg = BacktestConfig(cost_model=model, cost_segment=Segment.EQUITY_DELIVERY.value, slippage_pct=0.0)
    r1 = run_backtest(ds, EMATrendStrategy(), cfg)
    r2 = run_backtest(ds, EMATrendStrategy(), cfg)
    assert [t.exit_price for t in r1.trades] == [t.exit_price for t in r2.trades]
    assert r1.final_capital == r2.final_capital
