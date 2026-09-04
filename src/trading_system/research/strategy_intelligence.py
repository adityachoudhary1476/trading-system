"""Phase 17 — Strategy Intelligence & Lifecycle layer (research only, no execution).

Deterministic, configurable layer over the Phase 16 registry/evidence system that
makes persisted strategies queryable, comparable, auditable, and safely
retireable:

    StrategySpec -> StrategyRegistry -> Historical Evidence -> Strategy Intelligence
                                                            |-> Comparison
                                                            |-> Freshness / Staleness
                                                            |-> Eligibility
                                                            |-> Lifecycle
                                                            |-> Audit / History

Design rules enforced here:
  * All configuration is explicit - no hard-coded production policy.
  * Missing evidence is represented as ``None``, never fabricated. A missing
    metric is not zero unless zero is mathematically correct.
  * Ordering is deterministic; ties break on ``strategy_id``.
  * Lifecycle transitions are explicit, typed, and append-only audited.
  * Staleness is an *interpretation* of historical evidence - it never mutates
    or deletes evidence.

This is a research/audit layer. It does not prove future profitability and does
not execute trades.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .evidence import (
    EvidenceStore,
    EvidenceType,
    LifecycleEvent,
    Strategy,
    StrategyEvidence,
    StrategyStatus,
)
from .strategy_registry import StrategyRegistry

__all__ = [
    "EvidenceFreshness",
    "Eligibility",
    "EvidenceFreshnessConfig",
    "EvidenceRequirement",
    "ComparisonConfig",
    "StrategyFreshness",
    "EligibilityResult",
    "ComparisonMetrics",
    "StrategyComparisonReport",
    "HistoryEntry",
    "StrategyHistory",
    "StrategyIntelligenceReport",
    "InvalidTransitionError",
    "StrategyIntelligence",
]


# --------------------------------------------------------------------------- #
# Deterministic timestamp helpers (ISO-8601 UTC, tz-aware)
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 string to a tz-aware UTC datetime (never raises)."""
    if not value:
        return None
    try:
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _iso_or_min(value: Optional[str]) -> str:
    """Sort key: missing timestamps sort first (oldest)."""
    return value or ""


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class EvidenceFreshness(str, Enum):
    """Interpretation of evidence age - never mutates the evidence itself."""

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class Eligibility(str, Enum):
    """Research eligibility outcome."""

    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
class EvidenceFreshnessConfig(BaseModel):
    """Configurable evidence-staleness policy.

    ``max_age_days`` is required and has no default: a staleness policy is a
    research decision that must be made explicit, never hard-coded.
    """

    model_config = ConfigDict(extra="forbid")

    max_age_days: int = Field(gt=0)


class EvidenceRequirement(BaseModel):
    """Configurable evidence requirements for research eligibility."""

    model_config = ConfigDict(extra="forbid")

    require_walk_forward: bool = True
    require_validation: bool = True
    min_validation_trades: int = Field(default=0, ge=0)
    require_recent_evidence: bool = True


class ComparisonConfig(BaseModel):
    """Deterministic, documented research-triage scoring (ordering only).

    ``research_score`` is a weighted average over the *available* normalized
    components below (each in [0, 1], higher = stronger research signal). A
    missing metric is excluded from the average - it is never fabricated as
    zero. The score drives ordering only; the raw metrics are exposed
    separately and remain ``None`` when unavailable.
    """

    model_config = ConfigDict(extra="forbid")

    w_consistency: float = Field(default=0.30, ge=0.0)
    w_return: float = Field(default=0.20, ge=0.0)
    w_profit_factor: float = Field(default=0.15, ge=0.0)
    w_drawdown: float = Field(default=0.20, ge=0.0)
    w_freshness: float = Field(default=0.15, ge=0.0)
    return_cap: float = Field(default=0.5, gt=0.0)
    drawdown_cap: float = Field(default=0.5, gt=0.0)
    profit_factor_cap: float = Field(default=3.0, gt=0.0)


# --------------------------------------------------------------------------- #
# Result models
# --------------------------------------------------------------------------- #
class StrategyFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    status: EvidenceFreshness
    latest_evidence_at: Optional[str] = None
    age_days: Optional[float] = None
    max_age_days: int


class EligibilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    status: Eligibility
    reasons: list[str] = Field(default_factory=list)


class ComparisonMetrics(BaseModel):
    """One strategy\'s comparable research snapshot.

    Metric fields are ``None`` when the underlying evidence does not support
    them - they are never silently substituted.
    """

    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    name: str
    symbol: str
    timeframe: str
    status: StrategyStatus
    generated_by: str
    latest_evidence_at: Optional[str] = None
    latest_research_evidence_id: Optional[str] = None
    latest_walk_forward_evidence_id: Optional[str] = None
    validation_return: Optional[float] = None
    validation_trade_count: Optional[int] = None
    validation_drawdown: Optional[float] = None
    profit_factor: Optional[float] = None
    consistency_score: Optional[float] = None
    positive_fold_ratio: Optional[float] = None
    evidence_freshness: EvidenceFreshness = EvidenceFreshness.UNKNOWN
    warning_count: int = 0
    comparison_eligible: bool = False
    research_score: Optional[float] = None


class StrategyComparisonReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: str
    strategies: list[ComparisonMetrics] = Field(default_factory=list)
    freshness_config: EvidenceFreshnessConfig
    comparison_config: ComparisonConfig
    schema_version: int
    report_version: str = "phase-17"


class HistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: str
    kind: str
    event_type: str
    description: str
    details: dict = Field(default_factory=dict)


class StrategyHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str
    entries: list[HistoryEntry] = Field(default_factory=list)


class StrategyIntelligenceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: str
    requested_strategy_ids: list[str] = Field(default_factory=list)
    comparison: StrategyComparisonReport
    eligibility: dict[str, EligibilityResult] = Field(default_factory=dict)
    freshness: dict[str, StrategyFreshness] = Field(default_factory=dict)
    lifecycle: dict[str, StrategyStatus] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    unavailable: dict[str, list[str]] = Field(default_factory=dict)
    report_version: str = "phase-17"
    schema_version: int


# --------------------------------------------------------------------------- #
# Lifecycle errors
# --------------------------------------------------------------------------- #
class InvalidTransitionError(Exception):
    """Raised when a requested strategy lifecycle transition is not permitted."""


# --------------------------------------------------------------------------- #
# Metric extraction from persisted evidence (defensive, None-preserving)
# --------------------------------------------------------------------------- #
def _latest_evidence_by_type(
    evidences: Sequence[StrategyEvidence], evidence_type: EvidenceType
) -> Optional[StrategyEvidence]:
    """Most recent evidence of a given type (by created_at, then evidence_id)."""
    matches = [e for e in evidences if e.evidence_type == evidence_type]
    if not matches:
        return None
    matches.sort(key=lambda e: (_iso_or_min(e.created_at), e.evidence_id), reverse=True)
    return matches[0]


def _walk_forward_summary_metrics(evidence: Optional[StrategyEvidence]) -> dict:
    """Extract aggregate metrics from a walk-forward SUMMARY evidence record.

    The summary record (fold_id is None) stores ``serialize_walk_forward_report``
    under ``metrics_json``; its ``summary`` sub-dict carries the cross-fold
    aggregates. Returns a dict that may be empty when the shape is unexpected.
    """
    if evidence is None:
        return {}
    summary = (evidence.metrics_json or {}).get("summary")
    if not isinstance(summary, dict):
        return {}

    def _float(key: str) -> Optional[float]:
        v = summary.get(key)
        if isinstance(v, (int, float)) and not (float(v) != float(v) or abs(float(v)) == float("inf")):
            return float(v)
        return None

    def _int(key: str) -> Optional[int]:
        v = summary.get(key)
        return int(v) if isinstance(v, (int, float)) else None

    return {
        "consistency_score": _float("consistency_score"),
        "max_validation_drawdown": _float("max_validation_drawdown"),
        "total_validation_trades": _int("total_validation_trades"),
        "positive_fold_ratio": _float("positive_fold_ratio"),
        "avg_fold_return": _float("avg_fold_return"),
        "return_dispersion": _float("return_dispersion"),
    }


def _best_research_metrics(evidence: Optional[StrategyEvidence]) -> dict:
    """Extract metrics from the best passing candidate of a research report.

    Research evidence stores ``serialize_research_report`` under ``metrics_json``.
    We prefer the first candidate that passed the quality filter; otherwise the
    first candidate carrying an evaluation.
    """
    if evidence is None:
        return {}
    candidates = (evidence.metrics_json or {}).get("candidates")
    if not isinstance(candidates, list):
        return {}

    chosen = None
    for c in candidates:
        if not isinstance(c, dict):
            continue
        if c.get("filter_passed") and c.get("evaluation"):
            chosen = c
            break
    if chosen is None:
        for c in candidates:
            if isinstance(c, dict) and c.get("evaluation"):
                chosen = c
                break
    if chosen is None:
        return {}

    ev = chosen.get("evaluation") or {}

    def _float(key: str) -> Optional[float]:
        v = ev.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    def _int(key: str) -> Optional[int]:
        v = ev.get(key)
        return int(v) if isinstance(v, (int, float)) else None

    return {
        "total_return": _float("total_return"),
        "profit_factor": _float("profit_factor"),
        "max_drawdown": _float("max_drawdown"),
        "n_trades": _int("n_trades"),
    }


def _count_warnings(evidence: Optional[StrategyEvidence]) -> int:
    if evidence is None:
        return 0
    warnings = (evidence.metrics_json or {}).get("warnings")
    return len(warnings) if isinstance(warnings, list) else 0

# --------------------------------------------------------------------------- #
# StrategyIntelligence - facade over StrategyRegistry / EvidenceStore
# --------------------------------------------------------------------------- #
class StrategyIntelligence:
    """Phase 17 research facade.

    Wraps a :class:`StrategyRegistry` and exposes comparison, freshness,
    eligibility, lifecycle, audit history, and an aggregated intelligence
    report. All operations are read-only except the explicit, audited lifecycle
    transitions.
    """

    def __init__(self, registry: StrategyRegistry) -> None:
        self.registry = registry
        self.store: EvidenceStore = registry.store
        self.store.ensure_schema_current()

    # ------------------------------------------------------------------- #
    # Lifecycle
    # ------------------------------------------------------------------- #
    _RETIRE_FROM = {
        StrategyStatus.PROPOSED,
        StrategyStatus.VALIDATED,
        StrategyStatus.RESEARCHED,
        StrategyStatus.WALK_FORWARD_VALIDATED,
        StrategyStatus.REJECTED,
    }
    _REACTIVATE_FROM = {StrategyStatus.RETIRED}

    def retire_strategy(self, strategy_id: str, reason: str) -> LifecycleEvent:
        """Retire a strategy. Append-only audit; historical evidence preserved."""
        strategy = self.registry.get_strategy(strategy_id)
        if strategy is None:
            raise KeyError(strategy_id)
        if strategy.status not in self._RETIRE_FROM:
            raise InvalidTransitionError(
                f"cannot retire strategy {strategy_id} from status "
                f"{strategy.status.value}; allowed from: "
                f"{sorted(s.value for s in self._RETIRE_FROM)}"
            )
        return self._transition(
            strategy_id=strategy_id,
            from_status=strategy.status,
            to_status=StrategyStatus.RETIRED,
            reason=reason,
        )

    def reactivate_strategy(self, strategy_id: str, reason: str) -> LifecycleEvent:
        """Reactivate a retired strategy back to PROPOSED. Preserves history."""
        strategy = self.registry.get_strategy(strategy_id)
        if strategy is None:
            raise KeyError(strategy_id)
        if strategy.status not in self._REACTIVATE_FROM:
            raise InvalidTransitionError(
                f"cannot reactivate strategy {strategy_id} from status "
                f"{strategy.status.value}; allowed only from: "
                f"{sorted(s.value for s in self._REACTIVATE_FROM)}"
            )
        return self._transition(
            strategy_id=strategy_id,
            from_status=strategy.status,
            to_status=StrategyStatus.PROPOSED,
            reason=reason,
        )

    def _transition(
        self,
        strategy_id: str,
        from_status: StrategyStatus,
        to_status: StrategyStatus,
        reason: str,
    ) -> LifecycleEvent:
        event = LifecycleEvent(
            event_type=f"{from_status.value}_to_{to_status.value}",
            strategy_id=strategy_id,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
            created_at=_now_iso(),
        )
        self.registry.update_strategy_status(strategy_id, to_status)
        return self.registry.record_lifecycle_event(event)

    # ------------------------------------------------------------------- #
    # Evidence freshness
    # ------------------------------------------------------------------- #
    def assess_freshness(
        self,
        strategy_id: str,
        config: EvidenceFreshnessConfig,
        *,
        now: Optional[datetime] = None,
    ) -> StrategyFreshness:
        """Deterministic evidence-freshness interpretation.

        FRESH  - evidence exists, timestamp parseable, age <= max_age_days.
        STALE  - evidence exists, timestamp parseable, age > max_age_days.
        UNKNOWN- insufficient timestamp/evidence information.
        """
        ref = now or datetime.now(timezone.utc)
        latest = self.registry.get_latest_evidence(strategy_id)
        ts = _parse_iso(latest.created_at) if latest else None
        if latest is None or ts is None:
            return StrategyFreshness(
                strategy_id=strategy_id,
                status=EvidenceFreshness.UNKNOWN,
                latest_evidence_at=latest.created_at if latest else None,
                age_days=None,
                max_age_days=config.max_age_days,
            )
        age = (ref - ts).total_seconds() / 86400.0
        status = (
            EvidenceFreshness.FRESH
            if age <= config.max_age_days
            else EvidenceFreshness.STALE
        )
        return StrategyFreshness(
            strategy_id=strategy_id,
            status=status,
            latest_evidence_at=latest.created_at,
            age_days=age,
            max_age_days=config.max_age_days,
        )

    # ------------------------------------------------------------------- #
    # Eligibility
    # ------------------------------------------------------------------- #
    def assess_eligibility(
        self,
        strategy_id: str,
        requirement: EvidenceRequirement,
        freshness_config: EvidenceFreshnessConfig,
        *,
        now: Optional[datetime] = None,
    ) -> EligibilityResult:
        """Deterministic research-eligibility evaluation with explicit reasons."""
        strategy = self.registry.get_strategy(strategy_id)
        if strategy is None:
            return EligibilityResult(
                strategy_id=strategy_id,
                status=Eligibility.INELIGIBLE,
                reasons=["unknown_strategy"],
            )

        reasons: list[str] = []

        if strategy.status == StrategyStatus.RETIRED:
            reasons.append("retired_strategy")
        if strategy.status == StrategyStatus.REJECTED:
            reasons.append("rejected_strategy")

        evidences = self.registry.list_evidence(strategy_id=strategy_id)
        has_walk_forward = any(
            e.evidence_type == EvidenceType.WALK_FORWARD for e in evidences
        )
        has_research = any(
            e.evidence_type == EvidenceType.RESEARCH for e in evidences
        )

        if requirement.require_walk_forward and not has_walk_forward:
            reasons.append("missing_walk_forward_evidence")
        if requirement.require_validation and not has_research:
            reasons.append("missing_validation_metrics")

        if requirement.min_validation_trades > 0 and has_walk_forward:
            wf_summary = _latest_evidence_by_type(evidences, EvidenceType.WALK_FORWARD)
            agg = _walk_forward_summary_metrics(wf_summary)
            total_trades = agg.get("total_validation_trades")
            if total_trades is None:
                reasons.append("missing_validation_metrics")
            elif total_trades < requirement.min_validation_trades:
                reasons.append("insufficient_validation_trades")

        if requirement.require_recent_evidence:
            freshness = self.assess_freshness(strategy_id, freshness_config, now=now)
            if freshness.status == EvidenceFreshness.STALE:
                reasons.append("stale_evidence")
            elif freshness.status == EvidenceFreshness.UNKNOWN:
                reasons.append("unknown_evidence_freshness")

        status = Eligibility.INELIGIBLE if reasons else Eligibility.ELIGIBLE
        return EligibilityResult(
            strategy_id=strategy_id, status=status, reasons=reasons
        )

    # ------------------------------------------------------------------- #
    # Comparison
    # ------------------------------------------------------------------- #
    def compare_strategies(
        self,
        strategy_ids: Sequence[str],
        freshness_config: EvidenceFreshnessConfig,
        *,
        comparison_config: Optional[ComparisonConfig] = None,
        now: Optional[datetime] = None,
    ) -> StrategyComparisonReport:
        """Build a deterministic cross-session strategy comparison report."""
        cfg = comparison_config or ComparisonConfig()
        rows: list[ComparisonMetrics] = []
        for sid in sorted(set(strategy_ids)):
            rows.append(self._build_comparison_row(sid, freshness_config, cfg, now=now))

        rows.sort(
            key=lambda r: (
                -(r.research_score if r.research_score is not None else -float("inf")),
                r.strategy_id,
            )
        )

        return StrategyComparisonReport(
            generated_at=_now_iso(),
            strategies=rows,
            freshness_config=freshness_config,
            comparison_config=cfg,
            schema_version=self.store._schema_version() or 1,
        )

    def _build_comparison_row(
        self,
        strategy_id: str,
        freshness_config: EvidenceFreshnessConfig,
        cfg: ComparisonConfig,
        *,
        now: Optional[datetime] = None,
    ) -> ComparisonMetrics:
        strategy = self.registry.get_strategy(strategy_id)
        if strategy is None:
            return ComparisonMetrics(
                strategy_id=strategy_id,
                name="",
                symbol="",
                timeframe="",
                status=StrategyStatus.PROPOSED,
                generated_by="",
                evidence_freshness=EvidenceFreshness.UNKNOWN,
            )

        evidences = self.registry.list_evidence(strategy_id=strategy_id)
        latest_research = _latest_evidence_by_type(evidences, EvidenceType.RESEARCH)
        latest_wf = _latest_evidence_by_type(evidences, EvidenceType.WALK_FORWARD)

        wf_summary = None
        if latest_wf is not None and latest_wf.fold_id is None:
            wf_summary = latest_wf
        else:
            for e in evidences:
                if e.evidence_type == EvidenceType.WALK_FORWARD and e.fold_id is None:
                    wf_summary = e
                    break

        agg = _walk_forward_summary_metrics(wf_summary)
        research = _best_research_metrics(latest_research)

        latest_at = None
        for e in evidences:
            if e.created_at and (latest_at is None or e.created_at > latest_at):
                latest_at = e.created_at

        freshness = self.assess_freshness(strategy_id, freshness_config, now=now)

        profit_factor = research.get("profit_factor")
        if profit_factor is None:
            profit_factor = None

        validation_return = agg.get("avg_fold_return")
        if validation_return is None:
            validation_return = research.get("total_return")

        validation_drawdown = agg.get("max_validation_drawdown")
        if validation_drawdown is None:
            validation_drawdown = research.get("max_drawdown")

        row = ComparisonMetrics(
            strategy_id=strategy_id,
            name=strategy.name,
            symbol=strategy.symbol,
            timeframe=strategy.timeframe,
            status=strategy.status,
            generated_by=strategy.generated_by,
            latest_evidence_at=latest_at or None,
            latest_research_evidence_id=latest_research.evidence_id if latest_research else None,
            latest_walk_forward_evidence_id=latest_wf.evidence_id if latest_wf else None,
            validation_return=validation_return,
            validation_trade_count=agg.get("total_validation_trades"),
            validation_drawdown=validation_drawdown,
            profit_factor=profit_factor,
            consistency_score=agg.get("consistency_score"),
            positive_fold_ratio=agg.get("positive_fold_ratio"),
            evidence_freshness=freshness.status,
            warning_count=_count_warnings(wf_summary) if wf_summary else _count_warnings(latest_research),
            comparison_eligible=latest_research is not None or latest_wf is not None,
        )
        row.research_score = self._research_score(row, cfg)
        return row

    def _research_score(
        self, row: ComparisonMetrics, cfg: ComparisonConfig
    ) -> Optional[float]:
        """Deterministic ordering score over available normalized components."""
        components: list[float] = []
        weights: list[float] = []

        if row.consistency_score is not None:
            components.append(_clamp(row.consistency_score, 0.0, 1.0))
            weights.append(cfg.w_consistency)
        if row.positive_fold_ratio is not None:
            components.append(_clamp(row.positive_fold_ratio, 0.0, 1.0))
            weights.append(cfg.w_return)
        if row.validation_return is not None:
            components.append(_clamp(row.validation_return / cfg.return_cap, -1.0, 1.0) * 0.5 + 0.5)
            weights.append(cfg.w_return)
        if row.profit_factor is not None:
            components.append(_clamp(row.profit_factor / cfg.profit_factor_cap, 0.0, 1.0))
            weights.append(cfg.w_profit_factor)
        if row.validation_drawdown is not None:
            components.append(_clamp(1.0 - abs(row.validation_drawdown) / cfg.drawdown_cap, 0.0, 1.0))
            weights.append(cfg.w_drawdown)

        freshness_val = {
            EvidenceFreshness.FRESH: 1.0,
            EvidenceFreshness.UNKNOWN: 0.5,
            EvidenceFreshness.STALE: 0.0,
        }[row.evidence_freshness]
        components.append(freshness_val)
        weights.append(cfg.w_freshness)

        if not components:
            return None
        total = sum(w for w in weights)
        if total <= 0:
            return None
        return sum(c * w for c, w in zip(components, weights)) / total

    # ------------------------------------------------------------------- #
    # Cross-session queries
    # ------------------------------------------------------------------- #
    def list_active_strategies(self) -> list[Strategy]:
        """All strategies that are neither retired nor rejected."""
        active: list[Strategy] = []
        for s in self.registry.list_strategies():
            if s.status not in (StrategyStatus.RETIRED, StrategyStatus.REJECTED):
                active.append(s)
        return sorted(active, key=lambda s: (s.created_at or "", s.strategy_id))

    def list_retired_strategies(self) -> list[Strategy]:
        return self.registry.list_strategies(status=StrategyStatus.RETIRED.value)

    def list_strategies_for_symbol(self, symbol: str) -> list[Strategy]:
        return self.registry.list_strategies(symbol=symbol)

    def list_stale_strategies(
        self, config: EvidenceFreshnessConfig, *, now: Optional[datetime] = None
    ) -> list[tuple[Strategy, StrategyFreshness]]:
        """All strategies whose latest evidence exceeds the freshness threshold."""
        stale: list[tuple[Strategy, StrategyFreshness]] = []
        for s in self.registry.list_strategies():
            freshness = self.assess_freshness(s.strategy_id, config, now=now)
            if freshness.status == EvidenceFreshness.STALE:
                stale.append((s, freshness))
        stale.sort(key=lambda x: x[0].strategy_id)
        return stale

    # ------------------------------------------------------------------- #
    # History / audit
    # ------------------------------------------------------------------- #
    def get_strategy_history(self, strategy_id: str) -> StrategyHistory:
        """Chronologically deterministic audit history."""
        entries: list[HistoryEntry] = []

        strategy = self.registry.get_strategy(strategy_id)
        if strategy is not None:
            entries.append(HistoryEntry(
                timestamp=strategy.created_at or _now_iso(),
                kind="registration",
                event_type="strategy_registered",
                description=f"registered strategy {strategy.name!r}",
                details={
                    "strategy_id": strategy.strategy_id,
                    "symbol": strategy.symbol,
                    "timeframe": strategy.timeframe,
                    "status": strategy.status.value,
                    "generated_by": strategy.generated_by,
                },
            ))

        for ev in self.registry.list_lifecycle_events(strategy_id=strategy_id):
            entries.append(HistoryEntry(
                timestamp=ev.created_at,
                kind="lifecycle",
                event_type=ev.event_type,
                description=ev.reason or ev.event_type,
                details={
                    "from_status": ev.from_status.value if ev.from_status else None,
                    "to_status": ev.to_status.value if ev.to_status else None,
                    "reason": ev.reason,
                },
            ))

        for ev in self.registry.list_evidence(strategy_id=strategy_id):
            detail = {
                "evidence_id": ev.evidence_id,
                "evidence_type": ev.evidence_type.value,
                "dataset_id": ev.dataset_id,
            }
            if ev.fold_id is not None:
                detail["fold_id"] = ev.fold_id
            prov = ev.provenance_json or {}
            if prov.get("source"):
                detail["source"] = prov["source"]
            entries.append(HistoryEntry(
                timestamp=ev.created_at,
                kind="evidence",
                event_type=f"evidence_{ev.evidence_type.value}",
                description=f"recorded {ev.evidence_type.value} evidence",
                details=detail,
            ))

        entries.sort(key=lambda e: (e.timestamp, e.kind, e.event_type))
        return StrategyHistory(strategy_id=strategy_id, entries=entries)

    # ------------------------------------------------------------------- #
    # Intelligence report
    # ------------------------------------------------------------------- #
    def build_intelligence_report(
        self,
        strategy_ids: Sequence[str],
        freshness_config: EvidenceFreshnessConfig,
        *,
        requirement: Optional[EvidenceRequirement] = None,
        comparison_config: Optional[ComparisonConfig] = None,
        now: Optional[datetime] = None,
    ) -> StrategyIntelligenceReport:
        """High-level, serializable intelligence report."""
        ids = list(strategy_ids)
        comparison = self.compare_strategies(
            ids, freshness_config,
            comparison_config=comparison_config, now=now,
        )
        req = requirement or EvidenceRequirement()

        eligibility: dict[str, EligibilityResult] = {}
        freshness: dict[str, StrategyFreshness] = {}
        lifecycle: dict[str, StrategyStatus] = {}
        unavailable: dict[str, list[str]] = {}
        warnings: list[str] = []

        for sid in ids:
            strat = self.registry.get_strategy(sid)
            if strat is None:
                warnings.append(f"unknown strategy id: {sid}")
                lifecycle[sid] = StrategyStatus.PROPOSED
                continue
            lifecycle[sid] = strat.status
            el = self.assess_eligibility(sid, req, freshness_config, now=now)
            eligibility[sid] = el
            fr = self.assess_freshness(sid, freshness_config, now=now)
            freshness[sid] = fr
            missing = self._unavailable_metrics(sid)
            if missing:
                unavailable[sid] = missing

        return StrategyIntelligenceReport(
            generated_at=_now_iso(),
            requested_strategy_ids=ids,
            comparison=comparison,
            eligibility=eligibility,
            freshness=freshness,
            lifecycle=lifecycle,
            warnings=warnings,
            unavailable=unavailable,
            schema_version=self.store._schema_version() or 1,
        )

    def _unavailable_metrics(self, strategy_id: str) -> list[str]:
        """Names of expected metrics that are unavailable for a strategy."""
        evidences = self.registry.list_evidence(strategy_id=strategy_id)
        missing: list[str] = []
        has_research = any(e.evidence_type == EvidenceType.RESEARCH for e in evidences)
        has_wf = any(e.evidence_type == EvidenceType.WALK_FORWARD for e in evidences)
        if not has_research and not has_wf:
            missing.extend([
                "validation_return", "validation_trade_count",
                "validation_drawdown", "profit_factor", "consistency_score",
            ])
            return missing
        if has_research:
            research = _best_research_metrics(
                _latest_evidence_by_type(evidences, EvidenceType.RESEARCH)
            )
            if research.get("profit_factor") is None:
                missing.append("profit_factor")
        if has_wf:
            wf_summary = None
            for e in evidences:
                if e.evidence_type == EvidenceType.WALK_FORWARD and e.fold_id is None:
                    wf_summary = e
                    break
            agg = _walk_forward_summary_metrics(wf_summary)
            for key in ("consistency_score", "avg_fold_return",
                        "max_validation_drawdown", "total_validation_trades"):
                if agg.get(key) is None:
                    missing.append(key)
        return missing


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))
