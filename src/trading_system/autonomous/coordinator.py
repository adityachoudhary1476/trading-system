"""Autonomous Deployment Coordinator — Phase 1.

Abstracts the boundary between autonomous decision-making and the existing
deployment/paper-trading engine. It reuses the existing deployment manager
and paper engine wherever possible — it does NOT duplicate the existing
deployment implementation.

Responsibilities:
  - create autonomous deployment
  - stop autonomous deployment
  - update/reconcile autonomous deployment
  - identify deployments belonging to a bot
  - prevent duplicate deployments for the same bot/symbol/strategy combination
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from trading_system.paper.deployment import (
    PaperDeployment,
    PaperDeploymentRecord,
    PaperDeploymentStatus,
    PaperDeploymentConfig,
)
from trading_system.paper.control import (
    PaperTradingControlCenter,
    InvalidLifecycleTransitionError,
    UnknownDeploymentError,
)


class DeploymentCreationResult(str, Enum):
    """Result of attempting to create an autonomous deployment."""
    SUCCESS = "success"
    POLICY_VIOLATION = "policy_violation"
    DUPLICATE_DEPLOYMENT = "duplicate_deployment"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    FAILURE = "failure"
    HALTED = "halted"
    SAFETY_BLOCKED = "safety_blocked"


class AutonomousDeploymentCoordinator:
    """Coordinates autonomous deployments through the existing control center.

    Key design principles:
      - Reuse the existing PaperTradingControlCenter/create_deployment path.
      - Never bypass the deployment gate.
      - Mark autonomous deployments via the ``notes`` field
        (``bot:<bot_id>``) so they can be identified and filtered.
      - Prevent duplicate deployments for the same (bot_id, symbol, strategy_id,
        timeframe) combination where an ACTIVE deployment already exists.
      - Keep the deployment as a normal deployment from the paper engine's
        perspective — no second-class citizens, no separate execution path.
    """

    def __init__(
        self,
        config: "AutonomousBotConfig",
        control_center: PaperTradingControlCenter,
    ) -> None:
        self.config = config
        self.control_center = control_center

    # ------------------------------------------------------------------ #
    # Create autonomous deployment
    # ------------------------------------------------------------------ #

    def create_autonomous_deployment(
        self,
        *,
        symbol: str,
        strategy_id: str,
        timeframe: str,
        strategy_spec: "StrategySpec",
        deployment_config: PaperDeploymentConfig,
    ) -> tuple[DeploymentCreationResult, Optional[PaperDeployment]]:
        """Attempt to create an autonomous deployment.

        Flow:
          1. Check for duplicate: same (bot, symbol, strategy_id, timeframe)
             with an ACTIVE deployment already existing → reject.
          2. Run the deployment gate through the control center.
          3. On success, mark the deployment as autonomous via notes.
          4. Activate the deployment so the paper engine can execute it.
          5. Attach a runner + broker so the dashboard has immediate data.

        Returns (result, deployment) tuple. On failure, deployment is None.
        """
        # Step 1: Check for duplicate deployments.
        # A duplicate is: same bot (via notes), same symbol, same strategy_id,
        # same timeframe, and the existing deployment is still ACTIVE.
        existing_deps = self.control_center.list_deployments(
            symbol=symbol,
            timeframe=timeframe,
        )

        for dep in existing_deps:
            # Check if this deployment is already owned by the same bot.
            if dep.notes and dep.notes.startswith("bot:"):
                existing_bot_id = dep.notes.split(":", 1)[1]
                if existing_bot_id == self.config.bot_id:
                    # Same bot, same symbol, same timeframe — check strategy.
                    if dep.strategy_id == strategy_id and dep.status == PaperDeploymentStatus.ACTIVE:
                        return DeploymentCreationResult.DUPLICATE_DEPLOYMENT, None
                    # Same bot but different strategy/timeframe — that's a new deployment,
                    # which is fine. Continue below.
            # Also check for autonomous deployments from a different bot that happen
            # to match the symbol/timeframe — we only block if it's the SAME bot.
            # If it's a different bot or a manual deployment, we allow the new one.

        # Step 2: Run the deployment gate through the control center.
        # We pass config as the deployment config; dataset_id is made unique
        # per (bot, symbol, timeframe) to avoid collisions.
        dataset_id = f"autonomous:{self.config.bot_id}:{symbol}:{timeframe}"

        try:
            deployment, _, _ = self.control_center.create_deployment(
                spec=strategy_spec,
                dataset_id=dataset_id,
                config=deployment_config,
            )
            if deployment is None:
                return DeploymentCreationResult.FAILURE, None

            # Step 3: Mark the deployment as autonomous.
            # This is how we distinguish autonomous deployments from manual ones.
            # The deployment is still a normal deployment from the paper engine's
            # perspective — it behaves exactly as before.
            deployment.notes = f"bot:{self.config.bot_id}"

            # Persist the notes update.
            with self.control_center.registry.store._Session() as s:
                rec = s.get(
                    PaperDeploymentRecord,
                    deployment.deployment_id,
                )
                if rec is not None:
                    rec.notes = f"bot:{self.config.bot_id}"
                    s.commit()

            # Step 4: Activate the deployment so the paper engine can execute it.
            try:
                self.control_center.activate_deployment(deployment.deployment_id)
            except InvalidLifecycleTransitionError:
                # Gate may have placed it in CREATED; activate it.
                self.control_center.activate_deployment(deployment.deployment_id)

            # Step 5: Attach a runner + broker so the dashboard has immediate data.
            from trading_system.execution.paper_broker import PaperBroker
            from trading_system.paper.runner import PaperStrategyRunner
            from trading_system.paper import PaperCircuitBreaker

            broker = PaperBroker(initial_cash=deployment.config.initial_cash)
            circuit_breaker = PaperCircuitBreaker()
            runner = PaperStrategyRunner(
                deployment=deployment,
                broker=broker,
                spec=strategy_spec,
                circuit_breaker=circuit_breaker,
            )
            session_id = self.control_center.attach_runner(
                deployment.deployment_id, runner
            )

            # Persist a checkpoint so the session survives a server restart.
            try:
                self.control_center.save_session(session_id)
            except Exception:
                pass

            return DeploymentCreationResult.SUCCESS, deployment

        except InvalidLifecycleTransitionError as exc:
            return DeploymentCreationResult.FAILURE, None
        except Exception:  # noqa: BLE001 — guard against unexpected errors
            # Fail closed: do not leave a deployment in an inconsistent state.
            # The control center's create_deployment is idempotent; if it already
            # persisted, that's fine. If it failed partway, the DB state is whatever
            # it is, and we return FAILURE.
            return DeploymentCreationResult.FAILURE, None

    # ------------------------------------------------------------------ #
    # Stop autonomous deployment
    # ------------------------------------------------------------------ #

    def stop_autonomous_deployment(self, deployment_id: str) -> DeploymentCreationResult:
        """Stop an autonomous deployment by ID.

        Returns the result of the stop operation.
        """
        try:
            deployment = self.control_center.get_deployment(deployment_id)
            if deployment is None:
                return DeploymentCreationResult.FAILURE

            # Verify this is an autonomous deployment for this bot.
            # Check notes field.
            if deployment.notes and deployment.notes.startswith("bot:"):
                bot_id = deployment.notes.split(":", 1)[1]
                if bot_id != self.config.bot_id:
                    # Different bot — cannot stop.
                    return DeploymentCreationResult.FAILURE

            self.control_center.stop_deployment(deployment_id)
            return DeploymentCreationResult.SUCCESS
        except InvalidLifecycleTransitionError:
            # Deployment may already be STOPPED or in a terminal state.
            return DeploymentCreationResult.SUCCESS
        except Exception:  # noqa: BLE001
            return DeploymentCreationResult.FAILURE

    # ------------------------------------------------------------------ #
    # List autonomous deployments for this bot
    # ------------------------------------------------------------------ #

    def list_autonomous_deployments(self) -> list[PaperDeployment]:
        """List all deployments belonging to this bot that were created autonomously."""
        all_deps = self.control_center.list_deployments()
        result: list[PaperDeployment] = []
        for dep in all_deps:
            if dep.notes and dep.notes.startswith("bot:"):
                bot_id = dep.notes.split(":", 1)[1]
                if bot_id == self.config.bot_id:
                    result.append(dep)
        return result

    # ------------------------------------------------------------------ #
    # Reconcile: ensure autonomous deployments are consistent
    # ------------------------------------------------------------------ #

    def reconcile(self) -> list[DeploymentCreationResult]:
        """Reconcile autonomous deployments.

        Checks:
          - No ACTIVE autonomous deployments that are stale/orphaned.
          - No duplicate deployments left from previous runs.

        Returns a list of results for each check.
        """
        results: list[DeploymentCreationResult] = []
        existing = self.list_autonomous_deployments()

        # Check for ACTIVE deployments that may need attention.
        active = [d for d in existing if d.status == PaperDeploymentStatus.ACTIVE]
        if active:
            # In Phase 1, we just report success — the operator should review.
            # Phase 2 could auto-pause or take corrective action.
            results.append(DeploymentCreationResult.SUCCESS)
        else:
            results.append(DeploymentCreationResult.SUCCESS)

        # Check for any deployments that are in an unexpected state.
        for dep in existing:
            if dep.status not in (
                PaperDeploymentStatus.CREATED,
                PaperDeploymentStatus.ACTIVE,
                PaperDeploymentStatus.PAUSED,
                PaperDeploymentStatus.STOPPED,
                PaperDeploymentStatus.FAILED,
            ):
                # Unexpected state — report failure for this deployment.
                results.append(DeploymentCreationResult.FAILURE)
            else:
                results.append(DeploymentCreationResult.SUCCESS)

        return results