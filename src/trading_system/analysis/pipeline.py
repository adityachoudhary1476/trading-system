"""Optional analysis pipeline: load stored data and compute analytics.

Day 1 only wires the foundational quant + indicator functions onto stored
data so the pipeline is exercised end-to-end. No AI, no signals yet.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..config import settings
from ..storage.database import MarketStore
from .quant import (
    simple_returns,
    annualized_volatility,
    drawdown,
    volume_stats,
)
from ..indicators import add_all_indicators


@dataclass
class AnalysisResult:
    symbol: str
    timeframe: str
    rows: int = 0
    annualized_vol: float = 0.0
    max_drawdown: float = 0.0
    last_close: float = 0.0
    enhanced: pd.DataFrame = field(default_factory=pd.DataFrame)


def analyze(
    symbol: str, timeframe: str, store: MarketStore | None = None
) -> AnalysisResult:
    store = store or MarketStore(settings.storage.db_url)
    df = store.load(symbol, timeframe)
    if df.empty:
        return AnalysisResult(symbol=symbol, timeframe=timeframe)

    df = add_all_indicators(df)
    rets = simple_returns(df["close"])
    dd = drawdown(df["close"])
    res = AnalysisResult(
        symbol=symbol,
        timeframe=timeframe,
        rows=len(df),
        annualized_vol=annualized_volatility(rets, timeframe),
        max_drawdown=float(dd.min()),
        last_close=float(df["close"].iloc[-1]),
        enhanced=df,
    )
    return res
