"""Phase 19 — Paper risk guard.

An additional operational circuit-breaker LAYER on top of the existing Phase 18
risk controls (``PaperDeploymentConfig`` max_allocation_pct / max_position_size
/ allow_short). This guard does NOT replace or widen those limits. It only
reads the broker's public views and, when an explicitly configured limit is
breached, returns a decision of ALLOW / WARNING / HALT.

The guard never submits, modifies, or cancels orders, and never changes
position sizing. It is a pure observer that produces a decision + reason.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from ..paper_trading import Position


class RiskDecision(str, Enum):
    """Operational decision from the risk guard."""

    ALLOW = "allow"
    WARNING = "warning"
    HALT = "halt"


class PaperRiskConfig(BaseModel):
    """Explicit, optional operational risk limits. ``None`` = not enforced."""

    model_config = ConfigDict(extra="forbid")

    # Halt when abs(drawdown) fraction reaches this.
    max_drawdown_pct: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    # Halt when position exposure (|market value| / equity) reaches this.
    max_position_value_pct: Optional[float] = Field(default=None, ge=0.0)
    # Halt when rejected-order count reaches this.
    max_rejected_orders: Optional[int] = Field(default=None, ge=0)
    # Halt when consecutive processing errors reach this.
    max_consecutive_errors: Optional[int] = Field(default=None, ge=0)


class PaperRiskGuard:
    """Deterministic, paper-only operational risk guard."""

    def __init__(self, config: Optional[PaperRiskConfig] = None) -> None:
        self.config = config or PaperRiskConfig()

    # ------------------------------------------------------------------ #
    # Core check
    # ------------------------------------------------------------------ #
    def check(
        self,
        *,
        max_drawdown: Optional[float],
        equity: Optional[float],
        position: Optional[Position],
        rejected_orders: int,
        consecutive_errors: int,
    ) -> tuple[RiskDecision, Optional[str]]:
        """Return (decision, reason). ``reason`` is None unless not ALLOW."""
        cfg = self.config

        if _dd_breaches(cfg.max_drawdown_pct, max_drawdown):
            return (
                RiskDecision.HALT,
                (
                    f"max drawdown {max_drawdown:.4f} reached limit "
                    f"{cfg.max_drawdown_pct:.4f}"
                ),
            )

        exposure = _exposure_fraction(equity, position)
        if cfg.max_position_value_pct is not None and exposure is not None:
            if exposure >= cfg.max_position_value_pct:
                return (
                    RiskDecision.HALT,
                    (
                        f"exposure {exposure:.4f} reached limit "
                        f"{cfg.max_position_value_pct:.4f}"
                    ),
                )

        if _count_reaches(cfg.max_rejected_orders, rejected_orders):
            return (
                RiskDecision.HALT,
                (
                    f"rejected orders {rejected_orders} reached limit "
                    f"{cfg.max_rejected_orders}"
                ),
            )

        if _count_reaches(cfg.max_consecutive_errors, consecutive_errors):
            return (
                RiskDecision.HALT,
                (
                    f"consecutive errors {consecutive_errors} reached limit "
                    f"{cfg.max_consecutive_errors}"
                ),
            )

        return (RiskDecision.ALLOW, None)


# --------------------------------------------------------------------------- #
# Shared pure helpers (duplicated locally to keep modules independent; no
# hidden state, trivially correct).
# --------------------------------------------------------------------------- #
def _dd_breaches(limit: Optional[float], max_drawdown: Optional[float]) -> bool:
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
    if equity is None or position is None or not position.is_open:
        return None
    if not equity > 0:
        return None
    return abs(position.market_value) / equity
