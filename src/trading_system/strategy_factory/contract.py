"""Canonical Strategy Factory contract.

This is the point-in-time strategy evaluation contract. It is intentionally
distinct from (and composes with) the batch backtest contract:

  * ``research.strategies.Strategy``   -- batch interface;
    ``generate(df) -> Series`` (one target per bar over a full historical
    frame; used by the existing backtester and the paper runner).
  * ``strategy_factory.Strategy``      -- point-in-time interface;
    ``evaluate(state) -> StrategySignal`` (one decision for the latest closed
    bar; used by the paper trader per-bar loop and the future orchestrator).

Both share the same indicator engine (``trading_system.indicators``) and the
same signal-direction convention (``Signal.LONG / SHORT / FLAT`` +1/0/-1).

A strategy NEVER reaches the broker directly. ``evaluate`` returns a
``StrategySignal``; the Risk/Execution layer is the sole component that may
translate a signal into orders.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..research.strategies import Signal as SignalDirection
from ..research.strategy_lab.spec import assert_no_code_payload
from .metadata import StrategyMetadata
from .parameters import ParameterSchema

_REQUIRED_BARS_COLUMNS = {"open", "high", "low", "close", "volume"}


class SignalAction(str, Enum):
    """Canonical strategy decision for the latest closed bar.

    BUY  -- open/extend a LONG position.
    SELL -- open/extend a SHORT position (only when the strategy allows shorting).
    EXIT -- flatten any open position (long or short).
    HOLD -- no change to the current position.
    """

    BUY = "buy"
    SELL = "sell"
    EXIT = "exit"
    HOLD = "hold"


@dataclass
class PositionState:
    """Snapshot of an existing position carried into ``evaluate``.

    ``side`` follows the existing research convention: +1 long, -1 short, 0 flat
    (see ``trading_system.research.strategies.Signal``).
    """

    symbol: str
    side: int = 0
    size: float = 0.0
    entry_price: Optional[float] = None

    def is_flat(self) -> bool:
        return self.side == 0

    def is_long(self) -> bool:
        return self.side > 0

    def is_short(self) -> bool:
        return self.side < 0


@dataclass
class MarketState:
    """Look-ahead-safe, point-in-time market context for a single symbol.

    ``bars`` must contain ONLY closed (historical) bars whose index is a
    timezone-aware ``DatetimeIndex``. ``timestamp`` -- the decision point -- must
    equal the last bar timestamp; this invariant is enforced so that no strategy
    can evaluate against an unclosed or future bar.
    """

    symbol: str
    timeframe: str
    timestamp: datetime
    bars: pd.DataFrame
    position: Optional[PositionState] = None
    lookahead_safe: bool = True

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("MarketState.timestamp must be timezone-aware (UTC)")
        idx = self.bars.index
        if not isinstance(idx, pd.DatetimeIndex):
            raise ValueError("MarketState.bars must be indexed by a timezone-aware DatetimeIndex")
        if idx.tz is None:
            raise ValueError("MarketState.bars index must be timezone-aware (UTC)")
        if self.timestamp != idx[-1]:
            raise ValueError(
                "MarketState.timestamp must equal the last bar timestamp "
                "(no future/unclosed-bar data allowed)"
            )
        if not _REQUIRED_BARS_COLUMNS.issubset(set(self.bars.columns)):
            raise ValueError("MarketState.bars missing required columns "
                             f"{_REQUIRED_BARS_COLUMNS}")
        if not self.lookahead_safe:
            raise ValueError("MarketState must be marked look-ahead safe")

    @property
    def latest_close(self) -> float:
        return float(self.bars["close"].iloc[-1])

    @property
    def close_series(self) -> pd.Series:
        return self.bars["close"]

    @property
    def high_series(self) -> pd.Series:
        return self.bars["high"]

    @property
    def low_series(self) -> pd.Series:
        return self.bars["low"]

    @property
    def volume_series(self) -> pd.Series:
        return self.bars["volume"]

    @property
    def bar_count(self) -> int:
        return int(len(self.bars))

    def current_position_side(self) -> int:
        if self.position is None:
            return 0
        return int(self.position.side)


@dataclass(frozen=True)
class StrategySignal:
    """Deterministic, broker-agnostic decision emitted by a strategy.

    Pure data: contains NO reference to a broker, paper-trading engine, or
    network. It is the contract between a strategy and the Risk/Execution
    layer. ``to_dict`` yields a JSON-serializable representation.
    """

    action: SignalAction
    strategy_id: str
    timestamp: datetime
    symbol: str
    reference_price: float
    confidence: float = 0.0
    reason: str = ""
    target_position: Optional[int] = None
    metadata: dict = field(default_factory=dict)
    version: str = ""

    def __post_init__(self) -> None:
        if self.reference_price <= 0:
            raise ValueError("reference_price must be > 0")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence must be in [0.0, 1.0]")
        if self.timestamp.tzinfo is None:
            raise ValueError("signal timestamp must be timezone-aware (UTC)")
        if self.target_position is not None and self.target_position not in (-1, 0, 1):
            raise ValueError("target_position must be in {-1, 0, 1}")
        if not self.action:
            raise ValueError("action is required")
        if not self.strategy_id:
            raise ValueError("strategy_id is required")
        if not self.symbol:
            raise ValueError("symbol is required")
        assert_no_code_payload(self.reason, "reason") if self.reason else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "strategy_id": self.strategy_id,
            "timestamp": self.timestamp.isoformat(),
            "symbol": self.symbol,
            "reference_price": self.reference_price,
            "confidence": self.confidence,
            "reason": self.reason,
            "target_position": self.target_position,
            "metadata": self.metadata,
            "version": self.version,
        }

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"StrategySignal(strategy_id={self.strategy_id!r}, "
            f"action={self.action.value}, target_position={self.target_position})"
        )


class Strategy(ABC):
    """Canonical Strategy Factory contract.

    A strategy is a deterministic function of (static definition + parameters)
    applied to a look-ahead-safe ``MarketState``, producing a single
    ``StrategySignal`` for the latest closed bar.

    Subclasses MUST be deterministic: identical inputs must always produce an
    identical signal. Strategies MUST NOT touch brokers, databases, networks,
    or the filesystem as a side effect of ``evaluate``.
    """

    @property
    @abstractmethod
    def metadata(self) -> StrategyMetadata:
        """Static, serializable description of the strategy definition."""

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """Concrete parameter values bound to this instance (validated)."""

    @abstractmethod
    def evaluate(self, state: MarketState) -> StrategySignal:
        """Produce a single, deterministic signal for the latest closed bar."""

    def __call__(self, state: MarketState) -> StrategySignal:
        return self.evaluate(state)


__all__ = [
    "SignalAction",
    "PositionState",
    "MarketState",
    "StrategySignal",
    "Strategy",
    "SignalDirection",
]
