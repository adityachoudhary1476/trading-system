"""V5 historical data adapter: provenance + validation + provider-agnostic load.

The core intelligence engine must not care whether data came from CSV, Parquet,
JSON, a database, an exchange archive, an external provider, or a future live
adapter. This module defines:

  * ``DatasetType`` — REAL_HISTORICAL / SYNTHETIC_RESEARCH / SYNTHETIC_TEST /
    UNKNOWN. Never label unknown provenance as verified historical data.
  * ``HistoricalProvenance`` — the auditable provenance contract.
  * ``validate_ohlcv`` — refuse silently-broken data.
  * ``HistoricalDataAdapter`` — the provider-agnostic normalized contract.

V5 rule: if a dataset's provenance is missing/UNKNOWN, the V5 report must say
so — it may not be called real historical.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import pandas as pd

UTC = timezone.utc


def utc_now_str() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# Dataset type / provenance
# --------------------------------------------------------------------------- #
class DatasetType(str, Enum):
    """Explicit data-type states. Only REAL_HISTORICAL may be called real."""

    REAL_HISTORICAL = "REAL_HISTORICAL"
    SYNTHETIC_RESEARCH = "SYNTHETIC_RESEARCH"
    SYNTHETIC_TEST = "SYNTHETIC_TEST"
    UNKNOWN = "UNKNOWN"


@dataclass
class HistoricalProvenance:
    """Auditable origin metadata for an imported dataset."""

    dataset_id: str = ""
    source: str = ""                    # provider/publisher, e.g. "NSE archive"
    source_url: str = ""
    downloaded_at: Optional[str] = None  # ISO8601
    imported_at: Optional[str] = None
    date_range: str = ""                # "2020-01-01..2024-12-31"
    instruments: list[str] = field(default_factory=list)
    timeframes: list[str] = field(default_factory=list)
    timezone: str = "UTC"
    adjusted: bool = False              # RAW_PRICES vs ADJUSTED_PRICES
    license_notes: str = ""
    completeness_notes: str = ""
    dataset_type: DatasetType = DatasetType.UNKNOWN

    def sha256(self) -> str:
        """Deterministic dataset-fingerprint (provenance + type)."""
        payload = json.dumps({
            "dataset_id": self.dataset_id, "source": self.source,
            "source_url": self.source_url, "downloaded_at": self.downloaded_at,
            "imported_at": self.imported_at, "date_range": self.date_range,
            "instruments": self.instruments, "timeframes": self.timeframes,
            "timezone": self.timezone, "adjusted": self.adjusted,
            "dataset_type": self.dataset_type.value,
        }, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def known_real(self) -> bool:
        return (self.dataset_type == DatasetType.REAL_HISTORICAL
                and bool(self.source) and bool(self.dataset_id))

# --------------------------------------------------------------------------- #
# OHLCV validation (never silently repair)
# --------------------------------------------------------------------------- #
@dataclass
class ValidationReport:
    rows: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    invalid_ohlc: int = 0           # high < max(o,c) or low > min(o,c) or high < low
    negative_volume: int = 0
    invalid_prices: int = 0         # NaN / <= 0 close
    gaps: int = 0
    timezone_inconsistent: int = 0
    issues: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return (self.duplicates == 0 and self.out_of_order == 0
                and self.invalid_ohlc == 0 and self.negative_volume == 0
                and self.invalid_prices == 0)


def validate_ohlcv(df: pd.DataFrame,
                   expected_freq: Optional[pd.Timedelta] = None,
                   max_gap_mult: float = 3.0) -> ValidationReport:
    """Validate OHLCV structure. Invalid observations are flagged, never
    silently repaired."""
    rep = ValidationReport()
    if df is None or len(df) == 0:
        rep.issues.append("empty data")
        return rep
    rep.rows = int(len(df))
    dup = df.index.duplicated()
    rep.duplicates = int(dup.sum())
    if rep.duplicates:
        rep.issues.append(f"{rep.duplicates} duplicate timestamps")
    srt = df.index.to_series().diff().dropna()
    rep.out_of_order = int((srt < pd.Timedelta(0)).sum())
    if rep.out_of_order:
        rep.issues.append("timestamps out of order")
    if expected_freq is not None and len(df) > 1:
        med = srt.median()
        if med > pd.Timedelta(0):
            rep.gaps = int((srt > med * max_gap_mult).sum())
    need = {"open", "high", "low", "close", "volume"}
    missing = need - set(df.columns)
    if missing:
        rep.issues.append(f"missing columns: {sorted(missing)}")
        return rep
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    v = df["volume"]
    oc = pd.concat([o, c], axis=1)
    bad_hl = (h < l) | (h < oc.max(axis=1)) | (l > oc.min(axis=1))
    rep.invalid_ohlc = int(bad_hl.sum())
    if rep.invalid_ohlc:
        rep.issues.append(f"{rep.invalid_ohlc} impossible OHLC rows")
    rep.negative_volume = int((v < 0).sum())
    if rep.negative_volume:
        rep.issues.append(f"{rep.negative_volume} negative volume rows")
    rep.invalid_prices = int(((c.isna() | (c <= 0)) | (h.isna() | (h <= 0))).sum())
    if rep.invalid_prices:
        rep.issues.append(f"{rep.invalid_prices} invalid price rows")
    if rep.gaps:
        rep.issues.append(f"{rep.gaps} calendar gaps > {max_gap_mult}x median")
    return rep

# --------------------------------------------------------------------------- #
# Provider-agnostic normalized observation
# --------------------------------------------------------------------------- #
@dataclass
class MarketObservation:
    """One normalized OHLCV observation, provider-agnostic."""

    instrument: str = ""
    timestamp: Optional[datetime] = None
    timeframe: str = "1d"
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    source: str = ""
    data_quality: str = "healthy"
    open_interest: Optional[float] = None      # when available
    adjusted: bool = False                     # ADJUSTED_PRICES flag


@dataclass
class NormalizedDataset:
    """Standardized adapter output consumed by the engine."""

    provenance: HistoricalProvenance = field(default_factory=HistoricalProvenance)
    frames: dict[str, dict[str, pd.DataFrame]] = field(default_factory=dict)
    validation: dict[str, ValidationReport] = field(default_factory=dict)

    def instruments(self) -> list[str]:
        return list(self.frames.keys())

    def all_valid(self) -> bool:
        if not self.validation:
            return True
        return all(rep.valid for rep in self.validation.values())

# --------------------------------------------------------------------------- #
# Local file adapter (CSV / JSON / Parquet)
# --------------------------------------------------------------------------- #
class LocalFileAdapter:
    """Loads local files into NormalizedDataset.

    Column mapping is configurable (``col_map``). Timestamps are normalized to
    timezone-aware; naive timestamps are assumed to be in ``tz`` (default UTC,
    documented). Invalid data is flagged, never repaired.
    """

    def __init__(self, col_map: Optional[dict[str, str]] = None,
                 tz: str = "UTC") -> None:
        self.col_map = col_map or {
            "datetime": "datetime", "timestamp": "timestamp", "time": "time",
            "date": "date", "open": "open", "o": "open", "high": "high",
            "h": "high", "low": "low", "l": "low", "close": "close",
            "c": "close", "volume": "volume", "vol": "volume",
            "oi": "open_interest", "open_interest": "open_interest",
        }
        self.tz = tz

    def load_csv(self, path: str, instrument: str, timeframe: str = "1d",
                 provenance: Optional[HistoricalProvenance] = None
                 ) -> NormalizedDataset:
        df = pd.read_csv(path)
        return self._finalize(df, instrument, timeframe, provenance)

    def load_json(self, path: str, instrument: str, timeframe: str = "1d",
                  provenance: Optional[HistoricalProvenance] = None,
                  records: bool = False) -> NormalizedDataset:
        with open(path, "r", encoding="utf-8-sig") as fh:
            if records:
                df = pd.DataFrame(json.load(fh))
            else:
                raw = json.load(fh)
                payload = raw.get("data", raw)
                if isinstance(payload, dict) and "OHLCV" in payload:
                    payload = payload["OHLCV"]
                df = pd.DataFrame(payload)
        return self._finalize(df, instrument, timeframe, provenance)

    def load_parquet(self, path: str, instrument: str, timeframe: str = "1d",
                     provenance: Optional[HistoricalProvenance] = None
                     ) -> NormalizedDataset:
        try:
            import pyarrow  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise ValueError(
                "pyarrow not installed; parquet support requires pyarrow. "
                "Use CSV/JSON instead.") from exc
        df = pd.read_parquet(path)
        return self._finalize(df, instrument, timeframe, provenance)

    def _finalize(self, df: pd.DataFrame, instrument: str, timeframe: str,
                  provenance: Optional[HistoricalProvenance]) -> NormalizedDataset:
        time_col = None
        for candidate in ("datetime", "timestamp", "time", "date"):
            if candidate in df.columns:
                time_col = candidate
                break
        if time_col is None:
            if isinstance(df.index, pd.DatetimeIndex):
                idx = df.index
            else:
                raise ValueError(f"no datetime column in {instrument} data; "
                                 "provide datetime/timestamp/date")
        else:
            idx = pd.DatetimeIndex(pd.to_datetime(df[time_col], utc=False))
            df = df.drop(columns=[time_col])
        if idx.tz is None:
            idx = idx.tz_localize(self.tz)
        else:
            idx = idx.tz_convert("UTC")
        df = df.copy()
        df.index = idx
        df = df.sort_index()
        rename = {}
        for col in df.columns:
            low = col.strip().lower()
            target = self.col_map.get(low)
            if target and col != target and target not in df.columns:
                rename[col] = target
        df = df.rename(columns=rename)
        keep = [c for c in ("open", "high", "low", "close", "volume",
                            "open_interest") if c in df.columns]
        df = df[keep].astype(float, errors="ignore")
        rep = validate_ohlcv(df)
        prov = provenance or HistoricalProvenance(
            dataset_id=f"local:{instrument}:{timeframe}",
            source="local_file", imported_at=utc_now_str(),
            instruments=[instrument], timeframes=[timeframe],
            timezone=self.tz)
        return NormalizedDataset(
            provenance=prov,
            frames={instrument: {timeframe: df}},
            validation={instrument: rep})
