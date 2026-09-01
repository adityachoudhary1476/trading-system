"""Upstox OAuth 2.0 authorization-code token lifecycle manager.

Handles the Upstox authorization-code grant:

  1. ``build_authorization_url()``  — user opens in browser; Upstox redirects
     with an authorization ``code``.
  2. ``exchange_auth_code()``      — form-encoded POST to the token endpoint,
     receives an access token.
  3. ``verify_authentication()``   — probe ``/v2/user/profile`` to confirm the
     token is actually accepted by Upstox (distinct from mere presence).
  4. Token validity tracking       — Upstox access tokens expire at **3:30 AM
     IST the following day**; the manager computes this boundary and reports
     expiry without guessing.

Design constraints
  * All credentials are read from environment variables (never hardcoded).
  * The client secret is **never** sent in the authorization URL (only at the
    token-exchange POST, server-to-server).
  * Access tokens, auth codes, and secrets are never printed or logged.
  * No refresh-token flow is invented — Upstox's current token model does not
    expose one for this application.  When a token is expired/invalid the
    manager requires re-authorization rather than pretending the token is valid.
  * The HTTP transport (``http_post`` / ``http_get``) is injectable so tests
    exercise the full parsing logic without network calls.
  * This module is **DATA-ONLY** — no order/execution code.
"""
from __future__ import annotations

import datetime as dt
import os
import secrets
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode

import requests

from ..config import log


# --- Upstox OAuth 2.0 endpoints (official) ------------------------------------
# Authorization:  user opens in browser → Upstox redirects with ``code=``
UPSTOX_AUTH_URL = "https://api.upstox.com/v2/login/authorization/dialog"
# Token exchange:  POST form-encoded → { "access_token": "..." }
UPSTOX_TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
# Connectivity probe:  GET with Bearer token → 200 if the token is live.
UPSTOX_PROFILE_URL = "https://api.upstox.com/v2/user/profile"

# Upstox access tokens expire at 3:30 AM IST the following day.
_EXPIRY_HOUR = 3
_EXPIRY_MINUTE = 30

_IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
_UTC = dt.timezone.utc


def _next_expiry(obtained_at: dt.datetime) -> dt.datetime:
    """Return the next 3:30 AM IST boundary at/after *obtained_at* (UTC datetime).

    Examples (IST):
      obtained 2026-09-01 02:00  → expires 2026-09-01 03:30 IST
      obtained 2026-09-01 10:00  → expires 2026-09-02 03:30 IST
    """
    obtained_ist = obtained_at.astimezone(_IST)
    today_330 = obtained_ist.replace(
        hour=_EXPIRY_HOUR, minute=_EXPIRY_MINUTE, second=0, microsecond=0
    )
    if obtained_ist < today_330:
        return today_330.astimezone(_UTC)
    return (today_330 + dt.timedelta(days=1)).astimezone(_UTC)


def _redact(value: Optional[str]) -> str:
    """Length-safe mask for any log message (never the raw secret)."""
    if not value:
        return "<none>"
    return f"<{len(value)} chars>"


class AuthStatus(str, Enum):
    """Observable Upstox authentication state. Never includes secret material."""
    AUTH_OK = "auth_ok"
    ACCESS_TOKEN_EXPIRED = "access_token_expired"
    AUTH_FAILED = "auth_failed"
    NETWORK_ERROR = "network_error"


class TokenError(Exception):
    """Raised on unrecoverable token problems. Messages contain NO secrets."""


@dataclass
class TokenState:
    """Snapshot of authentication state (safe to log/print)."""
    status: AuthStatus
    access_token_present: bool
    refresh_token_present: bool  # Always False for Upstox (no refresh flow).
    message: str = ""


# Injectable HTTP callables for testability (no network in unit tests).
HttpPost = Callable[[str, dict], object]
HttpGet = Callable[[str, dict], object]


class UpstoxTokenManager:
    """Upstox OAuth 2.0 access-token lifecycle.

    Credentials (read from env via ``settings`` / ``load_dotenv``):
      ``UPSTOX_CLIENT_ID``     — API key
      ``UPSTOX_CLIENT_SECRET``  — API secret (server-to-server exchange only)
      ``UPSTOX_REDIRECT_URI``   — exact URI registered in the Upstox console
      ``UPSTOX_ACCESS_TOKEN``   — access token (pre-obtained, if any)

    Does **not** automate browser login or TOTP. The user opens the
    authorization URL, logs in manually, and supplies the resulting
    ``code`` to ``exchange_auth_code()``.
    """

    def __init__(
        self,
        client_id: Optional[str] = None,
        access_token: Optional[str] = None,
        secret: Optional[str] = None,
        client_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
        token_url: str = UPSTOX_TOKEN_URL,
        profile_url: str = UPSTOX_PROFILE_URL,
        auth_url: str = UPSTOX_AUTH_URL,
        http_post: Optional[HttpPost] = None,
        http_get: Optional[HttpGet] = None,
        auth_status_callback: Optional[Callable[[str], None]] = None,
        timeout: int = 20,
    ) -> None:
        # Accept both ``secret`` and ``client_secret`` for backward compat.
        resolved_secret = client_secret if client_secret is not None else secret
        self.client_id: str = (
            client_id if client_id is not None else os.getenv("UPSTOX_CLIENT_ID", "")
        )
        self.secret: str = (
            resolved_secret if resolved_secret is not None
            else os.getenv("UPSTOX_CLIENT_SECRET", "")
        )
        self._access_token: str = (
            access_token if access_token is not None
            else os.getenv("UPSTOX_ACCESS_TOKEN", "")
        )
        self.redirect_uri: str = (
            redirect_uri if redirect_uri is not None
            else os.getenv("UPSTOX_REDIRECT_URI", "")
        )
        self.token_url = token_url
        self.profile_url = profile_url
        self.auth_url = auth_url
        self._http_post = http_post or self._default_post
        self._http_get = http_get or self._default_get
        self._auth_status_callback = auth_status_callback
        self._timeout = timeout
        # When the token was obtained (UTC).  None until exchange or explicit load.
        self._obtained_at: Optional[dt.datetime] = None

    # ------------------------------------------------------------------ tokens
    @property
    def access_token(self) -> str:
        return self._access_token

    @access_token.setter
    def access_token(self, value: str) -> None:
        self._access_token = value

    @property
    def refresh_token(self) -> str:
        """Upstox has no refresh token in this architecture."""
        return ""

    @refresh_token.setter
    def refresh_token(self, value: str) -> None:
        # Accepted for backward-compat with existing constructor calls; silently
        # ignored — Upstox does not provide a refresh-token flow here.
        pass

    def has_access_token(self) -> bool:
        return bool(self._access_token)

    def has_refresh_token(self) -> bool:
        return False

    def get_access_token(self) -> Optional[str]:
        """Return the access token if present and within its validity window."""
        if not self._access_token:
            return None
        if self._is_expired():
            return None
        return self._access_token

    def get_valid_access_token(self) -> str:
        """Return the access token if usable, else raise (no silent fabrication)."""
        token = self.get_access_token()
        if not token:
            raise TokenError(
                "No valid Upstox access token (absent or expired); "
                "re-authorize via `auth-login` then `auth-exchange`."
            )
        return token

    def is_authenticated(self) -> bool:
        return bool(self.get_access_token())

    # ------------------------------------------------------- authorization url
    def build_authorization_url(
        self,
        redirect_uri: Optional[str] = None,
        state: Optional[str] = None,
    ) -> str:
        """Construct the Upstox authorize URL the user opens in a browser.

        The URL carries ONLY: ``response_type``, ``client_id``,
        ``redirect_uri``, and ``state``.  The client secret is never placed
        in the authorization URL (OAuth2 spec; Upstox rejects it).

        ``state`` defaults to a fresh ``secrets.token_urlsafe(16)`` so every
        URL is unique (CSRF protection + prevents replay).
        """
        if not self.client_id:
            raise TokenError(
                "Missing UPSTOX_CLIENT_ID; cannot build authorization URL."
            )
        redirect = redirect_uri or self.redirect_uri
        if not redirect:
            raise TokenError(
                "Missing UPSTOX_REDIRECT_URI; register one in the Upstox console "
                "and configure it via env or parameter."
            )
        if state is None:
            state = secrets.token_urlsafe(16)
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect,
            "state": state,
        }
        return self.auth_url + "?" + urlencode(params)

    # Backward-compatible alias (kept so existing CLI/tests compile).
    def generate_auth_url(
        self,
        redirect_uri: Optional[str] = None,
        state: Optional[str] = None,
    ) -> str:
        return self.build_authorization_url(redirect_uri=redirect_uri, state=state)

    # ----------------------------------------------------- auth-code exchange
    def exchange_auth_code(
        self,
        auth_code: str,
        redirect_uri: Optional[str] = None,
    ) -> str:
        """Exchange an authorization code for an access token via Upstox OAuth2.

        Sends a form-encoded POST to ``UPSTOX_TOKEN_URL`` with:
          ``code``, ``client_id``, ``client_secret``, ``redirect_uri``,
          ``grant_type=authorization_code``

        The ``redirect_uri`` must exactly match the one registered with Upstox
        and used in ``build_authorization_url``.

        Returns the access token.  Stores it internally with an
        ``obtained_at`` timestamp.  Does NOT write to disk — call
        ``save_access_token()`` to persist.

        Raises :class:`TokenError` on any failure (message never contains the
        secret, token, or auth code).
        """
        if not self.client_id or not self.secret:
            raise TokenError(
                "Missing UPSTOX_CLIENT_ID or UPSTOX_CLIENT_SECRET; "
                "cannot exchange authorization code."
            )
        if not auth_code:
            raise TokenError(
                "No authorization code supplied; complete the login first."
            )
        redirect = redirect_uri or self.redirect_uri

        form = {
            "code": auth_code,
            "client_id": self.client_id,
            "client_secret": self.secret,
            "redirect_uri": redirect,
            "grant_type": "authorization_code",
        }
        try:
            resp = self._http_post(self.token_url, form)
        except requests.RequestException:
            log.warning("Upstox token exchange network error")
            raise TokenError("Upstox token exchange network error.")

        access = self._parse_token_response(resp)
        self._access_token = access
        self._obtained_at = dt.datetime.now(_UTC)
        log.info("Upstox access token received (length %d)", len(access))
        return access

    # -------------------------------------------------- persistence / storage
    def save_access_token(self, access_token: str) -> Path:
        """Persist ``access_token`` to the project ``.env`` file.

        Updates (or appends) the ``UPSTOX_ACCESS_TOKEN`` line.  Does not touch
        any other key.  Returns the path written.
        """
        env_path = _find_env_file()
        token_line = f"UPSTOX_ACCESS_TOKEN={access_token}"
        lines: list[str] = (
            env_path.read_text(encoding="utf-8").splitlines()
            if env_path.exists()
            else []
        )
        seen = False
        for i, line in enumerate(lines):
            if line.strip().startswith("UPSTOX_ACCESS_TOKEN="):
                lines[i] = token_line
                seen = True
                break
        if not seen:
            lines.append(token_line)
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return env_path

    def load_access_token(self) -> Optional[str]:
        """Read ``UPSTOX_ACCESS_TOKEN`` from the environment at call time."""
        return os.getenv("UPSTOX_ACCESS_TOKEN", "")

    # ----------------------------------------------------- status / lifecycle
    def token_status(self) -> TokenState:
        """Best-known state from presence + computed expiry (no network)."""
        if not self._access_token:
            return TokenState(
                AuthStatus.ACCESS_TOKEN_EXPIRED, False, False,
                "no Upstox access token configured",
            )
        if self._is_expired():
            return TokenState(
                AuthStatus.ACCESS_TOKEN_EXPIRED, True, False,
                "Upstox access token expired (past 3:30 AM IST boundary); "
                "re-authorize",
            )
        return TokenState(
            AuthStatus.AUTH_OK, True, False,
            "Upstox access token present and within validity window",
        )

    def verify_authentication(self) -> TokenState:
        """Prove the stored token is actually accepted by Upstox.

        Issues a GET to ``UPSTOX_PROFILE_URL`` with
        ``Authorization: Bearer <token>``.

        Three distinct outcomes:
          1. token absent → presence-only state, **no** network probe
          2. token expired (3:30 AM boundary) → ACCESS_TOKEN_EXPIRED, no probe
          3. token present + not expired → live probe:
             200 → AUTH_OK,  401/403 → ACCESS_TOKEN_EXPIRED,  else → AUTH_FAILED
        """
        # --- no token or locally-expired → skip the network call
        if not self._access_token:
            state = TokenState(
                AuthStatus.ACCESS_TOKEN_EXPIRED, False, False,
                "no Upstox access token configured; re-authorize via auth-login",
            )
            return state
        if self._is_expired():
            state = TokenState(
                AuthStatus.ACCESS_TOKEN_EXPIRED, True, False,
                "Upstox access token expired (past 3:30 AM IST boundary); "
                "re-authorize",
            )
            self._notify(state.status)
            return state

        # --- live probe
        try:
            resp = self._http_get(
                self.profile_url,
                {"Authorization": f"Bearer {self._access_token}"},
            )
        except requests.RequestException:
            log.warning("Upstox connectivity probe network error")
            result = TokenState(
                AuthStatus.NETWORK_ERROR, True, False,
                "network error during Upstox connectivity probe",
            )
            self._notify(result.status)
            return result

        status, body = self._parse(resp)
        if status == 200:
            result = TokenState(
                AuthStatus.AUTH_OK, True, False,
                "access token accepted by Upstox (profile call succeeded)",
            )
            self._notify(result.status)
            return result
        if status in (401, 403):
            result = TokenState(
                AuthStatus.ACCESS_TOKEN_EXPIRED, True, False,
                "access token rejected by Upstox; re-authorize required",
            )
            self._notify(result.status)
            return result
        # Non-auth error (rate-limit, server error, etc.).
        msg = _safe_err(body)
        log.warning("Upstox connectivity probe error (status=%s): %s", status, msg)
        result = TokenState(
            AuthStatus.AUTH_FAILED, True, False,
            f"Upstox error during connectivity probe (status={status})",
        )
        self._notify(result.status)
        return result

    # ---------------------------------------------------------------- internals
    def _default_post(self, url: str, data: dict) -> object:
        return requests.post(url, data=data, timeout=self._timeout)

    def _default_get(self, url: str, headers: dict) -> object:
        return requests.get(url, headers=headers, timeout=self._timeout)

    def _is_expired(self) -> bool:
        """True if obtained_at is known and past the 3:30 AM IST expiry."""
        if self._obtained_at is None:
            return False
        return dt.datetime.now(_UTC) >= _next_expiry(self._obtained_at)

    def _notify(self, status: AuthStatus) -> None:
        if self._auth_status_callback is not None:
            try:
                self._auth_status_callback(status.value)
            except Exception:  # pragma: no cover - callback must never break caller
                log.debug("auth_status_callback raised; ignored")

    # -- response parsing ------------------------------------------------------
    @staticmethod
    def _parse(resp) -> tuple[int, dict]:
        """Return (http_status_code, body_dict).  The HTTP status is the source of truth;
        Upstox error bodies are parsed only for messages, not for status overrides."""
        status = getattr(resp, "status_code", 200)
        try:
            body = resp.json()
        except Exception:
            body = {}
        return status, (body if isinstance(body, dict) else {})

    def _parse_token_response(self, resp) -> str:
        """Parse the Upstox token-exchange HTTP response.

        Raises :class:`TokenError` (without leaking the secret/auth-code) on
        any problem: HTTP error, malformed JSON, missing ``access_token``,
        invalid/expired code, redirect-URI mismatch, or invalid credentials.
        """
        status = getattr(resp, "status_code", 200)
        body = _safe_parse_json(resp)

        if status != 200:
            msg = _safe_err(body) if body else "unknown error"
            raise TokenError(
                f"Upstox authorization code rejected (HTTP {status}): {msg}"
            )

        if not isinstance(body, dict):
            raise TokenError("Upstox token response was not a JSON object.")

        token = body.get("access_token")
        if not token:
            msg = _safe_err(body)
            if msg:
                raise TokenError(
                    f"Upstox token exchange failed: {msg}"
                )
            raise TokenError(
                "Upstox token exchange returned no access_token."
            )
        return token


# Backward-compatible alias for code/tests that import ``TokenManager``.
TokenManager = UpstoxTokenManager


# --------------------------------------------------------------------- helpers
def _find_env_file() -> Path:
    from ..config import settings
    return settings.project_root / ".env"


def _safe_parse_json(resp) -> dict:
    try:
        body = resp.json()
    except (ValueError, AttributeError):
        return {}
    return body if isinstance(body, dict) else {}


def _safe_err(body: dict) -> str:
    """Extract a human-readable (non-secret) error message from an Upstox response."""
    if not isinstance(body, dict):
        return "unknown error"
    errs = body.get("errors")
    if isinstance(errs, list) and errs:
        first = errs[0]
        if isinstance(first, dict):
            return first.get("message", "unknown error")
    return body.get("message", "unknown error")
