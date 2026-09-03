"""Bounded historical recovery for the persistent market runtime."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

import pandas as pd

from src.trading_system.india.closed_candle_pipeline import ClosedCandle, CandleState
from src.trading_system.india.market_calendar import DEFAULT_CALENDAR, SessionPhase
from src.trading_system.data.validation import validate_ohlcv


class RecoveryState(str, Enum):
    HEALTHY = "healthy"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"
    RECOVERING = "recovering"
    DEGRADED = "degraded"


class MarketRecovery:
    """Recover closed candles using the existing provider and candle engine."""

    def __init__(self, provider, pipeline, read_model, store=None) -> None:
        self.provider = provider
        self.pipeline = pipeline
        self.read_model = read_model
        self.store = store
        self.state = RecoveryState.DISCONNECTED
        self.last_error: Optional[str] = None
        self.last_recovered_at: Optional[int] = None
        self.recovery_count = 0

    def recover(self, symbols: list[str], *, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        phase = DEFAULT_CALENDAR.phase(now)
        if phase not in {SessionPhase.PRE_MARKET, SessionPhase.REGULAR, SessionPhase.POST_MARKET}:
            self.state = RecoveryState.HEALTHY
            self.last_error = None
            return True

        self.state = RecoveryState.RECOVERING
        self.last_error = None
        self.pipeline.begin_recovery()
        success = True
        try:
            for symbol in symbols:
                if not self._recover_symbol(symbol, now):
                    success = False
        finally:
            self.pipeline.end_recovery()

        self.recovery_count += 1
        if success:
            self.state = RecoveryState.HEALTHY
            self.last_recovered_at = int(now.timestamp() * 1000)
        else:
            self.state = RecoveryState.DEGRADED
        return success

    def _recover_symbol(self, symbol: str, now: datetime) -> bool:
        timeframe = self.pipeline.timeframe
        durable = self.store.get_recovery_point(symbol, timeframe) if self.store else None
        durable_latest = durable.get("latest_closed_candle") if durable else None
        latest = self.read_model.latest_closed_timestamp(symbol, timeframe)
        if durable_latest is not None:
            durable_ms = int(durable_latest.timestamp() * 1000)
            latest = max(latest or 0, durable_ms)
        start = None
        if latest is not None:
            start = pd.Timestamp(latest, unit="ms", tz="UTC")
        try:
            frame = self.provider.get_historical(
                symbol, timeframe, limit=160, start=start, end=pd.Timestamp(now),
            )
            if frame is None or len(frame) == 0:
                return True
            report = validate_ohlcv(frame, timeframe)
            if not report.ok:
                self.last_error = f"invalid recovery data for {symbol}"
                self._record_failure(symbol, timeframe)
                return False
            frame = report.valid.copy()
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
            frame = frame.set_index("timestamp").sort_index()
            frame = self._completed_session_bars(frame, timeframe, now)
            rows = []
            for timestamp, row in frame.iterrows():
                rows.append({
                    "timestamp": timestamp.to_pydatetime(),
                    "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"]),
                    "volume": float(row.get("volume", 0) or 0),
                    "exchange": symbol.split(":", 1)[0] if ":" in symbol else None,
                    "provider": "upstox",
                })
            latest_closed = frame.index[-1].to_pydatetime() if not frame.empty else None
            if self.store and latest_closed is not None:
                live_snapshot = self.read_model.latest_snapshot(symbol)
                self.store.commit_recovery(
                    symbol, timeframe, rows, latest_closed,
                    last_live_timestamp=(live_snapshot.market_timestamp if live_snapshot else None),
                )
            self.pipeline.seed_historical_df(symbol, frame)
            for timestamp, row in frame.iterrows():
                self.read_model.seed_closed_candle(ClosedCandle(
                    symbol=symbol,
                    timeframe=timeframe,
                    start=timestamp.to_pydatetime(),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0) or 0),
                    state=CandleState.CLOSED,
                ))
            return True
        except Exception as exc:
            self.last_error = f"recovery failed for {symbol}: {exc}"
            self._record_failure(symbol, timeframe)
            return False

    def _record_failure(self, symbol: str, timeframe: str) -> None:
        if not self.store:
            return
        try:
            self.store.record_recovery_status(symbol, timeframe, "degraded", self.last_error)
        except Exception:
            pass

    @staticmethod
    def _completed_session_bars(frame: pd.DataFrame, timeframe: str, now: datetime) -> pd.DataFrame:
        if frame.empty:
            return frame
        current = pd.Timestamp(now).tz_convert("UTC")
        current_local = current.tz_convert("Asia/Kolkata")
        current_phase = DEFAULT_CALENDAR.phase(now)
        daily = timeframe in {"1d", "1w", "1M"}
        keep = []
        for timestamp in frame.index:
            ts = pd.Timestamp(timestamp)
            local = ts.tz_convert("Asia/Kolkata")
            if daily:
                valid_day = local.weekday() < 5 and local.date() not in DEFAULT_CALENDAR.holidays
                day_is_complete = (
                    local.date() <= current_local.date()
                    if current_phase == SessionPhase.POST_MARKET
                    else local.date() < current_local.date()
                )
                keep.append(valid_day and day_is_complete)
            else:
                keep.append(DEFAULT_CALENDAR.is_regular_session(ts.to_pydatetime()) and ts < current)
        return frame.loc[keep]