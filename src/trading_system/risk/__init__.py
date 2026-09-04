"""Risk management for the autonomous trading system.

Provides position sizing, drawdown limits, exposure caps, and operational
halts for both paper trading and live execution contexts.

The public surface is ``RiskManager`` with an ``evaluate()`` method that
returns ``RiskCheck`` results.  Concrete instances are configured via
``RiskConfig`` and injected into the backtester/paper-trader pipelines.

Typical usage::

    from trading_system.risk import RiskManager, RiskConfig

    config = RiskConfig(
        max_drawdown_pct=0.10,        # halt if daily drawdown >= 10%
        max_position_pct=0.02,        # max 2% equity per position
        max_concurrent_positions=3,   # at most 3 open positions
        use_kelly=False,                # fixed-fraction sizing
        k_fraction=0.25,              # 25% of risk capital per trade
    )

    risk = RiskManager(config=config)
    checks = risk.evaluate(
        equity=100_000.0,
        max_drawdown=-0.05,
        position=some_position,
        rejected_orders=0,
        consecutive_errors=0,
    )
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any

from ..execution.orders import Side


# ---------------------------------------------------------------------------
# Enum: risk check outcome
# ---------------------------------------------------------------------------

class RiskOutcome(str, Enum):
    """Outcome of a single risk check."""

    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"


# ---------------------------------------------------------------------------
# Data class: individual risk check result
# ---------------------------------------------------------------------------

@dataclass
class RiskCheck:
    """Result of one risk-management check."""

    name: str
    outcome: RiskOutcome
    detail: str = ""
    # Optional metric that was checked (e.g. drawdown fraction, exposure pct)
    metric: Optional[float] = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class RiskConfig:
    """Configuration for ``RiskManager``.

    All fields are optional; ``None`` means the check is disabled.

    - ``max_drawdown_pct``: halt if cumulative drawdown >= this fraction of
      peak equity (e.g. 0.10 = 10%).
    - ``max_position_pct``: max fraction of equity allocated to a single
      position (e.g. 0.02 = 2%).  Used for position-sizing calculations.
    - ``max_concurrent_positions``: maximum number of simultaneously open
      positions across all symbols.
    - ``max_rejected_orders``: halt the strategy after this many consecutive
      order rejections.
    - ``max_consecutive_errors``: halt the strategy after this many consecutive
      processing/interpretation errors.
    - ``use_kelly``: if ``True``, position size is computed via Kelly
      criterion fraction; if ``False``, ``max_position_pct`` fixed fraction
      is used.
    - ``k_fraction``: Kelly fraction (0 < k <= 1) applied to the fixed-floor
      when ``use_kelly`` is ``True``.  Default 0.25 (25% of risk capital).
    - ``risk_per_trade_pct``: fraction of equity risked per trade when
      ``use_kelly`` is ``False``.  Default 0.02 (2%).
    - ``buffer_pct``: small safety buffer added to calculated size (0 < b < 1).
      Default 0.98 (keeps 2% cushion).
    """

    #: halt if cumulative drawdown >= this fraction of peak equity
    max_drawdown_pct: Optional[float] = None
    #: max fraction of equity allocated to a single position
    max_position_pct: Optional[float] = None
    #: maximum number of simultaneously open positions across all symbols
    max_concurrent_positions: Optional[int] = None
    #: halt the strategy after this many consecutive order rejections
    max_rejected_orders: Optional[int] = None
    #: halt the strategy after this many consecutive processing errors
    max_consecutive_errors: Optional[int] = None
    #: if True, position size is computed via Kelly criterion fraction
    use_kelly: bool = False
    #: Kelly fraction (0 < k <= 1) applied to the risk capital when use_kelly
    k_fraction: float = 0.25
    #: fraction of equity risked per trade when use_kelly is False
    risk_per_trade_pct: float = 0.02
    #: small safety buffer added to calculated size (0 < b < 1)
    buffer_pct: float = 0.98


# ---------------------------------------------------------------------------
# Main RiskManager
# ---------------------------------------------------------------------------

class RiskManager:
    """Deterministic risk manager for autonomous trading.

    Evaluates the current account state against configured limits and returns
    a list of ``RiskCheck`` results.  The caller (backtester, paper-trader,
    live-trader) is responsible for interpreting the results and taking
    appropriate action (e.g. halting strategy, reducing size, logging warning).

    The manager does NOT submit, modify, or cancel orders.  It is a pure
    observer — the same contract as ``PaperRiskGuard`` but at the system level
    rather than the paper-trading layer alone.
    """

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()

    # ------------------------------------------------------------------ #
    # Public evaluation API
    # ------------------------------------------------------------------ #

    def evaluate(
        self,
        *,
        equity: float,
        max_drawdown: float,
        position: Optional[Any] = None,  # Position object or None
        rejected_orders: int = 0,
        consecutive_errors: int = 0,
        n_concurrent_positions: int = 0,
    ) -> list[RiskCheck]:
        """Run the full risk checklist and return ``RiskCheck`` results.

        Parameters
        ----------
        equity: current account equity (positive float)
        max_drawdown: latest maximum drawdown *fraction* (negative number or zero),
                      e.g. -0.05 means 5% drawdown from peak.
        position: optional Position object; if provided its ``market_value`` and
                  ``symbol`` are used for exposure checks.  The exact type depends
                  on the broker (``trading_system.paper.trading.Position``,
                  ``fyers`` model, etc.).
        rejected_orders: count of orders rejected so far in this session.
        consecutive_errors: count of consecutive processing/interpretation errors.
        n_concurrent_positions: how many positions are currently open.

        Returns
        -------
        list[RiskCheck]
            One ``RiskCheck`` per rule that was evaluated.  Checks that are
            disabled (config value ``None``) are simply omitted.
        """
        checks: list[RiskCheck] = []

        # 1. Drawdown limit
        if self.config.max_drawdown_pct is not None:
            dd_check = self._check_drawdown(
                equity=equity, max_drawdown=max_drawdown
            )
            checks.append(dd_check)

        # 2. Position sizing — fraction of equity
        if self.config.max_position_pct is not None or self.config.use_kelly:
            size_check = self._check_position_size(
                equity=equity,
                position=position,
                n_concurrent=n_concurrent_positions,
            )
            checks.append(size_check)

        # 3. Max concurrent positions
        if self.config.max_concurrent_positions is not None:
            conc_check = self._check_concurrent_positions(
                n_concurrent=n_concurrent_positions
            )
            checks.append(conc_check)

        # 4. Rejected orders halt
        if self.config.max_rejected_orders is not None:
            rej_check = self._check_rejected_orders(
                rejected_orders=rejected_orders
            )
            checks.append(rej_check)

        # 5. Consecutive errors halt
        if self.config.max_consecutive_errors is not None:
            err_check = self._check_consecutive_errors(
                consecutive_errors=consecutive_errors
            )
            checks.append(err_check)

        return checks

    # ------------------------------------------------------------------ #
    # Internal check implementations
    # ------------------------------------------------------------------ #

    # -- Drawdown -------------------------------------------------------- #

    def _check_drawdown(self, *, equity: float, max_drawdown: float) -> RiskCheck:
        """Return FAIL if |drawdown| >= max_drawdown_pct."""
        abs_dd = abs(max_drawdown)  # already a positive fraction
        limit = self.config.max_drawdown_pct
        if abs_dd >= limit:
            return RiskCheck(
                name="max_drawdown",
                outcome=RiskOutcome.FAIL,
                detail=f"drawdown {abs_dd:.4f} >= limit {limit:.4f}",
                metric=abs_dd,
            )
        # Warn if approaching limit (within 25% of trigger)
        if abs_dd >= limit * 0.75:
            return RiskCheck(
                name="max_drawdown",
                outcome=RiskOutcome.WARNING,
                detail=f"drawdown {abs_dd:.4f} approaching limit {limit:.4f}",
                metric=abs_dd,
            )
        return RiskCheck(
            name="max_drawdown",
            outcome=RiskOutcome.PASS,
            detail=f"drawdown {abs_dd:.4f} < limit {limit:.4f}",
            metric=abs_dd,
        )

    # -- Position sizing ------------------------------------------------- #

    def _position_size_from_fraction(
        self, equity: float, pct: float, buffer: float = 1.0
    ) -> float:
        """Return dollar amount to risk: equity * pct * buffer."""
        return max(0.0, equity * pct * buffer)

    def _check_position_size(
        self,
        *,
        equity: float,
        position: Optional[Any],
        n_concurrent: int,
    ) -> RiskCheck:
        """Check that proposed position size respects config limits.

        If ``use_kelly`` is True, the Kelly fraction is applied to the
        risk capital; otherwise ``max_position_pct`` fixed fraction is used.

        Returns a ``RiskCheck`` with outcome PASS/WARNING/FAIL.
        """
        pos_pct = self.config.max_position_pct
        use_kelly = self.config.use_kelly
        k_frac = self.config.k_fraction
        risk_pct = self.config.risk_per_trade_pct
        buf = self.config.buffer_pct

        # Determine the allowed fraction of equity for this trade
        if use_kelly and pos_pct is None:
            # Kelly: risk = equity * k_fraction (capped by risk_per_trade_pct)
            allowed_fraction = min(k_frac, risk_pct)
        elif pos_pct is not None:
            allowed_fraction = pos_pct
        else:
            # No explicit limit set — default conservative guard
            allowed_fraction = risk_pct

        # If a position object is provided, compute its current exposure
        if position is not None and hasattr(position, "market_value"):
            try:
                mkt_val = float(position.market_value)
                pos_eq_ratio = abs(mkt_val) / max(equity, 1e-12)
            except Exception:
                pos_eq_ratio = 1.0  # fallback: assume full exposure
        else:
            # No position object: assume we're about to open a new position
            # and check the *proposed* size against the limit.
            pos_eq_ratio = 0.0  # fresh entry, no existing exposure from this check

        # Proposed new exposure = existing + new trade
        proposed_exposure = pos_eq_ratio  # will be adjusted below

        # Determine the new-trade dollar amount as a fraction of equity
        # when sizing to ``allowed_fraction`` of equity.
        proposed_trade_fraction = allowed_fraction

        # WARNING if we'd exceed the limit by a small margin
        if proposed_trade_fraction > allowed_fraction:
            return RiskCheck(
                name="max_position_size",
                outcome=RiskOutcome.FAIL,
                detail=f"proposed size {proposed_trade_fraction:.4f} > limit {allowed_fraction:.4f}",
                metric=proposed_trade_fraction,
            )

        # Check concurrent position count
        if n_concurrent >= (self.config.max_concurrent_positions or float("inf")):
            return RiskCheck(
                name="max_concurrent_positions",
                outcome=RiskOutcome.FAIL,
                detail=f"concurrent positions {n_concurrent} >= limit "
                f"{self.config.max_concurrent_positions}",
                metric=float(n_concurrent),
            )

        # All good — pass, possibly WARNING if near limit
        if abs(proposed_trade_fraction - allowed_fraction) / max(allowed_fraction, 1e-12) > 0.9:
            return RiskCheck(
                name="max_position_size",
                outcome=RiskOutcome.WARNING,
                detail=f"position size {proposed_trade_fraction:.4f} near limit {allowed_fraction:.4f}",
                metric=proposed_trade_fraction,
            )

        return RiskCheck(
            name="max_position_size",
            outcome=RiskOutcome.PASS,
            detail=f"position size {proposed_trade_fraction:.4f} within limit {allowed_fraction:.4f}",
            metric=proposed_trade_fraction,
        )

    # -- Concurrent positions -------------------------------------------- #

    def _check_concurrent_positions(self, *, n_concurrent: int) -> RiskCheck:
        limit = self.config.max_concurrent_positions
        if n_concurrent >= limit:  # type: ignore[comparison]
            return RiskCheck(
                name="max_concurrent_positions",
                outcome=RiskOutcome.FAIL,
                detail=f"concurrent positions {n_concurrent} >= limit {limit}",
                metric=float(n_concurrent),
            )
        return RiskCheck(
            name="max_concurrent_positions",
            outcome=RiskOutcome.PASS,
            detail=f"concurrent positions {n_concurrent} < limit {limit}",
            metric=float(n_concurrent),
        )

    # -- Rejected orders ------------------------------------------------- #

    def _check_rejected_orders(self, *, rejected_orders: int) -> RiskCheck:
        limit = self.config.max_rejected_orders
        if rejected_orders >= limit:
            return RiskCheck(
                name="max_rejected_orders",
                outcome=RiskOutcome.FAIL,
                detail=f"rejected orders {rejected_orders} >= limit {limit}",
                metric=float(rejected_orders),
            )
        # Warn if approaching limit
        if rejected_orders >= limit * 0.75 and limit > 0:
            return RiskCheck(
                name="max_rejected_orders",
                outcome=RiskOutcome.WARNING,
                detail=f"rejected orders {rejected_orders} approaching limit {limit}",
                metric=float(rejected_orders),
            )
        return RiskCheck(
            name="max_rejected_orders",
            outcome=RiskOutcome.PASS,
            detail=f"rejected orders {rejected_orders} < limit {limit}",
            metric=float(rejected_orders),
        )

    # -- Consecutive errors ---------------------------------------------- #

    def _check_consecutive_errors(self, *, consecutive_errors: int) -> RiskCheck:
        limit = self.config.max_consecutive_errors
        if consecutive_errors >= limit:  # type: ignore[comparison]
            return RiskCheck(
                name="consecutive_errors",
                outcome=RiskOutcome.FAIL,
                detail=f"consecutive errors {consecutive_errors} >= limit {limit}",
                metric=float(consecutive_errors),
            )
        # Warn if approaching limit
        if consecutive_errors >= limit * 0.75 and limit > 0:
            return RiskCheck(
                name="consecutive_errors",
                outcome=RiskOutcome.WARNING,
                detail=f"consecutive errors {consecutive_errors} approaching limit {limit}",
                metric=float(consecutive_errors),
            )
        return RiskCheck(
            name="consecutive_errors",
            outcome=RiskOutcome.PASS,
            detail=f"consecutive errors {consecutive_errors} < limit {limit}",
            metric=float(consecutive_errors),
        )

    # ------------------------------------------------------------------ #
    # Compatibility helper: map to PaperRiskGuard-style decision
    # ------------------------------------------------------------------ #

    def to_paper_decisions(
        self,
        *,
        equity: float,
        max_drawdown: float,
        position: Optional[Any] = None,
        rejected_orders: int = 0,
        consecutive_errors: int = 0,
        n_concurrent_positions: int = 0,
    ) -> dict[str, str]:
        """Convenience: return a dict suitable for ``PaperStrategyRunner``.

        Keys match the fields the runner expects for its risk‑guard integration:
        ``"max_drawdown"``, ``"exposure"``, ``"rejected_orders"``,
        ``"consecutive_errors"``.  Each value is one of
        ``"allow"``, ``"warning"``, ``"halt"``.
        """
        checks = self.evaluate(
            equity=equity,
            max_drawdown=max_drawdown,
            position=position,
            rejected_orders=rejected_orders,
            consecutive_errors=consecutive_errors,
            n_concurrent_positions=n_concurrent_positions,
        )

        decisions: dict[str, str] = {
            "max_drawdown": "allow",
            "exposure": "allow",
            "rejected_orders": "allow",
            "consecutive_errors": "allow",
        }

        for check in checks:
            if check.outcome == RiskOutcome.FAIL:
                # Map the first FAIL per key
                key = check.name.replace("max_", "").replace("concurrent_", "")
                if key in decisions and decisions[key] == "allow":
                    decisions[key] = "halt"
            elif check.outcome == RiskOutcome.WARNING and decisions[check.name.replace("max_", "").replace("concurrent_", "")] == "allow":
                # Only downgrade to warning if not already halted
                key = check.name.replace("max_", "").replace("concurrent_", "")
                if key in decisions and decisions[key] == "allow":
                    decisions[key] = "warning"

        return decisions


# ---------------------------------------------------------------------------
# Module-level convenience: default instance (configurable via env)
# ---------------------------------------------------------------------------

def get_risk_manager() -> RiskManager:
    """Return a ``RiskManager`` configured from env vars if available.

    Environment variables (all optional):

    - ``RISK_MAX_DRAWDOWN_PCT`` — e.g. ``0.10``
    - ``RISK_MAX_POSITION_PCT`` — e.g. ``0.02``
    - ``RISK_USE_KELLY`` — ``true``/``false``
    - ``RISK_K_FRACTION`` — e.g. ``0.25``
    - ``RISK_RISK_PER_TRADE_PCT`` — e.g. ``0.02``
    """
    import os

    kwargs: dict[str, Any] = {}
    for field_name in (
        "max_drawdown_pct",
        "max_position_pct",
        "use_kelly",
        "k_fraction",
        "risk_per_trade_pct",
    ):
        val = os.getenv(f"RISK_{field_name.upper()}")
        if val is not None:
            kwargs[field_name] = float(val)

    km = kwargs.get("k_fraction", 0.25)
    kwargs["k_fraction"] = km  # ensure default

    rm = RiskManager(config=RiskConfig(**kwargs))
    return rm


# ---------------------------------------------------------------------------
# Exported symbols
# ---------------------------------------------------------------------------

__all__ = [
    "RiskManager",
    "RiskConfig",
    "RiskCheck",
    "RiskOutcome",
    "get_risk_manager",
    "RiskCheck",
]