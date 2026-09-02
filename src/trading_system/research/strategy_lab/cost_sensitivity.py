"""Cost-sensitivity sweep (Phase 19 - Strategy Research).

For a single candidate StrategySpec, re-run the EXISTING deterministic backtest
across a range of transaction-cost / slippage multipliers. The output is a
deterministic, auditable artifact that records how the strategy's net
performance changes as friction increases.

The sweep:
  * never modifies the original BacktestConfig (uses ``replace``)
  * uses the existing ``run_backtest`` and ``evaluate_spec`` paths
  * records both gross (cost multiplier = 0.0) and net (real cost) results
  * preserves order: low-cost to high-cost
  * marks a candidate as "cost-fragile" when the highest-cost run's
    net_pnl drops below the gross net_pnl minus a configurable threshold

Cost sensitivity is a research-quality signal, NOT a guarantee of future
performance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..backtester import BacktestConfig, run_backtest
from ..dataset import HistoricalDataset
from .engine import merged_backtest_config
from .evaluation import StrategyEvaluation, evaluate_spec
from .interpreter import build_strategy
from .spec import StrategySpec


@dataclass
class CostSensitivityConfig:
    """Sweep configuration.

    ``cost_multipliers`` is a tuple of fractions of the base cost: 0.0 means
    zero friction (gross); 1.0 means the configured cost; 2.0 means doubling
    the friction to stress-test the edge.
    """

    cost_multipliers: tuple = (0.0, 0.5, 1.0, 1.5, 2.0)
    slippage_multipliers: tuple = (1.0,)
    # If the worst-friction net_pnl is more than this fraction below the
    # gross net_pnl, the strategy is considered "cost-fragile".
    fragility_threshold: float = 0.5

    def __post_init__(self) -> None:
        if not self.cost_multipliers:
            raise ValueError("cost_multipliers must be non-empty")
        for m in self.cost_multipliers:
            if m < 0:
                raise ValueError("cost multipliers must be >= 0")
        for m in self.slippage_multipliers:
            if m < 0:
                raise ValueError("slippage multipliers must be >= 0")
        if not 0.0 < self.fragility_threshold <= 1.0:
            raise ValueError("fragility_threshold must be in (0, 1]")


@dataclass
class CostSensitivityPoint:
    cost_multiplier: float
    slippage_multiplier: float
    evaluation: Optional[StrategyEvaluation]
    error: str = ""


@dataclass
class CostSensitivityResult:
    candidate_id: str
    spec_name: str
    symbol: str
    timeframe: str
    gross_evaluation: Optional[StrategyEvaluation]
    points: list = field(default_factory=list)
    cost_fragile: bool = False
    net_total_return_at_base: Optional[float] = None
    net_total_return_at_worst: Optional[float] = None
    warnings: list = field(default_factory=list)

    def to_record(self) -> dict:
        """Serializable summary (no evaluation objects)."""
        return {
            "candidate_id": self.candidate_id,
            "spec_name": self.spec_name,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "gross_total_return": (
                self.gross_evaluation.total_return if self.gross_evaluation else None
            ),
            "net_total_return_at_base": self.net_total_return_at_base,
            "net_total_return_at_worst": self.net_total_return_at_worst,
            "cost_fragile": self.cost_fragile,
            "n_points": len(self.points),
            "warnings": list(self.warnings),
        }


def run_cost_sensitivity(
    candidate_id: str,
    spec: StrategySpec,
    dataset: HistoricalDataset,
    backtest_config: BacktestConfig,
    config: Optional[CostSensitivityConfig] = None,
) -> CostSensitivityResult:
    """Run a deterministic cost-sensitivity sweep over a single spec.

    Returns a ``CostSensitivityResult`` with one entry per multiplier combination.
    """
    cfg = config or CostSensitivityConfig()
    merged_base = merged_backtest_config(spec, backtest_config)
    strategy = build_strategy(spec)

    result = CostSensitivityResult(
        candidate_id=candidate_id,
        spec_name=spec.name,
        symbol=dataset.symbol,
        timeframe=dataset.timeframe,
        gross_evaluation=None,
        points=[],
        cost_fragile=False,
    )

    # Pre-compute the gross evaluation (cost multiplier = 0).
    gross_cfg = _scaled_config(merged_base, cost_mult=0.0, slip_mult=1.0)
    try:
        gross_result = run_backtest(dataset, strategy, gross_cfg)
        gross_eval = evaluate_spec(spec, gross_result)
        result.gross_evaluation = gross_eval
    except Exception as e:  # noqa: BLE001 - record failure, don't crash sweep
        result.warnings.append(f"gross backtest failed: {type(e).__name__}: {e}")

    # Run the sweep.
    for cm in cfg.cost_multipliers:
        for sm in cfg.slippage_multipliers:
            point = CostSensitivityPoint(
                cost_multiplier=float(cm),
                slippage_multiplier=float(sm),
                evaluation=None,
            )
            scaled = _scaled_config(merged_base, cost_mult=float(cm), slip_mult=float(sm))
            try:
                bt = run_backtest(dataset, strategy, scaled)
                point.evaluation = evaluate_spec(spec, bt)
            except Exception as e:  # noqa: BLE001
                point.error = f"{type(e).__name__}: {e}"
            result.points.append(point)

    # Identify base (cm=1.0) and worst (largest cm) for reporting.
    base_point = next(
        (p for p in result.points if abs(p.cost_multiplier - 1.0) < 1e-9), None
    )
    worst_point = max(
        (p for p in result.points if p.evaluation is not None),
        key=lambda p: p.cost_multiplier,
        default=None,
    )
    if base_point and base_point.evaluation:
        result.net_total_return_at_base = base_point.evaluation.total_return
    if worst_point and worst_point.evaluation:
        result.net_total_return_at_worst = worst_point.evaluation.total_return

    # Cost fragility: is the worst-cost net total return a large fraction
    # below the gross?
    if (
        result.gross_evaluation is not None
        and worst_point is not None
        and worst_point.evaluation is not None
        and result.gross_evaluation.total_return > 0
    ):
        gross = result.gross_evaluation.total_return
        worst = worst_point.evaluation.total_return
        if gross > 0 and worst < gross * (1.0 - cfg.fragility_threshold):
            result.cost_fragile = True
            result.warnings.append(
                f"cost-fragile: worst-friction total_return {worst:.4f} is "
                f">{cfg.fragility_threshold:.0%} below gross {gross:.4f}"
            )

    if not result.points:
        result.warnings.append("no cost-sensitivity points produced")

    return result


def _scaled_config(
    base: BacktestConfig, *, cost_mult: float, slip_mult: float
) -> BacktestConfig:
    """Return a copy of ``base`` with the cost/slippage scaled by the multipliers."""
    from dataclasses import replace

    new_cost = float(base.transaction_cost_pct) * float(cost_mult)
    new_slip = float(base.slippage_pct) * float(slip_mult)
    return replace(base, transaction_cost_pct=new_cost, slippage_pct=new_slip)


__all__ = [
    "CostSensitivityConfig",
    "CostSensitivityPoint",
    "CostSensitivityResult",
    "run_cost_sensitivity",
]