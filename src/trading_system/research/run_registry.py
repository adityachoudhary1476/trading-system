"""V5 research run registry (append-only, reproducible).

Every validation run stores:
  run_id, created_at, git_commit (if available), dataset_id, dataset_hash,
  configuration hash, seed, status, results (JSON), warnings.

Research results are APPEND-ONLY — a run is never overwritten. This enables
exact reproduction and data-snooping protection (config/dataset/seed hashes).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Column, String, Text, DateTime, Integer, select
from sqlalchemy.orm import sessionmaker

from ..storage.database import Base
from ..research.costs import __name__ as _unused  # noqa: F401 (keep import simple)


def utc_now_str() -> str:
    return datetime.now(timezone.utc).isoformat()


def config_hash(config: dict[str, Any]) -> str:
    """Deterministic sha256 of a canonical JSON config (sorted keys)."""
    payload = json.dumps(config, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3, cwd=os.getcwd())
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


class RunRecord(Base):
    __tablename__ = "v5_research_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(40), unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, index=True)
    git_commit = Column(String(64), default="unknown")
    dataset_id = Column(String(64), default="")
    dataset_hash = Column(String(32), default="")
    config_hash = Column(String(32), default="")
    seed = Column(Integer, default=0)
    status = Column(String(24), default="running")   # running/done/failed
    results_json = Column(Text, default="")
    warnings_json = Column(Text, default="")

    def results(self) -> dict:
        try:
            return json.loads(self.results_json or "{}")
        except (ValueError, TypeError):
            return {"repr": self.results_json}


class ResearchRunRegistry:
    """Append-only registry on the same SQLAlchemy Base as MarketStore."""

    def __init__(self, engine) -> None:
        self.engine = engine
        Base.metadata.create_all(engine)
        self._Session = sessionmaker(bind=engine, future=True)

    def create_run(self, dataset_id: str = "", dataset_hash: str = "",
                   config: Optional[dict] = None, seed: int = 0,
                   commit: Optional[str] = None) -> RunRecord:
        run_id = uuid.uuid4().hex[:16]
        rec = RunRecord(
            run_id=run_id,
            created_at=datetime.now(timezone.utc),
            git_commit=commit or git_commit(),
            dataset_id=dataset_id,
            dataset_hash=dataset_hash,
            config_hash=config_hash(config or {}),
            seed=seed,
            status="running",
        )
        with self._Session() as s:
            s.add(rec)
            s.commit()
            s.refresh(rec)
        return rec

    def complete_run(self, run_id: str, results: dict,
                     status: str = "done",
                     warnings: Optional[list[str]] = None) -> RunRecord:
        with self._Session() as s:
            rec = s.query(RunRecord).filter(RunRecord.run_id == run_id).one()
            rec.status = status
            rec.results_json = json.dumps(results, default=str)
            rec.warnings_json = json.dumps(warnings or [], default=str)
            s.commit()
            s.refresh(rec)
        return rec

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        with self._Session() as s:
            rec = s.query(RunRecord).filter(RunRecord.run_id == run_id).one_or_none()
            if rec is None:
                return None
            rec = rec  # detached; results() handles JSON
            return rec

    def list_runs(self, limit: int = 50) -> list[RunRecord]:
        with self._Session() as s:
            recs = s.execute(
                select(RunRecord).order_by(RunRecord.created_at.desc())
                .limit(limit)).scalars().all()
            return list(recs)
