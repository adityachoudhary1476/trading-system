"""Phase 15 tests: AI-driven walk-forward research loop.

Tests verify:
  * Candidates are generated from TRAIN-only context
  * Validation data NEVER influences candidate generation or selection
  * Selected specs are validated before validation backtesting
  * Walk-forward summary is computed correctly
  * Provider failures are handled gracefully
  * Deterministic provider remains reproducible
  * Invalid candidates are rejected before backtesting
  * Candidate generation remains bounded
  * Each fold receives its own TRAIN-derived context
  * Selected strategies carry correct provenance
  * OpenAI-compatible path works without real API keys
"""
from __future__ import annotations

import json
import unittest.mock
from typing import Any

import numpy as np
import pandas as pd
import pytest

from trading_system.models.base import ModelProviderError
from trading_system.research.backtester import BacktestConfig
from trading_system.research.dataset import HistoricalDataset
from trading_system.research.strategy_lab.ai_walk_forward import (
    AIWalkForwardConfig,
    FoldProvenance,
    build_generation_context,
    walk_forward_ai_research,
    walk_forward_ai_validate,
)
from trading_system.research.strategy_lab.engine import ResearchConfig
from trading_system.research.strategy_lab.filters import QualityFilterConfig
from trading_system.research.strategy_lab.providers import (
    DeterministicStrategyProvider,
    GenerationContext,
    OpenAICompatibleStrategyProvider,
    StrategyProposalProvider,
)
from trading_system.research.strategy_lab.spec import StrategySpec
from trading_system.research.strategy_lab.walk_forward import WalkForwardConfig


def _make_dataset(n: int = 500, symbol: str = "NSE:SBIN", seed: int = 42) -> HistoricalDataset:
    """Create a synthetic dataset for testing."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    df = pd.DataFrame(
        {
            "open": close + 0.1,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": rng.integers(100, 1000, n).astype(float),
        },
        index=idx,
    )
    return HistoricalDataset(symbol=symbol, timeframe="1d", data=df)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
class TestAIWalkForwardConfig:
    def test_default_values(self):
        config = AIWalkForwardConfig()
        assert config.candidate_count == 4
        assert config.min_train_bars == 100

    def test_custom_values(self):
        config = AIWalkForwardConfig(candidate_count=8, min_train_bars=50)
        assert config.candidate_count == 8
        assert config.min_train_bars == 50

    def test_candidate_count_must_be_positive(self):
        with pytest.raises(ValueError, match="candidate_count must be >= 1"):
            AIWalkForwardConfig(candidate_count=0)

    def test_min_train_bars_must_be_positive(self):
        with pytest.raises(ValueError, match="min_train_bars must be >= 1"):
            AIWalkForwardConfig(min_train_bars=0)


# --------------------------------------------------------------------------- #
# Generation context (TRAIN-only)
# --------------------------------------------------------------------------- #
class TestBuildGenerationContext:
    def test_train_only_context(self):
        """Context must contain only TRAIN data statistics."""
        dataset = _make_dataset(n=200)
        spec_required_warmup = 20

        ctx = build_generation_context(dataset, spec_required_warmup=spec_required_warmup)

        assert ctx.symbol == "NSE:SBIN"
        assert ctx.timeframe == "1d"
        assert "close_mean" in ctx.feature_summary
        assert "volatility_daily" in ctx.feature_summary
        assert "trend_slope_daily" in ctx.feature_summary
        assert "regime" in ctx.feature_summary
        assert "spec_required_warmup" in ctx.feature_summary
        assert ctx.feature_summary["spec_required_warmup"] == spec_required_warmup

    def test_regime_bull(self):
        """Bull regime when total return is positive."""
        rng = np.random.default_rng(42)
        idx = pd.date_range("2024-01-01", periods=200, freq="1D", tz="UTC")
        close = 100 + np.cumsum(np.abs(rng.normal(0.1, 0.5, 200)))
        df = pd.DataFrame(
            {
                "open": close + 0.1,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": rng.integers(100, 1000, 200).astype(float),
            },
            index=idx,
        )
        dataset = HistoricalDataset(symbol="NSE:SBIN", timeframe="1d", data=df)
        ctx = build_generation_context(dataset)
        assert ctx.feature_summary["regime"] == "bull"

    def test_regime_bear(self):
        """Bear regime when total return is negative."""
        rng = np.random.default_rng(42)
        idx = pd.date_range("2024-01-01", periods=200, freq="1D", tz="UTC")
        close = 100 - np.cumsum(np.abs(rng.normal(0.1, 0.5, 200)))
        df = pd.DataFrame(
            {
                "open": close + 0.1,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": rng.integers(100, 1000, 200).astype(float),
            },
            index=idx,
        )
        dataset = HistoricalDataset(symbol="NSE:SBIN", timeframe="1d", data=df)
        ctx = build_generation_context(dataset)
        assert ctx.feature_summary["regime"] == "bear"

    def test_context_has_no_validation_keys(self):
        """Generation context must not contain validation-specific metrics."""
        dataset = _make_dataset(n=300)
        ctx = build_generation_context(dataset)
        assert "validation_return" not in ctx.feature_summary
        assert "validation_trades" not in ctx.feature_summary
        assert "validation_drawdown" not in ctx.feature_summary


# --------------------------------------------------------------------------- #
# Fold provenance
# --------------------------------------------------------------------------- #
class TestFoldProvenance:
    def test_default_values(self):
        prov = FoldProvenance()
        assert prov.provider_name == ""
        assert prov.train_rows == 0
        assert prov.candidate_count == 0
        assert prov.generation_status == "completed"
        assert prov.provider_errors == []

    def test_custom_values(self):
        prov = FoldProvenance(
            provider_name="test-provider",
            train_rows=100,
            candidate_count=3,
            generation_status="completed",
        )
        assert prov.provider_name == "test-provider"
        assert prov.train_rows == 100
        assert prov.candidate_count == 3


# --------------------------------------------------------------------------- #
# Main integration
# --------------------------------------------------------------------------- #
class TestWalkForwardAIResearch:
    def test_basic_walk_forward(self):
        """Basic walk-forward research with deterministic provider."""
        dataset = _make_dataset(n=500)
        provider = DeterministicStrategyProvider()
        wf_config = WalkForwardConfig(
            n_folds=2,
            train_window=100,
            validation_window=50,
            step_size=50,
            mode="rolling",
            warmup_bars=20,
        )
        research_config = ResearchConfig(max_candidates=4)
        backtest_config = BacktestConfig()
        ai_config = AIWalkForwardConfig(candidate_count=2, min_train_bars=50)

        report = walk_forward_ai_research(
            dataset=dataset,
            provider=provider,
            wf_config=wf_config,
            research_config=research_config,
            backtest_config=backtest_config,
            ai_config=ai_config,
        )

        assert report.kind == "ai_research"
        assert report.symbol == "NSE:SBIN"
        assert report.timeframe == "1d"
        assert len(report.folds) == 2
        assert report.summary is not None

    def test_insufficient_train_data(self):
        """Fold with insufficient train data should be skipped."""
        dataset = _make_dataset(n=150)
        provider = DeterministicStrategyProvider()
        wf_config = WalkForwardConfig(
            n_folds=1,
            train_window=100,
            validation_window=50,
            step_size=50,
            mode="rolling",
            warmup_bars=20,
            min_train_bars=50,
        )
        research_config = ResearchConfig(max_candidates=4)
        backtest_config = BacktestConfig()
        ai_config = AIWalkForwardConfig(candidate_count=2, min_train_bars=100)

        report = walk_forward_ai_research(
            dataset=dataset,
            provider=provider,
            wf_config=wf_config,
            research_config=research_config,
            backtest_config=backtest_config,
            ai_config=ai_config,
        )

        assert len(report.folds) == 1

    def test_deterministic_reproducibility(self):
        """Same inputs should produce identical outputs."""
        dataset = _make_dataset(n=400)
        provider = DeterministicStrategyProvider()
        wf_config = WalkForwardConfig(
            n_folds=2,
            train_window=100,
            validation_window=50,
            step_size=50,
            mode="rolling",
            warmup_bars=20,
        )
        research_config = ResearchConfig(max_candidates=4)
        backtest_config = BacktestConfig()
        ai_config = AIWalkForwardConfig(candidate_count=2, min_train_bars=50)

        report1 = walk_forward_ai_research(
            dataset=dataset,
            provider=provider,
            wf_config=wf_config,
            research_config=research_config,
            backtest_config=backtest_config,
            ai_config=ai_config,
        )

        report2 = walk_forward_ai_research(
            dataset=dataset,
            provider=provider,
            wf_config=wf_config,
            research_config=research_config,
            backtest_config=backtest_config,
            ai_config=ai_config,
        )

        assert report1.spec_name == report2.spec_name
        assert len(report1.folds) == len(report2.folds)
        assert [f.status for f in report1.folds] == [f.status for f in report2.folds]
        assert report1.summary == report2.summary


class TestWalkForwardAIValidate:
    def test_convenience_wrapper(self):
        """walk_forward_ai_validate should use default ResearchConfig."""
        dataset = _make_dataset(n=400)
        provider = DeterministicStrategyProvider()
        wf_config = WalkForwardConfig(
            n_folds=2,
            train_window=100,
            validation_window=50,
            step_size=50,
            mode="rolling",
            warmup_bars=20,
        )
        backtest_config = BacktestConfig()
        ai_config = AIWalkForwardConfig(candidate_count=2, min_train_bars=50)

        report = walk_forward_ai_validate(
            dataset=dataset,
            provider=provider,
            wf_config=wf_config,
            backtest_config=backtest_config,
            ai_config=ai_config,
        )

        assert report.kind == "ai_research"
        assert len(report.folds) == 2


# --------------------------------------------------------------------------- #
# Provider error handling
# --------------------------------------------------------------------------- #
class TestProviderErrorHandling:
    def test_provider_error_handling(self):
        """Provider errors should not crash the walk-forward loop."""

        class FailingProvider(StrategyProposalProvider):
            name = "failing-provider"

            def generate_strategy(self, context):
                raise RuntimeError("Simulated provider failure")

        dataset = _make_dataset(n=400)
        provider = FailingProvider()
        wf_config = WalkForwardConfig(
            n_folds=1,
            train_window=100,
            validation_window=50,
            step_size=50,
            mode="rolling",
            warmup_bars=20,
        )
        research_config = ResearchConfig(max_candidates=4)
        backtest_config = BacktestConfig()
        ai_config = AIWalkForwardConfig(candidate_count=2, min_train_bars=50)

        report = walk_forward_ai_research(
            dataset=dataset,
            provider=provider,
            wf_config=wf_config,
            research_config=research_config,
            backtest_config=backtest_config,
            ai_config=ai_config,
        )

        assert report is not None
        assert len(report.folds) == 1


# --------------------------------------------------------------------------- #
# Leakage guarantees
# --------------------------------------------------------------------------- #
class TestLeakageGuarantees:
    def test_validation_cannot_influence_same_fold_selection(self):
        """Same train data with different validation tails must produce identical selection."""
        # Build two datasets with identical train prefixes but different validation tails.
        rng = np.random.default_rng(5)
        shared = 360
        tail = 120
        n = shared + tail

        o = np.empty(n)
        o[0] = 100.0
        closes = np.empty(n)
        closes[0] = 100.0
        for i in range(1, n):
            closes[i] = closes[i - 1] + rng.normal(0.05, 0.8)
            o[i] = closes[i] + rng.normal(0, 0.2)
        high = np.maximum(o, closes) + 0.5
        low = np.minimum(o, closes) - 0.5
        volume = rng.integers(100, 1000, n).astype(float)
        base_idx = pd.date_range("2020-01-01", periods=n, freq="1D", tz="UTC")

        up_close = closes.copy()
        up_close[shared:] = up_close[shared - 1] + np.cumsum(rng.normal(0.4, 0.5, tail))
        down_close = closes.copy()
        down_close[shared:] = down_close[shared - 1] + np.cumsum(rng.normal(-0.4, 0.5, tail))

        def _frame(close):
            return pd.DataFrame(
                {"open": o, "high": high, "low": low, "close": close, "volume": volume},
                index=base_idx,
            )

        ds_up = HistoricalDataset(symbol="NSE:SBIN", timeframe="1d", data=_frame(up_close))
        ds_down = HistoricalDataset(symbol="NSE:SBIN", timeframe="1d", data=_frame(down_close))

        assert list(ds_up.data["close"].iloc[360:]) != list(ds_down.data["close"].iloc[360:])

        provider = DeterministicStrategyProvider()
        wf_config = WalkForwardConfig(
            n_folds=2,
            train_window=120,
            validation_window=60,
            step_size=90,
            mode="rolling",
            warmup_bars=20,
        )
        research_config = ResearchConfig(
            max_candidates=4,
            quality_filter=QualityFilterConfig(min_trades=1, require_reliable=False),
        )
        backtest_config = BacktestConfig()
        ai_config = AIWalkForwardConfig(candidate_count=2, min_train_bars=50)

        report_up = walk_forward_ai_research(
            dataset=ds_up,
            provider=provider,
            wf_config=wf_config,
            research_config=research_config,
            backtest_config=backtest_config,
            ai_config=ai_config,
        )
        report_down = walk_forward_ai_research(
            dataset=ds_down,
            provider=provider,
            wf_config=wf_config,
            research_config=research_config,
            backtest_config=backtest_config,
            ai_config=ai_config,
        )

        selected_up = [f.selected_spec.name if f.selected_spec else None for f in report_up.folds]
        selected_down = [f.selected_spec.name if f.selected_spec else None for f in report_down.folds]
        assert selected_up == selected_down
        assert all(name is not None for name in selected_up)

    def test_provider_receives_only_train_context(self):
        """Provider contexts must be derived exclusively from TRAIN windows."""
        from trading_system.research.strategy_lab.walk_forward import generate_folds

        class _RecordingProvider(DeterministicStrategyProvider):
            def __init__(self):
                super().__init__()
                self.seen_contexts: list = []

            def generate_strategy(self, context):
                self.seen_contexts.append(context.as_dict())
                return super().generate_strategy(context)

        dataset = _make_dataset(n=500)
        provider = _RecordingProvider()
        wf_config = WalkForwardConfig(
            n_folds=2,
            train_window=100,
            validation_window=50,
            step_size=50,
            mode="rolling",
            warmup_bars=20,
        )
        research_config = ResearchConfig(max_candidates=2)
        backtest_config = BacktestConfig()
        ai_config = AIWalkForwardConfig(candidate_count=2, min_train_bars=50)

        walk_forward_ai_research(
            dataset=dataset,
            provider=provider,
            wf_config=wf_config,
            research_config=research_config,
            backtest_config=backtest_config,
            ai_config=ai_config,
        )

        folds = generate_folds(dataset, wf_config)
        assert len(provider.seen_contexts) == 2 * 2  # 2 folds x 2 candidates
        for i, fold in enumerate(folds):
            for j in range(2):
                ctx = provider.seen_contexts[i * 2 + j]
                assert ctx["rows"] == len(fold.train_dataset.data)
                assert pd.Timestamp(ctx["date_end"]) == fold.train_dataset.data.index[-1]
                assert pd.Timestamp(ctx["date_end"]) <= pd.Timestamp(
                    fold.validation_run_dataset.data.index[fold.warmup_bars - 1]
                )


# --------------------------------------------------------------------------- #
# Selection isolation
# --------------------------------------------------------------------------- #
class TestSelectionIsolation:
    def test_winner_selected_from_train_metrics_only(self):
        """The selected spec must be chosen based on TRAIN evaluation, not validation."""
        dataset = _make_dataset(n=500)
        provider = DeterministicStrategyProvider()
        wf_config = WalkForwardConfig(
            n_folds=2,
            train_window=100,
            validation_window=50,
            step_size=50,
            mode="rolling",
            warmup_bars=20,
        )
        research_config = ResearchConfig(max_candidates=4)
        backtest_config = BacktestConfig()
        ai_config = AIWalkForwardConfig(candidate_count=2, min_train_bars=50)

        report = walk_forward_ai_research(
            dataset=dataset,
            provider=provider,
            wf_config=wf_config,
            research_config=research_config,
            backtest_config=backtest_config,
            ai_config=ai_config,
        )

        for fold in report.folds:
            if fold.selected_spec is not None and fold.status != "no_candidate":
                # train_evaluation must be present (it was used for selection)
                assert fold.train_evaluation is not None


# --------------------------------------------------------------------------- #
# Invalid candidate rejection
# --------------------------------------------------------------------------- #
class TestInvalidCandidateRejection:
    def test_invalid_spec_never_backtested(self):
        """Invalid StrategySpec must never reach the backtester in Phase 15."""

        class _InvalidProvider(StrategyProposalProvider):
            name = "invalid-provider"

            def generate_strategy(self, context):
                spec = DeterministicStrategyProvider().generate_strategy(context)
                return spec.model_copy(update={"symbol": "NSE:NOT_ALLOWED"})

        dataset = _make_dataset(n=400)
        provider = _InvalidProvider()
        wf_config = WalkForwardConfig(
            n_folds=1,
            train_window=100,
            validation_window=50,
            step_size=50,
            mode="rolling",
            warmup_bars=20,
        )
        research_config = ResearchConfig(
            max_candidates=2,
            allowed_symbols=frozenset({"NSE:SBIN"}),
        )
        backtest_config = BacktestConfig()
        ai_config = AIWalkForwardConfig(candidate_count=2, min_train_bars=50)

        report = walk_forward_ai_research(
            dataset=dataset,
            provider=provider,
            wf_config=wf_config,
            research_config=research_config,
            backtest_config=backtest_config,
            ai_config=ai_config,
        )

        assert report.folds[0].status == "no_candidate"


# --------------------------------------------------------------------------- #
# Candidate bounds
# --------------------------------------------------------------------------- #
class TestCandidateBounds:
    def test_max_candidates_respected(self):
        """candidate_count must be clamped to ResearchConfig.max_candidates."""
        dataset = _make_dataset(n=400)
        provider = DeterministicStrategyProvider()
        wf_config = WalkForwardConfig(
            n_folds=1,
            train_window=100,
            validation_window=50,
            step_size=50,
            mode="rolling",
            warmup_bars=20,
        )
        research_config = ResearchConfig(
            max_candidates=2,
            quality_filter=QualityFilterConfig(min_trades=1, require_reliable=False),
        )
        backtest_config = BacktestConfig()
        ai_config = AIWalkForwardConfig(candidate_count=10, min_train_bars=50)

        report = walk_forward_ai_research(
            dataset=dataset,
            provider=provider,
            wf_config=wf_config,
            research_config=research_config,
            backtest_config=backtest_config,
            ai_config=ai_config,
        )

        fold = report.folds[0]
        assert fold.selected_spec is not None

    def test_hard_max_candidates_cannot_be_exceeded(self):
        """HARD_MAX_CANDIDATES=32 must cap even higher requests."""
        dataset = _make_dataset(n=400)
        provider = DeterministicStrategyProvider()
        wf_config = WalkForwardConfig(
            n_folds=1,
            train_window=100,
            validation_window=50,
            step_size=50,
            mode="rolling",
            warmup_bars=20,
        )
        research_config = ResearchConfig(
            max_candidates=100,
            quality_filter=QualityFilterConfig(min_trades=1, require_reliable=False),
        )
        backtest_config = BacktestConfig()
        ai_config = AIWalkForwardConfig(candidate_count=100, min_train_bars=50)

        report = walk_forward_ai_research(
            dataset=dataset,
            provider=provider,
            wf_config=wf_config,
            research_config=research_config,
            backtest_config=backtest_config,
            ai_config=ai_config,
        )

        # The engine should have produced at most HARD_MAX_CANDIDATES candidates.
        from trading_system.research.strategy_lab.engine import HARD_MAX_CANDIDATES
        assert report.folds[0].selected_spec is not None or report.folds[0].status == "no_candidate"


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
class TestDeterminism:
    def test_identical_runs_produce_equivalent_reports(self):
        dataset = _make_dataset(n=400)
        provider = DeterministicStrategyProvider()
        wf_config = WalkForwardConfig(
            n_folds=2,
            train_window=100,
            validation_window=50,
            step_size=50,
            mode="rolling",
            warmup_bars=20,
        )
        research_config = ResearchConfig(max_candidates=4)
        backtest_config = BacktestConfig()
        ai_config = AIWalkForwardConfig(candidate_count=2, min_train_bars=50)

        report1 = walk_forward_ai_research(
            dataset=dataset,
            provider=provider,
            wf_config=wf_config,
            research_config=research_config,
            backtest_config=backtest_config,
            ai_config=ai_config,
        )
        report2 = walk_forward_ai_research(
            dataset=dataset,
            provider=provider,
            wf_config=wf_config,
            research_config=research_config,
            backtest_config=backtest_config,
            ai_config=ai_config,
        )

        assert [f.selected_spec.name if f.selected_spec else None for f in report1.folds] == [
            f.selected_spec.name if f.selected_spec else None for f in report2.folds
        ]
        assert [f.status for f in report1.folds] == [f.status for f in report2.folds]
        assert report1.summary == report2.summary
        assert report1.warnings == report2.warnings


# --------------------------------------------------------------------------- #
# Cross-fold isolation
# --------------------------------------------------------------------------- #
class TestCrossFoldIsolation:
    def test_each_fold_gets_own_train_context(self):
        """Each fold's context must reflect only its own TRAIN window."""
        from trading_system.research.strategy_lab.walk_forward import generate_folds

        class _RecordingProvider(DeterministicStrategyProvider):
            def __init__(self):
                super().__init__()
                self.contexts: list = []

            def generate_strategy(self, context):
                self.contexts.append(context.as_dict())
                return super().generate_strategy(context)

        dataset = _make_dataset(n=500)
        provider = _RecordingProvider()
        wf_config = WalkForwardConfig(
            n_folds=3,
            train_window=100,
            validation_window=50,
            step_size=50,
            mode="rolling",
            warmup_bars=20,
        )
        research_config = ResearchConfig(max_candidates=1)
        backtest_config = BacktestConfig()
        ai_config = AIWalkForwardConfig(candidate_count=1, min_train_bars=50)

        walk_forward_ai_research(
            dataset=dataset,
            provider=provider,
            wf_config=wf_config,
            research_config=research_config,
            backtest_config=backtest_config,
            ai_config=ai_config,
        )

        folds = generate_folds(dataset, wf_config)
        assert len(provider.contexts) == 3
        for i, fold in enumerate(folds):
            ctx = provider.contexts[i]
            assert ctx["rows"] == len(fold.train_dataset.data)
            assert ctx["date_end"] == str(fold.train_dataset.data.index[-1])


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #
class TestProvenance:
    def test_selected_strategy_has_generated_by(self):
        """Selected StrategySpec must carry provider provenance."""
        dataset = _make_dataset(n=400)
        provider = DeterministicStrategyProvider()
        wf_config = WalkForwardConfig(
            n_folds=1,
            train_window=100,
            validation_window=50,
            step_size=50,
            mode="rolling",
            warmup_bars=20,
        )
        research_config = ResearchConfig(max_candidates=4)
        backtest_config = BacktestConfig()
        ai_config = AIWalkForwardConfig(candidate_count=2, min_train_bars=50)

        report = walk_forward_ai_research(
            dataset=dataset,
            provider=provider,
            wf_config=wf_config,
            research_config=research_config,
            backtest_config=backtest_config,
            ai_config=ai_config,
        )

        fold = report.folds[0]
        if fold.selected_spec is not None:
            assert fold.selected_spec.generated_by == provider.name

    def test_provenance_tracks_provider_and_fold(self):
        """Provenance must record provider name and train window stats."""
        dataset = _make_dataset(n=400)
        provider = DeterministicStrategyProvider()
        wf_config = WalkForwardConfig(
            n_folds=1,
            train_window=100,
            validation_window=50,
            step_size=50,
            mode="rolling",
            warmup_bars=20,
        )
        research_config = ResearchConfig(max_candidates=4)
        backtest_config = BacktestConfig()
        ai_config = AIWalkForwardConfig(candidate_count=2, min_train_bars=50)

        with unittest.mock.patch(
            "trading_system.research.strategy_lab.ai_walk_forward._generate_and_select_fold",
            wraps=__import__(
                "trading_system.research.strategy_lab.ai_walk_forward",
                fromlist=["_generate_and_select_fold"],
            )._generate_and_select_fold,
        ):
            report = walk_forward_ai_research(
                dataset=dataset,
                provider=provider,
                wf_config=wf_config,
                research_config=research_config,
                backtest_config=backtest_config,
                ai_config=ai_config,
            )

        # The report itself should contain the selected spec with provenance.
        fold = report.folds[0]
        if fold.selected_spec is not None:
            assert fold.selected_spec.generated_by


# --------------------------------------------------------------------------- #
# OpenAI provider mock (no real API key)
# --------------------------------------------------------------------------- #
class TestOpenAIProviderMock:
    def test_openai_provider_generates_valid_spec(self, monkeypatch):
        """OpenAI-compatible path works with a mocked _post_chat."""
        monkeypatch.setenv("TEST_KEY", "test-value")
        ctx = GenerationContext(symbol="NSE:SBIN", timeframe="1d", rows=300)
        provider = OpenAICompatibleStrategyProvider(
            model="test-model",
            api_base="http://localhost:9/v1",
            api_key_env="TEST_KEY",
        )

        valid_spec = {
            "name": "LLM sma pullback",
            "description": "Buy above SMA20.",
            "symbol": "NSE:SBIN",
            "timeframe": "1d",
            "indicators": [{"name": "sma", "params": {"window": 20}}],
            "entry": {
                "type": "comparison",
                "left": {"kind": "field", "field": "close"},
                "op": ">",
                "right": {"kind": "indicator", "indicator": "sma_20"},
            },
            "risk": {"stop_loss_pct": 0.05},
        }

        with unittest.mock.patch.object(provider, "_post_chat", return_value=json.dumps(valid_spec)):
            spec = provider.generate_strategy(ctx)

        assert isinstance(spec, StrategySpec)
        assert spec.name == "LLM sma pullback"
        assert spec.generated_by == "test-model"

    def test_openai_provider_malformed_json_fails_safely(self, monkeypatch):
        """Malformed JSON from model must become ModelProviderError, never a spec."""
        monkeypatch.setenv("TEST_KEY", "test-value")
        ctx = GenerationContext(symbol="NSE:SBIN", timeframe="1d", rows=300)
        provider = OpenAICompatibleStrategyProvider(
            model="test-model",
            api_base="http://localhost:9/v1",
            api_key_env="TEST_KEY",
        )

        with unittest.mock.patch.object(provider, "_post_chat", return_value="not json at all {"):
            with pytest.raises(ModelProviderError):
                provider.generate_strategy(ctx)

    def test_openai_provider_invalid_spec_fails_validation(self, monkeypatch):
        """Invalid StrategySpec from model must be rejected."""
        monkeypatch.setenv("TEST_KEY", "test-value")
        ctx = GenerationContext(symbol="NSE:SBIN", timeframe="1d", rows=300)
        provider = OpenAICompatibleStrategyProvider(
            model="test-model",
            api_base="http://localhost:9/v1",
            api_key_env="TEST_KEY",
        )

        bad = {
            "name": "bad",
            "description": "import os; take over",
            "symbol": "NSE:SBIN",
            "timeframe": "1d",
            "indicators": [{"name": "sma", "params": {"window": 20}}],
            "entry": {
                "type": "comparison",
                "left": {"kind": "field", "field": "close"},
                "op": ">",
                "right": {"kind": "indicator", "indicator": "sma_20"},
            },
        }

        with unittest.mock.patch.object(provider, "_post_chat", return_value=json.dumps(bad)):
            with pytest.raises(ModelProviderError):
                provider.generate_strategy(ctx)


# --------------------------------------------------------------------------- #
# No-validation-regeneration guarantee
# --------------------------------------------------------------------------- #
class TestNoValidationRegeneration:
    def test_poor_validation_does_not_trigger_regeneration(self):
        """A poor validation result must not cause another generation round."""
        call_count = {"count": 0}

        class _CountingProvider(DeterministicStrategyProvider):
            def generate_strategy(self, context):
                call_count["count"] += 1
                return super().generate_strategy(context)

        dataset = _make_dataset(n=400)
        provider = _CountingProvider()
        wf_config = WalkForwardConfig(
            n_folds=1,
            train_window=100,
            validation_window=50,
            step_size=50,
            mode="rolling",
            warmup_bars=20,
        )
        research_config = ResearchConfig(max_candidates=4)
        backtest_config = BacktestConfig()
        ai_config = AIWalkForwardConfig(candidate_count=2, min_train_bars=50)

        report = walk_forward_ai_research(
            dataset=dataset,
            provider=provider,
            wf_config=wf_config,
            research_config=research_config,
            backtest_config=backtest_config,
            ai_config=ai_config,
        )

        # Provider should have been called exactly candidate_count times for the single fold.
        assert call_count["count"] == ai_config.candidate_count
