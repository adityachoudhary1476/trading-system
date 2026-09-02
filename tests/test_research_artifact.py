"""Tests for the research artifact aggregator (Phase 19)."""
import numpy as np
import pandas as pd
import pytest

from trading_system.research.backtester import BacktestConfig
from trading_system.research.dataset import HistoricalDataset
from trading_system.research.strategy_lab.evaluation import StrategyEvaluation
from trading_system.research.strategy_lab.research_artifact import (
    InvalidTransitionError,
    LifecycleState,
    ResearchArtifact,
    RobustnessConfig,
    RobustnessEvaluationConfig,
    compute_regime_diversity,
    evaluate_candidate_research,
    transition,
)
from trading_system.research.strategy_lab.spec import (
    PositionSizing,
    RiskParams,
    StrategySpec,
    indicator_operand,
    make_condition,
)


def _synthetic_dataset(n: int = 400, seed: int = 0) -> HistoricalDataset:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    idx = pd.date_range("2022-01-01", periods=n, freq="D")
    df = pd.DataFrame(
        {
            "open": close + rng.normal(0, 0.1, n),
            "high": close + np.abs(rng.normal(0, 0.3, n)),
            "low": close - np.abs(rng.normal(0, 0.3, n)),
            "close": close,
            "volume": rng.integers(1000, 5000, n).astype(float),
        },
        index=idx,
    )
    return HistoricalDataset(
        symbol="NSE:SBIN",
        timeframe="1d",
        data=df,
    )


def _synthetic_spec() -> StrategySpec:
    return StrategySpec(
        name="ema-cross-12-26",
        description="LONG when EMA(12) > EMA(26); exit on inverse cross.",
        symbol="NSE:SBIN",
        timeframe="1d",
        indicators=(
            {"name": "ema", "params": {"window": 12}},
            {"name": "ema", "params": {"window": 26}},
        ),
        entry=make_condition(
            indicator_operand("ema_12"),
            ">",
            indicator_operand("ema_26"),
        ),
        exit=make_condition(
            indicator_operand("ema_12"),
            "<",
            indicator_operand("ema_26"),
        ),
        position_sizing=PositionSizing(max_allocation_pct=0.95),
        risk=RiskParams(stop_loss_pct=0.05, take_profit_pct=0.10),
        generated_by="test",
    )


def _synthetic_evaluation(positive: bool = True) -> StrategyEvaluation:
    tr = 0.05 if positive else -0.02
    return StrategyEvaluation(
        spec_name="ema-cross-12-26",
        symbol="NSE:SBIN",
        timeframe="1d",
        generated_by="test",
        initial_capital=100_000.0,
        final_capital=100_000.0 * (1.0 + tr),
        net_pnl=100_000.0 * tr,
        total_return=tr,
        n_trades=20,
        winning=12,
        losing=8,
        win_rate=0.6,
        avg_trade=250.0,
        avg_trade_return=0.0025,
        max_drawdown=-0.08,
        transaction_costs=200.0,
        slippage_estimate=50.0,
        exposure_pct=0.9,
        profit_factor=1.4,
        sharpe=1.0,
        sortino=1.3,
        reliable=True,
        notes=[],
        unavailable_metrics=[],
    )


def _synthetic_config() -> BacktestConfig:
    return BacktestConfig(
        initial_capital=100_000.0,
        transaction_cost_pct=0.0005,
        slippage_pct=0.0002,
    )


# ---------------- Lifecycle transitions ----------------


def test_lifecycle_state_enum_values():
    assert LifecycleState.DISCOVERED.value == "discovered"
    assert LifecycleState.PAPER_ELIGIBLE.value == "paper_eligible"
    assert LifecycleState.PAPER_ACTIVE.value == "paper_active"
    assert LifecycleState.REJECTED.value == "rejected"
    assert LifecycleState.RETIRED.value == "retired"


def test_transition_happy_path():
    assert transition(LifecycleState.DISCOVERED, LifecycleState.SPECIFIED) == LifecycleState.SPECIFIED
    assert transition(LifecycleState.SPECIFIED, LifecycleState.BACKTESTED) == LifecycleState.BACKTESTED
    assert transition(LifecycleState.BACKTESTED, LifecycleState.VALIDATING) == LifecycleState.VALIDATING
    assert transition(LifecycleState.VALIDATING, LifecycleState.VALIDATED) == LifecycleState.VALIDATED
    assert transition(LifecycleState.VALIDATED, LifecycleState.PAPER_ELIGIBLE) == LifecycleState.PAPER_ELIGIBLE
    assert transition(LifecycleState.PAPER_ELIGIBLE, LifecycleState.PAPER_ACTIVE) == LifecycleState.PAPER_ACTIVE


def test_transition_rejects_invalid_skip():
    # Cannot jump straight from SPECIFIED to PAPER_ACTIVE.
    with pytest.raises(InvalidTransitionError):
        transition(LifecycleState.SPECIFIED, LifecycleState.PAPER_ACTIVE)


def test_transition_rejects_terminal():
    with pytest.raises(InvalidTransitionError):
        transition(LifecycleState.REJECTED, LifecycleState.DISCOVERED)
    with pytest.raises(InvalidTransitionError):
        transition(LifecycleState.RETIRED, LifecycleState.VALIDATED)


def test_transition_allows_reject_from_any_non_terminal():
    for state in (LifecycleState.DISCOVERED, LifecycleState.SPECIFIED, LifecycleState.BACKTESTED,
                  LifecycleState.VALIDATING, LifecycleState.VALIDATED, LifecycleState.PAPER_ELIGIBLE,
                  LifecycleState.PAPER_ACTIVE):
        assert transition(state, LifecycleState.REJECTED) == LifecycleState.REJECTED


# ---------------- RobustnessConfig ----------------


def test_robustness_config_default_thresholds():
    cfg = RobustnessConfig()
    assert 0.0 <= cfg.min_regime_diversity_ratio <= 1.0
    assert 0.0 <= cfg.min_parameter_sensitivity_score <= 1.0


def test_robustness_config_rejects_invalid():
    with pytest.raises(ValueError):
        RobustnessConfig(min_regime_diversity_ratio=1.5)
    with pytest.raises(ValueError):
        RobustnessConfig(min_regime_diversity_ratio=-0.1)
    with pytest.raises(ValueError):
        RobustnessConfig(min_parameter_sensitivity_score=2.0)


# ---------------- compute_regime_diversity ----------------


def test_compute_regime_diversity_handles_empty():
    class _StubReport:
        results = []
    pos_ratio, n_eval, n_pos = compute_regime_diversity(_StubReport())
    assert pos_ratio == 0.0
    assert n_eval == 0
    assert n_pos == 0


def test_compute_regime_diversity_counts_positive_only():
    from trading_system.research.strategy_lab.evaluation import StrategyEvaluation

    ev_pos = StrategyEvaluation(
        spec_name="x", symbol="X", timeframe="1d", generated_by="t",
        initial_capital=100.0, final_capital=105.0, net_pnl=5.0,
        total_return=0.05, n_trades=1, winning=1, losing=0, win_rate=1.0,
        avg_trade=5.0, avg_trade_return=0.05, max_drawdown=-0.01,
        transaction_costs=0.0, slippage_estimate=0.0, exposure_pct=1.0,
        profit_factor=None, sharpe=None, sortino=None, reliable=True,
        notes=[], unavailable_metrics=[],
    )
    ev_neg = StrategyEvaluation(
        spec_name="x", symbol="X", timeframe="1d", generated_by="t",
        initial_capital=100.0, final_capital=95.0, net_pnl=-5.0,
        total_return=-0.05, n_trades=1, winning=0, losing=1, win_rate=0.0,
        avg_trade=-5.0, avg_trade_return=-0.05, max_drawdown=-0.05,
        transaction_costs=0.0, slippage_estimate=0.0, exposure_pct=1.0,
        profit_factor=None, sharpe=None, sortino=None, reliable=True,
        notes=[], unavailable_metrics=[],
    )

    class _StubResult:
        def __init__(self, ev, error=""):
            self.evaluation = ev
            self.error = error
            self.rows = 50

    class _StubReport:
        def __init__(self, results):
            self.results = results

    report = _StubReport([
        _StubResult(ev_pos),
        _StubResult(ev_neg),
    ])
    pos_ratio, n_eval, n_pos = compute_regime_diversity(report)
    assert n_eval == 2
    assert n_pos == 1
    assert pos_ratio == 0.5


# ---------------- evaluate_candidate_research ----------------


def test_evaluate_candidate_research_returns_artifact():
    spec = _synthetic_spec()
    evaluation = _synthetic_evaluation(positive=True)
    artifact = evaluate_candidate_research(
        candidate_id="ema-cross-12-26",
        spec=spec,
        evaluation=evaluation,
        dataset=_synthetic_dataset(),
        backtest_config=_synthetic_config(),
        config=RobustnessEvaluationConfig(
            cost_sensitivity=__import__(
                "trading_system.research.strategy_lab.cost_sensitivity", fromlist=["CostSensitivityConfig"]
            ).CostSensitivityConfig(
                cost_multipliers=(0.0, 1.0),
                slippage_multipliers=(0.0, 1.0),
            ),
        ),
    )
    assert isinstance(artifact, ResearchArtifact)
    assert artifact.candidate_id == "ema-cross-12-26"
    assert artifact.decision in ("validated", "rejected")
    assert artifact.decision_reasons  # at least one reason is always produced


def test_evaluate_candidate_research_rejects_negative_return():
    spec = _synthetic_spec()
    evaluation = _synthetic_evaluation(positive=False)
    artifact = evaluate_candidate_research(
        candidate_id="ema-cross-12-26",
        spec=spec,
        evaluation=evaluation,
        dataset=_synthetic_dataset(),
        backtest_config=_synthetic_config(),
    )
    assert artifact.lifecycle_state == LifecycleState.REJECTED
    assert any("non-positive" in r for r in artifact.decision_reasons)


def test_evaluate_candidate_research_rejects_unreliable():
    spec = _synthetic_spec()
    evaluation = _synthetic_evaluation(positive=True)
    evaluation.reliable = False
    artifact = evaluate_candidate_research(
        candidate_id="ema-cross-12-26",
        spec=spec,
        evaluation=evaluation,
        dataset=_synthetic_dataset(),
        backtest_config=_synthetic_config(),
    )
    assert artifact.lifecycle_state == LifecycleState.REJECTED
    assert any("unreliable" in r for r in artifact.decision_reasons)


def test_research_artifact_serializes():
    spec = _synthetic_spec()
    evaluation = _synthetic_evaluation(positive=True)
    artifact = evaluate_candidate_research(
        candidate_id="ema-cross-12-26",
        spec=spec,
        evaluation=evaluation,
        dataset=_synthetic_dataset(),
        backtest_config=_synthetic_config(),
    )
    rec = artifact.to_record()
    assert "candidate_id" in rec
    assert "lifecycle_state" in rec
    assert "evaluation" in rec
    assert "robustness_score" in rec
    assert "decision_reasons" in rec
    assert rec["lifecycle_state"] in {s.value for s in LifecycleState}


def test_search_count_zero_is_treated_as_one():
    """search_count=0 is invalid; the penalty clamps to 1/sqrt(1) = 1.0."""
    from trading_system.research.strategy_lab.research_artifact import (
        _compute_components,
    )

    spec = _synthetic_spec()
    evaluation = _synthetic_evaluation(positive=True)
    artifact = ResearchArtifact(
        candidate_id="x",
        spec=spec,
        evaluation=evaluation,
        lifecycle_state=LifecycleState.BACKTESTED,
        cost_sensitivity=None,
        regime_evaluation=None,
        parameter_sensitivity=None,
        search_count=0,
    )
    cfg = RobustnessConfig()
    components = _compute_components(artifact, cfg, None, None, None, evaluation)
    assert components["search_count_penalty"] == 1.0
    assert components["search_count"] == 0


def test_search_count_higher_yields_lower_penalty():
    from trading_system.research.strategy_lab.research_artifact import (
        _compute_components,
    )

    spec = _synthetic_spec()
    evaluation = _synthetic_evaluation(positive=True)
    a1 = ResearchArtifact(
        candidate_id="x", spec=spec, evaluation=evaluation,
        lifecycle_state=LifecycleState.BACKTESTED,
        cost_sensitivity=None, regime_evaluation=None,
        parameter_sensitivity=None, search_count=1,
    )
    a100 = ResearchArtifact(
        candidate_id="x", spec=spec, evaluation=evaluation,
        lifecycle_state=LifecycleState.BACKTESTED,
        cost_sensitivity=None, regime_evaluation=None,
        parameter_sensitivity=None, search_count=100,
    )
    cfg = RobustnessConfig()
    c1 = _compute_components(a1, cfg, None, None, None, evaluation)
    c100 = _compute_components(a100, cfg, None, None, None, evaluation)
    assert c1["search_count_penalty"] == 1.0
    assert c100["search_count_penalty"] < c1["search_count_penalty"]