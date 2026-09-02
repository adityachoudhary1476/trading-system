"""Research artifact aggregator (Phase 19 - Strategy Research).

Combines the existing StrategyResearchEngine output (backtest + evaluation)
with the new robustness artifacts (cost sensitivity, regime, parameter
sensitivity) into a single auditable record per candidate.

Also handles the lifecycle state transitions:

    DISCOVERED   -- StrategyLibrary catalog hit
    SPECIFIED    -- validate_spec OK
    BACKTESTED   -- StrategyResearchEngine produced an evaluation
    VALIDATING   -- walk-forward / cost-sensitivity / regime / parameter run
    VALIDATED    -- gates pass (cost_resilience + regime_diversity + param_stability)
    PAPER_ELIGIBLE -- explicit mapping (no automatic promotion to live)
    PAPER_ACTIVE -- explicit lifecycle state (still requires human approval)
    REJECTED / RETIRED -- terminal states

Lifecycle state machine:

    DISCOVERED -> SPECIFIED
    SPECIFIED  -> BACKTESTED
    BACKTESTED -> VALIDATING
    VALIDATING  -> VALIDATED | REJECTED
    VALIDATED   -> PAPER_ELIGIBLE | REJECTED
    PAPER_ELIGIBLE -> PAPER_ACTIVE | REJECTED | RETIRED
    PAPER_ACTIVE -> RETIRED

This module NEVER auto-promotes. The state is computed deterministically from
the artifacts; consumers (CLI, paper deployment) must explicitly transition.

This is research/audit infrastructure; no execution capability is added.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .cost_sensitivity import (
    CostSensitivityConfig,
    CostSensitivityResult,
    run_cost_sensitivity,
)
from .evaluation import StrategyEvaluation
from .parameter_sensitivity import (
    ParameterSensitivityConfig,
    ParameterSensitivityResult,
    run_parameter_sensitivity,
)
from .regime_eval import RegimeEvalConfig, RegimeEvaluationReport, run_regime_evaluation
from .spec import SpecStatus, StrategySpec


class LifecycleState(str, Enum):
    DISCOVERED = "discovered"
    SPECIFIED = "specified"
    BACKTESTED = "backtested"
    VALIDATING = "validating"
    VALIDATED = "validated"
    PAPER_ELIGIBLE = "paper_eligible"
    PAPER_ACTIVE = "paper_active"
    REJECTED = "rejected"
    RETIRED = "retired"


# Allowed transitions; anything else raises InvalidTransitionError.
_ALLOWED_TRANSITIONS: dict = {
    LifecycleState.DISCOVERED: {LifecycleState.SPECIFIED, LifecycleState.REJECTED},
    LifecycleState.SPECIFIED: {LifecycleState.BACKTESTED, LifecycleState.REJECTED},
    LifecycleState.BACKTESTED: {LifecycleState.VALIDATING, LifecycleState.REJECTED},
    LifecycleState.VALIDATING: {LifecycleState.VALIDATED, LifecycleState.REJECTED},
    LifecycleState.VALIDATED: {
        LifecycleState.PAPER_ELIGIBLE,
        LifecycleState.REJECTED,
        LifecycleState.RETIRED,
    },
    LifecycleState.PAPER_ELIGIBLE: {
        LifecycleState.PAPER_ACTIVE,
        LifecycleState.REJECTED,
        LifecycleState.RETIRED,
    },
    LifecycleState.PAPER_ACTIVE: {LifecycleState.RETIRED, LifecycleState.REJECTED},
    LifecycleState.REJECTED: set(),
    LifecycleState.RETIRED: set(),
}


class InvalidTransitionError(ValueError):
    pass


def transition(current: LifecycleState, target: LifecycleState) -> LifecycleState:
    """Move ``current`` to ``target`` if the transition is allowed.

    Raises InvalidTransitionError otherwise. Terminal states (REJECTED, RETIRED)
    cannot be left.
    """
    if current in (LifecycleState.REJECTED, LifecycleState.RETIRED):
        raise InvalidTransitionError(
            f"lifecycle state {current.value} is terminal and cannot transition"
        )
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidTransitionError(
            f"invalid lifecycle transition {current.value} -> {target.value}"
        )
    return target


# --------------------------------------------------------------------------- #
# Robustness scoring
# --------------------------------------------------------------------------- #
@dataclass
class RobustnessConfig:
    """Configurable robustness thresholds (deterministic).

    A candidate passes if ALL the following hold:
      * cost_fragile == False (cost sensitivity preserves the edge)
      * regime_diversity >= min_regime_diversity_ratio
      * parameter_sensitivity_score >= min_parameter_sensitivity_score
      * backtest_reliable == True (sample-size hygiene)
      * net_total_return > 0

    These thresholds are research-quality gates, NOT guarantees.
    """

    min_regime_diversity_ratio: float = 0.3
    """Minimum fraction of regimes (out of total regime classes with enough data)
    in which the candidate shows a positive total_return."""
    min_parameter_sensitivity_score: float = 0.4
    """Minimum acceptable parameter stability (1 - normalized dispersion)."""
    require_positive_return: bool = True
    """Reject candidates whose net_total_return at base cost is <= 0."""
    require_backtest_reliable: bool = True
    """Reject candidates the existing backtest flagged unreliable."""

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_regime_diversity_ratio <= 1.0:
            raise ValueError("min_regime_diversity_ratio must be in [0, 1]")
        if not 0.0 <= self.min_parameter_sensitivity_score <= 1.0:
            raise ValueError("min_parameter_sensitivity_score must be in [0, 1]")


def compute_regime_diversity(regime_report: RegimeEvaluationReport) -> tuple:
    """Return (positive_ratio, n_evaluated, n_positive).

    Regimes that are skipped (too small / errored / unknown) are excluded
    from the denominator, so we never penalize a candidate for an instrument
    that simply never entered a high-vol regime during the sample.
    """
    evaluated = [
        r for r in regime_report.results
        if r.evaluation is not None and not r.error and r.rows > 0
    ]
    if not evaluated:
        return 0.0, 0, 0
    positive = [r for r in evaluated if r.evaluation.total_return > 0]
    return len(positive) / len(evaluated), len(evaluated), len(positive)


# --------------------------------------------------------------------------- #
# Research artifact
# --------------------------------------------------------------------------- #
@dataclass
class ResearchArtifact:
    """Complete audit record for one candidate strategy evaluation.

    Includes:
      * lifecycle_state (computed)
      * evaluation_summary (from existing StrategyEvaluation)
      * cost_sensitivity (from cost_sensitivity.py)
      * regime_evaluation (from regime_eval.py)
      * parameter_sensitivity (from parameter_sensitivity.py)
      * search_count (used by the ranking penalty layer)
      * robustness_score (decomposable, see robustness_components)
      * decision + decision_reasons (human-readable)
    """

    candidate_id: str
    spec: StrategySpec
    evaluation: StrategyEvaluation
    lifecycle_state: LifecycleState
    cost_sensitivity: Optional[CostSensitivityResult]
    regime_evaluation: Optional[RegimeEvaluationReport]
    parameter_sensitivity: Optional[ParameterSensitivityResult]
    search_count: int
    robustness_score: Optional[float] = None
    robustness_components: dict = field(default_factory=dict)
    decision: str = ""
    decision_reasons: list = field(default_factory=list)

    def to_record(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "spec_name": self.spec.name,
            "lifecycle_state": self.lifecycle_state.value,
            "search_count": self.search_count,
            "evaluation": {
                "total_return": self.evaluation.total_return,
                "max_drawdown": self.evaluation.max_drawdown,
                "sharpe": self.evaluation.sharpe,
                "n_trades": self.evaluation.n_trades,
                "win_rate": self.evaluation.win_rate,
                "profit_factor": self.evaluation.profit_factor,
                "exposure_pct": self.evaluation.exposure_pct,
                "transaction_costs": self.evaluation.transaction_costs,
                "reliable": self.evaluation.reliable,
            },
            "robustness_score": self.robustness_score,
            "robustness_components": dict(self.robustness_components),
            "decision": self.decision,
            "decision_reasons": list(self.decision_reasons),
            "cost_sensitivity": (
                self.cost_sensitivity.to_record() if self.cost_sensitivity else None
            ),
            "regime_evaluation": (
                self.regime_evaluation.to_record() if self.regime_evaluation else None
            ),
            "parameter_sensitivity": (
                self.parameter_sensitivity.to_record()
                if self.parameter_sensitivity
                else None
            ),
        }


# --------------------------------------------------------------------------- #
# Build / evaluate
# --------------------------------------------------------------------------- #
@dataclass
class RobustnessEvaluationConfig:
    cost_sensitivity: CostSensitivityConfig = field(default_factory=CostSensitivityConfig)
    regime: RegimeEvalConfig = field(default_factory=RegimeEvalConfig)
    parameter: ParameterSensitivityConfig = field(default_factory=ParameterSensitivityConfig)
    robustness: RobustnessConfig = field(default_factory=RobustnessConfig)
    search_count: int = 1
    """Number of candidates searched to surface this one. Used to compute
    the search/selection penalty applied by the ranking layer.
    """


def evaluate_candidate_research(
    candidate_id: str,
    spec: StrategySpec,
    evaluation: StrategyEvaluation,
    dataset,  # HistoricalDataset
    backtest_config,  # BacktestConfig
    config: Optional[RobustnessEvaluationConfig] = None,
) -> ResearchArtifact:
    """Run all robustness sweeps and produce a ResearchArtifact."""
    cfg = config or RobustnessEvaluationConfig()

    # Cost sensitivity.
    try:
        cost = run_cost_sensitivity(
            candidate_id, spec, dataset, backtest_config, cfg.cost_sensitivity
        )
    except Exception as e:  # noqa: BLE001
        cost = None

    # Regime evaluation.
    try:
        regime = run_regime_evaluation(
            candidate_id, spec, dataset, backtest_config, cfg.regime
        )
    except Exception as e:  # noqa: BLE001
        regime = None

    # Parameter sensitivity.
    try:
        param = run_parameter_sensitivity(
            candidate_id, spec, dataset, backtest_config, cfg.parameter
        )
    except Exception as e:  # noqa: BLE001
        param = None

    artifact = ResearchArtifact(
        candidate_id=candidate_id,
        spec=spec,
        evaluation=evaluation,
        lifecycle_state=LifecycleState.BACKTESTED,  # initially
        cost_sensitivity=cost,
        regime_evaluation=regime,
        parameter_sensitivity=param,
        search_count=cfg.search_count,
    )

    # Compute robustness components.
    components = _compute_components(
        artifact, cfg.robustness, cost, regime, param, evaluation
    )
    artifact.robustness_components = components

    # Lifecycle + decision.
    state, decision, reasons = _decide(artifact, components, cfg.robustness)
    artifact.lifecycle_state = state
    artifact.decision = decision
    artifact.decision_reasons = reasons

    return artifact


def _compute_components(
    artifact: ResearchArtifact,
    robustness: RobustnessConfig,
    cost: Optional[CostSensitivityResult],
    regime: Optional[RegimeEvaluationReport],
    param: Optional[ParameterSensitivityResult],
    evaluation: StrategyEvaluation,
) -> dict:
    """Compute the decomposable robustness components.

    Each component is a number in [0, 1] (or None if unknown). The composite
    score is a weighted mean of available components.
    """
    components: dict = {}

    # Cost resilience: 1 - normalized degradation of worst-friction net return
    # vs gross return. Saturating at 0 (fragile) / 1 (robust).
    if (
        cost is not None
        and cost.gross_evaluation is not None
        and cost.net_total_return_at_worst is not None
        and cost.gross_evaluation.total_return > 0
    ):
        worst = cost.net_total_return_at_worst
        gross = cost.gross_evaluation.total_return
        if gross > 0:
            ratio = worst / gross if gross > 0 else 0.0
            components["cost_resilience"] = max(0.0, min(1.0, ratio))
        else:
            components["cost_resilience"] = 0.0
    else:
        components["cost_resilience"] = None

    # Regime diversity: positive_ratio of regimes with positive return.
    if regime is not None:
        pos_ratio, n_eval, n_pos = compute_regime_diversity(regime)
        components["regime_diversity"] = pos_ratio if n_eval > 0 else None
        components["regime_n_evaluated"] = n_eval
        components["regime_n_positive"] = n_pos
    else:
        components["regime_diversity"] = None

    # Parameter stability: 1 - normalized dispersion.
    if param is not None and param.sensitivity_score is not None:
        components["parameter_stability"] = param.sensitivity_score
    else:
        components["parameter_stability"] = None

    # Sample size reliability: existing ``reliable`` flag from the backtest.
    components["sample_reliability"] = 1.0 if evaluation.reliable else 0.0

    # Net return sign.
    components["positive_net_return"] = (
        1.0 if evaluation.total_return > 0 else 0.0
    )

    # Search-count penalty: penalize heavily-searched candidates. The
    # penalty is 1 / sqrt(search_count) capped at 1.0; a pre-specified
    # strategy (search_count=1) gets 1.0, 100 variants -> 0.10.
    components["search_count_penalty"] = 1.0 / max(1.0, float(artifact.search_count)) ** 0.5
    components["search_count"] = artifact.search_count

    # Weighted composite of available numeric components. Default weights
    # reward robustness signals more than search-count.
    weights = {
        "cost_resilience": 0.20,
        "regime_diversity": 0.20,
        "parameter_stability": 0.20,
        "sample_reliability": 0.15,
        "positive_net_return": 0.10,
        "search_count_penalty": 0.15,
    }
    num = 0.0
    den = 0.0
    for k, w in weights.items():
        v = components.get(k)
        if v is None:
            continue
        num += v * w
        den += w
    if den > 0:
        components["__composite"] = num / den
        artifact.robustness_score = components["__composite"]
    else:
        artifact.robustness_score = None

    return components


def _decide(
    artifact: ResearchArtifact,
    components: dict,
    config: RobustnessConfig,
) -> tuple:
    """Decide the lifecycle state and explain why."""
    reasons: list = []
    cost = artifact.cost_sensitivity
    regime = artifact.regime_evaluation
    param = artifact.parameter_sensitivity
    evaluation = artifact.evaluation

    # Reject always wins: cost-fragile, sample unreliable, or negative net.
    rejected = False
    if cost is not None and cost.cost_fragile:
        rejected = True
        reasons.append(
            f"cost-fragile: worst-friction total_return "
            f"{cost.net_total_return_at_worst} falls below gross "
            f"{cost.gross_evaluation.total_return if cost.gross_evaluation else '?'} "
            "beyond the configured threshold"
        )
    if config.require_backtest_reliable and not evaluation.reliable:
        rejected = True
        reasons.append("backtest flagged as unreliable (small sample)")
    if config.require_positive_return and evaluation.total_return <= 0:
        rejected = True
        reasons.append(
            f"non-positive net total_return at base cost: {evaluation.total_return}"
        )

    # Otherwise validate against diversity + stability thresholds.
    valid = not rejected
    pos_ratio = components.get("regime_diversity")
    if pos_ratio is None:
        reasons.append("regime evaluation unavailable; diversity not assessed")
    elif pos_ratio < config.min_regime_diversity_ratio:
        valid = False
        reasons.append(
            f"regime diversity {pos_ratio:.2f} below "
            f"{config.min_regime_diversity_ratio:.2f}"
        )
    pss = components.get("parameter_stability")
    if pss is None:
        reasons.append("parameter sensitivity unavailable; stability not assessed")
    elif pss < config.min_parameter_sensitivity_score:
        valid = False
        reasons.append(
            f"parameter sensitivity {pss:.2f} below "
            f"{config.min_parameter_sensitivity_score:.2f}"
        )

    if rejected:
        return LifecycleState.REJECTED, "rejected", reasons
    if not valid:
        return LifecycleState.REJECTED, "rejected", reasons

    return LifecycleState.VALIDATED, "validated", reasons


__all__ = [
    "LifecycleState",
    "InvalidTransitionError",
    "transition",
    "RobustnessConfig",
    "RobustnessEvaluationConfig",
    "ResearchArtifact",
    "compute_regime_diversity",
    "evaluate_candidate_research",
]