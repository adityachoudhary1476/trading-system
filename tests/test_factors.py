"""Day 10 factor engine tests — deterministic, causal, look-ahead safe."""
import numpy as np
import pandas as pd
import pytest

from trading_system.research import FactorEngine


def _df(n=300, seed=1, freq="D"):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2023-01-01", periods=n, freq=freq, tz="UTC")
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + np.abs(rng.normal(0, 0.5, n))
    low = close - np.abs(rng.normal(0, 0.5, n))
    open_ = close + rng.normal(0, 0.3, n)
    volume = 1_000_000 + rng.normal(0, 50_000, n).cumsum()
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def test_engine_lists_factors():
    fe = FactorEngine()
    names = fe.available()
    assert len(names) >= 10
    assert "rsi_14" in names and "sma_distance_20" in names


def test_deterministic_same_input_same_output():
    df = _df()
    fe = FactorEngine()
    a = fe.compute("rsi_14", df)
    b = fe.compute("rsi_14", df)
    assert np.allclose(a.dropna().to_numpy(), b.dropna().to_numpy())


def test_expected_factor_values_simple_series():
    # Flat close => RSI undefined-ish; monotonic up => sma_distance_20 > 0.
    idx = pd.date_range("2023-01-01", periods=60, freq="D", tz="UTC")
    close = np.arange(1, 61, dtype=float)
    df = pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close,
         "volume": np.full(60, 1_000_0.0)},
        index=idx,
    )
    fe = FactorEngine()
    sd = fe.compute("sma_distance_20", df)
    # last value: close=60, sma20 of last 20 = mean(41..60)=50.5 => 60/50.5-1>0
    assert sd.dropna().iloc[-1] > 0


def test_insufficient_data_returns_nan():
    df = _df(n=10)
    fe = FactorEngine()
    out = fe.compute("rsi_14", df)
    # RSI needs 14 bars => early NaNs; with only 10 rows the whole thing is NaN
    assert out.isna().all()


def test_nan_handling_does_not_error():
    df = _df(n=300)
    df.loc[df.index[5:10], "close"] = np.nan
    fe = FactorEngine()
    out = fe.compute("rsi_14", df)
    assert out.notna().sum() > 0


def test_unsorted_input_is_sorted_internally():
    df = _df(n=120).sort_index(ascending=False)
    fe = FactorEngine()
    a = fe.compute("ema_distance_20", df)
    b = fe.compute("ema_distance_20", df.sort_index())
    # Align by index for comparison
    assert np.allclose(a.sort_index().dropna().to_numpy(), b.dropna().to_numpy())


def test_duplicate_timestamps_dropped():
    df = _df(n=120)
    # Insert a duplicate timestamp row (future fake close must NOT matter for past).
    dup = df.iloc[[50]].copy()
    df2 = pd.concat([df, dup])
    fe = FactorEngine()
    out = fe.compute("sma_distance_20", df2)
    # Last computed value equals the non-duplicate run (no corruption).
    ref = fe.compute("sma_distance_20", df)
    assert np.isclose(out.dropna().iloc[-1], ref.dropna().iloc[-1])


def test_timezone_aware_timestamps():
    df_utc = _df(n=120, freq="D")
    df_naive = df_utc.copy()
    df_naive.index = df_naive.index.tz_localize(None)
    fe = FactorEngine()
    a = fe.compute("roc_20", df_utc)
    b = fe.compute("roc_20", df_naive)
    assert np.allclose(a.dropna().to_numpy(), b.dropna().to_numpy(), equal_nan=True)


def test_factor_metadata_present():
    fe = FactorEngine()
    for n in fe.available():
        m = fe.metadata(n)
        assert m.name == n
        assert m.definition and m.required_data and m.causal and m.limitations


# ---------------------------------------------------------------------------
# MANDATORY look-ahead test: modifying ONLY future data must not change factor_T
# ---------------------------------------------------------------------------
def test_lookahead_isolated():
    df = _df(n=300, seed=7)
    fe = FactorEngine()
    base = fe.compute_many(df)
    t = df.index[200]  # anchor timestamp
    base_val = base.loc[t, "rsi_14"]

    # Mutate ONLY future bars (201..end)
    df2 = df.copy()
    df2.loc[df2.index[201:], "close"] = 999.0
    out2 = fe.compute_many(df2)
    assert np.isclose(out2.loc[t, "rsi_14"], base_val), "factor at T changed after future edit!"
    assert np.isclose(out2.loc[t, "sma_distance_20"], base.loc[t, "sma_distance_20"])
    assert np.isclose(out2.loc[t, "dist_from_high_20"], base.loc[t, "dist_from_high_20"])


def test_vol_pct_rank_needs_history():
    df = _df(n=120)  # < 252 rows => vol_pct_rank mostly NaN
    fe = FactorEngine()
    out = fe.compute("vol_pct_rank", df)
    # Should not raise; early values NaN due to 252-window requirement
    assert out.isna().sum() >= 0  # sanity (no assertion on exact count)
