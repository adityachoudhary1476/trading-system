"""Supabase JWT authentication for the Python backend."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import get_settings

logger = logging.getLogger(__name__)
security = HTTPBearer(auto_error=False)


@dataclass
class AuthenticatedUser:
    """Represents an authenticated user."""

    user_id: str
    email: Optional[str] = None


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> AuthenticatedUser:
    """
    Validate Supabase JWT and return authenticated user.

    Expected header: Authorization: Bearer <Supabase JWT>
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Validate JWT using Supabase
    user = await _validate_supabase_jwt(token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


async def _validate_supabase_jwt(token: str) -> Optional[AuthenticatedUser]:
    """
    Validate a Supabase JWT token and extract user information.

    Uses the Supabase Python client to validate the JWT.
    """
    try:
        from supabase import create_client

        settings = get_settings()
        if not settings.supabase_url or not settings.supabase_service_role_key:
            logger.error("Supabase configuration is missing")
            return None

        sb = create_client(
            settings.supabase_url,
            settings.supabase_service_role_key,
        )
        response = sb.auth.get_user(token)
        if response and response.user:
            return AuthenticatedUser(
                user_id=response.user.id,
                email=response.user.email,
            )
        return None
    except Exception as e:
        logger.warning("JWT validation failed: %s", str(e))
        return None
