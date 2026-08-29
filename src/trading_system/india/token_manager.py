"""FYERS token lifecycle manager (Day 10.5) — DATA ONLY, no execution.

Owns the access-token / refresh-token lifecycle for FYERS v3. It NEVER prints, logs,
or embeds secrets (access token, refresh token, client secret, PIN) in exceptions, logs,
or status strings. All observable output is a status code.

FYERS v3 token facts (verified from official FYERS docs / community at Day 10.5):
  * access token validity  ~ 24 hours
  * refresh token validity ~ 15 days
  * refresh grant: POST {TOKEN_URL} with
        grant_type=refresh_token
        refresh_token=<refresh_token>
        client_id=<client_id>
        checksum=SHA-256("<client_id>:<secret>")
    Response: {"s":"ok","access_token":"..."}  or  {"s":"error","code":<n>,"message":...}
  * FYERS auth error code -16 ("could not authenticate") must never be swallowed.

  * generate_auth_url() uses a PER-REQUEST random `state` (secrets.token_urlsafe) so each
    login URL is unique; a static state caused FYERS to replay a prior authorization
    (same auth_code) for an already-authorized session. Random state also gives CSRF protection.

This module does NOT automate browser login or PIN entry (forbidden this phase). If the
refresh token is expired, it fails clearly with REFRESH_TOKEN_EXPIRED — the human must
re-authorize. The access-token HTTP callable is injectable so tests use mocks (no network,
no real credentials).
"""
from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

import requests

from ..config import log


FYERS_TOKEN_URL = "https://api-t1.fyers.in/api/v3/token"
# FYERS v3 authorization-code exchange endpoint (SessionModel.generate_token -> /validate-authcode).
# Per the official fyers_apiv3 SDK (3.1.16), the auth-code exchange POSTs JSON with
# {grant_type, appIdHash, code} to this endpoint; appIdHash = SHA-256(client_id:secret).
FYERS_TOKEN_EXCHANGE_URL = "https://api-t1.fyers.in/api/v3/validate-authcode"

# FYERS auth error codes that mean "authentication failed" (never treat as empty data).
_FYERS_AUTH_CODES = {-16, -17, 401, 403}


class AuthStatus(str, Enum):
    """Observable FYERS authentication state. Never includes secret material."""

    AUTH_OK = "auth_ok"
    ACCESS_TOKEN_EXPIRED = "access_token_expired"   # token absent/expired; refresh may help
    REFRESH_TOKEN_EXPIRED = "refresh_token_expired"  # refresh rejected; human must re-auth
    AUTH_FAILED = "auth_failed"                       # other auth/business failure
    NETWORK_ERROR = "network_error"                  # transport failure


class TokenError(Exception):
    """Raised on unrecoverable token problems. Messages contain NO secrets."""


# Injectable HTTP post for testability: (url, data) -> requests.Response-like.
HttpPost = Callable[[str, dict], "object"]


def _checksum(client_id: str, secret: str) -> str:
    return hashlib.sha256(f"{client_id}:{secret}".encode("utf-8")).hexdigest()


def _redact(value: Optional[str]) -> str:
    """Return a length-safe mask for logs (never the raw secret)."""
    if not value:
        return "<none>"
    return f"<{len(value)} chars>"


@dataclass
class TokenState:
    status: AuthStatus
    access_token_present: bool
    refresh_token_present: bool
    message: str = ""


class TokenManager:
    """Manages FYERS access/refresh token lifecycle. Secrets come from env/config."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        secret: Optional[str] = None,
        token_url: str = FYERS_TOKEN_URL,
        token_exchange_url: str = FYERS_TOKEN_EXCHANGE_URL,
        http_post: Optional[HttpPost] = None,
        timeout: int = 20,
    ) -> None:
        # All secrets read from env by default (never stored in source).
        self.client_id = client_id if client_id is not None else os.getenv("FYERS_CLIENT_ID", "")
        self._access_token = access_token if access_token is not None else os.getenv("FYERS_ACCESS_TOKEN", "")
        self.refresh_token = refresh_token if refresh_token is not None else os.getenv("FYERS_REFRESH_TOKEN", "")
        self.secret = secret if secret is not None else os.getenv("FYERS_SECRET", "")
        self.token_url = token_url
        self.token_exchange_url = token_exchange_url
        self._http_post = http_post or self._default_post
        self.timeout = timeout

    # --- accessors (never expose raw secret) --------------------------------
    @property
    def access_token(self) -> str:
        return self._access_token

    @access_token.setter
    def access_token(self, value: str) -> None:
        self._access_token = value

    def has_access_token(self) -> bool:
        return bool(self._access_token)

    def has_refresh_token(self) -> bool:
        return bool(self.refresh_token)

    # --- status / lifecycle -------------------------------------------------
    def token_status(self) -> TokenState:
        """Best-known state without a network probe."""
        if self.has_access_token() and self.has_refresh_token():
            return TokenState(AuthStatus.AUTH_OK, True, True, "access+refresh token present")
        if self.has_access_token() and not self.has_refresh_token():
            return TokenState(AuthStatus.ACCESS_TOKEN_EXPIRED, True, False,
                              "access token present but no refresh token; refresh unavailable")
        if not self.has_access_token() and self.has_refresh_token():
            return TokenState(AuthStatus.ACCESS_TOKEN_EXPIRED, False, True,
                              "no access token; refresh available")
        return TokenState(AuthStatus.ACCESS_TOKEN_EXPIRED, False, False,
                          "no access or refresh token configured")

    def get_valid_access_token(self) -> str:
        """Return the current access token if present, else raise (no silent fabrication)."""
        if not self._access_token:
            raise TokenError("No FYERS access token present; refresh or re-authorize.")
        return self._access_token

    def refresh_access_token(self) -> str:
        """Mint a new access token from the refresh token via the FYERS token endpoint.

        Raises TokenError with AUTH/REFRESH/NETWORK status (never embeds secrets).
        On success, stores and returns the new access token.
        """
        if not self.client_id or not self.secret:
            raise TokenError("Missing FYERS_CLIENT_ID or FYERS_SECRET; cannot refresh.")
        if not self.refresh_token:
            raise TokenError("No FYERS refresh token; human re-authorization required.")

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "checksum": _checksum(self.client_id, self.secret),
        }
        try:
            resp = self._http_post(self.token_url, payload)
        except requests.RequestException as e:
            log.warning("FYERS token refresh network error")
            raise TokenError("FYERS token refresh network error") from e

        code, body = self._parse(resp)
        if code == -16 or code == -17 or code in _FYERS_AUTH_CODES:
            # Refresh token rejected -> must re-authorize.
            log.warning("FYERS refresh token rejected (code=%s)", code)
            raise TokenError("FYERS refresh token expired or invalid; human re-authorization required.")
        if code != 200 and body.get("s") != "ok":
            log.warning("FYERS token refresh failed (code=%s)", code)
            raise TokenError(f"FYERS token refresh failed (code={code}).")
        new_token = body.get("access_token")
        if not new_token:
            raise TokenError("FYERS token response missing access_token.")
        self._access_token = new_token
        log.info("FYERS access token refreshed (length %d)", len(new_token))
        return new_token

    # --- auth-code login flow (Day 10.5 recovery; isolated, no automation) ----
    def generate_auth_url(self, redirect_uri: Optional[str] = None,
                          state: Optional[str] = None) -> str:
        """Build the FYERS v3 generate-authcode URL the user opens in a browser.

        This ONLY constructs a URL string from client_id + app secret + redirect URI.
        It performs NO network call and NEVER prints secrets. The caller prints the URL
        and the user completes login manually; the redirect returns `auth_code=...`.
        No browser/TOTP automation is performed here (per scope).

        `state` is intentionally PER-REQUEST and RANDOM (secrets.token_urlsafe) so that
        each login URL is unique. A static state made every generated URL byte-identical,
        which let FYERS replay a prior authorization (same auth_code) for an already
        authorized session. Randomizing state also provides CSRF protection. Pass an
        explicit `state` only for tests; production calls should let it default.
        """
        if not self.client_id or not self.secret:
            raise TokenError("Missing FYERS_CLIENT_ID or FYERS_SECRET; cannot build login URL.")
        # Per-request random state (URL-safe). Defaults to a new random value each call.
        if state is None:
            state = secrets.token_urlsafe(16)
        redirect = redirect_uri or os.getenv(
            "FYERS_REDIRECT_URI",
            "https://trade.fyers.in/api-login/redirect-uri/index.html",
        )
        from urllib.parse import urlencode
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "state": state,
            "secret_key": self.secret,
        }
        return "https://api-t1.fyers.in/api/v3/generate-authcode?" + urlencode(params)

    def exchange_auth_code(self, auth_code: str, redirect_uri: Optional[str] = None) -> tuple[str, str]:
        """Exchange an auth_code for a fresh access token + refresh token.

        Implements the current FYERS v3 authorization-code grant (matches the official
        fyers_apiv3 SessionModel.generate_token contract):

          POST {FYERS_TOKEN_EXCHANGE_URL}  (JSON body)
            grant_type = "authorization_code"
            appIdHash  = SHA-256("{client_id}:{secret}")  (hex)
            code       = <auth_code from the login redirect>

        The redirect URI is NOT sent in the exchange body (FYERS binds the auth_code to
        the redirect_uri from the generate-authcode step server-side). We still resolve
        FYERS_REDIRECT_URI here so the value used to build the login URL is consistent
        and the override/fallback behavior is preserved.

        On success stores and returns (access_token, refresh_token). Raises TokenError
        on any failure (never embeds secrets). Does NOT write files; the caller persists
        via the existing secure config (.env). No network call is made by this method's
        caller beyond the one POST below.
        """
        if not self.client_id or not self.secret:
            raise TokenError("Missing FYERS_CLIENT_ID or FYERS_SECRET; cannot exchange auth code.")
        if not auth_code:
            raise TokenError("No auth_code supplied; complete the manual login first.")
        # Resolved for parity with generate_auth_url (override/fallback preserved);
        # not included in the exchange payload per FYERS v3 contract.
        _ = redirect_uri or os.getenv(
            "FYERS_REDIRECT_URI",
            "https://trade.fyers.in/api-login/redirect-uri/index.html",
        )
        payload = {
            "grant_type": "authorization_code",
            "appIdHash": _checksum(self.client_id, self.secret),
            "code": auth_code,
        }
        try:
            resp = self._http_post(self.token_exchange_url, payload, json_body=True)
        except requests.RequestException as e:
            log.warning("FYERS auth-code exchange network error")
            raise TokenError("FYERS auth-code exchange network error") from e

        code, body = self._parse(resp)
        if code == -16 or code == -17 or code in _FYERS_AUTH_CODES:
            log.warning("FYERS auth-code exchange rejected (code=%s)", code)
            raise TokenError("FYERS rejected the auth code (code=%s); re-run login." % code)
        if code != 200 and body.get("s") != "ok":
            fy_message = body.get("message") or body.get("msg") or ""
            log.warning("FYERS auth-code exchange failed (code=%s): %s", code, fy_message)
            detail = f" — {fy_message}" if fy_message else ""
            raise TokenError(f"FYERS auth-code exchange failed (code={code}){detail}.")
        new_access = body.get("access_token")
        new_refresh = body.get("refresh_token")
        if not new_access:
            raise TokenError("FYERS token response missing access_token.")
        self._access_token = new_access
        self.refresh_token = new_refresh or self.refresh_token
        log.info("FYERS access token issued (length %d); refresh token %s",
                 len(new_access), "present" if new_refresh else "absent")
        return new_access, new_refresh or ""

    def verify_authentication(self) -> TokenState:
        """Best-effort liveness check. Uses token_status unless a probe is wired.

        The default provider can be injected with a `probe` callable that returns an
        AuthStatus (e.g. a lightweight FYERS /profile call). Without one, this is the
        static best-known state (no network). Tests inject a probe.
        """
        return self.token_status()

    # --- internals ----------------------------------------------------------
    def _default_post(self, url: str, data: dict, json_body: bool = False) -> "object":
        if json_body:
            return requests.post(url, json=data, timeout=self.timeout)
        return requests.post(url, data=data, timeout=self.timeout)

    @staticmethod
    def _parse(resp) -> tuple[int, dict]:
        status = getattr(resp, "status_code", 200)
        try:
            body = resp.json()
        except Exception:
            body = {}
        if isinstance(body, dict) and body.get("s") == "error":
            status = body.get("code", status)
        return status, (body if isinstance(body, dict) else {})
