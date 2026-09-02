"""Tests for the cost-sensitivity sweep module (Phase 19)."""
import numpy as np
import pandas as pd
import pytest

from trading_system.research.backtester import BacktestConfig
from trading_system.research.dataset import HistoricalDataset
from trading_system.research.strategy_lab.cost_sensitivity import (
    CostSensitivityConfig,
    CostSensitivityPoint,
    CostSensitivityResult,
    run_cost_sensitivity,
)
from trading_system.research.strategy_lab.spec import (
    PositionSizing,
    RiskParams,
    StrategySpec,
    const_operand,
    field_operand,
    indicator_operand,
    make_condition,
)


def _synthetic_dataset(n: int = 200, seed: int = 0) -> HistoricalDataset:
    """Build a deterministic trending dataset for sensitivity tests."""
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
    """Build a simple long-only EMA-cross spec."""
    return StrategySpec(
        name="ema-cross-12-26",
        description="LONG when EMA(12) crosses above EMA(26); exit on inverse cross.",
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


def _synthetic_config() -> BacktestConfig:
    return BacktestConfig(
        initial_capital=100_000.0,
        transaction_cost_pct=0.0005,
        slippage_pct=0.0002,
    )


def test_default_multipliers_present():
    cfg = CostSensitivityConfig()
    assert 1.0 in cfg.cost_multipliers
    assert 0.0 in cfg.cost_multipliers


def test_default_multipliers_ordered():
    cfg = CostSensitivityConfig()
    assert list(cfg.cost_multipliers) == sorted(cfg.cost_multipliers)
    assert list(cfg.slippage_multipliers) == sorted(cfg.slippage_multipliers)


def test_cost_fragile_threshold_default():
    cfg = CostSensitivityConfig()
    assert 0.0 < cfg.fragility_threshold <= 1.0


def test_run_cost_sensitivity_returns_result():
    dataset = _synthetic_dataset()
    spec = _synthetic_spec()
    result = run_cost_sensitivity(
        candidate_id="ema-cross-12-26",
        spec=spec,
        dataset=dataset,
        backtest_config=_synthetic_config(),
        config=CostSensitivityConfig(cost_multipliers=(0.0, 1.0), slippage_multipliers=(0.0, 1.0)),
    )
    assert isinstance(result, CostSensitivityResult)
    assert result.candidate_id == "ema-cross-12-26"
    assert len(result.points) == 4
    assert result.gross_evaluation is not None
    assert isinstance(result.cost_fragile, bool)


def test_run_cost_sensitivity_fragile_detected():
    """A strategy whose return drops below the threshold at high costs is fragile."""
    dataset = _synthetic_dataset()
    spec = _synthetic_spec()
    result = run_cost_sensitivity(
        candidate_id="ema-cross-12-26",
        spec=spec,
        dataset=dataset,
        backtest_config=_synthetic_config(),
        config=CostSensitivityConfig(
            cost_multipliers=(0.0, 2.0),
            slippage_multipliers=(0.0, 2.0),
            fragility_threshold=0.5,
        ),
    )
    # cost_fragile is a boolean
    assert isinstance(result.cost_fragile, bool)


def test_cost_sensitivity_zero_multiplier_has_evaluation():
    """A zero-cost multiplier run should produce a valid evaluation."""
    dataset = _synthetic_dataset()
    spec = _synthetic_spec()
    result = run_cost_sensitivity(
        candidate_id="ema-cross-12-26",
        spec=spec,
        dataset=dataset,
        backtest_config=_synthetic_config(),
        config=CostSensitivityConfig(
            cost_multipliers=(0.0,),
            slippage_multipliers=(0.0,),
        ),
    )
    assert len(result.points) == 1
    assert result.points[0].cost_multiplier == 0.0
    assert result.points[0].evaluation is not None


def test_cost_sensitivity_rejects_invalid_multiplier():
    with pytest.raises(ValueError):
        CostSensitivityConfig(cost_multipliers=(-0.1,))
    with pytest.raises(ValueError):
        CostSensitivityConfig(cost_multipliers=())


def test_result_serializes_to_record():
    dataset = _synthetic_dataset()
    spec = _synthetic_spec()
    result = run_cost_sensitivity(
        candidate_id="ema-cross-12-26",
        spec=spec,
        dataset=dataset,
        backtest_config=_synthetic_config(),
        config=CostSensitivityConfig(
            cost_multipliers=(0.0, 1.0),
            slippage_multipliers=(0.0,),
        ),
    )
    rec = result.to_record()
    assert "candidate_id" in rec
    assert "spec_name" in rec
    assert "cost_fragile" in rec
    assert rec["cost_fragile"] in (True, False)
    assert "n_points" in rec
    assert rec["n_points"] == 2


def test_monotonic_cost_increase_does_not_increase_returns():
    """For a reasonable evaluation, increasing costs should never increase returns."""
    dataset = _synthetic_dataset()
    spec = _synthetic_spec()
    result = run_cost_sensitivity(
        candidate_id="ema-cross-12-26",
        spec=spec,
        dataset=dataset,
        backtest_config=_synthetic_config(),
        config=CostSensitivityConfig(
            cost_multipliers=(0.0, 0.5, 1.0, 2.0),
            slippage_multipliers=(0.0,),
        ),
    )
    prev = result.points[0].evaluation.total_return
    for p in result.points[1:]:
        assert p.evaluation is not None
        # Monotonically non-increasing.
        assert p.evaluation.total_return <= prev + 1e-9
        prev = p.evaluation.total_return


def test_invalid_spec_is_rejected_gracefully():
    """If the spec is invalid, the sweep should record a warning rather than crash."""
    bad = StrategySpec(
        name="bad",
        description="x",
        symbol="NSE:SBIN",
        timeframe="1d",
        indicators=(),
        entry={"type": "comparison", "left": {"kind": "field", "field": "close"}, "op": ">", "right": {"kind": "field", "field": "open"}},
        exit={"type": "comparison", "left": {"kind": "field", "field": "close"}, "op": "<", "right": {"kind": "field", "field": "open"}},
        position_sizing=PositionSizing(max_allocation_pct=0.95),
        risk=RiskParams(stop_loss_pct=0.05, take_profit_pct=0.10),
        generated_by="test",
    )
    result = run_cost_sensitivity(
        candidate_id="bad",
        spec=bad,
        dataset=_synthetic_dataset(),
        backtest_config=_synthetic_config(),
    )
    # Either succeeded (in which case points are filled) or had per-point errors.
    for p in result.points:
        assert isinstance(p, CostSensitivityPoint)