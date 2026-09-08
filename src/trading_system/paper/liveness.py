"""Phase 23 — Scheduler Liveness Computation.

This module provides the authoritative liveness check for autonomous paper
trading deployments. It uses the persistent scheduler heartbeat fields
written by the Autonomous Scheduler worker, NOT the passive
PaperStrategyRunner state.

This is a separate module to avoid circular imports between control.py
and dashboard.py.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from .deployment import PaperDeployment, PaperDeploymentStatus


class SchedulerLiveness(str, Enum):
    """Explicit liveness states for autonomous deployments.

    These are derived from the persistent scheduler heartbeat, NOT from the
    passive PaperStrategyRunner. This allows the dashboard to distinguish
    an actually running worker from a stale deployment record.

    States:
      WORKER_ALIVE      - Scheduler heartbeat is recent; deployment is expected to run.
      WORKER_STALE      - Deployment is ACTIVE and market/session requires the worker,
                          but heartbeat is older than threshold.
      MARKET_CLOSED     - Market is legitimately closed; no worker execution expected.
      DATA_STALE        - Worker is alive (recent heartbeat) but market data is stale.
      WORKER_ERROR      - Worker is alive but has entered an error state.
      DISABLED          - Deployment is not supposed to execute (not ACTIVE, or scheduler disabled).
    """
    WORKER_ALIVE = "worker_alive"
    WORKER_STALE = "worker_stale"
    MARKET_CLOSED = "market_closed"
    DATA_STALE = "data_stale"
    WORKER_ERROR = "worker_error"
    DISABLED = "disabled"


# Default heartbeat threshold: consider worker stale after this many seconds.
# Configurable via env or deployment config in future.
DEFAULT_HEARTBEAT_THRESHOLD_SECONDS = 120  # 2 minutes


def compute_scheduler_liveness(
    deployment: PaperDeployment,
    *,
    now: Optional[datetime] = None,
    heartbeat_threshold_seconds: int = DEFAULT_HEARTBEAT_THRESHOLD_SECONDS,
    market_data_freshness_seconds: int = 300,  # 5 minutes
) -> SchedulerLiveness:
    """Compute the scheduler liveness state for a deployment.

    This is the authoritative liveness check for autonomous paper trading.
    It uses the persistent heartbeat fields written by the Autonomous Scheduler
    worker, not the passive PaperStrategyRunner state.

    Args:
        deployment: The paper deployment to check.
        now: Current time (UTC). Defaults to datetime.now(timezone.utc).
        heartbeat_threshold_seconds: Seconds after which a heartbeat is considered stale.
        market_data_freshness_seconds: Seconds after which market data is considered stale.

    Returns:
        SchedulerLiveness enum value.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # 1. Deployment must be ACTIVE for autonomous execution.
    if deployment.status != PaperDeploymentStatus.ACTIVE:
        return SchedulerLiveness.DISABLED

    # 2. Check market session.
    from trading_system.india.market_calendar import TradingCalendar
    calendar = TradingCalendar()
    if not calendar.is_regular_session(now):
        return SchedulerLiveness.MARKET_CLOSED

    # 3. Check scheduler heartbeat.
    last_tick = deployment.last_tick_at
    if last_tick is None:
        # Never ticked = worker never ran or hasn't ticked yet.
        # For autonomous scheduler, no heartbeat = not running.
        return SchedulerLiveness.WORKER_STALE

    try:
        last_tick_dt = datetime.fromisoformat(last_tick.replace("Z", "+00:00"))
        if last_tick_dt.tzinfo is None:
            last_tick_dt = last_tick_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return SchedulerLiveness.WORKER_STALE

    seconds_since_tick = (now - last_tick_dt).total_seconds()

    if seconds_since_tick > heartbeat_threshold_seconds:
        return SchedulerLiveness.WORKER_STALE

    # 4. Worker is alive (heartbeat recent). Check market data freshness.
    last_market_data = deployment.last_market_data_at
    if last_market_data is not None:
        try:
            md_dt = datetime.fromisoformat(last_market_data.replace("Z", "+00:00"))
            if md_dt.tzinfo is None:
                md_dt = md_dt.replace(tzinfo=timezone.utc)
            seconds_since_market_data = (now - md_dt).total_seconds()
            if seconds_since_market_data > market_data_freshness_seconds:
                return SchedulerLiveness.DATA_STALE
        except Exception:
            pass

    # 5. Check for worker error state.
    if deployment.notes and "worker_error" in deployment.notes:
        return SchedulerLiveness.WORKER_ERROR

    # 6. All checks passed: worker is alive and healthy.
    return SchedulerLiveness.WORKER_ALIVE