"""Bridge: register DB-persisted StrategySpecs into the Strategy Factory
discovery catalog so the autonomous scheduler can resolve them by ID during
tick execution.

Composition contract
--------------------
The scheduler (_build_control_center, line ~718) calls this module after the
PaperTradingControlCenter is built. The bridge:

1. Reads every ``StrategySpec`` persisted in the research registry
   (``center.registry`` → ``research.strategy_registry.StrategyRegistry``).
2. Wraps each spec in a lightweight factory ``Strategy`` subclass whose
   ``evaluate(MarketState) -> StrategySignal`` delegates to the deterministic
   ``SpecStrategy.generate`` interpreter (Phase 13).
3. Calls ``strategy_factory.discovery.register_strategy`` so the class becomes
   addressable via ``build_from_discovery(strategy_id)`` / ``get_strategy_class``.
"""

from __future__ import annotations

from typing import Any, List, Optional

from trading_system.research.strategy_lab.interpreter import SpecStrategy
from trading_system.research.strategy_registry import StrategyRegistry as ResearchRegistry
from trading_system.research.strategy_lab.spec import StrategySpec

from .contract import (
    MarketState,
    SignalAction,
    Strategy,
    StrategySignal,
)
from .discovery import register_strategy, registered_strategy_ids
from .metadata import StrategyFamily, StrategyMetadata, StrategyVersion
from .parameters import ParameterDefinition, ParameterSchema, ParameterType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_family(spec: StrategySpec) -> StrategyFamily:
    name = (spec.spec_name or "").lower()
    if any(w in name for w in ("trend", "ema", "ma", "sma")):
        return StrategyFamily.TREND
    if any(w in name for w in ("mean", "reversion", "rsi", "oscillator")):
        return StrategyFamily.MEAN_REVERSION
    if any(w in name for w in ("breakout", "donchian", "nbar", "channel")):
        return StrategyFamily.BREAKOUT
    if any(w in name for w in ("vwap", "volume", "flow")):
        return StrategyFamily.VOLUME
    if any(w in name for w in ("momentum", "macd", "roc")):
        return StrategyFamily.MOMENTUM
    return StrategyFamily.TREND


def _spec_params_to_schema(spec: StrategySpec) -> ParameterSchema:
    params: list[ParameterDefinition] = []
    for key, val in (spec.parameters or {}).items():
        params.append(ParameterDefinition(
            name=key,
            type=ParameterType.FLOAT,
            default=float(val) if isinstance(val, (int, float)) else 0.0,
            description=f"Auto-bridged parameter from {spec.spec_name}",
        ))
    return ParameterSchema(parameters=params)


def _spec_to_metadata(spec: StrategySpec) -> StrategyMetadata:
    return StrategyMetadata(
        strategy_id=spec.strategy_id or f"spec_{spec.spec_name}",
        name=spec.name or spec.spec_name or "DB Strategy",
        version=spec.version or "1.0.0",
        family=_infer_family(spec),
        description=spec.description or f"Auto-bridged spec: {spec.spec_name}",
        timeframes=[spec.timeframe] if spec.timeframe else ["1d"],
        supported_instruments=[spec.symbol] if spec.symbol else ["*"],
        required_data=["ohlcv"],
        required_indicators=[],
        parameter_schema=_spec_params_to_schema(spec),
        author="db-bridge",
        tags=["db-bridged", "spec"],
        long_short_support=spec.allow_short if hasattr(spec, "allow_short") else False,
        intraday=False,
        requires_volume=True,
        requires_ohlcv=True,
        minimum_history=20,
    )


# ---------------------------------------------------------------------------
# Per-spec factory Strategy subclass (created once per strategy_id, cached)
# ---------------------------------------------------------------------------

_STREAM_CACHE: dict[str, type[Strategy]] = {}


def _make_factory_strategy_class(spec: StrategySpec) -> type[Strategy]:
    """Return (and cache) a factory ``Strategy`` subclass wrapping *spec*."""
    sid = spec.strategy_id or f"spec_{spec.spec_name}"
    if sid in _STREAM_CACHE:
        return _STREAM_CACHE[sid]

    metadata = _spec_to_metadata(spec)

    class _SpecFactoryStrategy(Strategy):
        """Deterministic factory Strategy wrapping a persisted StrategySpec.

        ``evaluate`` runs the spec through the Phase-13 interpreter
        (``SpecStrategy.generate``) restricted to the closed bars in *state*,
        then translates the resulting target position (+1/0/-1) into a
        ``StrategySignal``.
        """

        metadata = metadata

        def __init__(self, **params: Any) -> None:
            # Ignore caller overrides; the spec is the source of truth.
            self._spec = spec
            self._research = SpecStrategy(spec)

        def evaluate(self, state: MarketState) -> StrategySignal:
            try:
                df = state.bars
                if len(df) < 2:
                    return self._hold(state, "insufficient bars")

                target_series = self._research.generate(df)
                if len(target_series) == 0:
                    return self._hold(state, "no generated signals")

                target = int(target_series.iloc[-1])
                current = state.current_position_side()

                if target > current:
                    action = SignalAction.BUY
                elif target < current:
                    action = SignalAction.SELL
                elif target == 0 and current != 0:
                    action = SignalAction.EXIT
                else:
                    action = SignalAction.HOLD

                return StrategySignal(
                    action=action,
                    strategy_id=metadata.strategy_id,
                    timestamp=state.timestamp,
                    symbol=state.symbol,
                    reference_price=state.latest_close,
                    confidence=0.5 if target != 0 else 0.0,
                    reason=f"Spec[{spec.spec_name}] target={target}",
                    target_position=target,
                    version=metadata.version,
                )
            except Exception as exc:  # noqa: BLE001
                return self._hold(state, f"spec evaluation error: {exc}")

        def _hold(self, state: MarketState, reason: str) -> StrategySignal:
            return StrategySignal(
                action=SignalAction.HOLD,
                strategy_id=metadata.strategy_id,
                timestamp=state.timestamp,
                symbol=state.symbol,
                reference_price=state.latest_close,
                confidence=0.0,
                reason=reason,
                target_position=0,
                version=metadata.version,
            )

    _STREAM_CACHE[sid] = _SpecFactoryStrategy
    return _SpecFactoryStrategy


# ---------------------------------------------------------------------------
# Public bridge API
# ---------------------------------------------------------------------------

def register_db_spec_strategies(center: Any) -> List[str]:
    """Register every DB-persisted ``StrategySpec`` into the factory catalog.

    Parameters
    ----------
    center:
        A ``PaperTradingControlCenter`` (or anything exposing
        ``.registry`` as a ``research.strategy_registry.StrategyRegistry``).

    Returns
    -------
    list[str]
        Sorted strategy IDs that were registered (or were already present).
    """
    registered: list[str] = []

    try:
        research_registry: ResearchRegistry = center.registry  # type: ignore[assignment]
    except AttributeError:
        return registered

    try:
        strategies = research_registry.list_strategies()
    except Exception:  # noqa: BLE001
        return registered

    for strategy in strategies:
        try:
            spec: Optional[StrategySpec] = getattr(strategy, "spec", None)
        except Exception:  # noqa: BLE001
            spec = None

        if spec is None:
            continue

        strategy_id = spec.strategy_id or f"spec_{spec.spec_name}"
        if strategy_id in registered_strategy_ids():
            registered.append(strategy_id)
            continue

        try:
            factory_cls = _make_factory_strategy_class(spec)
            register_strategy(factory_cls)
            registered.append(strategy_id)
        except Exception:  # noqa: BLE001
            continue

    return sorted(set(registered))
