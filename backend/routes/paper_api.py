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

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/paper", tags=["paper"])

_api_router: Optional[object] = None


def _get_api_router():
    """Lazily initialise the PaperAPIRouter singleton.

    Mirrors the CLI startup in ``trading_system.__main__._cmd_serve_paper_api``:
    creates a SQLAlchemy engine from the backend's ``market_data_db_url``
    setting, builds a ``PaperTradingControlCenter`` with relaxed evidence
    requirements, and wraps it in a ``PaperAPIRouter``.
    """
    global _api_router
    if _api_router is not None:
        return _api_router

    from sqlalchemy import create_engine

    from trading_system.paper_api import PaperAPIRouter
    from trading_system.paper.control import PaperTradingControlCenter
    from trading_system.research.strategy_intelligence import (
        EvidenceFreshnessConfig,
        EvidenceRequirement,
    )

    settings = get_settings()

    engine = create_engine(
        settings.market_data_db_url,
        connect_args={"check_same_thread": False},
    )

    # Relaxed evidence requirements — paper-only dev mode.
    # The gate still enforces: paper-only mode, spec identity binding,
    # symbol/timeframe match, and non-retired/non-rejected strategy status.
    requirement = EvidenceRequirement(
        require_walk_forward=False,
        require_validation=False,
        require_recent_evidence=False,
        min_validation_trades=0,
    )
    freshness = EvidenceFreshnessConfig(max_age_days=180)

    center = PaperTradingControlCenter.from_engine(
        engine, requirement=requirement, freshness_config=freshness
    )
    _api_router = PaperAPIRouter(center)

    logger.info(
        "Paper API router initialised (db=%s, routes=%d)",
        settings.market_data_db_url,
        len(_api_router.routes()),
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
async def _catch_all(request: Request, path: str) -> Response:
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
