"""Phase 23 configuration (deterministic, auditable).

All thresholds and weights are configurable and documented. The defaults
represent a balanced research-quality gate; they are NOT guarantees and
should be calibrated to the dataset/instrument under study.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class RejectionThresholds:
    """Hard rejection gates. A strategy failing ANY gate is rejected."""

    min_trades: int = 20
    """Minimum number of trades for a stable backtest read."""

    min_validation_trades: int = 3
    """Minimum trades per walk-forward validation fold."""

    min_walk_forward_coverage: float = 0.5
    """Minimum fraction of folds that must produce valid results."""

    max_drawdown: float = 0.30
    """Maximum acceptable drawdown (fraction of capital)."""

    min_total_return: float = 0.0
    """Minimum net total return at base cost."""

    min_win_rate: float = 0.30
    """Minimum win rate."""

    min_profit_factor: float = 0.80
    """Minimum profit factor (gross win / gross loss)."""

    max_turnover: float = 10.0
    """Maximum average turnover (trades per period)."""

    max_cost_sensitivity: float = 0.50
    """Maximum acceptable cost fragility ratio (worst/gross return)."""

    min_parameter_stability: float = 0.40
    """Minimum parameter stability score [0, 1]."""

    min_regime_diversity: float = 0.30
    """Minimum fraction of regimes with positive return."""

    min_cross_sectional_coverage: float = 0.40
    """Minimum fraction of instruments with positive return."""

    max_concentration: float = 0.60
    """Maximum P&L concentration in a single instrument."""

    max_correlation: float = 0.80
    """Maximum return correlation with an already-selected strategy."""

    min_bootstrap_prob_positive: float = 0.45
    """Minimum bootstrap probability of positive performance."""


@dataclass(frozen=True)
class RobustnessWeights:
    """Configurable weights for the robustness score.

    All components are normalized to [0, 1] before weighting. Unknown
    components are excluded from the composite (not zeroed).
    """

    oos_quality: float = 0.15
    walk_forward_consistency: float = 0.15
    cross_sectional_generalization: float = 0.10
    bootstrap_stability: float = 0.10
    cost_resilience: float = 0.15
    risk_adjusted_performance: float = 0.15
    parameter_stability: float = 0.10
    trade_count_penalty_weight: float = 0.05
    drawdown_penalty: float = 0.05
    concentration_penalty: float = 0.05
    turnover_penalty: float = 0.05
    overfitting_penalty: float = 0.05


@dataclass(frozen=True)
class Phase23Config:
    """Top-level Phase 23 tournament configuration."""

    tournament_id: str = "phase23-default"
    code_version: str = ""
    dataset_version: str = "default"
    universe_version: str = "v1"
    cost_model_version: str = "v1"
    scoring_version: str = "v1"
    random_seed: int = 42

    rejection: RejectionThresholds = field(default_factory=RejectionThresholds)
    weights: RobustnessWeights = field(default_factory=RobustnessWeights)

    # Tournament execution
    target_candidates: int = 100
    top_n_report: int = 20
    top_n_qualified: int = 10
    top_n_deploy: int = 5

    # Walk-forward
    wf_n_folds: int = 5
    wf_validation_window: int = 60
    wf_train_window: int = 120
    wf_step_size: int = 60
    wf_warmup_bars: int = 50
    wf_min_validation_trades: int = 1
    wf_min_fold_coverage: float = 0.2

    # Bootstrap
    bootstrap_n: int = 1000
    bootstrap_alpha: float = 0.05

    # Cross-sectional
    min_instruments_for_cross_section: int = 5

    # Cost scenarios
    cost_baseline_bps: float = 6.0
    cost_elevated_bps: float = 15.0
    cost_adverse_bps: float = 25.0

    # Paper deployment
    paper_initial_cash: float = 100_000.0
    paper_slippage_bps: float = 5.0
    paper_fee_bps: float = 0.0


DEFAULT_CONFIG = Phase23Config()
