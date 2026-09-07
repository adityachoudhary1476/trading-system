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
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Optional

from pydantic import BaseModel, ConfigDict, ValidationError

if TYPE_CHECKING:
    from ..autonomous.controller import AutonomousController

from ..paper.control import PaperTradingControlCenter, ControlCenterError
from ..paper.dashboard import (
    DashboardEventSummary,
    build_deployment_summary,
)
from ..research.evidence import strategy_identity
from ..research.strategy_lab.spec import StrategySpec
from ..execution.paper_broker import PaperBroker
from ..execution.broker import BrokerError
from ..paper.circuit_breaker import PaperCircuitBreaker
from ..paper.runner import PaperStrategyRunner
from ..paper.deployment import PaperDeploymentConfig
from .errors import (
    APIErrorCode,
    APIErrorException,
    ErrorResponse,
    map_domain_exception,
)
from .models import (
    AccountResponse,
    AutonomousBotResponse,
    AutonomousDecideResponse,
    AutonomousDeploymentsResponse,
    AutonomousEventsResponse,
    AutonomousLifecycleResponse,
    AutonomousScanResponse,
    CheckpointRequest,
    CircuitBreakerResponse,
    DeploymentCreateRequest,
    DeploymentCreateResponse,
    DeploymentListResponse,
    DeploymentResponse,
    EventsResponse,
    EvidenceResponse,
    HealthEndpointResponse,
    HealthResponse,
    LifecycleRequest,
    OptionsCapabilityResponse,
    OptionsProviderStatus,
    PROVIDER_STATUS_AVAILABLE,
    PROVIDER_STATUS_DISABLED,
    PROVIDER_STATUS_NOT_CONFIGURED,
    PROVIDER_STATUS_UNAVAILABLE,
    OrderIntentRequest,
    OrderIntentResponse,
    PerformanceResponse,
    PositionsResponse,
    RestoreRequest,
    RiskResponse,
    SessionResponse,
)


# Type alias for a route handler.
RouteHandler = Callable[["RequestContext"], "ResponseEnvelope"]

logger = logging.getLogger(__name__)


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

    def __init__(
        self,
        center: PaperTradingControlCenter,
        controller: Optional["AutonomousController"] = None,
    ) -> None:
        self.center = center
        self._controller: Optional["AutonomousController"] = controller
        self._routes: list[tuple[re.Pattern, frozenset[str], RouteHandler]] = []
        self._register_routes()

    def _require_controller(self) -> "AutonomousController":
        """Return the autonomous controller or raise if not configured."""
        if self._controller is None:
            raise APIErrorException(
                code=APIErrorCode.NOT_FOUND,
                message="autonomous controller not configured",
                status=501,
            )
        return self._controller

    def _register_routes(self) -> None:
        self._add(r"^/health$", frozenset({"GET"}), self._route_health)
        self._add(r"^/deployments$", frozenset({"GET", "POST"}), self._route_deployments)
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
        self._add(r"^/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)/orders$",
                  frozenset({"POST"}), self._route_submit_order)
        # Phase 8 — Options capability surface (Phase A, observational only).
        self._add(
            r"^/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)/options-capability$",
            frozenset({"GET"}),
            self._route_options_capability,
        )

        # Phase 6 — Autonomous Trading Operations Center
        self._add(r"^/autonomous/bot$", frozenset({"GET"}), self._route_autonomous_bot)
        self._add(r"^/autonomous/bot/(?P<action>start|pause|resume|stop)$",
                  frozenset({"POST"}), self._route_autonomous_lifecycle)
        self._add(r"^/autonomous/scan$", frozenset({"GET"}), self._route_autonomous_scan)
        self._add(r"^/autonomous/decide$", frozenset({"POST"}), self._route_autonomous_decide)
        self._add(r"^/autonomous/deployments$", frozenset({"GET"}), self._route_autonomous_deployments)
        self._add(r"^/autonomous/deployments/(?P<deployment_id>[A-Za-z0-9_-]+)/stop$",
                  frozenset({"POST"}), self._route_autonomous_stop_deployment)
        self._add(r"^/autonomous/events$", frozenset({"GET"}), self._route_autonomous_events)

        # Phase 22 - Adaptive Multi-Strategy Market Intelligence
        self._add(r"^/regime$", frozenset({"GET"}), self._route_regime)
        self._add(r"^/strategies$", frozenset({"GET"}), self._route_phase22_strategies)
        self._add(r"^/allocation$", frozenset({"GET"}), self._route_allocation)

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

    def _route_deployments(self, ctx: RequestContext) -> ResponseEnvelope:
        if ctx.method == "POST":
            return self._route_create_deployment(ctx)
        return self._route_list_deployments(ctx)

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

        try:
            rows = self.center.list_deployments(
                deployment_id=deployment_id,
                strategy_id=strategy_id,
                symbol=symbol,
                timeframe=timeframe,
                status=status,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "list_deployments failed in /deployments; returning empty list"
            )
            return ResponseEnvelope(
                status=200,
                body={
                    "deployments": [],
                    "count": 0,
                    "warning": f"deployment query failed: {exc.__class__.__name__}",
                    "skipped": [],
                },
            )

        rows = rows[:limit]
        summaries: list[DashboardDeploymentSummary] = []
        skipped: list[str] = []
        for d in rows:
            dep_id = self._safe_deployment_id(d)
            try:
                summaries.append(build_deployment_summary(d))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "skipping un-serializable deployment %s in /deployments: %s",
                    dep_id,
                    exc,
                )
                skipped.append(dep_id)
        body = DeploymentListResponse(
            deployments=summaries,
            count=len(summaries),
        )
        dumped = _safe_dump(body)
        dumped["skipped"] = skipped
        return ResponseEnvelope(status=200, body=dumped)

    @staticmethod
    def _safe_deployment_id(d) -> str:
        try:
            return str(d.deployment_id) if d.deployment_id is not None else "<unknown>"
        except Exception:
            return "<unknown>"

    def _route_create_deployment(self, ctx: RequestContext) -> ResponseEnvelope:
        """POST /deployments — create (or rehydrate) a paper deployment.

        Accepts either a full ``StrategySpec`` dict (``spec``) or a
        ``strategy_id`` referencing an already-registered strategy. The spec is
        registered in the strategy registry (idempotent), then the deployment
        gate is evaluated. On success the deployment is activated and a live
        ``PaperStrategyRunner`` + ``PaperBroker`` are attached so the dashboard
        returns a real account/balance immediately.

        Body::
            {
              "spec": { ...StrategySpec dict... },
              "symbol": "NSE:SBIN",         # optional, must match spec
              "timeframe": "1d",            # optional, must match spec
              "dataset_id": "market_data",  # optional
              "config": { ... }             # optional PaperDeploymentConfig overrides
            }

        Returns ``201`` with the created :class:`DeploymentCreateResponse`.
        Gate failure returns ``409`` with the reasons.
        """
        if ctx.body is None:
            raise APIErrorException(
                code=APIErrorCode.BAD_REQUEST,
                message="request body is required (spec or strategy_id, symbol, timeframe)",
                status=400,
            )
        try:
            req = DeploymentCreateRequest.model_validate(ctx.body)
        except ValidationError as exc:
            raise APIErrorException(
                code=APIErrorCode.BAD_REQUEST,
                message=f"invalid create request: {exc}",
                status=400,
            ) from exc

        spec: Optional[StrategySpec] = None
        strategy_id: Optional[str] = None

        if req.spec is not None:
            try:
                spec = StrategySpec(**req.spec)
            except ValidationError as exc:
                raise APIErrorException(
                    code=APIErrorCode.INVALID_STRATEGY_SPEC,
                    message=f"invalid StrategySpec: {exc}",
                    status=400,
                ) from exc
            strategy_id = strategy_identity(spec)
        elif req.strategy_id is not None:
            strategy_id = req.strategy_id
            existing = self.center.registry.get_strategy(strategy_id)
            if existing is None:
                raise APIErrorException(
                    code=APIErrorCode.UNKNOWN_STRATEGY,
                    message=f"strategy {strategy_id!r} is not registered",
                    status=404,
                )
            spec = StrategySpec.model_validate_json(existing.spec_json)
        else:
            raise APIErrorException(
                code=APIErrorCode.BAD_REQUEST,
                message="either 'spec' or 'strategy_id' must be provided",
                status=400,
            )

        if spec is None:
            raise APIErrorException(
                code=APIErrorCode.BAD_REQUEST,
                message="could not resolve a StrategySpec for this request",
                status=400,
            )

        if strategy_id is None:
            raise APIErrorException(
                code=APIErrorCode.BAD_REQUEST,
                message="could not derive strategy_id from spec",
                status=400,
            )

        # Idempotency: register the strategy if not already present.
        if self.center.registry.get_strategy(strategy_id) is None:
            self.center.registry.register_strategy(spec)

        # Build deployment config from optional overrides.
        config_overrides: dict[str, Any] = {}
        if req.config:
            config_overrides.update(req.config)
        cfg = PaperDeploymentConfig(**config_overrides)

        dataset_id = (req.dataset_id or "market_data")

        # Attempt creation (runs the gate). The gate may still fail if
        # evidence requirements are strict — that is the intended safety check.
        try:
            deployment, _spec, decision = self.center.create_deployment(
                spec=spec, dataset_id=dataset_id, config=cfg
            )
        except ControlCenterError as exc:
            raise APIErrorException(
                code=APIErrorCode.DEPLOYMENT_GATE_FAILED,
                message=f"deployment creation failed: {exc}",
                status=409,
                details={"reason": str(exc)},
            ) from exc

        if deployment is None:
            raise APIErrorException(
                code=APIErrorCode.DEPLOYMENT_GATE_FAILED,
                message="deployment gate rejected this strategy",
                details={"reasons": decision.reasons if decision else []},
                status=409,
            )

        # Activate the deployment and attach a live runner so the dashboard
        # has a session with real account/balance data.
        self.center.activate_deployment(deployment.deployment_id)

        broker = PaperBroker(initial_cash=cfg.initial_cash)
        circuit_breaker = PaperCircuitBreaker()
        runner = PaperStrategyRunner(
            deployment=deployment,
            broker=broker,
            spec=spec,
            circuit_breaker=circuit_breaker,
        )
        session_id = self.center.attach_runner(deployment.deployment_id, runner)

        # Also persist a checkpoint so the session survives a server restart.
        try:
            self.center.save_session(session_id)
        except Exception:
            logger.exception(
                "save_session failed for deployment %s after creation",
                deployment.deployment_id,
                exc_info=True,
            )

        body = DeploymentCreateResponse(
            deployment=build_deployment_summary(deployment),
            session_id=session_id,
        )
        return ResponseEnvelope(status=201, body=_safe_dump(body))

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

    # ------------------------------------------------------------------ #
    # Phase 8 — Options capability surface (Phase A, observational only)
    # ------------------------------------------------------------------ #
    def _inspect_options_providers(
        self,
    ) -> tuple[dict[str, "OptionsProviderStatus"], bool, Optional[str]]:
        """Inspect the controller's option-data provider wiring.

        Returns ``(providers, capable, last_error)``. ``capable`` is True
        iff the discoverer and quote provider are present and
        non-synthetic. The ``InMemoryOptionsChainProvider`` is explicitly
        rejected as production capability — this matches the existing
        controller guard in
        ``AutonomousController.resolve_options_plan``.

        This function is observational. It never instantiates a
        provider, never calls the network, and never modifies the
        controller. It tolerates an unattached controller (returns
        ``not_configured`` for every provider).

        The chain provider is treated as optional in Phase A: single-leg
        paper execution does not require it.
        """
        from trading_system.autonomous.options.discovery import (
            CurrentOptionDiscoverer,
        )
        from trading_system.autonomous.options_contract import (
            InMemoryOptionsChainProvider,
        )

        providers: dict[str, OptionsProviderStatus] = {
            "discoverer": OptionsProviderStatus(
                status=PROVIDER_STATUS_NOT_CONFIGURED,
                detail="autonomous controller not attached",
            ),
            "quote": OptionsProviderStatus(
                status=PROVIDER_STATUS_NOT_CONFIGURED,
                detail="autonomous controller not attached",
            ),
            "chain": OptionsProviderStatus(
                status=PROVIDER_STATUS_NOT_CONFIGURED,
                detail="autonomous controller not attached",
            ),
        }
        last_error: Optional[str] = None
        controller = self._controller
        if controller is None:
            return providers, False, last_error

        # Read controller slots via direct attribute access. The slots
        # are typed ``Optional[...]`` on ``AutonomousController``; on
        # unrelated test doubles a missing attribute is treated as
        # "not attached". The Python AST safety scan forbids ``getattr``
        # so we use ``try/except AttributeError`` instead.

        # --- discoverer ---
        try:
            discoverer = controller._option_discoverer  # type: ignore[attr-defined]
        except AttributeError:
            discoverer = None
        if isinstance(discoverer, CurrentOptionDiscoverer):
            providers["discoverer"] = OptionsProviderStatus(
                status=PROVIDER_STATUS_AVAILABLE,
                detail="CurrentOptionDiscoverer attached",
            )
        elif discoverer is None:
            providers["discoverer"] = OptionsProviderStatus(
                status=PROVIDER_STATUS_DISABLED,
                detail="option discoverer not attached",
            )
        else:
            providers["discoverer"] = OptionsProviderStatus(
                status=PROVIDER_STATUS_UNAVAILABLE,
                detail="option discoverer has unexpected type",
            )

        # --- quote provider ---
        try:
            quote_provider = controller._quote_provider  # type: ignore[attr-defined]
        except AttributeError:
            quote_provider = None
        if quote_provider is not None:
            # ``is_authenticated`` is the duck-typed contract on the
            # real ``CurrentOptionQuoteProvider``. On unrelated test
            # doubles lacking the attribute we must NOT silently claim
            # availability.
            try:
                auth_attr = quote_provider.is_authenticated
            except AttributeError:
                auth_attr = False
            if callable(auth_attr):
                authenticated = bool(auth_attr())
            else:
                authenticated = bool(auth_attr)
            if authenticated:
                providers["quote"] = OptionsProviderStatus(
                    status=PROVIDER_STATUS_AVAILABLE,
                    detail="CurrentOptionQuoteProvider authenticated",
                )
            else:
                providers["quote"] = OptionsProviderStatus(
                    status=PROVIDER_STATUS_UNAVAILABLE,
                    detail="quote provider not authenticated",
                )
        else:
            providers["quote"] = OptionsProviderStatus(
                status=PROVIDER_STATUS_DISABLED,
                detail="option quote provider not attached",
            )

        # --- chain provider ---
        try:
            chain_provider = controller._chain_provider  # type: ignore[attr-defined]
        except AttributeError:
            chain_provider = None
        if chain_provider is None:
            providers["chain"] = OptionsProviderStatus(
                status=PROVIDER_STATUS_DISABLED,
                detail="option chain provider not attached (Phase 8A multi-leg only)",
            )
        elif isinstance(chain_provider, InMemoryOptionsChainProvider):
            providers["chain"] = OptionsProviderStatus(
                status=PROVIDER_STATUS_UNAVAILABLE,
                detail="synthetic chain provider must not be used in production",
            )
            last_error = (
                "synthetic InMemoryOptionsChainProvider is attached; "
                "production capability rejected"
            )
        else:
            providers["chain"] = OptionsProviderStatus(
                status=PROVIDER_STATUS_AVAILABLE,
                detail="real chain provider attached",
            )

        # ``capable`` for single-leg options data means: discoverer + quote
        # are both available. The chain provider is optional for Phase A.
        capable = (
            providers["discoverer"].status == PROVIDER_STATUS_AVAILABLE
            and providers["quote"].status == PROVIDER_STATUS_AVAILABLE
        )
        return providers, capable, last_error

    def _route_options_capability(self, ctx: RequestContext) -> ResponseEnvelope:
        """Capability probe for the deployment's options stack.

        Strictly observational. Returns 200 + a typed
        :class:`OptionsCapabilityResponse` describing the deployment's
        configured options permissions plus the controller's option-data
        provider wiring. Returns 404 if the deployment does not exist.

        Does NOT place orders, does NOT change the scheduler, does NOT
        instantiate providers, and does NOT enable autonomous option
        execution. ``autonomous_execution_active`` is always False in
        Phase A; it is a hard-coded constant that future phases may
        consult.
        """
        deployment_id = ctx.params["deployment_id"]
        deployment = self.center.get_deployment(deployment_id)
        if deployment is None:
            raise APIErrorException(
                code=APIErrorCode.UNKNOWN_DEPLOYMENT,
                message=f"unknown deployment {deployment_id!r}",
                status=404,
            )

        cfg = deployment.config
        # Defensive read — PaperDeploymentConfig validates CE/PE at model
        # construction time, but a malformed deployment config_json could
        # in principle bypass that. We never crash the dashboard.
        try:
            allowed_option_types = list(cfg.allowed_option_types or [])
        except Exception:  # noqa: BLE001
            allowed_option_types = []
        try:
            max_contracts = cfg.max_options_contracts_per_trade
        except Exception:  # noqa: BLE001
            max_contracts = None

        try:
            providers, capable, last_error = self._inspect_options_providers()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "options capability inspection failed for %s: %r",
                deployment_id,
                exc,
            )
            providers = {
                name: OptionsProviderStatus(
                    status=PROVIDER_STATUS_UNAVAILABLE,
                    detail="capability inspection failed",
                )
                for name in ("discoverer", "quote", "chain")
            }
            capable = False
            last_error = "capability inspection failed"

        # ``enabled`` reflects only the deployment configuration. The
        # capability surface is intentionally decoupled from the
        # scheduler: a deployment may be configured for options even if
        # the scheduler never exercises them (Phase A status quo).
        enabled = bool(cfg.options_enabled)

        body = OptionsCapabilityResponse(
            enabled=enabled,
            allowed_option_types=allowed_option_types,
            max_contracts_per_trade=max_contracts,
            providers=providers,
            capable=capable,
            autonomous_execution_active=False,
            last_error=last_error,
        )
        return ResponseEnvelope(status=200, body=_safe_dump(body))

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

    def _route_submit_order(self, ctx: RequestContext) -> ResponseEnvelope:
        """POST /deployments/{id}/orders — external order-intent endpoint.

        Parses and validates the request, resolves the session, then delegates
        to ``PaperTradingControlCenter.submit_order_intent``. PaperBroker is
        never instantiated by this layer.
        """
        sid, _deployment = self._require_live_session(ctx)
        if ctx.body is None:
            raise APIErrorException(
                code=APIErrorCode.BAD_REQUEST,
                message="request body is required",
                status=400,
            )
        try:
            req = OrderIntentRequest.model_validate(ctx.body)
        except ValidationError as exc:
            raise APIErrorException(
                code=APIErrorCode.BAD_REQUEST,
                message=f"invalid order intent: {exc}",
                status=400,
            ) from exc
        # Translate validated request into the domain OrderIntent.
        from ..execution.orders import OrderIntent
        from ..paper.control import (
            ControlCenterError,
        )
        intent = OrderIntent(
            symbol=req.symbol,
            side=req.side,
            quantity=req.quantity,
            order_type=req.order_type,
            limit_price=req.limit_price,
            client_order_id=req.client_order_id,
            current_price=req.current_price,
            options_contract_id=req.options_contract_id,
            strike=req.strike,
            expiry=req.expiry,
            option_type=req.option_type,
        )
        try:
            result = self.center.submit_order_intent(session_id=sid, intent=intent)
        except APIErrorException:
            raise
        except ControlCenterError as exc:
            # Lifecycle / risk / short-selling rejections.
            msg = str(exc)
            if "not active" in msg or "is not active" in msg:
                code = APIErrorCode.LIFECYCLE_LOCKED
                status = 409
            elif "risk guard" in msg:
                code = APIErrorCode.RISK_HALTED
                status = 409
            elif "short selling" in msg:
                code = APIErrorCode.SHORTING_DISABLED
                status = 403
            else:
                code = APIErrorCode.ORDER_REJECTED
                status = 400
            raise APIErrorException(
                code=code, message=msg, status=status,
            ) from exc
        body = OrderIntentResponse(
            order_id=result.order_id,
            client_order_id=result.client_order_id,
            symbol=result.symbol,
            side=result.side,
            quantity=result.quantity,
            order_type=result.order_type,
            limit_price=result.limit_price,
            status=result.status,
            filled_quantity=result.filled_quantity,
            avg_fill_price=result.avg_fill_price,
            fills=result.fills,
            cash_after=result.cash_after,
            equity_after=result.equity_after,
            realized_pnl_after=result.realized_pnl_after,
            unrealized_pnl_after=result.unrealized_pnl_after,
            position_qty_after=result.position_qty_after,
            reject_reason=result.reject_reason,
            idempotent=result.is_idempotent_replay,
            options_contract_id=result.options_contract_id,
            strike=result.strike,
            expiry=result.expiry,
            option_type=result.option_type,
        )
        # A rejected broker validation is reported as a 400; otherwise 201.
        status_code = 201
        if result.status in ("REJECTED",):
            status_code = 400
        return ResponseEnvelope(status=status_code, body=_safe_dump(body))

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

    # ------------------------------------------------------------------ #
    # Phase 22 - Adaptive Multi-Strategy Market Intelligence routes
    # ------------------------------------------------------------------ #
    def _route_regime(self, ctx: RequestContext) -> ResponseEnvelope:
        """GET /regime — classify the current market regime from market data."""
        from ..research.phase22 import RegimeClassifier

        symbol = _single(ctx.query, "symbol") or "NSE:SBIN"
        timeframe = _single(ctx.query, "timeframe") or "1d"
        limit = _bounded_int(ctx.query, "limit", default=250, lo=10, hi=5000)

        df = self.center.load_market_data(symbol, timeframe)
        if df is None or len(df) == 0:
            raise APIErrorException(
                code=APIErrorCode.BAD_REQUEST,
                message=f"no market data provider configured for {symbol} {timeframe}",
                status=400,
            )

        df = df.tail(limit)
        classifier = RegimeClassifier()
        result = classifier.classify(df)

        body = {
            "regime": result.regime.value,
            "confidence": result.confidence,
            "features": result.features,
            "warnings": result.warnings,
            "regime_at_ms": result.regime_at_ms,
        }
        return ResponseEnvelope(status=200, body=body)

    def _route_phase22_strategies(self, ctx: RequestContext) -> ResponseEnvelope:
        """GET /strategies — list available Phase 22 strategy specs."""
        from ..research.phase22 import build_phase22_strategy_specs

        specs = build_phase22_strategy_specs()
        body = []
        for name, spec in specs.items():
            body.append({
                "name": name,
                "strategy_id": strategy_identity(spec),
                "spec_name": spec.name,
                "description": spec.description,
                "symbol": spec.symbol,
                "timeframe": spec.timeframe,
                "indicators": [d.key for d in spec.indicators],
                "entry_condition": spec.entry.op.value if spec.entry else None,
                "allow_short": spec.risk.allow_short,
                "generated_by": spec.generated_by,
            })
        return ResponseEnvelope(status=200, body=body)

    def _route_allocation(self, ctx: RequestContext) -> ResponseEnvelope:
        """GET /allocation — compute adaptive strategy allocation for current regime."""
        from ..research.phase22 import AdaptiveStrategySelector

        symbol = _single(ctx.query, "symbol") or "NSE:SBIN"
        timeframe = _single(ctx.query, "timeframe") or "1d"
        limit = _bounded_int(ctx.query, "limit", default=250, lo=10, hi=5000)

        df = self.center.load_market_data(symbol, timeframe)
        if df is None or len(df) == 0:
            raise APIErrorException(
                code=APIErrorCode.BAD_REQUEST,
                message=f"no market data provider configured for {symbol} {timeframe}",
                status=400,
            )

        df = df.tail(limit)
        selector = AdaptiveStrategySelector(self.center.intelligence)
        result = selector.allocate(df)

        body = {
            "regime": result.regime.value,
            "regime_confidence": result.regime_confidence,
            "regime_fit": result.regime_fit,
            "total_strategies_available": result.total_strategies_available,
            "timestamp_ms": result.timestamp_ms,
            "selected_strategies": [
                {
                    "strategy_name": sw.strategy_name,
                    "category": sw.category,
                    "regime_compatibility": sw.regime_compatibility,
                    "research_score": sw.research_score,
                    "weight": sw.weight,
                }
                for sw in result.selected_strategies
            ],
        }
        return ResponseEnvelope(status=200, body=body)

    # ------------------------------------------------------------------ #
    # Phase 6 — Autonomous Trading Operations Center routes
    # ------------------------------------------------------------------ #

    def _route_autonomous_bot(self, ctx: RequestContext) -> ResponseEnvelope:
        """GET /autonomous/bot — inspect the autonomous bot state."""
        controller = self._require_controller()
        inspect_dict = controller.inspect()
        body = AutonomousBotResponse(bot=inspect_dict)
        return ResponseEnvelope(status=200, body=_safe_dump(body))

    def _route_autonomous_lifecycle(self, ctx: RequestContext) -> ResponseEnvelope:
        """POST /autonomous/bot/{action} — manage bot lifecycle."""
        controller = self._require_controller()
        action = ctx.params["action"]
        dispatch = {
            "start": controller.start_bot,
            "pause": controller.pause_bot,
            "resume": controller.resume_bot,
            "stop": controller.stop_bot,
        }
        fn = dispatch[action]
        success, message = fn()
        body = AutonomousLifecycleResponse(
            success=success,
            message=message,
            bot=controller.inspect(),
        )
        return ResponseEnvelope(status=200, body=_safe_dump(body))

    def _route_autonomous_scan(self, ctx: RequestContext) -> ResponseEnvelope:
        """GET /autonomous/scan — run a market scan and rank candidates."""
        controller = self._require_controller()
        try:
            scan = controller.scan_market()
            ranking = controller.rank_candidates(scan)
        except Exception as exc:  # noqa: BLE001
            return self._error_response(
                APIErrorException(
                    code=APIErrorCode.INTERNAL_ERROR,
                    message=f"autonomous scan failed: {exc}",
                    status=500,
                )
            )
        scan_dict = json.loads(
            scan.model_dump_json(), parse_constant=_strict_constant
        )
        ranking_dict = (
            json.loads(ranking.model_dump_json(), parse_constant=_strict_constant)
            if ranking is not None
            else None
        )
        body = AutonomousScanResponse(scan=scan_dict, ranking=ranking_dict)
        return ResponseEnvelope(status=200, body=_safe_dump(body))

    def _route_autonomous_decide(self, ctx: RequestContext) -> ResponseEnvelope:
        """POST /autonomous/decide — produce structured trading decisions."""
        controller = self._require_controller()
        scan = controller.scan_market()
        ranking = controller.rank_candidates(scan)
        compatibility = controller.evaluate_strategy_compatibility(ranking)
        result = controller.generate_strategy_decisions(compatibility)
        body = AutonomousDecideResponse(
            result_id=result.result_id,
            scan_id=result.scan_id,
            ranking_id=result.ranking_id,
            evaluated_at=result.evaluated_at,
            decisions=[d.to_dict() for d in result.decisions],
            valid_count=result.valid_count,
            rejected_count=result.rejected_count,
        )
        return ResponseEnvelope(status=200, body=_safe_dump(body))

    def _route_autonomous_deployments(self, ctx: RequestContext) -> ResponseEnvelope:
        """GET /autonomous/deployments — list autonomous deployments."""
        controller = self._require_controller()
        try:
            deps = controller.list_autonomous_deployments()
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "list_autonomous_deployments failed in /autonomous/deployments"
            )
            return ResponseEnvelope(
                status=200,
                body={
                    "deployments": [],
                    "count": 0,
                    "warning": f"autonomous deployment query failed: {exc.__class__.__name__}",
                    "schema_version": 1,
                },
            )
        summaries: list[DashboardDeploymentSummary] = []
        skipped: list[str] = []
        for d in deps:
            dep_id = self._safe_deployment_id(d)
            try:
                summaries.append(build_deployment_summary(d))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "skipping un-serializable autonomous deployment %s: %s",
                    dep_id,
                    exc,
                )
                skipped.append(dep_id)
        body = AutonomousDeploymentsResponse(
            deployments=summaries,
            count=len(summaries),
        )
        dumped = _safe_dump(body)
        dumped["skipped"] = skipped
        return ResponseEnvelope(status=200, body=dumped)

    def _route_autonomous_stop_deployment(self, ctx: RequestContext) -> ResponseEnvelope:
        """POST /autonomous/deployments/{id}/stop — stop an autonomous deployment."""
        controller = self._require_controller()
        deployment_id = ctx.params["deployment_id"]
        result, message = controller.stop_autonomous_deployment(deployment_id)
        status = result.value
        return ResponseEnvelope(
            status=200,
            body=json.loads(
                json.dumps({"status": status, "message": message}),
                parse_constant=_strict_constant,
            ),
        )

    def _route_autonomous_events(self, ctx: RequestContext) -> ResponseEnvelope:
        """GET /autonomous/events — list autonomous events."""
        controller = self._require_controller()
        events = controller.event_log.events
        event_type = _single(ctx.query, "event_type")
        if event_type:
            events = [e for e in events if e.event_type.value == event_type]
        try:
            limit = _bounded_int(ctx.query, "limit", default=100, lo=1, hi=1000)
        except APIErrorException as exc:
            return self._error_response(exc)
        events = events[:limit]
        body = AutonomousEventsResponse(
            events=[e.model_dump() for e in events],
            count=len(events),
        )
        return ResponseEnvelope(status=200, body=_safe_dump(body))