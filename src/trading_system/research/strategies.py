"""Strategy abstraction + baseline research strategies (Day 7).

A Strategy receives the RAW OHLCV DataFrame (timestamp-indexed) and returns a
deterministic target position per bar: +1 (LONG), -1 (SHORT), 0 (FLAT).

Each strategy computes its OWN causal indicators from the raw data so it is fully
self-contained and provider-independent. The backtester executes the signal on the
NEXT bar's open (never the signal bar's own close) — see backtester docs.

Every indicator below is causal: a value at T uses only data available at/before T
(rolling/shift over [0..T], never [T+1..]).

These strategies are RESEARCH BASELINES, not trading recommendations.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Signal:
    """Deterministic per-bar target. value in {+1, -1, 0}."""

    LONG: int = 1
    SHORT: int = -1
    FLAT: int = 0


@dataclass
class StrategyMeta:
    name: str
    description: str


class Strategy(ABC):
    """Provider-independent strategy interface.

    ``generate`` must return a pandas Series indexed identically to ``df`` with
    values in {-1, 0, +1}. Implementations must be deterministic and causal.
    """

    meta: StrategyMeta = StrategyMeta("base", "abstract")

    @abstractmethod
    def generate(self, df: pd.DataFrame) -> pd.Series:
        raise NotImplementedError

    @property
    def params(self) -> dict:
        return {}


class EMATrendStrategy(Strategy):
    """Fast EMA vs slow EMA cross (causal: EWM over [0..T]).

    LONG when fast EMA > slow EMA; SHORT when fast < slow (if ``allow_short``),
    else FLAT when fast < slow.
    """

    meta = StrategyMeta(
        "ema", "EMA trend cross: LONG fast>slow; SHORT/FLAT otherwise (configurable)."
    )

    def __init__(self, fast: int = 12, slow: int = 26, allow_short: bool = False) -> None:
        self.fast = fast
        self.slow = slow
        self.allow_short = allow_short

    @property
    def params(self) -> dict:
        return {"fast": self.fast, "slow": self.slow, "allow_short": self.allow_short}

    def generate(self, df: pd.DataFrame) -> pd.Series:
        fe = df["close"].ewm(span=self.fast, adjust=False, min_periods=self.fast).mean()
        se = df["close"].ewm(span=self.slow, adjust=False, min_periods=self.slow).mean()
        target = pd.Series(0, index=df.index, dtype=int)
        long_mask = fe > se
        short_mask = fe < se
        target[long_mask] = Signal.LONG
        if self.allow_short:
            target[short_mask] = Signal.SHORT
        return target


class MomentumStrategy(Strategy):
    """Momentum threshold (causal: close[T]/close[T-window]-1).

    LONG above entry threshold; FLAT below exit threshold; neutral otherwise.
    """

    meta = StrategyMeta("momentum", "Momentum threshold: LONG > entry_thr, FLAT < exit_thr.")

    def __init__(self, window: int = 10, entry_thr: float = 0.02, exit_thr: float = -0.02) -> None:
        self.window = window
        self.entry_thr = entry_thr
        self.exit_thr = exit_thr

    @property
    def params(self) -> dict:
        return {"window": self.window, "entry_thr": self.entry_thr, "exit_thr": self.exit_thr}

    def generate(self, df: pd.DataFrame) -> pd.Series:
        mom = df["close"] / df["close"].shift(self.window) - 1.0
        target = pd.Series(0, index=df.index, dtype=int)
        target[mom > self.entry_thr] = Signal.LONG
        target[mom < self.exit_thr] = Signal.FLAT
        return target


class BreakoutStrategy(Strategy):
    """N-bar high/low breakout (causal: rolling max over [T-n .. T-1]).

    LONG when close[T] > highest high of the prior N bars. The current bar's high is
    excluded (shift(1)) so the breakout cannot trigger on its own spike.
    """

    meta = StrategyMeta("breakout", "N-bar breakout: LONG on new N-bar high.")

    def __init__(self, lookback: int = 20) -> None:
        self.lookback = lookback

    @property
    def params(self) -> dict:
        return {"lookback": self.lookback}

    def generate(self, df: pd.DataFrame) -> pd.Series:
        n = self.lookback
        prior_high = df["high"].shift(1).rolling(window=n, min_periods=n).max()
        target = pd.Series(0, index=df.index, dtype=int)
        target[df["close"] > prior_high] = Signal.LONG
        return target


def list_strategies() -> dict[str, type["Strategy"]]:
    return {"ema": EMATrendStrategy, "momentum": MomentumStrategy, "breakout": BreakoutStrategy}


def get_strategy(name: str, **params) -> "Strategy":
    registry = list_strategies()
    if name not in registry:
        raise KeyError(f"Unknown strategy {name!r}; available: {sorted(registry)}")
    return registry[name](**params)
