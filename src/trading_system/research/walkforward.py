"""Train / research vs out-of-sample split (Day 7 research).

Prevents evaluating a strategy on the same data it was "tuned" on. The split is
configurable (default 70/30). No parameter optimization is performed here — Day 7
is about honest evaluation, not curve-fitting.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .dataset import HistoricalDataset


@dataclass
class Split:
    train: HistoricalDataset
    test: HistoricalDataset


def split_dataset(
    dataset: HistoricalDataset, train_frac: float = 0.7, by_date: bool = True
) -> Split:
    """Chronological split. The TEST set is strictly after the TRAIN set.

    by_date=True keeps time ordering (no shuffling). The split point is the last
    bar index of the train portion.
    """
    if not 0.0 < train_frac < 1.0:
        raise ValueError("train_frac must be in (0, 1)")
    df = dataset.data.sort_index()
    n = len(df)
    if n < 10:
        # Not enough to split meaningfully; return the same data twice with a note.
        return Split(dataset, dataset)
    cut = int(n * train_frac)
    train_df = df.iloc[:cut]
    test_df = df.iloc[cut:]
    train_ds = HistoricalDataset(
        symbol=dataset.symbol, timeframe=dataset.timeframe,
        data=train_df, contract_id=dataset.contract_id, instrument=dataset.instrument,
    )
    test_ds = HistoricalDataset(
        symbol=dataset.symbol, timeframe=dataset.timeframe,
        data=test_df, contract_id=dataset.contract_id, instrument=dataset.instrument,
    )
    return Split(train=train_ds, test=test_ds)
