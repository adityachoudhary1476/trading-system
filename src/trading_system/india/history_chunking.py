"""Provider-independent historical-data chunking.

Splits a large date range into chunks that respect each provider's per-request
limits, fetches each chunk, normalizes, combines, dedupes, sorts, and validates.

The chunk *planning* (date math) is pure and fully testable offline. The *fetch*
is delegated to a callable so the same engine works for any provider (Upstox,
Binance, Stooq) without the engine knowing provider specifics.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Optional

import pandas as pd

from ..data.validation import validate_ohlcv

# Provider resolution-caps (days per single historical request).
# These are conservative upper bounds based on each provider's documented limits.
_PROVIDER_MAX_DAYS = {
    "upstox": {
        "1d": 365, "1w": 365, "1M": 365, "D": 365,  # daily+ capped at 365 days
        "1m": 100, "5m": 100, "15m": 100, "30m": 100, "60m": 100,  # intraday capped at 100
    },
    "binance": {
        "1d": 1000, "1w": 1000, "1M": 1000,  # Binance allows larger daily windows
        "1m": 1000, "5m": 1000, "15m": 1000, "1h": 1000,
    },
}

# Internal timeframe -> provider-agnostic default cap (days).
_DEFAULT_CAP_DAYS = {
    "1d": 365, "1w": 365, "1M": 365, "D": 365,
    "1m": 100, "5m": 100, "15m": 100, "30m": 100,
    "60m": 100, "1h": 100, "2h": 100, "4h": 100,
    "2m": 100, "3m": 100, "10m": 100, "20m": 100, "45m": 100,
}


def _default_cap_days(timeframe: str) -> int:
    """Default days/request cap for an internal timeframe.

    Conservative fallback (no provider import required). If the timeframe is
    unknown, returns 30 days.
    """
    return _DEFAULT_CAP_DAYS.get(timeframe, 30)


def provider_cap_days(provider_name: str, timeframe: str) -> int:
    """Max days/request for a given provider + internal timeframe."""
    caps = _PROVIDER_MAX_DAYS.get(provider_name.lower(), {})
    if timeframe in caps:
        return caps[timeframe]
    # Try matching the provider's resolution token
    return caps.get(timeframe, _default_cap_days(timeframe))


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

    Chunks are stored as **inclusive-looking** DateChunks whose `end` is the last
    datetime *included* in that chunk; consecutive chunks are **exclusive** at the
    boundary (chunk N+1 starts at chunk N's end + 1 day), so there is never a day
    of overlap that would double-count candles. Time-of-day from `start`/`end` is
    preserved (not floored to midnight) so intraday backfills reach the true end.

    Each chunk's span never exceeds the provider's per-request cap. Pure
    function (no I/O).
    """
    start = pd.Timestamp(start)
    end = pd.Timestamp(end)
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    if start > end:
        raise ValueError("start must be <= end")
    cap = max_days_per_request if max_days_per_request is not None else _default_cap_days(timeframe)
    if cap < 1:
        cap = 1
    chunks: list[DateChunk] = []
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=cap), end)
        chunks.append(DateChunk(start=cur, end=nxt))
        cur = nxt + timedelta(days=1)  # exclusive boundary -> no overlap
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
