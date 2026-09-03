"""Analysis API routes."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from auth import AuthenticatedUser, get_current_user
from schemas.market import AIAnalysisDTO, ErrorResponse
from services import market_data, broker, analysis

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/market", tags=["market"])


def _raise_market_data_http_error(exc: Exception, symbol: str) -> None:
    """Translate a :class:`UpstoxMarketDataError` into the right HTTP code.

    The AI analysis endpoint contract is preserved (401, 403, 404, 503)
    but we now distinguish a *legitimate* "no data" 404 (Upstox returned
    a success body with zero candles) from a *transient* 502 (Upstox
    returned garbage or rejected our request).  The frontend already
    understands 404 as "no analysis", and the new 502 carries enough
    context for the operator to diagnose.
    """
    from services.market_data import (
        UpstoxUnauthorizedError,
        UpstoxRateLimitedError,
        UpstoxBadResponseError,
        UpstoxMalformedError,
        UpstoxNetworkError,
    )

    if isinstance(exc, UpstoxUnauthorizedError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Upstox rejected the access token. Please reconnect your broker account.",
        )
    if isinstance(exc, UpstoxRateLimitedError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Upstox rate limit reached while fetching market data. Please retry shortly.",
        )
    if isinstance(exc, (UpstoxBadResponseError, UpstoxMalformedError, UpstoxNetworkError)):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Upstox market data unavailable for {symbol}: {exc}",
        )
    raise exc


@router.get(
    "/analysis",
    response_model=AIAnalysisDTO,
    responses={
        401: {"model": ErrorResponse, "description": "Authentication required"},
        403: {"model": ErrorResponse, "description": "Upstox not connected"},
        404: {"model": ErrorResponse, "description": "Symbol not found"},
        502: {"model": ErrorResponse, "description": "Upstox upstream error"},
        503: {"model": ErrorResponse, "description": "Service unavailable"},
    },
)
async def get_analysis(
    symbol: str = Query(..., description="Trading symbol (e.g., NSE:SBIN)"),
    timeframe: str = Query(default="1d", description="Timeframe (e.g., 1d, 1h)"),
    user: AuthenticatedUser = Depends(get_current_user),
) -> AIAnalysisDTO:
    """
    Get AI market analysis for a symbol.

    Requires authentication and an active Upstox connection.
    """
    access_token = await broker.get_upstox_access_token(user.user_id)
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Upstox connection not found. Please connect your broker account.",
        )

    try:
        df = await market_data.fetch_ohlcv(symbol, timeframe, access_token)
    except market_data.UpstoxMarketDataError as exc:
        _raise_market_data_http_error(exc, symbol)
    if df is None or len(df) == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No market data available for {symbol}",
        )

    result = await analysis.analyze_market(symbol, timeframe, df)
    return result
