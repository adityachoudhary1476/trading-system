"""Tests for OHLCV data validation — the safety gate."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_system.data.validation import (
    validate_ohlcv,
    DataValidationError,
    Severity,
)


def _good_df(n=50, start="2024-01-01", freq="1d") -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    rng = np.random.default_rng(0)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    # Guarantee valid OHLC relationships (high/low bracket the range).
    open_ = close + rng.normal(0, 0.5, n)
    high = np.maximum(close, open_) + abs(rng.normal(0, 1, n))
    low = np.minimum(close, open_) - abs(rng.normal(0, 1, n))
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.integers(100, 1000, n).astype(float),
        },
        index=idx,
    )


def test_valid_data_passes():
    rep = validate_ohlcv(_good_df(), "1d")
    assert rep.ok
    assert len(rep.valid) == 50
    assert len(rep.rejected) == 0


def test_missing_values_rejected():
    df = _good_df(10)
    df.loc[df.index[3], "close"] = np.nan
    rep = validate_ohlcv(df, "1d")
    assert not rep.ok
    assert len(rep.rejected) == 1
    codes = {i.code for i in rep.issues}
    assert "MISSING_VALUE" in codes


def test_impossible_price_rejected():
    df = _good_df(10)
    df.loc[df.index[2], "close"] = -5.0
    df.loc[df.index[2], "high"] = -4.0
    df.loc[df.index[2], "low"] = -6.0
    df.loc[df.index[2], "open"] = -5.0
    rep = validate_ohlcv(df, "1d")
    assert not rep.ok
    assert "IMPOSSIBLE_PRICE" in {i.code for i in rep.issues}


def test_bad_ohlc_relationship_rejected():
    df = _good_df(10)
    # Force high < low on one row.
    df.loc[df.index[4], "high"] = 50.0
    df.loc[df.index[4], "low"] = 80.0
    rep = validate_ohlcv(df, "1d")
    assert not rep.ok
    assert "BAD_OHLC" in {i.code for i in rep.issues}


def test_duplicate_timestamps_rejected():
    df = _good_df(10)
    # Duplicate the first index onto the last row.
    dup = df.iloc[[0]].copy()
    df2 = pd.concat([df, dup])
    df2.index = df.index.tolist() + [df.index[0]]
    rep = validate_ohlcv(df2, "1d")
    assert not rep.ok
    assert "ORDERING" in {i.code for i in rep.issues}


def test_out_of_order_timestamps_rejected():
    df = _good_df(10)
    # Reorder so timestamps are not monotonic.
    order = list(range(10))
    order[1], order[5] = order[5], order[1]
    df2 = df.iloc[order]
    rep = validate_ohlcv(df2, "1d")
    assert not rep.ok
    assert "ORDERING" in {i.code for i in rep.issues}


def test_abnormal_gap_warns_not_rejects_by_default():
    df = _good_df(20, freq="1d")
    # Insert a huge gap by shifting the second half far forward.
    idx = df.index.tolist()
    idx[10:] = [t + pd.Timedelta(days=400) for t in idx[10:]]
    df.index = pd.DatetimeIndex(idx, tz="UTC")
    rep = validate_ohlcv(df, "1d")
    # Default: gap is a warning -> report still ok.
    assert "ABNORMAL_GAP" in {i.code for i in rep.issues}
    assert rep.ok


def test_reject_on_gap_enabled():
    df = _good_df(20, freq="1d")
    idx = df.index.tolist()
    idx[10:] = [t + pd.Timedelta(days=400) for t in idx[10:]]
    df.index = pd.DatetimeIndex(idx, tz="UTC")
    rep = validate_ohlcv(df, "1d", reject_on_gap=True)
    assert not rep.ok
    assert "ABNORMAL_GAP" in {i.code for i in rep.issues}


def test_empty_frame_errors():
    rep = validate_ohlcv(pd.DataFrame(), "1d")
    assert not rep.ok
    assert "EMPTY" in {i.code for i in rep.issues}


def test_assert_valid_raises_on_bad_data():
    df = _good_df(10)
    df.loc[df.index[1], "high"] = -1.0
    rep = validate_ohlcv(df, "1d")
    with pytest.raises(DataValidationError):
        from trading_system.data.validation import assert_valid

        assert_valid(rep)
