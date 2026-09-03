"""Live quote + market status routes.

The quote route is the polling source for the dashboard.  The status
route is the authoritative Indian market session state (replaces the
client-side guesswork that used to live in ``MarketStatusPanel``).
"""
from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from auth import AuthenticatedUser, get_current_user
from schemas.market import ErrorResponse, MarketStatusDTO, QuoteDTO, CandleReadModelDTO
from runtime import get_trading_runtime, RuntimeStateEnum
from services import broker, market_data
from src.trading_system.india.market_calendar import (
    DEFAULT_CALENDAR,
    SessionPhase,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/market", tags=["market"])


@router.get(
    "/candles",
    response_model=CandleReadModelDTO,
    response_model_by_alias=True,
    responses={
        401: {"model": ErrorResponse, "description": "Authentication required"},
        404: {"model": ErrorResponse, "description": "No authoritative candle state"},
        503: {"model": ErrorResponse, "description": "Live candle runtime unavailable"},
    },
)
async def get_authoritative_candles(
    symbol: str = Query(..., description='Trading symbol, e.g. "NSE:SBIN"'),
    timeframe: str = Query(default="1d"),
    limit: int = Query(default=160, ge=1, le=500),
    user: AuthenticatedUser = Depends(get_current_user),
) -> CandleReadModelDTO:
    """Return the shared runtime's closed and provisional candle state."""
    runtime = get_trading_runtime()
    if runtime.state.state not in (RuntimeStateEnum.CONNECTED, RuntimeStateEnum.CONNECTING):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Live candle runtime unavailable")
    model = runtime.candle_read_model
    canonical_timeframe = {"1D": "1d", "1W": "1w", "1M": "1M"}.get(timeframe, timeframe)
    result = model.read(symbol, canonical_timeframe, runtime.pipeline, limit) if model else None
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No authoritative candle state")
    return CandleReadModelDTO.model_validate(result)


def _session_phase_for(utc_now: datetime) -> SessionPhase:
    return DEFAULT_CALENDAR.phase(utc_now)


def _session_state_for(phase: SessionPhase) -> str:
    """Map internal SessionPhase to the wire ``sessionState`` enum."""
    if phase == SessionPhase.REGULAR:
        return "REGULAR"
    if phase == SessionPhase.PRE_MARKET:
        return "PRE_MARKET"
    if phase == SessionPhase.POST_MARKET:
        return "POST_MARKET"
    return "CLOSED"


@router.get(
    "/quote",
    response_model=QuoteDTO,
    response_model_by_alias=True,
    responses={
        200: {"description": "Live quote snapshot"},
        401: {"model": ErrorResponse, "description": "Authentication required"},
        403: {"model": ErrorResponse, "description": "Upstox not connected"},
        502: {"model": ErrorResponse, "description": "Upstox upstream error"},
        503: {"model": ErrorResponse, "description": "Upstox rate limit"},
    },
)
async def get_quote(
    symbol: str = Query(..., description='Trading symbol, e.g. "NSE:SBIN"'),
    user: AuthenticatedUser = Depends(get_current_user),
) -> QuoteDTO:
    """Return a single live quote snapshot for ``symbol``.

    Intended to be polled approximately once per second by the
    dashboard's centralized market-data store; each call hits Upstox
    once, never fabricates values, and surfaces a fresh ``timestamp``
    on every successful response.
    """
    access_token = await broker.get_upstox_access_token(user.user_id)
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Upstox connection not found. Please connect your broker account.",
        )

    try:
        quote = await market_data.fetch_quote(symbol, access_token)
    except market_data.UpstoxUnauthorizedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Upstox rejected the access token. Please reconnect your broker account.",
        ) from exc
    except market_data.UpstoxRateLimitedError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Upstox rate limit reached: {exc}",
        ) from exc
    except (
        market_data.UpstoxBadResponseError,
        market_data.UpstoxMalformedError,
        market_data.UpstoxNetworkError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Upstox quote unavailable for {symbol}: {exc}",
        ) from exc

    if quote is None:
        # Upstox succeeded but the quote body lacked a usable last_price
        # or the symbol key — treat as a 502 so the client distinguishes
        # "no data" from "auth failure".
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Upstox returned no usable quote for {symbol}",
        )

    now_utc = datetime.now(timezone.utc)
    phase = _session_phase_for(now_utc)
    session_state = _session_state_for(phase)
    fallback_ms = int(now_utc.timestamp() * 1000)

    return QuoteDTO.from_upstox_quote(
        symbol=symbol,
        quote=quote,
        session_state=session_state,
        fallback_timestamp_ms=fallback_ms,
    )


@router.get(
    "/status",
    response_model=MarketStatusDTO,
    response_model_by_alias=True,
    responses={
        200: {"description": "Authoritative market session status"},
    },
)
async def get_market_status() -> MarketStatusDTO:
    """Return the authoritative NSE equity session phase and boundaries.

    Uses :class:`TradingCalendar` to compute phase from the server
    clock.  ``serverTime`` is the canonical epoch-ms the frontend
    should use as the "now" reference for staleness calculations,
    eliminating the clock-skew between client and server.
    """
    now_utc = datetime.now(timezone.utc)
    phase = _session_phase_for(now_utc)
    open_dt, close_dt = DEFAULT_CALENDAR.session_boundaries(now_utc)

    # If we are *after* today's close, the boundaries above still
    # returned today's session — surface the *next* open in that case.
    if now_utc >= close_dt:
        next_open_dt, next_close_dt = DEFAULT_CALENDAR.session_boundaries(
            now_utc.fromtimestamp(now_utc.timestamp() + 24 * 3600, tz=timezone.utc)
        )
    else:
        next_open_dt, next_close_dt = open_dt, close_dt

    return MarketStatusDTO(
        market="NSE",
        phase=phase.value,
        serverTime=int(now_utc.timestamp() * 1000),
        nextOpen=int(next_open_dt.timestamp() * 1000),
        nextClose=int(next_close_dt.timestamp() * 1000),
    )
