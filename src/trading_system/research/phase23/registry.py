"""Phase 23 registry integration (Phase 24).

Extends the existing StrategyRegistry with tournament-specific lifecycle
states and evidence persistence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..evidence import EvidenceType, StrategyEvidence, StrategyStatus
from ..strategy_lab.spec import StrategySpec
from ..strategy_lab.walk_forward import WalkForwardReport
from ..strategy_registry import StrategyRegistry, evidence_identity

__all__ = [
    "GenuineTournamentResult",
    "Phase23Registry",
    "TournamentRecord",
]


@dataclass
class GenuineTournamentResult:
    """Output of a genuine Phase 23 tournament evaluation.

    Only results of this type may produce ``PAPER_APPROVED`` evidence.
    All fields are populated by the tournament runner from actual evaluation
    output — never supplied by a caller ad-hoc.
    """
    tournament_id: str
    candidate_id: str
    strategy_id: str
    strategy_version: str
    score: float
    state: str  # QUALIFIED | REJECTED | DATA_UNAVAILABLE | EXECUTION_ERROR | INVALID_SPEC
    walk_forward: WalkForwardReport | None = None
    scorer_output: Any = None
    rejection_reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    bootstrap: dict[str, Any] = field(default_factory=dict)
    cluster_id: str | None = None
    evaluation_started_at: str = ""
    evaluation_completed_at: str = ""
    run_id: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)


class Phase23Registry:
    """Tournament-aware wrapper around StrategyRegistry."""

    def __init__(self, registry: StrategyRegistry) -> None:
        self._registry = registry

    def record_genuine_tournament_result(self, result: GenuineTournamentResult) -> None:
        """Persist a genuine tournament result as lifecycle evidence.

        This is the ONLY method that may produce ``PAPER_APPROVED`` evidence.
        It accepts a typed :class:`GenuineTournamentResult` populated by the
        tournament runner from actual evaluation output.

        Raises:
            ValueError: if the result is incomplete or provenance is missing.
            KeyError: if the strategy is not registered.
        """
        strategy = self._registry.get_strategy(result.strategy_id)
        if strategy is None:
            raise KeyError(f"strategy {result.strategy_id} not registered")

        self._validate_genuine_result(result)

        ts = result.evaluation_completed_at or datetime.now(UTC).isoformat()

        # Derive qualification status from genuine state
        status_map = {
            "QUALIFIED": "PAPER_APPROVED",
            "REJECTED": "REJECTED",
            "DATA_UNAVAILABLE": "DATA_UNAVAILABLE",
            "EXECUTION_ERROR": "EXECUTION_ERROR",
            "INVALID_SPEC": "INVALID_SPEC",
        }
        qualification_status = status_map.get(result.state, "REJECTED")

        evidence_payload = {
            "tournament_id": result.tournament_id,
            "strategy_version": result.strategy_version,
            "score": result.score,
            "qualification_status": qualification_status,
            "validation_summary": {
                "rejection_reasons": result.rejection_reasons,
                "cluster_id": result.cluster_id,
                "metrics": result.metrics,
                "bootstrap": result.bootstrap,
            },
            "candidate_id": result.candidate_id,
            "timestamp": ts,
            "code_version": result.provenance.get("code_version", "phase24"),
            "run_id": result.run_id,
            "provenance": result.provenance,
        }

        # Update strategy status based on genuine qualification
        new_status = {
            "PAPER_APPROVED": StrategyStatus.VALIDATED,
            "REJECTED": StrategyStatus.REJECTED,
            "DATA_UNAVAILABLE": StrategyStatus.PROPOSED,
            "EXECUTION_ERROR": StrategyStatus.PROPOSED,
            "INVALID_SPEC": StrategyStatus.REJECTED,
        }.get(qualification_status, StrategyStatus.PROPOSED)

        self._registry.update_strategy_status(result.strategy_id, new_status)

        evidence = StrategyEvidence(
            evidence_id=evidence_identity(
                result.strategy_id,
                EvidenceType.RESEARCH,
                f"tournament-{result.tournament_id}",
                {"candidate": result.candidate_id, "run_id": result.run_id},
            ),
            strategy_id=result.strategy_id,
            strategy_spec_hash=strategy.spec_hash,
            evidence_type=EvidenceType.RESEARCH,
            dataset_id=f"tournament-{result.tournament_id}",
            configuration_json=evidence_payload,
            provenance_json=result.provenance,
            created_at=ts,
        )
        self._registry.record_evidence(evidence)

        # Also record walk-forward evidence if available
        if result.walk_forward is not None and result.walk_forward.summary is not None:
            wf = result.walk_forward
            wf_payload = {
                "tournament_id": result.tournament_id,
                "strategy_version": result.strategy_version,
                "wf_summary": {
                    "n_folds": wf.summary.n_folds,
                    "n_valid": wf.summary.n_valid,
                    "n_failed": wf.summary.n_failed,
                    "coverage": wf.summary.coverage,
                    "avg_fold_return": wf.summary.avg_fold_return,
                    "median_fold_return": wf.summary.median_fold_return,
                    "consistency_score": wf.summary.consistency_score,
                    "total_validation_trades": wf.summary.total_validation_trades,
                },
                "candidate_id": result.candidate_id,
                "timestamp": ts,
                "code_version": result.provenance.get("code_version", "phase24"),
                "run_id": result.run_id,
                "provenance": result.provenance,
            }
            wf_evidence = StrategyEvidence(
                evidence_id=evidence_identity(
                    result.strategy_id,
                    EvidenceType.WALK_FORWARD,
                    f"tournament-{result.tournament_id}",
                    {"candidate": result.candidate_id, "run_id": result.run_id},
                ),
                strategy_id=result.strategy_id,
                strategy_spec_hash=strategy.spec_hash,
                evidence_type=EvidenceType.WALK_FORWARD,
                dataset_id=f"tournament-{result.tournament_id}",
                configuration_json=wf_payload,
                provenance_json=result.provenance,
                created_at=ts,
            )
            self._registry.record_evidence(wf_evidence)

    def _validate_genuine_result(self, result: GenuineTournamentResult) -> None:
        """Validate that a tournament result contains complete genuine provenance."""
        if not result.run_id:
            raise ValueError("genuine tournament result missing run_id")
        if not result.tournament_id:
            raise ValueError("genuine tournament result missing tournament_id")
        if not result.candidate_id:
            raise ValueError("genuine tournament result missing candidate_id")
        if not result.evaluation_completed_at:
            raise ValueError("genuine tournament result missing evaluation_completed_at")
        if not result.provenance:
            raise ValueError("genuine tournament result missing provenance")
        required_prov = ["code_version", "dataset_version", "universe_version"]
        for key in required_prov:
            if key not in result.provenance:
                raise ValueError(f"genuine tournament result provenance missing {key!r}")

    def record_tournament_result(
        self,
        *,
        tournament_id: str,
        strategy_id: str,
        strategy_version: str,
        score: float,
        qualification_status: str,
        validation_summary: dict[str, Any],
        candidate_id: str,
        walk_forward: WalkForwardReport | None = None,
        timestamp: str | None = None,
    ) -> None:
        """LEGACY — record a tournament result without genuine provenance.

        .. deprecated::
            Use :meth:`record_genuine_tournament_result` instead.  This legacy
            method is preserved for backward compatibility but **can never
            produce PAPER_APPROVED evidence**.  Calling it with
            ``qualification_status="PAPER_APPROVED"`` will raise ``ValueError``.

        Raises:
            ValueError: if ``qualification_status`` is ``PAPER_APPROVED``.
        """
        if qualification_status == "PAPER_APPROVED":
            raise ValueError(
                "Legacy record_tournament_result cannot create PAPER_APPROVED. "
                "Use record_genuine_tournament_result with a GenuineTournamentResult."
            )

        strategy = self._registry.get_strategy(strategy_id)
        if strategy is None:
            return

        ts = timestamp or datetime.now(UTC).isoformat()
        evidence_payload = {
            "tournament_id": tournament_id,
            "strategy_version": strategy_version,
            "score": score,
            "qualification_status": qualification_status,
            "validation_summary": validation_summary,
            "candidate_id": candidate_id,
            "timestamp": ts,
            "code_version": "legacy",
            "run_id": "",
            "provenance": {"source": "legacy_api", "note": "cannot_produce_PAPER_APPROVED"},
        }

        status_map = {
            "REJECTED": StrategyStatus.REJECTED,
            "DATA_UNAVAILABLE": StrategyStatus.PROPOSED,
            "EXECUTION_ERROR": StrategyStatus.PROPOSED,
            "INVALID_SPEC": StrategyStatus.REJECTED,
        }
        new_status = status_map.get(qualification_status, StrategyStatus.PROPOSED)
        self._registry.update_strategy_status(strategy_id, new_status)

        evidence = StrategyEvidence(
            evidence_id=evidence_identity(
                strategy_id, EvidenceType.RESEARCH, f"tournament-{tournament_id}", {"candidate": candidate_id}
            ),
            strategy_id=strategy_id,
            strategy_spec_hash=strategy.spec_hash,
            evidence_type=EvidenceType.RESEARCH,
            dataset_id=f"tournament-{tournament_id}",
            configuration_json=evidence_payload,
            provenance_json={"source": "legacy_api", "note": "cannot_produce_PAPER_APPROVED"},
            created_at=ts,
        )
        self._registry.record_evidence(evidence)

        if walk_forward is not None and walk_forward.summary is not None:
            wf = walk_forward
            wf_payload = {
                "tournament_id": tournament_id,
                "strategy_version": strategy_version,
                "wf_summary": {
                    "n_folds": wf.summary.n_folds,
                    "n_valid": wf.summary.n_valid,
                    "n_failed": wf.summary.n_failed,
                    "coverage": wf.summary.coverage,
                    "avg_fold_return": wf.summary.avg_fold_return,
                    "median_fold_return": wf.summary.median_fold_return,
                    "consistency_score": wf.summary.consistency_score,
                    "total_validation_trades": wf.summary.total_validation_trades,
                },
                "candidate_id": candidate_id,
                "timestamp": ts,
                "code_version": "legacy",
                "run_id": "",
                "provenance": {"source": "legacy_api", "note": "cannot_produce_PAPER_APPROVED"},
            }
            wf_evidence = StrategyEvidence(
                evidence_id=evidence_identity(
                    strategy_id, EvidenceType.WALK_FORWARD, f"tournament-{tournament_id}", {"candidate": candidate_id}
                ),
                strategy_id=strategy_id,
                strategy_spec_hash=strategy.spec_hash,
                evidence_type=EvidenceType.WALK_FORWARD,
                dataset_id=f"tournament-{tournament_id}",
                configuration_json=wf_payload,
                provenance_json={"source": "legacy_api", "note": "cannot_produce_PAPER_APPROVED"},
                created_at=ts,
            )
            self._registry.record_evidence(wf_evidence)

    def get_qualified_strategies(self) -> list[Any]:
        """Return all VALIDATED strategies (potentially paper-deployable)."""
        return [
            s for s in self._registry.list_strategies()
            if s.status == StrategyStatus.VALIDATED
        ]

    def get_paper_approved(self) -> list[Any]:
        """Return strategies marked as paper-approved."""
        results = []
        for s in self._registry.list_strategies():
            if s.status != StrategyStatus.VALIDATED:
                continue
            evidence = self._registry.list_evidence(strategy_id=s.strategy_id)
            for ev in evidence:
                config = ev.configuration_json or {}
                if config.get("qualification_status") == "PAPER_APPROVED":
                    results.append(s)
                    break
        return results

    def get_paper_experimental(self) -> list[Any]:
        """Return strategies marked as paper-experimental (Phase 24 qualified but not fully approved)."""
        results = []
        for s in self._registry.list_strategies():
            if s.status != StrategyStatus.PAPER_EXPERIMENTAL:
                continue
            evidence = self._registry.list_evidence(strategy_id=s.strategy_id)
            for ev in evidence:
                config = ev.configuration_json or {}
                if config.get("qualification_status") == "PAPER_EXPERIMENTAL":
                    results.append(s)
                    break
        return results

    def get_strategy(self, strategy_id: str) -> Any | None:
        return self._registry.get_strategy(strategy_id)

    def list_strategies(self, **filters: Any) -> list[Any]:
        return self._registry.list_strategies(**filters)

    def list_evidence(self, strategy_id: str) -> list[Any]:
        return self._registry.list_evidence(strategy_id=strategy_id)

    def register_strategy(self, spec: StrategySpec):
        return self._registry.register_strategy(spec)


@dataclass
class TournamentRecord:
    tournament_id: str
    strategy_id: str
    strategy_version: str
    score: float
    qualification_status: str
    validation_summary: dict[str, Any]
    candidate_id: str
    timestamp: str
