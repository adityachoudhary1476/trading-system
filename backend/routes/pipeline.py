"""Pipeline API routes."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from auth import AuthenticatedUser, get_current_user
from schemas.market import ErrorResponse, PipelineStageDTO
from services import broker, pipeline

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/market", tags=["market"])


@router.get(
    "/pipeline",
    response_model=list[PipelineStageDTO],
    responses={
        401: {"model": ErrorResponse, "description": "Authentication required"},
        503: {"model": ErrorResponse, "description": "Service unavailable"},
    },
)
async def get_pipeline(
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[PipelineStageDTO]:
    """
    Get the current pipeline/data health status.

    Requires authentication.
    """
    # Get user's Upstox access token for live status check
    access_token = await broker.get_upstox_access_token(user.user_id)

    # Get pipeline status
    result = await pipeline.get_pipeline_status(user.user_id, access_token)
    return result
