"""India factor engine (Day 10) — deterministic, causal, provider-independent.

Factors are derived ONLY from information available at/before bar T (no look-ahead).
Each factor documents its name, definition, required data, lookback, output, causal
behavior, and known limitations. We keep a SMALL, economically interpretable set rather
than a sprawling library — quality over quantity.

Reuses ``indicators`` (pure primitives) and ``analysis.quant``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd

from ..indicators import sma, ema, rsi, atr, rolling_std
from ..analysis.quant import annualized_volatility, TRADING_PERIODS


@dataclass
class FactorMeta:
    name: str
    category: str
    definition: str
    required_data: str
    lookback: int
    output: str
    causal: str
    limitations: str


class Factor:
    """A named, documented, causal factor.

    ``compute(df)`` returns a pandas Series aligned to ``df.index``. Values that cannot
    be computed (insufficient warmup) are NaN — never fabricated.
    """

    def __init__(self, meta: FactorMeta, fn: Callable[[pd.DataFrame], pd.Series]) -> None:
        self.meta = meta
        self._fn = fn

    @property
    def name(self) -> str:
        return self.meta.name

    def compute(self, df: pd.DataFrame) -> pd.Series:
        if df is None or len(df) == 0:
            raise ValueError("cannot compute factor on empty frame")
        out = self._fn(df.sort_index())
        out.name = self.meta.name
        return out


def _s(name, category, definition, req, lookback, output, causal, limitations) -> FactorMeta:
    return FactorMeta(name, category, definition, req, lookback, output, causal, limitations)


# --------------------------------------------------------------------------- #
# Factor library (small, interpretable)
# --------------------------------------------------------------------------- #
def _build_factors() -> dict[str, Factor]:
    P = []

    # --- Trend ---
    P.append(Factor(_s(
        "sma_distance_20", "trend",
        "close / SMA20 - 1 (normalized distance from 20-bar mean)",
        "close", 20, "ratio", "uses only closes <= T",
        "needs >=20 bars; mean-reversion vs trend depending on use"),
        lambda df: df["close"] / sma(df["close"], 20) - 1.0))

    P.append(Factor(_s(
        "ema_distance_20", "trend",
        "close / EMA20 - 1",
        "close", 20, "ratio", "uses only closes <= T",
        "EMA is backward-weighted; still a lagging measure"),
        lambda df: df["close"] / ema(df["close"], 20) - 1.0))

    P.append(Factor(_s(
        "ma_spread", "trend",
        "(EMA20 - EMA50) / close — short/long MA spread",
        "close", 50, "ratio", "uses only closes <= T",
        "needs >=50 bars; sign flip lags turning points"),
        lambda df: (ema(df["close"], 20) - ema(df["close"], 50)) / df["close"]))

    P.append(Factor(_s(
        "trend_strength", "trend",
        "Spearman rank corr of close vs bar index over 60 bars (scaled to [-1,1])",
        "close", 60, "ratio in [-1,1]", "rank corr within [T-59, T]",
        "needs >=60 bars; measures persistence not magnitude"),
        lambda df: _trend_strength(df["close"], 60)))

    # --- Momentum ---
    P.append(Factor(_s(
        "rsi_14", "momentum",
        "Wilder RSI(14)",
        "close", 14, "0..100", "uses only closes <= T",
        "bounded; overbought/oversold is heuristic not predictive"),
        lambda df: rsi(df["close"], 14)))

    P.append(Factor(_s(
        "roc_20", "momentum",
        "20-bar rate of change: close/close[-20] - 1",
        "close", 20, "ratio", "uses only closes <= T",
        "single-window; noisy"),
        lambda df: df["close"] / df["close"].shift(20) - 1.0))

    P.append(Factor(_s(
        "momentum_60_20", "momentum",
        "roc(60) - roc(20): intermediate vs short momentum",
        "close", 60, "ratio", "uses only closes <= T",
        "needs >=60 bars"),
        lambda df: (df["close"] / df["close"].shift(60) - 1.0)
        - (df["close"] / df["close"].shift(20) - 1.0)))

    P.append(Factor(_s(
        "multi_mom", "momentum",
        "mean of roc over [20,60,120] windows",
        "close", 120, "ratio", "uses only closes <= T",
        "needs >=120 bars; smooths single-window noise"),
        lambda df: (df["close"] / df["close"].shift(20) - 1.0
                   + (df["close"] / df["close"].shift(60) - 1.0)
                   + (df["close"] / df["close"].shift(120) - 1.0)) / 3.0))

    # --- Volatility ---
    P.append(Factor(_s(
        "atr_14", "volatility",
        "Average True Range(14) / close (normalized)",
        "high,low,close", 14, "ratio", "uses only bars <= T",
        "normalized by close; absolute level loses comparability across names"),
        lambda df: atr(df["high"], df["low"], df["close"], 14) / df["close"]))

    P.append(Factor(_s(
        "realized_vol_20", "volatility",
        "annualized stdev of daily returns over 20 bars",
        "close", 20, "ratio (annualized)", "uses only returns <= T",
        "annualization factor is timeframe-agnostic; for non-daily interpret as relative"),
        lambda df: _ann_vol(df["close"].pct_change(), 20)))

    P.append(Factor(_s(
        "vol_pct_rank", "volatility",
        "rolling percentile rank of realized_vol_20 within 252-bar window",
        "close", 252, "0..100", "uses only vol history <= T",
        "needs >=252 bars for a stable rank; early values are partial"),
        lambda df: _vol_pct_rank(df["close"].pct_change(), 20, 252)))

    P.append(Factor(_s(
        "vol_expansion", "volatility",
        "realized_vol_20 / realized_vol_60 - 1 (vol regime change)",
        "close", 60, "ratio", "uses only returns <= T",
        "needs >=60 bars; sign indicates expansion/contraction"),
        lambda df: _vol_expansion(df["close"].pct_change(), 20, 60)))

    # --- Volume ---
    P.append(Factor(_s(
        "relative_volume_20", "volume",
        "volume / SMA20(volume)",
        "volume", 20, "ratio", "uses only volume <= T",
        "needs >=20 bars; compares to own history"),
        lambda df: df["volume"] / sma(df["volume"], 20)))

    P.append(Factor(_s(
        "volume_momentum", "volume",
        "SMA5(volume)/SMA20(volume) - 1",
        "volume", 20, "ratio", "uses only volume <= T",
        "needs >=20 bars; signals participation change"),
        lambda df: sma(df["volume"], 5) / sma(df["volume"], 20) - 1.0))

    # --- Price structure ---
    P.append(Factor(_s(
        "dist_from_high_20", "price_structure",
        "close / 20-bar high - 1 (negative = below recent high)",
        "high,close", 20, "ratio", "uses only highs <= T",
        "recent window only"),
        lambda df: df["close"] / df["high"].rolling(20).max() - 1.0))

    P.append(Factor(_s(
        "dist_from_low_20", "price_structure",
        "close / 20-bar low - 1 (positive = above recent low)",
        "low,close", 20, "ratio", "uses only lows <= T",
        "recent window only"),
        lambda df: df["close"] / df["low"].rolling(20).min() - 1.0))

    P.append(Factor(_s(
        "range_position", "price_structure",
        "(close - 20-bar low) / (20-bar high - 20-bar low); 0=low, 1=high",
        "high,low,close", 20, "0..1", "uses only bars <= T",
        "degenerate when high==low"),
        lambda df: _range_position(df)))

    return {f.name: f for f in P}


def _trend_strength(close: pd.Series, w: int) -> pd.Series:
    idx = pd.Series(np.arange(len(close)), index=close.index)
    return close.rolling(w).apply(
        lambda x: _spearman(x, idx.iloc[len(idx) - len(x):].values), raw=False
    ).astype(float)


def _spearman(a, b) -> float:
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if len(a) < 3 or np.allclose(a, a[0]) or np.allclose(b, b[0]):
        return float("nan")
    ra = pd.Series(a).rank().to_numpy()
    rb = pd.Series(b).rank().to_numpy()
    d = ra - rb
    n = len(a)
    return 1.0 - 6.0 * float((d ** 2).sum()) / (n * (n * n - 1))


def _ann_vol(rets: pd.Series, w: int) -> pd.Series:
    r = rets.dropna()
    sd = rolling_std(r, w)
    # annualize using the project's per-timeframe factor (generic, not India-specific)
    return sd * np.sqrt(TRADING_PERIODS.get("1d", 252))


def _vol_pct_rank(rets: pd.Series, w: int, win: int) -> pd.Series:
    rv = _ann_vol(rets, w)
    return rv.rolling(win).apply(lambda x: (x[-1] <= x).mean() * 100.0, raw=True)


def _vol_expansion(rets: pd.Series, w1: int, w2: int) -> pd.Series:
    v1 = _ann_vol(rets, w1)
    v2 = _ann_vol(rets, w2)
    return v1 / v2 - 1.0


def _range_position(df: pd.DataFrame) -> pd.Series:
    hi = df["high"].rolling(20).max()
    lo = df["low"].rolling(20).min()
    return (df["close"] - lo) / (hi - lo)


# --------------------------------------------------------------------------- #
# Factor engine
# --------------------------------------------------------------------------- #
class FactorEngine:
    """Compute one or many factors on a DataFrame. Deterministic and causal."""

    def __init__(self, names: Optional[list[str]] = None) -> None:
        self._all = _build_factors()
        self.names = names or list(self._all.keys())

    def available(self) -> list[str]:
        return list(self._all.keys())

    def metadata(self, name: str) -> FactorMeta:
        return self._all[name].meta

    def compute(self, name: str, df: pd.DataFrame) -> pd.Series:
        return self._all[name].compute(df)

    def compute_many(self, df: pd.DataFrame, names: Optional[list[str]] = None) -> pd.DataFrame:
        names = names or self.names
        out = pd.DataFrame(index=df.sort_index().index)
        for n in names:
            out[n] = self._all[n].compute(df)
        return out

    def metas(self, names: Optional[list[str]] = None) -> list[FactorMeta]:
        names = names or self.names
        return [self._all[n].meta for n in names]
