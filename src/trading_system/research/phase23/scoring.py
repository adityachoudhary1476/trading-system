"""Deterministic robustness scoring and rejection gates (Phase 24).

This module provides:
- `RobustnessScorer`: computes a deterministic composite robustness score
- `RejectionGate`: applies hard rejection thresholds with machine-readable reasons

All scoring is deterministic, configurable, and based on the existing
`RankingConfig` / `rank_candidates` infrastructure extended with Phase 24
validation dimensions.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from typing import Any

from .config import (
    DEFAULT_CONFIG,
    Phase23Config,
    RejectionThresholds,
    RobustnessWeights,
)

__all__ = [
    "GatedResult",
    "RejectionGate",
    "RejectionReason",
    "RobustnessScorer",
    "ScorerInput",
    "ScorerOutput",
]


# --------------------------------------------------------------------------- #
# Rejection reasons (machine-readable)
# --------------------------------------------------------------------------- #
class RejectionReason:
    INVALID_SPEC = "INVALID_SPEC"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    INSUFFICIENT_TRADES = "INSUFFICIENT_TRADES"
    NEGATIVE_TOTAL_RETURN = "NEGATIVE_TOTAL_RETURN"
    EXCESSIVE_DRAWDOWN = "EXCESSIVE_DRAWDOWN"
    LOW_WIN_RATE = "LOW_WIN_RATE"
    LOW_PROFIT_FACTOR = "LOW_PROFIT_FACTOR"
    HIGH_COST_SENSITIVITY = "HIGH_COST_SENSITIVITY"
    POOR_WALK_FORWARD = "POOR_WALK_FORWARD"
    LOW_BOOTSTRAP_PROBABILITY = "LOW_BOOTSTRAP_PROBABILITY"
    HIGH_CONCENTRATION = "HIGH_CONCENTRATION"
    HIGH_CORRELATION = "HIGH_CORRELATION"
    UNSTABLE_PARAMETERS = "UNSTABLE_PARAMETERS"
    INSUFFICIENT_CROSS_SECTION = "INSUFFICIENT_CROSS_SECTION"
    OVERFIT = "OVERFIT"
    SCORE_BELOW_THRESHOLD = "SCORE_BELOW_THRESHOLD"


# --------------------------------------------------------------------------- #
# Input / Output dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class ScorerInput:
    """All metrics needed for robustness scoring."""
    # Backtest metrics
    total_return: float = 0.0
    cagr: float = 0.0
    volatility: float = 0.0
    sharpe: float | None = None
    sortino: float | None = None
    max_drawdown: float = 0.0
    calmar: float | None = None
    win_rate: float | None = None
    profit_factor: float | None = None
    avg_trade: float | None = None
    trade_count: int = 0
    turnover: float = 0.0
    exposure: float = 0.0
    gross_pnl: float = 0.0
    costs: float = 0.0
    slippage: float = 0.0
    net_pnl: float = 0.0

    # Walk-forward
    wf_consistency_score: float | None = None
    wf_oos_sharpe: float | None = None
    wf_oos_return: float | None = None
    wf_median_return: float | None = None
    wf_degradation: float | None = None
    wf_coverage: float = 0.0

    # Bootstrap
    bootstrap_prob_positive: float = 0.0
    bootstrap_return_ci: tuple[float, float] = (0.0, 0.0)
    bootstrap_sharpe_ci: tuple[float, float] = (0.0, 0.0)

    # Cross-sectional
    cross_section_median: float | None = None
    cross_section_profitable_pct: float = 0.0
    cross_section_dispersion: float | None = None
    cross_section_worst: float | None = None
    cross_section_concentration: float = 0.0

    # Cost resilience
    cost_adjusted_return_baseline: float = 0.0
    cost_adjusted_return_elevated: float = 0.0
    cost_adjusted_return_adverse: float = 0.0
    cost_sensitivity: float = 0.0

    # Parameter stability (0-1, higher is more stable)
    parameter_stability: float = 1.0

    # Correlation / similarity
    max_correlation: float = 0.0
    correlation_cluster_id: str | None = None

    # Overfitting signals
    is_overfit: bool = False
    train_test_gap: float | None = None


@dataclass
class ScorerOutput:
    score: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    penalties: dict[str, float] = field(default_factory=dict)
    rejection_reasons: list[str] = field(default_factory=list)
    passed: bool = False


# --------------------------------------------------------------------------- #
# Robustness Scorer
# --------------------------------------------------------------------------- #
class RobustnessScorer:
    """Deterministic robustness scorer using configurable weights."""

    def __init__(self, config: Phase23Config | None = None) -> None:
        self.config = config or DEFAULT_CONFIG
        self.weights: RobustnessWeights = self.config.weights

    def score(self, inp: ScorerInput) -> ScorerOutput:
        """Compute a deterministic robustness score and apply rejection gates."""
        output = ScorerOutput()

        # ---- Core components (0-1 normalized, capped) ----
        components: dict[str, float] = {}
        penalties: dict[str, float] = {}

        # 1. OOS quality (Sharpe-like)
        oos_quality = self._cap(self._safe_sharpe(inp.sharpe), 3.0)
        if inp.wf_oos_sharpe is not None:
            oos_quality = max(oos_quality, self._cap(self._safe_sharpe(inp.wf_oos_sharpe), 3.0))
        components["oos_quality"] = oos_quality

        # 2. Walk-forward consistency
        if inp.wf_consistency_score is not None:
            components["walk_forward_consistency"] = self._cap(inp.wf_consistency_score, 1.0)
        else:
            components["walk_forward_consistency"] = 0.0

        # 3. Cross-sectional generalization
        if inp.cross_section_profitable_pct is not None:
            components["cross_sectional_generalization"] = inp.cross_section_profitable_pct
        else:
            components["cross_sectional_generalization"] = 0.5  # neutral if unavailable

        # 4. Bootstrap stability
        components["bootstrap_stability"] = inp.bootstrap_prob_positive

        # 5. Cost resilience
        if inp.cost_sensitivity > 0:
            cost_resilience = max(0.0, 1.0 - inp.cost_sensitivity)
        else:
            cost_resilience = 1.0
        components["cost_resilience"] = cost_resilience

        # 6. Risk-adjusted performance
        risk_adj = self._cap(self._safe_sharpe(inp.sharpe), 3.0)
        if inp.sortino is not None:
            risk_adj = max(risk_adj, self._cap(self._safe_sharpe(inp.sortino), 3.0))
        components["risk_adjusted_performance"] = risk_adj

        # 7. Parameter stability
        components["parameter_stability"] = self._cap(inp.parameter_stability, 1.0)

        # ---- Penalties ----
        # Drawdown penalty (linear beyond threshold)
        dd = abs(inp.max_drawdown)
        if dd > self.config.rejection.max_drawdown:
            penalties["drawdown"] = min(1.0, (dd - self.config.rejection.max_drawdown) / 0.5)
        else:
            penalties["drawdown"] = 0.0

        # Concentration penalty
        if inp.cross_section_concentration > self.config.rejection.max_concentration:
            penalties["concentration"] = min(1.0,
                (inp.cross_section_concentration - self.config.rejection.max_concentration) / 0.4)
        else:
            penalties["concentration"] = 0.0

        # Turnover penalty
        if inp.turnover > self.config.rejection.max_turnover:
            penalties["turnover"] = min(1.0,
                (inp.turnover - self.config.rejection.max_turnover) / 10.0)
        else:
            penalties["turnover"] = 0.0

        # Instability penalty (low walk-forward consistency)
        if components["walk_forward_consistency"] < 0.3:
            penalties["instability"] = 0.5
        else:
            penalties["instability"] = 0.0

        # Overfitting penalty
        if inp.is_overfit:
            penalties["overfitting"] = 0.5
        else:
            penalties["overfitting"] = 0.0

        # Trade count penalty
        if inp.trade_count < self.config.rejection.min_trades:
            penalties["trade_count"] = 0.5
        else:
            penalties["trade_count"] = 0.0

        # ---- Weighted composite ----
        score = 0.0
        weights_dict = dataclasses.asdict(self.weights)
        for key, weight in weights_dict.items():
            if key.startswith("_") or key.endswith(("_penalty", "_weight")):
                continue
            if key in components:
                score += weights_dict.get(key, 0.0) * components[key]
            elif key == "trade_count_penalty_weight":
                score -= weights_dict.get(key, 0.0) * penalties.get("trade_count", 0.0)

        # Apply penalty weights
        for penalty_key, penalty_value in penalties.items():
            weight_key = f"{penalty_key}_penalty"
            weight = weights_dict.get(weight_key, 0.0)
            score -= weight * penalty_value

        # Normalize to 0-100
        score = max(0.0, min(100.0, score * 100.0))

        output.score = round(score, 4)
        output.components = {k: round(v, 4) for k, v in components.items()}
        output.penalties = {k: round(v, 4) for k, v in penalties.items()}
        return output

    def _safe_sharpe(self, value: float | None) -> float:
        if value is None or not math.isfinite(value):
            return 0.0
        return max(-10.0, min(10.0, value))

    def _cap(self, value: float, cap: float) -> float:
        if not math.isfinite(value):
            return 0.0
        return max(0.0, min(cap, value))


# --------------------------------------------------------------------------- #
# Rejection Gate
# --------------------------------------------------------------------------- #
class RejectionGate:
    """Hard rejection gates with machine-readable reasons."""

    def __init__(self, config: Phase23Config | None = None) -> None:
        self.config = config or DEFAULT_CONFIG
        self.thresholds: RejectionThresholds = self.config.rejection

    def evaluate(self, inp: ScorerInput, evaluation: Any = None) -> GatedResult:
        """Return a GatedResult with explicit pass/fail and reasons."""
        reasons: list[str] = []

        trade_count = getattr(evaluation, "n_trades", inp.trade_count)
        total_return = getattr(evaluation, "total_return", inp.total_return)
        max_drawdown = getattr(evaluation, "max_drawdown", inp.max_drawdown)
        win_rate = getattr(evaluation, "win_rate", inp.win_rate)
        profit_factor = getattr(evaluation, "profit_factor", inp.profit_factor)

        if trade_count < self.thresholds.min_trades:
            reasons.append(RejectionReason.INSUFFICIENT_TRADES)
        if total_return < self.thresholds.min_total_return:
            reasons.append(RejectionReason.NEGATIVE_TOTAL_RETURN)
        if max_drawdown > self.thresholds.max_drawdown:
            reasons.append(RejectionReason.EXCESSIVE_DRAWDOWN)
        if win_rate is not None and win_rate < self.thresholds.min_win_rate:
            reasons.append(RejectionReason.LOW_WIN_RATE)
        if profit_factor is not None and profit_factor < self.thresholds.min_profit_factor:
            reasons.append(RejectionReason.LOW_PROFIT_FACTOR)

        if inp.cost_sensitivity > self.thresholds.max_cost_sensitivity:
            reasons.append(RejectionReason.HIGH_COST_SENSITIVITY)

        if inp.wf_coverage < self.thresholds.min_walk_forward_coverage:
            reasons.append(RejectionReason.POOR_WALK_FORWARD)

        if inp.bootstrap_prob_positive < self.thresholds.min_bootstrap_prob_positive:
            reasons.append(RejectionReason.LOW_BOOTSTRAP_PROBABILITY)

        if inp.cross_section_concentration > 0 and inp.cross_section_concentration > self.thresholds.max_concentration:
            reasons.append(RejectionReason.HIGH_CONCENTRATION)

        if inp.parameter_stability < self.thresholds.min_parameter_stability:
            reasons.append(RejectionReason.UNSTABLE_PARAMETERS)

        if inp.is_overfit:
            reasons.append(RejectionReason.OVERFIT)

        if inp.cross_section_profitable_pct < self.thresholds.min_cross_sectional_coverage and inp.cross_section_concentration > 0:
            reasons.append(RejectionReason.INSUFFICIENT_CROSS_SECTION)

        if inp.max_correlation > self.thresholds.max_correlation:
            reasons.append(RejectionReason.HIGH_CORRELATION)

        passed = len(reasons) == 0
        return GatedResult(passed=passed, rejection_reasons=reasons)


@dataclass
class GatedResult:
    passed: bool = False
    rejection_reasons: list[str] = field(default_factory=list)
