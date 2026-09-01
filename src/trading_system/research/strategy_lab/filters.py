"""Deterministic research-quality filters (Phase 13, Step 8).

A QualityFilter is a RESEARCH GATE, not a guarantee. Passing these filters does
NOT mean a strategy is profitable or will ever be profitable; it only means the
backtest result cleared configurable thresholds for statistical hygiene
(enough trades, tolerable drawdown, bounded exposure, enough data, sane risk
parameters). Every rejection reason is recorded verbatim for auditability.

All comparisons are deterministic — the same evaluation + config always yields
the same outcome.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .evaluation import StrategyEvaluation

__all__ = ["QualityFilterConfig", "FilterOutcome", "apply_quality_filter"]


@dataclass
class QualityFilterConfig:
    """Configurable research-quality thresholds (all deterministic)."""

    # Sample-size hygiene.
    min_trades: int = 5
    min_bars: int = 100                  # dataset must have at least this many bars
    require_reliable: bool = True        # honor PerformanceReport.reliable flag
    # Risk hygiene.
    max_drawdown: float = 0.5            # reject |max_drawdown| above this (fraction)
    max_exposure_pct: float = 1.0        # reject exposure above this (fraction)
    # Absolute result gates (None disables the gate).
    max_loss: Optional[float] = None     # reject net_pnl below this (currency)
    min_total_return: Optional[float] = None  # reject total_return below this (fraction)

    def validate(self) -> list[str]:
        """Config sanity (also deterministic)."""
        problems: list[str] = []
        if self.min_trades < 0:
            problems.append("min_trades must be >= 0")
        if self.min_bars < 0:
            problems.append("min_bars must be >= 0")
        if not 0.0 < self.max_drawdown <= 1.0:
            problems.append("max_drawdown must be in (0, 1]")
        if not 0.0 < self.max_exposure_pct <= 1.0:
            problems.append("max_exposure_pct must be in (0, 1]")
        return problems


@dataclass
class FilterOutcome:
    passed: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return not self.passed


def apply_quality_filter(
    evaluation: StrategyEvaluation,
    config: QualityFilterConfig,
    *,
    spec_errors: Optional[list[str]] = None,
    dataset_rows: Optional[int] = None,
) -> FilterOutcome:
    """Apply the configured gates. Returns a FilterOutcome with all reasons."""
    reasons: list[str] = []

    # Invalid risk parameters / malformed spec can never pass.
    if spec_errors:
        reasons.append(f"invalid strategy specification: {'; '.join(spec_errors)}")

    if dataset_rows is not None and dataset_rows < config.min_bars:
        reasons.append(
            f"insufficient historical data: {dataset_rows} bars < min_bars {config.min_bars}"
        )

    if evaluation.n_trades < config.min_trades:
        reasons.append(
            f"too few trades: {evaluation.n_trades} < min_trades {config.min_trades} "
            "(tiny sample; any ranking would be noise)"
        )

    if abs(evaluation.max_drawdown) > config.max_drawdown:
        reasons.append(
            f"max drawdown {evaluation.max_drawdown:.4f} exceeds "
            f"allowed {config.max_drawdown:.4f}"
        )

    if config.max_loss is not None and evaluation.net_pnl < config.max_loss:
        reasons.append(
            f"net P&L {evaluation.net_pnl:.2f} is below max_loss {config.max_loss:.2f}"
        )

    if (config.min_total_return is not None
            and evaluation.total_return < config.min_total_return):
        reasons.append(
            f"total return {evaluation.total_return:.4f} is below "
            f"min_total_return {config.min_total_return:.4f}"
        )

    if evaluation.exposure_pct > config.max_exposure_pct:
        reasons.append(
            f"exposure {evaluation.exposure_pct:.4f} exceeds "
            f"max_exposure_pct {config.max_exposure_pct:.4f}"
        )

    if config.require_reliable and not evaluation.reliable:
        reasons.append("performance metrics flagged unreliable (small sample)")

    return FilterOutcome(passed=not reasons, reasons=reasons)
