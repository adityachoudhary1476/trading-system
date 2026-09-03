"""Backend read model for authoritative live candle state."""
from __future__ import annotations

from datetime import timezone
from threading import RLock
from typing import Optional

from src.trading_system.india.closed_candle_pipeline import ClosedCandle
from src.trading_system.india.live_market_state import LiveMarketSnapshot


class LiveCandleReadModel:
    """Thread-safe projection of closed and current candles for API readers."""

    def __init__(self, timeframe: str) -> None:
        self.timeframe = timeframe
        self._closed: dict[tuple[str, str, int], dict] = {}
        self._snapshots: dict[str, LiveMarketSnapshot] = {}
        self._versions: dict[tuple[str, str], int] = {}
        self._lock = RLock()

    def on_snapshot(self, snapshot: LiveMarketSnapshot) -> None:
        with self._lock:
            self._snapshots[snapshot.symbol] = snapshot
            pair = (snapshot.symbol, self.timeframe)
            self._versions[pair] = self._versions.get(pair, 0) + 1

    def on_closed_candle(self, candle: ClosedCandle) -> None:
        timestamp_ms = int(candle.start.astimezone(timezone.utc).timestamp() * 1000)
        key = (candle.symbol, candle.timeframe, timestamp_ms)
        value = {
            "time": timestamp_ms,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
            "is_closed": True,
            "source": "upstox",
        }
        with self._lock:
            self._closed[key] = value
            pair = (candle.symbol, candle.timeframe)
            self._versions[pair] = self._versions.get(pair, 0) + 1

    def read(self, symbol: str, timeframe: str, pipeline, limit: int = 160) -> Optional[dict]:
        pair = (symbol, timeframe)
        with self._lock:
            candles = [
                dict(value)
                for (s, tf, _), value in self._closed.items()
                if s == symbol and tf == timeframe
            ]
            snapshot = self._snapshots.get(symbol)
            version = self._versions.get(pair, 0)

        candles.sort(key=lambda value: value["time"])
        current = None
        candle_pipeline = getattr(pipeline, "candle_pipeline", pipeline)
        if candle_pipeline is not None and getattr(candle_pipeline, "timeframe", None) == timeframe:
            provisional = candle_pipeline.provisional_candle(symbol)
            if provisional is not None:
                current = {
                    "time": int(provisional.start.astimezone(timezone.utc).timestamp() * 1000),
                    "open": provisional.open,
                    "high": provisional.high,
                    "low": provisional.low,
                    "close": provisional.close,
                    "volume": provisional.volume,
                    "is_closed": False,
                    "source": "upstox",
                }

        if current is not None:
            candles = [candle for candle in candles if candle["time"] != current["time"]]
        candles = candles[-limit:]
        if current is not None:
            candles.append(current)

        if not candles and snapshot is None:
            return None
        latest_market_timestamp = snapshot.market_timestamp if snapshot else None
        fetched_at = snapshot.fetched_at if snapshot else None
        freshness = snapshot.freshness_ms if snapshot else None
        session = snapshot.session if snapshot else "UNKNOWN"
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "candles": candles,
            "current_candle": current,
            "market_timestamp": latest_market_timestamp,
            "fetched_at": fetched_at,
            "freshness_ms": freshness,
            "session": session,
            "version": version,
        }

    def clear(self) -> None:
        with self._lock:
            self._closed.clear()
            self._snapshots.clear()
            self._versions.clear()

    def latest_closed_timestamp(self, symbol: str, timeframe: str) -> Optional[int]:
        with self._lock:
            times = [
                timestamp for (s, tf, timestamp) in self._closed
                if s == symbol and tf == timeframe
            ]
        return max(times) if times else None

    def latest_snapshot(self, symbol: str) -> Optional[LiveMarketSnapshot]:
        with self._lock:
            return self._snapshots.get(symbol)

    def seed_closed_candle(self, candle: ClosedCandle) -> None:
        """Seed a closed provider candle without emitting a duplicate event."""
        self.on_closed_candle(candle)

    def seed_historical_df(self, symbol: str, timeframe: str, frame) -> None:
        for timestamp, row in frame.iterrows():
            self.seed_closed_candle(ClosedCandle(
                symbol=symbol,
                timeframe=timeframe,
                start=timestamp.to_pydatetime() if hasattr(timestamp, "to_pydatetime") else timestamp,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 0) or 0),
            ))
