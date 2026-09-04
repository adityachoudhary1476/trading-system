"""Foundational quantitative calculations.

Deterministic and pandas/numpy based. Day 1 scope only: returns, volatility,
volume statistics, drawdown, and a simple (risk-free optional) Sharpe ratio.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_PERIODS = {
    "1m": 525_600,
    "5m": 105_120,
    "15m": 35_040,
    "30m": 17_520,
    "1h": 8_760,
    "4h": 2_190,
    "1d": 365,
    "1w": 52,
    "1M": 12,
}


def pct_change(series: pd.Series) -> pd.Series:
    return series.pct_change()


def simple_returns(close: pd.Series) -> pd.Series:
    """Period-over-period simple returns."""
    return close.pct_change()


def log_returns(close: pd.Series) -> pd.Series:
    """Continuously compounded returns."""
    return np.log(close / close.shift(1))


def cumulative_returns(simple_rets: pd.Series) -> pd.Series:
    """Cumulative growth of 1 unit invested at the first valid return."""
    return (1.0 + simple_rets).cumprod()


def rolling_volatility(returns: pd.Series, window: int = 20) -> pd.Series:
    """Rolling standard deviation of returns (per-period)."""
    if window <= 0:
        raise ValueError("window must be positive")
    return returns.rolling(window=window, min_periods=window).std(ddof=0)


def annualized_volatility(returns: pd.Series, timeframe: str = "1d") -> float:
    """Annualized volatility from per-period returns."""
    periods = TRADING_PERIODS.get(timeframe, 365)
    return float(returns.std(ddof=0) * np.sqrt(periods))


def drawdown(close: pd.Series) -> pd.Series:
    """Drawdown series (<= 0) from running peak."""
    running_max = close.cummax()
    result = close / running_max - 1.0
    result[running_max == 0] = 0.0
    return result


def volume_stats(volume: pd.Series, window: int = 20) -> pd.DataFrame:
    """Rolling volume mean and z-score of current volume."""
    mean = volume.rolling(window=window, min_periods=1).mean()
    std = volume.rolling(window=window, min_periods=1).std(ddof=0)
    zscore = (volume - mean) / std.replace(0.0, np.nan)
    return pd.DataFrame({"volume": volume, "volume_ma": mean, "volume_z": zscore})


def sharpe_ratio(
    returns: pd.Series, risk_free_per_period: float = 0.0, periods_per_year: int = 365
) -> float:
    """Annualized Sharpe ratio given per-period returns."""
    excess = returns - risk_free_per_period
    std = excess.std(ddof=0)
    if std == 0 or np.isnan(std) or np.isinf(std):
        return 0.0
    return float(excess.mean() * periods_per_year / std)
