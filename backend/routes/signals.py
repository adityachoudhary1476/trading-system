"""Signals API routes."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from auth import AuthenticatedUser, get_current_user
from config import get_settings
from schemas.market import ErrorResponse, SignalDTO
from services import market_data, broker, signals

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/market", tags=["market"])


def _get_signal_universe() -> list[str]:
    """Get the configured signal universe.

    Returns:
        List of symbols from configuration
    """
    settings = get_settings()
    return [s.strip() for s in settings.signal_universe.split(",") if s.strip()]


@router.get(
    "/signals",
    response_model=list[SignalDTO],
    responses={
        401: {"model": ErrorResponse, "description": "Authentication required"},
        403: {"model": ErrorResponse, "description": "Upstox not connected"},
        503: {"model": ErrorResponse, "description": "Service unavailable"},
    },
)
async def get_signals(
    limit: int = Query(default=12, ge=1, le=50, description="Maximum number of signals to return"),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[SignalDTO]:
    """
    Get trading signals for the configured symbol universe.

    Requires authentication and an active Upstox connection.

    The limit parameter controls the maximum number of signals returned.
    Signals are generated for symbols in the configured universe, up to the limit.
    If fewer symbols have valid data, fewer signals are returned.
    """
    # Get user's Upstox access token
    access_token = await broker.get_upstox_access_token(user.user_id)
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Upstox connection not found. Please connect your broker account.",
        )

    # Get configured symbol universe
    universe = _get_signal_universe()
    symbols_to_analyze = universe[:limit]

    all_signals = []

    for symbol in symbols_to_analyze:
        try:
            df = await market_data.fetch_ohlcv(symbol, "1d", access_token, bars=60)
            if df is not None and len(df) > 0:
                result = await signals.generate_signals(symbol, "1d", df, access_token)
                all_signals.extend(result)
        except Exception as e:
            logger.warning("Failed to generate signal for %s: %s", symbol, str(e))
            continue

    return all_signals
