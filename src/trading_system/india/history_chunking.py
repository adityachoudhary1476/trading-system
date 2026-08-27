"""Provider-independent historical-data chunking.

Splits a large date range into chunks that respect each provider's per-request
limits, fetches each chunk, normalizes, combines, dedupes, sorts, and validates.

The chunk *planning* (date math) is pure and fully testable offline. The *fetch*
is delegated to a callable so the same engine works for any provider (FYERS,
Binance, Stooq) without the engine knowing provider specifics.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Optional

import pandas as pd

from ..data.validation import validate_ohlcv
from .fyers import _RESOLUTION  # documented FYERS caps drive the default planner


# Documented FYERS caps (Day 3 research, docs/FYERS.md).
# resolution -> max days per single /history request.
FYERS_MAX_DAYS_PER_REQUEST = {
    # minute resolutions
    "1": 100, "2": 100, "3": 100, "5": 100, "10": 100, "15": 100,
    "20": 100, "30": 100, "45": 100, "60": 100, "120": 100, "240": 100,
    # day / week / month
    "D": 366, "1D": 366, "1W": 366, "1M": 366,
    # seconds (last 30 trading days) — not used as internal timeframe today
}

# Internal timeframe -> FYERS resolution token (mirrors fyers._RESOLUTION).
_INTERNAL_TO_FYERS = {v: k for k, v in _RESOLUTION.items()} if False else None
_INTERNAL_TF = {v: k for k, v in _RESOLUTION.items()}


def _fy_cap_days(timeframe: str) -> int:
    """Max days/request for an internal timeframe under FYERS caps."""
    token = _INTERNAL_TF.get(timeframe)
    if token is None:
        # Unknown resolution: be conservative (small window) rather than assume.
        return 30
    return FYERS_MAX_DAYS_PER_REQUEST.get(token, 30)


@dataclass
class DateChunk:
    start: pd.Timestamp
    end: pd.Timestamp

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"[{self.start.date()} -> {self.end.date()}]"


def plan_chunks(
    start: pd.Timestamp,
    end: pd.Timestamp,
    timeframe: str,
    max_days_per_request: int | None = None,
) -> list[DateChunk]:
    """Split [start, end] into non-overlapping daily-bounded chunks.

    Each chunk's span never exceeds the provider's per-request cap. Chunks are
    adjacent and cover the full range exactly. Pure function (no I/O).
    """
    start = pd.Timestamp(start).normalize()
    end = pd.Timestamp(end).normalize()
    if start > end:
        raise ValueError("start must be <= end")
    cap = max_days_per_request if max_days_per_request is not None else _fy_cap_days(timeframe)
    if cap < 1:
        cap = 1
    chunks: list[DateChunk] = []
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=cap - 1), end)
        chunks.append(DateChunk(start=cur, end=nxt))
        cur = nxt + timedelta(days=1)
    return chunks


def combine_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate, drop duplicate timestamps, sort, return normalized frame."""
    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="first")]
    df = df.sort_index()
    return df


class ChunkedHistoricalFetcher:
    """Fetches history in capped chunks via a provider-supplied fetch callable.

    `fetch_chunk(start, end) -> pd.DataFrame` is the only provider coupling. The
    engine handles planning, combining, dedup, validation, partial-failure
    tolerance, and retry of transient errors.
    """

    def __init__(
        self,
        timeframe: str,
        fetch_chunk: Callable[[pd.Timestamp, pd.Timestamp], pd.DataFrame],
        max_days_per_request: int | None = None,
        max_retries: int = 2,
        retry_backoff: float = 0.0,
    ) -> None:
        self.timeframe = timeframe
        self.fetch_chunk = fetch_chunk
        self.max_days_per_request = max_days_per_request
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff

    def fetch(
        self,
        start: pd.Timestamp,
        end: pd.Timestamp,
        validate: bool = True,
    ) -> pd.DataFrame:
        chunks = plan_chunks(start, end, self.timeframe, self.max_days_per_request)
        frames: list[pd.DataFrame] = []
        failures = 0
        for ch in chunks:
            ok = False
            for attempt in range(self.max_retries + 1):
                try:
                    frames.append(self.fetch_chunk(ch.start, ch.end))
                    ok = True
                    break
                except Exception:
                    if attempt < self.max_retries and self.retry_backoff:
                        import time

                        time.sleep(self.retry_backoff)
            if not ok:
                failures += 1  # tolerate partial failure; record and continue
        combined = combine_frames(frames)
        if validate and not combined.empty:
            report = validate_ohlcv(combined, self.timeframe)
            if not report.ok:
                # Surface validation problems loudly but still return data.
                from ..config import log

                log.error("Chunked history combined dataset invalid: %s", report.errors)
        if failures:
            from ..config import log

            log.warning("Chunked fetch: %d/%d chunks failed", failures, len(chunks))
        return combined
