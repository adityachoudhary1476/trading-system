"""Provider-independent historical-data interface (Day 7 research).

Wraps whatever source produces normalized OHLCV. The backtester depends ONLY on this
interface, never on FYERS or MarketStore directly.

A dataset is fully described by:
  * the OHLCV DataFrame (timestamp-indexed)
  * the instrument / contract identity
  * the data quality (missing/duplicate bars, gaps) so the engine can refuse to
    silently run on insufficient data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from ..india.instruments import Instrument


@dataclass
class DataQuality:
    """Auditable summary of a dataset's fitness for research."""

    rows: int = 0
    date_start: Optional[pd.Timestamp] = None
    date_end: Optional[pd.Timestamp] = None
    missing_bars: int = 0
    duplicate_bars: int = 0
    gaps: int = 0
    contract_id: str = ""

    @property
    def usable(self) -> bool:
        return self.rows >= 30 and self.duplicate_bars == 0 or self.rows >= 30


@dataclass
class HistoricalDataset:
    symbol: str
    timeframe: str
    data: pd.DataFrame
    contract_id: str = ""
    instrument: Optional[Instrument] = None
    quality: DataQuality = field(default_factory=DataQuality)

    def __post_init__(self) -> None:
        if self.quality.rows == 0 and self.data is not None:
            self._compute_quality()

    def _compute_quality(self) -> None:
        df = self.data
        q = DataQuality(
            rows=int(len(df)),
            contract_id=self.contract_id,
        )
        if len(df):
            q.date_start = df.index.min()
            q.date_end = df.index.max()
            # Duplicates: identical timestamps.
            q.duplicate_bars = int(df.index.to_series().duplicated().sum())
            # Missing bars: cannot know the "true" calendar without a reference; we
            # report calendar gaps larger than 1 expected period as gaps.
            q.gaps = self._count_gaps()
        self.quality = q

    def _count_gaps(self, max_gap=None) -> int:
        idx = self.data.index
        if len(idx) < 2:
            return 0
        deltas = idx.to_series().diff().dropna()
        if max_gap is None:
            med = deltas.median()
            max_gap = med * 3 if med else pd.Timedelta("1d")
        return int((deltas > max_gap).sum())


class HistoricalDataSource:
    """Pulls a HistoricalDataset from a store. Provider-agnostic: pass a loader."""

    def __init__(self, loader) -> None:
        # loader(symbol, timeframe) -> pd.DataFrame (timestamp-indexed OHLCV)
        self._loader = loader

    def get(self, symbol: str, timeframe: str, contract_id: str = "") -> HistoricalDataset:
        df = self._loader(symbol, timeframe)
        return HistoricalDataset(
            symbol=symbol, timeframe=timeframe, data=df, contract_id=contract_id
        )


class MarketDataRepository:
    """Concrete HistoricalDataSource wired to MarketStore (read-only)."""

    def __init__(self, store) -> None:
        self._store = store

    def load(self, symbol: str, timeframe: str) -> pd.DataFrame:
        return self._store.load(symbol, timeframe)
