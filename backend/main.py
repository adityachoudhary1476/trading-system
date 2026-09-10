"""Main FastAPI application for the trading system backend."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware

from config import get_settings
from routes.analysis import router as analysis_router
from routes.signals import router as signals_router
from routes.pipeline import router as pipeline_router
from routes.live import router as live_router
from routes.paper_api import router as paper_api_router

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler.

    Manages the lifecycle of the trading runtime:
    - Startup: Initialize the live pipeline if configured
    - Shutdown: Stop the live pipeline and clean up resources
    """
    settings = get_settings()
    logger.info("Starting trading system backend in %s mode", settings.environment)

    # Initialize trading runtime if LIVE_PIPELINE_ENABLED is set
    # The runtime is started on-demand when the first analysis request comes in
    # or when explicitly configured to start at startup
    if settings.live_pipeline_enabled:
        try:
            from runtime import get_trading_runtime

            runtime = get_trading_runtime()
            symbols = [s.strip() for s in settings.signal_universe.split(",") if s.strip()]
            runtime.start(
                access_token=settings.upstox_service_account_token,
                symbols=symbols,
            )
            logger.info("Live pipeline started for %d configured symbols", len(symbols))

        except Exception as e:
            logger.error("Failed to initialize trading runtime: %s", str(e))
            # Don't fail startup, just log the error

    yield

    # Shutdown: stop the trading runtime
    logger.info("Shutting down trading system backend")
    try:
        from runtime import get_trading_runtime
        runtime = get_trading_runtime()
        if runtime.state.state.value not in ("stopped", "disabled"):
            runtime.stop()
            logger.info("Trading runtime stopped")
    except Exception as e:
        logger.error("Error stopping trading runtime: %s", str(e))


app = FastAPI(
    title="Trading System Backend",
    description="AI-powered market analysis backend",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware
# Production must set CORS_ORIGINS to a comma-separated list of trusted origins.
# An empty value disables cross-origin access entirely.
settings = get_settings()
cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=bool(cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(analysis_router)
app.include_router(signals_router)
app.include_router(pipeline_router)
app.include_router(live_router)
app.include_router(paper_api_router)


@app.get("/health", tags=["health"])
async def health_check():
    """
    Health check endpoint.

    Returns service status and live pipeline state.
    """
    try:
        from runtime import get_trading_runtime
        runtime = get_trading_runtime()
        pipeline_status = {
            "status": runtime.state.state.value,
            "connected": runtime.state.connected,
        }
    except Exception:
        pipeline_status = {"status": "unknown", "connected": False}

    return {
        "status": "ok",
        "service": "trading-system-backend",
        "environment": get_settings().environment,
        "pipeline": pipeline_status,
    }


@app.get("/health/detailed", tags=["health"])
async def detailed_health_check():
    """
    Detailed health check including dependency status.

    Only reports status of dependencies that can be actually checked.
    """
    health = {
        "status": "ok",
        "service": "trading-system-backend",
        "dependencies": {},
    }

    # Check Supabase connectivity
    try:
        from supabase import create_client
        settings = get_settings()
        if settings.supabase_url and settings.supabase_service_role_key:
            sb = create_client(settings.supabase_url, settings.supabase_service_role_key)
            # Simple query to verify connectivity
            sb.table("broker_connections").select("id").limit(1).execute()
            health["dependencies"]["supabase"] = "connected"
        else:
            health["dependencies"]["supabase"] = "not_configured"
    except Exception as e:
        health["dependencies"]["supabase"] = f"error: {str(e)}"
        health["status"] = "degraded"

    # Check trading runtime status
    try:
        from runtime import get_trading_runtime
        runtime = get_trading_runtime()
        health["dependencies"]["trading_runtime"] = {
            "status": runtime.state.state.value,
            "connected": runtime.state.connected,
            "events_received": runtime.state.events_received,
            "candles_generated": runtime.state.candles_generated,
            "started_at": runtime.state.started_at,
            "last_event_time": runtime.state.last_event_time,
        }
    except Exception as e:
        health["dependencies"]["trading_runtime"] = f"error: {str(e)}"

    return health
