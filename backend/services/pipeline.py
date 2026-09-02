"""Pipeline service for data health monitoring.

This service reports the actual state of the live trading runtime.
It uses the shared DataHealthMonitor instance from the runtime manager
instead of creating a new monitor per request.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from schemas.market import PipelineStageDTO

logger = logging.getLogger(__name__)


async def get_pipeline_status(
    user_id: str,
    access_token: Optional[str] = None,
) -> list[PipelineStageDTO]:
    """
    Get the current pipeline status from the shared runtime.

    This endpoint reports the actual state of the live trading pipeline.
    If the pipeline is not running, it returns an honest degraded state.

    Args:
        user_id: The authenticated user's ID (unused but kept for API compatibility)
        access_token: Optional Upstox access token (unused, runtime uses its own)

    Returns:
        List of PipelineStageDTO objects representing actual pipeline state
    """
    try:
        from runtime import get_trading_runtime, RuntimeStateEnum

        runtime = get_trading_runtime()
        runtime_state = runtime.state
        monitor = runtime.health_monitor

        # Map runtime state to pipeline status
        state = runtime_state.state

        # If runtime is not running, return honest state
        if state == RuntimeStateEnum.DISABLED:
            return [
                PipelineStageDTO(
                    id="service",
                    label="Backend Service",
                    status="ready",
                    last_activity=int(time.time() * 1000),
                    metric="Running",
                ),
                PipelineStageDTO(
                    id="live-pipeline",
                    label="Live Pipeline",
                    status="disconnected",
                    last_activity=None,
                    metric="Disabled",
                ),
            ]

        if state in (RuntimeStateEnum.STOPPED, RuntimeStateEnum.STOPPING):
            return [
                PipelineStageDTO(
                    id="service",
                    label="Backend Service",
                    status="ready",
                    last_activity=int(time.time() * 1000),
                    metric="Running",
                ),
                PipelineStageDTO(
                    id="live-pipeline",
                    label="Live Pipeline",
                    status="disconnected",
                    last_activity=None,
                    metric=state.value,
                ),
            ]

        if state == RuntimeStateEnum.AUTH_ERROR:
            return [
                PipelineStageDTO(
                    id="service",
                    label="Backend Service",
                    status="ready",
                    last_activity=int(time.time() * 1000),
                    metric="Running",
                ),
                PipelineStageDTO(
                    id="upstox-connection",
                    label="Upstox Connection",
                    status="auth_error",
                    last_activity=None,
                    metric="Authentication failed",
                ),
            ]

        if state == RuntimeStateEnum.ERROR:
            return [
                PipelineStageDTO(
                    id="service",
                    label="Backend Service",
                    status="ready",
                    last_activity=int(time.time() * 1000),
                    metric="Running",
                ),
                PipelineStageDTO(
                    id="live-pipeline",
                    label="Live Pipeline",
                    status="disconnected",
                    last_activity=None,
                    metric=f"Error: {runtime_state.last_error or 'Unknown error'}",
                ),
            ]

        if state == RuntimeStateEnum.CONNECTING:
            return [
                PipelineStageDTO(
                    id="service",
                    label="Backend Service",
                    status="ready",
                    last_activity=int(time.time() * 1000),
                    metric="Running",
                ),
                PipelineStageDTO(
                    id="upstox-connection",
                    label="Upstox Connection",
                    status="disconnected",
                    last_activity=None,
                    metric="Connecting...",
                ),
            ]

        # Runtime is connected - get actual monitor state
        if not monitor:
            return [
                PipelineStageDTO(
                    id="service",
                    label="Backend Service",
                    status="ready",
                    last_activity=int(time.time() * 1000),
                    metric="Running",
                ),
                PipelineStageDTO(
                    id="live-pipeline",
                    label="Live Pipeline",
                    status="disconnected",
                    last_activity=None,
                    metric="No health monitor",
                ),
            ]

        snapshot = monitor.snapshot()
        current_time = int(time.time() * 1000)

        # Build pipeline stages from actual runtime state
        stages = [
            PipelineStageDTO(
                id="upstox-connection",
                label="Upstox Connection",
                status=_map_feed_status(snapshot.get("status", "disconnected")),
                last_activity=(
                    int(snapshot["latest_event_ts"] * 1000)
                    if snapshot.get("latest_event_ts")
                    else None
                ),
                metric="Connected" if snapshot.get("connected") else "Disconnected",
            ),
            PipelineStageDTO(
                id="data-feed",
                label="Data Feed",
                status=_map_feed_status(snapshot.get("status", "disconnected")),
                last_activity=(
                    int(snapshot["latest_event_ts"] * 1000)
                    if snapshot.get("events_received", 0) > 0
                    else None
                ),
                metric=f"Events: {snapshot.get('events_received', 0)}",
            ),
            PipelineStageDTO(
                id="candle-aggregation",
                label="Candle Aggregation",
                status="ready" if snapshot.get("connected") else "disconnected",
                last_activity=(
                    int(snapshot["latest_closed_candle"] * 1000)
                    if snapshot.get("candles_generated", 0) > 0
                    else None
                ),
                metric=f"Candles: {snapshot.get('candles_generated', 0)}",
            ),
            PipelineStageDTO(
                id="analysis-engine",
                label="Analysis Engine",
                status="ready",
                last_activity=current_time,
                metric="Available",
            ),
        ]

        return stages

    except Exception as e:
        logger.error("Failed to get pipeline status: %s", str(e))
        # Return minimal status indicating the service is running
        return [
            PipelineStageDTO(
                id="service",
                label="Backend Service",
                status="ready",
                last_activity=int(time.time() * 1000),
                metric="Running",
            ),
        ]


def _map_feed_status(status: str) -> str:
    """Map FeedStatus to frontend status format."""
    mapping = {
        "healthy": "healthy",
        "stale": "stale",
        "disconnected": "disconnected",
        "auth_error": "auth_error",
        "invalid_data": "invalid_data",
    }
    return mapping.get(status, "disconnected")
