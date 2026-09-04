"""Ingestion pipeline: request -> validate -> normalize -> store -> report.

This is the heart of Day 1. It ties together a provider, the validator, and
the store, and it is strictly data-only: it never trades.
"""
from __future__ import annotations

import time
import datetime as dt
import os
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from ..config import settings, configure_logging, log
from ..config.settings import Settings
from .provider_exports import get_provider
from .validation import validate_ohlcv, DataValidationError
from ..storage.database import MarketStore


@dataclass
class IngestionResult:
    symbol: str
    timeframe: str
    provider: str
    requested: int = 0
    received: int = 0
    valid: int = 0
    inserted: int = 0
    rejected: int = 0
    duration_s: float = 0.0
    error: Optional[str] = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "provider": self.provider,
            "requested": self.requested,
            "received": self.received,
            "valid": self.valid,
            "inserted": self.inserted,
            "rejected": self.rejected,
            "duration_s": round(self.duration_s, 3),
            "error": self.error,
            "warnings": self.warnings,
        }


class IngestionPipeline:
    def __init__(
        self,
        cfg: Settings | None = None,
        store: MarketStore | None = None,
    ) -> None:
        self.cfg = cfg or settings
        configure_logging(self.cfg.logging)
        self.provider = get_provider(
            self.cfg.market.provider,
            timeout=int(os.getenv("PROVIDER_TIMEOUT", "20")),
        )
        self.store = store or MarketStore(self.cfg.storage.db_url)

    def ingest_symbol(
        self, symbol: str, timeframe: str | None = None, limit: int | None = None
    ) -> IngestionResult:
        tf = timeframe or self.cfg.market.timeframe
        limit = limit or self.cfg.market.lookback_bars
        res = IngestionResult(
            symbol=symbol, timeframe=tf, provider=self.provider.name
        )
        start = time.perf_counter()
        try:
            log.info(
                "INGEST_START symbol=%s tf=%s provider=%s limit=%s",
                symbol, tf, self.provider.name, limit,
            )
            df = self.provider.get_historical(symbol, tf, limit)
            res.requested = limit
            res.received = len(df)
            log.info("FETCHED symbol=%s rows=%d", symbol, len(df))

            report = validate_ohlcv(df, tf)
            res.valid = len(report.valid)
            res.rejected = len(report.rejected)
            for iss in report.issues:
                if iss.severity.value == "warning":
                    res.warnings.append(f"{iss.code}: {iss.message}")
                    log.warning("VALIDATION_WARN %s: %s", iss.code, iss.message)
                else:
                    log.error("VALIDATION_ERR %s: %s", iss.code, iss.message)

            if not report.ok:
                raise DataValidationError(report)

            # Persist as idempotent rows.
            rows = self._to_records(report.valid, symbol, tf, self.provider.name)
            res.inserted = self.store.upsert_many(rows)
            log.info(
                "STORED symbol=%s inserted=%d (duplicates skipped)",
                symbol, res.inserted,
            )
        except Exception as e:  # surface explicitly, never swallow
            res.error = f"{type(e).__name__}: {e}"
            log.error("INGEST_FAILED symbol=%s %s", symbol, res.error)
        finally:
            res.duration_s = time.perf_counter() - start
        return res

    @staticmethod
    def _to_records(
        df: pd.DataFrame, symbol: str, tf: str, provider: str
    ) -> list[dict]:
        recs = []
        for _, row in df.iterrows():
            ts = row["timestamp"]
            # Normalize to a tz-aware UTC python datetime for consistent storage.
            if isinstance(ts, pd.Timestamp):
                ts = ts.to_pydatetime()
            if isinstance(ts, dt.datetime) and ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            recs.append(
                {
                    "symbol": symbol,
                    "timeframe": tf,
                    "timestamp": ts,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                    "provider": provider,
                }
            )
        return recs

    def run(self, symbols: list[str] | None = None) -> list[IngestionResult]:
        symbols = symbols or self.cfg.market.symbols
        return [self.ingest_symbol(s) for s in symbols]
