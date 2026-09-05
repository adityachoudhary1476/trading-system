"""V5 data-layer tests: local import, validation, provenance, dataset types.

No network, no external providers. Uses temp CSV/JSON files written inline.
"""
from __future__ import annotations

import json
import pathlib
import sys

import numpy as np
import pandas as pd
import pytest

_TESTS = pathlib.Path(__file__).resolve().parent
for _p in (_TESTS, _TESTS.parent / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from trading_system.research.historical_data import (  # noqa: E402
    DatasetType, HistoricalProvenance, LocalFileAdapter, NormalizedDataset,
    ValidationReport, validate_ohlcv,
)


def _good_df(n=100, start=100.0, drift=0.2, vol=0.8, freq="1D", seed=5):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq=freq, tz="UTC")
    c = start + np.cumsum(rng.normal(drift, vol, n))
    return pd.DataFrame({
        "open": c, "high": c + 0.5, "low": c - 0.5, "close": c,
        "volume": rng.integers(1_000_000, 5_000_000, n).astype(float),
    }, index=idx)


class TestValidation:

    def test_valid_data_passes(self):
        assert validate_ohlcv(_good_df()).valid

    def test_duplicate_timestamps_flagged(self):
        df = pd.concat([_good_df(), _good_df().iloc[[5]]])
        rep = validate_ohlcv(df)
        assert not rep.valid
        assert rep.duplicates == 1

    def test_impossible_ohlc_flagged(self):
        df = _good_df()
        df.loc[df.index[10], "high"] = df.loc[df.index[10], "low"] - 10
        rep = validate_ohlcv(df)
        assert not rep.valid and rep.invalid_ohlc == 1

    def test_negative_volume_flagged(self):
        df = _good_df()
        df.loc[df.index[3], "volume"] = -5
        rep = validate_ohlcv(df)
        assert not rep.valid and rep.negative_volume == 1

    def test_zero_price_flagged(self):
        df = _good_df()
        df.loc[df.index[4], "close"] = 0.0
        rep = validate_ohlcv(df)
        assert not rep.valid and rep.invalid_prices >= 1

    def test_missing_columns_reported(self):
        rep = validate_ohlcv(_good_df().drop(columns=["volume"]))
        assert "volume" in " ".join(rep.issues)

    def test_gaps_flagged_with_freq(self):
        df = _good_df()
        extra = df.iloc[-10:].copy()
        extra.index = extra.index + pd.Timedelta(days=400)
        rep = validate_ohlcv(pd.concat([df, extra]),
                             expected_freq=pd.Timedelta(days=1))
        assert rep.gaps >= 1

class TestLocalFileAdapter:

    def _csv_path(self, tmp_path):
        df = _good_df()
        out = df.reset_index()
        out.columns = ["datetime", "open", "high", "low", "close", "volume"]
        p = tmp_path / "ohlcv.csv"
        out.to_csv(p, index=False)
        return str(p)

    def test_csv_import_timezone_aware(self, tmp_path):
        ds = LocalFileAdapter().load_csv(self._csv_path(tmp_path),
                                         "NSE:TEST-EQ", "1d")
        assert isinstance(ds, NormalizedDataset)
        frame = ds.frames["NSE:TEST-EQ"]["1d"]
        assert frame.index.tz is not None and len(frame) == 100

    def test_naive_timestamp_assumed_utc(self, tmp_path):
        df = _good_df().tz_localize(None)
        out = df.reset_index()
        out.columns = ["datetime", "open", "high", "low", "close", "volume"]
        p = tmp_path / "naive.csv"
        out.to_csv(p, index=False)
        ds = LocalFileAdapter(tz="UTC").load_csv(str(p), "NSE:T-EQ", "1d")
        assert ds.frames["NSE:T-EQ"]["1d"].index.tz is not None

    def test_json_import(self, tmp_path):
        df = _good_df()
        records = [{"timestamp": str(ts), "open": o, "high": h, "low": l,
                    "close": c, "volume": v}
                   for ts, (o, h, l, c, v) in zip(
                       df.index, df[["open", "high", "low", "close",
                                     "volume"]].values)]
        p = tmp_path / "data.json"
        p.write_text(json.dumps({"data": records}))
        ds = LocalFileAdapter().load_json(str(p), "NSE:J-EQ", "1d")
        assert len(ds.frames["NSE:J-EQ"]["1d"]) == 100

    def test_column_alias_mapping(self, tmp_path):
        df = _good_df()
        out = df.reset_index()
        out.columns = ["date", "o", "h", "l", "c", "vol"]
        p = tmp_path / "alias.csv"
        out.to_csv(p, index=False)
        frame = LocalFileAdapter().load_csv(str(p), "NSE:A-EQ", "1d") \
            .frames["NSE:A-EQ"]["1d"]
        assert {"open", "high", "low", "close", "volume"}.issubset(frame.columns)

    def test_missing_datetime_column_raises(self, tmp_path):
        p = tmp_path / "bad.csv"
        pd.DataFrame({"a": [1], "b": [2]}).to_csv(p, index=False)
        with pytest.raises(ValueError):
            LocalFileAdapter().load_csv(str(p), "NSE:B-EQ", "1d")


class TestProvenance:

    def test_dataset_type_states(self):
        assert DatasetType.REAL_HISTORICAL.value == "REAL_HISTORICAL"
        assert DatasetType.SYNTHETIC_TEST.value == "SYNTHETIC_TEST"
        assert DatasetType.UNKNOWN.value == "UNKNOWN"

    def test_unknown_provenance_not_real(self):
        prov = HistoricalProvenance()
        assert prov.known_real() is False

    def test_real_requires_source_and_id(self):
        prov = HistoricalProvenance(dataset_id="d1", source="NSE",
                                    dataset_type=DatasetType.REAL_HISTORICAL)
        assert prov.known_real() is True

    def test_sha256_deterministic(self):
        a = HistoricalProvenance(dataset_id="d1", source="s")
        b = HistoricalProvenance(dataset_id="d1", source="s")
        c = HistoricalProvenance(dataset_id="d2", source="s")
        assert a.sha256() == b.sha256()
        assert a.sha256() != c.sha256()
