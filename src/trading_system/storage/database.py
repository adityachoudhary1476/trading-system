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
        {"sqlite_autoincrement": True},
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"<OHLCV {self.symbol} {self.timeframe} {self.timestamp}>"


def init_db(engine) -> None:
    Base.metadata.create_all(engine)
    # SQLite-safe forward migration: add columns introduced after Day 1 without
    # dropping existing data. create_all() does NOT add columns to existing tables.
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing = {c["name"] for c in inspector.get_columns("market_data")}
    needed = {"exchange"}
    with engine.begin() as conn:
        for col in needed:
            if col not in existing:
                conn.execute(
                    text(f"ALTER TABLE market_data ADD COLUMN {col} VARCHAR(16)")
                )


class MarketStore:
    """Thin persistence layer over the market_data table."""

    def __init__(self, db_url: str, echo: bool = False) -> None:
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
        return ts

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
            rec = dict(r)
            rec["timestamp"] = ts
            key = (r["symbol"], r["timeframe"], ts, r["provider"], r.get("exchange"))
            if key in seen:
                continue
            seen.add(key)
            norm.append(rec)

        new_count = 0
        with self._Session() as session:
            existing = session.execute(
                select(OHLCVRecord.symbol, OHLCVRecord.timeframe,
                       OHLCVRecord.timestamp, OHLCVRecord.provider, OHLCVRecord.exchange)
            ).all()
            existing_keys = {
                (e.symbol, e.timeframe,
                 self._to_dt(e.timestamp), e.provider, e.exchange) for e in existing
            }
            to_add = [
                OHLCVRecord(**r)
                for r in norm
                if (r["symbol"], r["timeframe"], r["timestamp"], r["provider"], r.get("exchange"))
                not in existing_keys
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
