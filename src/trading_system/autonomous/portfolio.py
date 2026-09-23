"""Autonomous Portfolio — portfolio-level autonomous PAPER trading (V1).

Design goal
-----------
Replace "deploy one strategy, then let that bot trade it" with a single
portfolio-level autonomous mode:

    market data
        -> strategy evaluation (the EXISTING Phase 22 strategies are signal
           providers, NOT independently deployed bots)
        -> valid opportunities
        -> autonomous portfolio (max N simultaneous positions)
        -> real, currently-available option contract (Phase 8B/8C Upstox)
        -> EXISTING PaperBroker execution path (submit_order_intent)
        -> multiple simultaneous positions
        -> ONE aggregated portfolio P&L

Everything is reused:

  * ``AutonomousController`` — kill switch, safety layer, option discovery,
    option execution (``execute_option_order``), event log.
  * ``PaperTradingControlCenter`` + ``PaperBroker`` — the ONLY execution path.
  * ``PaperSessionStore`` — order persistence / idempotency.
  * ``AutonomousDeploymentCoordinator`` — the one "portfolio mandate"
    deployment that owns the paper account + session for the portfolio.
  * Phase 22 research — ``RegimeClassifier``, ``AdaptiveStrategySelector``
    categorisation, ``regime_compatibility``, ``Phase23Discovery``,
    ``build_strategy``.

Safety (all fail-closed — data is never fabricated):
  * paper-only (enforced by the Pydantic bot config + DeploymentGate),
  * kill switch,
  * missing/stale market data -> no trade,
  * missing/unusable option chain -> no trade,
  * missing/stale/invalid quote -> no trade,
  * max positions -> no further entry,
  * insufficient paper capital -> the PaperBroker rejects the order.

No live broker, no network calls in this module, no real-money path.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

from pydantic import BaseModel

from .coordinator import (
    AutonomousDeploymentCoordinator,
    DeploymentCreationResult,
)
from .safety import KillSwitchReason

# The single autonomous portfolio deployment is uniquely identified by its
# dataset_id, while still carrying the canonical ``notes="bot:<bot_id>"``
# attribution shared with the rest of the autonomous stack.
PORTFOLIO_DATASET_PREFIX = "autonomous-portfolio"

DEFAULT_MAX_POSITIONS = 5
DEFAULT_CAPITAL = 100_000.0
DEFAULT_QUANTITY = 1.0

# Bounded activity-feed size persisted with the portfolio read model.
ACTION_LOG_LIMIT = 200


class PortfolioActionType(str, Enum):
    """Auditable autonomous-portfolio actions (the activity feed)."""

    STARTED = "STARTED"
    STOPPED = "STOPPED"
    ENTERED = "ENTERED"
    EXITED = "EXITED"
    SIGNAL_DETECTED = "SIGNAL_DETECTED"
    SIGNAL_REJECTED = "SIGNAL_REJECTED"
    OPTION_SELECTED = "OPTION_SELECTED"
    POSITION_LIMIT_REACHED = "POSITION_LIMIT_REACHED"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    FAIL_CLOSED = "FAIL_CLOSED"
    EVALUATED = "EVALUATED"
    SKIPPED = "SKIPPED"


class PortfolioAction(BaseModel):
    """One append-only portfolio action (JSON-safe, no secrets)."""

    model_config = {"extra": "forbid"}

    timestamp: str
    action: PortfolioActionType
    symbol: str = ""
    strategy_id: str = ""
    strategy_name: str = ""
    contract_id: str = ""
    option_type: str = ""
    strike: Optional[float] = None
    expiry: Optional[str] = None
    quantity: Optional[float] = None
    price: Optional[float] = None
    pnl: Optional[float] = None
    reason: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


@dataclass
class StrategyOpportunity:
    """One valid strategy opportunity produced by strategy evaluation."""

    strategy_id: str
    strategy_name: str
    symbol: str
    direction: int  # +1 bullish (CE), -1 bearish (PE)
    signal_value: int
    aggregate_score: float = 0.0
    regime_compatibility: Optional[float] = None
    research_score: Optional[float] = None
    category: str = ""
    timeframe: str = "1d"
    spot_price: Optional[float] = None

    @property
    def option_type(self) -> str:
        return "CE" if self.direction > 0 else "PE"

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "symbol": self.symbol,
            "direction": self.direction,
            "signal_value": self.signal_value,
            "aggregate_score": round(float(self.aggregate_score), 6),
            "regime_compatibility": self.regime_compatibility,
            "research_score": self.research_score,
            "category": self.category,
            "timeframe": self.timeframe,
            "option_type": self.option_type,
        }


def lookup_strategy_spec(center: Any, strategy_id: str):
    """Return the ``StrategySpec`` for a registered strategy_id, or None.

    Canonical implementation (previously inline in the scheduler): the
    registry can expose a strategy under an id that differs from the
    discovery-catalog id, so the registry is also scanned.
    """
    from trading_system.research.strategy_lab.spec import StrategySpec

    try:
        strategy = center.registry.get_strategy(strategy_id)
    except Exception:  # noqa: BLE001
        strategy = None
    if strategy is not None:
        try:
            return StrategySpec.model_validate_json(strategy.spec_json)
        except Exception:  # noqa: BLE001
            return None
    try:
        for s in center.registry.list_strategies():
            if s.strategy_id == strategy_id:
                try:
                    return StrategySpec.model_validate_json(s.spec_json)
                except Exception:  # noqa: BLE001
                    return None
    except Exception:  # noqa: BLE001
        return None
    return None


def instrument_from_position(position: Any):
    """Reconstruct an ``Instrument`` for an existing option position.

    The position's symbol key IS the provider symbol the contract was
    discovered under (``Instrument.key``), so it is preserved verbatim; the
    canonical ``options_contract_id`` is only a fallback lookup key.
    """
    from trading_system.india.instruments import (
        Instrument,
        InstrumentType,
        InternalSymbol,
    )

    symbol_key = str(getattr(position, "symbol", "") or "")
    if ":" in symbol_key:
        exchange, raw_symbol = symbol_key.split(":", 1)
    else:
        exchange, raw_symbol = "NFO", symbol_key

    option_type = getattr(position, "option_type", None)
    instr = Instrument(
        internal=InternalSymbol(exchange=exchange, symbol=raw_symbol),
        instrument_type=(
            InstrumentType.OPTION_CE if option_type == "CE" else InstrumentType.OPTION_PE
        ),
        name=symbol_key or getattr(position, "options_contract_id", None),
    )
    instr.provider_symbol = raw_symbol or getattr(position, "options_contract_id", None)
    instr.exchange_full = exchange
    instr.underlying = getattr(position, "underlying", None) or raw_symbol
    instr.expiry = getattr(position, "expiry", None)
    instr.strike = getattr(position, "strike", None)
    instr.option_type = option_type
    instr.lot_size = getattr(position, "contract_size", None) or 1
    return instr


def _buy_sell_decision(
    *,
    decision_id: str,
    symbol: str,
    strategy_id: str,
    action: str,
    reference_price: float,
    option_type: str,
    timeframe: str = "1d",
):
    """Build the minimal decision object ``execute_option_order`` expects.

    The controller only reads ``decision_id``, ``opportunity_symbol``,
    ``selected_configuration.{strategy_id,timeframe}``, ``action`` and
    ``signal.{action,reference_price,option_intent}``. Using the existing
    ``SimpleNamespace`` shape keeps the portfolio on the canonical execution
    path without inventing a parallel decision model.
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        decision_id=decision_id,
        opportunity_symbol=symbol,
        selected_configuration=SimpleNamespace(
            strategy_id=strategy_id, timeframe=timeframe
        ),
        action=action,
        signal=SimpleNamespace(
            action=action,
            reference_price=float(reference_price),
            option_intent=option_type,
        ),
    )



class AutonomousPortfolio:
    """Coordinate the single autonomous paper portfolio.

    Parameters
    ----------
    controller:
        The ``AutonomousController`` for this bot (kill switch, safety layer,
        option discoverer/quote provider, paper execution path).
    persistence:
        Optional ``AutonomousBotStateStore``-compatible object used to share
        the portfolio read model (P&L, positions, attribution, actions) with
        the API process. Without it the portfolio is process-local.
    max_positions:
        Maximum simultaneous positions. Defaults to the bot config's
        ``max_simultaneous_positions`` (5 by default).
    capital:
        Initial paper capital for the portfolio account (used only when the
        portfolio deployment is first created).
    quantity:
        Contracts per entry (default 1).
    evaluator:
        Optional strategy-evaluation callable
        ``(df, spot_price, regime) -> list[StrategyOpportunity]``. Defaults to
        the real Phase 22 pipeline; injectable for deterministic tests only.
    """

    def __init__(
        self,
        controller: Any,
        *,
        persistence: Optional[Any] = None,
        max_positions: Optional[int] = None,
        capital: float = DEFAULT_CAPITAL,
        quantity: float = DEFAULT_QUANTITY,
        max_contracts_per_trade: Optional[int] = None,
        evaluator: Optional[Callable[..., list[StrategyOpportunity]]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        action_log_limit: int = ACTION_LOG_LIMIT,
    ) -> None:
        self.controller = controller
        self.persistence = persistence
        self.capital = float(capital)
        self.quantity = float(quantity)
        self.max_contracts_per_trade = max_contracts_per_trade
        self._evaluator = evaluator
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._action_log_limit = int(action_log_limit)

        configured = getattr(controller.config, "max_simultaneous_positions", None)
        self.max_positions = int(
            max_positions
            if max_positions is not None
            else (configured if configured else DEFAULT_MAX_POSITIONS)
        )
        if self.max_positions < 1:
            self.max_positions = 1

        # contract_id / symbol -> strategy_id attribution for open positions.
        self._attribution: dict[str, str] = {}
        self._actions: list[PortfolioAction] = []
        self._deployment_id: Optional[str] = None

        # Cross-process read model (best effort; never fatal).
        state = self._load_persisted_state()
        if state:
            self._deployment_id = state.get("deployment_id") or None
            for raw in (state.get("actions") or [])[-self._action_log_limit:]:
                try:
                    self._actions.append(PortfolioAction.model_validate(raw))
                except Exception:  # noqa: BLE001
                    continue
            attribution = state.get("attribution") or {}
            if isinstance(attribution, dict):
                self._attribution = {
                    str(k): str(v) for k, v in attribution.items() if v
                }

    # ------------------------------------------------------------------ #
    # Identity / helpers
    # ------------------------------------------------------------------ #
    @property
    def bot_id(self) -> str:
        return self.controller.config.bot_id

    @property
    def portfolio_dataset_id(self) -> str:
        return f"{PORTFOLIO_DATASET_PREFIX}:{self.bot_id}"

    @property
    def actions(self) -> list[PortfolioAction]:
        return list(self._actions)


    def record_action(
        self,
        action: PortfolioActionType,
        *,
        symbol: str = "",
        strategy_id: str = "",
        strategy_name: str = "",
        contract_id: str = "",
        option_type: str = "",
        strike: Optional[float] = None,
        expiry: Optional[str] = None,
        quantity: Optional[float] = None,
        price: Optional[float] = None,
        pnl: Optional[float] = None,
        reason: str = "",
        detail: str = "",
    ) -> PortfolioAction:
        """Record one auditable portfolio action.

        The action is appended to the portfolio activity feed AND mirrored
        into the controller's existing typed ``AutonomousEventLog`` (reusing
        the existing event types) so ``/autonomous/events`` stays useful.
        """
        entry = PortfolioAction(
            timestamp=self._now().isoformat(),
            action=action,
            symbol=symbol,
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            contract_id=contract_id,
            option_type=option_type,
            strike=strike,
            expiry=expiry,
            quantity=quantity,
            price=price,
            pnl=pnl,
            reason=reason,
            detail=detail,
        )
        self._actions.append(entry)
        if len(self._actions) > self._action_log_limit:
            self._actions = self._actions[-self._action_log_limit:]
        self._mirror_event(entry)
        return entry

    def _mirror_event(self, entry: PortfolioAction) -> None:
        try:
            from .events import AutonomousEventType

            mapped = {
                PortfolioActionType.ENTERED: AutonomousEventType.DEPLOYMENT_CREATED,
                PortfolioActionType.EXITED: AutonomousEventType.DECISION_CREATED,
                PortfolioActionType.OPTION_SELECTED: AutonomousEventType.DECISION_CREATED,
                PortfolioActionType.SIGNAL_DETECTED: AutonomousEventType.DECISION_CREATED,
                PortfolioActionType.STARTED: AutonomousEventType.BOT_STARTED,
                PortfolioActionType.STOPPED: AutonomousEventType.BOT_STOPPED,
                PortfolioActionType.FAIL_CLOSED: AutonomousEventType.DECISION_REJECTED,
                PortfolioActionType.DATA_UNAVAILABLE: AutonomousEventType.DECISION_REJECTED,
                PortfolioActionType.POSITION_LIMIT_REACHED: (
                    AutonomousEventType.DECISION_REJECTED
                ),
                PortfolioActionType.SIGNAL_REJECTED: AutonomousEventType.DECISION_REJECTED,
            }.get(entry.action, AutonomousEventType.SCAN_COMPLETED)

            message = entry.action.value
            if entry.reason:
                message = f"{message}: {entry.reason}"
            self.controller.event_log.record(
                mapped,
                deployment_id=self._deployment_id or "",
                bot_id=self.bot_id,
                symbol=entry.symbol,
                timeframe="1d",
                message=message,
                payload=entry.model_dump(mode="json"),
            )
        except Exception:  # noqa: BLE001 — auditing must not break trading
            pass

    # ------------------------------------------------------------------ #
    # Persistence (cross-process read model)
    # ------------------------------------------------------------------ #
    def _load_persisted_state(self) -> dict[str, Any]:
        if self.persistence is None:
            return {}
        try:
            return self.persistence.load_portfolio_state(self.bot_id) or {}
        except Exception:  # noqa: BLE001
            return {}

    def _save_persisted_state(self, payload: dict[str, Any]) -> None:
        if self.persistence is None:
            return
        try:
            self.persistence.save_portfolio_state(self.bot_id, payload)
        except Exception:  # noqa: BLE001
            pass

    def _now(self) -> datetime:
        return self._clock()

    # ------------------------------------------------------------------ #
    # Account (one portfolio deployment + paper session)
    # ------------------------------------------------------------------ #
    def find_portfolio_deployment(self):
        """Return this bot's portfolio deployment, or None."""
        try:
            deployments = self.controller.control_center.list_deployments()
        except Exception:  # noqa: BLE001
            return None
        for dep in deployments:
            if getattr(dep, "dataset_id", None) != self.portfolio_dataset_id:
                continue
            if not (dep.notes or "").startswith(f"bot:{self.bot_id}"):
                continue
            return dep
        return None

    def mandate_strategy(self):
        """Resolve the ``(strategy_id, symbol, spec)`` mandate for the portfolio.

        The portfolio deployment is the *container* for the paper account, but
        it is still created through the existing deployment gate — so it must
        bind to a real, non-retired strategy with the required evidence.
        PAPER_APPROVED / PAPER_EXPERIMENTAL strategies are preferred and the
        bot's allowed symbols are honoured when possible. Returns None when no
        usable strategy exists (the caller then fails closed).
        """
        center = self.controller.control_center
        allowed = set(
            getattr(self.controller.config.user_constraints, "allowed_symbols", [])
            or []
        )
        candidates: list[tuple[str, str]] = []
        try:
            for item in self._discover_approved_strategies():
                candidates.append((item.strategy_id, item.symbol))
        except Exception:  # noqa: BLE001
            pass

        if not candidates:
            # Fall back to any registered strategy (the gate still decides).
            try:
                for strategy in center.registry.list_strategies():
                    symbol = getattr(strategy, "symbol", None)
                    if symbol:
                        candidates.append((strategy.strategy_id, symbol))
            except Exception:  # noqa: BLE001
                pass

        if not candidates:
            return None

        preferred = [c for c in candidates if not allowed or c[1] in allowed]
        for strategy_id, symbol in preferred + candidates:
            spec = lookup_strategy_spec(center, strategy_id)
            if spec is not None:
                return strategy_id, symbol, spec
        return None

    def _discover_approved_strategies(self) -> list[Any]:
        from trading_system.research.phase23.discovery import Phase23Discovery

        registry = self.controller.control_center.registry
        if not hasattr(registry, "get_paper_approved"):
            try:
                from trading_system.research.phase23.registry import Phase23Registry

                registry = Phase23Registry(registry)
            except Exception:  # noqa: BLE001
                return []
        discovery = Phase23Discovery(registry)
        return list(discovery.discover(max_candidates=50, include_experimental=True))

    def ensure_account(self) -> Optional[str]:
        """Ensure the portfolio deployment + live paper session exist.

        Idempotent. Returns the live session_id, or None (fail closed) when no
        portfolio account can be provisioned.
        """
        center = self.controller.control_center
        dep = self.find_portfolio_deployment()
        if dep is None:
            dep = self._create_portfolio_deployment()
            if dep is None:
                return None

        self._deployment_id = getattr(dep, "deployment_id", None)

        # Only ACTIVE deployments accept orders.
        try:
            from trading_system.paper.deployment import PaperDeploymentStatus

            if dep.status != PaperDeploymentStatus.ACTIVE:
                self._activate(dep)
        except Exception:  # noqa: BLE001
            pass

        sid = center.find_session_for_deployment(dep.deployment_id)
        if sid is not None:
            return sid

        # Fresh process: reconstruct the live session from the persisted
        # checkpoint via the existing control-center recovery path.
        try:
            return center.ensure_live_session(dep.deployment_id)
        except Exception:  # noqa: BLE001
            return None

    def _activate(self, dep) -> None:
        center = self.controller.control_center
        try:
            center.activate_deployment(dep.deployment_id)
        except Exception:  # noqa: BLE001
            refreshed = center.get_deployment(dep.deployment_id)
            if refreshed is not None:
                dep.status = refreshed.status



    def _create_portfolio_deployment(self):
        """Create the single portfolio deployment through the existing gate."""
        from trading_system.paper.deployment import PaperDeploymentConfig

        resolved = self.mandate_strategy()
        if resolved is None:
            self.record_action(
                PortfolioActionType.FAIL_CLOSED,
                reason="no_eligible_strategy_for_portfolio_mandate",
            )
            return None
        strategy_id, symbol, spec = resolved

        config = PaperDeploymentConfig(
            execution_mode="paper",
            initial_cash=self.capital,
            allow_short=False,
            options_enabled=True,
            allowed_option_types=["CE", "PE"],
            max_options_contracts_per_trade=self.max_contracts_per_trade,
        )
        coordinator = AutonomousDeploymentCoordinator(
            config=self.controller.config,
            control_center=self.controller.control_center,
        )
        try:
            result, dep = coordinator.create_autonomous_deployment(
                symbol=symbol,
                strategy_id=strategy_id,
                timeframe=getattr(spec, "timeframe", "1d"),
                strategy_spec=spec,
                deployment_config=config,
                dataset_id=self.portfolio_dataset_id,
            )
        except Exception:  # noqa: BLE001
            result, dep = DeploymentCreationResult.FAILURE, None

        if result == DeploymentCreationResult.DUPLICATE_DEPLOYMENT:
            # Another process created it concurrently — re-resolve.
            return self.find_portfolio_deployment()
        if result != DeploymentCreationResult.SUCCESS or dep is None:
            self.record_action(
                PortfolioActionType.FAIL_CLOSED,
                symbol=symbol,
                strategy_id=strategy_id,
                reason=f"portfolio_deployment_failed:{result.value}",
            )
            return None

        self.record_action(
            PortfolioActionType.STARTED,
            symbol=symbol,
            strategy_id=strategy_id,
            reason="portfolio_account_provisioned",
            detail=f"deployment={dep.deployment_id}",
        )
        return self.find_portfolio_deployment() or dep

    def on_start(self) -> Optional[str]:
        """Called when the operator starts the autonomous bot."""
        sid = self.ensure_account()
        if sid is not None:
            self.record_action(
                PortfolioActionType.STARTED,
                reason="autonomous_portfolio_running",
                detail=f"max_positions={self.max_positions}",
            )
        return sid

    def on_stop(self) -> None:
        """Called when the operator stops the autonomous bot."""
        dep = self.find_portfolio_deployment()
        if dep is not None:
            try:
                self.controller.control_center.stop_deployment(dep.deployment_id)
            except Exception:  # noqa: BLE001
                pass
            self.record_action(
                PortfolioActionType.STOPPED,
                reason="autonomous_portfolio_stopped",
                detail=f"deployment={dep.deployment_id}",
            )
        self._publish_snapshot()

    # ------------------------------------------------------------------ #
    # Market data / positions
    # ------------------------------------------------------------------ #
    def _symbols(self) -> list[str]:
        allowed = getattr(
            self.controller.config.user_constraints, "allowed_symbols", None
        )
        return sorted(allowed) if allowed else []

    def _market_data(self, symbol: str, timeframe: str = "1d"):
        try:
            return self.controller.control_center.load_market_data(symbol, timeframe)
        except Exception:  # noqa: BLE001
            return None

    @property
    def session_id(self) -> Optional[str]:
        dep = self.find_portfolio_deployment()
        if dep is None:
            return None
        self._deployment_id = dep.deployment_id
        try:
            return self.controller.control_center.find_session_for_deployment(
                dep.deployment_id
            )
        except Exception:  # noqa: BLE001
            return None

    def _runner(self):
        sid = self.session_id
        if sid is None:
            return None
        return self.controller.control_center.get_runner(sid)

    def open_positions(self) -> list[Any]:
        """Open positions in the portfolio's paper book (live broker only)."""
        runner = self._runner()
        if runner is None:
            return []
        try:
            positions = runner.broker.positions().values()
        except Exception:  # noqa: BLE001
            return []
        return [p for p in positions if getattr(p, "qty", 0) and p.qty != 0]

    def account(self):
        runner = self._runner()
        if runner is None:
            return None
        try:
            return runner.broker.account()
        except Exception:  # noqa: BLE001
            return None

    def _deployment_config(self):
        dep = self.find_portfolio_deployment()
        return dep.config if dep is not None else None

    def _realized_pnl(self) -> float:
        account = self.account()
        return float(getattr(account, "realized_pnl", 0.0) or 0.0) if account else 0.0



    # ------------------------------------------------------------------ #
    # Strategy evaluation (signal providers, NOT deployments)
    # ------------------------------------------------------------------ #
    def evaluate_opportunities(
        self,
        df: Any,
        spot_price: float,
        regime: Any,
        *,
        symbol: str = "NSE:NIFTY",
        timeframe: str = "1d",
    ) -> list[StrategyOpportunity]:
        """Evaluate ALL available strategies for the current market state.

        Returns every actionable (non-HOLD) opportunity, best score first.
        A strategy may also produce nothing — that is a valid outcome.
        """
        if self._evaluator is not None:
            result = self._evaluator(df, spot_price, regime)
            return sorted(
                list(result or []), key=lambda o: o.aggregate_score, reverse=True
            )
        return self._default_evaluator(
            df, spot_price, regime, symbol=symbol, timeframe=timeframe
        )

    def _default_evaluator(
        self,
        df: Any,
        spot_price: float,
        regime: Any,
        *,
        symbol: str,
        timeframe: str,
    ) -> list[StrategyOpportunity]:
        """Reuse the existing Phase 22 regime-aware strategy pipeline.

        This is the multi-strategy generalisation of the previous
        single-strategy Phase 22 tick: instead of keeping only the best
        strategy, every strategy with a non-HOLD signal is collected.
        """
        from trading_system.research.phase22 import (
            AdaptiveStrategySelector,
            Phase22Regime,
            regime_compatibility,
        )

        if regime is None or getattr(regime, "regime", None) == Phase22Regime.UNKNOWN:
            return []

        center = self.controller.control_center
        try:
            approved = self._discover_approved_strategies()
        except Exception:  # noqa: BLE001
            return []

        try:
            selector = AdaptiveStrategySelector(center.intelligence)
        except Exception:  # noqa: BLE001
            selector = None

        opportunities: list[StrategyOpportunity] = []
        for item in approved:
            strategy_id = getattr(item, "strategy_id", None)
            if not strategy_id:
                continue
            spec = lookup_strategy_spec(center, strategy_id)
            if spec is None:
                continue
            strategy_name = getattr(spec, "name", None) or strategy_id
            try:
                category_value = ""
                if selector is not None:
                    category = selector._categorize(strategy_name)
                    compat = float(regime_compatibility(category, regime.regime))
                    category_value = category.value
                else:  # pragma: no cover - selector construction is deterministic
                    compat = 0.0
                if compat <= 0.0:
                    continue
                item_score = getattr(item, "score", None)
                research_score = float(item_score) / 100.0 if item_score else None
                if research_score is None:
                    aggregate = compat
                else:
                    aggregate = 0.6 * compat + 0.4 * min(max(research_score, 0.0), 1.0)
            except Exception:  # noqa: BLE001
                continue

            try:
                from trading_system.research.strategy_lab.interpreter import (
                    build_strategy,
                )

                signal_series = build_strategy(spec).generate(df)
                signal_value = int(signal_series.iloc[-1])
            except Exception:  # noqa: BLE001
                continue

            if signal_value == 0:
                continue

            opportunities.append(
                StrategyOpportunity(
                    strategy_id=strategy_id,
                    strategy_name=strategy_name,
                    symbol=symbol,
                    direction=1 if signal_value > 0 else -1,
                    signal_value=signal_value,
                    aggregate_score=float(aggregate),
                    regime_compatibility=compat,
                    research_score=research_score,
                    category=category_value,
                    timeframe=timeframe,
                    spot_price=spot_price,
                )
            )

        opportunities.sort(key=lambda o: o.aggregate_score, reverse=True)
        return opportunities

    # ------------------------------------------------------------------ #
    # Position management
    # ------------------------------------------------------------------ #
    def refresh_marks(self) -> dict[str, Any]:
        """Mark every open option position to its current premium.

        Fail-closed: a position whose quote is missing/stale keeps its previous
        mark (never fabricated) and is reported as unmarked.
        """
        runner = self._runner()
        quote_provider = getattr(self.controller, "quote_provider", None)
        marked: list[str] = []
        unmarked: list[str] = []
        if runner is None or quote_provider is None:
            return {"marked": marked, "unmarked": unmarked, "provider": False}

        for position in self.open_positions():
            try:
                instrument = instrument_from_position(position)
                quote = quote_provider.get_quote(instrument)
                if quote is None or not quote_provider.is_fresh(quote):
                    unmarked.append(getattr(position, "symbol", ""))
                    continue
                ltp = getattr(quote, "ltp", None)
                if ltp is None or float(ltp) <= 0:
                    unmarked.append(getattr(position, "symbol", ""))
                    continue
                runner.broker.update_market_price(position.symbol, float(ltp))
                marked.append(position.symbol)
            except Exception:  # noqa: BLE001
                unmarked.append(getattr(position, "symbol", ""))
        return {"marked": marked, "unmarked": unmarked, "provider": True}

    def _position_strategy(self, position: Any) -> str:
        contract_id = getattr(position, "options_contract_id", None) or ""
        symbol = getattr(position, "symbol", "") or ""
        return self._attribution.get(contract_id) or self._attribution.get(symbol) or ""

    def _set_attribution(self, contract_id: str, symbol: str, strategy_id: str) -> None:
        if contract_id:
            self._attribution[contract_id] = strategy_id
        if symbol:
            self._attribution[symbol] = strategy_id

    def _clear_attribution(self, position: Any) -> None:
        for key in (
            getattr(position, "options_contract_id", None),
            getattr(position, "symbol", None),
        ):
            if key:
                self._attribution.pop(key, None)

    def _exit_reason(
        self, *, position: Any, strategy_id: str, opportunity_by_strategy: dict
    ) -> Optional[str]:
        """Decide whether an open position must be closed.

        Reuses (a) the deployment's configured stop-loss / take-profit
        thresholds and (b) the owning strategy's latest exit/reversal signal.
        """
        config = self._deployment_config()
        entry = float(getattr(position, "avg_entry_price", 0.0) or 0.0)
        current = float(getattr(position, "current_price", 0.0) or 0.0)
        if entry > 0 and current > 0:
            pnl_pct = (current - entry) / entry
            stop_loss = getattr(config, "stop_loss_pct", None)
            take_profit = getattr(config, "take_profit_pct", None)
            if stop_loss is not None and pnl_pct <= -abs(float(stop_loss)):
                return "stop_loss"
            if take_profit is not None and pnl_pct >= abs(float(take_profit)):
                return "take_profit"

        if strategy_id:
            opportunity = opportunity_by_strategy.get(strategy_id)
            position_direction = 1 if float(getattr(position, "qty", 0.0) or 0.0) > 0 else -1
            if opportunity is None:
                return "strategy_exit_signal"
            if opportunity.direction != position_direction:
                return "strategy_reversal_signal"
        return None



    @staticmethod
    def _underlying_symbol(position_symbol: str) -> str:
        """Best-effort underlying for an exit decision (options need CE/PE)."""
        raw = str(position_symbol or "").split(":", 1)[-1]
        upper = raw.upper()
        for name in ("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTY", "SENSEX"):
            if upper.startswith(name):
                return f"NSE:{name}"
        return f"NSE:{raw}" if raw else "NSE:NIFTY"

    def exit_positions(
        self,
        *,
        spot_price: float,
        opportunity_by_strategy: dict,
        session_id: str,
    ) -> list[dict[str, Any]]:
        """Manage/close open positions. Returns one result dict per position."""
        results: list[dict[str, Any]] = []
        config = self._deployment_config()
        if self._runner() is None:
            return results

        for position in list(self.open_positions()):
            strategy_id = self._position_strategy(position)
            reason = self._exit_reason(
                position=position,
                strategy_id=strategy_id,
                opportunity_by_strategy=opportunity_by_strategy,
            )
            if reason is None:
                continue

            option_type = getattr(position, "option_type", None)
            contract_id = getattr(position, "options_contract_id", None) or ""
            symbol = getattr(position, "symbol", "")
            if option_type not in ("CE", "PE"):
                results.append(
                    {
                        "result": "fail_closed",
                        "reason": "position_missing_option_metadata",
                        "symbol": symbol,
                    }
                )
                self.record_action(
                    PortfolioActionType.FAIL_CLOSED,
                    symbol=symbol,
                    strategy_id=strategy_id,
                    reason="position_missing_option_metadata",
                )
                continue

            self.record_action(
                PortfolioActionType.SIGNAL_DETECTED,
                symbol=symbol,
                strategy_id=strategy_id,
                contract_id=contract_id,
                option_type=option_type,
                reason=f"exit:{reason}",
            )

            realized_before = self._realized_pnl()
            exit_decision = _buy_sell_decision(
                decision_id=f"portfolio-exit:{contract_id or symbol}",
                symbol=self._underlying_symbol(symbol),
                strategy_id=strategy_id or "portfolio",
                action="sell",
                reference_price=spot_price,
                option_type=option_type,
            )
            result = self.controller.execute_option_order(
                decision=exit_decision,
                spot_price=spot_price,
                deployment_id=self._deployment_id,
                session_id=session_id,
                options_deployment_config=config,
                explicit_option_type=option_type,
                existing_position=position,
            )

            if result is None or str(getattr(result, "status", "")) != "FILLED":
                reason_text = (
                    "exit_execution_rejected"
                    if result is not None
                    else "exit_fail_closed"
                )
                detail = str(getattr(result, "reject_reason", "") or "")
                self.record_action(
                    PortfolioActionType.FAIL_CLOSED,
                    symbol=symbol,
                    strategy_id=strategy_id,
                    contract_id=contract_id,
                    option_type=option_type,
                    reason=reason_text,
                    detail=detail,
                )
                results.append(
                    {
                        "result": "rejected",
                        "reason": reason_text,
                        "detail": detail,
                        "symbol": symbol,
                    }
                )
                continue

            trade_pnl = self._realized_pnl() - realized_before
            results.append(
                {
                    "result": "exited",
                    "reason": reason,
                    "order_id": getattr(result, "order_id", None),
                    "symbol": symbol,
                    "contract_id": getattr(result, "options_contract_id", None),
                    "option_type": option_type,
                    "filled_quantity": getattr(result, "filled_quantity", None),
                    "avg_fill_price": getattr(result, "avg_fill_price", None),
                    "pnl": round(trade_pnl, 2),
                }
            )
            self.record_action(
                PortfolioActionType.EXITED,
                symbol=symbol,
                strategy_id=strategy_id,
                contract_id=contract_id,
                option_type=option_type,
                strike=getattr(position, "strike", None),
                expiry=getattr(position, "expiry", None),
                quantity=getattr(result, "filled_quantity", None),
                price=getattr(result, "avg_fill_price", None),
                pnl=round(trade_pnl, 2),
                reason=reason,
            )
            self._clear_attribution(position)
        return results

    # ------------------------------------------------------------------ #
    # Entries
    # ------------------------------------------------------------------ #
    def _signal_id(self, opportunity: StrategyOpportunity, spot_price: float) -> str:
        import hashlib

        payload = (
            f"{self.bot_id}:{opportunity.strategy_id}:{opportunity.symbol}:"
            f"{opportunity.option_type}:{opportunity.timeframe}:{spot_price}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:48]

    def _find_position(self, positions: list[Any], *, underlying: str, option_type: str):
        for position in positions:
            if getattr(position, "option_type", None) != option_type:
                continue
            symbol = str(getattr(position, "symbol", "") or "")
            if underlying.upper() in symbol.upper():
                return position
        return None

    def enter_opportunities(
        self,
        opportunities: list[StrategyOpportunity],
        *,
        spot_price: float,
        session_id: str,
    ) -> list[dict[str, Any]]:
        """Open paper positions for valid opportunities (portfolio-limited)."""
        results: list[dict[str, Any]] = []

        if self.controller.is_halted:
            self.record_action(
                PortfolioActionType.FAIL_CLOSED, reason="kill_switch_halted"
            )
            return results
        if not opportunities:
            return results

        positions = self.open_positions()
        if len(positions) >= self.max_positions:
            self.record_action(
                PortfolioActionType.POSITION_LIMIT_REACHED,
                reason=f"open_positions={len(positions)} max={self.max_positions}",
            )
            return results

        account = self.account()
        available_cash = (
            float(getattr(account, "available_cash", 0.0) or 0.0) if account else 0.0
        )
        if available_cash <= 0:
            self.record_action(
                PortfolioActionType.FAIL_CLOSED,
                reason="insufficient_paper_capital",
                detail=f"available_cash={available_cash}",
            )
            return results

        config = self._deployment_config()
        allowed_underlyings = set(
            getattr(
                self.controller.config.user_constraints,
                "allowed_option_underlyings",
                frozenset(),
            )
            or frozenset()
        )

        for opportunity in opportunities:
            if len(positions) >= self.max_positions:
                self.record_action(
                    PortfolioActionType.POSITION_LIMIT_REACHED,
                    reason=f"open_positions={len(positions)} max={self.max_positions}",
                )
                break

            underlying = opportunity.symbol.split(":")[-1]
            if allowed_underlyings and underlying.upper() not in {
                u.upper() for u in allowed_underlyings
            }:
                self.record_action(
                    PortfolioActionType.SIGNAL_REJECTED,
                    symbol=opportunity.symbol,
                    strategy_id=opportunity.strategy_id,
                    strategy_name=opportunity.strategy_name,
                    option_type=opportunity.option_type,
                    reason="underlying_not_allowed",
                )
                continue

            duplicate = self._find_position(
                positions, underlying=underlying, option_type=opportunity.option_type
            )
            if duplicate is not None:
                self.record_action(
                    PortfolioActionType.SIGNAL_REJECTED,
                    symbol=opportunity.symbol,
                    strategy_id=opportunity.strategy_id,
                    strategy_name=opportunity.strategy_name,
                    contract_id=getattr(duplicate, "options_contract_id", None) or "",
                    option_type=opportunity.option_type,
                    reason="position_already_open",
                )
                continue

            self.record_action(
                PortfolioActionType.SIGNAL_DETECTED,
                symbol=opportunity.symbol,
                strategy_id=opportunity.strategy_id,
                strategy_name=opportunity.strategy_name,
                option_type=opportunity.option_type,
                reason="valid_entry_signal",
                detail=f"score={opportunity.aggregate_score:.4f}",
            )

            decision = _buy_sell_decision(
                decision_id=self._signal_id(opportunity, spot_price),
                symbol=opportunity.symbol,
                strategy_id=opportunity.strategy_id,
                action="buy",
                reference_price=spot_price,
                option_type=opportunity.option_type,
                timeframe=opportunity.timeframe,
            )
            result = self.controller.execute_option_order(
                decision=decision,
                spot_price=spot_price,
                deployment_id=self._deployment_id,
                session_id=session_id,
                options_deployment_config=config,
                explicit_option_type=opportunity.option_type,
                explicit_side="buy",
                order_quantity=self.quantity,
            )

            self._handle_entry_result(opportunity, result, results)
            positions = self.open_positions()

        return results

    def _handle_entry_result(
        self,
        opportunity: StrategyOpportunity,
        result: Any,
        results: list[dict[str, Any]],
    ) -> None:
        """Record an entry attempt outcome (fail-closed on every non-fill)."""
        if result is None:
            self.record_action(
                PortfolioActionType.FAIL_CLOSED,
                symbol=opportunity.symbol,
                strategy_id=opportunity.strategy_id,
                strategy_name=opportunity.strategy_name,
                option_type=opportunity.option_type,
                reason="no_valid_option_contract_or_quote",
            )
            results.append(
                {
                    "result": "fail_closed",
                    "strategy_id": opportunity.strategy_id,
                    "symbol": opportunity.symbol,
                }
            )
            return

        status = str(getattr(result, "status", ""))
        if status != "FILLED":
            reject_reason = str(getattr(result, "reject_reason", "") or "")
            self.record_action(
                PortfolioActionType.SIGNAL_REJECTED,
                symbol=opportunity.symbol,
                strategy_id=opportunity.strategy_id,
                strategy_name=opportunity.strategy_name,
                option_type=opportunity.option_type,
                reason=reject_reason or f"order_status_{status.lower()}",
                detail="paper broker rejected the entry",
            )
            results.append(
                {
                    "result": "rejected",
                    "strategy_id": opportunity.strategy_id,
                    "symbol": opportunity.symbol,
                    "reason": reject_reason,
                }
            )
            return

        contract_id = getattr(result, "options_contract_id", None) or ""
        option_type = getattr(result, "option_type", opportunity.option_type)
        self.record_action(
            PortfolioActionType.OPTION_SELECTED,
            symbol=opportunity.symbol,
            strategy_id=opportunity.strategy_id,
            strategy_name=opportunity.strategy_name,
            contract_id=contract_id,
            option_type=option_type,
            strike=getattr(result, "strike", None),
            expiry=getattr(result, "expiry", None),
            price=getattr(result, "avg_fill_price", None),
            reason="option_contract_selected",
        )
        self.record_action(
            PortfolioActionType.ENTERED,
            symbol=opportunity.symbol,
            strategy_id=opportunity.strategy_id,
            strategy_name=opportunity.strategy_name,
            contract_id=contract_id,
            option_type=option_type,
            strike=getattr(result, "strike", None),
            expiry=getattr(result, "expiry", None),
            quantity=getattr(result, "filled_quantity", None),
            price=getattr(result, "avg_fill_price", None),
            reason="valid_entry_signal",
        )
        self._set_attribution(
            contract_id,
            str(getattr(result, "symbol", "") or ""),
            opportunity.strategy_id,
        )
        results.append(
            {
                "result": "entered",
                "strategy_id": opportunity.strategy_id,
                "strategy_name": opportunity.strategy_name,
                "symbol": opportunity.symbol,
                "contract_id": contract_id,
                "option_type": option_type,
                "strike": getattr(result, "strike", None),
                "expiry": getattr(result, "expiry", None),
                "order_id": getattr(result, "order_id", None),
                "filled_quantity": getattr(result, "filled_quantity", None),
                "avg_fill_price": getattr(result, "avg_fill_price", None),
            }
        )

        return results



    # ------------------------------------------------------------------ #
    # The autonomous tick
    # ------------------------------------------------------------------ #
    def tick(self, *, now: Optional[datetime] = None) -> dict[str, Any]:
        """One portfolio-level autonomous iteration.

          1. kill switch (fail closed)
          2. regular market session (fail closed outside it)
          3. market data for the bot's universe (fail closed when missing)
          4. regime classification (existing Phase 22 classifier)
          5. exits for existing positions
          6. multi-strategy evaluation
          7. portfolio-limited entries
          8. publish the portfolio snapshot (P&L, positions, actions)
        """
        started_at = now or self._now()
        base: dict[str, Any] = {
            "phase": "portfolio",
            "bot_id": self.bot_id,
            "started_at": started_at.isoformat(),
        }

        if self.controller.is_halted:
            self.record_action(
                PortfolioActionType.FAIL_CLOSED, reason="kill_switch_halted"
            )
            self._publish_snapshot(now=started_at)
            return {**base, "result": "skip", "reason": "kill_switch_halted"}

        if not self._is_regular_session(started_at):
            self.record_action(PortfolioActionType.SKIPPED, reason="market_closed")
            self._publish_snapshot(now=started_at)
            return {**base, "result": "skip", "reason": "market_closed"}

        market = None
        for symbol in self._symbols():
            df = self._market_data(symbol)
            if df is not None and len(df) > 0:
                market = (symbol, df)
                break
        if market is None:
            self.record_action(
                PortfolioActionType.DATA_UNAVAILABLE, reason="no_market_data"
            )
            self._publish_snapshot(now=started_at)
            return {**base, "result": "skip", "reason": "no_market_data"}

        symbol, df = market
        try:
            spot_price = float(df["close"].iloc[-1])
        except Exception:  # noqa: BLE001
            spot_price = 0.0
        if spot_price <= 0:
            self.record_action(
                PortfolioActionType.FAIL_CLOSED, reason="invalid_spot_price"
            )
            self._publish_snapshot(now=started_at)
            return {**base, "result": "skip", "reason": "invalid_spot_price"}

        timeframe = "1d"
        regime = self._classify_regime(df)

        session_id = self.ensure_account()
        if session_id is None:
            self.record_action(
                PortfolioActionType.DATA_UNAVAILABLE,
                reason="portfolio_account_unavailable",
            )
            self._publish_snapshot(now=started_at)
            return {**base, "result": "skip", "reason": "portfolio_account_unavailable"}

        marks = self.refresh_marks()


        from trading_system.research.phase22 import Phase22Regime

        regime_reason = ""
        if regime is None or getattr(regime, "regime", None) == Phase22Regime.UNKNOWN:
            regime_reason = "regime_unknown"
        elif float(getattr(regime, "confidence", 0.0) or 0.0) < 0.5:
            regime_reason = "regime_low_confidence"

        opportunities: list[StrategyOpportunity] = []
        if not regime_reason:
            try:
                opportunities = self.evaluate_opportunities(
                    df, spot_price, regime, symbol=symbol, timeframe=timeframe
                )
            except Exception:  # noqa: BLE001
                self.record_action(
                    PortfolioActionType.FAIL_CLOSED, reason="strategy_evaluation_failed"
                )
                opportunities = []
            self.record_action(
                PortfolioActionType.EVALUATED,
                symbol=symbol,
                reason="strategies_evaluated",
                detail=f"opportunities={len(opportunities)}",
            )

        opportunity_by_strategy = {o.strategy_id: o for o in opportunities}

        exits = self.exit_positions(
            spot_price=spot_price,
            opportunity_by_strategy=opportunity_by_strategy,
            session_id=session_id,
        )

        entries: list[dict[str, Any]] = []
        if regime_reason:
            self.record_action(
                PortfolioActionType.SIGNAL_REJECTED,
                symbol=symbol,
                reason=regime_reason,
                detail="no new entries without a usable market regime",
            )
        else:
            entries = self.enter_opportunities(
                opportunities, spot_price=spot_price, session_id=session_id
            )

        snapshot = self._publish_snapshot(now=started_at)
        executed = bool(entries) or bool(
            [e for e in exits if e.get("result") == "exited"]
        )
        return {
            **base,
            "result": "executed" if executed else "skip",
            "reason": None if executed else (regime_reason or "no_valid_opportunity"),
            "symbol": symbol,
            "spot_price": spot_price,
            "regime": (
                getattr(regime.regime, "value", regime.regime)
                if regime is not None and getattr(regime, "regime", None) is not None
                else None
            ),
            "regime_confidence": getattr(regime, "confidence", None),
            "evaluated_strategies": len(opportunities),
            "open_positions": snapshot["open_position_count"],
            "max_positions": self.max_positions,
            "entries": entries,
            "exits": exits,
            "marks": marks,
            "pnl": snapshot["pnl"],
        }

    def _classify_regime(self, df: Any) -> Any:
        try:
            from trading_system.research.phase22 import RegimeClassifier

            return RegimeClassifier().classify(df)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _is_regular_session(now: datetime) -> bool:
        """NSE regular session: 03:45-10:00 UTC (09:15-15:30 IST), weekdays."""
        try:
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            now = now.astimezone(timezone.utc)
            if now.weekday() >= 5:
                return False
            minutes = now.hour * 60 + now.minute
            return 3 * 60 + 45 <= minutes < 10 * 60
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ #
    # Snapshot (the UI read model)
    # ------------------------------------------------------------------ #
    def position_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for position in self.open_positions():
            rows.append(
                {
                    "symbol": getattr(position, "symbol", ""),
                    "contract_id": getattr(position, "options_contract_id", None) or "",
                    "option_type": getattr(position, "option_type", None) or "",
                    "strike": getattr(position, "strike", None),
                    "expiry": getattr(position, "expiry", None),
                    "quantity": getattr(position, "qty", 0.0),
                    "contract_size": getattr(position, "contract_size", 1),
                    "avg_entry_price": getattr(position, "avg_entry_price", 0.0),
                    "current_price": getattr(position, "current_price", 0.0),
                    "unrealized_pnl": round(
                        float(getattr(position, "unrealized_pnl", 0.0) or 0.0), 2
                    ),
                    "market_value": round(
                        float(getattr(position, "market_value", 0.0) or 0.0), 2
                    ),
                    "strategy_id": self._position_strategy(position),
                    "status": "OPEN",
                }
            )
        return rows

    def _attribution_rows(self, positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Strategy attribution: unrealized (open) + realized (closed) P&L."""
        buckets: dict[str, dict[str, Any]] = {}

        def _bucket(strategy_id: str) -> dict[str, Any]:
            return buckets.setdefault(
                strategy_id or "unattributed",
                {
                    "strategy_id": strategy_id or "unattributed",
                    "open_positions": 0,
                    "unrealized_pnl": 0.0,
                    "realized_pnl": 0.0,
                    "total_pnl": 0.0,
                },
            )

        for row in positions:
            bucket = _bucket(str(row.get("strategy_id") or ""))
            bucket["open_positions"] += 1
            bucket["unrealized_pnl"] = round(
                bucket["unrealized_pnl"] + float(row.get("unrealized_pnl") or 0.0), 2
            )

        # Realized P&L per strategy comes from the EXITED actions recorded on
        # the portfolio (closed positions no longer appear in the book).
        for action in self._actions:
            if action.action != PortfolioActionType.EXITED:
                continue
            bucket = _bucket(action.strategy_id)
            bucket["realized_pnl"] = round(
                bucket["realized_pnl"] + float(action.pnl or 0.0), 2
            )

        for bucket in buckets.values():
            bucket["total_pnl"] = round(
                bucket["unrealized_pnl"] + bucket["realized_pnl"], 2
            )
        return sorted(buckets.values(), key=lambda b: b["total_pnl"], reverse=True)



    def snapshot(self) -> dict[str, Any]:
        """Return the autonomous-portfolio read model.

        Live data (this process's paper broker) is authoritative when a live
        session exists. Otherwise the last snapshot published by the scheduler
        worker is returned, explicitly flagged as persisted/stale.
        """
        persisted = self._load_persisted_state()
        account = self.account()
        dep = self.find_portfolio_deployment()
        deployment_id = dep.deployment_id if dep is not None else self._deployment_id

        if account is None:
            if persisted:
                payload = dict(persisted)
                payload["data_source"] = "persisted"
                payload["stale"] = True
                payload["max_positions"] = self.max_positions
                return payload
            return self._empty_snapshot(
                deployment_id=deployment_id, reason="no_live_portfolio_session"
            )

        positions = self.position_rows()
        unrealized = round(float(getattr(account, "unrealized_pnl", 0.0) or 0.0), 2)
        realized = round(float(getattr(account, "realized_pnl", 0.0) or 0.0), 2)
        cash = round(float(getattr(account, "cash", 0.0) or 0.0), 2)
        available = round(float(getattr(account, "available_cash", 0.0) or 0.0), 2)
        equity = round(float(getattr(account, "equity", 0.0) or 0.0), 2)
        initial = round(float(getattr(account, "initial_cash", 0.0) or 0.0), 2)

        now = self._now()
        day = now.date().isoformat()
        baseline = persisted.get("realized_at_day_start")
        if persisted.get("trading_day") != day or baseline is None:
            baseline = realized
        today_realized = round(realized - float(baseline), 2)

        deployed = round(
            sum(float(row.get("market_value") or 0.0) for row in positions), 2
        )
        bot_state = getattr(getattr(self.controller, "config", None), "state", None)

        return {
            "portal": "autonomous-portfolio",
            "bot_id": self.bot_id,
            "trading_mode": str(
                getattr(
                    getattr(self.controller.config, "trading_mode", ""), "value", "paper"
                )
            ),
            "status": getattr(bot_state, "value", None) or "unknown",
            "data_source": "live",
            "stale": False,
            "updated_at": now.isoformat(),
            "trading_day": day,
            "deployment_id": deployment_id,
            "session_id": self.session_id,
            "max_positions": self.max_positions,
            "open_position_count": len(positions),
            "capital": {
                "initial": initial,
                "cash": cash,
                "available": available,
                "equity": equity,
                "invested": deployed,
            },
            "pnl": {
                "today": round(today_realized + unrealized, 2),
                "today_realized": today_realized,
                "realized": realized,
                "unrealized": unrealized,
                "total": round(realized + unrealized, 2),
                "starting_capital": initial,
                "return_pct": round(
                    ((equity - initial) / initial * 100.0) if initial > 0 else 0.0, 4
                ),
            },
            "positions": positions,
            "attribution": self._attribution_rows(positions),
            "strategies": self._strategy_rows(),
            "actions": [a.model_dump(mode="json") for a in self._actions],
            "safety": {
                "kill_switch_state": self.controller.kill_switch.state.value,
                "kill_switch_reason": (
                    self.controller.kill_switch.reason.value
                    if self.controller.kill_switch.reason
                    else None
                ),
                "paper_only": True,
            },
        }

    def read_snapshot(self) -> dict[str, Any]:
        """Read the portfolio state without mutating anything.

        Prefers the snapshot last published by the scheduler/worker (so the
        API process renders the same data the trading process sees), falling
        back to a live in-process snapshot. Never raises.
        """
        try:
            return self.snapshot()
        except Exception:  # noqa: BLE001
            persisted = self._load_persisted_state()
            if persisted:
                return persisted
            return self._empty_snapshot(
                deployment_id=self._deployment_id, reason="portfolio_unavailable"
            )

    def _strategy_rows(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        try:
            for item in self._discover_approved_strategies():
                rows.append(
                    {
                        "strategy_id": getattr(item, "strategy_id", ""),
                        "symbol": getattr(item, "symbol", ""),
                        "timeframe": getattr(item, "timeframe", ""),
                        "score": getattr(item, "score", None),
                    }
                )
        except Exception:  # noqa: BLE001
            rows = []
        evaluated = [
            a for a in self._actions[-50:] if a.action == PortfolioActionType.EVALUATED
        ]
        return {
            "available_count": len(rows),
            "available": rows,
            "last_evaluated_at": evaluated[-1].timestamp if evaluated else None,
        }


    def _empty_snapshot(
        self, *, deployment_id: Optional[str], reason: str
    ) -> dict[str, Any]:
        now = self._now()
        bot_state = getattr(getattr(self.controller, "config", None), "state", None)
        return {
            "portal": "autonomous-portfolio",
            "bot_id": self.bot_id,
            "trading_mode": "paper",
            "status": getattr(bot_state, "value", None) or "unknown",
            "data_source": "none",
            "stale": True,
            "updated_at": now.isoformat(),
            "trading_day": now.date().isoformat(),
            "deployment_id": deployment_id,
            "session_id": None,
            "max_positions": self.max_positions,
            "open_position_count": 0,
            "capital": {
                "initial": self.capital,
                "cash": self.capital,
                "available": self.capital,
                "equity": self.capital,
                "invested": 0.0,
            },
            "pnl": {
                "today": 0.0,
                "today_realized": 0.0,
                "realized": 0.0,
                "unrealized": 0.0,
                "total": 0.0,
                "starting_capital": self.capital,
                "return_pct": 0.0,
            },
            "positions": [],
            "attribution": [],
            "strategies": self._strategy_rows(),
            "actions": [a.model_dump(mode="json") for a in self._actions],
            "safety": {
                "kill_switch_state": self.controller.kill_switch.state.value,
                "kill_switch_reason": (
                    self.controller.kill_switch.reason.value
                    if self.controller.kill_switch.reason
                    else None
                ),
                "paper_only": True,
            },
            "warning": reason,
        }

    def _day_baseline(self, snapshot: dict[str, Any]) -> float:
        """Realized-P&L baseline for 'today's P&L' (resets when the day rolls)."""
        persisted = self._load_persisted_state()
        day = snapshot.get("trading_day")
        if (
            persisted.get("trading_day") == day
            and persisted.get("realized_at_day_start") is not None
        ):
            return float(persisted["realized_at_day_start"])
        return float((snapshot.get("pnl") or {}).get("realized") or 0.0)

    def _publish_snapshot(self, *, now: Optional[datetime] = None) -> dict[str, Any]:
        """Build + persist the portfolio read model. Never raises."""
        try:
            snapshot = self.snapshot()
        except Exception:  # noqa: BLE001
            return {}
        payload = dict(snapshot)
        payload.setdefault("trading_day", (now or self._now()).date().isoformat())
        payload["realized_at_day_start"] = self._day_baseline(snapshot)
        attribution = dict(self._attribution)
        for row in snapshot.get("positions") or []:
            key = row.get("contract_id") or row.get("symbol")
            if key and row.get("strategy_id"):
                attribution[key] = row["strategy_id"]
        payload["attribution"] = attribution
        self._save_persisted_state(payload)
        return snapshot


__all__ = [
    "ACTION_LOG_LIMIT",
    "DEFAULT_CAPITAL",
    "DEFAULT_MAX_POSITIONS",
    "DEFAULT_QUANTITY",
    "PORTFOLIO_DATASET_PREFIX",
    "AutonomousPortfolio",
    "PortfolioAction",
    "PortfolioActionType",
    "StrategyOpportunity",
    "instrument_from_position",
    "lookup_strategy_spec",
]
