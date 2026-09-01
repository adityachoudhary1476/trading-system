"""Technical indicators.

All functions are pure and deterministic: given the same input they always
return the same output, which makes them straightforward to unit-test. They
operate on pandas Series and return pandas Series (with NaN where undefined).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average."""
    if window <= 0:
        raise ValueError("window must be positive")
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    """Exponential moving average."""
    if span <= 0:
        raise ValueError("span must be positive")
    return series.ewm(span=span, adjust=False, min_periods=1).mean()


def momentum(series: pd.Series, window: int) -> pd.Series:
    """Rate-of-change momentum: series[T] / series[T-window] - 1 (causal)."""
    if window <= 0:
        raise ValueError("window must be positive")
    return series / series.shift(window) - 1.0


def rolling_std(series: pd.Series, window: int) -> pd.Series:
    """Rolling standard deviation (population)."""
    if window <= 0:
        raise ValueError("window must be positive")
    return series.rolling(window=window, min_periods=window).std(ddof=0)


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder's smoothing)."""
    if window <= 0:
        raise ValueError("window must be positive")
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi_vals = 100.0 - (100.0 / (1.0 + rs))
    # When avg_loss == 0, RSI is 100 (pure uptrend).
    rsi_vals = rsi_vals.where(avg_loss != 0, 100.0)
    return rsi_vals


def macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """Moving Average Convergence Divergence.

    Returns a DataFrame with columns macd, signal, histogram.
    """
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = macd_line - signal_line
    return pd.DataFrame(
        {"macd": macd_line, "signal": signal_line, "histogram": hist}
    )


def bollinger_bands(
    series: pd.Series, window: int = 20, num_std: float = 2.0
) -> pd.DataFrame:
    """Bollinger Bands."""
    middle = sma(series, window)
    std = rolling_std(series, window)
    upper = middle + num_std * std
    lower = middle - num_std * std
    return pd.DataFrame({"upper": upper, "middle": middle, "lower": lower})


def atr(
    high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14
) -> pd.Series:
    """Average True Range."""
    if not (len(high) == len(low) == len(close)):
        raise ValueError("high, low, close must have equal length")
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).rename("hl"),
            (high - prev_close).abs().rename("hc"),
            (low - prev_close).abs().rename("lc"),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Convenience: attach a standard set of indicators to an OHLCV frame.

    Expects columns: open, high, low, close, volume.
    Returns a copy with indicator columns appended.
    """
    out = df.copy()
    out["sma_20"] = sma(out["close"], 20)
    out["ema_12"] = ema(out["close"], 12)
    out["rsi_14"] = rsi(out["close"], 14)
    bb = bollinger_bands(out["close"], 20, 2.0)
    out["bb_upper"] = bb["upper"]
    out["bb_middle"] = bb["middle"]
    out["bb_lower"] = bb["lower"]
    macd_df = macd(out["close"])
    out["macd"] = macd_df["macd"]
    out["macd_signal"] = macd_df["signal"]
    out["macd_hist"] = macd_df["histogram"]
    out["atr_14"] = atr(out["high"], out["low"], out["close"], 14)
    return out
