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


def _http_status_for_upstox_error(
    exc: market_data.UpstoxMarketDataError,
    symbol: str,
) -> int:
    """Pick the right HTTP status for a signals-loop Upstox failure."""
    if isinstance(exc, market_data.UpstoxUnauthorizedError):
        return status.HTTP_403_FORBIDDEN
    if isinstance(exc, market_data.UpstoxRateLimitedError):
        return status.HTTP_503_SERVICE_UNAVAILABLE
    return status.HTTP_502_BAD_GATEWAY


@router.get(
    "/signals",
    response_model=list[SignalDTO],
    response_model_by_alias=True,
    responses={
        200: {"description": "Signals (possibly empty if no symbol has a usable signal)"},
        401: {"model": ErrorResponse, "description": "Authentication required"},
        403: {"model": ErrorResponse, "description": "Upstox not connected"},
        502: {"model": ErrorResponse, "description": "Upstox upstream error"},
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

    The limit parameter controls the maximum number of symbols analysed.
    The endpoint returns:

    * ``200`` with a (possibly empty) list when at least one symbol
      produced a signal or failed for a non-Upstox reason (insufficient
      data, etc.) so the frontend can render the dashboard.
    * ``502``/``403``/``503`` when *every* requested symbol failed at
      the Upstox boundary.  This distinguishes "the market data is
      genuinely unavailable" from "the configured universe produced no
      signals today".
    * ``403`` when the user has not connected Upstox.

    The endpoint never manufactures signals to avoid an empty list.
    """
    access_token = await broker.get_upstox_access_token(user.user_id)
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Upstox connection not found. Please connect your broker account.",
        )

    universe = _get_signal_universe()
    symbols_to_analyze = universe[:limit]

    if not symbols_to_analyze:
        return []

    all_signals: list[SignalDTO] = []
    upstox_failures: list[tuple[str, market_data.UpstoxMarketDataError]] = []

    for symbol in symbols_to_analyze:
        try:
            df = await market_data.fetch_ohlcv(symbol, "1d", access_token, bars=60)
        except market_data.UpstoxMarketDataError as exc:
            logger.warning(
                "Upstox market data failure for %s while collecting signals: %s",
                symbol, exc,
            )
            upstox_failures.append((symbol, exc))
            continue
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Unexpected error fetching OHLCV for %s: %s", symbol, exc)
            continue

        if df is None or len(df) == 0:
            continue

        try:
            result = await signals.generate_signals(symbol, "1d", df, access_token)
        except Exception as exc:
            logger.warning("Failed to generate signal for %s: %s", symbol, exc)
            continue
        all_signals.extend(result)

    if not all_signals and upstox_failures and len(upstox_failures) == len(symbols_to_analyze):
        first_symbol, first_exc = upstox_failures[0]
        http_status = _http_status_for_upstox_error(first_exc, first_symbol)
        raise HTTPException(
            status_code=http_status,
            detail=f"Upstox market data unavailable for {first_symbol}: {first_exc}",
        )

    return all_signals
