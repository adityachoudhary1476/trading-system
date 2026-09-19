"""Phase 1 — Option-chain snapshot persistence models (v5 schema migration).

These ORM models follow the same ``Base`` / ``Engine`` convention used by
``PaperDeploymentRecord`` (see ``src/trading_system/paper/deployment.py``).
They are imported lazily inside ``ensure_schema_current`` so that
``research/evidence.py`` can create the tables via ``Base.metadata.create_all``
without a circular import (the models only depend on ``storage.database``).

Tables:
  * ``option_chain_snapshots`` — one row per fetched chain (raw JSON preserved)
  * ``option_chain_rows``      — one row per strike/type (normalized)
"""
from __future__ import annotations

from sqlalchemy import Column, Float, Integer, String, Text, Index, DateTime

from ..storage.database import Base


class OptionChainSnapshotRecord(Base):
    """Immutable snapshot of an Upstox option-chain fetch.

    ``raw_json`` is the exact provider response (minus auth token) so that
    downstream analytics (Phase 2+ GREEKS / OI / IV) can be reproduced from
    the original market-data snapshot — never from a re-derived copy.
    """

    __tablename__ = "option_chain_snapshots"

    snapshot_id = Column(String(64), primary_key=True)  # UUID4 hex
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    provider = Column(String(32), nullable=False, default="upstox")
    underlying = Column(String(32), nullable=False, index=True)
    expiry = Column(String(10), nullable=False)
    raw_json = Column(Text, nullable=False)
    validation_status = Column(String(16), nullable=False, default="validated")
    row_count = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("idx_chain_snapshot_underlying", "underlying"),
    )


class OptionChainRowRecord(Base):
    """Normalized per-strike/option-type row, linked to a snapshot.

    Each chain snapshot contributes 2 × N rows (N = strike count): one for
    the call leg, one for the put leg.
    """

    __tablename__ = "option_chain_rows"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(String(64), nullable=False, index=True)
    instrument_key = Column(String(64), nullable=True)
    underlying = Column(String(32), nullable=False)
    expiry = Column(String(10), nullable=False)
    strike = Column(Float, nullable=False)
    option_type = Column(String(4), nullable=False)  # "CE" or "PE"
    ltp = Column(Float, nullable=True)
    bid = Column(Float, nullable=True)
    ask = Column(Float, nullable=True)
    volume = Column(Integer, nullable=True)
    oi = Column(Integer, nullable=True)
    change_oi = Column(Integer, nullable=True)
    bid_iv = Column(Float, nullable=True)
    ask_iv = Column(Float, nullable=True)

    __table_args__ = (
        Index(
            "idx_chain_row_underlying_expiry_strike_type",
            "underlying", "expiry", "strike", "option_type",
        ),
    )


# --------------------------------------------------------------------------- #
# Row-level persistence helper
# --------------------------------------------------------------------------- #
def persist_snapshot(
    session,
    *,
    snapshot_id: str,
    timestamp,
    provider: str,
    underlying: str,
    expiry: str,
    raw_json: str,
    validation_status: str,
    rows: list,
) -> int:
    """Persist a validated snapshot + its normalized rows in a single tx.

    ``rows`` is a list of ``NormalizedOptionRow`` dataclass instances.
    Returns the number of row records written.
    """
    snapshot = OptionChainSnapshotRecord(
        snapshot_id=snapshot_id,
        timestamp=timestamp,
        provider=provider,
        underlying=underlying,
        expiry=expiry,
        raw_json=raw_json,
        validation_status=validation_status,
        row_count=len(rows),
    )
    session.add(snapshot)

    for row in rows:
        session.add(OptionChainRowRecord(
            snapshot_id=snapshot_id,
            instrument_key=row.instrument_key,
            underlying=row.underlying,
            expiry=row.expiry,
            strike=row.strike,
            option_type=row.option_type,
            ltp=row.ltp,
            bid=row.bid,
            ask=row.ask,
            volume=row.volume,
            oi=row.oi,
            change_oi=row.change_oi,
            bid_iv=row.bid_iv,
            ask_iv=row.ask_iv,
        ))

    return len(rows)
