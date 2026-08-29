"""Day 10 factor-analysis tests — IC/IR, grouped backtest, breakeven, no look-ahead."""
import numpy as np
import pandas as pd
import pytest

from trading_system.research.factor_analysis import (
    compute_ic_series, ic_statistics, grouped_backtest, breakeven_fee_bps,
    forward_return, MIN_CROSS_SECTION,
)


def _universe(n_dates=400, n_inst=8, seed=3, predictive=True):
    """Build a factor panel + price panel. If predictive, factor leads return."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2023-01-01", periods=n_dates, freq="D", tz="UTC")
    insts = [f"NSE:S{i}" for i in range(n_inst)]
    prices = pd.DataFrame(index=idx, columns=insts, dtype=float)
    factor = pd.DataFrame(index=idx, columns=insts, dtype=float)
    for j, inst in enumerate(insts):
        rets = rng.normal(0.0005, 0.02, n_dates)
        prices[inst] = 100 * np.cumprod(1 + rets)
        # factor = lagged signal + noise; if predictive, future return correlates
        signal = rng.normal(0, 1, n_dates)
        factor[inst] = signal
    if predictive:
        # Make forward returns correlate with current factor (inject mild predictability)
        fwd = prices.pct_change().shift(-1)
        for inst in insts:
            fwd[inst] = fwd[inst] + 0.01 * factor[inst].shift(1)
        prices = (1 + fwd.fillna(0)).cumprod() * 100
    return factor, prices


def test_forward_return_alignment():
    prices = _universe(n_dates=50, n_inst=4, predictive=False)[1]
    fr = forward_return(prices, lag=1)
    # return at T = price[T+1]/price[T]-1 (uses forward price; labeled as T's fwd ret)
    assert not fr.iloc[0].isna().any()
    assert fr.iloc[-1].isna().all()  # last row has no T+1


def test_ic_alignment_lag():
    factor, prices = _universe(predictive=True)
    fr = forward_return(prices, lag=1)
    ic = compute_ic_series(factor, fr, lag=1)
    assert isinstance(ic, pd.Series)
    assert ic.notna().sum() > 0
    # mean IC should be positive-ish for a predictive factor (sanity, not proof)
    stats = ic_statistics(ic)
    assert stats["n_obs"] >= MIN_CROSS_SECTION


def test_minimum_cross_section():
    # Only 3 instruments => IC must be NaN (below MIN_CROSS_SECTION=5)
    factor, prices = _universe(n_dates=60, n_inst=3, predictive=True)
    fr = forward_return(prices, lag=1)
    ic = compute_ic_series(factor, fr, lag=1)
    assert ic.dropna().empty, "IC computed below minimum cross-section!"


def test_icir_zero_variance_safe():
    # Constant factor => Spearman undefined => NaNs, ICIR NaN (not inf)
    idx = pd.date_range("2023-01-01", periods=60, freq="D", tz="UTC")
    factor = pd.DataFrame(np.ones((60, 6)), index=idx, columns=[f"NSE:S{i}" for i in range(6)])
    prices = pd.DataFrame(np.random.default_rng(1).normal(100, 1, (60, 6)),
                          index=idx, columns=factor.columns)
    fr = forward_return(prices, lag=1)
    ic = compute_ic_series(factor, fr, lag=1)
    stats = ic_statistics(ic)
    assert np.isnan(stats["icir"])
    assert stats["n_obs"] >= 0


def test_spearman_vs_known():
    # Perfect monotone cross-section: at each date, factor varies across instruments
    # and forward return equals the factor -> cross-sectional Spearman IC ~ 1.
    idx = pd.date_range("2023-01-01", periods=30, freq="D", tz="UTC")
    cols = [f"NSE:S{i}" for i in range(6)]
    rng = np.random.default_rng(0)
    factor = pd.DataFrame(rng.normal(0, 1, (30, 6)), index=idx, columns=cols)
    fr = factor.copy()  # forward return exactly proportional to factor rank per date
    ic = compute_ic_series(factor, fr, lag=1)
    assert ic.dropna().iloc[-1] > 0.9


def test_grouped_backtest_runs():
    factor, prices = _universe(predictive=True)
    fr = forward_return(prices, lag=1)
    res = grouped_backtest(factor, fr, n_groups=10)
    assert res.n_dates > 0
    assert res.group_returns.shape[1] == 10
    assert len(res.long_short) > 0


def test_grouped_duplicate_factor_values():
    # All factors identical on some dates => qcut must not crash
    factor, prices = _universe(n_dates=80, n_inst=8, predictive=False)
    factor.iloc[10:20, :] = 5.0  # duplicate values across instruments
    fr = forward_return(prices, lag=1)
    res = grouped_backtest(factor, fr, n_groups=5)
    assert res.n_dates > 0


def test_grouped_insufficient_instruments():
    factor, prices = _universe(n_dates=60, n_inst=3, predictive=True)
    fr = forward_return(prices, lag=1)
    res = grouped_backtest(factor, fr, n_groups=10)
    # fewer than MIN_CROSS_SECTION => no valid dates
    assert res.n_dates == 0


def test_breakeven_half_position_doubles_fee():
    full = breakeven_fee_bps(0.001, n_trades=10, position_size=1.0)
    half = breakeven_fee_bps(0.001, n_trades=10, position_size=0.5)
    assert half / full == pytest.approx(2.0, rel=1e-9)


def test_breakeven_units_explicit():
    # 10 bps (=0.001) portfolio alpha, 20 trades, full size => 0.25 BPS fee breakeven
    # formula: 0.001*1e4/(2*20*1.0) = 10/40 = 0.25 (in BPS)
    fee = breakeven_fee_bps(0.001, 20, 1.0)
    assert fee == pytest.approx(0.25, rel=1e-9)


def test_breakeven_invalid_returns_nan():
    assert np.isnan(breakeven_fee_bps(0.001, 0, 1.0))
    assert np.isnan(breakeven_fee_bps(0.001, 10, 0.0))


# ---------------------------------------------------------------------------
# RESEARCH INTEGRITY: future returns changed after an eval timestamp must NOT
# change the research result computed at that earlier timestamp.
# ---------------------------------------------------------------------------
def test_research_integrity_future_return_change():
    factor, prices = _universe(n_dates=400, n_inst=8, predictive=True, seed=11)
    fr = forward_return(prices, lag=1)
    ic_before = compute_ic_series(factor, fr, lag=1)
    t = factor.index[200]
    val_before = ic_before.loc[t]

    # Drastically change ALL future returns after t (e.g. double them)
    fr2 = fr.copy()
    mask = fr2.index > t
    fr2[mask] = fr2[mask] * 5.0
    ic_after = compute_ic_series(factor, fr2, lag=1)
    # The IC value AT t must be identical — it only depended on factor_t vs fwd_ret_t.
    assert np.isclose(ic_after.loc[t], val_before), "research result changed by future data!"
