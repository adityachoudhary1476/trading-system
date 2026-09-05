"""Reference strategy: EMA Crossover (Strategy Factory Phase 1 fixture).

Deterministic, causal, point-in-time. Reuses ``trading_system.indicators.ema``
(never re-implements indicator math). Produces a single ``StrategySignal`` for
the latest closed bar. Emits no orders and performs no I/O.

LONG when fast EMA is above slow EMA; SHORT (optional) on the reverse cross.
Reversals are signalled as EXIT first (two-step), so a single signal never
combines closing an existing position with opening a new direction.
"""
from __future__ import annotations

from typing import Any

from ...indicators import ema
from ..contract import (
    MarketState,
    SignalAction,
    Strategy,
    StrategySignal,
)
from ..metadata import StrategyFamily, StrategyMetadata
from ..discovery import register_strategy
from ..parameters import ParameterDefinition, ParameterSchema, ParameterType

_PARAMETER_SCHEMA = ParameterSchema(
    parameters=[
        ParameterDefinition(
            name="fast_period",
            type=ParameterType.INTEGER,
            default=12,
            description="Span of the fast EMA.",
            minimum=2,
            maximum=500,
            step=1,
        ),
        ParameterDefinition(
            name="slow_period",
            type=ParameterType.INTEGER,
            default=26,
            description="Span of the slow EMA (must exceed fast_period).",
            minimum=2,
            maximum=500,
            step=1,
        ),
        ParameterDefinition(
            name="allow_short",
            type=ParameterType.BOOLEAN,
            default=False,
            description="Emit SELL (short) signals on a bearish cross.",
        ),
    ]
)

_METADATA = StrategyMetadata(
    strategy_id="ema_crossover",
    name="EMA Crossover",
    version="1.0.0",
    family=StrategyFamily.TREND,
    description=(
        "Long when the fast EMA is above the slow EMA; short (optional) on the "
        "reverse cross. Deterministic and causal: indicators use only closed bars."
    ),
    timeframes=["1m", "5m", "15m", "30m", "1h", "4h", "1d"],
    supported_instruments=["*"],
    required_data=["ohlcv"],
    required_indicators=["ema"],
    parameter_schema=_PARAMETER_SCHEMA,
    author="strategy-factory",
    tags=["trend", "ema", "reference", "deterministic"],
    long_short_support=True,
    intraday=False,
    requires_volume=False,
    requires_ohlcv=True,
    minimum_history=26,
)


@register_strategy
class EMACrossoverStrategy(Strategy):
    """Trend-following EMA crossover."""

    metadata = _METADATA

    def __init__(self, fast_period: int = 12, slow_period: int = 26, allow_short: bool = False) -> None:
        values = _PARAMETER_SCHEMA.validate_values(
            {"fast_period": fast_period, "slow_period": slow_period, "allow_short": allow_short}
        )
        self._fast = int(values["fast_period"])
        self._slow = int(values["slow_period"])
        self._allow_short = bool(values["allow_short"])
        if self._fast >= self._slow:
            raise ValueError(
                f"fast_period ({self._fast}) must be < slow_period ({self._slow})"
            )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "fast_period": self._fast,
            "slow_period": self._slow,
            "allow_short": self._allow_short,
        }

    def _desired(self, fast: float, slow: float) -> int:
        if fast > slow:
            return 1
        if fast < slow:
            return -1 if self._allow_short else 0
        return 0

    @staticmethod
    def _transition(current: int, desired: int, strength: float) -> tuple[SignalAction, float, int]:
        if desired == 1:
            if current < 0:
                return SignalAction.EXIT, round(0.5 + 0.5 * strength, 6), 0
            if current > 0:
                return SignalAction.HOLD, round(0.5 + 0.5 * strength, 6), 1
            return SignalAction.BUY, round(0.5 + 0.5 * strength, 6), 1
        if desired == -1:
            if current > 0:
                return SignalAction.EXIT, round(0.5 + 0.5 * strength, 6), 0
            if current < 0:
                return SignalAction.HOLD, round(0.5 + 0.5 * strength, 6), -1
            return SignalAction.SELL, round(0.5 + 0.5 * strength, 6), -1
        if current != 0:
            return SignalAction.EXIT, 0.5, 0
        return SignalAction.HOLD, 0.0, 0

    def _hold_signal(self, state: MarketState, reason: str) -> StrategySignal:
        return StrategySignal(
            action=SignalAction.HOLD,
            strategy_id=self.metadata.strategy_id,
            timestamp=state.timestamp,
            symbol=state.symbol,
            reference_price=state.latest_close,
            confidence=0.0,
            reason=reason,
            target_position=0,
            version=self.metadata.version,
        )

    def evaluate(self, state: MarketState) -> StrategySignal:
        close = state.close_series
        n = len(close)
        need = max(self._fast, self._slow)
        if n < need:
            return self._hold_signal(state, f"insufficient history: need {need} bars, have {n}")

        fast_val = float(ema(close, self._fast).iloc[-1])
        slow_val = float(ema(close, self._slow).iloc[-1])
        desired = self._desired(fast_val, slow_val)
        current = state.current_position_side()
        strength = min(abs(fast_val - slow_val) / max(slow_val, 1e-12), 1.0)

        action, confidence, target = self._transition(current, desired, strength)
        direction = (
            "long (fast EMA > slow EMA)" if desired == 1
            else "short (fast EMA < slow EMA)" if desired == -1
            else "flat (fast EMA <= slow EMA)"
        )
        return StrategySignal(
            action=action,
            strategy_id=self.metadata.strategy_id,
            timestamp=state.timestamp,
            symbol=state.symbol,
            reference_price=state.latest_close,
            confidence=confidence,
            reason=f"{direction}; fast={fast_val:.6f} slow={slow_val:.6f}",
            target_position=target,
            metadata={
                "fast_ema": fast_val,
                "slow_ema": slow_val,
                "allow_short": self._allow_short,
                "content_hash": self.metadata.content_hash(),
            },
            version=self.metadata.version,
        )
