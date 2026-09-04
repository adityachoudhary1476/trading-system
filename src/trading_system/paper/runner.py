"""Phase 18/19 — Paper Strategy Runner.

Phase 18 drives the existing ``SpecStrategy`` interpreter over a chronological
stream of bars, producing paper orders on the existing ``PaperBroker``. Phase 19
adds an optional, non-intrusive operations layer (event log, health monitor,
risk guard, circuit breaker, performance snapshots) that *observes* execution.

Critical rule: monitoring observes execution. It does not secretly alter strategy
behavior. The only exception is an explicitly configured circuit-breaker/risk
halt, which forces ``NO_ACTION``.

When no operations components are supplied, behavior is identical to Phase 18.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Union

import pandas as pd

from ..execution.broker import BrokerError
from ..execution.orders import Side
from ..execution.paper_broker import PaperBroker
from ..research.strategy_lab.interpreter import SpecStrategy
from ..research.strategy_lab.spec import StrategySpec
from .circuit_breaker import PaperCircuitBreaker
from .deployment import (
    PaperDeployment,
    PaperDeploymentConfig,
    PaperDeploymentStatus,
    STATUS_ACCEPTS_ORDERS,
)
from .events import PaperOperationEventType, PaperOperationsEventLog
from .health import PaperHealthMonitor
from .operations import PaperOperationsState, position_dict
from .risk import PaperRiskGuard
from .snapshot import PaperPerformanceSnapshot, build_snapshot


class SignalType(str, Enum):
    """Deterministic strategy signal mapped to paper order direction."""

    LONG_ENTRY = "long_entry"
    LONG_EXIT = "long_exit"
    SHORT_ENTRY = "short_entry"
    SHORT_EXIT = "short_exit"
    NO_ACTION = "no_action"


@dataclass
class RunnerEvent:
    """One auditable runner event (logged for the report)."""

    timestamp: pd.Timestamp
    bar_index: int
    event_type: str
    details: dict = field(default_factory=dict)


class PaperStrategyRunner:
    """Deterministic, paper-only strategy runner bound to one deployment.

    Hard safety: only accepts a ``PaperBroker`` instance. Any other broker is
    rejected (see ``DeploymentGate.assert_paper_broker``).
    """

    def __init__(
        self,
        deployment: PaperDeployment,
        broker: PaperBroker,
        spec: StrategySpec,
        *,
        health_monitor: Optional[PaperHealthMonitor] = None,
        risk_guard: Optional[PaperRiskGuard] = None,
        risk_manager: Optional["RiskManager"] = None,
        circuit_breaker: Optional[PaperCircuitBreaker] = None,
        event_log: Optional[PaperOperationsEventLog] = None,
    ) -> None:
        # Identity binding — verify the broker is exactly PaperBroker.
        if not isinstance(broker, PaperBroker):
            raise TypeError(
                "runner requires PaperBroker; got "
                f"{type(broker).__module__}.{type(broker).__name__}"
            )
        if deployment.strategy_spec_hash != _spec_identity(spec):
            raise ValueError(
                "deployment is bound to a different StrategySpec identity; "
                "create a new deployment for a modified spec"
            )

        self.deployment = deployment
        self.broker = broker
        self.spec = spec
        self.strategy = SpecStrategy(spec)
        self.config: PaperDeploymentConfig = deployment.config

        # Rolling bar window — causal: indicators are computed from this.
        self._window = pd.DataFrame()
        self._bar_count = 0
        self._events: list[RunnerEvent] = []
        self._rejected_orders = 0
        self._orders_submitted = 0
        self._fills_received = 0
        # Idempotency: dedupe by last processed bar timestamp.
        self._last_processed_bar: Optional[pd.Timestamp] = None
        self._position_state = 0  # +1 long, -1 short, 0 flat (mirrors target)

        # ---- Phase 19 operations layer (all optional, keyword-only) ----
        self._health_monitor = health_monitor
        self._risk_guard = risk_guard
        self._risk_manager = risk_manager
        self._circuit_breaker = circuit_breaker
        self._event_log = event_log

        # Operational metrics derived from the broker (single accounting system).
        self._starting_equity: Optional[float] = None
        self._peak_equity: Optional[float] = None
        self._max_drawdown: Optional[float] = 0.0
        self._consecutive_errors: int = 0
        self._generated_signals: int = 0
        self._last_signal: Optional[str] = None
        self._last_order: Optional[dict] = None
        self._last_fill: Optional[dict] = None
        self._started_at: Optional[str] = None
        self._health_status: str = "healthy"
        self._halt_reason: Optional[str] = None
        self._snapshots: list[PaperPerformanceSnapshot] = []

    # ------------------------------------------------------------------ #
    # Public bar interface
    # ------------------------------------------------------------------ #
    def process_bar(self, bar: Union[dict, pd.Series, pd.DataFrame]) -> SignalType:
        """Feed one bar to the runner. Returns the resulting signal/NO_ACTION.

        ``bar`` must contain open/high/low/close/volume (and an index value).
        Repeated calls with the SAME bar are no-ops (idempotency).
        """
        # Deployment must be in an order-accepting state.
        if self.deployment.status not in STATUS_ACCEPTS_ORDERS:
            return SignalType.NO_ACTION

        # Explicit circuit breaker: an OPEN circuit forces NO_ACTION.
        if self._circuit_breaker is not None and self._circuit_breaker.is_open:
            return SignalType.NO_ACTION

        bar_df, ts = _coerce_bar(bar)
        if self._last_processed_bar is not None and ts == self._last_processed_bar:
            # Same bar processed again — no-op (idempotency).
            return SignalType.NO_ACTION

        # Append to the rolling window.
        self._window = pd.concat([self._window, bar_df])
        self._bar_count += 1
        self._last_processed_bar = ts

        # Initialize operational baseline on the first processed bar.
        if self._starting_equity is None:
            self._starting_equity = float(self.broker.account().equity)
            self._peak_equity = self._starting_equity
            self._started_at = ts.isoformat()
            if self._event_log is not None:
                self._event_log.record(
                    PaperOperationEventType.DEPLOYMENT_ACTIVATED,
                    ts.isoformat(),
                    "deployment activated (first bar processed)",
                    {"bar_index": self._bar_count - 1},
                )

        # Warm-up gate: do not produce signals until enough bars have been
        # seen. ``bar_count`` is now 1-indexed (1 after the first bar).
        # We require ``bar_count > warmup_bars`` so that warmup_bars=20 lets
        # the 21st bar be the first eligible.
        if self._bar_count <= self.config.warmup_bars:
            self._events.append(RunnerEvent(
                timestamp=ts, bar_index=self._bar_count - 1,
                event_type="warmup", details={},
            ))
            self._record_bar_processed(ts)
            return SignalType.NO_ACTION

        # Compute target position series via the existing SpecStrategy
        # interpreter. The interpreter is causal: it only uses bars <= T.
        try:
            target_series = self.strategy.generate(self._window)
        except Exception as exc:
            self._events.append(RunnerEvent(
                timestamp=ts, bar_index=self._bar_count - 1,
                event_type="interpreter_error", details={"error": str(exc)},
            ))
            self._consecutive_errors += 1
            self._emit(
                PaperOperationEventType.ERROR,
                ts.isoformat(),
                "interpreter error",
                {"error": str(exc)},
            )
            self._finalize_bar(SignalType.NO_ACTION, ts, error=str(exc))
            return SignalType.NO_ACTION

        # The signal we act on is the LATEST bar in the window.
        new_target = int(target_series.iloc[-1])
        if new_target not in (-1, 0, 1):
            new_target = 0

        prev_state = self._position_state
        signal = self._map_target_transition(prev_state, new_target)
        self._last_signal = signal.value
        if signal != SignalType.NO_ACTION:
            self._generated_signals += 1

        fill_happened = False
        rejected = False
        if signal != SignalType.NO_ACTION:
            order = self._build_order(
                signal, ts, float(self._window["close"].iloc[-1])
            )
            if order is not None:
                try:
                    submitted = self.broker.submit_order(
                        symbol=self.deployment.symbol,
                        side=order["side"],
                        quantity=order["quantity"],
                        order_type=order["order_type"],
                        limit_price=order["limit_price"],
                        current_price=order["current_price"],
                    )
                    self._orders_submitted += 1
                    self._fills_received += sum(1 for f in submitted.fills)
                    if submitted.fills:
                        fill_happened = True
                        self._last_fill = {
                            "price": float(submitted.fills[-1].price),
                            "quantity": float(submitted.fills[-1].quantity),
                            "side": submitted.fills[-1].side.value,
                            "fee": float(submitted.fills[-1].fee),
                        }
                    self._last_order = {
                        "order_id": submitted.order_id,
                        "side": submitted.side.value,
                        "quantity": submitted.quantity,
                        "signal": signal.value,
                        "status": submitted.status.value,
                    }
                    self._events.append(RunnerEvent(
                        timestamp=ts, bar_index=self._bar_count - 1,
                        event_type="order_submitted",
                        details=dict(self._last_order),
                    ))
                    self._emit(
                        PaperOperationEventType.ORDER_SUBMITTED,
                        ts.isoformat(),
                        "order submitted",
                        dict(self._last_order),
                    )
                    if fill_happened:
                        self._emit(
                            PaperOperationEventType.ORDER_FILLED,
                            ts.isoformat(),
                            "order filled",
                            dict(self._last_fill),
                        )
                    # Update local position state from the actual broker book.
                    pos = self.broker.get_position(self.deployment.symbol)
                    self._position_state = (
                        int(pos.qty > 0) - int(pos.qty < 0) if pos is not None else 0
                    )
                except BrokerError as exc:
                    self._rejected_orders += 1
                    rejected = True
                    self._events.append(RunnerEvent(
                        timestamp=ts, bar_index=self._bar_count - 1,
                        event_type="order_rejected",
                        details={"signal": signal.value, "reason": str(exc)},
                    ))
                    self._emit(
                        PaperOperationEventType.ORDER_REJECTED,
                        ts.isoformat(),
                        "order rejected",
                        {"signal": signal.value, "reason": str(exc)},
                    )
        else:
            self._events.append(RunnerEvent(
                timestamp=ts, bar_index=self._bar_count - 1,
                event_type="signal_no_action",
                details={"prev_state": prev_state, "target": new_target},
            ))

        self._finalize_bar(
            signal, ts, fill_happened=fill_happened, rejected=rejected
        )

        return signal

    # ------------------------------------------------------------------ #
    # Signals engine integration (Phase 19+)
    # ------------------------------------------------------------------ #
    def process_signal_bar(
        self,
        bar: Union[dict, pd.Series, pd.DataFrame],
        *,
        market_view: Optional[object] = None,
        signal_config: Optional[object] = None,
    ) -> object:
        """Process one bar using the deterministic signals engine.

        This wires ``signals.generate_signal()`` into the paper broker pipeline.
        The ``market_view`` can be a ``MarketView`` instance or a dict; if ``None``
        a neutral view is constructed from the bar data (HOLD signal guaranteed).

        Returns a ``Signal`` object (see ``signals.Signal``) with direction,
        confidence, and reason — without modifying the runner's position state
        or broker book (call ``submit_order_from_signal()`` separately if desired).

        Lazy import avoids the circular dependency between ``signals`` and ``models``
        at module load time.
        """
        # Lazy import to avoid circular dependency with models package.
        from ..models.snapshot import MarketSnapshot, build_snapshot_from_df  # type: ignore
        from ..models.market_view import MarketView, MarketViewEnum  # type: ignore
        from ..signals import generate_signal as sig_gen, SignalConfig  # type: ignore

        bar_df, ts = _coerce_bar(bar)

        # Build snapshot from the bar data.
        try:
            snapshot = build_snapshot_from_df(
                bar_df,
                symbol=self.deployment.symbol,
                timeframe=self.config.timeframe or "1d",
                lookback_closes=self.config.lookback_bars or 60,
            )
        except Exception:
            # Minimal snapshot when full build fails (e.g. not enough history).
            snapshot = MarketSnapshot(
                symbol=self.deployment.symbol,
                timeframe=self.config.timeframe or "1d",
                timestamp=ts,
                last_bar_timestamp=ts,
                latest_price=max(float(bar_df["close"].iloc[-1]), 1e-6),
                data_points=max(self._bar_count, 1),
                data_start=ts,
                data_end=ts,
                lookahead_safe=True,
            )

        # Resolve market_view.
        if market_view is not None:
            if isinstance(market_view, MarketView):
                view = market_view
            elif isinstance(market_view, dict):
                view = MarketView(**market_view)
            else:
                view = MarketView(
                    symbol=self.deployment.symbol,
                    timeframe=self.config.timeframe or "1d",
                    market_view=MarketViewEnum.NEUTRAL,
                    confidence=0.5,
                    reasoning_summary="provided via market_view dict",
                    bullish_factors=[],
                    bearish_factors=[],
                    risks=[],
                    invalidating_conditions=[],
                    model="provided",
                )
        else:
            # Auto-view: simple rule-based view from price vs SMA20 and MACD if available.
            # This ensures a non-NEUTRAL view when indicators exist, otherwise NEUTRAL.
            close = float(bar_df["close"].iloc[-1])
            # Direct attribute access (no getattr) — snapshot may lack indicators
            # when there is insufficient history.
            try:
                sma20 = snapshot.sma_20
            except AttributeError:
                sma20 = None
            try:
                macd = snapshot.macd
            except AttributeError:
                macd = None
            try:
                macd_signal = snapshot.macd_signal
            except AttributeError:
                macd_signal = None
            if sma20 is not None and macd is not None and macd_signal is not None:
                if close > sma20 and macd > macd_signal:
                    view = MarketView(
                        symbol=self.deployment.symbol,
                        timeframe=self.config.timeframe or "1d",
                        market_view=MarketViewEnum.BULLISH,
                        confidence=0.7,
                        reasoning_summary="auto: price>SMA20 + MACD>signal",
                        bullish_factors=["price above SMA20", "MACD bullish crossover"],
                        bearish_factors=[],
                        risks=["trend dependence on SMA/MACD"],
                        invalidating_conditions=["price breaks below SMA20"],
                        model="auto-signals",
                    )
                elif close < sma20 and macd < macd_signal:
                    view = MarketView(
                        symbol=self.deployment.symbol,
                        timeframe=self.config.timeframe or "1d",
                        market_view=MarketViewEnum.BEARISH,
                        confidence=0.7,
                        reasoning_summary="auto: price<SMA20 + MACD<signal",
                        bullish_factors=[],
                        bearish_factors=["price below SMA20", "MACD bearish crossover"],
                        risks=["trend dependence on SMA/MACD"],
                        invalidating_conditions=["price breaks above SMA20"],
                        model="auto-signals",
                    )
                else:
                    view = MarketView(
                        symbol=self.deployment.symbol,
                        timeframe=self.config.timeframe or "1d",
                        market_view=MarketViewEnum.NEUTRAL,
                        confidence=0.5,
                        reasoning_summary="auto: indicators conflicting -> neutral",
                        bullish_factors=[],
                        bearish_factors=[],
                        risks=["indicator conflict"],
                        invalidating_conditions=[],
                        model="auto-signals",
                    )
            else:
                view = MarketView(
                    symbol=self.deployment.symbol,
                    timeframe=self.config.timeframe or "1d",
                    market_view=MarketViewEnum.NEUTRAL,
                    confidence=0.5,
                    reasoning_summary="auto: insufficient indicators -> neutral",
                    bullish_factors=[],
                    bearish_factors=[],
                    risks=["insufficient data"],
                    invalidating_conditions=[],
                    model="auto-signals",
                )

        # Resolve signal_config.
        if signal_config is not None:
            if isinstance(signal_config, SignalConfig):
                cfg = signal_config
            elif isinstance(signal_config, dict):
                cfg = SignalConfig(**signal_config)
            else:
                cfg = SignalConfig()
        else:
            cfg = SignalConfig()

        # Generate signal via the deterministic engine.
        signal = sig_gen(snapshot, view, config=cfg)
        return signal

    # ------------------------------------------------------------------ #
    # Signal mapping (unchanged from Phase 18)
    # ------------------------------------------------------------------ #
    def _map_target_transition(self, prev: int, target: int) -> SignalType:
        """Map (prev_state, new_target) -> deterministic signal."""
        # Flat -> long
        if prev == 0 and target == 1:
            return SignalType.LONG_ENTRY
        # Flat -> short (only if base config permits shorts)
        if prev == 0 and target == -1:
            if self.config.allow_short and self.spec.risk.allow_short:
                return SignalType.SHORT_ENTRY
            return SignalType.NO_ACTION
        # Long -> flat
        if prev == 1 and target == 0:
            return SignalType.LONG_EXIT
        # Short -> flat
        if prev == -1 and target == 0:
            return SignalType.SHORT_EXIT
        # Long -> short (flip)
        if prev == 1 and target == -1:
            # Documented flip: exit long first.
            return SignalType.LONG_EXIT
        # Short -> long (flip)
        if prev == -1 and target == 1:
            return SignalType.SHORT_EXIT  # always flatten long-side first
        # Already in target state → NO_ACTION (duplicate-entry protection).
        return SignalType.NO_ACTION

    # ------------------------------------------------------------------ #
    # Order construction + validation (unchanged from Phase 18)
    # ------------------------------------------------------------------ #
    def _build_order(
        self, signal: SignalType, ts: pd.Timestamp, price: float
    ) -> Optional[dict]:
        """Build + validate the order dict. Returns None on rejection."""
        if signal == SignalType.LONG_ENTRY:
            side = Side.BUY
        elif signal == SignalType.LONG_EXIT:
            side = Side.SELL
        elif signal == SignalType.SHORT_ENTRY:
            side = Side.SELL
        elif signal == SignalType.SHORT_EXIT:
            side = Side.BUY
        else:
            return None

        if not self.deployment.symbol:
            self._record_reject(signal, ts, reason="invalid_symbol")
            return None
        if price is None or not math.isfinite(price) or price <= 0:
            self._record_reject(signal, ts, reason="invalid_price")
            return None

        quantity = self._compute_quantity(price)
        if quantity is None:
            # Rejection already recorded inside _compute_quantity.
            return None

        # Disallow short orders when the base risk config forbids them.
        if signal in (SignalType.SHORT_ENTRY, SignalType.SHORT_EXIT) \
                and not self.config.allow_short:
            self._record_reject(signal, ts, reason="shorting_disabled")
            return None
        if signal == SignalType.SHORT_ENTRY and not self.spec.risk.allow_short:
            self._record_reject(signal, ts, reason="spec_shorting_disabled")
            return None

        return {
            "side": side,
            "quantity": quantity,
            "order_type": "MARKET",
            "limit_price": None,
            "current_price": float(price),
        }

    def _compute_quantity(self, price: float) -> Optional[float]:
        """Position sizing with risk + allocation caps."""
        try:
            equity = float(self.broker.account().equity)
        except Exception:
            equity = float(self.broker.initial_cash)

        if not math.isfinite(equity) or equity <= 0:
            self._record_reject(None, None, reason="invalid_equity")
            return None

        alloc = self.config.max_allocation_pct * equity
        if alloc <= 0 or price <= 0:
            return None
        quantity = alloc / price
        if self.config.max_position_size is not None:
            quantity = min(quantity, float(self.config.max_position_size))
        if not math.isfinite(quantity) or quantity <= 0:
            self._record_reject(None, None, reason="invalid_quantity")
            return None
        return float(quantity)

    def _record_reject(
        self,
        signal: Optional[SignalType],
        ts: Optional[pd.Timestamp],
        reason: str,
    ) -> None:
        self._rejected_orders += 1
        self._events.append(RunnerEvent(
            timestamp=ts if ts is not None else pd.Timestamp.utcnow(),
            bar_index=self._bar_count - 1,
            event_type="order_rejected",
            details={"signal": signal.value if signal else None, "reason": reason},
        ))
        self._emit(
            PaperOperationEventType.ORDER_REJECTED,
            ts.isoformat() if ts is not None else None,
            "order rejected",
            {"signal": signal.value if signal else None, "reason": reason},
        )

    # ------------------------------------------------------------------ #
    # Phase 19 operations helpers
    # ------------------------------------------------------------------ #
    def _emit(
        self,
        event_type: PaperOperationEventType,
        timestamp: Optional[str],
        message: str,
        payload: dict,
    ) -> None:
        """Record an operations event if an event log is configured."""
        if self._event_log is None:
            return
        self._event_log.record(
            event_type,
            timestamp if timestamp is not None else "",
            message,
            payload,
        )

    def _record_bar_processed(self, ts: pd.Timestamp) -> None:
        """Record a BAR_PROCESSED event during warmup (no signal)."""
        if self._event_log is None:
            return
        self._event_log.record(
            PaperOperationEventType.BAR_PROCESSED,
            ts.isoformat(),
            "bar processed (warmup)",
            {"bar_index": self._bar_count - 1, "warmup": True},
        )

    def _finalize_bar(
        self,
        signal: SignalType,
        ts: pd.Timestamp,
        *,
        fill_happened: bool,
        rejected: bool,
        error: Optional[str] = None,
    ) -> None:
        """Update operational metrics, health, risk, snapshots, events."""
        account = self.broker.account()
        position = self.broker.get_position(self.deployment.symbol)

        # Error tracking.
        if error is not None:
            self._consecutive_errors += 1
        else:
            self._consecutive_errors = 0

        # Running max drawdown from the broker's equity (single source).
        equity = float(account.equity)
        if self._peak_equity is None or equity > self._peak_equity:
            self._peak_equity = equity
        if self._peak_equity and self._peak_equity > 0:
            dd = (equity - self._peak_equity) / self._peak_equity
            self._max_drawdown = min(self._max_drawdown or 0.0, dd)

        # Record signal/bar events.
        self._emit(
            PaperOperationEventType.BAR_PROCESSED,
            ts.isoformat(),
            "bar processed",
            {
                "bar_index": self._bar_count - 1,
                "signal": signal.value,
                "equity": equity,
            },
        )
        if signal != SignalType.NO_ACTION:
            self._emit(
                PaperOperationEventType.SIGNAL_GENERATED,
                ts.isoformat(),
                "signal generated",
                {"signal": signal.value},
            )

        # Health evaluation.
        if self._health_monitor is not None:
            health = self._health_monitor.evaluate(
                deployment_status=self.deployment.status,
                processed_bars=self._bar_count,
                filled_orders=self._fills_received,
                rejected_orders=self._rejected_orders,
                consecutive_errors=self._consecutive_errors,
                max_drawdown=self._max_drawdown,
                equity=equity,
                position=position,
            )
            self._health_status = health.status.value
            self._halt_reason = health.halt_reason
            for w in health.warnings:
                self._emit(
                    PaperOperationEventType.HEALTH_WARNING, ts.isoformat(), w, {}
                )

        # Risk evaluation.
        risk_halt: Optional[str] = None
        if self._risk_guard is not None:
            decision, reason = self._risk_guard.check(
                max_drawdown=self._max_drawdown,
                equity=equity,
                position=position,
                rejected_orders=self._rejected_orders,
                consecutive_errors=self._consecutive_errors,
            )
            if decision.value == "halt":
                risk_halt = reason

        # System-level risk manager evaluation (Phase 19+).
        if self._risk_manager is not None:
            decisions = self._risk_manager.to_paper_decisions(
                equity=equity,
                max_drawdown=self._max_drawdown,
                position=position,
                rejected_orders=self._rejected_orders,
                consecutive_errors=self._consecutive_errors,
                n_concurrent_positions=len(self.broker.positions()) if self.broker else 0,
            )
            # Merge decisions: if any check halts, risk_halt is set.
            for key, decision in decisions.items():
                if decision == "halt" and key in ("max_drawdown", "exposure", "rejected_orders", "consecutive_errors"):
                    if risk_halt is None:
                        risk_halt = f"risk_manager:{key}"
                    else:
                        risk_halt = f"{risk_halt}+{key}"

        # Circuit breaker integration: halt conditions trip the breaker.
        if self._circuit_breaker is not None:
            if risk_halt is not None:
                self._circuit_breaker.trip(risk_halt)
                self._emit(
                    PaperOperationEventType.HALTED,
                    ts.isoformat(),
                    "circuit breaker tripped (risk)",
                    {"reason": risk_halt},
                )
            if self._health_monitor is not None and self._halt_reason is not None:
                self._circuit_breaker.trip(self._halt_reason)
                self._emit(
                    PaperOperationEventType.HALTED,
                    ts.isoformat(),
                    "circuit breaker tripped (health)",
                    {"reason": self._halt_reason},
                )

        # Performance snapshot.
        trade_count, win_rate, profit_factor = _trade_stats(
            self.broker, self.deployment.symbol
        )
        snapshot = build_snapshot(
            deployment_id=self.deployment.deployment_id,
            strategy_id=self.deployment.strategy_id,
            timestamp=ts.isoformat(),
            account=account,
            position=position,
            starting_equity=self._starting_equity,
            max_drawdown=self._max_drawdown,
            trade_count=trade_count,
            win_rate=win_rate,
            profit_factor=profit_factor,
            health_status=self._health_status,
        )
        self._snapshots.append(snapshot)

    # ------------------------------------------------------------------ #
    # Accessors (used by report + tests)
    # ------------------------------------------------------------------ #
    @property
    def events(self) -> list[RunnerEvent]:
        return list(self._events)

    @property
    def orders_submitted(self) -> int:
        return self._orders_submitted

    @property
    def rejected_orders(self) -> int:
        return self._rejected_orders

    @property
    def fills_received(self) -> int:
        return self._fills_received

    @property
    def bar_count(self) -> int:
        return self._bar_count

    @property
    def position_state(self) -> int:
        return self._position_state

    # ---- Phase 19 accessors ----
    @property
    def generated_signals(self) -> int:
        return self._generated_signals

    @property
    def max_drawdown(self) -> Optional[float]:
        return self._max_drawdown

    @property
    def health_status(self) -> str:
        return self._health_status

    @property
    def halt_reason(self) -> Optional[str]:
        return self._halt_reason

    @property
    def consecutive_errors(self) -> int:
        return self._consecutive_errors

    @property
    def circuit_breaker(self) -> Optional[PaperCircuitBreaker]:
        return self._circuit_breaker

    @property
    def event_log(self) -> Optional[PaperOperationsEventLog]:
        return self._event_log

    @property
    def snapshots(self) -> list[PaperPerformanceSnapshot]:
        return list(self._snapshots)

    def operations_state(self) -> PaperOperationsState:
        """Build the current operational state snapshot."""
        account = self.broker.account()
        position = self.broker.get_position(self.deployment.symbol)
        return PaperOperationsState(
            deployment_id=self.deployment.deployment_id,
            strategy_id=self.deployment.strategy_id,
            status=self.deployment.status.value,
            started_at=self._started_at,
            last_bar_timestamp=(
                self._last_processed_bar.isoformat()
                if self._last_processed_bar is not None
                else None
            ),
            processed_bars=self._bar_count,
            generated_signals=self._generated_signals,
            submitted_orders=self._orders_submitted,
            filled_orders=self._fills_received,
            rejected_orders=self._rejected_orders,
            current_equity=float(account.equity),
            starting_equity=self._starting_equity,
            realized_pnl=float(account.realized_pnl),
            unrealized_pnl=float(account.unrealized_pnl),
            max_drawdown=self._max_drawdown,
            current_position=position_dict(position),
            last_signal=self._last_signal,
            last_order=self._last_order,
            last_fill=self._last_fill,
            health_status=self._health_status,
            halt_reason=self._halt_reason,
            consecutive_errors=self._consecutive_errors,
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _coerce_bar(bar) -> tuple[pd.DataFrame, pd.Timestamp]:
    """Normalize a bar input (dict / Series / 1-row DataFrame) to a DataFrame
    with a single index and the required OHLCV columns.
    """
    required = {"open", "high", "low", "close", "volume"}
    if isinstance(bar, dict):
        if not required.issubset(bar.keys()):
            missing = sorted(required - bar.keys())
            raise ValueError(f"bar missing required keys: {missing}")
        ts = pd.Timestamp(bar.get("timestamp") or bar.get("ts") or pd.Timestamp.utcnow())
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        df = pd.DataFrame([{k: bar[k] for k in required}], index=[ts])
        return df, ts
    if isinstance(bar, pd.Series):
        if not required.issubset(bar.index):
            missing = sorted(required - set(bar.index))
            raise ValueError(f"bar missing required keys: {missing}")
        ts = bar.name if bar.name is not None else pd.Timestamp.utcnow()
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        df = pd.DataFrame([bar[list(required)].to_dict()], index=[ts])
        return df, ts
    if isinstance(bar, pd.DataFrame):
        if len(bar) == 0:
            raise ValueError("empty bar DataFrame")
        if not required.issubset(bar.columns):
            missing = sorted(required - set(bar.columns))
            raise ValueError(f"bar missing required columns: {missing}")
        ts = pd.Timestamp(bar.index[-1])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return bar.iloc[[-1]].copy(), ts
    raise TypeError(f"unsupported bar type: {type(bar).__name__}")


def _spec_identity(spec: StrategySpec) -> str:
    from ..research.evidence import strategy_identity as _sid
    return _sid(spec)


def _trade_stats(
    broker: PaperBroker, symbol: str
) -> tuple[int, Optional[float], Optional[float]]:
    """Reconstruct round-trip trades from the broker's fill ledger.

    Returns (trade_count, win_rate, profit_factor). Win rate and profit factor
    are ``None`` when there are no completed round-trip trades. The broker
    remains the source of truth for cash/equity; this is reporting-only.
    """
    fills: list = []
    for order in broker._orders.values():
        fills.extend(order.fills)
    trades: list[dict] = []
    open_legs: list[dict] = []
    for f in fills:
        if f.symbol != symbol:
            continue
        if f.side.value == "BUY":
            open_legs.append({
                "price": float(f.price),
                "qty": float(f.quantity),
                "fee": float(f.fee),
            })
        else:
            remaining = float(f.quantity)
            close_price = float(f.price)
            close_fee = float(f.fee)
            while remaining > 0 and open_legs:
                leg = open_legs[0]
                leg_qty = min(remaining, leg["qty"])
                gross = (close_price - leg["price"]) * leg_qty
                leg_cost = leg["fee"] * (leg_qty / leg["qty"])
                trades.append({
                    "net": gross - leg_cost - close_fee * (leg_qty / f.quantity),
                })
                leg["qty"] -= leg_qty
                remaining -= leg_qty
                if leg["qty"] <= 1e-12:
                    open_legs.pop(0)
    n = len(trades)
    if n == 0:
        return 0, None, None
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    win_rate = len(wins) / n
    gross_win = sum(t["net"] for t in wins)
    gross_loss = -sum(t["net"] for t in losses)
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    else:
        profit_factor = None
    return n, win_rate, profit_factor
