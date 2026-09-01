"""AI-driven walk-forward research loop (Phase 15).

Extends the Phase 14 chronological walk-forward machinery by integrating a
StrategyProposalProvider so that candidates are *generated* per fold from the
TRAIN window only.

Pipeline per fold (critical ordering):

    TRAIN data
      -> build_generation_context (TRAIN-only aggregates)
      -> provider.generate_strategy (sees TRAIN context only)
      -> validate every spec (train data sufficiency)
      -> backtest on TRAIN only
      -> evaluate on TRAIN
      -> quality filter on TRAIN
      -> rank on TRAIN
      -> SELECT winner (TRAIN metrics only)
      -> backtest SELECTED spec on VALIDATION (warm-up context + window)
      -> record validation result

    The validation window is NEVER presented to generation, validation-of-spec
    suitability, backtesting-for-selection, filtering, ranking, or selection
    for the same fold. This is enforced structurally: the research engine
    (StrategyResearchEngine) only ever receives fold.train_dataset.

Walk-forward validation is historical evidence of robustness, NOT a guarantee
of future profitability.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

from ..backtester import BacktestConfig
from ..dataset import HistoricalDataset
from .engine import ResearchConfig, StrategyResearchEngine
from .evaluation import StrategyEvaluation
from .filters import FilterOutcome
from .interpreter import build_strategy
from .providers import GenerationContext, StrategyProposalProvider
from .spec import StrategySpec, SpecStatus
from .validation import required_warmup_bars, validate_spec
from .walk_forward import (
    STATUS_NO_CANDIDATE,
    WalkForwardConfig,
    WalkForwardReport,
    _assess_fold,
    _select_top_candidate,
    collect_warnings as _collect_wf_warnings,
    compute_walk_forward_summary,
    generate_folds,
    validate_fold,
)

__all__ = [
    "AIWalkForwardConfig",
    "build_generation_context",
    "FoldProvenance",
    "walk_forward_ai_research",
    "walk_forward_ai_validate",
]


@dataclass
class AIWalkForwardConfig:
    """Configuration for AI-driven walk-forward research (Phase 15, Step 2).

    Extends WalkForwardConfig with candidate-generation parameters.
    """

    candidate_count: int = 4
    """Number of candidates the provider should propose per fold (train-only).
    Clamped to ResearchConfig.max_candidates."""

    min_train_bars: int = 100
    """Minimum bars the fold's train window must have to attempt generation."""

    def __post_init__(self) -> None:
        if self.candidate_count < 1:
            raise ValueError("AIWalkForwardConfig.candidate_count must be >= 1")
        if self.min_train_bars < 1:
            raise ValueError("AIWalkForwardConfig.min_train_bars must be >= 1")


@dataclass
class FoldProvenance:
    """Auditable provenance for one fold's candidate generation (Phase 15, Step 11).

    Captures the train-only nature of every fold: which provider was asked, how
    many bars of train data were available, candidate counts, provider errors,
    and the selected spec's identity. No validation metrics leak in -- this is
    purely about the generation/selection side.
    """

    provider_name: str = ""
    train_rows: int = 0
    train_start: str = ""
    train_end: str = ""
    candidate_count: int = 0
    valid_candidate_count: int = 0
    selected_spec_name: str = ""
    generation_status: str = "completed"
    """'completed', 'no_valid_candidates', or 'insufficient_train_data'."""
    provider_errors: list = field(default_factory=list)


def build_generation_context(
    dataset: HistoricalDataset,
    spec_required_warmup: int = 0,
    variant_index: int = 0,
) -> GenerationContext:
    """Build a TRAIN-only GenerationContext with enriched aggregate statistics.

    Phase 15, Step 3: the context handed to the AI provider contains ONLY
    information derivable from the train window. It NEVER includes validation
    prices, returns, trades, drawdowns, or any out-of-sample signal.

    Enrichment over the base GenerationContext:
      - volatility (std of daily returns)
      - trend (slope of linear fit to close)
      - regime (bull/bear based on trend sign)
      - high/low range statistics
      - volume statistics
      - the spec's required warmup
    """
    base = GenerationContext.from_dataset(dataset, variant_index=variant_index)
    data = dataset.data
    summary: dict = dict(base.feature_summary)

    if data is not None and len(data):
        close = data["close"]
        summary["close_mean"] = round(float(close.mean()), 6)
        summary["close_std"] = round(float(close.std()), 6)
        pct = close.pct_change().dropna()
        if len(pct) > 1:
            summary["volatility_daily"] = round(float(pct.std()), 6)
        n = len(close)
        if n > 1:
            x = range(n)
            x_mean = (n - 1) / 2.0
            y_mean = float(close.mean())
            denom = sum((xi - x_mean) ** 2 for xi in x)
            if denom > 0:
                slope = sum(
                    (xi - x_mean) * (float(close.iloc[xi]) - y_mean)
                    for xi in x
                ) / denom
            else:
                slope = 0.0
            summary["close_start"] = round(float(close.iloc[0]), 6)
            summary["close_end"] = round(float(close.iloc[-1]), 6)
            summary["trend_slope_daily"] = round(slope, 6)
            if float(close.iloc[0]) != 0:
                total_ret = (
                    float(close.iloc[-1]) - float(close.iloc[0])
                ) / float(close.iloc[0])
            else:
                total_ret = 0.0
            summary["total_return"] = round(total_ret, 6)
            summary["regime"] = (
                "bull" if total_ret > 0 else ("bear" if total_ret < 0 else "flat")
            )
        summary["close_min"] = round(float(close.min()), 6)
        summary["close_max"] = round(float(close.max()), 6)
        vol = data["volume"]
        summary["volume_mean"] = round(float(vol.mean()), 6)
        summary["volume_std"] = round(float(vol.std()), 6)

        summary["spec_required_warmup"] = spec_required_warmup
    return replace(base, feature_summary=summary)


@dataclass
class _FoldGenerationOutcome:
    """Internal result of one fold's TRAIN-only candidate generation+selection."""

    selected_spec: Optional[StrategySpec]
    train_report: Optional[object]
    provenance: FoldProvenance
    generation_status: str


def _generate_and_select_fold(
    fold,
    provider: StrategyProposalProvider,
    research_config: ResearchConfig,
    backtest_config: BacktestConfig,
    ai_config: AIWalkForwardConfig,
) -> _FoldGenerationOutcome:
    """Run the TRAIN-only generation -> validate -> backtest -> filter -> rank -> select.

    Receives ``fold.train_dataset`` only. Validation data is never used.
    Returns the selected spec (or None) to be validated on the fold's
    validation window by the Phase 14 warm-up path.
    """
    n_train = (
        len(fold.train_dataset.data)
        if fold.train_dataset.data is not None
        else 0
    )
    prov = FoldProvenance(
        provider_name=provider.name,
        train_rows=n_train,
        train_start=str(fold.train_dataset.data.index[0]) if n_train else "",
        train_end=str(fold.train_dataset.data.index[-1]) if n_train else "",
    )

    if n_train < ai_config.min_train_bars:
        prov.generation_status = "insufficient_train_data"
        prov.candidate_count = 0
        return _FoldGenerationOutcome(
            selected_spec=None,
            train_report=None,
            provenance=prov,
            generation_status="insufficient_train_data",
        )

    # Engine wired to the provider; research_candidates sees train data only.
    engine = StrategyResearchEngine(provider, research_config)
    candidate_count = min(ai_config.candidate_count, research_config.max_candidates)

    # Context derived from TRAIN only -- never validation data.
    ctx = build_generation_context(fold.train_dataset)

    train_report = engine.research_candidates(
        fold.train_dataset, ctx, candidate_count, backtest_config
    )

    prov.candidate_count = len(train_report.candidates)
    prov.valid_candidate_count = len(train_report.passed)

    for cand in train_report.candidates:
        if cand.status == SpecStatus.PROVIDER_ERROR.value:
            prov.provider_errors.append(cand.error)

    selected = _select_top_candidate(train_report)
    if selected is None:
        prov.generation_status = "no_valid_candidates"
        prov.selected_spec_name = ""
        return _FoldGenerationOutcome(
            selected_spec=None,
            train_report=train_report,
            provenance=prov,
            generation_status="no_valid_candidates",
        )

    spec, _ = selected
    prov.selected_spec_name = spec.name
    prov.generation_status = "completed"
    return _FoldGenerationOutcome(
        selected_spec=spec,
        train_report=train_report,
        provenance=prov,
        generation_status="completed",
    )


def walk_forward_ai_research(
    dataset: HistoricalDataset,
    provider: StrategyProposalProvider,
    wf_config: WalkForwardConfig,
    research_config: ResearchConfig,
    backtest_config: BacktestConfig,
    ai_config: AIWalkForwardConfig,
) -> WalkForwardReport:
    """AI-driven walk-forward research loop (Phase 15).

    For each fold:
      1. Generate candidates using TRAIN-only context
      2. Validate, backtest, evaluate, filter, rank on TRAIN only
      3. Select winner based on TRAIN metrics only
      4. Backtest selected spec on VALIDATION (untouched until now)

    The validation window NEVER influences candidate generation or selection.
    """
    from .walk_forward import Fold, FoldResult, WalkForwardReport, STATUS_NO_CANDIDATE, STATUS_INVALID

    disclaimer = (
        "Walk-forward validation is historical evidence of robustness, "
        "NOT a guarantee of future profitability."
    )

    folds = list(generate_folds(dataset, wf_config))
    fold_results: list = []
    all_provenances: list = []

    for fold in folds:
        # Step 1: TRAIN-only generation and selection
        outcome = _generate_and_select_fold(
            fold, provider, research_config, backtest_config, ai_config
        )
        all_provenances.append(outcome.provenance)

        if outcome.selected_spec is None:
            fold_results.append(
                FoldResult(
                    fold_id=fold.fold_id,
                    status=STATUS_NO_CANDIDATE,
                    error=f"generation_status={outcome.generation_status}",
                    selected_spec=None,
                    train_start=str(fold.train_dataset.data.index[0]) if fold.train_dataset.data is not None and len(fold.train_dataset.data) else "",
                    train_end=str(fold.train_dataset.data.index[-1]) if fold.train_dataset.data is not None and len(fold.train_dataset.data) else "",
                    validation_start=str(fold.validation_run_dataset.data.index[fold.warmup_bars]) if fold.validation_run_dataset.data is not None and len(fold.validation_run_dataset.data) > fold.warmup_bars else "",
                    validation_end=str(fold.validation_run_dataset.data.index[-1]) if fold.validation_run_dataset.data is not None and len(fold.validation_run_dataset.data) else "",
                    unavailable_metrics=["train_evaluation", "validation_evaluation"],
                )
            )
            continue

        spec = outcome.selected_spec
        train_evaluation = None
        if outcome.train_report and outcome.train_report.ranking:
            top_name = outcome.train_report.ranking[0].key
            for cand in outcome.train_report.passed:
                if cand.spec_name == top_name:
                    train_evaluation = cand.evaluation
                    break

        # Guard: selected spec must fit within the configured warm-up budget.
        spec_required = required_warmup_bars(spec)
        if spec_required > wf_config.warmup_bars:
            fold_results.append(
                FoldResult(
                    fold_id=fold.fold_id,
                    status=STATUS_INVALID,
                    error=(
                        f"selected spec requires {spec_required} warm-up bars; "
                        f"config provides {wf_config.warmup_bars}"
                    ),
                    selected_spec=spec,
                    train_start=str(fold.train_dataset.data.index[0]) if fold.train_dataset.data is not None and len(fold.train_dataset.data) else "",
                    train_end=str(fold.train_dataset.data.index[-1]) if fold.train_dataset.data is not None and len(fold.train_dataset.data) else "",
                    validation_start=str(fold.validation_run_dataset.data.index[fold.warmup_bars]) if fold.validation_run_dataset.data is not None and len(fold.validation_run_dataset.data) > fold.warmup_bars else "",
                    validation_end=str(fold.validation_run_dataset.data.index[-1]) if fold.validation_run_dataset.data is not None and len(fold.validation_run_dataset.data) else "",
                    train_evaluation=train_evaluation,
                    unavailable_metrics=["validation_evaluation"],
                )
            )
            continue

        # Step 2: Validate the selected spec
        try:
            validate_spec(spec, fold.validation_run_dataset)
        except Exception as e:
            fold_results.append(
                FoldResult(
                    fold_id=fold.fold_id,
                    status=STATUS_INVALID,
                    error=str(e),
                    selected_spec=spec,
                    train_start=str(fold.train_dataset.data.index[0]) if fold.train_dataset.data is not None and len(fold.train_dataset.data) else "",
                    train_end=str(fold.train_dataset.data.index[-1]) if fold.train_dataset.data is not None and len(fold.train_dataset.data) else "",
                    validation_start=str(fold.validation_run_dataset.data.index[fold.warmup_bars]) if fold.validation_run_dataset.data is not None and len(fold.validation_run_dataset.data) > fold.warmup_bars else "",
                    validation_end=str(fold.validation_run_dataset.data.index[-1]) if fold.validation_run_dataset.data is not None and len(fold.validation_run_dataset.data) else "",
                    train_evaluation=train_evaluation,
                    unavailable_metrics=["validation_evaluation"],
                )
            )
            continue

        # Step 3: Assess on VALIDATION (warm-up context + window)
        fold_result = _assess_fold(
            fold,
            spec,
            backtest_config,
            wf_config,
            train_evaluation=train_evaluation,
        )
        fold_results.append(fold_result)

    summary = compute_walk_forward_summary(fold_results, wf_config)
    warnings = _collect_wf_warnings(fold_results, summary, wf_config)

    first_spec = next(
        (fr.selected_spec for fr in fold_results if fr.selected_spec is not None),
        None,
    )

    return WalkForwardReport(
        kind="ai_research",
        spec_name=first_spec.name if first_spec is not None else "no-candidate",
        symbol=dataset.symbol,
        timeframe=dataset.timeframe,
        mode=wf_config.mode,
        config=wf_config,
        folds=fold_results,
        summary=summary,
        warnings=warnings,
        notes=[disclaimer],
    )


def walk_forward_ai_validate(
    dataset: HistoricalDataset,
    provider: StrategyProposalProvider,
    wf_config: WalkForwardConfig,
    backtest_config: BacktestConfig,
    ai_config: AIWalkForwardConfig,
) -> WalkForwardReport:
    """AI-driven walk-forward validation with default research config.

    Convenience wrapper that uses default ResearchConfig.
    """
    return walk_forward_ai_research(
        dataset=dataset,
        provider=provider,
        wf_config=wf_config,
        research_config=ResearchConfig(),
        backtest_config=backtest_config,
        ai_config=ai_config,
    )
