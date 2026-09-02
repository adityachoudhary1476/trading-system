"""Parameter sensitivity (Phase 19 - Strategy Research).

Re-run the existing deterministic backtest across a small grid of parameter
perturbations around a candidate spec. The output is a deterministic artifact
that records the dispersion of total_return / max_drawdown / Sharpe across
perturbations.

A candidate that depends on a single precise parameter value scores poorly on
robustness: the sensitivity dispersion should be small.

Sensitivity only perturbs NUMERIC parameters in declared indicators and the
spec's ``stop_loss_pct`` / ``take_profit_pct`` / ``max_allocation_pct``. It
never invents new conditions, never changes the DSL structure, and never
modifies the spec's identity.

This is a research signal, not a forecast.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Optional

from ..backtester import BacktestConfig, run_backtest
from ..dataset import HistoricalDataset
from .engine import merged_backtest_config
from .evaluation import StrategyEvaluation, evaluate_spec
from .interpreter import build_strategy
from .spec import StrategySpec, _NAME_RE  # internal but useful


@dataclass
class ParameterPerturbation:
    description: str
    spec: StrategySpec


@dataclass
class ParameterSensitivityPoint:
    description: str
    evaluation: Optional[StrategyEvaluation]
    error: str = ""


@dataclass
class ParameterSensitivityResult:
    candidate_id: str
    spec_name: str
    symbol: str
    timeframe: str
    baseline_evaluation: Optional[StrategyEvaluation]
    perturbations: list = field(default_factory=list)
    total_return_min: Optional[float] = None
    total_return_max: Optional[float] = None
    total_return_median: Optional[float] = None
    total_return_std: Optional[float] = None
    sensitivity_score: Optional[float] = None
    warnings: list = field(default_factory=list)

    def to_record(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "spec_name": self.spec_name,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "baseline_total_return": (
                self.baseline_evaluation.total_return if self.baseline_evaluation else None
            ),
            "total_return_min": self.total_return_min,
            "total_return_max": self.total_return_max,
            "total_return_median": self.total_return_median,
            "total_return_std": self.total_return_std,
            "sensitivity_score": self.sensitivity_score,
            "n_perturbations": len(self.perturbations),
            "warnings": list(self.warnings),
        }


@dataclass
class ParameterSensitivityConfig:
    """Configuration for parameter sensitivity.

    ``window_offsets`` are integer offsets added to indicator ``window`` parameters
    (e.g. SMA window = 20 with offset +5 -> 25). ``allocation_offsets`` are
    fractional offsets added to ``max_allocation_pct``. ``stop_offsets`` are
    fractional offsets added to ``stop_loss_pct``.
    """

    window_offsets: tuple = (-3, -1, 0, 1, 3)
    allocation_offsets: tuple = (-0.1, 0.0, 0.1)
    stop_offsets: tuple = (-0.02, 0.0, 0.02)

    def __post_init__(self) -> None:
        if not self.window_offsets:
            raise ValueError("window_offsets must be non-empty")
        if not self.allocation_offsets:
            raise ValueError("allocation_offsets must be non-empty")
        if not self.stop_offsets:
            raise ValueError("stop_offsets must be non-empty")


def _perturb_window(spec: StrategySpec, offset: int) -> Optional[StrategySpec]:
    """Return a deep-copy of ``spec`` with each indicator's ``window`` shifted by ``offset``.

    Returns None when the spec has no window-based indicators.
    """
    new = deepcopy(spec)
    found = False
    for ind in new.indicators:
        params = dict(ind.params)
        if "window" in params:
            old = int(params["window"])
            new_val = max(2, old + int(offset))
            params["window"] = new_val
            found = True
        ind.__dict__["params"] = params
    return new if found else None


def _perturb_allocation(spec: StrategySpec, offset: float) -> StrategySpec:
    new = deepcopy(spec)
    cur = float(new.position_sizing.max_allocation_pct)
    new_val = max(0.05, min(1.0, cur + float(offset)))
    new.position_sizing.max_allocation_pct = float(new_val)
    return new


def _perturb_stop(spec: StrategySpec, offset: float) -> Optional[StrategySpec]:
    new = deepcopy(spec)
    if new.risk.stop_loss_pct is None:
        return None
    cur = float(new.risk.stop_loss_pct)
    new_val = max(0.005, min(0.95, cur + float(offset)))
    new.risk.stop_loss_pct = float(new_val)
    return new


def run_parameter_sensitivity(
    candidate_id: str,
    spec: StrategySpec,
    dataset: HistoricalDataset,
    backtest_config: BacktestConfig,
    config: Optional[ParameterSensitivityConfig] = None,
) -> ParameterSensitivityResult:
    """Deterministically perturb numeric parameters and re-run the backtest."""
    cfg = config or ParameterSensitivityConfig()
    base_merged = merged_backtest_config(spec, backtest_config)
    strategy = build_strategy(spec)

    report = ParameterSensitivityResult(
        candidate_id=candidate_id,
        spec_name=spec.name,
        symbol=dataset.symbol,
        timeframe=dataset.timeframe,
        baseline_evaluation=None,
    )

    # Baseline.
    try:
        base_bt = run_backtest(dataset, strategy, base_merged)
        report.baseline_evaluation = evaluate_spec(spec, base_bt)
    except Exception as e:  # noqa: BLE001
        report.warnings.append(f"baseline backtest failed: {type(e).__name__}: {e}")

    perturbations: list[ParameterSensitivityPoint] = []

    # Window perturbations.
    for offset in cfg.window_offsets:
        new_spec = _perturb_window(spec, offset)
        if new_spec is None:
            continue
        perturbations.append(_evaluate_perturbation(
            candidate_id, new_spec, dataset, base_merged,
            description=f"window+{offset}",
        ))

    # Allocation perturbations.
    for offset in cfg.allocation_offsets:
        if offset == 0.0:
            continue
        new_spec = _perturb_allocation(spec, offset)
        perturbations.append(_evaluate_perturbation(
            candidate_id, new_spec, dataset, base_merged,
            description=f"allocation+{offset:+.2f}",
        ))

    # Stop perturbations.
    for offset in cfg.stop_offsets:
        if offset == 0.0:
            continue
        new_spec = _perturb_stop(spec, offset)
        if new_spec is None:
            continue
        perturbations.append(_evaluate_perturbation(
            candidate_id, new_spec, dataset, base_merged,
            description=f"stop+{offset:+.3f}",
        ))

    report.perturbations = perturbations

    valid = [p.evaluation.total_return for p in perturbations if p.evaluation is not None]
    if valid:
        report.total_return_min = min(valid)
        report.total_return_max = max(valid)
        sorted_returns = sorted(valid)
        n = len(sorted_returns)
        report.total_return_median = (
            sorted_returns[n // 2] if n % 2 == 1
            else (sorted_returns[n // 2 - 1] + sorted_returns[n // 2]) / 2.0
        )
        if len(valid) >= 2:
            mean = sum(valid) / len(valid)
            variance = sum((v - mean) ** 2 for v in valid) / len(valid)
            report.total_return_std = variance ** 0.5
        # Sensitivity score: 1 - normalized dispersion; saturating at 1 when
        # dispersion is small.
        if report.total_return_std is not None:
            # Use 0.5 as a fixed dispersion saturation scale.
            report.sensitivity_score = max(
                0.0,
                1.0 - min(1.0, report.total_return_std / 0.5),
            )
        else:
            report.sensitivity_score = 1.0

    if not perturbations:
        report.warnings.append("no perturbations produced")
    return report


def _evaluate_perturbation(
    candidate_id: str,
    spec: StrategySpec,
    dataset: HistoricalDataset,
    base_merged: BacktestConfig,
    description: str,
) -> ParameterSensitivityPoint:
    point = ParameterSensitivityPoint(description=description, evaluation=None)
    try:
        # Re-merge each perturbed spec against the base config (so the
        # risk/position fields stay consistent with the base).
        from .engine import merged_backtest_config as _merged
        merged = _merged(spec, base_merged)
        strategy = build_strategy(spec)
        bt = run_backtest(dataset, strategy, merged)
        point.evaluation = evaluate_spec(spec, bt)
    except Exception as e:  # noqa: BLE001
        point.error = f"{type(e).__name__}: {e}"
    return point


__all__ = [
    "ParameterSensitivityConfig",
    "ParameterSensitivityPoint",
    "ParameterSensitivityResult",
    "run_parameter_sensitivity",
]