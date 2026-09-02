"""Analysis API routes."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from auth import AuthenticatedUser, get_current_user
from schemas.market import AIAnalysisDTO, ErrorResponse
from services import market_data, broker, analysis

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/market", tags=["market"])


@router.get(
    "/analysis",
    response_model=AIAnalysisDTO,
    responses={
        401: {"model": ErrorResponse, "description": "Authentication required"},
        403: {"model": ErrorResponse, "description": "Upstox not connected"},
        404: {"model": ErrorResponse, "description": "Symbol not found"},
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
    # Get user's Upstox access token
    access_token = await broker.get_upstox_access_token(user.user_id)
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Upstox connection not found. Please connect your broker account.",
        )

    # Fetch live market data
    df = await market_data.fetch_ohlcv(symbol, timeframe, access_token)
    if df is None or len(df) == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No market data available for {symbol}",
        )

    # Run analysis
    result = await analysis.analyze_market(symbol, timeframe, df)
    return result
