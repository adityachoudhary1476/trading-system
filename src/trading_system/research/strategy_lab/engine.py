"""Bounded AI strategy research engine (Phase 13, Step 9).

Pipeline (per candidate):

    generate -> validate -> backtest -> evaluate -> filter -> rank

Hard architectural guarantees:

  * BOUNDED: ``candidate_count`` is clamped to ``ResearchConfig.max_candidates``.
    There is no autonomous loop, no re-generation, no self-improvement cycle.
  * OFFLINE: the engine makes NO network calls. An AI provider is injected at
    construction; tests use DeterministicStrategyProvider.
  * SEPARATION FROM EXECUTION: this module never imports Broker, PaperBroker,
    FYERS, or anything in trading_system.execution. Candidates flow only to the
    deterministic backtester. Invalid specs NEVER reach the backtester.
  * DETERMINISTIC: same provider, dataset, and config -> same report.

A ResearchReport is a research artifact. Filter pass/fail is a quality gate,
never a claim of future profitability.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

from ..backtester import BacktestConfig, BacktestResult, run_backtest
from ..dataset import HistoricalDataset
from ..risk import RiskConfig
from ..walkforward import split_dataset
from ...models.base import ModelProviderError
from .evaluation import StrategyEvaluation, evaluate_spec
from .filters import FilterOutcome, QualityFilterConfig, apply_quality_filter
from .interpreter import build_strategy
from .providers import GenerationContext, StrategyProposalProvider
from .ranking import RankingConfig, rank_candidates
from .spec import SpecStatus, StrategySpec
from .validation import StrategyValidationError, require_valid

__all__ = [
    "ResearchConfig",
    "CandidateOutcome",
    "ResearchReport",
    "HoldoutEvaluation",
    "StrategyResearchEngine",
]

# Absolute upper bound on candidates per research run (belt and braces on top
# of the configurable limit — the engine can never be tricked into a huge loop).
HARD_MAX_CANDIDATES = 32


@dataclass
class ResearchConfig:
    max_candidates: int = 8
    min_bars: int = 100
    # Optional instrument allowlist (None = any symbol the dataset carries).
    allowed_symbols: Optional[frozenset] = None
    quality_filter: QualityFilterConfig = field(default_factory=QualityFilterConfig)
    ranking: RankingConfig = field(default_factory=RankingConfig)

    def __post_init__(self) -> None:
        if self.max_candidates < 1:
            raise ValueError("max_candidates must be >= 1")
        self.max_candidates = min(self.max_candidates, HARD_MAX_CANDIDATES)


@dataclass
class CandidateOutcome:
    """Full audit trail for one candidate (spec or failure, never silent)."""

    variant_index: int
    status: str = SpecStatus.PROVIDER_ERROR.value
    spec: Optional[StrategySpec] = None
    spec_errors: list = field(default_factory=list)
    error: str = ""
    backtest: Optional[BacktestResult] = None
    evaluation: Optional[StrategyEvaluation] = None
    filter_outcome: Optional[FilterOutcome] = None

    @property
    def spec_name(self) -> str:
        return self.spec.name if self.spec is not None else ""


@dataclass
class ResearchReport:
    symbol: str
    timeframe: str
    rows: int
    requested_candidates: int
    candidates: list = field(default_factory=list)
    passed: list = field(default_factory=list)
    ranking: list = field(default_factory=list)
    notes: list = field(default_factory=list)


@dataclass
class HoldoutEvaluation:
    """Out-of-sample check for one candidate on the held-out (test) split."""

    spec_name: str
    evaluation: Optional[StrategyEvaluation] = None
    error: str = ""


def merged_backtest_config(spec: StrategySpec, base: BacktestConfig) -> BacktestConfig:
    """Merge a spec's risk/sizing into a BacktestConfig (documented policy).

    * spec stop-loss/take-profit override the base config ONLY when the spec
      actually sets them; otherwise the base values are kept.
    * allow_short = spec.risk.allow_short AND base.risk.allow_short — the
      engine can never GRANT short permission the backtest config denies.
    * position sizing comes from the spec (bounded by pydantic validation).
    """
    risk = RiskConfig(
        max_position_size=(
            spec.position_sizing.max_position_size
            if spec.position_sizing.max_position_size is not None
            else base.risk.max_position_size
        ),
        max_allocation_pct=spec.position_sizing.max_allocation_pct,
        allow_short=bool(spec.risk.allow_short and base.risk.allow_short),
        leverage=base.risk.leverage,
        stop_loss_pct=(
            spec.risk.stop_loss_pct
            if spec.risk.stop_loss_pct is not None
            else base.risk.stop_loss_pct
        ),
        take_profit_pct=(
            spec.risk.take_profit_pct
            if spec.risk.take_profit_pct is not None
            else base.risk.take_profit_pct
        ),
        max_loss_per_trade_pct=(
            spec.risk.max_loss_per_trade_pct
            if spec.risk.max_loss_per_trade_pct is not None
            else base.risk.max_loss_per_trade_pct
        ),
        max_positions=base.risk.max_positions,
    )
    return replace(base, risk=risk)


class StrategyResearchEngine:
    """Bounded, offline generate->validate->backtest->evaluate->filter->rank."""

    def __init__(
        self,
        provider: StrategyProposalProvider,
        config: Optional[ResearchConfig] = None,
    ) -> None:
        if not isinstance(provider, StrategyProposalProvider):
            raise TypeError(
                "provider must implement StrategyProposalProvider (got %s)"
                % type(provider).__name__
            )
        self._provider = provider
        self._config = config or ResearchConfig()

    @property
    def provider(self) -> StrategyProposalProvider:
        return self._provider

    @property
    def config(self) -> ResearchConfig:
        return self._config

    # ------------------------------------------------------------------ #
    def research_candidates(
        self,
        dataset: HistoricalDataset,
        generation_context: GenerationContext,
        candidate_count: int,
        backtest_config: BacktestConfig,
    ) -> ResearchReport:
        """Run ONE bounded research pass. Deterministic and offline."""
        n = int(candidate_count)
        if n < 0:
            raise ValueError("candidate_count must be >= 0")
        n = min(n, self._config.max_candidates, HARD_MAX_CANDIDATES)

        rows = 0 if dataset.data is None else len(dataset.data)
        report = ResearchReport(
            symbol=dataset.symbol,
            timeframe=dataset.timeframe,
            rows=rows,
            requested_candidates=n,
            notes=[
                "Quality filters are research-quality gates, NOT proof of "
                "future profitability."
            ],
        )

        for i in range(n):
            ctx = generation_context.with_variant(i)
            outcome = CandidateOutcome(variant_index=i)

            # 1) generate — provider failures are recorded, never fatal.
            try:
                spec = self._provider.generate_strategy(ctx)
                outcome.status = SpecStatus.GENERATED.value
                outcome.spec = spec
            except ModelProviderError as e:
                outcome.status = SpecStatus.PROVIDER_ERROR.value
                outcome.error = str(e)
                report.candidates.append(outcome)
                continue
            except Exception as e:  # noqa: BLE001 - a provider bug must not crash research
                outcome.status = SpecStatus.PROVIDER_ERROR.value
                outcome.error = f"{type(e).__name__}: {e}"
                report.candidates.append(outcome)
                continue

            # 2) validate — invalid specs NEVER reach the backtester.
            errors = self._validation_errors(spec, dataset)
            if errors:
                outcome.status = SpecStatus.INVALID.value
                outcome.spec_errors = errors
                report.candidates.append(outcome)
                continue
            outcome.status = SpecStatus.VALIDATED.value

            # 3) backtest through the EXISTING deterministic engine.
            merged = merged_backtest_config(spec, backtest_config)
            try:
                result = run_backtest(dataset, build_strategy(spec), merged)
            except ValueError as e:
                outcome.status = SpecStatus.BACKTEST_FAILED.value
                outcome.error = str(e)
                report.candidates.append(outcome)
                continue
            outcome.backtest = result
            outcome.status = SpecStatus.BACKTESTED.value

            # 4) evaluate.
            outcome.evaluation = evaluate_spec(spec, result)
            outcome.status = SpecStatus.EVALUATED.value

            # 5) filter.
            outcome.filter_outcome = apply_quality_filter(
                outcome.evaluation,
                self._config.quality_filter,
                spec_errors=[],
                dataset_rows=rows,
            )
            if outcome.filter_outcome.passed:
                report.passed.append(outcome)

            report.candidates.append(outcome)

        # 6) rank the survivors deterministically.
        evaluations = {c.spec_name: c.evaluation for c in report.passed}
        report.ranking = rank_candidates(evaluations, self._config.ranking)
        return report

    # ------------------------------------------------------------------ #
    def research_with_holdout(
        self,
        dataset: HistoricalDataset,
        generation_context: GenerationContext,
        candidate_count: int,
        backtest_config: BacktestConfig,
        train_frac: float = 0.7,
    ) -> dict:
        """Research on a TRAIN split, then re-test survivors OUT-OF-SAMPLE.

        Uses the existing chronological ``split_dataset`` (no shuffling, no
        leakage). Selection happens ONLY on the train split; the test split is
        scored afterwards so candidates are not ranked on the same data used to
        propose/select them. This is the Phase 13 foundation for overfitting
        protection (full walk-forward is a later phase).
        """
        split = split_dataset(dataset, train_frac=train_frac)
        train_report = self.research_candidates(
            split.train, generation_context, candidate_count, backtest_config
        )
        out_of_sample: list = []
        for cand in train_report.passed:
            item = HoldoutEvaluation(spec_name=cand.spec_name)
            try:
                # The test split must independently satisfy data sufficiency.
                errors = self._validation_errors(cand.spec, split.test)
                if errors:
                    item.error = "; ".join(errors)
                else:
                    merged = merged_backtest_config(cand.spec, backtest_config)
                    oos_result = run_backtest(
                        split.test, build_strategy(cand.spec), merged
                    )
                    item.evaluation = evaluate_spec(cand.spec, oos_result)
            except ValueError as e:
                item.error = str(e)
            out_of_sample.append(item)
        return {
            "train_report": train_report,
            "train_split": split.train,
            "test_split": split.test,
            "out_of_sample": out_of_sample,
        }

    # ------------------------------------------------------------------ #
    def _validation_errors(
        self, spec: StrategySpec, dataset: HistoricalDataset
    ) -> list:
        try:
            require_valid(
                spec,
                dataset,
                allowed_symbols=self._config.allowed_symbols,
                min_bars=self._config.min_bars,
            )
        except StrategyValidationError as e:
            return list(e.errors)
        return []


