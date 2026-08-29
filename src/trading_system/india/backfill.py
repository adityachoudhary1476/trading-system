"""Historical backfill for Indian (FYERS) market data — DATA ONLY.

This command reuses the existing, provider-independent architecture:

    FYERSMarketDataProvider.get_historical   (provider boundary; symbol mapping)
        -> plan_chunks                       (history_chunking, FYERS caps)
        -> per-chunk fetch + normalize
        -> validate_ohlcv                    (data.validation safety gate)
        -> MarketStore.upsert_many           (storage.database, idempotent)

It NEVER places orders, NEVER trades, and NEVER exposes credentials. It is a
bulk, resumable, idempotent loader for historical OHLCV candles into the same
SQLite dataset the rest of the system reads from.

Design notes
------------
* The FYERS per-request caps (100 days for minute resolutions, 366 for day/
  week/month) live in ``history_chunking`` and are reused here via ``plan_chunks``.
  We do NOT implement a second chunking algorithm.
* Validation reuses ``validate_ohlcv``. Error-severity problems are recorded and
  the offending rows are SKIPPED (never stored); valid rows are persisted. This is
  the intended batch behavior for a large download — one bad row must not void a
  multi-year pull. The rejection codes are surfaced in the report.
* Storage reuses ``MarketStore.upsert_many``, whose UNIQUE(symbol, timeframe,
  timestamp, provider, exchange) constraint makes re-runs idempotent: a second run
  over the same period stores zero new rows.
* FYERS errors are represented by the typed exceptions in ``fyers``
  (FYERSAuthError / FYERSAPIError / FYERSNetworkError / FYERSRateLimitError) so an
  authentication failure is never mistaken for "no market data".
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

import pandas as pd

from ..config import log
from ..data.validation import validate_ohlcv
from ..storage.database import MarketStore
from .fyers import (
    FYERSMarketDataProvider,
    FYERSAuthError,
    FYERSAPIError,
    FYERSNetworkError,
    FYERSRateLimitError,
    FYERSError,
)
from .history_chunking import plan_chunks, DateChunk
from .instruments import InstrumentRegistry


class BackfillStatus(str, Enum):
    """Final status for a single symbol's backfill."""

    COMPLETE = "COMPLETE"      # all chunks fetched, some data stored
    PARTIAL = "PARTIAL"        # some chunks failed but data was stored
    EMPTY = "EMPTY"            # no data returned and no errors
    AUTH_ERROR = "AUTH_ERROR"  # FYERS authentication failed
    API_ERROR = "API_ERROR"    # FYERS returned an API-level error
    NETWORK_ERROR = "NETWORK_ERROR"  # transport failure / exhausted retries
    VALIDATION_ERROR = "VALIDATION_ERROR"  # data returned but rejected by validation
    FAILED = "FAILED"          # all chunks failed (non-auth)


@dataclass
class ChunkOutcome:
    """Outcome of a single chunk fetch (for progress + summary)."""

    index: int
    total: int
    start: pd.Timestamp
    end: pd.Timestamp
    status: str                # OK | FAILED
    rows: int = 0
    error: str = ""


@dataclass
class BackfillSymbolResult:
    """Aggregated result for one symbol/timeframe backfill."""

    symbol: str
    timeframe: str
    fyers_symbol: str = ""
    exchange: str = ""
    provider: str = "fyers"
    contract_id: str = ""

    # Requested vs actual (what FYERS actually returned).
    requested_start: Optional[pd.Timestamp] = None
    requested_end: Optional[pd.Timestamp] = None
    actual_start: Optional[pd.Timestamp] = None
    actual_end: Optional[pd.Timestamp] = None

    chunks_total: int = 0
    chunks_ok: int = 0
    chunks_failed: int = 0

    fetched: int = 0          # total rows returned by FYERS across chunks
    valid: int = 0            # rows that passed validation
    stored: int = 0           # new rows persisted (idempotent)
    skipped: int = 0          # rows rejected by validation or failed chunks

    status: BackfillStatus = BackfillStatus.EMPTY
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    chunks: list[ChunkOutcome] = field(default_factory=list)
    dry_run: bool = False

    @property
    def non_auth_failure(self) -> bool:
        return self.status in (
            BackfillStatus.PARTIAL,
            BackfillStatus.FAILED,
            BackfillStatus.API_ERROR,
            BackfillStatus.NETWORK_ERROR,
            BackfillStatus.VALIDATION_ERROR,
        )


def _as_utc(ts: pd.Timestamp) -> pd.Timestamp:
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _resolve_range(
    timeframe: str,
    days: Optional[int],
    start: Optional[pd.Timestamp],
    end: Optional[pd.Timestamp],
):
    """Resolve the requested [start, end] range from user inputs.

    Precedence:
      * explicit --start / --end win over --days for their respective bound;
      * if --end omitted, the endpoint is the current UTC time ("until now");
      * if --start omitted, it is derived from --days (default 365 when neither
        --days nor --start is supplied, with a recorded warning).

    Returns (start, end) as tz-aware UTC timestamps.
    """
    now = pd.Timestamp.now(tz="UTC")

    if end is None:
        end = now
    else:
        end = _as_utc(pd.Timestamp(end))

    if start is not None:
        start = _as_utc(pd.Timestamp(start))
    elif days is not None:
        start = end - pd.Timedelta(days=int(days))
    else:
        # Safe default; user should usually pass --days explicitly.
        start = end - pd.Timedelta(days=365)
        # recorded by the caller as a warning

    if start > end:
        raise ValueError(f"start ({start.date()}) is after end ({end.date()})")
    return start, end


def _chunk_plan(start: pd.Timestamp, end: pd.Timestamp, timeframe: str) -> list[DateChunk]:
    """Plan chunks on the date range.

    Delegates to ``plan_chunks`` (the existing, tested chunking primitive) which
    now preserves time-of-day and produces exclusive boundaries (no overlap). The
    final chunk's end is already the true ``end`` (which may carry a time-of-day),
    so intraday backfills reach the latest not-yet-finalized candle FYERS has.
    """
    return plan_chunks(start, end, timeframe)


class BackfillEngine:
    """Provider-agnostic historical backfill engine (wired to FYERS by default)."""

    def __init__(
        self,
        provider: FYERSMarketDataProvider,
        store: MarketStore,
        registry: Optional[InstrumentRegistry] = None,
        max_retries: int = 2,
        retry_backoff: float = 0.5,
        verbose: bool = False,
        progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.provider = provider
        self.store = store
        self.registry = registry or InstrumentRegistry()
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.verbose = verbose
        self._progress = progress or (lambda s: None)

    # -- public API ----------------------------------------------------------
    def backfill_symbol(
        self,
        symbol: str,
        timeframe: str,
        days: Optional[int] = None,
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
        dry_run: bool = False,
    ) -> BackfillSymbolResult:
        res = BackfillSymbolResult(symbol=symbol, timeframe=timeframe)
        res.provider = self.provider.name

        # Symbol mapping (reuse existing abstraction; no ad-hoc manipulation).
        try:
            instr = self.registry.resolve(symbol)
            res.exchange = instr.internal.exchange
            res.fyers_symbol = self.provider._fyers_symbol(symbol)
            res.contract_id = getattr(instr, "contract_id", None) or symbol
        except Exception as e:  # pragma: no cover - registry is robust
            res.exchange = symbol.split(":", 1)[0] if ":" in symbol else ""
            res.fyers_symbol = symbol
            res.contract_id = symbol

        # Resolve the requested range.
        try:
            req_start, req_end = _resolve_range(timeframe, days, start, end)
        except ValueError as e:
            res.status = BackfillStatus.FAILED
            res.error = str(e)
            return res
        res.requested_start = req_start
        res.requested_end = req_end
        if days is None and start is None:
            res.warnings.append(
                "No --days or --start supplied; defaulting to 365 days ending now."
            )

        chunks = _chunk_plan(req_start, req_end, timeframe)
        res.chunks_total = len(chunks)

        if dry_run:
            res.dry_run = True
            res.status = BackfillStatus.COMPLETE
            for i, ch in enumerate(chunks, 1):
                res.chunks.append(
                    ChunkOutcome(i, len(chunks), ch.start, ch.end, "PLAN")
                )
            self._progress(
                f"  [dry-run] {symbol} {timeframe}: {len(chunks)} chunk(s), "
                f"{req_start.date()} -> {req_end.date()}"
            )
            return res

        # ---- actual fetch / validate / store --------------------------------
        combined_rows: list[dict] = []
        seen_ts: set[pd.Timestamp] = set()
        fatal_auth = False
        dominant_error: Optional[str] = None

        for i, ch in enumerate(chunks, 1):
            outcome = ChunkOutcome(i, len(chunks), ch.start, ch.end, "OK")
            try:
                df = self._fetch_chunk_with_retry(symbol, timeframe, ch.start, ch.end)
                rows = self._df_to_rows(df, symbol, timeframe, res.exchange, res.provider, res.contract_id)
                # Deduplicate across chunk boundaries by timestamp.
                new_rows = [r for r in rows if r["timestamp"] not in seen_ts]
                for r in new_rows:
                    seen_ts.add(r["timestamp"])
                combined_rows.extend(new_rows)
                outcome.rows = len(new_rows)
                res.fetched += len(rows)
                res.chunks_ok += 1
            except FYERSAuthError as e:
                fatal_auth = True
                outcome.status = "FAILED"
                outcome.error = f"AUTH: {e}"
                dominant_error = dominant_error or "auth"
                res.chunks_failed += 1
                res.warnings.append(f"Chunk {i}: authentication failed: {e}")
            except (FYERSAPIError, FYERSRateLimitError) as e:
                outcome.status = "FAILED"
                outcome.error = f"API: {e}"
                dominant_error = dominant_error or "api"
                res.chunks_failed += 1
                res.warnings.append(f"Chunk {i}: API error: {e}")
            except FYERSNetworkError as e:
                outcome.status = "FAILED"
                outcome.error = f"NETWORK: {e}"
                dominant_error = dominant_error or "network"
                res.chunks_failed += 1
                res.warnings.append(f"Chunk {i}: network error: {e}")
            except FYERSError as e:
                outcome.status = "FAILED"
                outcome.error = f"FYERS: {e}"
                dominant_error = dominant_error or "api"
                res.chunks_failed += 1
                res.warnings.append(f"Chunk {i}: FYERS error: {e}")
            except Exception as e:  # noqa: BLE001 - surface, never crash the whole run
                outcome.status = "FAILED"
                outcome.error = f"{type(e).__name__}: {e}"
                dominant_error = dominant_error or "network"
                res.chunks_failed += 1
                res.warnings.append(f"Chunk {i}: unexpected error: {e}")

            res.chunks.append(outcome)
            self._progress(
                f"  [{i}/{len(chunks)}] {ch.start.date()} -> {ch.end.date()} ... "
                f"{'OK' if outcome.status == 'OK' else 'FAIL'} ({outcome.rows} rows)"
            )
            if fatal_auth:
                # All remaining chunks would also fail; stop early.
                res.chunks_failed = len(chunks) - (i - 1)
                break

        # ---- validation + persistence ---------------------------------------
        stored = 0
        if combined_rows:
            df = pd.DataFrame(combined_rows).set_index("timestamp").sort_index()
            report = validate_ohlcv(df, timeframe)
            res.valid = len(report.valid)
            res.skipped = len(report.rejected)  # rows rejected by validation
            for iss in report.issues:
                if iss.severity.value == "warning":
                    res.warnings.append(f"{iss.code}: {iss.message}")
                else:
                    res.warnings.append(f"REJECTED({iss.code}): {iss.message}")

            if report.valid.empty:
                res.status = BackfillStatus.VALIDATION_ERROR
                res.error = "All fetched rows rejected by validation"
            else:
                # Persist only validated rows (idempotent insert).
                recs = self._df_to_rows(
                    report.valid, symbol, timeframe, res.exchange, res.provider, res.contract_id
                )
                try:
                    stored = self.store.upsert_many(recs)
                except Exception as e:  # noqa: BLE001
                    res.warnings.append(f"Store failed: {type(e).__name__}: {e}")
                    res.status = BackfillStatus.FAILED
                    res.error = f"store error: {e}"
                res.stored = stored
                if res.status != BackfillStatus.FAILED:
                    if res.chunks_failed > 0 and res.fetched > 0:
                        res.status = BackfillStatus.PARTIAL
                    elif res.fetched == 0:
                        res.status = BackfillStatus.EMPTY
                    else:
                        res.status = BackfillStatus.COMPLETE
                # If validation rejected everything but we had data, keep
                # VALIDATION_ERROR.
                if res.fetched > 0 and res.stored == 0 and report.valid.empty:
                    res.status = BackfillStatus.VALIDATION_ERROR
        else:
            # Nothing fetched.
            if fatal_auth:
                res.status = BackfillStatus.AUTH_ERROR
                res.error = "FYERS authentication failed (see warnings)"
            elif dominant_error == "api":
                res.status = BackfillStatus.API_ERROR
                res.error = "All chunks failed with FYERS API errors"
            elif dominant_error == "network":
                res.status = BackfillStatus.NETWORK_ERROR
                res.error = "All chunks failed with network errors"
            elif res.chunks_failed > 0:
                res.status = BackfillStatus.FAILED
                res.error = "All chunks failed"
            else:
                res.status = BackfillStatus.EMPTY

        # Actual stored range from what FYERS returned (never fabricated).
        if combined_rows:
            ts_sorted = sorted(seen_ts)
            res.actual_start = ts_sorted[0]
            res.actual_end = ts_sorted[-1]

        return res

    # -- internals -----------------------------------------------------------
    def _fetch_chunk_with_retry(
        self, symbol: str, timeframe: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        last: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                return self.provider.get_historical(
                    symbol, timeframe, start=start, end=end
                )
            except (FYERSAuthError, FYERSAPIError, FYERSRateLimitError, FYERSError):
                raise  # authoritative; do not retry
            except Exception as e:  # noqa: BLE001 - transient network/parse
                last = e
                if attempt < self.max_retries and self.retry_backoff:
                    import time

                    time.sleep(self.retry_backoff)
        # Re-raise as a network-style error so the status mapping is consistent.
        raise FYERSNetworkError(f"chunk fetch failed after retries: {last}")

    @staticmethod
    def _df_to_rows(df, symbol, timeframe, exchange, provider, contract_id=None) -> list[dict]:
        if df is None or len(df) == 0:
            return []
        # `validate_ohlcv` returns frames with a `timestamp` COLUMN + RangeIndex;
        # provider frames / combined frames use a DatetimeIndex. Support both.
        if isinstance(df.index, pd.DatetimeIndex):
            timestamps = df.index
        elif "timestamp" in getattr(df, "columns", []):
            timestamps = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        else:
            timestamps = df.index
        recs = []
        for ts, (_, row) in zip(timestamps, df.iterrows()):
            ts = pd.Timestamp(ts)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            t = ts.to_pydatetime()
            if t.tzinfo is None:
                t = t.replace(tzinfo=dt.timezone.utc)
            recs.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "timestamp": t,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                    "provider": provider,
                    "exchange": exchange,
                    "contract_id": contract_id or symbol,
                }
            )
        return recs


def format_symbol_summary(res: BackfillSymbolResult) -> str:
    """One-line summary row for a single symbol (used in the final table)."""
    return (
        f"{res.symbol:12s} {res.status.value:14s} "
        f"{res.fetched:>9,} fetched  {res.stored:>9,} new"
    )
