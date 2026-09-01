"""Phase 21 — Pure routing layer.

The :class:`PaperAPIRouter` is a transport-agnostic dispatcher. It maps
``(method, path, query, body)`` to ``(status, body_json)`` using a small
set of route handlers. It is fully decoupled from sockets so the API
contract can be unit-tested without binding to a port.

The router delegates every domain operation to the existing Phase 20
``PaperTradingControlCenter``. It does NOT implement order placement,
does NOT bypass the deployment gate, and does NOT instantiate brokers
of any kind.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict, ValidationError

from ..paper.control import PaperTradingControlCenter
from ..paper.dashboard import (
    DashboardEventSummary,
    build_deployment_summary,
)
from .errors import (
    APIErrorCode,
    APIErrorException,
    ErrorResponse,
    map_domain_exception,
)
from .models import (
    AccountResponse,
    CheckpointRequest,
    CircuitBreakerResponse,
    DeploymentListResponse,
    DeploymentResponse,
    EventsResponse,
    EvidenceResponse,
    HealthEndpointResponse,
    HealthResponse,
    LifecycleRequest,
    PerformanceResponse,
    PositionsResponse,
    RestoreRequest,
    RiskResponse,
    SessionResponse,
)


# Type alias for a route handler.
RouteHandler = Callable[["RequestContext"], "ResponseEnvelope"]


# --------------------------------------------------------------------------- #
# Request / response envelopes (router-internal)
# --------------------------------------------------------------------------- #
class RequestContext(BaseModel):
    """Normalized request envelope passed to every route handler."""

    model_config = ConfigDict(extra="forbid")

    method: str
    path: str
    params: dict[str, str] = {}
    query: dict[str, list[str]] = {}
    body: Optional[dict] = None
    raw_body: str = ""


class ResponseEnvelope(BaseModel):
    """Router-internal response envelope."""

    model_config = ConfigDict(extra="forbid")

    status: int
    body: Any = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_dump(model: BaseModel) -> dict:
    """JSON-safe pydantic dump with NaN/Infinity rejected."""
    try:
        blob = model.model_dump_json()
        return json.loads(blob, parse_constant=_strict_constant)
    except (ValueError, TypeError) as exc:
        raise APIErrorException(
            code=APIErrorCode.INTERNAL_ERROR,
            message=f"response serialization failed: {exc}",
            status=500,
        ) from exc


def _strict_constant(_const: str):
    """Reject Infinity / NaN / non-standard JSON tokens during parse."""
    raise ValueError("non-finite or non-JSON value in response")


def _single(query: dict[str, list[str]], key: str) -> Optional[str]:
    values = query.get(key)
    if not values:
        return None
    return values[0]


def _bounded_int(
    query: dict[str, list[str]], key: str, *, default: int, lo: int, hi: int
) -> int:
    raw = _single(query, key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise APIErrorException(
            code=APIErrorCode.BAD_REQUEST,
            message=f"query parameter {key!r} must be an integer",
            status=400,
        ) from exc
    if value < lo or value > hi:
        raise APIErrorException(
            code=APIErrorCode.BAD_REQUEST,
            message=f"query parameter {key!r} must be in [{lo}, {hi}]",
            status=400,
        )
    return value


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #
class PaperAPIRouter:
    """Pure routing layer over :class:`PaperTradingControlCenter`."""

    def __init__(self, center: PaperTradingControlCenter) -> None:
        self.center = center
        self._routes: list[tuple[re.Pattern, frozenset[str], RouteHandler]] = []
        self._register_routes()

    def _register_routes(self) -> None:
        self._add(r"^/health$", frozenset({"GET"}), self._route_health)
        self._add(r"^/deployments$", frozenset({"GET"}), self._route_list_deployments)
        self._add(r"^/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)$",
                  frozenset({"GET"}), self._route_get_deployment)
        self._add(r"^/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)/session$",
                  frozenset({"GET", "POST"}), self._route_session)
        self._add(r"^/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)/account$",
                  frozenset({"GET"}), self._route_account)
        self._add(r"^/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)/positions$",
                  frozenset({"GET"}), self._route_positions)
        self._add(r"^/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)/performance$",
                  frozenset({"GET"}), self._route_performance)
        self._add(r"^/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)/health$",
                  frozenset({"GET"}), self._route_health_block)
        self._add(r"^/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)/risk$",
                  frozenset({"GET"}), self._route_risk)
        self._add(r"^/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)/circuit-breaker$",
                  frozenset({"GET", "POST"}), self._route_circuit_breaker)
        self._add(r"^/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)/events$",
                  frozenset({"GET"}), self._route_events)
        self._add(r"^/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)/evidence$",
                  frozenset({"GET"}), self._route_evidence)
        self._add(r"^/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)/dashboard$",
                  frozenset({"GET"}), self._route_dashboard)
        self._add(r"^/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)/export$",
                  frozenset({"GET"}), self._route_export)
        self._add(r"^/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)/activate$",
                  frozenset({"POST"}), self._route_activate)
        self._add(r"^/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)/pause$",
                  frozenset({"POST"}), self._route_pause)
        self._add(r"^/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)/resume$",
                  frozenset({"POST"}), self._route_resume)
        self._add(r"^/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)/stop$",
                  frozenset({"POST"}), self._route_stop)
        self._add(r"^/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)/reset-circuit-breaker$",
                  frozenset({"POST"}), self._route_reset_circuit_breaker)
        self._add(r"^/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)/checkpoint$",
                  frozenset({"POST"}), self._route_checkpoint)
        self._add(r"^/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)/restore$",
                  frozenset({"POST"}), self._route_restore)

    def _add(self, pattern: str, methods: frozenset[str], handler: RouteHandler) -> None:
        self._routes.append((re.compile(pattern), methods, handler))

    def routes(self) -> list[tuple[str, frozenset[str]]]:
        """Return a serializable list of registered routes for documentation."""
        return [(r.pattern, methods) for r, methods, _ in self._routes]

    def dispatch(
        self,
        method: str,
        path: str,
        *,
        query: Optional[dict[str, list[str]]] = None,
        raw_body: str = "",
    ) -> ResponseEnvelope:
        """Dispatch a single request to the matching route handler.

        ``path`` may include a query string (e.g. ``"/deployments/x?limit=5"``);
        any inline query is merged with the explicit ``query`` argument.
        """
        if not isinstance(method, str) or not isinstance(path, str):
            return self._error_response(
                APIErrorException(
                    code=APIErrorCode.BAD_REQUEST,
                    message="method and path must be strings",
                    status=400,
                )
            )
        method = method.upper()
        # Split an inline query string off ``path`` if present.
        from urllib.parse import parse_qs as _parse_qs
        if "?" in path:
            raw_path, _, raw_inline = path.partition("?")
            inline = _parse_qs(raw_inline, keep_blank_values=True)
        else:
            raw_path = path
            inline = {}
        merged_query: dict[str, list[str]] = {}
        for k, v in (query or {}).items():
            merged_query[k] = list(v)
        for k, v in inline.items():
            merged_query.setdefault(k, []).extend(v)
        path = raw_path

        body_json: Optional[dict] = None
        if raw_body:
            try:
                parsed = json.loads(raw_body, parse_constant=_strict_constant)
            except ValueError as exc:
                return self._error_response(
                    APIErrorException(
                        code=APIErrorCode.BAD_REQUEST,
                        message=f"invalid JSON body: {exc}",
                        status=400,
                    )
                )
            if not isinstance(parsed, dict):
                return self._error_response(
                    APIErrorException(
                        code=APIErrorCode.BAD_REQUEST,
                        message="request body must be a JSON object",
                        status=400,
                    )
                )
            body_json = parsed

        for regex, methods, handler in self._routes:
            m = regex.match(path)
            if not m:
                continue
            if method not in methods:
                return self._error_response(
                    APIErrorException(
                        code=APIErrorCode.METHOD_NOT_ALLOWED,
                        message=f"method {method!r} not allowed for {path!r}",
                        status=405,
                    )
                )
            ctx = RequestContext(
                method=method,
                path=path,
                params=dict(m.groupdict()),
                query=merged_query,
                body=body_json,
                raw_body=raw_body,
            )
            try:
                return handler(ctx)
            except APIErrorException as exc:
                return self._error_response(exc)
            except (ValidationError, ValueError, TypeError) as exc:
                return self._error_response(
                    APIErrorException(
                        code=APIErrorCode.BAD_REQUEST,
                        message=f"bad request: {exc}",
                        status=400,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — last-resort safety net
                return self._error_response(map_domain_exception(exc))

        return self._error_response(
            APIErrorException(
                code=APIErrorCode.NOT_FOUND,
                message=f"no route for {method} {path!r}",
                status=404,
            )
        )

    def _error_response(self, exc: APIErrorException) -> ResponseEnvelope:
        envelope = ErrorResponse(
            error=exc.payload,
            timestamp=_now_iso(),
            schema_version=1,
        )
        return ResponseEnvelope(status=exc.status, body=_safe_dump(envelope))

    def _resolve_session_id(self, ctx: RequestContext) -> tuple[str, Any]:
        """Map a deployment_id to a live session id (or persisted fallback)."""
        deployment_id = ctx.params["deployment_id"]
        deployment = self.center.get_deployment(deployment_id)
        if deployment is None:
            raise APIErrorException(
                code=APIErrorCode.UNKNOWN_DEPLOYMENT,
                message=f"unknown deployment {deployment_id!r}",
                status=404,
            )
        sid = self.center.find_session_for_deployment(deployment_id)
        if sid is not None:
            return sid, deployment
        sessions = self.center.list_sessions(deployment_id=deployment_id)
        if sessions:
            return sessions[-1].session_id, deployment
        raise APIErrorException(
            code=APIErrorCode.UNKNOWN_SESSION,
            message=(
                f"no live or persisted session for deployment {deployment_id!r}"
            ),
            status=404,
        )

    def _require_live_session(self, ctx: RequestContext) -> tuple[str, Any]:
        """Return ``(session_id, deployment)`` for the live session only."""
        deployment_id = ctx.params["deployment_id"]
        deployment = self.center.get_deployment(deployment_id)
        if deployment is None:
            raise APIErrorException(
                code=APIErrorCode.UNKNOWN_DEPLOYMENT,
                message=f"unknown deployment {deployment_id!r}",
                status=404,
            )
        sid = self.center.find_session_for_deployment(deployment_id)
        if sid is None:
            raise APIErrorException(
                code=APIErrorCode.UNKNOWN_SESSION,
                message=(
                    f"no live session attached to deployment {deployment_id!r}"
                ),
                status=404,
            )
        return sid, deployment

    def _route_health(self, ctx: RequestContext) -> ResponseEnvelope:
        body = HealthEndpointResponse()
        return ResponseEnvelope(status=200, body=_safe_dump(body))

    def _route_list_deployments(self, ctx: RequestContext) -> ResponseEnvelope:
        deployment_id = _single(ctx.query, "deployment_id")
        strategy_id = _single(ctx.query, "strategy_id")
        symbol = _single(ctx.query, "symbol")
        timeframe = _single(ctx.query, "timeframe")
        status = _single(ctx.query, "status")
        try:
            limit = _bounded_int(ctx.query, "limit", default=200, lo=1, hi=1000)
        except APIErrorException as exc:
            return self._error_response(exc)

        rows = self.center.list_deployments(
            deployment_id=deployment_id,
            strategy_id=strategy_id,
            symbol=symbol,
            timeframe=timeframe,
            status=status,
        )
        rows = rows[:limit]
        body = DeploymentListResponse(
            deployments=[build_deployment_summary(d) for d in rows],
            count=len(rows),
        )
        return ResponseEnvelope(status=200, body=_safe_dump(body))

    def _route_get_deployment(self, ctx: RequestContext) -> ResponseEnvelope:
        deployment_id = ctx.params["deployment_id"]
        deployment = self.center.get_deployment(deployment_id)
        if deployment is None:
            raise APIErrorException(
                code=APIErrorCode.UNKNOWN_DEPLOYMENT,
                message=f"unknown deployment {deployment_id!r}",
                status=404,
            )
        body = DeploymentResponse(deployment=build_deployment_summary(deployment))
        return ResponseEnvelope(status=200, body=_safe_dump(body))

    def _do_lifecycle(self, ctx: RequestContext, op_name: str) -> ResponseEnvelope:
        deployment_id = ctx.params["deployment_id"]
        if ctx.body is not None:
            try:
                LifecycleRequest.model_validate(ctx.body)
            except ValidationError as exc:
                raise APIErrorException(
                    code=APIErrorCode.BAD_REQUEST,
                    message=f"invalid lifecycle request body: {exc}",
                    status=400,
                ) from exc
        # Explicit dispatch table — no dynamic attribute lookup.
        ops = {
            "activate": self.center.activate_deployment,
            "pause": self.center.pause_deployment,
            "resume": self.center.resume_deployment,
            "stop": self.center.stop_deployment,
        }
        if op_name not in ops:
            raise APIErrorException(
                code=APIErrorCode.INTERNAL_ERROR,
                message=f"unknown lifecycle op {op_name!r}",
                status=500,
            )
        ops[op_name](deployment_id)
        body = DeploymentResponse(
            deployment=build_deployment_summary(self.center.get_deployment(deployment_id))
        )
        return ResponseEnvelope(status=200, body=_safe_dump(body))

    def _route_activate(self, ctx: RequestContext) -> ResponseEnvelope:
        return self._do_lifecycle(ctx, "activate")

    def _route_pause(self, ctx: RequestContext) -> ResponseEnvelope:
        return self._do_lifecycle(ctx, "pause")

    def _route_resume(self, ctx: RequestContext) -> ResponseEnvelope:
        return self._do_lifecycle(ctx, "resume")

    def _route_stop(self, ctx: RequestContext) -> ResponseEnvelope:
        return self._do_lifecycle(ctx, "stop")

    def _route_reset_circuit_breaker(self, ctx: RequestContext) -> ResponseEnvelope:
        deployment_id = ctx.params["deployment_id"]
        sid, _deployment = self._require_live_session(ctx)
        try:
            self.center.reset_circuit_breaker(sid)
        except Exception as exc:  # noqa: BLE001
            raise APIErrorException(
                code=APIErrorCode.CIRCUIT_BREAKER_OPEN,
                message=f"circuit-breaker reset failed: {exc}",
                status=409,
            ) from exc
        body = CircuitBreakerResponse(
            circuit_breaker=self.center.inspect_circuit_breaker(sid)
        )
        return ResponseEnvelope(status=200, body=_safe_dump(body))

    def _route_session(self, ctx: RequestContext) -> ResponseEnvelope:
        if ctx.method == "POST":
            return self._route_checkpoint(ctx)
        sid, _ = self._resolve_session_id(ctx)
        session = self.center.inspect_session(sid)
        cp = self.center.session_store.get_checkpoint(sid)
        body = SessionResponse(session=session, checkpoint=cp)
        return ResponseEnvelope(status=200, body=_safe_dump(body))

    def _route_checkpoint(self, ctx: RequestContext) -> ResponseEnvelope:
        if ctx.body is not None:
            try:
                CheckpointRequest.model_validate(ctx.body)
            except ValidationError as exc:
                raise APIErrorException(
                    code=APIErrorCode.BAD_REQUEST,
                    message=f"invalid checkpoint request body: {exc}",
                    status=400,
                ) from exc
        sid, _ = self._require_live_session(ctx)
        try:
            cp = self.center.save_session(sid)
        except APIErrorException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise APIErrorException(
                code=APIErrorCode.INVALID_CHECKPOINT,
                message=f"checkpoint failed: {exc}",
                status=409,
            ) from exc
        session = self.center.inspect_session(sid)
        body = SessionResponse(session=session, checkpoint=cp)
        return ResponseEnvelope(status=200, body=_safe_dump(body))

    def _route_restore(self, ctx: RequestContext) -> ResponseEnvelope:
        if ctx.body is not None:
            try:
                RestoreRequest.model_validate(ctx.body)
            except ValidationError as exc:
                raise APIErrorException(
                    code=APIErrorCode.BAD_REQUEST,
                    message=f"invalid restore request body: {exc}",
                    status=400,
                ) from exc
        sid, _ = self._require_live_session(ctx)
        runner = self.center.get_runner(sid)
        if runner is None:
            raise APIErrorException(
                code=APIErrorCode.UNKNOWN_SESSION,
                message=f"no live runner for session {sid!r}",
                status=404,
            )
        try:
            cp = self.center.restore_session(session_id=sid, runner=runner)
        except APIErrorException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise APIErrorException(
                code=APIErrorCode.CORRUPTED_PERSISTED_STATE,
                message=f"restore failed: {exc}",
                status=409,
            ) from exc
        session = self.center.inspect_session(sid)
        body = SessionResponse(session=session, checkpoint=cp)
        return ResponseEnvelope(status=200, body=_safe_dump(body))

    def _route_account(self, ctx: RequestContext) -> ResponseEnvelope:
        sid, _ = self._resolve_session_id(ctx)
        body = AccountResponse(account=self.center.inspect_account(sid))
        return ResponseEnvelope(status=200, body=_safe_dump(body))

    def _route_positions(self, ctx: RequestContext) -> ResponseEnvelope:
        sid, _ = self._resolve_session_id(ctx)
        body = PositionsResponse(positions=self.center.inspect_positions(sid))
        return ResponseEnvelope(status=200, body=_safe_dump(body))

    def _route_performance(self, ctx: RequestContext) -> ResponseEnvelope:
        sid, _ = self._resolve_session_id(ctx)
        body = PerformanceResponse(performance=self.center.inspect_performance(sid))
        return ResponseEnvelope(status=200, body=_safe_dump(body))

    def _route_health_block(self, ctx: RequestContext) -> ResponseEnvelope:
        sid, _ = self._resolve_session_id(ctx)
        body = HealthResponse(health=self.center.inspect_health(sid))
        return ResponseEnvelope(status=200, body=_safe_dump(body))

    def _route_risk(self, ctx: RequestContext) -> ResponseEnvelope:
        sid, _ = self._resolve_session_id(ctx)
        body = RiskResponse(risk=self.center.inspect_risk(sid))
        return ResponseEnvelope(status=200, body=_safe_dump(body))

    def _route_circuit_breaker(self, ctx: RequestContext) -> ResponseEnvelope:
        sid, _ = self._resolve_session_id(ctx)
        body = CircuitBreakerResponse(
            circuit_breaker=self.center.inspect_circuit_breaker(sid)
        )
        return ResponseEnvelope(status=200, body=_safe_dump(body))

    def _route_events(self, ctx: RequestContext) -> ResponseEnvelope:
        sid, _ = self._resolve_session_id(ctx)
        event_type = _single(ctx.query, "event_type")
        try:
            since_sequence = _bounded_int(
                ctx.query, "since_sequence", default=0, lo=0, hi=10**9
            )
            limit = _bounded_int(
                ctx.query, "limit", default=100, lo=1, hi=1000
            )
        except APIErrorException as exc:
            return self._error_response(exc)
        raw_events = self.center.inspect_events(
            session_id=sid,
            event_type=event_type,
            since_sequence=since_sequence,
            limit=limit,
        )
        summary = DashboardEventSummary(
            total_events=len(raw_events),
            last_event_sequence=raw_events[-1]["sequence"] if raw_events else -1,
            last_event_type=raw_events[-1]["event_type"] if raw_events else None,
            last_event_timestamp=raw_events[-1]["timestamp"] if raw_events else None,
            recent=raw_events,
        )
        body = EventsResponse(events=summary)
        return ResponseEnvelope(status=200, body=_safe_dump(body))

    def _route_evidence(self, ctx: RequestContext) -> ResponseEnvelope:
        deployment_id = ctx.params["deployment_id"]
        deployment = self.center.get_deployment(deployment_id)
        if deployment is None:
            raise APIErrorException(
                code=APIErrorCode.UNKNOWN_DEPLOYMENT,
                message=f"unknown deployment {deployment_id!r}",
                status=404,
            )
        evidence = self.center.inspect_evidence(strategy_id=deployment.strategy_id)
        body = EvidenceResponse(evidence=evidence)
        return ResponseEnvelope(status=200, body=_safe_dump(body))

    def _route_dashboard(self, ctx: RequestContext) -> ResponseEnvelope:
        sid, _ = self._resolve_session_id(ctx)
        snap = self.center.build_dashboard_snapshot(sid)
        return ResponseEnvelope(
            status=200,
            body=json.loads(snap.model_dump_json(by_alias=True),
                            parse_constant=_strict_constant),
        )

    def _route_export(self, ctx: RequestContext) -> ResponseEnvelope:
        sid, _ = self._resolve_session_id(ctx)
        try:
            payload = self.center.export_json(sid)
        except APIErrorException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise APIErrorException(
                code=APIErrorCode.INTERNAL_ERROR,
                message=f"export failed: {exc}",
                status=500,
            ) from exc
        return ResponseEnvelope(
            status=200,
            body=json.loads(json.dumps(payload, default=str),
                            parse_constant=_strict_constant),
        )