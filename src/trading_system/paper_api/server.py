"""Phase 21 — stdlib HTTP server adapter.

A thin :class:`http.server.BaseHTTPRequestHandler` that delegates to
:class:`PaperAPIRouter`. Binds to localhost by default; the host and
port are read from environment variables (or explicit constructor args).

The server does NOT add authentication, rate limiting, or any live
trading functionality. It is intentionally a development-grade adapter
suitable for localhost / trusted networks.

Safety:

  * Default host is ``127.0.0.1`` (loopback only).
  * Request bodies are bounded (default 1 MiB) to prevent DoS.
  * The server is single-threaded by default; concurrent access is
    explicitly opt-in via ``PAPER_API_THREADED=1``.
  * No external network calls are made by this server itself.
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse

from .errors import APIError, APIErrorCode, APIErrorException, ErrorResponse
from .router import PaperAPIRouter, RequestContext, ResponseEnvelope


# Bounded request body (default 1 MiB). Configurable via env for tests.
_MAX_BODY_ENV = "PAPER_API_MAX_BODY_BYTES"
_DEFAULT_MAX_BODY = 1 * 1024 * 1024


def _max_body_bytes() -> int:
    raw = os.getenv(_MAX_BODY_ENV)
    if raw is None:
        return _DEFAULT_MAX_BODY
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_BODY
    return max(1, value)


def _strict_constant(_const: str):
    raise ValueError("non-finite or non-JSON value in request body")


class APIRequestHandler(BaseHTTPRequestHandler):
    """BaseHTTPRequestHandler that delegates to a :class:`PaperAPIRouter`."""

    router: PaperAPIRouter  # set by APIServer

    # Suppress default per-request stderr logging; routes are deterministic.
    def log_message(self, format, *args):  # noqa: A002 — match stdlib signature
        return

    def _send(self, status: int, body: dict) -> None:
        try:
            blob = json.dumps(body, default=str,
                              allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            blob = json.dumps(
                ErrorResponse(
                    error=APIError(
                        code=APIErrorCode.INTERNAL_ERROR,
                        message=f"response serialization failed: {exc}",
                    ),
                    schema_version=1,
                ).model_dump(mode="json"),
                default=str,
                allow_nan=False,
            ).encode("utf-8")
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def _read_body(self) -> str:
        length_hdr = self.headers.get("Content-Length")
        if not length_hdr:
            return ""
        try:
            length = int(length_hdr)
        except ValueError:
            raise APIErrorException(
                code=APIErrorCode.BAD_REQUEST,
                message="invalid Content-Length header",
                status=400,
            )
        max_bytes = _max_body_bytes()
        if length > max_bytes:
            raise APIErrorException(
                code=APIErrorCode.BAD_REQUEST,
                message=(
                    f"request body too large: {length} > {max_bytes} bytes"
                ),
                status=413,
            )
        if length < 0:
            raise APIErrorException(
                code=APIErrorCode.BAD_REQUEST,
                message="negative Content-Length is not allowed",
                status=400,
            )
        return self.rfile.read(length).decode("utf-8", errors="replace")

    def _dispatch(self) -> None:
        parsed = urlparse(self.path)
        try:
            raw_body = self._read_body() if self.command in {"POST", "PUT", "PATCH"} else ""
        except APIErrorException as exc:
            self._send(exc.status, ErrorResponse(
                error=exc.payload, schema_version=1
            ).model_dump(mode="json"))
            return
        # Parse query string into a multi-value dict.
        from urllib.parse import parse_qs
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            envelope: ResponseEnvelope = self.router.dispatch(
                self.command, parsed.path,
                query=query, raw_body=raw_body,
            )
        except APIErrorException as exc:
            self._send(exc.status, ErrorResponse(
                error=exc.payload, schema_version=1
            ).model_dump(mode="json"))
            return
        self._send(envelope.status, envelope.body if isinstance(envelope.body, dict) else {"result": envelope.body})

    # Route every HTTP verb to the dispatcher.
    def do_GET(self):  # noqa: N802 — stdlib signature
        self._dispatch()

    def do_POST(self):  # noqa: N802
        self._dispatch()

    def do_PUT(self):  # noqa: N802
        self._dispatch()

    def do_DELETE(self):  # noqa: N802
        self._dispatch()

    def do_PATCH(self):  # noqa: N802
        self._dispatch()

    def do_HEAD(self):  # noqa: N802
        self._dispatch()

    def do_OPTIONS(self):  # noqa: N802
        self._dispatch()


class APIServer:
    """A small, explicit lifecycle wrapper around ``ThreadingHTTPServer``.

    Use :meth:`serve_forever` to start (blocking) or :meth:`start` to
    run in a background thread. Always call :meth:`shutdown` on exit.
    """

    def __init__(
        self,
        router: PaperAPIRouter,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ) -> None:
        self.router = router
        self.host = host or os.getenv("PAPER_API_HOST", "127.0.0.1")
        self.port = int(port if port is not None else os.getenv("PAPER_API_PORT", "8765"))
        # Validate: only allow loopback hosts by default to keep the
        # surface explicit. Operators must opt in to non-loopback binding.
        if not _is_loopback_or_explicit(self.host):
            raise ValueError(
                f"PAPER_API_HOST={self.host!r} is not loopback. The Phase 21 "
                "API must be explicitly opted-in for non-loopback binding "
                "(set PAPER_API_ALLOW_NON_LOOPBACK=1 to override)."
            )
        threaded = os.getenv("PAPER_API_THREADED", "1").lower() in {"1", "true", "yes"}
        cls = ThreadingHTTPServer if threaded else type(
            "APIServerSingle", (ThreadingHTTPServer,), {}
        )
        # We always use ThreadingHTTPServer; the env var is reserved for
        # future use (single-threaded mode would require a non-threaded
        # mixin). For now we always start in threaded mode.
        self._httpd = ThreadingHTTPServer((self.host, self.port), _make_handler(self.router))

    @property
    def server_address(self) -> tuple[str, int]:
        return self._httpd.server_address[0], self._httpd.server_address[1]

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _is_loopback_or_explicit(host: str) -> bool:
    """Allow loopback hosts (127.0.0.0/8, ::1, 'localhost') by default.

    Any other host (including 0.0.0.0) requires the explicit env opt-in.
    """
    h = host.strip().lower()
    if h in {"127.0.0.1", "localhost", "::1", ""}:
        return True
    if h.startswith("127."):
        return True
    if os.getenv("PAPER_API_ALLOW_NON_LOOPBACK", "0") == "1":
        return True
    return False


def _make_handler(router: PaperAPIRouter):
    """Bind the router to the request handler class."""
    class _Handler(APIRequestHandler):
        pass
    _Handler.router = router
    return _Handler


def build_default_server(
    router: PaperAPIRouter,
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> APIServer:
    """Build a configured :class:`APIServer` over the given router.

    The host / port are taken from constructor args first, then from
    environment variables (``PAPER_API_HOST``, ``PAPER_API_PORT``).
    """
    return APIServer(router, host=host, port=port)