"""Phase 21 paper-trading API adapter for FastAPI.

This module exposes the existing :class:`PaperAPIRouter` (Phase 21,
transport-agnostic dispatcher) as a FastAPI sub-application mounted under
``/api/paper``.  All requests are forwarded to ``PaperAPIRouter.dispatch``
which returns a :class:`ResponseEnvelope`; we translate that into a
``JSONResponse`` so the backend serves the same endpoints as the standalone
``paper-api`` CLI server without duplicating routing logic.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from auth import get_current_user, AuthenticatedUser
from config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/paper", tags=["paper"])

_api_router: Optional[object] = None
_controller: Optional[object] = None


def _build_market_data_callable():
    from trading_system.india.upstox import UpstoxMarketDataProvider

    settings = get_settings()
    md_provider = UpstoxMarketDataProvider(
        client_id=settings.upstox_client_id or None,
        access_token=settings.upstox_service_account_token or None,
    )

    def market_data_callable(symbol: str, timeframe: str):
        if not md_provider.is_authenticated:
            return None
        try:
            return md_provider.get_historical(symbol, timeframe, limit=250)
        except Exception:
            return None

    return md_provider, market_data_callable


def _build_controller(center, settings, md_provider, market_data_callable, persistence=None):
    from trading_system.autonomous.bot_config import (
        AutonomousBotConfig,
        BotMode,
        TradingMode,
        Source,
        UserConstraints,
    )
    from trading_system.autonomous.controller import AutonomousController

    bot_config = AutonomousBotConfig(
        bot_id="bot-nifty-options",
        name="Paper Autonomous Bot",
        mode=BotMode.AUTONOMOUS,
        trading_mode=TradingMode.PAPER,
        enabled=True,
        user_constraints=UserConstraints(
            allowed_symbols=frozenset({"NSE:NIFTY"}),
            allowed_strategy_ids=frozenset(),
            allowed_timeframes=frozenset({"1d"}),
            allowed_option_underlyings=frozenset({"NIFTY"}),
        ),
        max_simultaneous_positions=5,
        source=Source.AUTONOMOUS,
    )
    controller = AutonomousController(config=bot_config, control_center=center, persistence=persistence)

    if persistence is not None:
        try:
            controller.load_state(persistence.load_state(bot_config.bot_id))
        except Exception:
            pass

    if md_provider.is_authenticated:
        try:
            from trading_system.autonomous.options.discovery import (
                CurrentOptionDiscoverer,
            )
            from trading_system.india.instrument_repository import (
                InstrumentRepository,
            )
            from trading_system.india.upstox_discovery import (
                UpstoxInstrumentDiscovery,
            )

            repo = InstrumentRepository()
            discoverer = CurrentOptionDiscoverer(
                repository=repo,
                market_data_provider=market_data_callable,
            )
            for underlying in ("NIFTY", "BANKNIFTY", "FINNIFTY", "NIFTY50"):
                UpstoxInstrumentDiscovery(md_provider, repo).discover_options(
                    underlying
                )
            controller.set_option_discoverer(discoverer, repository=repo)
        except Exception as exc:
            logger.warning(
                "Autonomous option discoverer not attached: %s", exc
            )

        try:
            from trading_system.india.option_quotes import (
                CurrentOptionQuoteProvider,
            )

            controller.set_quote_provider(CurrentOptionQuoteProvider(md_provider))
        except Exception as exc:
            logger.warning(
                "Autonomous option quote provider not attached: %s", exc
            )

    controller.set_chain_provider(None)

    return controller


def _get_api_router():
    global _api_router, _controller
    if _api_router is not None:
        return _api_router

    from sqlalchemy import create_engine

    from trading_system.paper_api import PaperAPIRouter
    from trading_system.paper.control import PaperTradingControlCenter
    from trading_system.research.evidence import EvidenceStore
    from trading_system.research.strategy_intelligence import (
        EvidenceFreshnessConfig,
        EvidenceRequirement,
    )

    settings = get_settings()

    connect_args: dict = {}
    if settings.market_data_db_url.startswith("sqlite"):
        from pathlib import Path

        if settings.market_data_db_url.startswith("sqlite:///"):
            rel_path = settings.market_data_db_url[10:]
            if not rel_path.endswith(":memory:"):
                Path(rel_path).parent.mkdir(parents=True, exist_ok=True)
        connect_args["check_same_thread"] = False

    engine = create_engine(
        settings.market_data_db_url,
        connect_args=connect_args,
    )

    # Import persistence module so Base.metadata knows about autonomous_bots.
    from trading_system.autonomous.persistence import AutonomousBotStateStore

    # Idempotent forward migration: adds missing columns introduced after the
    # initial ``Base.metadata.create_all``. Phase 8 added
    # ``options_enabled`` / ``allowed_option_types_json`` /
    # ``max_options_contracts_per_trade`` to ``paper_deployments``; without
    # this step the SELECT against that table fails with
    # ``UndefinedColumn: paper_deployments.options_enabled`` on the existing
    # Railway database (schema pre-dating Phase 8). Fail-closed: a broken
    # migration must not prevent the API from starting — the deployment
    # routes have their own degraded fallback returning 200 + warning.
    try:
        EvidenceStore(engine).ensure_schema_current()
        AutonomousBotStateStore(engine).ensure_schema()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "paper_deployments migration helper raised: %r; deployment "
            "listing may fall back to empty + warning.",
            exc,
        )

    requirement = EvidenceRequirement(
        require_walk_forward=False,
        require_validation=False,
        require_recent_evidence=False,
        min_validation_trades=0,
    )
    freshness = EvidenceFreshnessConfig(max_age_days=180)

    md_provider, market_data_callable = _build_market_data_callable()

    center = PaperTradingControlCenter.from_engine(
        engine,
        requirement=requirement,
        freshness_config=freshness,
        market_data_provider=market_data_callable,
    )

    controller = None
    try:
        bot_store = AutonomousBotStateStore(engine)
        controller = _build_controller(
            center, settings, md_provider, market_data_callable, persistence=bot_store
        )
    except Exception as exc:
        logger.warning(
            "Autonomous controller not constructed for FastAPI paper API: %s",
            exc,
        )
        controller = None

    _controller = controller
    _api_router = PaperAPIRouter(center, controller=controller)

    db_kind = "postgresql" if settings.market_data_db_url.startswith("postgresql") else "sqlite"
    logger.info(
        "Paper API router initialised (db_kind=%s, routes=%d, autonomous=%s)",
        db_kind,
        len(_api_router.routes()),
        "wired" if controller is not None else "unwired",
    )
    return _api_router


def _build_query(request: Request) -> dict[str, list[str]]:
    """Extract query parameters as a multi-value dict (parse_qs shape)."""
    raw_qs = request.url.query
    if not raw_qs:
        return {}
    from urllib.parse import parse_qs

    return parse_qs(raw_qs, keep_blank_values=True)


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def _catch_all(
    request: Request,
    path: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Forward every request to the Phase 21 dispatcher."""
    api_router = _get_api_router()

    # Reconstruct the path WITHOUT the query string.  The query is passed
    # separately via the ``query`` argument, matching the stdlib server
    # contract (server.py `_dispatch`).  Including it in the path would
    # cause ``dispatch`` to merge duplicate values — its merge logic
    # extends (not replaces) inline params against the explicit ``query``
    # dict, so ``?limit=200`` would become ``limit=["200","200"]``.
    full_path = f"/{path}"

    raw_body = await request.body()
    raw_body_str = raw_body.decode("utf-8") if raw_body else ""

    query = _build_query(request)

    envelope = api_router.dispatch(
        request.method,
        full_path,
        query=query,
        raw_body=raw_body_str,
    )

    return JSONResponse(
        status_code=envelope.status,
        content=envelope.body,
    )
