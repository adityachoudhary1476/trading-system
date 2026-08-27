"""Tests for technical indicators."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_system.indicators import (
    sma, ema, rsi, macd, bollinger_bands, atr, rolling_std, add_all_indicators,
)


def _series(n=60, seed=1):
    rng = np.random.default_rng(seed)
    return pd.Series(100 + np.cumsum(rng.normal(0, 1, n)))


def test_sma_basic():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    out = sma(s, 3)
    assert out.iloc[2] == 2.0
    assert np.isnan(out.iloc[0])


def test_sma_invalid_window():
    with pytest.raises(ValueError):
        sma(_series(10), 0)


def test_ema_matches_pandas():
    s = _series(30)
    out = ema(s, 12)
    ref = s.ewm(span=12, adjust=False).mean()
    assert np.allclose(out.dropna(), ref.dropna())


def test_rsi_bounds():
    s = _series(60)
    out = rsi(s, 14)
    vals = out.dropna()
    assert (vals >= 0).all() and (vals <= 100).all()


def test_rsi_pure_uptrend_is_100():
    s = pd.Series(np.arange(1, 31, dtype=float))  # strictly increasing
    out = rsi(s, 14)
    assert out.dropna().iloc[-1] == 100.0


def test_macd_columns():
    out = macd(_series(60))
    assert list(out.columns) == ["macd", "signal", "histogram"]
    assert out["histogram"].dropna().equals(
        (out["macd"] - out["signal"]).dropna()
    )


def test_bollinger_order():
    out = bollinger_bands(_series(60), 20, 2.0)
    valid = out.dropna()
    assert (valid["upper"] >= valid["middle"]).all()
    assert (valid["middle"] >= valid["lower"]).all()


def test_atr_positive():
    rng = np.random.default_rng(2)
    n = 60
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + abs(rng.normal(0, 0.5, n))
    low = close - abs(rng.normal(0, 0.5, n))
    out = atr(pd.Series(high), pd.Series(low), pd.Series(close), 14)
    assert (out.dropna() > 0).all()


def test_add_all_indicators_shape():
    df = pd.DataFrame(
        {
            "open": _series(60),
            "high": _series(60) + 1,
            "low": _series(60) - 1,
            "close": _series(60),
            "volume": pd.Series(np.full(60, 1000.0)),
        }
    )
    out = add_all_indicators(df)
    for col in ["sma_20", "ema_12", "rsi_14", "bb_upper", "macd", "atr_14"]:
        assert col in out.columns
