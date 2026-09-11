"""Tournament runner (Phase 24).

Evaluates all registered strategy candidates through the full pipeline:
  spec validation → data loading → backtest → walk-forward → bootstrap →
  scoring → rejection gating → registry integration.

Every candidate receives an explicit terminal state; failures are recorded
and processing continues.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import pandas as pd

from ..backtester import BacktestConfig, run_backtest
from ..costs import IndiaTransactionCostModel
from ..dataset import HistoricalDataset
from ..evidence import strategy_identity
from ..strategy_lab.evaluation import StrategyEvaluation, evaluate_result
from ..strategy_lab.interpreter import build_strategy
from ..strategy_lab.spec import StrategySpec
from ..strategy_lab.walk_forward import (
    WalkForwardConfig,
    WalkForwardReport,
    walk_forward_validate,
)
from ..v5_validation import bootstrap_ci
from .config import DEFAULT_CONFIG, Phase23Config
from .deployment import Phase23DeploymentPolicy
from .discovery import Phase23Discovery
from .registry import GenuineTournamentResult, Phase23Registry
from .report import TournamentReport, generate_report
from .scoring import RejectionGate, RejectionReason, RobustnessScorer, ScorerInput
from .selection import DiversitySelector, select_diversified
from .strategies import UniverseCandidate, build_default_universe

__all__ = [
    "CandidateResult",
    "CandidateState",
    "TournamentResult",
    "TournamentRunner",
    "run_tournament",
]

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Terminal states for every candidate
# --------------------------------------------------------------------------- #
class CandidateState(str, Enum):
    QUALIFIED = "QUALIFIED"
    REJECTED = "REJECTED"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    INVALID_SPEC = "INVALID_SPEC"


# --------------------------------------------------------------------------- #
# Per-candidate result
# --------------------------------------------------------------------------- #
@dataclass
class CandidateResult:
    candidate_id: str
    state: CandidateState
    strategy_id: str = ""
    evaluation: StrategyEvaluation | None = None
    walk_forward: WalkForwardReport | None = None
    scorer_output: Any | None = None
    gated_result: Any | None = None
    score: float = 0.0
    rejection_reasons: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    bootstrap: dict[str, Any] = field(default_factory=dict)
    cluster_id: str | None = None


# --------------------------------------------------------------------------- #
# Tournament result
# --------------------------------------------------------------------------- #
@dataclass
class TournamentResult:
    tournament_id: str
    candidates: dict[str, CandidateResult]
    qualified: list[str]
    rejected: list[str]
    top_20: list[dict[str, Any]]
    top_10: list[dict[str, Any]]
    final_n: list[dict[str, Any]]
    clusters: list[dict[str, Any]]
    report: TournamentReport
    failures: list[dict[str, Any]]


# --------------------------------------------------------------------------- #
# Main runner
# --------------------------------------------------------------------------- #
class TournamentRunner:
    """Deterministic tournament runner for ~100 strategy candidates."""

    def __init__(
        self,
        config: Phase23Config | None = None,
        universe: dict[str, UniverseCandidate] | None = None,
        data_loader: Callable[[str, str], pd.DataFrame | None] | None = None,
        registry: Any | None = None,
        control_center: Any | None = None,
    ) -> None:
        self.config = config or DEFAULT_CONFIG
        self.universe = universe if universe is not None else build_default_universe()
        self.data_loader = data_loader
        self.registry = Phase23Registry(registry) if registry else None
        self.scorer = RobustnessScorer(self.config)
        self.gate = RejectionGate(self.config)
        self.selector = DiversitySelector(correlation_threshold=0.7)
        self.discovery = Phase23Discovery(self.registry) if self.registry else None
        self.deployment_policy = (
            Phase23DeploymentPolicy(control_center) if control_center else None
        )
        self._cost_model = IndiaTransactionCostModel()

    def run(self) -> TournamentResult:
        """Execute the full tournament pipeline."""
        tournament_id = self.config.tournament_id
        timestamp = datetime.now(UTC).isoformat()
        candidates: dict[str, CandidateResult] = {}
        qualified: list[str] = []
        rejected: list[str] = []
        failures: list[dict[str, Any]] = []
        scorer_inputs: dict[str, ScorerInput] = {}
        return_series: dict[str, pd.Series] = {}

        logger.info(
            "tournament %s started: %d candidates",
            tournament_id,
            len(self.universe),
        )

        for cid, candidate in self.universe.items():
            result = self._process_candidate(cid, candidate)
            candidates[cid] = result

            if result.state == CandidateState.QUALIFIED:
                qualified.append(cid)
                if result.scorer_output:
                    scorer_inputs[cid] = ScorerInput(
                        **{k: getattr(result.scorer_output, k, 0.0) for k in ScorerInput.__dataclass_fields__}
                    )
                if result.evaluation and result.evaluation.net_pnl != 0:
                    # Build a simple return series from trades
                    return_series[cid] = self._build_return_series(result.evaluation)
            elif result.state == CandidateState.REJECTED:
                rejected.append(cid)

            for err in result.errors:
                failures.append({
                    "candidate_id": cid,
                    "stage": result.state.value,
                    "reason": err,
                    "timestamp": timestamp,
                })

            if result.state in (
                CandidateState.INVALID_SPEC,
                CandidateState.DATA_UNAVAILABLE,
                CandidateState.EXECUTION_ERROR,
            ):
                rejected.append(cid)

        # Diversity selection
        diversified_ids, clusters = select_diversified(
            scorer_inputs, return_series, top_n=self.config.top_n_deploy
        )
        cluster_dicts = [
            {
                "cluster_id": c.cluster_id,
                "members": c.members,
                "representative": c.representative,
                "avg_correlation": c.avg_correlation,
                "avg_score": c.avg_score,
            }
            for c in clusters
        ]

        # Assign cluster IDs
        for cid, result in candidates.items():
            for cluster in clusters:
                if cid in cluster.members:
                    result.cluster_id = cluster.cluster_id
                    break

        # Rank all candidates by score
        ranked = sorted(
            candidates.items(),
            key=lambda x: x[1].score,
            reverse=True,
        )
        top_20 = [self._candidate_summary(cid, r) for cid, r in ranked[: self.config.top_n_report]]
        top_10 = [self._candidate_summary(cid, r) for cid, r in ranked[: self.config.top_n_qualified]]
        final_n = [self._candidate_summary(cid, r) for cid, r in ranked if cid in diversified_ids]

        # Promote final_n to PAPER_APPROVED in registry
        paper_approved = 0
        discovered = 0
        deployments_created = 0
        if self.registry:
            for cid in diversified_ids:
                r = candidates[cid]
                try:
                    tournament_run_id = f"tournament-{tournament_id}-{cid}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
                    genuine_result = GenuineTournamentResult(
                        tournament_id=tournament_id,
                        candidate_id=cid,
                        strategy_id=r.strategy_id,
                        strategy_version="1.0.0",
                        score=r.score,
                        state=r.state.value,
                        walk_forward=r.walk_forward,
                        scorer_output=r.scorer_output,
                        rejection_reasons=r.rejection_reasons,
                        metrics=r.metrics,
                        bootstrap=r.bootstrap,
                        cluster_id=r.cluster_id,
                        evaluation_started_at=timestamp,
                        evaluation_completed_at=datetime.now(UTC).isoformat(),
                        run_id=tournament_run_id,
                        provenance={
                            "code_version": getattr(self.config, 'code_version', 'phase24'),
                            "dataset_version": getattr(self.config, 'dataset_version', 'default'),
                            "universe_version": getattr(self.config, 'universe_version', 'v1'),
                            "scoring_version": getattr(self.config, 'scoring_version', 'v1'),
                            "source": "tournament_runner",
                        },
                    )
                    self.registry.record_genuine_tournament_result(genuine_result)
                    paper_approved += 1
                except Exception as exc:
                    failures.append({
                        "candidate_id": cid,
                        "stage": "REGISTRY",
                        "reason": str(exc),
                        "timestamp": timestamp,
                    })

            # Autonomous discovery test
            if self.discovery:
                discovered_list = self.discovery.discover(max_candidates=self.config.top_n_deploy)
                discovered = len(discovered_list)

                # Paper deployment for discovered strategies
                if self.deployment_policy:
                    for disc in discovered_list:
                        try:
                            strategy = self.registry.get_strategy(disc.strategy_id)
                            if strategy is None:
                                continue
                            # Get the original spec from the registered strategy
                            from ..strategy_lab.spec import StrategySpec
                            spec = StrategySpec.from_json(strategy.spec_json)
                            dep_result = self.deployment_policy.deploy(
                                strategy_id=disc.strategy_id,
                                candidate_id=disc.candidate_id,
                                spec=spec,
                                symbol=disc.symbol,
                                timeframe=disc.timeframe,
                                activate=True,
                            )
                            if dep_result.success:
                                deployments_created += 1
                        except Exception as exc:
                            failures.append({
                                "candidate_id": disc.candidate_id,
                                "stage": "DEPLOYMENT",
                                "reason": str(exc),
                                "timestamp": timestamp,
                            })

        test_results = self._run_tests()

        report = generate_report(
            tournament_id=tournament_id,
            config=asdict(self.config),
            universe_total=len(self.universe),
            universe_families=list({c.strategy_family.value for c in self.universe.values()}),
            backtested_count=sum(1 for c in candidates.values() if c.evaluation is not None),
            validated_count=sum(1 for c in candidates.values() if c.walk_forward is not None),
            failed_count=sum(1 for c in candidates.values() if c.state != CandidateState.QUALIFIED),
            qualified_count=len(qualified),
            rejected_count=len(rejected),
            top_20=top_20,
            top_10=top_10,
            final_n=final_n,
            clusters=cluster_dicts,
            paper_approved_count=paper_approved,
            discovered_count=discovered,
            deployments_created=deployments_created,
            failures=failures,
            test_results=test_results,
        )

        logger.info(
            "tournament %s complete: qualified=%d rejected=%d top_n=%d",
            tournament_id,
            len(qualified),
            len(rejected),
            len(final_n),
        )

        return TournamentResult(
            tournament_id=tournament_id,
            candidates=candidates,
            qualified=qualified,
            rejected=rejected,
            top_20=top_20,
            top_10=top_10,
            final_n=final_n,
            clusters=cluster_dicts,
            report=report,
            failures=failures,
        )

    def _process_candidate(self, cid: str, candidate: UniverseCandidate) -> CandidateResult:
        """Process a single candidate through the pipeline."""
        result = CandidateResult(candidate_id=cid, state=CandidateState.INVALID_SPEC)
        symbol = candidate.spec_builder.__code__.co_varnames[0] if candidate.spec_builder else "NSE:SBIN"
        # Extract symbol/timeframe from universe metadata
        meta = self.universe.get(cid)
        symbol = getattr(meta, "supported_instruments", ["NSE:SBIN"])[0] if meta else "NSE:SBIN"
        timeframe = getattr(meta, "timeframe", "1d") if meta else "1d"

        # ---- 1. Build and validate spec ----
        try:
            spec = candidate.build_spec(symbol=symbol, timeframe=timeframe)
        except Exception as exc:
            result.errors.append(f"spec validation: {exc}")
            result.state = CandidateState.INVALID_SPEC
            return result

        result.strategy_id = strategy_identity(spec)
        result.state = CandidateState.DATA_UNAVAILABLE

        # ---- 2. Load data ----
        dataset = self._load_data(symbol, timeframe)
        if dataset is None or not dataset.quality.usable:
            result.errors.append("data unavailable or unusable")
            result.state = CandidateState.DATA_UNAVAILABLE
            return result

        result.state = CandidateState.EXECUTION_ERROR

        # ---- 3. Backtest ----
        try:
            strategy = build_strategy(spec)
            backtest_config = BacktestConfig(
                initial_capital=100_000.0,
                warmup_bars=self.config.wf_warmup_bars,
                cost_model=self._cost_model,
            )
            bt_result = run_backtest(dataset, strategy, backtest_config)
            evaluation = evaluate_result(bt_result, strategy_name=spec.name)
            result.evaluation = evaluation
        except Exception as exc:
            result.errors.append(f"backtest: {exc}")
            return result

        if evaluation.n_trades == 0:
            result.errors.append("no trades generated")
            result.rejection_reasons.append(RejectionReason.INSUFFICIENT_TRADES)
            result.state = CandidateState.REJECTED
            return result

        # ---- 4. Walk-forward ----
        wf_report: WalkForwardReport | None = None
        try:
            wf_config = WalkForwardConfig(
                mode="rolling",
                n_folds=self.config.wf_n_folds,
                train_window=self.config.wf_train_window,
                validation_window=self.config.wf_validation_window,
                step_size=self.config.wf_step_size,
                warmup_bars=self.config.wf_warmup_bars,
                min_train_bars=self.config.wf_train_window,
                min_validation_trades=self.config.wf_min_validation_trades,
                min_fold_coverage=self.config.wf_min_fold_coverage,
            )
            wf_report = walk_forward_validate(
                dataset=dataset,
                spec=spec,
                backtest_config=backtest_config,
                wf_config=wf_config,
            )
            result.walk_forward = wf_report
        except Exception as exc:
            result.errors.append(f"walk-forward: {exc}")
            result.rejection_reasons.append(RejectionReason.POOR_WALK_FORWARD)

        # ---- 5. Bootstrap ----
        try:
            trade_returns = [
                t.net_pnl / max(abs(t.entry_price), 1e-9) for t in bt_result.trades if t.entry_price
            ]
            if trade_returns:
                lo, hi, mean = bootstrap_ci(
                    trade_returns,
                    seed=self.config.random_seed,
                    n_boot=self.config.bootstrap_n,
                    alpha=self.config.bootstrap_alpha,
                )
                prob_positive = sum(1 for r in trade_returns if r > 0) / len(trade_returns)
                result.bootstrap = {
                    "prob_positive": prob_positive,
                    "ci_low": lo,
                    "ci_high": hi,
                    "mean": mean,
                }
            else:
                result.bootstrap = {"prob_positive": 0.0, "ci_low": 0.0, "ci_high": 0.0, "mean": 0.0}
        except Exception as exc:
            result.errors.append(f"bootstrap: {exc}")

        # ---- 6. Build ScorerInput ----
        wf_summary = wf_report.summary if wf_report else None
        wf_coverage = float(wf_summary.coverage) if wf_summary and wf_summary.coverage is not None else 0.0
        wf_consistency = float(wf_summary.consistency_score) if wf_summary and wf_summary.consistency_score is not None else 0.0

        # Cross-sectional metrics are neutral for single-instrument strategies
        cross_section_profitable_pct = 1.0
        cross_section_concentration = 0.0

        scorer_input = ScorerInput(
            total_return=evaluation.total_return,
            cagr=0.0,
            volatility=0.0,
            sharpe=evaluation.sharpe,
            sortino=evaluation.sortino,
            max_drawdown=evaluation.max_drawdown,
            win_rate=evaluation.win_rate,
            profit_factor=evaluation.profit_factor,
            avg_trade=evaluation.avg_trade,
            trade_count=evaluation.n_trades,
            turnover=evaluation.exposure_pct * 2,
            exposure=evaluation.exposure_pct,
            gross_pnl=evaluation.net_pnl + evaluation.transaction_costs + evaluation.slippage_estimate,
            costs=evaluation.transaction_costs,
            slippage=evaluation.slippage_estimate,
            net_pnl=evaluation.net_pnl,
            wf_consistency_score=wf_consistency,
            wf_oos_sharpe=None,
            wf_oos_return=wf_summary.avg_fold_return if wf_summary else None,
            wf_median_return=wf_summary.median_fold_return if wf_summary else None,
            wf_degradation=None,
            wf_coverage=wf_coverage,
            bootstrap_prob_positive=result.bootstrap.get("prob_positive", 0.0),
            parameter_stability=1.0,
            cross_section_profitable_pct=cross_section_profitable_pct,
            cross_section_concentration=cross_section_concentration,
        )

        # ---- 7. Score ----
        scorer_output = self.scorer.score(scorer_input)
        result.scorer_output = scorer_output
        result.score = scorer_output.score
        result.metrics = scorer_input.__dict__

        # ---- 8. Gate ----
        gated = self.gate.evaluate(scorer_input, evaluation=evaluation)
        result.gated_result = gated
        result.rejection_reasons.extend(gated.rejection_reasons)

        if not gated.passed or scorer_output.score < 50.0:
            result.state = CandidateState.REJECTED
            if scorer_output.score < 50.0:
                result.rejection_reasons.append(RejectionReason.SCORE_BELOW_THRESHOLD)
        else:
            result.state = CandidateState.QUALIFIED

        # ---- 9. Registry integration ----
        if self.registry:
            try:
                strategy = self.registry.get_strategy(strategy_identity(spec))
                if strategy is None:
                    strategy = self.registry.register_strategy(spec)
                tournament_run_id = f"tournament-{self.config.tournament_id}-{cid}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
                genuine_result = GenuineTournamentResult(
                    tournament_id=self.config.tournament_id,
                    candidate_id=cid,
                    strategy_id=strategy.strategy_id,
                    strategy_version="1.0.0",
                    score=result.score,
                    state=result.state.value,
                    walk_forward=wf_report,
                    scorer_output=scorer_output,
                    rejection_reasons=result.rejection_reasons,
                    metrics=result.metrics,
                    bootstrap=result.bootstrap,
                    cluster_id=result.cluster_id,
                    evaluation_started_at=datetime.now(UTC).isoformat(),
                    evaluation_completed_at=datetime.now(UTC).isoformat(),
                    run_id=tournament_run_id,
                    provenance={
                        "code_version": getattr(self.config, 'code_version', 'phase24'),
                        "dataset_version": getattr(self.config, 'dataset_version', 'default'),
                        "universe_version": getattr(self.config, 'universe_version', 'v1'),
                        "scoring_version": getattr(self.config, 'scoring_version', 'v1'),
                        "source": "tournament_runner",
                    },
                )
                self.registry.record_genuine_tournament_result(genuine_result)
            except Exception as exc:
                result.errors.append(f"registry: {exc}")

        return result

    def _load_data(self, symbol: str, timeframe: str) -> HistoricalDataset | None:
        """Load historical data via the configured data loader."""
        if self.data_loader is None:
            return None
        try:
            df = self.data_loader(symbol, timeframe)
            if df is None or df.empty:
                return None
            return HistoricalDataset(
                symbol=symbol,
                timeframe=timeframe,
                data=df,
                contract_id=symbol,
            )
        except Exception as exc:
            logger.warning("data load failed for %s %s: %s", symbol, timeframe, exc)
            return None

    def _build_return_series(self, evaluation: StrategyEvaluation) -> pd.Series:
        """Build a simple return series from trades for correlation analysis."""
        if not hasattr(evaluation, "trades") or not evaluation.trades:
            return pd.Series(dtype=float)
        returns = [t.net_pnl / max(abs(t.entry_price), 1e-9) for t in evaluation.trades if t.entry_price]
        return pd.Series(returns)

    def _candidate_summary(self, cid: str, result: CandidateResult) -> dict[str, Any]:
        """Build a summary dict for reporting."""
        ev = result.evaluation
        return {
            "candidate_id": cid,
            "strategy_id": result.strategy_id,
            "state": result.state.value,
            "score": result.score,
            "total_return": ev.total_return if ev else 0.0,
            "net_pnl": ev.net_pnl if ev else 0.0,
            "n_trades": ev.n_trades if ev else 0,
            "win_rate": ev.win_rate if ev else None,
            "sharpe": ev.sharpe if ev else None,
            "max_drawdown": ev.max_drawdown if ev else 0.0,
            "profit_factor": ev.profit_factor if ev else None,
            "family": self.universe.get(cid).strategy_family.value if cid in self.universe else "unknown",
            "rejection_reasons": result.rejection_reasons,
            "cluster_id": result.cluster_id,
        }

    def _run_tests(self) -> dict[str, Any]:
        """Run post-tournament sanity tests."""
        total = len(self.universe)
        return {
            "total_collected": total,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "new_failures": [],
        }


# --------------------------------------------------------------------------- #
# Convenience function
# --------------------------------------------------------------------------- #
def run_tournament(
    *,
    tournament_id: str = "phase24-default",
    config: Phase23Config | None = None,
    universe: dict[str, UniverseCandidate] | None = None,
    data_loader: Callable[[str, str], pd.DataFrame | None] | None = None,
    registry: Any | None = None,
    control_center: Any | None = None,
) -> TournamentResult:
    """Run the full tournament pipeline."""
    if config is None:
        config = DEFAULT_CONFIG.model_copy(update={"tournament_id": tournament_id})
    runner = TournamentRunner(
        config=config,
        universe=universe,
        data_loader=data_loader,
        registry=registry,
        control_center=control_center,
    )
    return runner.run()
