"""Strategy Lab — AI Strategy Research Engine (Phase 13).

The AI proposes structured StrategySpec documents through a controlled DSL;
this package validates, interprets, backtests, evaluates, filters, and ranks
them deterministically. The AI never produces code and never touches
execution: no Broker, no PaperBroker, no FYERS, no live trading.

Pipeline: generate -> validate -> backtest -> evaluate -> filter -> rank.
"""
from .dsl import (
    Comparison,
    ComparisonOp,
    Condition,
    IndicatorName,
    LogicNode,
    LogicOp,
    NotNode,
    Operand,
    OperandKind,
    PriceField,
    collect_indicator_refs,
    indicator_key,
    is_known_indicator,
    iter_conditions,
    param_spec,
    validate_indicator_params,
    warmup_bars_for,
)
from .spec import (
    IndicatorDef,
    PositionSizing,
    RiskParams,
    StrategySpec,
    SpecStatus,
    const_operand,
    field_operand,
    indicator_operand,
    logic,
    make_condition,
    not_,
)
from .validation import (
    StrategyValidationError,
    require_valid,
    validate_spec,
)
from .interpreter import (
    InterpreterError,
    SpecStrategy,
    build_strategy,
    compute_indicators,
    evaluate_condition,
)
from .providers import (
    DeterministicStrategyProvider,
    GenerationContext,
    OpenAICompatibleStrategyProvider,
    StrategyProposalProvider,
)
from .evaluation import (
    StrategyEvaluation,
    evaluate_result,
    evaluate_spec,
    evaluate_strategy,
)
from .filters import FilterOutcome, QualityFilterConfig, apply_quality_filter
from .ranking import CandidateScore, RankingConfig, rank_candidates
from .engine import (
    CandidateOutcome,
    HoldoutEvaluation,
    ResearchConfig,
    ResearchReport,
    StrategyResearchEngine,
    merged_backtest_config,
)
from .walk_forward import (
    Fold,
    FoldResult,
    WalkForwardConfig,
    WalkForwardReport,
    WalkForwardSummary,
    collect_warnings,
    compute_walk_forward_summary,
    generate_folds,
    validate_fold,
    walk_forward_research,
    walk_forward_validate,
)
from .ai_walk_forward import (
    AIWalkForwardConfig,
    FoldProvenance,
    build_generation_context,
    walk_forward_ai_research,
    walk_forward_ai_validate,
)
from .cost_sensitivity import (
    CostSensitivityConfig,
    CostSensitivityPoint,
    CostSensitivityResult,
    run_cost_sensitivity,
)
from .regime_eval import (
    RegimeEvalConfig,
    RegimeEvaluationReport,
    RegimeResult,
    RegimeWindow,
    run_regime_evaluation,
)
from .parameter_sensitivity import (
    ParameterSensitivityConfig,
    ParameterSensitivityPoint,
    ParameterSensitivityResult,
    run_parameter_sensitivity,
)
from .research_artifact import (
    InvalidTransitionError,
    LifecycleState,
    ResearchArtifact,
    RobustnessConfig,
    RobustnessEvaluationConfig,
    compute_regime_diversity,
    evaluate_candidate_research,
    transition,
)

__all__ = [
    # DSL
    "Comparison", "ComparisonOp", "Condition", "IndicatorName", "LogicNode",
    "LogicOp", "NotNode", "Operand", "OperandKind", "PriceField",
    "collect_indicator_refs", "indicator_key", "is_known_indicator",
    "iter_conditions", "param_spec", "validate_indicator_params",
    "warmup_bars_for",
    # Spec
    "IndicatorDef", "PositionSizing", "RiskParams", "StrategySpec", "SpecStatus",
    "const_operand", "field_operand", "indicator_operand", "logic",
    "make_condition", "not_",
    # Validation
    "StrategyValidationError", "require_valid", "validate_spec",
    # Interpreter
    "InterpreterError", "SpecStrategy", "build_strategy", "compute_indicators",
    "evaluate_condition",
    # AI providers
    "DeterministicStrategyProvider", "GenerationContext",
    "OpenAICompatibleStrategyProvider", "StrategyProposalProvider",
    # Evaluation
    "StrategyEvaluation", "evaluate_result", "evaluate_spec", "evaluate_strategy",
    # Filters
    "FilterOutcome", "QualityFilterConfig", "apply_quality_filter",
    # Ranking
    "CandidateScore", "RankingConfig", "rank_candidates",
    # Engine
    "CandidateOutcome", "HoldoutEvaluation", "ResearchConfig", "ResearchReport",
    "StrategyResearchEngine", "merged_backtest_config",
    # Walk-forward (Phase 14)
    "Fold", "FoldResult", "WalkForwardConfig", "WalkForwardReport",
    "WalkForwardSummary", "collect_warnings", "compute_walk_forward_summary",
    "generate_folds", "validate_fold", "walk_forward_research",
    "walk_forward_validate",
    # AI walk-forward (Phase 15)
    "AIWalkForwardConfig", "FoldProvenance", "build_generation_context",
    "walk_forward_ai_research", "walk_forward_ai_validate",
    # Phase 19 - Strategy Research & Robustness
    "CostSensitivityConfig", "CostSensitivityPoint", "CostSensitivityResult",
    "run_cost_sensitivity",
    "RegimeEvalConfig", "RegimeEvaluationReport", "RegimeResult", "RegimeWindow",
    "run_regime_evaluation",
    "ParameterSensitivityConfig", "ParameterSensitivityPoint",
    "ParameterSensitivityResult", "run_parameter_sensitivity",
    "LifecycleState", "InvalidTransitionError", "transition",
    "ResearchArtifact", "RobustnessConfig", "RobustnessEvaluationConfig",
    "compute_regime_diversity", "evaluate_candidate_research",
]
