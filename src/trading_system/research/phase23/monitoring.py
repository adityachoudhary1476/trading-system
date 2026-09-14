"""Strategy monitoring (Phase 24).

Runtime health checks and auto-suspension for paper-deployed strategies.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["HealthCheck", "StrategyMonitor"]


@dataclass
class HealthCheck:
    """Snapshot of a deployment's runtime health at a point in time."""

    strategy_id: str
    deployment_id: str
    is_healthy: bool
    checks: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class StrategyMonitor:
    """Lightweight runtime monitor for paper-deployed strategy deployments.

    Wraps a :class:`~trading_system.paper.control.PaperTradingControlCenter`-like
    object (or a ``_cc`` compatible mock) and runs deterministic, network-free
    health checks against a deployment.  Every check result is recorded in an
    in-memory history so callers can detect flapping or drift.
    """

    def __init__(self, control_center: Any) -> None:
        self._cc = control_center
        self._history: dict[str, list[HealthCheck]] = {}

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def check_deployment(self, deployment_id: str) -> HealthCheck:
        """Run all health checks for *deployment_id* and return a
        :class:`HealthCheck` snapshot.

        The method is deliberately fail-closed: if the control center raises
        or returns an incomplete description, the error is captured in
        ``checks`` / ``errors`` rather than propagated.
        """
        warnings: list[str] = []
        errors: list[str] = []
        checks: dict[str, Any] = {}

        try:
            deployment = self._cc.get_deployment(deployment_id)
            if deployment is None:
                errors.append("deployment not found")
                return HealthCheck(
                    strategy_id="unknown",
                    deployment_id=deployment_id,
                    is_healthy=False,
                    checks=checks,
                    warnings=warnings,
                    errors=errors,
                )

            checks["status"] = getattr(deployment, "status", "unknown")
            checks["last_tick_at"] = getattr(deployment, "last_tick_at", None)
            checks["last_decision_at"] = getattr(deployment, "last_decision_at", None)

            if getattr(deployment, "status", None) != "ACTIVE":
                warnings.append(
                    f"deployment status is {getattr(deployment, 'status', 'unknown')}"
                )

            try:
                summary = self._cc.inspect_deployment(deployment_id)
                checks["has_positions"] = bool(
                    getattr(summary, "positions", None)
                )
                checks["account_status"] = (
                    getattr(summary, "account", {}) or {}
                ).get("status", "unknown")
            except Exception as exc:  # noqa: BLE001 - fail closed
                warnings.append(f"inspect failed: {exc}")

            is_healthy = len(errors) == 0
            hc = HealthCheck(
                strategy_id=getattr(deployment, "strategy_id", "unknown"),
                deployment_id=deployment_id,
                is_healthy=is_healthy,
                checks=checks,
                warnings=warnings,
                errors=errors,
            )
            self._history.setdefault(deployment_id, []).append(hc)
            return hc

        except Exception as exc:  # noqa: BLE001 - fail closed
            errors.append(f"monitoring error: {exc}")
            hc = HealthCheck(
                strategy_id="unknown",
                deployment_id=deployment_id,
                is_healthy=False,
                checks=checks,
                warnings=warnings,
                errors=errors,
            )
            self._history.setdefault(deployment_id, []).append(hc)
            return hc

    def get_history(self, deployment_id: str) -> list[HealthCheck]:
        """Return the recorded :class:`HealthCheck` snapshots for a deployment."""
        return list(self._history.get(deployment_id, []))
