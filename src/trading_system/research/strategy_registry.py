"""Phase 16 — Strategy Registry + Evidence Store integration.

Provides:
  * deterministic strategy identity (SHA-256 of canonical StrategySpec JSON)
  * deterministic dataset identity
  * deterministic evidence identity
  * StrategyRegistry: high-level facade for strategy persistence and retrieval
  * serialization helpers for ResearchReport, WalkForwardReport, FoldProvenance
  * persistence adapters for Phase 13/14/15 research outputs

All persisted data remains validated through the existing StrategySpec validation
choke point. No broker, no execution, no credentials.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from .backtester import BacktestConfig
from .dataset import HistoricalDataset
from .evidence import (
    EvidenceStore,
    EvidenceType,
    LifecycleEvent,
    ResearchRegistry,
    Strategy,
    StrategyEvidence,
    StrategyStatus,
    dataset_identity,
    strategy_identity,
)
from .strategy_lab.spec import StrategySpec
from .strategy_lab.evaluation import StrategyEvaluation

__all__ = [
    "StrategyRegistry",
    "strategy_identity",
    "dataset_identity",
    "evidence_identity",
    "serialize_research_report",
    "serialize_walk_forward_report",
    "serialize_fold_provenance",
    "serialize_evaluation",
    "deserialize_strategy_spec",
    "persist_research_report",
    "persist_walk_forward_report",
    "persist_paper_trading_report",
]


# --------------------------------------------------------------------------- #
# Deterministic evidence identity
# --------------------------------------------------------------------------- #
def evidence_identity(
    strategy_id: str,
    evidence_type: EvidenceType,
    dataset_id: str,
    configuration: dict,
) -> str:
    """Deterministic evidence identity derived from immutable payload content."""
    payload = {
        "strategy_id": strategy_id,
        "evidence_type": evidence_type.value,
        "dataset_id": dataset_id,
        "configuration": _stable(configuration),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:64]


# --------------------------------------------------------------------------- #
# Serialization helpers
# --------------------------------------------------------------------------- #
def _stable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, sort_keys=True, default=str))


def serialize_evaluation(eval_: Optional[StrategyEvaluation]) -> dict:
    """Serialize StrategyEvaluation to a JSON-safe dict."""
    if eval_ is None:
        return {}
    return {
        "spec_name": eval_.spec_name,
        "symbol": eval_.symbol,
        "timeframe": eval_.timeframe,
        "generated_by": eval_.generated_by,
        "initial_capital": eval_.initial_capital,
        "final_capital": eval_.final_capital,
        "net_pnl": eval_.net_pnl,
        "total_return": eval_.total_return,
        "n_trades": eval_.n_trades,
        "winning": eval_.winning,
        "losing": eval_.losing,
        "win_rate": eval_.win_rate,
        "avg_trade": eval_.avg_trade,
        "avg_trade_return": eval_.avg_trade_return,
        "max_drawdown": eval_.max_drawdown,
        "transaction_costs": eval_.transaction_costs,
        "slippage_estimate": eval_.slippage_estimate,
        "exposure_pct": eval_.exposure_pct,
        "profit_factor": eval_.profit_factor,
        "sharpe": eval_.sharpe,
        "sortino": eval_.sortino,
        "reliable": eval_.reliable,
        "notes": eval_.notes,
        "unavailable_metrics": eval_.unavailable_metrics,
    }


def serialize_fold_provenance(prov) -> dict:
    """Serialize FoldProvenance (Phase 15) to a JSON-safe dict."""
    return {
        "provider_name": prov.provider_name,
        "train_rows": prov.train_rows,
        "train_start": prov.train_start,
        "train_end": prov.train_end,
        "candidate_count": prov.candidate_count,
        "valid_candidate_count": prov.valid_candidate_count,
        "selected_spec_name": prov.selected_spec_name,
        "generation_status": prov.generation_status,
        "provider_errors": list(prov.provider_errors),
    }


def serialize_research_report(report) -> dict:
    """Serialize a Phase 13 ResearchReport to a JSON-safe dict."""
    candidates = []
    for c in report.candidates:
        candidates.append({
            "variant_index": c.variant_index,
            "status": c.status,
            "spec_name": c.spec_name,
            "spec_errors": list(c.spec_errors),
            "error": c.error,
            "evaluation": serialize_evaluation(c.evaluation),
            "filter_passed": c.filter_outcome.passed if c.filter_outcome else False,
            "filter_reasons": list(c.filter_outcome.reasons) if c.filter_outcome else [],
        })
    ranking = []
    for r in report.ranking:
        ranking.append({
            "key": r.key,
            "score": r.score,
            "components": r.components,
        })
    return {
        "symbol": report.symbol,
        "timeframe": report.timeframe,
        "rows": report.rows,
        "requested_candidates": report.requested_candidates,
        "candidates": candidates,
        "ranking": ranking,
        "notes": list(report.notes),
    }


def serialize_walk_forward_report(report) -> dict:
    """Serialize a Phase 14/15 WalkForwardReport to a JSON-safe dict."""
    folds = []
    for f in report.folds:
        folds.append({
            "fold_id": f.fold_id,
            "status": f.status,
            "error": f.error,
            "selected_spec_name": f.selected_spec.name if f.selected_spec else None,
            "train_start": f.train_start,
            "train_end": f.train_end,
            "validation_start": f.validation_start,
            "validation_end": f.validation_end,
            "train_evaluation": serialize_evaluation(f.train_evaluation),
            "validation_evaluation": serialize_evaluation(f.validation_evaluation),
            "validation_trade_count": f.validation_trade_count,
            "validation_total_return": f.validation_total_return,
            "validation_win_rate": f.validation_win_rate,
            "validation_profit_factor": f.validation_profit_factor,
            "validation_avg_trade_return": f.validation_avg_trade_return,
            "unavailable_metrics": list(f.unavailable_metrics),
        })
    summary = None
    if report.summary is not None:
        s = report.summary
        summary = {
            "n_folds": s.n_folds,
            "n_valid": s.n_valid,
            "n_failed": s.n_failed,
            "coverage": s.coverage,
            "coverage_ok": s.coverage_ok,
            "positive_folds": s.positive_folds,
            "positive_fold_ratio": s.positive_fold_ratio,
            "avg_fold_return": s.avg_fold_return,
            "median_fold_return": s.median_fold_return,
            "worst_fold_return": s.worst_fold_return,
            "best_fold_return": s.best_fold_return,
            "return_std": s.return_std,
            "return_dispersion": s.return_dispersion,
            "max_validation_drawdown": s.max_validation_drawdown,
            "consistency_score": s.consistency_score,
            "total_validation_trades": s.total_validation_trades,
            "min_validation_trades": s.min_validation_trades,
            "valid_fold_ids": list(s.valid_fold_ids),
        }
    return {
        "kind": report.kind,
        "spec_name": report.spec_name,
        "symbol": report.symbol,
        "timeframe": report.timeframe,
        "mode": report.mode,
        "folds": folds,
        "summary": summary,
        "warnings": list(report.warnings),
        "notes": list(report.notes),
    }


def deserialize_strategy_spec(payload: str) -> StrategySpec:
    """Reconstruct a validated StrategySpec from persisted JSON."""
    return StrategySpec.model_validate_json(payload)


# --------------------------------------------------------------------------- #
# StrategyRegistry — thin facade over EvidenceStore
# --------------------------------------------------------------------------- #
class StrategyRegistry:
    """Phase 16 high-level API for strategy persistence and audit.

    Wraps the existing EvidenceStore/ResearchRegistry. Persistence is optional
    for the research engine; this class is only imported where persistence is
    explicitly desired.
    """

    def __init__(self, store: EvidenceStore) -> None:
        self.store = store
        self._registry = ResearchRegistry(store)

    # --- strategy lifecycle ---
    def register_strategy(self, spec: StrategySpec) -> Strategy:
        """Register a validated StrategySpec. Idempotent: same spec -> same record."""
        if spec.generated_by == "unknown":
            spec = spec.model_copy(update={"generated_by": "manual"})
        return self._registry.register_strategy(spec)

    def get_strategy(self, strategy_id: str) -> Optional[Strategy]:
        return self._registry.get_strategy(strategy_id)

    def get_strategy_by_spec(self, spec: StrategySpec) -> Optional[Strategy]:
        return self._registry.get_strategy_by_spec(spec)

    def list_strategies(self, **filters) -> list[Strategy]:
        return self._registry.list_strategies(**filters)

    def update_strategy_status(self, strategy_id: str, status: StrategyStatus) -> None:
        self._registry.update_strategy_status(strategy_id, status)

    # --- evidence ---
    def record_evidence(self, evidence: StrategyEvidence) -> StrategyEvidence:
        return self._registry.record_strategy_evidence(evidence)

    def get_evidence(self, evidence_id: str) -> Optional[StrategyEvidence]:
        return self._registry.get_strategy_evidence(evidence_id)

    def list_evidence(self, strategy_id: Optional[str] = None, **filters) -> list[StrategyEvidence]:
        return self._registry.list_strategy_evidence(strategy_id=strategy_id, **filters)

    def get_latest_evidence(self, strategy_id: str) -> Optional[StrategyEvidence]:
        return self._registry.get_latest_strategy_evidence(strategy_id)

    def get_strategy_history(self, strategy_id: str) -> list[StrategyEvidence]:
        return self._registry.get_strategy_history(strategy_id)

    # --- lifecycle events (Phase 17) ---
    def record_lifecycle_event(self, event: LifecycleEvent) -> LifecycleEvent:
        return self._registry.record_lifecycle_event(event)

    def list_lifecycle_events(self, strategy_id: Optional[str] = None) -> list[LifecycleEvent]:
        return self._registry.list_lifecycle_events(strategy_id=strategy_id)

    # --- persistence adapters ---
    def persist_research_report(
        self,
        report,
        dataset: HistoricalDataset,
        provider_name: str,
        research_config,
        backtest_config: BacktestConfig,
        spec: Optional[StrategySpec] = None,
    ) -> StrategyEvidence:
        """Persist a Phase 13 ResearchReport as strategy evidence."""
        strategy = None
        if spec is not None:
            strategy = self.register_strategy(spec)
        elif report.passed:
            first_passed = next((c for c in report.passed if c.spec is not None), None)
            if first_passed is not None:
                strategy = self.register_strategy(first_passed.spec)

        if strategy is None:
            raise ValueError("cannot persist research report without a strategy spec")

        ds_id = dataset_identity(dataset)
        configuration = {
            "provider": provider_name,
            "research_config": _serialize_research_config(research_config),
            "backtest_config": _serialize_backtest_config(backtest_config),
            "requested_candidates": report.requested_candidates,
            "rows": report.rows,
        }
        metrics = serialize_research_report(report)
        provenance = {
            "source": "phase_13_research_engine",
            "symbol": dataset.symbol,
            "timeframe": dataset.timeframe,
        }

        ev = StrategyEvidence(
            evidence_id=evidence_identity(
                strategy.strategy_id, EvidenceType.RESEARCH, ds_id, configuration
            ),
            strategy_id=strategy.strategy_id,
            strategy_spec_hash=strategy.spec_hash,
            evidence_type=EvidenceType.RESEARCH,
            dataset_id=ds_id,
            configuration_json=configuration,
            metrics_json=metrics,
            provenance_json=provenance,
        )
        return self.record_evidence(ev)

    def persist_walk_forward_report(
        self,
        report,
        dataset: HistoricalDataset,
        wf_config,
        backtest_config: BacktestConfig,
    ) -> list[StrategyEvidence]:
        """Persist a Phase 14/15 WalkForwardReport as strategy evidence.

        Returns one StrategyEvidence per fold that has a selected strategy, plus
        one summary evidence record for the overall report.
        """
        from .strategy_lab.walk_forward import WalkForwardReport

        ds_id = dataset_identity(dataset)
        evidences: list[StrategyEvidence] = []

        # Find the strategy used across folds (same strategy may be selected in multiple folds).
        strategy = None
        for fold in report.folds:
            if fold.selected_spec is not None:
                strategy = self.register_strategy(fold.selected_spec)
                break

        if strategy is None:
            return evidences

        # Per-fold evidence
        for fold in report.folds:
            if fold.selected_spec is None:
                continue
            configuration = {
                "fold_id": fold.fold_id,
                "walk_forward_mode": report.mode,
                "backtest_config": _serialize_backtest_config(backtest_config),
                "wf_config": _serialize_walk_forward_config(wf_config),
                "train_start": fold.train_start,
                "train_end": fold.train_end,
                "validation_start": fold.validation_start,
                "validation_end": fold.validation_end,
            }
            metrics = {
                "train_evaluation": serialize_evaluation(fold.train_evaluation),
                "validation_evaluation": serialize_evaluation(fold.validation_evaluation),
                "validation_total_return": fold.validation_total_return,
                "validation_trade_count": fold.validation_trade_count,
                "validation_win_rate": fold.validation_win_rate,
                "validation_profit_factor": fold.validation_profit_factor,
                "validation_avg_trade_return": fold.validation_avg_trade_return,
                "status": fold.status,
                "error": fold.error,
                "unavailable_metrics": list(fold.unavailable_metrics),
            }
            provenance = {
                "source": "phase_14_15_walk_forward",
                "symbol": dataset.symbol,
                "timeframe": dataset.timeframe,
                "report_kind": report.kind,
            }
            ev = StrategyEvidence(
                evidence_id=evidence_identity(
                    strategy.strategy_id,
                    EvidenceType.WALK_FORWARD,
                    ds_id,
                    configuration,
                ),
                strategy_id=strategy.strategy_id,
                strategy_spec_hash=strategy.spec_hash,
                evidence_type=EvidenceType.WALK_FORWARD,
                dataset_id=ds_id,
                configuration_json=configuration,
                metrics_json=metrics,
                provenance_json=provenance,
                fold_id=fold.fold_id,
                train_start=fold.train_start,
                train_end=fold.train_end,
                validation_start=fold.validation_start,
                validation_end=fold.validation_end,
            )
            evidences.append(self.record_evidence(ev))

        # Summary evidence
        summary_config = {
            "report_kind": report.kind,
            "walk_forward_mode": report.mode,
            "backtest_config": _serialize_backtest_config(backtest_config),
            "wf_config": _serialize_walk_forward_config(wf_config),
        }
        summary_metrics = serialize_walk_forward_report(report)
        summary_provenance = {
            "source": "phase_14_15_walk_forward_summary",
            "symbol": dataset.symbol,
            "timeframe": dataset.timeframe,
            "report_kind": report.kind,
        }
        summary_ev = StrategyEvidence(
            evidence_id=evidence_identity(
                strategy.strategy_id,
                EvidenceType.WALK_FORWARD,
                ds_id,
                summary_config,
            ),
            strategy_id=strategy.strategy_id,
            strategy_spec_hash=strategy.spec_hash,
            evidence_type=EvidenceType.WALK_FORWARD,
            dataset_id=ds_id,
            configuration_json=summary_config,
            metrics_json=summary_metrics,
            provenance_json=summary_provenance,
        )
        evidences.append(self.record_evidence(summary_ev))
        return evidences


# --- Phase 18 paper-trading evidence adapter ---
    def persist_paper_trading_report(
        self,
        deployment,
        spec: "StrategySpec",
        report,
        dataset_id: str,
    ) -> StrategyEvidence:
        """Persist a Phase 18 paper-trading report as PAPER_TRADING evidence.

        Returns the persisted :class:`StrategyEvidence`. The deployment
        identity, strategy identity, and report are immutable inputs; this
        method never mutates them.
        """
        from trading_system.paper.report import build_paper_trading_evidence

        evidence = build_paper_trading_evidence(
            deployment=deployment,
            spec=spec,
            report=report,
            dataset_id=dataset_id,
        )
        return self.record_evidence(evidence)


# --------------------------------------------------------------------------- #
# Config serialization helpers
# --------------------------------------------------------------------------- #
def _serialize_backtest_config(cfg: BacktestConfig) -> dict:
    return {
        "initial_capital": cfg.initial_capital,
        "transaction_cost_pct": cfg.transaction_cost_pct,
        "slippage_pct": cfg.slippage_pct,
        "max_positions": cfg.risk.max_positions if cfg.risk else None,
    }


def _serialize_walk_forward_config(cfg) -> dict:
    return {
        "mode": cfg.mode,
        "n_folds": cfg.n_folds,
        "validation_window": cfg.validation_window,
        "train_window": cfg.train_window,
        "step_size": cfg.step_size,
        "allow_overlap": cfg.allow_overlap,
        "min_train_bars": cfg.min_train_bars,
        "min_validation_bars": cfg.min_validation_bars,
        "min_validation_trades": cfg.min_validation_trades,
        "warmup_bars": cfg.warmup_bars,
        "min_fold_coverage": cfg.min_fold_coverage,
    }


def _serialize_research_config(cfg) -> dict:
    from .strategy_lab.engine import ResearchConfig
    if hasattr(cfg, "max_candidates"):
        return {
            "max_candidates": cfg.max_candidates,
            "min_bars": cfg.min_bars,
            "allowed_symbols": list(cfg.allowed_symbols) if cfg.allowed_symbols else None,
        }
    return {}
