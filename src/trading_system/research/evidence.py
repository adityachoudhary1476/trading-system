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


class StrategyStatus(str, Enum):
    """Phase 16 strategy lifecycle (research state, NOT execution permission)."""

    PROPOSED = "proposed"
    VALIDATED = "validated"
    RESEARCHED = "researched"
    WALK_FORWARD_VALIDATED = "walk_forward_validated"
    REJECTED = "rejected"
    RETIRED = "retired"


class EvidenceType(str, Enum):
    """Phase 16 evidence classification (Phase 18 adds PAPER_TRADING)."""

    BACKTEST = "backtest"
    RESEARCH = "research"
    WALK_FORWARD = "walk_forward"
    AI_RESEARCH = "ai_research"
    HOLDOUT = "holdout"
    PAPER_TRADING = "paper_trading"


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
# Phase 16 — deterministic strategy + dataset identity
# --------------------------------------------------------------------------- #
def strategy_identity(spec: "StrategySpec") -> str:
    """Deterministic strategy identity from canonical StrategySpec JSON.

    Same spec -> same identity. Independent of dict ordering, whitespace, or
    runtime object identity.
    """
    from trading_system.research.strategy_lab.spec import StrategySpec
    if not isinstance(spec, StrategySpec):
        raise TypeError("spec must be a StrategySpec")
    blob = json.dumps(spec.model_dump(mode="json"), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:64]


def dataset_identity(dataset: "HistoricalDataset") -> str:
    """Deterministic dataset identity from metadata (not raw OHLCV hash)."""
    from trading_system.research.dataset import HistoricalDataset
    if not isinstance(dataset, HistoricalDataset):
        raise TypeError("dataset must be a HistoricalDataset")
    data = dataset.data if dataset.data is not None else pd.DataFrame()
    payload = {
        "symbol": dataset.symbol,
        "timeframe": dataset.timeframe,
        "contract_id": dataset.contract_id or "",
        "rows": int(len(data)),
        "date_start": str(data.index.min()) if len(data) else "",
        "date_end": str(data.index.max()) if len(data) else "",
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:64]


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
# Strategy registry models (Phase 16)
# --------------------------------------------------------------------------- #
class Strategy(BaseModel):
    """Persisted strategy identity derived from a validated StrategySpec."""

    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    name: str
    symbol: str
    timeframe: str
    spec_json: str
    spec_hash: str
    status: StrategyStatus = StrategyStatus.PROPOSED
    generated_by: str = ""
    description: str = ""
    parent_strategy_id: str = ""
    generation_metadata: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def as_record(self) -> "StrategyRecord":
        return StrategyRecord(
            strategy_id=self.strategy_id,
            name=self.name,
            symbol=self.symbol,
            timeframe=self.timeframe,
            spec_json=self.spec_json,
            spec_hash=self.spec_hash,
            status=self.status.value,
            generated_by=self.generated_by,
            description=self.description,
            parent_strategy_id=self.parent_strategy_id or None,
            generation_metadata=json.dumps(self.generation_metadata),
            created_at=_parse_dt(self.created_at or _now()),
            updated_at=_parse_dt(self.updated_at or _now()),
        )


class StrategyRecord(Base):
    __tablename__ = "strategies"
    strategy_id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False)
    symbol = Column(String(32), nullable=False, index=True)
    timeframe = Column(String(8), nullable=False)
    spec_json = Column(Text, nullable=False)
    spec_hash = Column(String(64), nullable=False, index=True)
    status = Column(String(24), nullable=False, default="proposed", index=True)
    generated_by = Column(String(64), nullable=False, default="")
    description = Column(Text, nullable=False, default="")
    parent_strategy_id = Column(String(64), nullable=True, index=True)
    generation_metadata = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class StrategyEvidence(BaseModel):
    """Immutable research observation about a strategy."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    strategy_id: str
    strategy_spec_hash: str
    evidence_type: EvidenceType
    dataset_id: str
    configuration_json: dict = field(default_factory=dict)
    metrics_json: dict = field(default_factory=dict)
    report_json: dict = field(default_factory=dict)
    provenance_json: dict = field(default_factory=dict)
    fold_id: Optional[int] = None
    train_start: Optional[str] = None
    train_end: Optional[str] = None
    validation_start: Optional[str] = None
    validation_end: Optional[str] = None
    created_at: str = ""

    def as_record(self) -> "StrategyEvidenceRecord":
        return StrategyEvidenceRecord(
            evidence_id=self.evidence_id,
            strategy_id=self.strategy_id,
            strategy_spec_hash=self.strategy_spec_hash,
            evidence_type=self.evidence_type.value,
            dataset_id=self.dataset_id,
            configuration_json=json.dumps(self.configuration_json),
            metrics_json=json.dumps(self.metrics_json),
            report_json=json.dumps(self.report_json),
            provenance_json=json.dumps(self.provenance_json),
            fold_id=self.fold_id,
            train_start=self.train_start,
            train_end=self.train_end,
            validation_start=self.validation_start,
            validation_end=self.validation_end,
            created_at=_parse_dt(self.created_at or _now()),
        )


class StrategyEvidenceRecord(Base):
    __tablename__ = "strategy_evidence"
    evidence_id = Column(String(64), primary_key=True)
    strategy_id = Column(String(64), ForeignKey("strategies.strategy_id"), index=True)
    strategy_spec_hash = Column(String(64), nullable=False, index=True)
    evidence_type = Column(String(24), nullable=False, index=True)
    dataset_id = Column(String(64), nullable=False, index=True)
    configuration_json = Column(Text, nullable=False, default="{}")
    metrics_json = Column(Text, nullable=False, default="{}")
    report_json = Column(Text, nullable=False, default="{}")
    provenance_json = Column(Text, nullable=False, default="{}")
    fold_id = Column(Integer, nullable=True, index=True)
    train_start = Column(String(24), nullable=True)
    train_end = Column(String(24), nullable=True)
    validation_start = Column(String(24), nullable=True)
    validation_end = Column(String(24), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)


class SchemaVersionRecord(Base):
    __tablename__ = "schema_version"
    id = Column(Integer, primary_key=True)
    version = Column(Integer, nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


# --------------------------------------------------------------------------- #
# Lifecycle event record (Phase 17 — append-only audit trail)
# --------------------------------------------------------------------------- #
class LifecycleEventRecord(Base):
    __tablename__ = "strategy_lifecycle_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(String(64), ForeignKey("strategies.strategy_id"), nullable=False, index=True)
    event_type = Column(String(32), nullable=False, index=True)
    from_status = Column(String(24), nullable=True)
    to_status = Column(String(24), nullable=True)
    reason = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False)


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
        # Ensure schema version is initialized.
        with self._Session() as s:
            rec = s.get(SchemaVersionRecord, 1)
            if rec is None:
                s.add(SchemaVersionRecord(id=1, version=1, updated_at=_parse_dt(_now())))
                s.commit()

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

    # --- strategies ---
    def register_strategy(self, s: Strategy) -> Strategy:
        rec = s.as_record()
        with self._Session() as session:
            existing = session.get(StrategyRecord, s.strategy_id)
            if existing:
                if existing.spec_json != s.spec_json:
                    raise ValueError(
                        f"strategy {s.strategy_id} already exists with a different spec"
                    )
                return _rec_to_strategy(existing)
            session.add(rec)
            session.commit()
        return s

    def get_strategy(self, strategy_id: str) -> Optional[Strategy]:
        with self._Session() as s:
            rec = s.get(StrategyRecord, strategy_id)
            return _rec_to_strategy(rec) if rec else None

    def get_strategy_by_spec(self, spec: "StrategySpec") -> Optional[Strategy]:
        sid = strategy_identity(spec)
        with self._Session() as s:
            rec = s.get(StrategyRecord, sid)
            return _rec_to_strategy(rec) if rec else None

    def list_strategies(self, symbol: Optional[str] = None,
                        timeframe: Optional[str] = None,
                        status: Optional[str] = None,
                        generated_by: Optional[str] = None) -> list[Strategy]:
        with self._Session() as s:
            q = select(StrategyRecord)
            if symbol:
                q = q.where(StrategyRecord.symbol == symbol)
            if timeframe:
                q = q.where(StrategyRecord.timeframe == timeframe)
            if status:
                q = q.where(StrategyRecord.status == status)
            if generated_by:
                q = q.where(StrategyRecord.generated_by == generated_by)
            recs = s.execute(q.order_by(StrategyRecord.created_at.desc())).scalars().all()
        return [_rec_to_strategy(r) for r in recs]

    def update_strategy_status(self, strategy_id: str, status: StrategyStatus) -> None:
        with self._Session() as s:
            rec = s.get(StrategyRecord, strategy_id)
            if rec is None:
                raise KeyError(strategy_id)
            rec.status = status.value
            rec.updated_at = _parse_dt(_now())
            s.commit()

    # --- strategy evidence ---
    def record_strategy_evidence(self, e: StrategyEvidence) -> StrategyEvidence:
        rec = e.as_record()
        with self._Session() as s:
            existing = s.get(StrategyEvidenceRecord, e.evidence_id)
            if existing:
                return _rec_to_strategy_evidence(existing)
            s.add(rec)
            s.commit()
        return e

    def get_strategy_evidence(self, evidence_id: str) -> Optional[StrategyEvidence]:
        with self._Session() as s:
            rec = s.get(StrategyEvidenceRecord, evidence_id)
            return _rec_to_strategy_evidence(rec) if rec else None

    def list_strategy_evidence(self, strategy_id: Optional[str] = None,
                               evidence_type: Optional[str] = None,
                               dataset_id: Optional[str] = None,
                               fold_id: Optional[int] = None) -> list[StrategyEvidence]:
        with self._Session() as s:
            q = select(StrategyEvidenceRecord)
            if strategy_id:
                q = q.where(StrategyEvidenceRecord.strategy_id == strategy_id)
            if evidence_type:
                q = q.where(StrategyEvidenceRecord.evidence_type == evidence_type)
            if dataset_id:
                q = q.where(StrategyEvidenceRecord.dataset_id == dataset_id)
            if fold_id is not None:
                q = q.where(StrategyEvidenceRecord.fold_id == fold_id)
            recs = s.execute(q.order_by(StrategyEvidenceRecord.created_at.desc())).scalars().all()
        return [_rec_to_strategy_evidence(r) for r in recs]

    def get_latest_strategy_evidence(self, strategy_id: str) -> Optional[StrategyEvidence]:
        evs = self.list_strategy_evidence(strategy_id=strategy_id)
        return evs[0] if evs else None

    def get_strategy_history(self, strategy_id: str) -> list[StrategyEvidence]:
        return self.list_strategy_evidence(strategy_id=strategy_id)

    def _schema_version(self) -> Optional[int]:
        with self._Session() as s:
            rec = s.get(SchemaVersionRecord, 1)
            return rec.version if rec else None

    def _set_schema_version(self, version: int) -> None:
        with self._Session() as s:
            rec = s.get(SchemaVersionRecord, 1)
            if rec is None:
                rec = SchemaVersionRecord(id=1, version=version, updated_at=_parse_dt(_now()))
                s.add(rec)
            else:
                rec.version = version
                rec.updated_at = _parse_dt(_now())
            s.commit()

    # --- schema migration (Phase 17) ---
    CURRENT_SCHEMA_VERSION = 2

    def ensure_schema_current(self) -> int:
        """Idempotent forward migration. Returns the resulting schema version.

        Phase 17 added the append-only ``strategy_lifecycle_events`` table;
        Phase 18 reuses the existing ``strategies`` / ``strategy_evidence``
        tables (PAPER_TRADING is just a new ``EvidenceType`` value, the
        underlying column is a free-text String). All migrations are additive
        only: existing tables/columns are untouched, so Phase 16/17 records
        remain readable. Safe to call on every startup.
        """
        current = self._schema_version()
        if current is not None and current >= self.CURRENT_SCHEMA_VERSION:
            return current
        Base.metadata.create_all(self.engine)
        self._set_schema_version(self.CURRENT_SCHEMA_VERSION)
        return self.CURRENT_SCHEMA_VERSION

    # --- lifecycle events ---
    def record_lifecycle_event(self, e: LifecycleEvent) -> LifecycleEvent:
        rec = e.as_record()
        with self._Session() as s:
            s.add(rec)
            s.commit()
        # Reconstruct from the known input (avoids reading detached-instance
        # attributes after the session closes; the DB-generated id is not
        # required for the audit trail).
        return LifecycleEvent(
            event_type=e.event_type,
            strategy_id=e.strategy_id,
            from_status=e.from_status,
            to_status=e.to_status,
            reason=e.reason,
            created_at=e.created_at,
        )

    def list_lifecycle_events(self, strategy_id: Optional[str] = None) -> list[LifecycleEvent]:
        with self._Session() as s:
            q = select(LifecycleEventRecord)
            if strategy_id:
                q = q.where(LifecycleEventRecord.strategy_id == strategy_id)
            recs = s.execute(q.order_by(LifecycleEventRecord.created_at.asc())).scalars().all()
        return [_rec_to_lifecycle_event(r) for r in recs]


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

    # --- strategies ---
    def register_strategy(self, spec: "StrategySpec") -> Strategy:
        sid = strategy_identity(spec)
        s = Strategy(
            strategy_id=sid,
            name=spec.name,
            symbol=spec.symbol,
            timeframe=spec.timeframe,
            spec_json=spec.to_json(),
            spec_hash=sid,
            status=StrategyStatus.PROPOSED,
            generated_by=spec.generated_by,
            description=spec.description,
        )
        return self.store.register_strategy(s)

    def get_strategy(self, strategy_id: str) -> Optional[Strategy]:
        return self.store.get_strategy(strategy_id)

    def get_strategy_by_spec(self, spec: "StrategySpec") -> Optional[Strategy]:
        return self.store.get_strategy_by_spec(spec)

    def list_strategies(self, symbol: Optional[str] = None,
                        timeframe: Optional[str] = None,
                        status: Optional[str] = None,
                        generated_by: Optional[str] = None) -> list[Strategy]:
        return self.store.list_strategies(
            symbol=symbol, timeframe=timeframe, status=status, generated_by=generated_by
        )

    def update_strategy_status(self, strategy_id: str, status: StrategyStatus) -> None:
        self.store.update_strategy_status(strategy_id, status)

    def record_strategy_evidence(self, e: StrategyEvidence) -> StrategyEvidence:
        return self.store.record_strategy_evidence(e)

    def get_strategy_evidence(self, evidence_id: str) -> Optional[StrategyEvidence]:
        return self.store.get_strategy_evidence(evidence_id)

    def list_strategy_evidence(self, strategy_id: Optional[str] = None,
                               evidence_type: Optional[str] = None,
                               dataset_id: Optional[str] = None,
                               fold_id: Optional[int] = None) -> list[StrategyEvidence]:
        return self.store.list_strategy_evidence(
            strategy_id=strategy_id, evidence_type=evidence_type,
            dataset_id=dataset_id, fold_id=fold_id,
        )

    def get_latest_strategy_evidence(self, strategy_id: str) -> Optional[StrategyEvidence]:
        return self.store.get_latest_strategy_evidence(strategy_id)

    def get_strategy_history(self, strategy_id: str) -> list[StrategyEvidence]:
        return self.store.get_strategy_history(strategy_id)

    # --- lifecycle events (Phase 17) ---
    def record_lifecycle_event(self, e: LifecycleEvent) -> LifecycleEvent:
        return self.store.record_lifecycle_event(e)

    def list_lifecycle_events(self, strategy_id: Optional[str] = None) -> list[LifecycleEvent]:
        return self.store.list_lifecycle_events(strategy_id=strategy_id)


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


def _rec_to_strategy(rec: StrategyRecord) -> Strategy:
    return Strategy(
        strategy_id=rec.strategy_id,
        name=rec.name,
        symbol=rec.symbol,
        timeframe=rec.timeframe,
        spec_json=rec.spec_json,
        spec_hash=rec.spec_hash,
        status=StrategyStatus(rec.status),
        generated_by=rec.generated_by,
        description=rec.description,
        parent_strategy_id=rec.parent_strategy_id or "",
        generation_metadata=json.loads(rec.generation_metadata or "{}"),
        created_at=rec.created_at.isoformat() if rec.created_at else "",
        updated_at=rec.updated_at.isoformat() if rec.updated_at else "",
    )


def _rec_to_strategy_evidence(rec: StrategyEvidenceRecord) -> StrategyEvidence:
    return StrategyEvidence(
        evidence_id=rec.evidence_id,
        strategy_id=rec.strategy_id,
        strategy_spec_hash=rec.strategy_spec_hash,
        evidence_type=EvidenceType(rec.evidence_type),
        dataset_id=rec.dataset_id,
        configuration_json=json.loads(rec.configuration_json or "{}"),
        metrics_json=json.loads(rec.metrics_json or "{}"),
        report_json=json.loads(rec.report_json or "{}"),
        provenance_json=json.loads(rec.provenance_json or "{}"),
        fold_id=rec.fold_id,
        train_start=rec.train_start,
        train_end=rec.train_end,
        validation_start=rec.validation_start,
        validation_end=rec.validation_end,
        created_at=rec.created_at.isoformat() if rec.created_at else "",
    )


# --------------------------------------------------------------------------- #
# Lifecycle event model (Phase 17)
# --------------------------------------------------------------------------- #
class LifecycleEvent(BaseModel):
    """Append-only strategy lifecycle/audit event.

    ``from_status`` / ``to_status`` are research lifecycle states (see
    ``StrategyStatus``), never execution permissions. Reasons are free-form but
    required for terminal transitions (retirement / rejection).
    """

    model_config = ConfigDict(extra="forbid")

    event_type: str
    strategy_id: str
    from_status: Optional[StrategyStatus] = None
    to_status: Optional[StrategyStatus] = None
    reason: str = ""
    created_at: str = ""

    def as_record(self) -> "LifecycleEventRecord":
        return LifecycleEventRecord(
            strategy_id=self.strategy_id,
            event_type=self.event_type,
            from_status=self.from_status.value if self.from_status else None,
            to_status=self.to_status.value if self.to_status else None,
            reason=self.reason,
            created_at=_parse_dt(self.created_at or _now()),
        )


def _rec_to_lifecycle_event(rec: LifecycleEventRecord) -> LifecycleEvent:
    return LifecycleEvent(
        event_type=rec.event_type,
        strategy_id=rec.strategy_id,
        from_status=StrategyStatus(rec.from_status) if rec.from_status else None,
        to_status=StrategyStatus(rec.to_status) if rec.to_status else None,
        reason=rec.reason or "",
        created_at=rec.created_at.isoformat() if rec.created_at else "",
    )
