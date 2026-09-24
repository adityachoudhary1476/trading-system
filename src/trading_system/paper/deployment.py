"""Phase 18 — Paper deployment models + SQLAlchemy persistence.

A ``PaperDeployment`` is an explicit, auditable record that binds:

  * a registered strategy (by ``strategy_id`` AND exact ``strategy_spec_hash``)
  * a target symbol / timeframe
  * a typed ``PaperDeploymentConfig`` (risk + sizing + paper-only mode)
  * the dataset identity the deployment will replay against
  * the deployment lifecycle status

Deployments are immutable snapshots bound to the exact StrategySpec identity
they were created from. A new spec hash requires a new deployment.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from ..research.evidence import Base
from ..india.instruments import OptionType


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
class PaperDeploymentStatus(str, Enum):
    """Explicit, append-only deployment lifecycle states."""

    CREATED = "created"
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"
    FAILED = "failed"


# Statuses that allow new order submission.
STATUS_ACCEPTS_ORDERS = frozenset({PaperDeploymentStatus.ACTIVE})


class PaperDeploymentConfig(BaseModel):
    """Typed, paper-only deployment configuration.

    ``execution_mode`` is fixed to ``"paper"`` and any attempt to set a live
    mode is rejected at validation time. No credentials are accepted here.
    """

    model_config = ConfigDict(extra="forbid")

    execution_mode: str = Field(default="paper")
    initial_cash: float = Field(default=100_000.0, gt=0.0)
    slippage_bps: float = Field(default=5.0, ge=0.0)
    fee_bps: float = Field(default=0.0, ge=0.0)

    # Risk / sizing — base policy that CANNOT be widened by the StrategySpec.
    max_allocation_pct: float = Field(default=1.0, gt=0.0, le=1.0)
    max_position_size: Optional[float] = Field(default=None, gt=0.0)
    allow_short: bool = False

    stop_loss_pct: Optional[float] = Field(default=None, gt=0.0, lt=1.0)
    take_profit_pct: Optional[float] = Field(default=None, gt=0.0, lt=1.0)
    max_loss_per_trade_pct: Optional[float] = Field(default=None, gt=0.0, lt=1.0)

    # How many bars must be observed before the first order may fire
    # (indicator warm-up). Same semantics as the backtester warm-up window.
    warmup_bars: int = Field(default=0, ge=0)

    # --- Phase 8: Options support ---
    # When True, StrategySignals may carry an ``options_selection`` and the
    # deployment may open/flatten option positions. When False (default, backward
    # compatible), any options_selection on a signal is rejected by the gate.
    options_enabled: bool = Field(default=False)

    # Restrict which option rights may be selected. Empty list = both allowed.
    allowed_option_types: list[str] = Field(default_factory=lambda: ["CE", "PE"])

    # Cap on contracts per single order (0 or None = no cap).
    max_options_contracts_per_trade: Optional[int] = Field(default=None, ge=0)

    # --- Strategy parameters (Phase F2) ---
    strategy_parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Strategy-specific parameter overrides validated against the strategy's parameter schema.",
    )

    @model_validator(mode="after")
    def _paper_only(self) -> "PaperDeploymentConfig":
        if self.execution_mode != "paper":
            raise ValueError(
                f"execution_mode must be 'paper'; got {self.execution_mode!r}"
            )
        if self.options_enabled:
            for ot in self.allowed_option_types:
                if ot not in ("CE", "PE"):
                    raise ValueError(f"allowed_option_types must be CE/PE, got {ot!r}")
        return self


# --------------------------------------------------------------------------- #
# Deployment record (pydantic + ORM)
# --------------------------------------------------------------------------- #
class PaperDeployment(BaseModel):
    """An explicit paper-trading deployment.

    Identity derives from the strategy_id + spec_hash + symbol + timeframe +
    configuration + dataset_id. The same inputs produce the same
    ``deployment_id`` (idempotent creation).
    """

    model_config = ConfigDict(extra="forbid")

    deployment_id: str
    strategy_id: str
    strategy_spec_hash: str
    symbol: str
    timeframe: str
    dataset_id: str
    config: PaperDeploymentConfig
    status: PaperDeploymentStatus = PaperDeploymentStatus.CREATED
    evidence_ids: list[str] = Field(default_factory=list)
    created_at: str = ""
    activated_at: Optional[str] = None
    updated_at: str = ""
    notes: str = ""

    # --- Scheduler heartbeat fields (Phase 23) ---
    # These track the autonomous scheduler worker liveness, separate from
    # the passive PaperStrategyRunner. They are persisted in PostgreSQL.
    last_tick_at: Optional[str] = None
    last_successful_tick_at: Optional[str] = None
    last_market_data_at: Optional[str] = None
    last_decision_at: Optional[str] = None
    last_execution_at: Optional[str] = None
    worker_id: Optional[str] = None
    worker_version: Optional[str] = None

    # Convenience accessors for Phase 8 options fields.
    @property
    def options_enabled(self) -> bool:
        return self.config.options_enabled

    @property
    def allowed_option_types(self) -> list[str]:
        return self.config.allowed_option_types

    @property
    def max_options_contracts_per_trade(self) -> Optional[int]:
        return self.config.max_options_contracts_per_trade

    def as_record(self) -> "PaperDeploymentRecord":
        return PaperDeploymentRecord(
            deployment_id=self.deployment_id,
            strategy_id=self.strategy_id,
            strategy_spec_hash=self.strategy_spec_hash,
            symbol=self.symbol,
            timeframe=self.timeframe,
            dataset_id=self.dataset_id,
            config_json=json.dumps(self.config.model_dump(mode="json")),
            status=self.status.value,
            evidence_ids_json=json.dumps(list(self.evidence_ids)),
            created_at=_parse_dt(self.created_at or _now_iso()),
            activated_at=_parse_dt(self.activated_at) if self.activated_at else None,
            updated_at=_parse_dt(self.updated_at or _now_iso()),
            notes=self.notes or "",
            options_enabled=self.config.options_enabled,
            allowed_option_types_json=json.dumps(self.config.allowed_option_types),
            max_options_contracts_per_trade=self.config.max_options_contracts_per_trade,
            strategy_parameters_json=json.dumps(self.config.strategy_parameters),
            last_tick_at=_parse_dt(self.last_tick_at) if self.last_tick_at else None,
            last_successful_tick_at=_parse_dt(self.last_successful_tick_at) if self.last_successful_tick_at else None,
            last_market_data_at=_parse_dt(self.last_market_data_at) if self.last_market_data_at else None,
            last_decision_at=_parse_dt(self.last_decision_at) if self.last_decision_at else None,
            last_execution_at=_parse_dt(self.last_execution_at) if self.last_execution_at else None,
            worker_id=self.worker_id,
            worker_version=self.worker_version,
        )

    @classmethod
    def _rec_to_deployment(cls, rec: "PaperDeploymentRecord") -> "PaperDeployment":
        """Build a PaperDeployment from a SQLAlchemy record."""
        config_data = json.loads(rec.config_json or "{}")
        return cls(
            deployment_id=rec.deployment_id,
            strategy_id=rec.strategy_id,
            strategy_spec_hash=rec.strategy_spec_hash,
            symbol=rec.symbol,
            timeframe=rec.timeframe,
            dataset_id=rec.dataset_id,
            config=PaperDeploymentConfig(**config_data),
            status=PaperDeploymentStatus(rec.status),
            evidence_ids=list(json.loads(rec.evidence_ids_json or "[]")),
            created_at=rec.created_at.isoformat() if rec.created_at else "",
            activated_at=rec.activated_at.isoformat() if rec.activated_at else None,
            updated_at=rec.updated_at.isoformat() if rec.updated_at else "",
            notes=rec.notes or "",
            last_tick_at=rec.last_tick_at.isoformat() if rec.last_tick_at else None,
            last_successful_tick_at=rec.last_successful_tick_at.isoformat() if rec.last_successful_tick_at else None,
            last_market_data_at=rec.last_market_data_at.isoformat() if rec.last_market_data_at else None,
            last_decision_at=rec.last_decision_at.isoformat() if rec.last_decision_at else None,
            last_execution_at=rec.last_execution_at.isoformat() if rec.last_execution_at else None,
            worker_id=rec.worker_id,
            worker_version=rec.worker_version,
        )


class PaperDeploymentRecord(Base):
    """SQLAlchemy persistence record for Phase 18 deployments."""

    __tablename__ = "paper_deployments"

    deployment_id = Column(String(64), primary_key=True)
    strategy_id = Column(String(64), nullable=False, index=True)
    strategy_spec_hash = Column(String(64), nullable=False, index=True)
    symbol = Column(String(32), nullable=False)
    timeframe = Column(String(8), nullable=False)
    dataset_id = Column(String(64), nullable=False)
    config_json = Column(Text, nullable=False, default="{}")
    status = Column(String(16), nullable=False, default="created", index=True)
    evidence_ids_json = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime(timezone=True), nullable=False)
    activated_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    notes = Column(Text, nullable=False, default="")
    options_enabled = Column(Boolean, nullable=False, default=False)
    allowed_option_types_json = Column(Text, nullable=False, default='["CE","PE"]')
    max_options_contracts_per_trade = Column(Integer, nullable=True)
    strategy_parameters_json = Column(Text, nullable=False, default="{}")

    # --- Scheduler heartbeat fields (Phase 23) ---
    last_tick_at = Column(DateTime(timezone=True), nullable=True)
    last_successful_tick_at = Column(DateTime(timezone=True), nullable=True)
    last_market_data_at = Column(DateTime(timezone=True), nullable=True)
    last_decision_at = Column(DateTime(timezone=True), nullable=True)
    last_execution_at = Column(DateTime(timezone=True), nullable=True)
    worker_id = Column(String(64), nullable=True)
    worker_version = Column(String(32), nullable=True)


# --------------------------------------------------------------------------- #
# Deterministic deployment identity
# --------------------------------------------------------------------------- #
def deployment_identity(
    strategy_id: str,
    strategy_spec_hash: str,
    symbol: str,
    timeframe: str,
    dataset_id: str,
    config: PaperDeploymentConfig,
) -> str:
    """SHA-256 of the immutable deployment inputs (idempotent creation)."""
    payload = {
        "strategy_id": strategy_id,
        "strategy_spec_hash": strategy_spec_hash,
        "symbol": symbol,
        "timeframe": timeframe,
        "dataset_id": dataset_id,
        "config": json.loads(
            json.dumps(config.model_dump(mode="json"), sort_keys=True)
        ),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:64]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _parse_dt(value: Any) -> datetime:
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


def rec_to_deployment(rec: PaperDeploymentRecord) -> PaperDeployment:
    """Convert a SQLAlchemy record to a PaperDeployment model."""
    return PaperDeployment._rec_to_deployment(rec)