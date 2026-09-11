"""Paper deployment policy (Phase 24).

Creates paper deployments for qualified strategies using the existing
PaperTradingControlCenter. Enforces paper-only mode and idempotency.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ...paper.control import PaperTradingControlCenter
from ...paper.deployment import (
    PaperDeployment,
    PaperDeploymentConfig,
    deployment_identity,
)
from ..evidence import strategy_identity
from ..strategy_lab.spec import StrategySpec

__all__ = [
    "DeploymentResult",
    "Phase23DeploymentPolicy",
]

logger = logging.getLogger(__name__)


@dataclass
class DeploymentResult:
    strategy_id: str
    candidate_id: str
    deployment: PaperDeployment | None
    success: bool
    error: str | None = None
    is_duplicate: bool = False


class Phase23DeploymentPolicy:
    """Paper-only deployment policy for tournament-qualified strategies."""

    def __init__(
        self,
        control_center: PaperTradingControlCenter,
        dataset_id: str = "tournament",
        initial_cash: float = 100_000.0,
    ) -> None:
        self._cc = control_center
        self._dataset_id = dataset_id
        self._initial_cash = initial_cash
        self._created: dict[str, PaperDeployment] = {}

    def deploy(
        self,
        *,
        strategy_id: str,
        candidate_id: str,
        spec: StrategySpec,
        symbol: str,
        timeframe: str,
        activate: bool = True,
    ) -> DeploymentResult:
        """Create (and optionally activate) a paper deployment.

        Idempotent: if a deployment for the same strategy/symbol/timeframe
        already exists, returns the existing deployment.
        """
        try:
            config = PaperDeploymentConfig(
                execution_mode="paper",
                initial_cash=self._initial_cash,
                slippage_bps=5.0,
                fee_bps=0.0,
                max_allocation_pct=0.25,
                max_position_size=1000.0,
                allow_short=False,
                stop_loss_pct=spec.risk.stop_loss_pct or 0.03,
                take_profit_pct=spec.risk.take_profit_pct or 0.06,
                max_loss_per_trade_pct=0.02,
                warmup_bars=50,
                strategy_parameters=spec.model_dump(mode="json"),
            )
        except Exception as exc:
            return DeploymentResult(
                strategy_id=strategy_id,
                candidate_id=candidate_id,
                deployment=None,
                success=False,
                error=f"config error: {exc}",
            )

        spec_hash = strategy_identity(spec) if isinstance(spec, StrategySpec) else spec.model_dump_json()
        dep_id = deployment_identity(
            strategy_id=strategy_id,
            strategy_spec_hash=spec_hash,
            symbol=symbol,
            timeframe=timeframe,
            dataset_id=self._dataset_id,
            config=config,
        )

        # Idempotency check
        if dep_id in self._created:
            return DeploymentResult(
                strategy_id=strategy_id,
                candidate_id=candidate_id,
                deployment=self._created[dep_id],
                success=True,
                is_duplicate=True,
            )

        # Check existing via control center
        existing = self._cc.get_deployment(dep_id)
        if existing is not None:
            self._created[dep_id] = existing
            return DeploymentResult(
                strategy_id=strategy_id,
                candidate_id=candidate_id,
                deployment=existing,
                success=True,
                is_duplicate=True,
            )

        try:
            deployment, _, gate_decision = self._cc.create_deployment(
                spec=spec,
                dataset_id=self._dataset_id,
                config=config,
            )
        except Exception as exc:
            logger.warning("deployment creation failed for %s: %s", strategy_id, exc)
            return DeploymentResult(
                strategy_id=strategy_id,
                candidate_id=candidate_id,
                deployment=None,
                success=False,
                error=str(exc),
            )

        if deployment is None:
            reason = gate_decision.rejection_reason if gate_decision else "unknown"
            return DeploymentResult(
                strategy_id=strategy_id,
                candidate_id=candidate_id,
                deployment=None,
                success=False,
                error=f"gate rejected: {reason}",
            )

        self._created[dep_id] = deployment

        if activate:
            try:
                deployment = self._cc.activate_deployment(dep_id)
            except Exception as exc:
                logger.warning("deployment activation failed for %s: %s", strategy_id, exc)

        return DeploymentResult(
            strategy_id=strategy_id,
            candidate_id=candidate_id,
            deployment=deployment,
            success=True,
        )
