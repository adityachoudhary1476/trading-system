"""Provider-independent feature engine (Day 7 research).

Operates on normalized OHLCV DataFrames. Every feature is causal: a feature value
at timestamp T depends ONLY on data available at or before T. This is the single
hardest invariant in the module — see :func:`assert_no_lookahead` in the tests and
the ``causal`` guarantees documented per feature.

All indicators are computed with pandas ``shift`` / expanding / rolling over
``[0..T]`` so the engine (which trades on the NEXT bar) never sees future information.

Derivative-specific fields (open interest, IV, greeks, basis) are NOT produced here
because the current storage schema does not contain them. They are listed under
``MISSING_REQUIRED_FEATURES`` so the gap is explicit rather than fabricated.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Reuse the project's existing deterministic quant primitives (Day 1).
from ..analysis.quant import (
    simple_returns,
    log_returns,
    rolling_volatility,
    annualized_volatility,
)
# Reuse the project's existing pure indicator calculations (no duplication).
from ..indicators.indicators import momentum as _momentum_indicator

# Features the engine can actually compute from stored OHLCV.
AVAILABLE_FEATURES = [
    "ret", "log_ret", "sma", "ema", "vol", "atr", "momentum",
    "hl_range", "vol_chg", "vol_ma", "trend", "vol_regime",
]

# Derivative analytics the research layer will need but the DB cannot currently
# supply. Documented so they are never silently invented.
MISSING_REQUIRED_FEATURES = [
    "open_interest", "oi_change", "basis", "basis_pct",
    "option_iv", "option_greeks", "option_delta", "option_theta",
]


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Wilder's true range — available at bar T (uses close[T-1], high[T], low[T])."""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr


def add_features(
    df: pd.DataFrame,
    *,
    sma_windows=(20, 50),
    ema_windows=(12, 26),
    vol_window=20,
    atr_window=14,
    momentum_window=10,
    prefix: str = "",
) -> pd.DataFrame:
    """Return a copy of ``df`` augmented with causal features.

    Parameters
    ----------
    df: OHLCV DataFrame indexed by timestamp with columns open/high/low/close/volume.
    """
    if df is None or len(df) == 0:
        return df
    out = df.copy()
    close = out["close"]

    # --- returns (causal: pct_change uses only [T-1, T]) ---
    out[prefix + "ret"] = simple_returns(close)
    out[prefix + "log_ret"] = log_returns(close)

    # --- SMAs (causal: mean over [T-window+1 .. T]) ---
    for w in sma_windows:
        out[f"{prefix}sma_{w}"] = close.rolling(window=w, min_periods=w).mean()

    # --- EMAs (causal: EWM over [0 .. T]) ---
    for w in ema_windows:
        out[f"{prefix}ema_{w}"] = close.ewm(span=w, adjust=False, min_periods=w).mean()

    # --- rolling volatility of returns (causal) ---
    out[prefix + "vol"] = rolling_volatility(out[prefix + "ret"], window=vol_window)

    # --- ATR (causal: TR[T] uses high[T], low[T], close[T-1]) ---
    tr = true_range(out["high"], out["low"], close)
    out[prefix + "atr"] = tr.rolling(window=atr_window, min_periods=atr_window).mean()

    # --- momentum (causal: close[T] / close[T-window] - 1) ---
    out[prefix + "momentum"] = _momentum_indicator(close, momentum_window)

    # --- high/low range relative to close (causal, uses only T) ---
    out[prefix + "hl_range"] = (out["high"] - out["low"]) / close

    # --- volume change (causal: pct_change) ---
    out[prefix + "vol_chg"] = out["volume"].pct_change()

    # --- rolling volume mean (causal) ---
    out[prefix + "vol_ma"] = out["volume"].rolling(window=vol_window, min_periods=1).mean()

    # --- trend classification (causal): sign of fast ema - slow ema ---
    fast_w, slow_w = ema_windows[0], ema_windows[1]
    out[prefix + "trend"] = np.sign(out[f"{prefix}ema_{fast_w}"] - out[f"{prefix}ema_{slow_w}"])

    # --- volatility regime (causal): compare vol[T] to expanding median of
    #     vol over [0 .. T-1] (shift(1) so the current bar does not peek at itself) ---
    expanding_med = out[prefix + "vol"].shift(1).expanding().median()
    out[prefix + "vol_regime"] = np.where(
        out[prefix + "vol"] > expanding_med, "high", "low"
    )
    return out


def annualized_vol(df: pd.DataFrame, timeframe: str, vol_window: int = 20) -> float:
    """Convenience wrapper around analysis.quant.annualized_volatility."""
    return annualized_volatility(df["ret"], timeframe)
