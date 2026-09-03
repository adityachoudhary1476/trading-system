"""SQLite-backed storage for market data using SQLAlchemy Core.

Design goals:
  * Idempotent insertion: re-running ingestion for the same (symbol, timeframe,
    timestamp, provider) never creates a duplicate row. We enforce a UNIQUE
    constraint and use INSERT ... ON CONFLICT (upsert) so repeats are no-ops.
  * Minimal schema: the seven required fields plus an integer primary key,
    indexes on symbol+timestamp for fast range queries.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Iterable

import pandas as pd

from sqlalchemy import (
    Column,
    Float,
    Integer,
    String,
    DateTime,
    UniqueConstraint,
    Index,
    create_engine,
    select,
)
from sqlalchemy.orm import sessionmaker, declarative_base

Base = declarative_base()


class OHLCVRecord(Base):
    __tablename__ = "market_data"
    __table_args__ = (
        UniqueConstraint(
            "symbol", "timeframe", "timestamp", "provider",
            name="uq_market_data_symbol_tf_ts_provider",
        ),
        {"sqlite_autoincrement": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(32), nullable=False, index=True)
    timeframe = Column(String(8), nullable=False, index=True)
    exchange = Column(String(16), nullable=True, index=True)
    # Canonical contract identity (EXCHANGE:UNDERLYING|EXPIRY|STRIKE|CE/PE/FUT) for
    # derivatives; equals "exchange:symbol" for equity/index. Lets us guarantee two
    # contracts with different expiries/strikes/option-types NEVER collide even if a
    # provider ever returned an ambiguous symbol. Backfilled from symbol by default.
    contract_id = Column(String(64), nullable=True, index=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)
    provider = Column(String(64), nullable=False, index=True)

    # Provider + exchange distinguish Binance vs FYERS vs future sources; together
    # with (symbol, timeframe, timestamp) this makes duplicate candles impossible.
    __table_args__ = (
        UniqueConstraint(
            "symbol", "timeframe", "timestamp", "provider", "exchange",
            name="uq_market_data_symbol_tf_ts_provider_exch",
        ),
        Index(
            "ix_ohlcv_unique_lookup",
            "symbol", "timeframe", "timestamp", "provider", "exchange",
        ),
        Index(
            "ix_ohlcv_contract_lookup",
            "contract_id", "timeframe", "timestamp", "provider", "exchange",
        ),
        {"sqlite_autoincrement": True},
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"<OHLCV {self.symbol} {self.timeframe} {self.timestamp}>"


class RecoveryPointRecord(Base):
    __tablename__ = "market_recovery_points"
    __table_args__ = ({"sqlite_autoincrement": True},)

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(32), nullable=False, index=True)
    timeframe = Column(String(8), nullable=False, index=True)
    latest_closed_candle = Column(DateTime(timezone=True), nullable=True)
    last_recovery_at = Column(DateTime(timezone=True), nullable=True)
    last_live_timestamp = Column(DateTime(timezone=True), nullable=True)
    recovery_status = Column(String(32), nullable=False, default="unknown")
    recovery_error = Column(String(512), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", name="uq_market_recovery_symbol_tf"),
        {"sqlite_autoincrement": True},
    )


def init_db(engine) -> None:
    Base.metadata.create_all(engine)
    # SQLite-safe forward migration: add columns introduced after Day 1 without
    # dropping existing data. create_all() does NOT add columns to existing tables.
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing = {c["name"] for c in inspector.get_columns("market_data")}
    needed = {"exchange", "contract_id"}
    with engine.begin() as conn:
        for col in needed:
            if col not in existing:
                conn.execute(
                    text(f"ALTER TABLE market_data ADD COLUMN {col} VARCHAR(64)")
                )


class MarketStore:
    """Thin persistence layer over the market_data table."""

    def __init__(self, db_url: str, echo: bool = False) -> None:
        if db_url.startswith("sqlite:///") and not db_url.endswith(":memory:"):
            Path(db_url[10:]).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(db_url, echo=echo, future=True)
        self._Session = sessionmaker(bind=self.engine, future=True)
        init_db(self.engine)

    # -- writes --------------------------------------------------------------
    @staticmethod
    def _to_dt(ts):
        """Normalize a timestamp to a tz-aware python datetime (UTC).

        Critical: pd.Timestamp and datetime hash differently, so the dedup set
        must use a single consistent type or idempotency silently breaks.
        """
        if isinstance(ts, pd.Timestamp):
            ts = ts.to_pydatetime()
        if isinstance(ts, dt.datetime) and ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        if isinstance(ts, dt.datetime):
            ts = ts.astimezone(dt.timezone.utc)
        return ts

    def get_recovery_point(self, symbol: str, timeframe: str) -> dict | None:
        with self._Session() as session:
            row = session.execute(
                select(RecoveryPointRecord)
                .where(RecoveryPointRecord.symbol == symbol)
                .where(RecoveryPointRecord.timeframe == timeframe)
            ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "symbol": row.symbol,
            "timeframe": row.timeframe,
            "latest_closed_candle": self._to_dt(row.latest_closed_candle),
            "last_recovery_at": self._to_dt(row.last_recovery_at) if row.last_recovery_at else None,
            "last_live_timestamp": self._to_dt(row.last_live_timestamp) if row.last_live_timestamp else None,
            "recovery_status": row.recovery_status,
            "recovery_error": row.recovery_error,
            "updated_at": self._to_dt(row.updated_at),
        }

    def record_recovery_status(
        self, symbol: str, timeframe: str, status: str, error: str | None = None
    ) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        with self._Session.begin() as session:
            row = session.execute(
                select(RecoveryPointRecord)
                .where(RecoveryPointRecord.symbol == symbol)
                .where(RecoveryPointRecord.timeframe == timeframe)
            ).scalar_one_or_none()
            if row is None:
                session.add(RecoveryPointRecord(
                    symbol=symbol, timeframe=timeframe,
                    recovery_status=status, recovery_error=error, updated_at=now,
                ))
            else:
                row.recovery_status = status
                row.recovery_error = error
                row.updated_at = now

    def commit_recovery(
        self,
        symbol: str,
        timeframe: str,
        rows: list[dict],
        latest_closed_candle,
        last_live_timestamp=None,
    ) -> int:
        """Atomically persist authoritative candles and advance the cursor."""
        now = dt.datetime.now(dt.timezone.utc)
        normalized = []
        for row in rows:
            record = dict(row)
            record["symbol"] = symbol
            record["timeframe"] = timeframe
            record["timestamp"] = self._to_dt(record["timestamp"])
            record["contract_id"] = record.get("contract_id") or symbol
            normalized.append(record)

        inserted = 0
        with self._Session.begin() as session:
            for record in normalized:
                existing = session.execute(
                    select(OHLCVRecord)
                    .where(OHLCVRecord.symbol == symbol)
                    .where(OHLCVRecord.timeframe == timeframe)
                    .where(OHLCVRecord.timestamp == record["timestamp"])
                ).scalars().first()
                if existing is None:
                    session.add(OHLCVRecord(**record))
                    inserted += 1
                else:
                    # Historical provider data is authoritative for completed bars.
                    for key in ("exchange", "contract_id", "open", "high", "low", "close", "volume", "provider"):
                        setattr(existing, key, record.get(key))

            point = session.execute(
                select(RecoveryPointRecord)
                .where(RecoveryPointRecord.symbol == symbol)
                .where(RecoveryPointRecord.timeframe == timeframe)
            ).scalar_one_or_none()
            latest = self._to_dt(latest_closed_candle) if latest_closed_candle else None
            live_ts = self._to_dt(last_live_timestamp) if last_live_timestamp else None
            if point is None:
                session.add(RecoveryPointRecord(
                    symbol=symbol, timeframe=timeframe,
                    latest_closed_candle=latest, last_recovery_at=now,
                    last_live_timestamp=live_ts, recovery_status="complete",
                    recovery_error=None, updated_at=now,
                ))
            else:
                stored_latest = self._to_dt(point.latest_closed_candle) if point.latest_closed_candle else None
                if latest is not None and (stored_latest is None or latest > stored_latest):
                    point.latest_closed_candle = latest
                point.last_recovery_at = now
                point.last_live_timestamp = live_ts or point.last_live_timestamp
                point.recovery_status = "complete"
                point.recovery_error = None
                point.updated_at = now
        return inserted

    def upsert_many(self, rows: list[dict]) -> int:
        """Idempotently insert rows. Returns number of NEW rows inserted."""
        if not rows:
            return 0
        # Normalize timestamps to tz-aware UTC datetimes for consistent storage
        # AND consistent dedup hashing.
        norm = []
        seen: set[tuple] = set()
        for r in rows:
            ts = self._to_dt(r["timestamp"])
            # Contract identity guards against expiry/strike collisions for F&O.
            contract = r.get("contract_id") or r.get("symbol")
            rec = dict(r)
            rec["timestamp"] = ts
            rec["contract_id"] = contract
            key = (
                r["symbol"], r["timeframe"], ts, r["provider"],
                r.get("exchange"), contract,
            )
            if key in seen:
                continue
            seen.add(key)
            norm.append(rec)

        new_count = 0
        with self._Session() as session:
            existing = session.execute(
                select(
                    OHLCVRecord.symbol, OHLCVRecord.timeframe,
                    OHLCVRecord.timestamp, OHLCVRecord.provider,
                    OHLCVRecord.exchange, OHLCVRecord.contract_id,
                )
            ).all()
            existing_keys = {
                (
                    e.symbol, e.timeframe,
                    self._to_dt(e.timestamp), e.provider, e.exchange,
                    e.contract_id or e.symbol,
                ) for e in existing
            }
            to_add = [
                OHLCVRecord(**r)
                for r in norm
                if (
                    r["symbol"], r["timeframe"], r["timestamp"], r["provider"],
                    r.get("exchange"), r.get("contract_id") or r.get("symbol"),
                ) not in existing_keys
            ]
            if to_add:
                session.add_all(to_add)
                session.commit()
                new_count = len(to_add)
        return new_count

    def count(self, symbol: str | None = None, timeframe: str | None = None) -> int:
        from sqlalchemy import func

        stmt = select(func.count()).select_from(OHLCVRecord)
        if symbol:
            stmt = stmt.where(OHLCVRecord.symbol == symbol)
        if timeframe:
            stmt = stmt.where(OHLCVRecord.timeframe == timeframe)
        with self._Session() as session:
            return int(session.execute(stmt).scalar_one())

    def load(self, symbol: str, timeframe: str) -> "pd.DataFrame":
        """Load a symbol/timeframe as an OHLCV DataFrame indexed by timestamp."""
        from sqlalchemy import desc
        import pandas as pd

        stmt = (
            select(OHLCVRecord)
            .where(OHLCVRecord.symbol == symbol)
            .where(OHLCVRecord.timeframe == timeframe)
            .order_by(OHLCVRecord.timestamp)
        )
        with self._Session() as session:
            rows = session.execute(stmt).scalars().all()
        if not rows:
            return pd.DataFrame()
        data = [
            {
                "timestamp": r.timestamp,
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "volume": r.volume,
                "symbol": r.symbol,
                "timeframe": r.timeframe,
                "provider": r.provider,
            }
            for r in rows
        ]
        df = pd.DataFrame(data).set_index("timestamp").sort_index()
        # SQLite does not preserve tz on DateTime columns; we always store UTC,
        # so re-localize on read to keep timestamps tz-aware and consistent.
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        return df
