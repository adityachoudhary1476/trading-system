"""Phase 23 autonomous PAPER-only execution scheduler.

A small, single-purpose worker that periodically drives the existing
autonomous pipeline (scan -> rank -> compatibility -> decision) and
submits each valid equity decision to ``PaperTradingControlCenter
.submit_order_intent`` so a real PaperBroker fills it.

This scheduler is EXPLICITLY DISABLED by default. Enable it by setting::

    AUTONOMOUS_SCHEDULER_ENABLED=true

Configuration (all read from environment, no defaults that change behaviour
without an opt-in):

    ``AUTONOMOUS_SCHEDULER_ENABLED``     default ``"false"`` — must be exactly
                                        ``"true"`` to start.
    ``AUTONOMOUS_SCAN_INTERVAL_SECONDS`` default ``60`` — integer seconds
                                        between ticks. Smallest allowed: 10.
    ``AUTONOMOUS_BOT_ID``               default ``"bot-paper-default"`` —
                                        matches the bot id used by the API
                                        (``routes/paper_api.py``).
    ``MARKET_DATA_DB_URL``              required — same persistent
                                        PostgreSQL database the FastAPI
                                        paper API uses. On SQLite the
                                        single-process lock falls back
                                        to a Python ``threading.Lock``.

Architecture:

  * Re-uses the **existing** ``PaperTradingControlCenter`` and
    ``AutonomousController`` — no second execution engine.
  * Re-uses the **existing** ``AutonomousDeploymentCoordinator`` to create
    autonomous deployments, so deployments carry the canonical
    ``notes="bot:<bot_id>"`` tag and appear under
    ``/api/paper/autonomous/deployments``.
  * Re-uses the **existing** ``PaperBroker`` by submitting every order
    through ``PaperTradingControlCenter.submit_order_intent`` — every
    safety gate (lifecycle, circuit breaker, risk guard, short-selling
    policy, durable idempotency, broker validation, accounting, event log)
    stays active. No direct position/balance/DB mutation.

Duplicate-prevention:

  * The signal identity is derived **only** from the ``StrategySignal``
    fields (``strategy_id``, ``symbol``, ``action``, ``timestamp``,
    ``reference_price``) — never from wall-clock time, never from
    Python object identity, never from the scheduler tick number.
  * The ``client_order_id`` for every order equals that signal id.
  * Before submission, the scheduler queries
    ``PaperSessionStore.get_order(session_id, signal_id)``. If a row
    already exists, the scheduler treats this signal as already
    executed and skips the submission. The order row is the canonical,
    restart-safe, replica-safe, single-source-of-truth.
  * The signal id is **also** invariant under repeated scheduler
    ticks because the bar close time and reference price only change
    when a new bar closes — at which point the strategy signal
    itself flips (or stays), and the new signal gets its own id.

Position-aware execution:

  * Before submission, the scheduler reads the live position via
    ``PaperBroker.get_position(symbol)``. If the position already
    matches the intended side with sufficient quantity, the
    scheduler skips — the signal has effectively been actioned.

Deployment lifecycle:

  * Re-uses the coordinator's existing deduplication rules:
    ``create_autonomous_deployment`` returns
    ``DUPLICATE_DEPLOYMENT`` when the bot already owns an ACTIVE
    deployment for the same ``(symbol, strategy_id, timeframe)``.
  * STOPPED deployments for the same bot+symbol+strategy are NOT
    silently resurrected; the coordinator falls through and creates
    a fresh deployment (this matches the existing Phase 1 contract
    documented in ``tests/test_autonomous_phase1.py:1066``).

Safety guarantees:

  * The scheduler **never** executes when:
        - ``AUTONOMOUS_SCHEDULER_ENABLED`` is not ``"true"``;
        - the kill switch is halted;
        - the trading calendar reports a closed session;
        - the configured market data provider returns ``None``;
        - another tick is already in progress (single-process lock);
        - a second replica is already running (Postgres advisory lock).
  * The scheduler **never** constructs a live broker. Only
    ``PaperBroker`` is instantiated by the coordinator.
  * The scheduler **never** prints, logs, or transmits credentials
    or access tokens.
  * Per-decision ``try/except`` ensures one bad decision cannot
    terminate the entire tick or block the rest of the queue.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

UTC = timezone.utc

logger = logging.getLogger("autonomous_scheduler")

ENV_ENABLED = "AUTONOMOUS_SCHEDULER_ENABLED"
ENV_INTERVAL = "AUTONOMOUS_SCAN_INTERVAL_SECONDS"
ENV_BOT_ID = "AUTONOMOUS_BOT_ID"
ENV_DB_URL = "MARKET_DATA_DB_URL"

DEFAULT_INTERVAL_SECONDS = 60
MIN_INTERVAL_SECONDS = 10
DEFAULT_BOT_ID = "bot-paper-default"
DEFAULT_ORDER_QUANTITY = 1

ADVISORY_LOCK_NAMESPACE = "autonomous-scheduler"

# Substrings that must NEVER appear in scheduler log output. Defence in
# depth: the scheduler does not read credentials directly, but if a
# future refactor accidentally picks one up, the redaction filter
# prevents accidental disclosure.
_REDACT_SUBSTRINGS = (
    "UPSTOX_SERVICE_ACCOUNT_TOKEN",
    "UPSTOX_CLIENT_ID",
    "FYERS_",
    "access_token=",
    "Authorization:",
    "Bearer ",
    "secret",
    "password",
    "api_key",
)

# When a credential-shaped token appears, also redact the value that
# follows it on the same line until whitespace or end-of-string.
_REDACT_TRAILING_VALUE_FOR = (
    "UPSTOX_SERVICE_ACCOUNT_TOKEN",
    "UPSTOX_CLIENT_ID",
    "FYERS_CLIENT_ID",
    "FYERS_SECRET_KEY",
    "FYERS_ACCESS_TOKEN",
)


# --------------------------------------------------------------------------- #
# Redacting formatter — strips credentials before any record hits the log
# --------------------------------------------------------------------------- #
class _RedactingFilter(logging.Filter):
    """Replace credential-shaped substrings with ``[REDACTED]`` in log records."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        redacted = msg
        # Run the trailing-value redactor FIRST so the bare-name
        # redactor downstream cannot strip the token name before the
        # value has been redacted.
        for token in _REDACT_TRAILING_VALUE_FOR:
            if token.lower() in redacted.lower():
                redacted = _redact_trailing_value_ci(redacted, token)
        for sub in _REDACT_SUBSTRINGS:
            if sub.lower() in redacted.lower():
                redacted = _replace_ci(redacted, sub, "[REDACTED]")
        record.msg = redacted
        record.args = ()
        return True


def _replace_ci(text: str, needle: str, replacement: str) -> str:
    """Case-insensitive replace without relying on ``re`` (keeps imports light)."""
    out = []
    i = 0
    n = len(text)
    nl = needle.lower()
    while i < n:
        idx = text.lower().find(nl, i)
        if idx < 0:
            out.append(text[i:])
            break
        out.append(text[i:idx])
        out.append(replacement)
        i = idx + len(needle)
    return "".join(out)


def _redact_trailing_value_ci(text: str, token: str) -> str:
    """For every occurrence of ``token`` in ``text``, redact everything
    after it until the next whitespace or end-of-string."""
    out = []
    i = 0
    n = len(text)
    tl = token.lower()
    while i < n:
        idx = text.lower().find(tl, i)
        if idx < 0:
            out.append(text[i:])
            break
        out.append(text[i:idx])
        out.append(token)
        # Skip past the token.
        j = idx + len(token)
        # Consume anything that looks like a value (until whitespace,
        # end-of-string, or comma). Redact it.
        k = j
        while k < n and not text[k].isspace() and text[k] not in {",", ";"}:
            k += 1
        out.append("[REDACTED]")
        i = k
    return "".join(out)


# --------------------------------------------------------------------------- #
# Config helpers
# --------------------------------------------------------------------------- #
def _env_enabled() -> bool:
    """``AUTONOMOUS_SCHEDULER_ENABLED`` must be exactly ``"true"`` to start."""
    raw = os.environ.get(ENV_ENABLED, "false").strip().lower()
    if raw == "true":
        return True
    if raw in {"1", "yes", "on"}:
        logger.warning(
            "%s=%r is not the literal 'true'; scheduler will NOT start. "
            "Use AUTONOMOUS_SCHEDULER_ENABLED=true to enable.",
            ENV_ENABLED,
            raw,
        )
    return False


def _env_interval() -> int:
    raw = os.environ.get(ENV_INTERVAL, str(DEFAULT_INTERVAL_SECONDS)).strip()
    try:
        seconds = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; using default %ds",
            ENV_INTERVAL, raw, DEFAULT_INTERVAL_SECONDS,
        )
        return DEFAULT_INTERVAL_SECONDS
    if seconds < MIN_INTERVAL_SECONDS:
        logger.warning(
            "%s=%d is below the minimum of %ds; clamping to %ds",
            ENV_INTERVAL, seconds, MIN_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS,
        )
        return MIN_INTERVAL_SECONDS
    return seconds


def _env_bot_id() -> str:
    bot_id = os.environ.get(ENV_BOT_ID, DEFAULT_BOT_ID).strip()
    return bot_id or DEFAULT_BOT_ID


def _env_db_url() -> Optional[str]:
    url = os.environ.get(ENV_DB_URL, "").strip()
    return url or None


# --------------------------------------------------------------------------- #
# Cross-replica advisory lock
# --------------------------------------------------------------------------- #
def _advisory_lock_int(name: str) -> int:
    """Derive a stable signed 63-bit int from a string."""
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    val = int.from_bytes(digest[:8], "big", signed=False)
    return val & 0x7FFFFFFFFFFFFFFF


class _InProcessLock:
    """Thread-safe in-process lock for the single-tick invariant."""

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._held = False

    def __enter__(self) -> bool:
        if not self._mutex.acquire(blocking=False):
            return False
        try:
            if self._held:
                return False
            self._held = True
            return True
        finally:
            self._mutex.release()

    def __exit__(self, exc_type, exc, tb) -> None:
        with self._mutex:
            self._held = False


@contextmanager
def _cross_replica_lock(engine: Engine) -> Iterator[bool]:
    """Acquire a Postgres advisory lock (or in-process mutex on SQLite).

    Yields True if the lock was acquired, False if a sibling tick is
    already running. Always released in ``finally``.
    """
    lock_id = _advisory_lock_int(ADVISORY_LOCK_NAMESPACE)
    dialect = engine.dialect.name
    if dialect == "postgresql":
        acquired = False
        conn = None
        try:
            conn = engine.connect()
            row = conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_id}
            ).scalar()
            acquired = bool(row)
            yield acquired
        finally:
            if conn is not None:
                try:
                    if acquired:
                        conn.execute(
                            text("SELECT pg_advisory_unlock(:k)"),
                            {"k": lock_id},
                        )
                except Exception:  # noqa: BLE001
                    logger.exception("failed to release Postgres advisory lock")
                conn.close()
    else:
        if dialect != "sqlite":
            logger.warning(
                "DB dialect %r is not Postgres; cross-replica lock falls "
                "back to a single-process lock. Use PostgreSQL in production.",
                dialect,
            )
        process_lock = _InProcessLock()
        with process_lock as acquired:
            yield acquired


# --------------------------------------------------------------------------- #
# Signal identity — stable across ticks, restarts, replicas
# --------------------------------------------------------------------------- #
def _signal_identity(
    strategy_id: str,
    symbol: str,
    action: str,
    signal_timestamp: str,
    reference_price: float,
) -> str:
    """Deterministic identity for one StrategySignal.

    Pure function of the signal payload (no wall-clock, no UUIDs, no
    Python object id, no tick number). The same signal — same
    ``(strategy_id, symbol, action, timestamp, reference_price)`` —
    always produces the same id, across process restarts and replicas.

    ``client_order_id`` for paper orders is set to this identity so
    that ``PaperSessionStore`` is the durable, restart-safe source of
    truth for "has this signal already been executed?".
    """
    payload = {
        "strategy_id": str(strategy_id),
        "symbol": str(symbol),
        "action": str(action),
        "signal_timestamp": str(signal_timestamp),
        "reference_price": round(float(reference_price), 6),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(("autonomous-signal:" + blob).encode("utf-8")).hexdigest()[:48]


def _signal_already_executed(session_store, session_id: str, signal_id: str) -> bool:
    """Return True if the durable paper-order store already records this signal."""
    try:
        rec = session_store.get_order(
            session_id=session_id, client_order_id=signal_id
        )
    except Exception:  # noqa: BLE001
        return False
    return rec is not None


def _position_matches(
    position, action: str, target_qty: float
) -> bool:
    """True iff the live position already reflects the intended signal.

    For BUY: already long with quantity >= target_qty.
    For SELL: already flat (no short allowed in Phase 23) — caller
    should treat this as "executed" because there is nothing to do.
    """
    if position is None:
        return False
    qty = float(position.qty)
    if action == "buy":
        return qty >= float(target_qty) and qty > 0
    if action == "sell":
        # SELL in Phase 23 is only ever a reduce/close because the
        # scheduler never sets allow_short=True. If the position is
        # flat, the " signal has effectively been actioned.
        return qty <= 0
    return False


# --------------------------------------------------------------------------- #
# Wire-up helpers (reuse the existing production classes)
# --------------------------------------------------------------------------- #
def _build_engine(db_url: str) -> Engine:
    connect_args: dict = {}
    if db_url.startswith("sqlite"):
        from pathlib import Path

        if db_url.startswith("sqlite:///"):
            rel_path = db_url[10:]
            if rel_path and not rel_path.endswith(":memory:"):
                Path(rel_path).parent.mkdir(parents=True, exist_ok=True)
        connect_args["check_same_thread"] = False
    return create_engine(db_url, connect_args=connect_args)


def _build_market_data_callable():
    """Fails closed if the market data provider is not authenticated."""
    from trading_system.india.upstox import UpstoxMarketDataProvider

    access_token = os.environ.get("UPSTOX_SERVICE_ACCOUNT_TOKEN", "").strip() or None
    md_provider = UpstoxMarketDataProvider(access_token=access_token)

    def market_data_callable(symbol: str, timeframe: str):
        if not md_provider.is_authenticated:
            return None
        try:
            return md_provider.get_historical(symbol, timeframe, limit=250)
        except Exception:  # noqa: BLE001
            return None

    return md_provider, market_data_callable


def _build_control_center(engine: Engine):
    """Mirror ``routes/paper_api._get_api_router`` so the scheduler and
    the API share the same DB state and schema."""
    from trading_system.paper.control import PaperTradingControlCenter
    from trading_system.research.evidence import EvidenceStore
    from trading_system.research.strategy_intelligence import (
        EvidenceFreshnessConfig,
        EvidenceRequirement,
    )

    try:
        EvidenceStore(engine).ensure_schema_current()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ensure_schema_current raised during scheduler startup: %r",
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
    return center, md_provider, market_data_callable


def _build_controller(center, md_provider, market_data_callable, bot_id: str):
    """Build a fresh ``AutonomousController`` for one bot."""
    from trading_system.autonomous.bot_config import (
        AutonomousBotConfig,
        BotMode,
        Source,
        TradingMode,
        UserConstraints,
    )
    from trading_system.autonomous.controller import AutonomousController

    bot_config = AutonomousBotConfig(
        bot_id=bot_id,
        name=f"Paper Autonomous Bot ({bot_id})",
        mode=BotMode.AUTONOMOUS,
        trading_mode=TradingMode.PAPER,  # ENFORCED by the Pydantic validator
        enabled=True,
        user_constraints=UserConstraints(
            allowed_symbols=frozenset({"NSE:SBIN", "NSE:TCS", "NSE:INFY"}),
            allowed_strategy_ids=frozenset(),
            allowed_timeframes=frozenset({"1m", "5m", "15m", "30m", "1h", "4h", "1d"}),
        ),
        max_simultaneous_positions=5,
        source=Source.AUTONOMOUS,
    )
    # Defence-in-depth: re-assert the trading mode at runtime.
    assert bot_config.trading_mode == TradingMode.PAPER, (
        "FATAL: scheduler must run in TradingMode.PAPER; aborting."
    )

    controller = AutonomousController(config=bot_config, control_center=center)
    controller.set_chain_provider(None)
    return controller


# --------------------------------------------------------------------------- #
# Decision-level execution (one decision, with isolation)
# --------------------------------------------------------------------------- #
def _execute_one_decision(
    controller,
    decision,
    target_qty: float,
) -> dict:
    """Execute a single decision through the canonical safety stack.

    Returns a structured result dict suitable for the tick log. Catches
    per-decision exceptions so one bad decision cannot kill the worker
    or block the rest of the queue.
    """
    from trading_system.autonomous.coordinator import (
        AutonomousDeploymentCoordinator,
        DeploymentCreationResult,
    )
    from trading_system.paper.deployment import (
        PaperDeploymentConfig,
        PaperDeploymentStatus,
    )
    from trading_system.execution.orders import OrderIntent, OrderType, Side

    symbol = decision.opportunity_symbol
    action = decision.signal.action.value
    timeframe = decision.selected_configuration.timeframe
    strategy_id = decision.selected_configuration.strategy_id
    ref_price = float(decision.signal.reference_price)

    # Compute the signal-identity once. This is the only key we trust
    # for both dedup lookup and the OrderIntent.client_order_id.
    signal_id = _signal_identity(
        strategy_id=strategy_id,
        symbol=symbol,
        action=action,
        signal_timestamp=str(decision.signal.timestamp),
        reference_price=ref_price,
    )

    center = controller.control_center

    # --- Locate (or create) an ACTIVE autonomous deployment. ---
    existing = None
    for d in center.list_deployments(symbol=symbol, timeframe=timeframe):
        if (
            d.notes
            and d.notes.startswith(f"bot:{controller.config.bot_id}")
            and d.strategy_id == strategy_id
            and d.status == PaperDeploymentStatus.ACTIVE
        ):
            existing = d
            break

    if existing is None:
        spec = _lookup_strategy_spec(center, strategy_id)
        if spec is None:
            return {
                "symbol": symbol,
                "strategy_id": strategy_id,
                "signal_id": signal_id,
                "result": "unknown_strategy",
            }
        coord = AutonomousDeploymentCoordinator(
            config=controller.config, control_center=center
        )
        result, dep = coord.create_autonomous_deployment(
            symbol=symbol,
            strategy_id=strategy_id,
            timeframe=timeframe,
            strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(
                execution_mode="paper",
                initial_cash=100_000.0,
                allow_short=False,
            ),
        )
        if result == DeploymentCreationResult.DUPLICATE_DEPLOYMENT:
            # Another worker created it concurrently; re-resolve.
            for d in center.list_deployments(symbol=symbol, timeframe=timeframe):
                if (
                    d.notes
                    and d.notes.startswith(f"bot:{controller.config.bot_id}")
                    and d.strategy_id == strategy_id
                    and d.status == PaperDeploymentStatus.ACTIVE
                ):
                    existing = d
                    break
            if existing is None:
                return {
                    "symbol": symbol,
                    "strategy_id": strategy_id,
                    "signal_id": signal_id,
                    "result": "duplicate_deployment_unresolved",
                }
        elif result != DeploymentCreationResult.SUCCESS or dep is None:
            return {
                "symbol": symbol,
                "strategy_id": strategy_id,
                "signal_id": signal_id,
                "result": "deployment_failed",
                "detail": result.value,
            }
        else:
            existing = dep

    # --- Resolve the live paper session. ---
    sid = center.find_session_for_deployment(existing.deployment_id)
    if sid is None:
        return {
            "symbol": symbol,
            "strategy_id": strategy_id,
            "deployment_id": existing.deployment_id,
            "signal_id": signal_id,
            "result": "no_session",
        }
    runner = center.get_runner(sid)
    if runner is None:
        return {
            "symbol": symbol,
            "strategy_id": strategy_id,
            "deployment_id": existing.deployment_id,
            "signal_id": signal_id,
            "result": "no_runner",
        }

    # --- Durable idempotency check via PaperSessionStore. ---
    if _signal_already_executed(center.session_store, sid, signal_id):
        return {
            "symbol": symbol,
            "strategy_id": strategy_id,
            "deployment_id": existing.deployment_id,
            "signal_id": signal_id,
            "result": "already_executed",
        }

    # --- Position-aware check via PaperBroker. ---
    position = runner.broker.get_position(symbol)
    if _position_matches(position, action, target_qty):
        return {
            "symbol": symbol,
            "strategy_id": strategy_id,
            "deployment_id": existing.deployment_id,
            "signal_id": signal_id,
            "result": "position_matches",
            "position_qty": float(position.qty) if position is not None else 0.0,
        }

    if ref_price <= 0:
        return {
            "symbol": symbol,
            "strategy_id": strategy_id,
            "deployment_id": existing.deployment_id,
            "signal_id": signal_id,
            "result": "no_reference_price",
        }
    runner.broker.update_market_price(symbol, ref_price)

    # --- Submit the canonical OrderIntent. ---
    side_enum = Side.BUY if action == "buy" else Side.SELL
    intent = OrderIntent(
        symbol=symbol,
        side=side_enum,
        quantity=target_qty,
        order_type=OrderType.MARKET,
        client_order_id=signal_id,
        current_price=ref_price,
    )
    try:
        order_result = center.submit_order_intent(session_id=sid, intent=intent)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "submit_order_intent rejected: symbol=%s strategy_id=%s "
            "signal_id=%s err_type=%s",
            symbol, strategy_id, signal_id, type(exc).__name__,
        )
        return {
            "symbol": symbol,
            "strategy_id": strategy_id,
            "deployment_id": existing.deployment_id,
            "signal_id": signal_id,
            "result": "rejected",
            "error_type": type(exc).__name__,
        }

    return {
        "symbol": symbol,
        "strategy_id": strategy_id,
        "deployment_id": existing.deployment_id,
        "session_id": sid,
        "signal_id": signal_id,
        "result": "submitted",
        "status": order_result.status,
        "is_idempotent_replay": order_result.is_idempotent_replay,
        "filled_quantity": order_result.filled_quantity,
        "order_id": order_result.order_id,
    }


def _lookup_strategy_spec(center, strategy_id: str):
    """Return the StrategySpec for a registered strategy_id, or None.

    Tries the registry by id first; falls back to scanning every
    registered strategy's stored spec for one whose ``strategy_id``
    matches (the discovery catalog and the registry can produce
    different identifiers for the same strategy).
    """
    from trading_system.research.strategy_lab.spec import StrategySpec

    strategy = center.registry.get_strategy(strategy_id)
    if strategy is not None:
        return StrategySpec.model_validate_json(strategy.spec_json)
    # Fallback — same strategy may be discoverable via the strategy
    # factory catalog even when not registered under that exact id.
    for s in center.registry.list_strategies():
        if s.strategy_id == strategy_id:
            return StrategySpec.model_validate_json(s.spec_json)
    return None


# --------------------------------------------------------------------------- #
# Tick logic
# --------------------------------------------------------------------------- #
def _is_regular_session(now: datetime) -> bool:
    from trading_system.india.market_calendar import TradingCalendar

    return TradingCalendar().is_regular_session(now)


def _has_fresh_data(center, symbol: str, timeframe: str) -> bool:
    """Returns True if the data provider returns a non-``None`` frame."""
    try:
        df = center.load_market_data(symbol, timeframe)
    except Exception:  # noqa: BLE001
        return False
    return df is not None


def _run_one_tick(controller, *, target_qty: float = DEFAULT_ORDER_QUANTITY) -> dict:
    """Run one scheduler tick. Returns a structured result for logging.

    Per-decision failures are caught and reported; they never abort the
    tick. The tick itself is wrapped in an outer try/except so an
    unexpected controller-level error is logged and the worker keeps
    running.
    """
    tick_id = uuid.uuid4().hex[:12]
    bot_id = controller.config.bot_id
    started_at = datetime.now(UTC)
    base = {
        "tick_id": tick_id,
        "bot_id": bot_id,
        "started_at": started_at.isoformat(),
    }

    # --- 1. Kill switch (public API) ---
    if controller.is_halted:
        return {**base, "result": "skip", "reason": "kill_switch_halted"}

    # --- 2. Regular session ---
    if not _is_regular_session(started_at):
        return {**base, "result": "skip", "reason": "market_closed"}

    # --- 3. Fresh data for at least one allowed symbol ---
    allowed_symbols = list(controller.config.user_constraints.allowed_symbols)
    timeframe = "1d"
    fresh_symbols = [
        s for s in allowed_symbols
        if _has_fresh_data(controller.control_center, s, timeframe)
    ]
    if not fresh_symbols:
        return {**base, "result": "skip", "reason": "no_fresh_market_data"}

    # --- 4. Run scan -> rank -> compatibility -> decisions ---
    try:
        scan = controller.scan_market()
    except Exception as exc:  # noqa: BLE001
        logger.exception("scan_market raised")
        return {
            **base, "result": "error",
            "reason": f"scan_error:{type(exc).__name__}",
            "error_category": "transient_infrastructure",
        }
    if not scan.candidates:
        return {**base, "result": "skip", "reason": "no_candidates"}
    try:
        ranking = controller.rank_candidates(scan)
        compat = controller.evaluate_strategy_compatibility(ranking)
        decisions = controller.generate_strategy_decisions(compat)
    except Exception as exc:  # noqa: BLE001
        logger.exception("decision pipeline raised")
        return {
            **base, "result": "error",
            "reason": f"decision_error:{type(exc).__name__}",
            "error_category": "unexpected_programming",
        }

    eligible = [
        d for d in decisions.decisions
        if d.is_valid
        and d.signal is not None
        and d.signal.action.value in ("buy", "sell")
        and d.selected_configuration is not None
    ]
    if not eligible:
        return {
            **base, "result": "skip", "reason": "no_eligible_decisions",
            "decision_count": len(decisions.decisions),
        }

    # --- 5. Per-decision execution with isolation ---
    submissions = []
    seen: set[tuple[str, str, str]] = set()
    for decision in eligible:
        identity = (
            decision.opportunity_symbol,
            decision.selected_configuration.strategy_id,
            decision.selected_configuration.timeframe,
        )
        if identity in seen:
            submissions.append(
                {
                    "symbol": decision.opportunity_symbol,
                    "result": "duplicate_in_tick",
                }
            )
            continue
        seen.add(identity)
        try:
            result = _execute_one_decision(controller, decision, target_qty)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "per-decision execution raised; isolating",
            )
            submissions.append(
                {
                    "symbol": decision.opportunity_symbol,
                    "result": "unexpected_error",
                    "error_type": type(exc).__name__,
                    "error_category": "unexpected_programming",
                }
            )
            continue
        submissions.append(result)

    return {
        **base,
        "result": "executed",
        "decision_count": len(decisions.decisions),
        "eligible_count": len(eligible),
        "submissions": submissions,
    }


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def _install_signal_handlers(stop_event: threading.Event) -> None:
    def _handler(signum, frame):  # noqa: ARG001
        logger.info("received signal %s; stopping scheduler", signum)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass


def _configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler()
    handler.addFilter(_RedactingFilter())
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    # Replace existing handlers so the redaction filter is always applied.
    root.handlers = [handler]
    root.setLevel(level)


def run() -> int:
    """Entrypoint for ``python -m backend.autonomous_scheduler``."""
    _configure_logging()

    if not _env_enabled():
        logger.info(
            "%s is not 'true'; scheduler is DISABLED. Exiting cleanly.",
            ENV_ENABLED,
        )
        return 0

    db_url = _env_db_url()
    if not db_url:
        logger.error(
            "%s is not set; cannot start the scheduler. Exiting.",
            ENV_DB_URL,
        )
        return 2

    interval = _env_interval()
    bot_id = _env_bot_id()

    logger.info(
        "starting autonomous scheduler: bot_id=%s interval=%ds db_kind=%s",
        bot_id,
        interval,
        "postgresql" if db_url.startswith("postgresql") else "sqlite",
    )

    engine = _build_engine(db_url)
    center, md_provider, market_data_callable = _build_control_center(engine)
    controller = _build_controller(center, md_provider, market_data_callable, bot_id)

    stop_event = threading.Event()
    _install_signal_handlers(stop_event)

    while not stop_event.is_set():
        tick_started = time.monotonic()
        try:
            with _cross_replica_lock(engine) as acquired:
                if not acquired:
                    logger.info(
                        "tick skipped: cross-replica lock held by another worker"
                    )
                else:
                    result = _run_one_tick(controller)
                    logger.info("tick result: %s", json.dumps(result, default=str))
        except Exception:  # noqa: BLE001
            logger.exception("tick raised; continuing")
        elapsed = time.monotonic() - tick_started
        sleep_for = max(0.0, float(interval) - elapsed)
        if stop_event.wait(timeout=sleep_for):
            break

    logger.info("autonomous scheduler stopped")
    return 0


if __name__ == "__main__":
    sys.exit(run())