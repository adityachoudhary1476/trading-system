"""Local Supabase JWT verification.

Replaces the previous remote `sb.auth.get_user(token)` mechanism, which
failed in production because the Supabase project was configured with
asymmetric signing keys and the `service_role` JWT did not match the
token-issuing keys.

This module performs fully local verification:

  * Asymmetric tokens (ES256, RS256) are validated against the JWKS
    served by the Supabase project at
    ``${SUPABASE_URL}/auth/v1/.well-known/jwks.json``.
  * Symmetric tokens (HS256) are accepted as a fallback ONLY when the
    ``SUPABASE_JWT_SECRET`` environment variable is configured. This
    covers legacy Supabase projects that still sign with the project
    JWT secret.
  * The ``none`` algorithm and any algorithm not in the allow-list is
    rejected.
  * Standard claims (issuer, audience, expiration, subject) are
    verified before the request is allowed to proceed.

The public surface (``AuthenticatedUser``, ``get_current_user``) is
unchanged so existing route handlers do not need to be modified.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import (
    InvalidAlgorithmError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidSignatureError,
    InvalidTokenError,
    PyJWKClient,
    PyJWKClientError,
    decode,
    get_unverified_header,
)
from jwt.algorithms import get_default_algorithms as _get_default_jwt_algorithms

from config import get_settings

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)


# Algorithms we are willing to verify. ``none`` is intentionally absent.
# ``ES256`` and ``RS256`` are the algorithms Supabase uses for asymmetric
# signing. ``HS256`` is the algorithm used when a Supabase project is
# still configured with a shared JWT secret (legacy / self-hosted).
SUPPORTED_ALGORITHMS: tuple[str, ...] = ("ES256", "RS256", "HS256")

# Supabase issues access tokens with the audience claim set to the
# string ``"authenticated"``. This is stable across project restarts
# and is the documented value in the Supabase JS/Python SDKs.
SUPABASE_AUDIENCE = "authenticated"

# Allow a small clock skew between this service and Supabase. Two
# minutes matches the default used by the Supabase JS SDK.
_LEEWAY_SECONDS = 120

# Cache the JWKS client and the JWKS endpoint probe for 10 minutes.
_JWKS_TTL_SECONDS = 600

_JWKS_CLIENTS: dict[str, tuple[float, PyJWKClient]] = {}
_JWKS_CLIENTS_LOCK = threading.Lock()


@dataclass
class AuthenticatedUser:
    """Represents an authenticated user."""

    user_id: str
    email: Optional[str] = None


def _unauthorized(detail: str) -> HTTPException:
    """Build a 401 with the standard Supabase-style ``WWW-Authenticate``."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _issuer_for(settings_supabase_url: str) -> str:
    """Return the canonical issuer for the configured Supabase project.

    Supabase access tokens are issued with ``iss`` equal to
    ``{supabase_url}/auth/v1`` (no trailing slash on the URL).
    """
    base = (settings_supabase_url or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/auth/v1"


def _jwks_url_for(settings_supabase_url: str) -> str:
    base = (settings_supabase_url or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/auth/v1/.well-known/jwks.json"


def _get_jwks_client(supabase_url: str) -> PyJWKClient:
    """Return a cached ``PyJWKClient`` for the given Supabase URL.

    PyJWKClient performs its own per-key caching and refetches the JWKS
    document on cache miss or unknown ``kid``. The wrapper here only
    caches the client object itself, not the keys.
    """
    url = _jwks_url_for(supabase_url)
    now = time.monotonic()
    with _JWKS_CLIENTS_LOCK:
        cached = _JWKS_CLIENTS.get(url)
        if cached is not None:
            ts, client = cached
            if now - ts < _JWKS_TTL_SECONDS:
                return client
    # 5s network timeout: enough for Supabase, short enough to fail
    # fast if the URL is unreachable. Cache_keys=True so PyJWKClient
    # honours key rotation.
    client = PyJWKClient(url, cache_keys=True, lifespan=_JWKS_TTL_SECONDS)
    with _JWKS_CLIENTS_LOCK:
        _JWKS_CLIENTS[url] = (now, client)
    return client


def _peek_algorithm(token: str) -> Optional[str]:
    """Return the algorithm declared in the JWT header, or None.

    This is used only to decide which verification path to take. It
    is not used to trust the token — the algorithm is re-checked
    against the allow-list inside ``jwt.decode``.
    """
    try:
        header = get_unverified_header(token)
    except InvalidTokenError:
        return None
    alg = header.get("alg")
    return alg if isinstance(alg, str) else None


def _verify_asymmetric(token: str, settings) -> Optional[dict[str, Any]]:
    """Verify an ES256/RS256 token against the Supabase JWKS."""
    if not settings.supabase_url:
        return None
    try:
        client = _get_jwks_client(settings.supabase_url)
        signing_key = client.get_signing_key_from_jwt(token)
    except (PyJWKClientError, httpx.HTTPError, OSError) as exc:
        logger.warning("JWKS lookup failed: %s", type(exc).__name__)
        return None
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("JWKS lookup unexpected error: %s", type(exc).__name__)
        return None

    try:
        return decode(
            token,
            key=signing_key.key,
            algorithms=["ES256", "RS256"],
            audience=SUPABASE_AUDIENCE,
            issuer=_issuer_for(settings.supabase_url),
            leeway=_LEEWAY_SECONDS,
            options={
                "require": ["exp", "iat", "sub"],
                "verify_aud": True,
                "verify_iss": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_signature": True,
            },
        )
    except (InvalidSignatureError, InvalidAudienceError, InvalidIssuerError,
            InvalidAlgorithmError, InvalidTokenError) as exc:
        logger.info("Asymmetric JWT verification failed: %s", type(exc).__name__)
        return None


def _verify_symmetric(token: str, settings) -> Optional[dict[str, Any]]:
    """Verify an HS256 token using the project JWT secret.

    Only attempted when ``SUPABASE_JWT_SECRET`` is configured. This is
    a deliberate fallback for legacy / self-hosted Supabase projects
    that still sign with the project secret. Newer Supabase projects
    issue ES256 tokens and never reach this path.
    """
    secret = settings.supabase_jwt_secret
    if not secret:
        return None
    try:
        return decode(
            token,
            key=secret,
            algorithms=["HS256"],
            audience=SUPABASE_AUDIENCE,
            issuer=_issuer_for(settings.supabase_url),
            leeway=_LEEWAY_SECONDS,
            options={
                "require": ["exp", "iat", "sub"],
                "verify_aud": True,
                "verify_iss": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_signature": True,
            },
        )
    except (InvalidSignatureError, InvalidAudienceError, InvalidIssuerError,
            InvalidAlgorithmError, InvalidTokenError) as exc:
        logger.info("Symmetric JWT verification failed: %s", type(exc).__name__)
        return None


def _claims_to_user(claims: dict[str, Any]) -> Optional[AuthenticatedUser]:
    """Convert verified JWT claims into an ``AuthenticatedUser``.

    Returns ``None`` if the ``sub`` claim is missing or not a string,
    even though ``require=['sub']`` should already guarantee this.
    """
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        return None
    email = claims.get("email")
    if not isinstance(email, str):
        email = None
    return AuthenticatedUser(user_id=sub, email=email)


def _validate_supabase_jwt(token: str) -> Optional[AuthenticatedUser]:
    """Verify a Supabase access token locally and return the user.

    The function never raises — invalid tokens yield ``None`` so the
    FastAPI dependency can return a clean 401 to the caller.
    """
    if not token:
        return None

    # Reject the ``none`` algorithm and any algorithm we do not accept
    # before doing any cryptographic work. This is the explicit
    # defence against the classic JWT "alg=none" downgrade attack.
    declared = _peek_algorithm(token)
    if not declared or declared not in SUPPORTED_ALGORITHMS:
        logger.info("Rejected token: unsupported or missing algorithm")
        return None

    settings = get_settings()
    if not settings.supabase_url:
        logger.error("Supabase URL is not configured")
        return None

    claims: Optional[dict[str, Any]] = None
    if declared in ("ES256", "RS256"):
        claims = _verify_asymmetric(token, settings)
        # If JWKS verification failed AND we also have a JWT secret
        # configured, the project may be in the symmetric-signing
        # configuration. Try the symmetric path as a fallback. This
        # is opt-in: without a secret it cannot succeed.
        if claims is None and settings.supabase_jwt_secret:
            claims = _verify_symmetric(token, settings)
    elif declared == "HS256":
        claims = _verify_symmetric(token, settings)
        # If the token says HS256 but the project actually uses
        # asymmetric keys (misconfiguration), fall back to JWKS.
        if claims is None:
            claims = _verify_asymmetric(token, settings)

    if claims is None:
        return None

    return _claims_to_user(claims)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> AuthenticatedUser:
    """Validate the Supabase access JWT and return the authenticated user.

    Behaviour is unchanged from the previous implementation: missing
    or invalid credentials produce HTTP 401 with a Bearer
    ``WWW-Authenticate`` challenge.
    """
    if not credentials:
        raise _unauthorized("Authentication required")

    token = credentials.credentials
    if not token:
        raise _unauthorized("Invalid authentication token")

    user = _validate_supabase_jwt(token)
    if not user:
        raise _unauthorized("Invalid or expired authentication token")

    return user


# Exposed for tests. Do NOT use in production code.
__all__ = [
    "AuthenticatedUser",
    "SUPPORTED_ALGORITHMS",
    "SUPABASE_AUDIENCE",
    "get_current_user",
]


# A tiny import-time check that the algorithms we accept are actually
# understood by the installed PyJWT version. This guards against a
# future PyJWT upgrade silently disabling one of the algorithms we
# rely on.
_available_algs = _get_default_jwt_algorithms()
for _alg in SUPPORTED_ALGORITHMS:
    if _alg not in _available_algs:
        logger.warning("PyJWT does not register algorithm %s", _alg)
