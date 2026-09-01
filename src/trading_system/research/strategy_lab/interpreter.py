"""Deterministic StrategySpec interpreter (Phase 13, Step 4).

Compiles a validated StrategySpec into a ``SpecStrategy`` — a subclass of the
EXISTING research ``Strategy`` ABC — so AI-proposed strategies run through the
unchanged deterministic backtester. No backtester modification, no code
execution, no look-ahead.

Semantics (documented contract):
  * The spec's ``entry`` condition produces LONG entries, ``entry_short``
    produces SHORT entries, ``exit`` flattens any open position.
  * The generated series is a TARGET POSITION STATE per bar (+1 / 0 / -1),
    exactly what the existing backtester expects. The backtester acts on the
    NEXT bar's open, so no look-ahead is introduced here.
  * A position is HELD until the exit condition (or an opposing entry, which
    flips the state) fires. This mirrors how the existing EMATrendStrategy
    expresses state.
  * NaN / warmup values make a condition FALSE (never True), so entries cannot
    fire before indicators are defined.
  * Short entries require both ``risk.allow_short`` (spec) and a non-empty
    ``entry_short``; the interpreter additionally refuses to emit -1 when the
    spec forbids shorts (the backtester's RiskConfig gates it again).

All indicator values come from ``trading_system.indicators`` (pure, causal).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..strategies import Signal, Strategy, StrategyMeta
from ...indicators import atr as _atr, bollinger_bands, ema as _ema, macd as _macd
from ...indicators import momentum as _momentum, rsi as _rsi, sma as _sma
from .dsl import (
    Comparison,
    ComparisonOp,
    LogicNode,
    NotNode,
    Operand,
    OperandKind,
    PriceField,
)
from .spec import StrategySpec


class InterpreterError(ValueError):
    """Raised when a spec cannot be interpreted (never silently ignored)."""


_REQUIRED_COLUMNS = {"open", "high", "low", "close", "volume"}


def _series_of(operand: Operand, fields: dict, indicators: dict):
    """Resolve an operand to a pandas Series (bar-aligned) or a float constant."""
    if operand.kind == OperandKind.CONSTANT:
        return float(operand.constant)
    if operand.kind == OperandKind.FIELD:
        return fields[operand.field]
    return indicators[operand.indicator]


def _as_bool_series(mask, index: pd.Index) -> pd.Series:
    if isinstance(mask, bool):
        return pd.Series(mask, index=index)
    return mask.fillna(False).astype(bool)


def evaluate_condition(condition, fields: dict, indicators: dict) -> pd.Series:
    """Deterministically evaluate a DSL condition tree over one bar index.

    Every comparison is element-wise over the SAME index; NaN in any operand
    makes the comparison False. Crossovers use shift(1) — strictly causal.
    """
    index = fields[PriceField.CLOSE].index
    if isinstance(condition, Comparison):
        left = _series_of(condition.left, fields, indicators)
        right = _series_of(condition.right, fields, indicators)
        op = condition.op
        if op == ComparisonOp.GT:
            mask = left > right
        elif op == ComparisonOp.LT:
            mask = left < right
        elif op == ComparisonOp.GTE:
            mask = left >= right
        elif op == ComparisonOp.LTE:
            mask = left <= right
        elif op == ComparisonOp.EQ:
            mask = left == right
        elif op == ComparisonOp.CROSSES_ABOVE:
            # left was at/below right on the prior bar and is above now.
            # A constant right side is materialized as a constant series
            # (a threshold does not change between bars).
            if isinstance(right, float):
                right = pd.Series(right, index=index)
            mask = (left > right) & (left.shift(1) <= right.shift(1))
        elif op == ComparisonOp.CROSSES_BELOW:
            if isinstance(right, float):
                right = pd.Series(right, index=index)
            mask = (left < right) & (left.shift(1) >= right.shift(1))
        else:  # pragma: no cover - enum-guarded
            raise InterpreterError(f"unsupported operator {op!r}")
        return _as_bool_series(mask, index)
    if isinstance(condition, LogicNode):
        subs = [evaluate_condition(c, fields, indicators) for c in condition.conditions]
        if condition.op.value == "AND":
            out = subs[0]
            for s in subs[1:]:
                out = out & s
        else:
            out = subs[0]
            for s in subs[1:]:
                out = out | s
        return _as_bool_series(out, index)
    if isinstance(condition, NotNode):
        return ~evaluate_condition(condition.condition, fields, indicators)
    raise InterpreterError(f"unsupported condition node {type(condition).__name__}")


def compute_indicators(spec: StrategySpec, df: pd.DataFrame) -> dict:
    """Compute every declared indicator once, via the EXISTING indicator module."""
    close = df["close"]
    out: dict = {}
    for ind in spec.indicators:
        name, p = ind.name.value, ind.params
        if name == "sma":
            out[ind.key] = _sma(close, int(p["window"]))
        elif name == "ema":
            out[ind.key] = _ema(close, int(p["window"]))
        elif name == "rsi":
            out[ind.key] = _rsi(close, int(p["window"]))
        elif name == "atr":
            out[ind.key] = _atr(df["high"], df["low"], close, int(p["window"]))
        elif name == "momentum":
            out[ind.key] = _momentum(close, int(p["window"]))
        elif name in ("macd", "macd_signal", "macd_histogram"):
            frame = _macd(close, int(p["fast"]), int(p["slow"]), int(p["signal"]))
            column = {"macd": "macd", "macd_signal": "signal",
                      "macd_histogram": "histogram"}[name]
            out[ind.key] = frame[column]
        elif name.startswith("bb_"):
            frame = bollinger_bands(close, int(p["window"]), float(p["num_std"]))
            column = {"bb_upper": "upper", "bb_middle": "middle",
                      "bb_lower": "lower"}[name]
            out[ind.key] = frame[column]
        else:  # pragma: no cover - registry-guarded
            raise InterpreterError(f"indicator {name!r} has no interpreter mapping")
    return out


class SpecStrategy(Strategy):
    """Adapter: interprets a validated StrategySpec for the existing backtester.

    Deterministic: the same spec + same df always produce the same target
    series. Only historical/current data at each timestep is used.
    """

    def __init__(self, spec: StrategySpec) -> None:
        self.spec = spec
        self.meta = StrategyMeta(
            spec.name,
            spec.description or f"Spec strategy generated by {spec.generated_by}",
        )

    @property
    def params(self) -> dict:
        return self.spec.model_dump(mode="json")

    def generate(self, df: pd.DataFrame) -> pd.Series:
        if df is None or len(df) == 0:
            raise InterpreterError("cannot interpret spec on an empty DataFrame")
        missing = _REQUIRED_COLUMNS - set(df.columns)
        if missing:
            raise InterpreterError(
                f"DataFrame is missing required column(s): {sorted(missing)}"
            )
        fields = {
            PriceField.OPEN: df["open"],
            PriceField.HIGH: df["high"],
            PriceField.LOW: df["low"],
            PriceField.CLOSE: df["close"],
            PriceField.VOLUME: df["volume"],
        }
        indicators = compute_indicators(self.spec, df)

        index = df.index
        allow_long = self.spec.allow_long
        allow_short = self.spec.risk.allow_short and self.spec.entry_short is not None

        long_entry = evaluate_condition(self.spec.entry, fields, indicators)
        short_entry = (
            evaluate_condition(self.spec.entry_short, fields, indicators)
            if self.spec.entry_short is not None
            else pd.Series(False, index=index)
        )
        exit_mask = (
            evaluate_condition(self.spec.exit, fields, indicators)
            if self.spec.exit is not None
            else pd.Series(False, index=index)
        )

        # Deterministic position-state loop over bars (uses ONLY bar-T info).
        values = np.zeros(len(index), dtype=int)
        state = 0
        le = long_entry.to_numpy()
        se = short_entry.to_numpy()
        ex = exit_mask.to_numpy()
        for i in range(len(index)):
            if state == 0:
                if allow_long and le[i]:
                    state = Signal.LONG
                elif allow_short and se[i]:
                    state = Signal.SHORT
            elif state == Signal.LONG:
                if ex[i]:
                    state = Signal.FLAT
                elif allow_short and se[i]:
                    state = Signal.SHORT  # documented flip via opposing entry
            elif state == Signal.SHORT:
                if ex[i]:
                    state = Signal.FLAT
                elif allow_long and le[i]:
                    state = Signal.LONG
            values[i] = state
        return pd.Series(values, index=index, dtype=int)


def build_strategy(spec: StrategySpec) -> SpecStrategy:
    """Factory: StrategySpec -> existing Strategy ABC instance.

    The spec must already be a parsed (validated) StrategySpec object; semantic
    dataset checks are the caller's responsibility (validation.require_valid).
    """
    if not isinstance(spec, StrategySpec):
        raise InterpreterError("build_strategy requires a StrategySpec instance")
    return SpecStrategy(spec)

