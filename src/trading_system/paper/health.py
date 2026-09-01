"""Phase 19 — Paper health monitor.

Deterministically classifies the operational health of a paper deployment as
HEALTHY / WARNING / HALTED based purely on explicit, caller-supplied
thresholds. There are no hidden magic numbers: every limit lives in
``PaperHealthConfig`` and an unset limit (``None``) means "do not evaluate".

The monitor is a pure observer: it never mutates the broker, runner, or
deployment. It only reads the operational state and the broker's public views.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from ..paper_trading import Position
from .operations import HealthStatus


class PaperHealthConfig(BaseModel):
    """Explicit, optional health thresholds. ``None`` = not enforced."""

    model_config = ConfigDict(extra="forbid")

    # Warning when abs(drawdown) fraction approaches this (e.g. 0.10 = 10%).
    warn_drawdown_pct: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    # Halt when abs(drawdown) fraction breaches this.
    halt_drawdown_pct: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    # Warning / halt when rejected-order count reaches this.
    warn_rejected_orders: Optional[int] = Field(default=None, ge=0)
    halt_rejected_orders: Optional[int] = Field(default=None, ge=0)

    # Warning / halt on consecutive processing errors.
    warn_consecutive_errors: Optional[int] = Field(default=None, ge=0)
    halt_consecutive_errors: Optional[int] = Field(default=None, ge=0)

    # Warning when no fill has occurred after this many processed bars.
    warn_bars_without_fill: Optional[int] = Field(default=None, ge=0)

    # Warning when position exposure (|market value| / equity) exceeds this.
    warn_exposure_pct: Optional[float] = Field(default=None, ge=0.0)


@dataclass
class HealthEvaluation:
    """Outcome of a health evaluation."""

    status: HealthStatus = HealthStatus.HEALTHY
    warnings: list[str] = field(default_factory=list)
    halt_reason: Optional[str] = None

    @property
    def is_halted(self) -> bool:
        return self.status == HealthStatus.HALTED

    @property
    def allowed_to_trade(self) -> bool:
        return self.status != HealthStatus.HALTED


class PaperHealthMonitor:
    """Deterministic paper-only health monitor."""

    def __init__(self, config: Optional[PaperHealthConfig] = None) -> None:
        self.config = config or PaperHealthConfig()

    # ------------------------------------------------------------------ #
    # Core evaluation
    # ------------------------------------------------------------------ #
    def evaluate(
        self,
        *,
        deployment_status: str,
        processed_bars: int,
        filled_orders: int,
        rejected_orders: int,
        consecutive_errors: int,
        max_drawdown: Optional[float],
        equity: Optional[float],
        position: Optional[Position],
    ) -> HealthEvaluation:
        """Classify operational health from current state.

        ``max_drawdown`` is expected as a negative fraction (e.g. -0.05) or 0,
        matching the convention used throughout the paper layer.
        """
        cfg = self.config
        result = HealthEvaluation()

        # Deployment lifecycle overrides everything.
        if deployment_status in ("stopped", "failed"):
            result.status = HealthStatus.HALTED
            result.halt_reason = f"deployment is {deployment_status}"
            return result

        # ---- Halt conditions (most severe first) ----
        if _drawdown_breaches(cfg.halt_drawdown_pct, max_drawdown):
            result.status = HealthStatus.HALTED
            result.halt_reason = (
                f"max drawdown {max_drawdown:.4f} breached halt limit "
                f"{cfg.halt_drawdown_pct:.4f}"
            )
            return result

        if _count_reaches(cfg.halt_rejected_orders, rejected_orders):
            result.status = HealthStatus.HALTED
            result.halt_reason = (
                f"rejected orders {rejected_orders} reached halt limit "
                f"{cfg.halt_rejected_orders}"
            )
            return result

        if _count_reaches(cfg.halt_consecutive_errors, consecutive_errors):
            result.status = HealthStatus.HALTED
            result.halt_reason = (
                f"consecutive errors {consecutive_errors} reached halt limit "
                f"{cfg.halt_consecutive_errors}"
            )
            return result

        # ---- Warning conditions ----
        if _drawdown_breaches(cfg.warn_drawdown_pct, max_drawdown):
            result.warnings.append(
                f"drawdown {max_drawdown:.4f} approaching limit "
                f"{cfg.warn_drawdown_pct:.4f}"
            )

        if _count_reaches(cfg.warn_rejected_orders, rejected_orders):
            result.warnings.append(
                f"rejected orders {rejected_orders} >= {cfg.warn_rejected_orders}"
            )

        if _count_reaches(cfg.warn_consecutive_errors, consecutive_errors):
            result.warnings.append(
                f"consecutive errors {consecutive_errors} >= "
                f"{cfg.warn_consecutive_errors}"
            )

        if (
            cfg.warn_bars_without_fill is not None
            and processed_bars >= cfg.warn_bars_without_fill
            and filled_orders == 0
        ):
            result.warnings.append(
                f"no fill after {processed_bars} processed bars"
            )

        exposure = _exposure_fraction(equity, position)
        if cfg.warn_exposure_pct is not None and exposure is not None:
            if exposure >= cfg.warn_exposure_pct:
                result.warnings.append(
                    f"exposure {exposure:.4f} >= limit {cfg.warn_exposure_pct:.4f}"
                )

        if result.warnings:
            result.status = HealthStatus.WARNING

        return result


# --------------------------------------------------------------------------- #
# Pure helper functions (no hidden state).
# --------------------------------------------------------------------------- #
def _drawdown_breaches(
    limit: Optional[float], max_drawdown: Optional[float]
) -> bool:
    """True when abs(drawdown fraction) reaches ``limit``."""
    if limit is None or max_drawdown is None:
        return False
    return abs(max_drawdown) >= limit


def _count_reaches(limit: Optional[int], value: int) -> bool:
    if limit is None:
        return False
    return value >= limit


def _exposure_fraction(
    equity: Optional[float], position: Optional[Position]
) -> Optional[float]:
    """|market value| / equity, or None when it cannot be computed."""
    if equity is None or position is None or not position.is_open:
        return None
    if not equity > 0:
        return None
    return abs(position.market_value) / equity
