"""Reference strategy: RSI Mean Reversion (Strategy Factory Phase 1 fixture).

Deterministic, causal, point-in-time. Reuses ``trading_system.indicators.rsi``
(never re-implements indicator math). Emits no orders and performs no I/O.

LONG when RSI is below the oversold threshold (mean-reversion bounce);
optionally SHORT when above the overbought threshold; EXIT/flat otherwise.
Reversals are signalled as EXIT first (two-step), so a single signal never
combines closing an existing position with opening a new direction.
"""
from __future__ import annotations

from typing import Any

from ...indicators import rsi
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
            name="rsi_period",
            type=ParameterType.INTEGER,
            default=14,
            description="RSI lookback window.",
            minimum=2,
            maximum=100,
            step=1,
        ),
        ParameterDefinition(
            name="oversold",
            type=ParameterType.FLOAT,
            default=30.0,
            description="RSI level below which to go long.",
            minimum=0.0,
            maximum=100.0,
            step=1.0,
        ),
        ParameterDefinition(
            name="overbought",
            type=ParameterType.FLOAT,
            default=70.0,
            description="RSI level above which to go short.",
            minimum=0.0,
            maximum=100.0,
            step=1.0,
        ),
        ParameterDefinition(
            name="allow_short",
            type=ParameterType.BOOLEAN,
            default=False,
            description="Emit SELL (short) signals when RSI is overbought.",
        ),
    ]
)

_METADATA = StrategyMetadata(
    strategy_id="rsi_mean_reversion",
    name="RSI Mean Reversion",
    version="1.0.0",
    family=StrategyFamily.MEAN_REVERSION,
    description=(
        "Long when RSI is below the oversold threshold; short (optional) when "
        "above overbought. Deterministic and causal: indicators use only closed bars."
    ),
    timeframes=["1m", "5m", "15m", "30m", "1h", "4h", "1d"],
    supported_instruments=["*"],
    required_data=["ohlcv"],
    required_indicators=["rsi"],
    parameter_schema=_PARAMETER_SCHEMA,
    author="strategy-factory",
    tags=["mean_reversion", "rsi", "reference", "deterministic"],
    long_short_support=True,
    intraday=False,
    requires_volume=False,
    requires_ohlcv=True,
    minimum_history=14,
)


@register_strategy
class RSIMeanReversionStrategy(Strategy):
    """Mean-reversion strategy driven by RSI thresholds."""

    metadata = _METADATA

    def __init__(
        self,
        rsi_period: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
        allow_short: bool = False,
    ) -> None:
        values = _PARAMETER_SCHEMA.validate_values(
            {
                "rsi_period": rsi_period,
                "oversold": oversold,
                "overbought": overbought,
                "allow_short": allow_short,
            }
        )
        self._period = int(values["rsi_period"])
        self._oversold = float(values["oversold"])
        self._overbought = float(values["overbought"])
        self._allow_short = bool(values["allow_short"])
        if self._oversold >= self._overbought:
            raise ValueError(
                f"oversold ({self._oversold}) must be < overbought ({self._overbought})"
            )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "rsi_period": self._period,
            "oversold": self._oversold,
            "overbought": self._overbought,
            "allow_short": self._allow_short,
        }

    @staticmethod
    def _transition(current: int, desired: int) -> tuple[SignalAction, float, int]:
        if desired == 1:
            if current < 0:
                return SignalAction.EXIT, 0.5, 0
            if current > 0:
                return SignalAction.HOLD, 1.0, 1
            return SignalAction.BUY, 1.0, 1
        if desired == -1:
            if current > 0:
                return SignalAction.EXIT, 0.5, 0
            if current < 0:
                return SignalAction.HOLD, 1.0, -1
            return SignalAction.SELL, 1.0, -1
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
        if n < self._period:
            return self._hold_signal(state, f"insufficient history: need {self._period} bars, have {n}")

        rsi_series = rsi(close, self._period)
        rsi_val = rsi_series.iloc[-1]
        if rsi_val != rsi_val:
            return self._hold_signal(state, "RSI undefined (warming up)")

        rsi_val = float(rsi_val)
        if rsi_val < self._oversold:
            desired = 1
            direction = f"long (RSI {rsi_val:.2f} < oversold {self._oversold})"
        elif rsi_val > self._overbought:
            desired = -1 if self._allow_short else 0
            direction = (
                f"short (RSI {rsi_val:.2f} > overbought {self._overbought})"
                if self._allow_short
                else f"flat (RSI {rsi_val:.2f} > overbought {self._overbought})"
            )
        else:
            desired = 0
            direction = f"flat (RSI {rsi_val:.2f} in neutral zone)"

        current = state.current_position_side()
        action, confidence, target = self._transition(current, desired)
        return StrategySignal(
            action=action,
            strategy_id=self.metadata.strategy_id,
            timestamp=state.timestamp,
            symbol=state.symbol,
            reference_price=state.latest_close,
            confidence=confidence,
            reason=f"{direction}; rsi={rsi_val:.6f}",
            target_position=target,
            metadata={
                "rsi": rsi_val,
                "oversold": self._oversold,
                "overbought": self._overbought,
                "allow_short": self._allow_short,
                "content_hash": self.metadata.content_hash(),
            },
            version=self.metadata.version,
        )
