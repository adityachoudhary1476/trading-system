"""Evidence store + hypothesis/evidence models (Day 10) — research only, no execution.

Reuses the project's SQLAlchemy ``Base``/``Engine`` (MarketStore) — NO second DB
framework. Research evidence is SEPARATE from execution authority: a hypothesis status
is research state, never a trading permission.

Components:
  * HypothesisStatus enum (research lifecycle states; no auto-promotion)
  * Hypothesis (pydantic) + HypothesisRecord (ORM)
  * EvidenceRun (pydantic) + EvidenceRecord (ORM)
  * ExperimentManifest (deterministic hash)
  * ResearchRegistry (clean API for future Hermes tools)
  * evidence-quality + freshness classification
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import pandas as pd
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import (
    Column, String, Integer, Float, Text, DateTime, ForeignKey, Index, select, func,
)
from sqlalchemy.orm import relationship

from ..storage.database import Base


# --------------------------------------------------------------------------- #
# Research lifecycle states (research state, NOT execution permission)
# --------------------------------------------------------------------------- #
class HypothesisStatus(str, Enum):
    HYPOTHESIS = "hypothesis"
    RESEARCH = "research"
    BACKTEST = "backtest"
    HOLDOUT = "holdout"
    PAPER = "paper"
    CANARY = "canary"
    LIVE = "live"
    REVIEW = "review"
    RETIRED = "retired"
    REJECTED = "rejected"


# --------------------------------------------------------------------------- #
# Experiment manifest (deterministic identity)
# --------------------------------------------------------------------------- #
@dataclass
class ExperimentManifest:
    """Deterministic research configuration. `run_metadata` is excluded from the hash."""

    experiment_id: str
    strategy_id: str
    factor_set: list[str]
    dataset: str                # e.g. "NSE:SBIN|1d"
    symbol_universe: list[str]
    timeframe: str
    date_start: Optional[str] = None
    date_end: Optional[str] = None
    warmup_bars: int = 0
    evaluation_start: Optional[str] = None
    backtest_config: dict = field(default_factory=dict)
    transaction_cost_bps: float = 0.0
    slippage_bps: float = 0.0
    code_version: str = ""
    # run_metadata (timestamp, host) is intentionally NOT part of identity_hash.

    def identity_dict(self) -> dict:
        d = {
            "experiment_id": self.experiment_id,
            "strategy_id": self.strategy_id,
            "factor_set": sorted(self.factor_set),
            "dataset": self.dataset,
            "symbol_universe": sorted(self.symbol_universe),
            "timeframe": self.timeframe,
            "date_start": self.date_start,
            "date_end": self.date_end,
            "warmup_bars": self.warmup_bars,
            "evaluation_start": self.evaluation_start,
            "backtest_config": _stable(self.backtest_config),
            "transaction_cost_bps": self.transaction_cost_bps,
            "slippage_bps": self.slippage_bps,
            "code_version": self.code_version,
        }
        return d

    @property
    def identity_hash(self) -> str:
        blob = json.dumps(self.identity_dict(), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _stable(obj):
    return json.loads(json.dumps(obj, sort_keys=True, default=str))


# --------------------------------------------------------------------------- #
# Hypothesis model
# --------------------------------------------------------------------------- #
class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str
    title: str
    description: str
    market: str = "india"
    timeframe: str
    instruments: list[str] = Field(default_factory=list)
    factor_names: list[str] = Field(default_factory=list)
    expected_behavior: str = ""
    status: HypothesisStatus = HypothesisStatus.HYPOTHESIS
    created_at: str = ""
    provenance: str = ""        # how it was created (e.g. "manual", "hermes-<run>")

    def as_record(self) -> "HypothesisRecord":
        return HypothesisRecord(
            hypothesis_id=self.hypothesis_id,
            title=self.title,
            description=self.description,
            market=self.market,
            timeframe=self.timeframe,
            instruments=json.dumps(self.instruments),
            factor_names=json.dumps(self.factor_names),
            expected_behavior=self.expected_behavior,
            status=self.status.value,
            created_at=_parse_dt(self.created_at or _now()),
            provenance=self.provenance,
        )


class HypothesisRecord(Base):
    __tablename__ = "hypotheses"
    hypothesis_id = Column(String(64), primary_key=True)
    title = Column(String(256), nullable=False)
    description = Column(Text, nullable=False)
    market = Column(String(32), nullable=False, default="india")
    timeframe = Column(String(8), nullable=False)
    instruments = Column(Text, nullable=False, default="[]")
    factor_names = Column(Text, nullable=False, default="[]")
    expected_behavior = Column(Text, nullable=False, default="")
    status = Column(String(16), nullable=False, default="hypothesis", index=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    provenance = Column(String(128), nullable=False, default="")


# --------------------------------------------------------------------------- #
# Evidence run model
# --------------------------------------------------------------------------- #
class EvidenceRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    hypothesis_id: str
    manifest_hash: str
    dataset_identity: str
    evaluation_start: Optional[str] = None
    evaluation_end: Optional[str] = None
    trade_count: int = 0
    total_return: float = 0.0
    sharpe: Optional[float] = None
    sortino: Optional[float] = None
    max_drawdown: Optional[float] = None
    profit_factor: Optional[float] = None
    ic: Optional[float] = None
    icir: Optional[float] = None
    cost_assumptions_bps: float = 0.0
    slippage_assumptions_bps: float = 0.0
    regime: str = "unknown"
    sample_size: int = 0
    quality: str = "insufficient"
    created_at: str = ""

    def as_record(self) -> "EvidenceRecord":
        return EvidenceRecord(
            run_id=self.run_id,
            hypothesis_id=self.hypothesis_id,
            manifest_hash=self.manifest_hash,
            dataset_identity=self.dataset_identity,
            evaluation_start=self.evaluation_start,
            evaluation_end=self.evaluation_end,
            trade_count=self.trade_count,
            total_return=self.total_return,
            sharpe=self.sharpe,
            sortino=self.sortino,
            max_drawdown=self.max_drawdown,
            profit_factor=self.profit_factor,
            ic=self.ic,
            icir=self.icir,
            cost_assumptions_bps=self.cost_assumptions_bps,
            slippage_assumptions_bps=self.slippage_assumptions_bps,
            regime=self.regime,
            sample_size=self.sample_size,
            quality=self.quality,
            created_at=_parse_dt(self.created_at or _now()),
        )


class EvidenceRecord(Base):
    __tablename__ = "evidence_runs"
    run_id = Column(String(64), primary_key=True)
    hypothesis_id = Column(String(64), ForeignKey("hypotheses.hypothesis_id"), index=True)
    manifest_hash = Column(String(32), nullable=False, index=True)
    dataset_identity = Column(String(128), nullable=False)
    evaluation_start = Column(String(24), nullable=True)
    evaluation_end = Column(String(24), nullable=True)
    trade_count = Column(Integer, nullable=False, default=0)
    total_return = Column(Float, nullable=False, default=0.0)
    sharpe = Column(Float, nullable=True)
    sortino = Column(Float, nullable=True)
    max_drawdown = Column(Float, nullable=True)
    profit_factor = Column(Float, nullable=True)
    ic = Column(Float, nullable=True)
    icir = Column(Float, nullable=True)
    cost_assumptions_bps = Column(Float, nullable=False, default=0.0)
    slippage_assumptions_bps = Column(Float, nullable=False, default=0.0)
    regime = Column(String(24), nullable=False, default="unknown", index=True)
    sample_size = Column(Integer, nullable=False, default=0)
    quality = Column(String(16), nullable=False, default="insufficient", index=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    hypothesis = relationship("HypothesisRecord", back_populates=None)


# --------------------------------------------------------------------------- #
# Evidence quality + freshness (deterministic, documented thresholds)
# --------------------------------------------------------------------------- #
# Provisional, configurable research policy (Day 10 placeholders — flagged as such).
MIN_TRADES_ADEQUATE = 30          # provisional: need >=30 trades for a stable read
MIN_TRADES_MARGINAL = 10
STALE_DAYS = 180                  # provisional: evidence older than this needs revalidation


def classify_quality(
    trade_count: int,
    has_oos: bool,
    missing_metrics: list[str],
    cost_assumptions_bps: float,
    regime: str = "unknown",
) -> str:
    """Classify evidence quality. Thresholds are provisional/configurable.

    INSUFFICIENT: too few trades, or no cost assumption at all (unrealistic).
    MARGINAL: meets minimal trade count but lacks OOS or has missing metrics.
    ADEQUATE: enough trades + OOS availability + cost assumption present.
    """
    if trade_count < MIN_TRADES_MARGINAL or cost_assumptions_bps <= 0:
        return "insufficient"
    if trade_count < MIN_TRADES_ADEQUATE or not has_oos or missing_metrics:
        return "marginal"
    return "adequate"


def is_evidence_stale(created_at: str, stale_days: int = STALE_DAYS) -> bool:
    try:
        ts = _parse_dt(created_at)
    except (ValueError, TypeError):
        return True
    age = (datetime.now(timezone.utc) - ts).days
    return age > stale_days


# --------------------------------------------------------------------------- #
# SQLite evidence store (same engine as MarketStore)
# --------------------------------------------------------------------------- #
class EvidenceStore:
    """Local SQLite evidence store. Reuses MarketStore's SQLAlchemy engine/Base."""

    def __init__(self, engine) -> None:
        self.engine = engine
        Base.metadata.create_all(engine)  # idempotent; adds hypotheses/evidence_runs
        from sqlalchemy.orm import sessionmaker
        self._Session = sessionmaker(bind=engine, future=True)

    # --- hypotheses ---
    def create_hypothesis(self, h: Hypothesis) -> Hypothesis:
        rec = h.as_record()
        with self._Session() as s:
            existing = s.get(HypothesisRecord, h.hypothesis_id)
            if existing:
                raise ValueError(f"hypothesis {h.hypothesis_id} already exists")
            s.add(rec)
            s.commit()
        return h

    def get_hypothesis(self, hypothesis_id: str) -> Optional[Hypothesis]:
        with self._Session() as s:
            rec = s.get(HypothesisRecord, hypothesis_id)
            return _rec_to_hypothesis(rec) if rec else None

    def update_status(self, hypothesis_id: str, status: HypothesisStatus) -> None:
        with self._Session() as s:
            rec = s.get(HypothesisRecord, hypothesis_id)
            if rec is None:
                raise KeyError(hypothesis_id)
            rec.status = status.value
            s.commit()

    def list_hypotheses(self, status: Optional[str] = None,
                        regime: Optional[str] = None) -> list[Hypothesis]:
        # regime filter applies via joined evidence; simple status filter here.
        with self._Session() as s:
            q = select(HypothesisRecord)
            if status:
                q = q.where(HypothesisRecord.status == status)
            recs = s.execute(q).scalars().all()
        return [_rec_to_hypothesis(r) for r in recs]

    # --- evidence ---
    def record_evidence(self, e: EvidenceRun) -> EvidenceRun:
        rec = e.as_record()
        with self._Session() as s:
            s.add(rec)
            s.commit()
        return e

    def get_evidence(self, run_id: str) -> Optional[EvidenceRun]:
        with self._Session() as s:
            rec = s.get(EvidenceRecord, run_id)
            return _rec_to_evidence(rec) if rec else None

    def list_evidence(self, hypothesis_id: Optional[str] = None,
                     status: Optional[str] = None, regime: Optional[str] = None,
                     quality: Optional[str] = None) -> list[EvidenceRun]:
        with self._Session() as s:
            q = select(EvidenceRecord)
            if hypothesis_id:
                q = q.where(EvidenceRecord.hypothesis_id == hypothesis_id)
            if regime:
                q = q.where(EvidenceRecord.regime == regime)
            if quality:
                q = q.where(EvidenceRecord.quality == quality)
            recs = s.execute(q.order_by(EvidenceRecord.created_at.desc())).scalars().all()
        return [_rec_to_evidence(r) for r in recs]

    def get_latest_evidence(self, hypothesis_id: str) -> Optional[EvidenceRun]:
        evs = self.list_evidence(hypothesis_id=hypothesis_id)
        return evs[0] if evs else None

    def compare_hypotheses(self, hypothesis_ids: list[str]) -> pd.DataFrame:
        rows = []
        for hid in hypothesis_ids:
            ev = self.get_latest_evidence(hid)
            h = self.get_hypothesis(hid)
            rows.append({
                "hypothesis_id": hid,
                "title": h.title if h else "",
                "status": h.status.value if h else "",
                "sharpe": ev.sharpe if ev else None,
                "max_dd": ev.max_drawdown if ev else None,
                "ic": ev.ic if ev else None,
                "icir": ev.icir if ev else None,
                "quality": ev.quality if ev else "",
            })
        return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Research registry (clean API for future Hermes tools — not SQLite internals)
# --------------------------------------------------------------------------- #
class ResearchRegistry:
    """High-level facade over EvidenceStore + manifest hashing. Deterministic."""

    def __init__(self, store: EvidenceStore) -> None:
        self.store = store

    def create_hypothesis(self, h: Hypothesis) -> Hypothesis:
        return self.store.create_hypothesis(h)

    def record_evidence(self, e: EvidenceRun) -> EvidenceRun:
        return self.store.record_evidence(e)

    def get_hypothesis(self, hid: str) -> Optional[Hypothesis]:
        return self.store.get_hypothesis(hid)

    def get_evidence(self, run_id: str) -> Optional[EvidenceRun]:
        return self.store.get_evidence(run_id)

    def list_hypotheses(self, status: Optional[str] = None) -> list[Hypothesis]:
        return self.store.list_hypotheses(status=status)

    def get_latest_evidence(self, hid: str) -> Optional[EvidenceRun]:
        return self.store.get_latest_evidence(hid)

    def compare_hypotheses(self, ids: list[str]) -> pd.DataFrame:
        return self.store.compare_hypotheses(ids)

    def is_evidence_stale(self, e: EvidenceRun, stale_days: int = STALE_DAYS) -> bool:
        return is_evidence_stale(e.created_at, stale_days)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value) -> datetime:
    """Accept datetime or ISO string; always return a tz-aware UTC datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    raise ValueError(f"cannot parse datetime from {value!r}")


def _rec_to_hypothesis(rec: HypothesisRecord) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=rec.hypothesis_id, title=rec.title, description=rec.description,
        market=rec.market, timeframe=rec.timeframe,
        instruments=json.loads(rec.instruments or "[]"),
        factor_names=json.loads(rec.factor_names or "[]"),
        expected_behavior=rec.expected_behavior, status=HypothesisStatus(rec.status),
        created_at=rec.created_at.isoformat() if rec.created_at else "", provenance=rec.provenance,
    )


def _rec_to_evidence(rec: EvidenceRecord) -> EvidenceRun:
    return EvidenceRun(
        run_id=rec.run_id, hypothesis_id=rec.hypothesis_id, manifest_hash=rec.manifest_hash,
        dataset_identity=rec.dataset_identity, evaluation_start=rec.evaluation_start,
        evaluation_end=rec.evaluation_end, trade_count=rec.trade_count,
        total_return=rec.total_return, sharpe=rec.sharpe, sortino=rec.sortino,
        max_drawdown=rec.max_drawdown, profit_factor=rec.profit_factor, ic=rec.ic, icir=rec.icir,
        cost_assumptions_bps=rec.cost_assumptions_bps, slippage_assumptions_bps=rec.slippage_assumptions_bps,
        regime=rec.regime, sample_size=rec.sample_size, quality=rec.quality,
        created_at=rec.created_at.isoformat() if rec.created_at else "",
    )
