"""Phase 13 tests: research engine, evaluation, filters, ranking.

The engine is bounded (candidate_count clamped), offline (deterministic mock
provider), deterministic (same inputs -> same report), and records every
candidate failure (provider error / invalid spec) instead of crashing.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_system.models.base import ModelProviderError
from trading_system.research.backtester import BacktestConfig, run_backtest
from trading_system.research.dataset import HistoricalDataset
from trading_system.research.strategy_lab.engine import (
    HARD_MAX_CANDIDATES,
    ResearchConfig,
    StrategyResearchEngine,
    merged_backtest_config,
)
from trading_system.research.strategy_lab.evaluation import (
    evaluate_result,
    evaluate_strategy,
)
from trading_system.research.strategy_lab.filters import (
    QualityFilterConfig,
    apply_quality_filter,
)
from trading_system.research.strategy_lab.interpreter import build_strategy
from trading_system.research.strategy_lab.providers import (
    DeterministicStrategyProvider,
    GenerationContext,
    StrategyProposalProvider,
)
from trading_system.research.strategy_lab.ranking import (
    RankingConfig,
    rank_candidates,
)
from trading_system.research.strategy_lab.spec import StrategySpec


def _df(n=400, seed=2):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    close = 100 + np.cumsum(rng.normal(0.05, 1.0, n))
    open_ = close + rng.normal(0, 0.3, n)
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + np.abs(rng.normal(0, 0.5, n)),
            "low": np.minimum(open_, close) - np.abs(rng.normal(0, 0.5, n)),
            "close": close,
            "volume": rng.integers(100, 1000, n).astype(float),
        },
        index=idx,
    )


@pytest.fixture()
def dataset():
    return HistoricalDataset(symbol="NSE:SBIN", timeframe="1d", data=_df())


@pytest.fixture()
def ctx():
    return GenerationContext(symbol="NSE:SBIN", timeframe="1d")


@pytest.fixture()
def bt_config():
    return BacktestConfig(
        initial_capital=100_000, transaction_cost_pct=0.0005, slippage_pct=0.0002
    )


class _CountingProvider(StrategyProposalProvider):
    """Records how many times generation was invoked (bounding proof)."""

    name = "counting"

    def __init__(self, provider=None):
        self._inner = provider or DeterministicStrategyProvider()
        self.calls = []

    def generate_strategy(self, context):
        self.calls.append(context.variant_index)
        return self._inner.generate_strategy(context)


class _BrokenProvider(StrategyProposalProvider):
    name = "broken"

    def generate_strategy(self, context):
        raise ModelProviderError("simulated provider outage")


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
def test_evaluation_metrics_present(dataset, ctx, bt_config):
    engine = StrategyResearchEngine(DeterministicStrategyProvider())
    report = engine.research_candidates(dataset, ctx, 1, bt_config)
    evaluation = report.candidates[0].evaluation
    assert evaluation is not None
    assert evaluation.n_trades == len(report.candidates[0].backtest.trades)
    assert evaluation.winning + evaluation.losing == evaluation.n_trades
    assert evaluation.max_drawdown <= 0.0
    assert evaluation.net_pnl == evaluation.final_capital - evaluation.initial_capital


def test_evaluation_transaction_costs_sum_from_ledger(dataset, ctx, bt_config):
    engine = StrategyResearchEngine(DeterministicStrategyProvider())
    report = engine.research_candidates(dataset, ctx, 1, bt_config)
    result = report.candidates[0].backtest
    evaluation = report.candidates[0].evaluation
    assert evaluation.transaction_costs == pytest.approx(
        sum(t.costs for t in result.trades)
    )


def test_evaluation_slippage_estimate_is_deterministic(dataset, ctx, bt_config):
    engine = StrategyResearchEngine(DeterministicStrategyProvider())
    r1 = engine.research_candidates(dataset, ctx, 1, bt_config)
    r2 = engine.research_candidates(dataset, ctx, 1, bt_config)
    e1 = r1.candidates[0].evaluation
    e2 = r2.candidates[0].evaluation
    assert e1.slippage_estimate == e2.slippage_estimate
    assert e1.slippage_estimate > 0.0  # config slippage is nonzero


def test_evaluation_zero_slippage_when_config_zero(dataset, ctx):
    engine = StrategyResearchEngine(DeterministicStrategyProvider())
    report = engine.research_candidates(
        dataset, ctx, 1, BacktestConfig(initial_capital=100_000, slippage_pct=0.0)
    )
    assert report.candidates[0].evaluation.slippage_estimate == 0.0


def test_evaluation_no_trades_marks_metrics_unavailable(dataset):
    # A threshold that can never trigger on this data -> zero trades.
    spec = StrategySpec(
        name="Never triggers",
        description="test",
        symbol="NSE:SBIN",
        timeframe="1d",
        indicators=[{"name": "sma", "params": {"window": 20}}],
        entry={
            "type": "comparison",
            "left": {"kind": "field", "field": "close"},
            "op": "<",
            "right": {"kind": "constant", "constant": 0.01},
        },
    )
    result = run_backtest(
        dataset,
        build_strategy(spec),
        merged_backtest_config(spec, BacktestConfig(initial_capital=100_000)),
    )
    evaluation = evaluate_result(result, spec.name)
    assert evaluation.n_trades == 0
    assert evaluation.win_rate is None
    assert evaluation.profit_factor is None
    assert "win_rate" in evaluation.unavailable_metrics
    assert "sharpe" in evaluation.unavailable_metrics


def test_evaluate_strategy_works_for_baseline_strategies(dataset):
    from trading_system.research.strategies import EMATrendStrategy

    strategy = EMATrendStrategy(12, 26)
    result = run_backtest(dataset, strategy, BacktestConfig(initial_capital=100_000))
    evaluation = evaluate_strategy(strategy, result)
    assert evaluation.spec_name == "ema"


# --------------------------------------------------------------------------- #
# Engine: generate -> validate -> backtest -> evaluate -> filter -> rank
# --------------------------------------------------------------------------- #
def test_engine_generates_and_ranks_candidates(dataset, ctx, bt_config):
    engine = StrategyResearchEngine(DeterministicStrategyProvider())
    report = engine.research_candidates(dataset, ctx, 4, bt_config)
    assert report.requested_candidates == 4
    assert len(report.candidates) == 4
    evaluated = [c for c in report.candidates if c.status == "evaluated"]
    assert evaluated, "at least one candidate must evaluate on 400 bars"
    for cand in evaluated:
        assert cand.spec is not None
        assert cand.evaluation is not None
        assert cand.filter_outcome is not None
    assert report.ranking  # survivors were ranked
    scores = [r.score for r in report.ranking]
    assert scores == sorted(scores, reverse=True)  # best first


def test_engine_is_bounded_by_max_candidates(dataset, ctx, bt_config):
    provider = _CountingProvider()
    engine = StrategyResearchEngine(provider, ResearchConfig(max_candidates=3))
    report = engine.research_candidates(dataset, ctx, 50, bt_config)
    assert report.requested_candidates == 3
    assert len(provider.calls) == 3
    assert provider.calls == [0, 1, 2]


def test_engine_hard_upper_bound(dataset, ctx, bt_config):
    provider = _CountingProvider()
    engine = StrategyResearchEngine(provider, ResearchConfig(max_candidates=999))
    report = engine.research_candidates(dataset, ctx, 9999, bt_config)
    assert report.requested_candidates == HARD_MAX_CANDIDATES
    assert len(provider.calls) == HARD_MAX_CANDIDATES


def test_engine_rejects_negative_candidate_count(dataset, ctx, bt_config):
    engine = StrategyResearchEngine(DeterministicStrategyProvider())
    with pytest.raises(ValueError):
        engine.research_candidates(dataset, ctx, -1, bt_config)


def test_engine_provider_error_is_recorded_not_raised(dataset, ctx, bt_config):
    engine = StrategyResearchEngine(_BrokenProvider())
    report = engine.research_candidates(dataset, ctx, 2, bt_config)
    assert len(report.candidates) == 2
    for cand in report.candidates:
        assert cand.status == "provider_error"
        assert cand.spec is None
        assert cand.backtest is None
    assert report.ranking == []


def test_engine_invalid_spec_never_reaches_backtester(dataset, ctx, bt_config):
    class _InvalidSpecProvider(StrategyProposalProvider):
        name = "invalid-proposer"

        def generate_strategy(self, context):
            # Structurally valid but semantically invalid: symbol mismatch.
            spec = DeterministicStrategyProvider().generate_strategy(context)
            return spec.model_copy(update={"symbol": "NSE:OTHER"})

    engine = StrategyResearchEngine(_InvalidSpecProvider())
    report = engine.research_candidates(dataset, ctx, 1, bt_config)
    cand = report.candidates[0]
    assert cand.status == "invalid"
    assert cand.spec_errors, "must record validation errors"
    assert any("symbol" in e for e in cand.spec_errors)
    assert cand.backtest is None
    assert cand.evaluation is None


def test_engine_respects_symbol_allowlist(dataset, ctx, bt_config):
    config = ResearchConfig(allowed_symbols=frozenset({"NSE:TCS"}))
    engine = StrategyResearchEngine(DeterministicStrategyProvider(), config)
    report = engine.research_candidates(dataset, ctx, 2, bt_config)
    assert all(c.status == "invalid" for c in report.candidates)
    assert any(
        "allowed instrument set" in e
        for c in report.candidates
        for e in c.spec_errors
    )


def test_engine_deterministic_reports(dataset, ctx, bt_config):
    engine = StrategyResearchEngine(DeterministicStrategyProvider())
    r1 = engine.research_candidates(dataset, ctx, 5, bt_config)
    r2 = engine.research_candidates(dataset, ctx, 5, bt_config)
    assert [c.status for c in r1.candidates] == [c.status for c in r2.candidates]
    e1 = [(c.spec_name, c.evaluation.net_pnl, c.evaluation.n_trades)
          for c in r1.candidates]
    e2 = [(c.spec_name, c.evaluation.net_pnl, c.evaluation.n_trades)
          for c in r2.candidates]
    assert e1 == e2
    assert [r.key for r in r1.ranking] == [r.key for r in r2.ranking]
    assert [round(r.score, 12) for r in r1.ranking] == [
        round(r.score, 12) for r in r2.ranking
    ]


def test_engine_report_notes_quality_gate_disclaimer(dataset, ctx, bt_config):
    engine = StrategyResearchEngine(DeterministicStrategyProvider())
    report = engine.research_candidates(dataset, ctx, 1, bt_config)
    assert any("NOT proof" in n for n in report.notes)


def test_engine_requires_provider_interface(dataset):
    with pytest.raises(TypeError):
        StrategyResearchEngine(provider="not-a-provider")


def test_engine_holdout_out_of_sample(dataset, ctx, bt_config):
    engine = StrategyResearchEngine(
        DeterministicStrategyProvider(),
        ResearchConfig(
            max_candidates=4,
            quality_filter=QualityFilterConfig(min_trades=2, require_reliable=False),
        ),
    )
    outcome = engine.research_with_holdout(dataset, ctx, 4, bt_config)
    train_report = outcome["train_report"]
    assert train_report.candidates
    # Chronological split: train strictly before test.
    assert (
        outcome["train_split"].data.index.max()
        < outcome["test_split"].data.index.min()
    )
    for item in outcome["out_of_sample"]:
        assert item.spec_name
        if item.evaluation is not None:
            assert item.evaluation.symbol == "NSE:SBIN"


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #
def _evaluation(n_trades=15, total_return=0.10, max_drawdown=-0.10,
                net_pnl=1000.0, exposure=0.4, reliable=True):
    from trading_system.research.strategy_lab.evaluation import StrategyEvaluation

    wins = max(1, n_trades // 2)
    return StrategyEvaluation(
        spec_name="X", symbol="NSE:SBIN", timeframe="1d",
        initial_capital=100_000.0, final_capital=100_000.0 + net_pnl,
        net_pnl=net_pnl, total_return=total_return,
        n_trades=n_trades, winning=wins, losing=n_trades - wins,
        win_rate=0.5 if n_trades else None,
        max_drawdown=max_drawdown, exposure_pct=exposure,
        reliable=reliable,
    )


def test_filter_passes_reasonable_evaluation():
    outcome = apply_quality_filter(
        _evaluation(), QualityFilterConfig(), dataset_rows=300
    )
    assert outcome.passed and outcome.reasons == []


def test_filter_rejects_tiny_sample():
    outcome = apply_quality_filter(
        _evaluation(n_trades=2), QualityFilterConfig(min_trades=5), dataset_rows=300
    )
    assert outcome.failed
    assert any("too few trades" in r for r in outcome.reasons)


def test_filter_rejects_excessive_drawdown():
    outcome = apply_quality_filter(
        _evaluation(max_drawdown=-0.75),
        QualityFilterConfig(max_drawdown=0.5),
        dataset_rows=300,
    )
    assert any("drawdown" in r for r in outcome.reasons)


def test_filter_rejects_below_max_loss():
    outcome = apply_quality_filter(
        _evaluation(net_pnl=-9_000.0),
        QualityFilterConfig(max_loss=-5_000.0),
        dataset_rows=300,
    )
    assert any("max_loss" in r for r in outcome.reasons)


def test_filter_rejects_below_min_return():
    outcome = apply_quality_filter(
        _evaluation(total_return=0.001),
        QualityFilterConfig(min_total_return=0.05),
        dataset_rows=300,
    )
    assert any("min_total_return" in r for r in outcome.reasons)


def test_filter_rejects_excess_exposure():
    outcome = apply_quality_filter(
        _evaluation(exposure=0.99),
        QualityFilterConfig(max_exposure_pct=0.9),
        dataset_rows=300,
    )
    assert any("exposure" in r for r in outcome.reasons)


def test_filter_rejects_insufficient_bars():
    outcome = apply_quality_filter(
        _evaluation(), QualityFilterConfig(min_bars=100), dataset_rows=50
    )
    assert any("insufficient historical data" in r for r in outcome.reasons)


def test_filter_rejects_unreliable_performance():
    outcome = apply_quality_filter(
        _evaluation(reliable=False),
        QualityFilterConfig(require_reliable=True),
        dataset_rows=300,
    )
    assert any("unreliable" in r for r in outcome.reasons)


def test_filter_records_spec_errors():
    outcome = apply_quality_filter(
        _evaluation(), QualityFilterConfig(), dataset_rows=300,
        spec_errors=["bad condition"],
    )
    assert outcome.failed
    assert any("invalid strategy specification" in r for r in outcome.reasons)


def test_filter_config_validates_itself():
    problems = QualityFilterConfig(min_trades=-1).validate()
    assert problems
    assert QualityFilterConfig(max_drawdown=1.5).validate()


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #
def test_ranking_prefers_higher_return_with_equal_risk():
    good = _evaluation(total_return=0.5, max_drawdown=-0.1)
    bad = _evaluation(total_return=-0.1, max_drawdown=-0.1)
    ranked = rank_candidates({"good": good, "bad": bad})
    assert ranked[0].key == "good"
    assert ranked[0].score > ranked[1].score


def test_ranking_prefers_lower_drawdown_with_equal_return():
    safe = _evaluation(total_return=0.2, max_drawdown=-0.05)
    risky = _evaluation(total_return=0.2, max_drawdown=-0.45)
    ranked = rank_candidates({"safe": safe, "risky": risky})
    assert ranked[0].key == "safe"


def test_ranking_is_not_pure_return_sort():
    high_return_deep_dd = _evaluation(
        total_return=0.6, max_drawdown=-0.95, n_trades=30
    )
    modest_return_shallow_dd = _evaluation(
        total_return=0.2, max_drawdown=-0.05, n_trades=30
    )
    ranked = rank_candidates(
        {"aggressive": high_return_deep_dd, "calm": modest_return_shallow_dd}
    )
    # The modest strategy must outrank a deep-drawdown high return: with the
    # default weights a -95% drawdown zeroes its drawdown AND return component.
    assert ranked[0].key == "calm"


def test_ranking_ties_break_alphabetically():
    ranked = rank_candidates({"beta": _evaluation(), "alpha": _evaluation()})
    assert [r.key for r in ranked] == ["alpha", "beta"]
    assert ranked[0].score == ranked[1].score


def test_ranking_config_rejects_unknown_metrics():
    with pytest.raises(ValueError):
        RankingConfig(weights={"moon_phase": 1.0})


def test_ranking_config_rejects_negative_weights():
    with pytest.raises(ValueError):
        RankingConfig(weights={"total_return": -0.5})


def test_ranking_weights_change_ordering():
    calm = _evaluation(total_return=0.05, max_drawdown=-0.02, n_trades=30)
    aggressive = _evaluation(total_return=0.8, max_drawdown=-0.7, n_trades=30)
    risk_weighted = rank_candidates(
        {"calm": calm, "aggressive": aggressive},
        RankingConfig(weights={"max_drawdown": 1.0}),
    )
    return_weighted = rank_candidates(
        {"calm": calm, "aggressive": aggressive},
        RankingConfig(weights={"total_return": 1.0}),
    )
    assert risk_weighted[0].key == "calm"
    assert return_weighted[0].key == "aggressive"


def test_ranking_unavailable_metrics_contribute_zero():
    empty = _evaluation(n_trades=0)
    empty.win_rate = None
    empty.profit_factor = None
    empty.sharpe = None
    score, components = RankingConfig().normalized_score(empty)
    # Only return/drawdown/trade_count components contribute.
    assert components["win_rate"] == 0.0
    assert components["profit_factor"] == 0.0
    assert components["risk_adjusted"] == 0.0
    assert score == pytest.approx(
        0.30 * max(-1.0, min(1.0, empty.total_return))
        + 0.20 * (1.0 - min(1.0, abs(empty.max_drawdown) / 0.5))
    )


def test_ranking_documented_default_weights():
    config = RankingConfig()
    assert config.weights == {
        "total_return": 0.30,
        "max_drawdown": 0.20,
        "risk_adjusted": 0.20,
        "win_rate": 0.15,
        "profit_factor": 0.10,
        "trade_count": 0.05,
    }




