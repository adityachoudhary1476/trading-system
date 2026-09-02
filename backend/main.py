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
            from services.broker import get_upstox_access_token

            runtime = get_trading_runtime()

            # Note: Starting the live pipeline requires an access token.
            # In a multi-user system, this would need to be handled differently.
            # For now, we log that the pipeline should be started with user credentials.
            logger.info(
                "Live pipeline is enabled but requires user credentials. "
                "Pipeline will be started on first authenticated request."
            )

        except Exception as e:
            logger.error("Failed to initialize trading runtime: %s", str(e))
            # Don't fail startup, just log the error

    yield

    # Shutdown: stop the trading runtime
    logger.info("Shutting down trading system backend")
    try:
        from runtime import get_trading_runtime
        runtime = get_trading_runtime()
        if runtime.state.running:
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
# Note: In production, this should be configured with specific origins
# The current permissive setting is for development only
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(analysis_router)
app.include_router(signals_router)
app.include_router(pipeline_router)


@app.get("/health", tags=["health"])
async def health_check():
    """
    Health check endpoint.

    Returns service status. Does NOT fabricate dependency health.
    """
    return {
        "status": "ok",
        "service": "trading-system-backend",
        "environment": get_settings().environment,
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
        from runtime import get_trading_runtime, RuntimeStateEnum
        runtime = get_trading_runtime()
        if runtime.state.state == RuntimeStateEnum.CONNECTED:
            health["dependencies"]["trading_runtime"] = {
                "status": "connected",
                "connected": True,
                "events_received": runtime.state.events_received,
                "candles_generated": runtime.state.candles_generated,
            }
        elif runtime.state.state in (RuntimeStateEnum.DISABLED, RuntimeStateEnum.STOPPED):
            health["dependencies"]["trading_runtime"] = runtime.state.state.value
        else:
            health["dependencies"]["trading_runtime"] = {
                "status": runtime.state.state.value,
                "connected": False,
                "last_error": runtime.state.last_error,
            }
    except Exception as e:
        health["dependencies"]["trading_runtime"] = f"error: {str(e)}"

    return health
