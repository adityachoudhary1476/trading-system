"""Factor analysis (Day 10) — IC/IR, grouped/decile backtest, breakeven.

All methods are deterministic, cross-sectional or time-series as appropriate, and
FREE of look-ahead. The Information Coefficient uses factor at T vs FORWARD return at
T+lag (NEVER factor at T vs return at T). Returns are forward-looking by construction.

No broker logic, no LLM logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd


MIN_CROSS_SECTION = 5  # minimum instruments for a meaningful cross-sectional IC


# --------------------------------------------------------------------------- #
# Information Coefficient (factor_T -> forward return_T+lag)
# --------------------------------------------------------------------------- #
def forward_return(prices: pd.DataFrame, lag: int = 1) -> pd.DataFrame:
    """Cross-sectional forward returns. Columns = instruments, index = time.

    return at T (realized at T+lag) = prices[T+lag]/prices[T] - 1.
    The last `lag` rows have NaN forward returns (no future data) — never used.
    """
    return prices.shift(-lag) / prices - 1.0


def compute_ic_series(factor: pd.DataFrame, fwd_ret: pd.DataFrame, lag: int = 1) -> pd.Series:
    """Cross-sectional Spearman IC per date: factor_T vs forward_return_T+lag.

    factor and fwd_ret are aligned DataFrames (instruments as columns, dates as index).
    Returns a Series indexed by date. Dates with < MIN_CROSS_SECTION valid pairs -> NaN.
    """
    if factor.shape != fwd_ret.shape:
        # Align on common index/columns.
        cols = factor.columns.intersection(fwd_ret.columns)
        idx = factor.index.intersection(fwd_ret.index)
        factor = factor.loc[idx, cols]
        fwd_ret = fwd_ret.loc[idx, cols]
    ics = {}
    for date, row in factor.iterrows():
        f = row
        r = fwd_ret.loc[date]
        mask = f.notna() & r.notna()
        if mask.sum() < MIN_CROSS_SECTION:
            ics[date] = float("nan")
            continue
        ic, _ = _spearman_valid(f[mask].to_numpy(), r[mask].to_numpy())
        ics[date] = ic
    return pd.Series(ics, name=f"ic_lag{lag}")


def _spearman_valid(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if len(a) < 2 or np.allclose(a, a[0]) or np.allclose(b, b[0]):
        return float("nan"), len(a)
    ra = pd.Series(a).rank().to_numpy()
    rb = pd.Series(b).rank().to_numpy()
    d = ra - rb
    n = len(a)
    rho = 1.0 - 6.0 * float((d ** 2).sum()) / (n * (n * n - 1))
    return rho, n


def ic_statistics(ic_series: pd.Series) -> dict:
    """Mean/median/std IC, ICIR, positive-IC fraction, n obs. Robust to NaNs."""
    v = ic_series.dropna()
    n = int(v.shape[0])
    if n == 0:
        return {
            "mean_ic": float("nan"), "median_ic": float("nan"), "std_ic": float("nan"),
            "icir": float("nan"), "positive_ic_fraction": float("nan"), "n_obs": 0,
        }
    mean = float(v.mean())
    std = float(v.std(ddof=0)) if n > 1 else 0.0
    icir = (mean / std) if std > 0 else float("nan")  # zero-variance -> NaN, not inf
    pos = float((v > 0).mean())
    return {
        "mean_ic": mean,
        "median_ic": float(v.median()),
        "std_ic": std,
        "icir": icir,
        "positive_ic_fraction": pos,
        "n_obs": n,
    }


# --------------------------------------------------------------------------- #
# Grouped / decile backtest
# --------------------------------------------------------------------------- #
@dataclass
class GroupBacktestResult:
    n_groups: int
    group_returns: pd.DataFrame          # index=date, columns=Q1..Qn (mean fwd ret of group)
    group_cum: pd.DataFrame              # cumulative product equity per group
    long_short: pd.Series                # Qn - Q1 (or top-bottom) per date
    monotonic: bool                      # crude monotonicity flag (top >= bottom)
    n_dates: int
    n_instruments_per_date: int


def grouped_backtest(
    factor: pd.DataFrame,
    fwd_ret: pd.DataFrame,
    n_groups: int = 10,
    top: int = 1,
    bottom: int = 1,
) -> GroupBacktestResult:
    """Each date: rank instruments by factor, split into n_groups, equal-weight fwd ret.

    Handles duplicate factor values (rank method='first' after ties handled by
    pd.qcut with duplicates_ok). Handles insufficient instruments (fewer groups).
    No look-ahead: factor at T predicts fwd_ret at T (already forward-shifted upstream).
    """
    if factor.shape != fwd_ret.shape:
        cols = factor.columns.intersection(fwd_ret.columns)
        idx = factor.index.intersection(fwd_ret.index)
        factor = factor.loc[idx, cols]
        fwd_ret = fwd_ret.loc[idx, cols]

    group_ret: dict[pd.Timestamp, dict[int, float]] = {}
    ls: dict[pd.Timestamp, float] = {}
    n_per_date = 0
    for date, row in factor.iterrows():
        f = row
        r = fwd_ret.loc[date]
        mask = f.notna() & r.notna()
        sub_f = f[mask]
        sub_r = r[mask]
        m = len(sub_f)
        if m < MIN_CROSS_SECTION:
            continue
        n_per_date = max(n_per_date, m)
        # Group assignment via qcut; if too few unique values, fall back to rank buckets.
        try:
            grp = pd.qcut(sub_f.rank(method="first"), n_groups, labels=False, duplicates="drop")
        except ValueError:
            grp = pd.cut(sub_f.rank(method="first"), n_groups, labels=False)
        gret: dict[int, list[float]] = {g: [] for g in range(n_groups)}
        for g, ret in zip(grp.to_numpy(), sub_r.to_numpy()):
            gret[int(g)].append(float(ret))
        row_out: dict[int, float] = {}
        for g in range(n_groups):
            vals = gret[g]
            row_out[g] = float(np.mean(vals)) if vals else float("nan")
        group_ret[date] = row_out
        # Long-short: average of top groups minus average of bottom groups.
        top_vals = [row_out[g] for g in range(n_groups - top, n_groups) if not np.isnan(row_out[g])]
        bot_vals = [row_out[g] for g in range(bottom) if not np.isnan(row_out[g])]
        if top_vals and bot_vals:
            ls[date] = float(np.mean(top_vals) - np.mean(bot_vals))

    if not group_ret:
        return GroupBacktestResult(n_groups, pd.DataFrame(), pd.DataFrame(), pd.Series(dtype=float),
                                    False, 0, 0)

    gdf = pd.DataFrame.from_dict(group_ret, orient="index").sort_index()
    gdf.columns = [f"Q{g+1}" for g in gdf.columns]
    cum = (1.0 + gdf).cumprod()
    ls_series = pd.Series(ls).sort_index()
    monotonic = bool(not gdf.empty and gdf.iloc[:, -1].mean() >= gdf.iloc[:, 0].mean())
    return GroupBacktestResult(
        n_groups=n_groups, group_returns=gdf, group_cum=cum,
        long_short=ls_series, monotonic=monotonic,
        n_dates=int(len(gdf)), n_instruments_per_date=n_per_date,
    )


# --------------------------------------------------------------------------- #
# Transaction-cost breakeven (generic, unit-explicit)
# --------------------------------------------------------------------------- #
def breakeven_fee_bps(alpha_daily: float, n_trades: int, position_size: float = 1.0) -> float:
    """Per-unit breakeven fee (BPS of notional) a strategy can absorb.

    Units (explicit):
      * alpha_daily  — FIXED portfolio gross alpha per period, in NOTIONAL FRACTION
                       of capital (e.g. 0.001 = 10 bps on the whole book).
      * n_trades      — number of round-trip trade legs per period.
      * position_size — notional deployed per trade, in FRACTION OF CAPITAL
                       (e.g. 1.0 = full capital per trade).

    Round-trip fee paid per period = (fee_bps/1e4) * 2 * n_trades * position_size.
    Setting that equal to alpha_daily and solving:
        fee_bps = alpha_daily * 1e4 / (2 * n_trades * position_size)

    position_size does NOT cancel: HALVING exposure doubles the per-unit fee needed
    to consume the same fixed portfolio alpha (the required invariant). If
    position_size is 0 or n_trades <= 0, returns NaN (undefined).
    """
    if n_trades <= 0 or position_size <= 0:
        return float("nan")
    return alpha_daily * 1e4 / (2.0 * n_trades * position_size)
