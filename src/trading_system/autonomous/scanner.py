"""Autonomous Market Scanner — Phase 2.

Deterministic, paper-only candidate discovery for the autonomous layer.

The scanner inspects a configured universe of instruments against the
repository's existing market-data path, applies deterministic eligibility
filters, and emits a self-contained, auditable :class:`MarketScanResult`.

This layer deliberately stops at *candidate discovery*. It does NOT:
  - select strategies or timeframes
  - generate trade signals
  - place orders or touch a broker
  - create deployments
  - perform live trading

Dependency direction (Phase 2 boundary)::

    Market Data (provider callable)  ->  Market Scanner  ->  Scan Result
        (Phase 3: candidate selection / strategy-timeframe selection come next)

Reused existing abstractions (no duplication):
  - trading_system.india.instruments.InternalSymbol  (symbol normalization/validation)
  - trading_system.india.market_calendar.TradingCalendar / SessionPhase  (session logic)
  - trading_system.data.validation.validate_ohlcv  (OHLCV validity rules)
  - trading_system.data.base.MarketDataProvider  (the existing provider ABC)
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Optional

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trading_system.data.base import MarketDataProvider
from trading_system.data.validation import ValidationIssue, validate_ohlcv
from trading_system.india.instruments import InternalSymbol
from trading_system.india.market_calendar import SessionPhase, TradingCalendar

UTC = timezone.utc

# Default number of bars requested from the data provider per symbol.
DEFAULT_SCAN_LIMIT = 250
# Default staleness threshold: a bar older than this is rejected as stale.
DEFAULT_MAX_FRESHNESS = timedelta(days=7)


def _normalize_symbol(symbol: str) -> str:
    """Normalize a symbol to the canonical ``EXCHANGE:SYMBOL`` key.

    Reuses :class:`InternalSymbol` so the scanner shares the repository's
    single symbol-identity abstraction. Raises ``ValueError`` on invalid input.
    """
    return InternalSymbol.parse(symbol).key


def _now_utc() -> datetime:
    return datetime.now(UTC)


class RejectionReason(str, Enum):
    """Structured, enumerable reasons an instrument was not a candidate.

    Using the smallest sensible enum supported by the actual implementation —
    no free-form strings drive eligibility.
    """

    INVALID_SYMBOL = "invalid_symbol"
    MISSING_MARKET_DATA = "missing_market_data"
    STALE_MARKET_DATA = "stale_market_data"
    OUTSIDE_SESSION = "outside_session"
    INSUFFICIENT_LIQUIDITY = "insufficient_liquidity"
    INVALID_MARKET_DATA = "invalid_market_data"
    UNIVERSE_REJECTED = "universe_rejected"


class MarketUniverse(BaseModel):
    """Deterministic, validated universe of instruments to scan.

    Symbols are normalized to ``EXCHANGE:SYMBOL`` keys, de-duplicated while
    preserving first-seen insertion order, and validated at construction
    time. An empty universe is valid (it yields an empty scan result).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbols: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("symbols", mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return tuple()
        if isinstance(value, str):
            raise ValueError(
                "symbols must be an iterable of symbol strings, not a single string"
            )
        if not hasattr(value, "__iter__"):
            raise ValueError("symbols must be iterable")
        seen: set[str] = set()
        ordered: list[str] = []
        for raw in value:
            normalized = _normalize_symbol(str(raw))
            if normalized not in seen:
                seen.add(normalized)
                ordered.append(normalized)
        return tuple(ordered)

    def __len__(self) -> int:
        return len(self.symbols)

    def __iter__(self):
        return iter(self.symbols)

    def __contains__(self, symbol: object) -> bool:
        try:
            return _normalize_symbol(str(symbol)) in self.symbols
        except ValueError:
            return False

    @property
    def size(self) -> int:
        return len(self.symbols)

    @classmethod
    def default(cls) -> "MarketUniverse":
        """Convenience universe built from the repository's curated defaults.

        Reuses ``DEFAULT_INSTRUMENTS`` (a small CLI/test convenience set).
        This is NOT hard-coded NIFTY 50/100 logic.
        """
        from trading_system.india.instruments import DEFAULT_INSTRUMENTS

        return cls(symbols=[instrument.key for instrument in DEFAULT_INSTRUMENTS])


class ScannerConfig(BaseModel):
    """Typed configuration for :class:`MarketScanner`.

    Only constraints implementable deterministically from existing repository
    data are exposed. Invalid values are rejected at construction time so a
    misconfigured scanner can never produce candidates.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    enabled: bool = True
    universe: MarketUniverse
    timeframe: str = Field(default="1d", min_length=1)
    # Optional deterministic cap on eligible candidates. Excess candidates are
    # truncated by ascending symbol order (never random selection).
    max_candidates: Optional[int] = Field(default=None, ge=1)
    # Maximum acceptable age of the latest bar. A bar older than this is
    # rejected as stale. ``None`` disables the freshness gate.
    max_freshness: Optional[timedelta] = Field(default=DEFAULT_MAX_FRESHNESS)
    # Minimum average volume over the scanned history. ``None`` disables the
    # liquidity gate. Liquidity is derived only from the OHLCV volume column;
    # no other liquidity source is inventoried in this repository.
    min_liquidity: Optional[float] = Field(default=None, ge=0.0)
    # When True, an instrument's latest bar must fall within a regular trading
    # session of the configured calendar (per-instrument data-session check).
    require_regular_session: bool = True
    # Frozen point in time the scan is evaluated against. ``None`` -> now(UTC).
    scan_timestamp: Optional[datetime] = None
    # Authoritative market calendar (session/holiday logic). Defaults to the
    # repository calendar (no holidays).
    calendar: Any = Field(default_factory=TradingCalendar)
    # A callable ``(symbol, timeframe) -> Optional[pd.DataFrame]`` matching
    # ``PaperTradingControlCenter.load_market_data``. A ``MarketDataProvider``
    # instance is also accepted and adapted.
    data_provider: Any = None
    # Number of bars requested from the provider per symbol.
    scan_limit: int = Field(default=DEFAULT_SCAN_LIMIT, ge=1)
    # Audit label identifying the data source.
    data_provider_source: str = Field(default="control_center.load_market_data")

    @field_validator("timeframe")
    @classmethod
    def _timeframe_nonempty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("timeframe must be a non-empty string")
        return value

    @model_validator(mode="after")
    def _validate_duration_constraints(self) -> "ScannerConfig":
        if self.max_freshness is not None and self.max_freshness <= timedelta(0):
            raise ValueError("max_freshness must be a positive duration")
        if self.max_candidates is not None and self.max_candidates < 1:
            raise ValueError("max_candidates must be >= 1 when set")
        return self

    def resolved_scan_timestamp(self) -> datetime:
        """The concrete scan timestamp (UTC, tz-aware), defaulting to now."""
        ts = self.scan_timestamp
        if ts is None:
            return _now_utc()
        if ts.tzinfo is None:
            return ts.replace(tzinfo=UTC)
        return ts

    def resolved_calendar(self) -> TradingCalendar:
        calendar = self.calendar
        if calendar is None:
            return TradingCalendar()
        return calendar


def _resolve_data_provider(
    data_provider: Any, scan_limit: int
) -> Callable[[str, str], Optional[pd.DataFrame]]:
    """Normalize the configured data source into a ``(symbol, timeframe)`` callable.

    Accepts either a plain callable (matching ``control_center.load_market_data``)
    or an existing :class:`MarketDataProvider` instance (adapted to the callable
    contract). ``None`` yields a provider that always returns no data (fail-closed).
    """
    if data_provider is None:
        return lambda symbol, timeframe: None

    def _from_provider(symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
        return data_provider.get_historical(symbol, timeframe, limit=scan_limit)

    if isinstance(data_provider, MarketDataProvider):
        return _from_provider

    if not callable(data_provider):
        raise TypeError(
            "data_provider must be a callable (symbol, timeframe) -> Optional[DataFrame] "
            "or a MarketDataProvider instance"
        )
    return data_provider


def _summarize_issues(issues: list[ValidationIssue]) -> str:
    parts = [f"{iss.code}: {iss.message}" for iss in issues]
    return "; ".join(parts) if parts else "data failed validation"


def _compute_scan_id(config: ScannerConfig, scan_ts: datetime) -> str:
    """Deterministic scan identity for the same universe/config/snapshot."""
    payload = {
        "symbols": list(config.universe.symbols),
        "timeframe": config.timeframe,
        "max_candidates": config.max_candidates,
        "max_freshness": config.max_freshness.total_seconds()
        if config.max_freshness is not None
        else None,
        "min_liquidity": config.min_liquidity,
        "require_regular_session": config.require_regular_session,
        "scan_timestamp": scan_ts.isoformat(),
        "scan_limit": config.scan_limit,
        "provider": config.data_provider_source,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(("market_scan:" + blob).encode("utf-8")).hexdigest()[:64]


def _config_snapshot(config: ScannerConfig) -> dict[str, Any]:
    """Serializable, auditable snapshot of the active scanner configuration."""
    return {
        "enabled": config.enabled,
        "timeframe": config.timeframe,
        "max_candidates": config.max_candidates,
        "max_freshness_seconds": config.max_freshness.total_seconds()
        if config.max_freshness is not None
        else None,
        "min_liquidity": config.min_liquidity,
        "require_regular_session": config.require_regular_session,
        "universe_size": config.universe.size,
        "scan_limit": config.scan_limit,
        "data_provider_source": config.data_provider_source,
    }


class ScanCandidate(BaseModel):
    """A single instrument that passed all eligibility filters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scan_id: str
    symbol: str
    timeframe: str
    scan_timestamp: str
    market_timestamp: Optional[str] = None
    latest_price: Optional[float] = None
    volume: Optional[float] = None
    avg_volume: Optional[float] = None
    freshness_seconds: Optional[float] = None
    data_points: int = 0
    eligibility: str = "eligible"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScanRejection(BaseModel):
    """A single instrument that failed an eligibility filter, with a structured reason."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scan_id: str
    symbol: str
    reason: RejectionReason
    detail: str = ""
    market_timestamp: Optional[str] = None


class MarketScanResult(BaseModel):
    """Self-contained, deterministic, auditable result of a market scan.

    For the same market snapshot and configuration the *eligibility outcome*
    (candidates/rejections) is deterministic; the audit timestamps
    (``started_at``/``completed_at``) reflect when the scan ran and are
    intentionally non-deterministic.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scan_id: str
    scan_timestamp: str
    timeframe: str
    enabled: bool
    universe_size: int
    scanned_count: int
    eligible_count: int
    rejected_count: int
    skipped_count: int
    started_at: str
    completed_at: str
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    candidates: list[ScanCandidate] = Field(default_factory=list)
    rejections: list[ScanRejection] = Field(default_factory=list)
    data_provider_source: str = "unknown"

    @property
    def symbols_eligible(self) -> list[str]:
        return [c.symbol for c in self.candidates]

    @property
    def symbols_rejected(self) -> list[str]:
        return [r.symbol for r in self.rejections]

    def rejection_for(self, symbol: str) -> Optional[ScanRejection]:
        for r in self.rejections:
            if r.symbol == symbol:
                return r
        return None

    def is_deterministic_with(self, other: "MarketScanResult") -> bool:
        """True when two scans share identity and produced identical eligibility."""
        return (
            self.scan_id == other.scan_id
            and [c.symbol for c in self.candidates] == [c.symbol for c in other.candidates]
            and [r.symbol for r in self.rejections] == [r.symbol for r in other.rejections]
        )


class MarketScanner:
    """Deterministic market scanner producing candidate discovery results.

    Callable independently of the :class:`AutonomousController`:

        scanner = MarketScanner(config)
        result = scanner.scan()

    The data provider is a pure read path; the scanner never places orders,
    creates deployments, or touches any broker.
    """

    def __init__(self, config: ScannerConfig) -> None:
        if not isinstance(config, ScannerConfig):
            raise TypeError("config must be a ScannerConfig")
        self.config = config
        self._provider = _resolve_data_provider(config.data_provider, config.scan_limit)
        self._calendar = config.resolved_calendar()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def scan(self) -> MarketScanResult:
        """Scan the configured universe and return a deterministic result."""
        scan_ts = self.config.resolved_scan_timestamp()
        scan_id = _compute_scan_id(self.config, scan_ts)
        started_at = _now_utc()

        if not self.config.enabled:
            # Disabled scans evaluate nothing; every universe member is skipped.
            completed_at = _now_utc()
            universe_size = self.config.universe.size
            return MarketScanResult(
                scan_id=scan_id,
                scan_timestamp=scan_ts.isoformat(),
                timeframe=self.config.timeframe,
                enabled=False,
                universe_size=universe_size,
                scanned_count=0,
                eligible_count=0,
                rejected_count=0,
                skipped_count=universe_size,
                started_at=started_at.isoformat(),
                completed_at=completed_at.isoformat(),
                config_snapshot=_config_snapshot(self.config),
                candidates=[],
                rejections=[],
                data_provider_source=self.config.data_provider_source,
            )

        candidates: list[ScanCandidate] = []
        rejections: list[ScanRejection] = []
        symbols = list(self.config.universe.symbols)

        for symbol in symbols:
            candidate, rejection = self._evaluate_symbol(symbol, scan_ts, scan_id)
            if candidate is not None:
                candidates.append(candidate)
            else:
                assert rejection is not None
                rejections.append(rejection)

        # Deterministic ordering (never random).
        candidates.sort(key=lambda c: c.symbol)
        rejections.sort(key=lambda r: r.symbol)

        # Deterministic cap: ascending symbol order after sorting.
        if (
            self.config.max_candidates is not None
            and len(candidates) > self.config.max_candidates
        ):
            candidates = candidates[: self.config.max_candidates]

        completed_at = _now_utc()
        return MarketScanResult(
            scan_id=scan_id,
            scan_timestamp=scan_ts.isoformat(),
            timeframe=self.config.timeframe,
            enabled=True,
            universe_size=len(symbols),
            scanned_count=len(symbols),
            eligible_count=len(candidates),
            rejected_count=len(rejections),
            skipped_count=0,
            started_at=started_at.isoformat(),
            completed_at=completed_at.isoformat(),
            config_snapshot=_config_snapshot(self.config),
            candidates=candidates,
            rejections=rejections,
            data_provider_source=self.config.data_provider_source,
        )

    # ------------------------------------------------------------------ #
    # Eligibility pipeline
    # ------------------------------------------------------------------ #
    def _evaluate_symbol(
        self, symbol: str, scan_ts: datetime, scan_id: str
    ) -> tuple[Optional[ScanCandidate], Optional[ScanRejection]]:
        # 1. Symbol identity / normalization (universe guarantees validity, but
        #    re-check defensively so a bad provider call can't forge a candidate).
        try:
            internal = InternalSymbol.parse(symbol)
        except ValueError as exc:
            return None, ScanRejection(
                scan_id=scan_id,
                symbol=symbol,
                reason=RejectionReason.INVALID_SYMBOL,
                detail=str(exc),
            )

        # 2. Market data fetch (fail-closed: never a false candidate).
        try:
            df = self._provider(internal.key, self.config.timeframe)
        except Exception as exc:  # noqa: BLE001 — record and reject, never forge
            return None, ScanRejection(
                scan_id=scan_id,
                symbol=internal.key,
                reason=RejectionReason.MISSING_MARKET_DATA,
                detail=f"provider error: {exc}",
            )

        if not isinstance(df, pd.DataFrame):
            return None, ScanRejection(
                scan_id=scan_id,
                symbol=internal.key,
                reason=RejectionReason.MISSING_MARKET_DATA,
                detail="provider returned no market data",
            )
        if len(df) == 0:
            return None, ScanRejection(
                scan_id=scan_id,
                symbol=internal.key,
                reason=RejectionReason.MISSING_MARKET_DATA,
                detail="provider returned an empty frame",
            )

        # 3. Data validity via the repository's authoritative OHLCV rules.
        try:
            report = validate_ohlcv(df, self.config.timeframe)
        except Exception as exc:  # noqa: BLE001 — defensive; validator should not raise
            return None, ScanRejection(
                scan_id=scan_id,
                symbol=internal.key,
                reason=RejectionReason.INVALID_MARKET_DATA,
                detail=f"data validation error: {exc}",
            )

        if not report.ok:
            return None, ScanRejection(
                scan_id=scan_id,
                symbol=internal.key,
                reason=RejectionReason.INVALID_MARKET_DATA,
                detail=_summarize_issues(report.issues),
            )

        valid = report.valid
        if len(valid) == 0:
            return None, ScanRejection(
                scan_id=scan_id,
                symbol=internal.key,
                reason=RejectionReason.INVALID_MARKET_DATA,
                detail="no valid rows after validation",
            )

        last_row = valid.iloc[-1]
        last_ts = pd.Timestamp(last_row["timestamp"])
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize("UTC")

        # 4. No future data (look-ahead guard).
        if last_ts > pd.Timestamp(scan_ts):
            return None, ScanRejection(
                scan_id=scan_id,
                symbol=internal.key,
                reason=RejectionReason.INVALID_MARKET_DATA,
                detail="market data timestamp is in the future relative to scan time",
            )

        # 5. Freshness.
        age = pd.Timestamp(scan_ts) - last_ts
        if self.config.max_freshness is not None and age > self.config.max_freshness:
            return None, ScanRejection(
                scan_id=scan_id,
                symbol=internal.key,
                reason=RejectionReason.STALE_MARKET_DATA,
                detail=(
                    f"latest bar {age} older than max_freshness "
                    f"{self.config.max_freshness}"
                ),
                market_timestamp=last_ts.isoformat(),
            )

        # 6. Session eligibility (regular session check on the bar timestamp).
        if self.config.require_regular_session and not self._calendar.is_regular_session(
            last_ts.to_pydatetime()
        ):
            return None, ScanRejection(
                scan_id=scan_id,
                symbol=internal.key,
                reason=RejectionReason.OUTSIDE_SESSION,
                detail=(
                    f"latest bar timestamp {last_ts.isoformat()} not in a regular session"
                ),
                market_timestamp=last_ts.isoformat(),
            )

        # 7. Liquidity.
        if self.config.min_liquidity is not None:
            if "volume" not in valid.columns:
                return None, ScanRejection(
                    scan_id=scan_id,
                    symbol=internal.key,
                    reason=RejectionReason.INVALID_MARKET_DATA,
                    detail="volume column required for liquidity gate",
                )
            avg_volume = float(valid["volume"].mean())
            if avg_volume < self.config.min_liquidity:
                return None, ScanRejection(
                    scan_id=scan_id,
                    symbol=internal.key,
                    reason=RejectionReason.INSUFFICIENT_LIQUIDITY,
                    detail=(
                        f"avg_volume {avg_volume} below min_liquidity "
                        f"{self.config.min_liquidity}"
                    ),
                    market_timestamp=last_ts.isoformat(),
                )

        latest_price = float(last_row["close"])
        last_volume = (
            float(last_row["volume"]) if "volume" in valid.columns else None
        )
        avg_volume = float(valid["volume"].mean()) if "volume" in valid.columns else None

        return ScanCandidate(
            scan_id=scan_id,
            symbol=internal.key,
            timeframe=self.config.timeframe,
            scan_timestamp=scan_ts.isoformat(),
            market_timestamp=last_ts.isoformat(),
            latest_price=latest_price,
            volume=last_volume,
            avg_volume=avg_volume,
            freshness_seconds=float((pd.Timestamp(scan_ts) - last_ts).total_seconds()),
            data_points=int(len(valid)),
            eligibility="eligible",
            metadata={
                "exchange": internal.exchange,
                "instrument": internal.symbol,
                "session_phase": self._calendar.phase(last_ts.to_pydatetime()).value,
                # Validated close prices captured at scan time — enables Phase 3
                # (ranker) to reproduce features without re-fetching market data.
                "close_prices": valid["close"].tolist(),
            },
        ), None
