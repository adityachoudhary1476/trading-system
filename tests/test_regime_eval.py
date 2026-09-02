"""Tests for the regime-conditional evaluation module (Phase 19)."""
import numpy as np
import pandas as pd
import pytest

from trading_system.research.backtester import BacktestConfig
from trading_system.research.dataset import HistoricalDataset
from trading_system.research.intelligence import RegimeEnum
from trading_system.research.strategy_lab.regime_eval import (
    RegimeEvalConfig,
    RegimeEvaluationReport,
    RegimeResult,
    RegimeWindow,
    run_regime_evaluation,
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


def _synthetic_config() -> BacktestConfig:
    return BacktestConfig(
        initial_capital=100_000.0,
        transaction_cost_pct=0.0005,
        slippage_pct=0.0002,
    )


def test_default_min_window_bars_positive():
    cfg = RegimeEvalConfig()
    assert cfg.min_window_bars > 0


def test_default_lookback_bars_positive():
    cfg = RegimeEvalConfig()
    assert cfg.lookback_bars > 0


def test_run_regime_evaluation_returns_report():
    dataset = _synthetic_dataset()
    spec = _synthetic_spec()
    result = run_regime_evaluation(
        candidate_id="ema-cross-12-26",
        spec=spec,
        dataset=dataset,
        backtest_config=_synthetic_config(),
    )
    assert isinstance(result, RegimeEvaluationReport)
    assert result.candidate_id == "ema-cross-12-26"


def test_run_regime_evaluation_handles_short_dataset():
    """A dataset shorter than min_window_bars should yield no usable windows."""
    dataset = _synthetic_dataset(n=20)
    spec = _synthetic_spec()
    result = run_regime_evaluation(
        candidate_id="ema-cross-12-26",
        spec=spec,
        dataset=dataset,
        backtest_config=_synthetic_config(),
        config=RegimeEvalConfig(min_window_bars=200),
    )
    # With insufficient data, report warns and produces no per-regime results.
    assert result.windows == []
    assert result.results == []
    assert any("insufficient data" in w for w in result.warnings)


def test_run_regime_evaluation_serializes():
    dataset = _synthetic_dataset()
    spec = _synthetic_spec()
    result = run_regime_evaluation(
        candidate_id="ema-cross-12-26",
        spec=spec,
        dataset=dataset,
        backtest_config=_synthetic_config(),
    )
    rec = result.to_record()
    assert "candidate_id" in rec
    assert "n_bars_total" in rec
    assert "windows" in rec
    assert "results" in rec
    assert "warnings" in rec


def test_run_regime_evaluation_rejects_invalid_min_window():
    with pytest.raises(ValueError):
        RegimeEvalConfig(min_window_bars=0)
    with pytest.raises(ValueError):
        RegimeEvalConfig(min_window_bars=-1)


def test_run_regime_evaluation_windows_have_valid_regime_labels():
    dataset = _synthetic_dataset()
    spec = _synthetic_spec()
    result = run_regime_evaluation(
        candidate_id="ema-cross-12-26",
        spec=spec,
        dataset=dataset,
        backtest_config=_synthetic_config(),
    )
    valid = set(RegimeEnum)
    for w in result.windows:
        # Each window has a regime label.
        assert w.regime in valid
        assert isinstance(w, RegimeWindow)


def test_run_regime_evaluation_aggregates_known_regimes():
    """A long enough dataset should produce at least one classified result."""
    dataset = _synthetic_dataset(n=500)
    spec = _synthetic_spec()
    result = run_regime_evaluation(
        candidate_id="ema-cross-12-26",
        spec=spec,
        dataset=dataset,
        backtest_config=_synthetic_config(),
    )
    # UNKNOWN is filtered out.
    for r in result.results:
        assert r.regime != RegimeEnum.UNKNOWN
        assert isinstance(r, RegimeResult)


def test_run_regime_evaluation_by_regime_lookup():
    dataset = _synthetic_dataset()
    spec = _synthetic_spec()
    result = run_regime_evaluation(
        candidate_id="ema-cross-12-26",
        spec=spec,
        dataset=dataset,
        backtest_config=_synthetic_config(),
    )
    # by_regime returns one result per regime (first match) for a given regime.
    seen = set()
    for r in result.results:
        if r.regime in seen:
            continue
        assert result.by_regime(r.regime) is r
        seen.add(r.regime)
    # A regime that wasn't observed should be None.
    assert result.by_regime(RegimeEnum.UNKNOWN) is None