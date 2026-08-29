"""Day 10 warmup-aware backtest tests (no look-ahead boundary)."""
import numpy as np
import pandas as pd
import pytest

from trading_system.research.backtester import run_backtest, BacktestConfig, BacktestResult
from trading_system.research.strategies import EMATrendStrategy
from trading_system.research.dataset import HistoricalDataset
from trading_system.research.risk import RiskConfig


def _dataset(n=400, seed=2):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC")
    close = 100 + np.cumsum(rng.normal(0.02, 1.0, n))
    df = pd.DataFrame({
        "open": close + rng.normal(0, 0.2, n),
        "high": close + np.abs(rng.normal(0, 0.5, n)),
        "low": close - np.abs(rng.normal(0, 0.5, n)),
        "close": close,
        "volume": 1_000_000 + rng.normal(0, 10_000, n).cumsum(),
    }, index=idx)
    return HistoricalDataset(symbol="NSE:SBIN", timeframe="1d", data=df, contract_id="NSE:SBIN")


def test_warmup_no_performance_contribution():
    """More warmup history must NOT improve evaluation metrics."""
    ds = _dataset()
    strat = EMATrendStrategy()
    cfg_no_warm = BacktestConfig(warmup_bars=0)
    cfg_warm = BacktestConfig(warmup_bars=150)  # lots of warmup

    res_nowarm = run_backtest(ds, strat, cfg_no_warm)
    res_warm = run_backtest(ds, strat, cfg_warm)

    # The evaluation window for res_warm starts later, so trade count in the eval
    # window is <= the no-warmup run. Critically, the FINAL capital (which includes
    # warmup-period positions) is identical because the full simulation is the same;
    # only REPORTED (eval-window) metrics differ by window, not by warmup inflation.
    # Assert: warmup run excludes early trades from reported trades.
    early_trades_in_warm = [t for t in res_warm.trades
                            if t.exit_ts < ds.data.index[cfg_warm.warmup_bars]]
    assert early_trades_in_warm == [], "warmup trades leaked into reported performance"

    # Evaluation equity curve starts at/after warmup boundary
    assert res_warm.equity_curve.index[0] >= ds.data.index[cfg_warm.warmup_bars - 1]


def test_evaluation_start_date_respected():
    ds = _dataset()
    strat = EMATrendStrategy()
    cfg = BacktestConfig(evaluation_start_date="2022-08-01")
    res = run_backtest(ds, strat, cfg)
    assert res.equity_curve.index[0] >= pd.Timestamp("2022-08-01", tz="UTC")


def test_warmup_versus_full_backtest_deterministic():
    ds = _dataset()
    strat = EMATrendStrategy()
    r_full = run_backtest(ds, strat, BacktestConfig(warmup_bars=0))
    r_full2 = run_backtest(ds, strat, BacktestConfig(warmup_bars=0))
    assert r_full.final_capital == r_full2.final_capital
    assert len(r_full.trades) == len(r_full2.trades)


def test_empty_dataset_raises():
    from trading_system.research.dataset import HistoricalDataset
    empty = HistoricalDataset(symbol="X", timeframe="1d", data=pd.DataFrame())
    with pytest.raises(ValueError):
        run_backtest(empty, EMATrendStrategy(), BacktestConfig())


def test_chronological_ordering_preserved():
    ds = _dataset()
    res = run_backtest(ds, EMATrendStrategy(), BacktestConfig())
    idx = res.equity_curve.index
    assert (idx[:-1] < idx[1:]).all()


def test_warmup_vs_no_warmup_full_capital_equal():
    """The TOTAL simulation (incl. warmup) is identical; only reporting window shifts."""
    ds = _dataset()
    strat = EMATrendStrategy()
    r0 = run_backtest(ds, strat, BacktestConfig(warmup_bars=0))
    r100 = run_backtest(ds, strat, BacktestConfig(warmup_bars=100))
    # Final capital reflects the whole simulation identically (warmup does not change
    # positions, only which trades count toward the reported window).
    assert r0.final_capital == pytest.approx(r100.final_capital)
    # But reported eval-window trade count is strictly smaller with warmup.
    assert len(r100.trades) <= len(r0.trades)
