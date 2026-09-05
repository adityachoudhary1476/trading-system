"""Strategy runtime wrapper for the Strategy Factory (Phase 4).

``StrategyRuntime`` is a thin, additive boundary between the static Strategy
Factory contract (``Strategy``, ``MarketState``, ``StrategySignal``) and the
runtime evaluation loop. It is NOT a broker, paper-trading engine, or market-data
client -- it performs no I/O, no network, no ordering. It is a deterministic
adapter that:

  * binds a concrete ``Strategy`` instance to a ``MarketContext`` (capabilities
    check at construction);
  * enforces the minimum-history gate before evaluation;
  * delegates ``evaluate`` to the underlying strategy;
  * surfaces structured errors (insufficient history, incompatibility).

The factory function ``create_strategy_runtime`` is the single construction entry
point: it resolves a strategy by id/version from a ``Catalog``, validates its
configuration, and returns a ready-to-evaluate ``StrategyRuntime``.
"""
from __future__ import annotations

from typing import Any, Optional, Union

from .capability import CompatibilityReport, MarketContext, capabilities_from, is_compatible
from .contract import MarketState, Strategy, StrategySignal
from .exceptions import InsufficientHistoryError, StrategyFactoryError
from .registry import StrategyRegistry


class StrategyRuntime:
    """Runtime evaluation boundary for a single ``Strategy`` + ``MarketContext``.

    A runtime is constructed with a validated strategy instance and a market
    context. The compatibility check runs at construction time so that
    incompatibility is a construction-time error, not an evaluation-time surprise.

    The runtime never mutates the underlying strategy; ``evaluate`` returns a new
    ``StrategySignal`` for the latest closed bar.
    """

    __slots__ = ("_strategy", "_context", "_capabilities", "_compat")

    def __init__(
        self,
        strategy: Strategy,
        context: Union[MarketContext, dict[str, Any]],
        *,
        strict_compatibility: bool = True,
    ) -> None:
        if not isinstance(strategy, Strategy):
            raise TypeError("strategy must be a Strategy Factory Strategy instance")

        if isinstance(context, dict):
            ctx = MarketContext(**context)
        elif isinstance(context, MarketContext):
            ctx = context
        else:
            raise TypeError("context must be a MarketContext or a dict of its fields")

        caps = capabilities_from(strategy)
        report = is_compatible(strategy, ctx)

        if strict_compatibility and not report.compatible:
            raise StrategyFactoryError(
                f"strategy {strategy.metadata.reference} is not compatible with "
                f"context {ctx.instrument}@{ctx.timeframe}: {report.reasons}"
            )

        self._strategy = strategy
        self._context = ctx
        self._capabilities = caps
        self._compat = report

    @property
    def strategy(self) -> Strategy:
        """The underlying strategy instance (read-only)."""
        return self._strategy

    @property
    def context(self) -> MarketContext:
        """The market context this runtime is bound to."""
        return self._context

    @property
    def capabilities(self):
        """The validated ``Capabilities`` derived from this runtime's strategy."""
        return self._capabilities

    @property
    def compatibility_report(self) -> CompatibilityReport:
        """The cached compatibility report from construction."""
        return self._compat

    @property
    def strategy_id(self) -> str:
        """Canonical strategy id (shorthand for ``strategy.metadata.strategy_id``)."""
        return self._strategy.metadata.strategy_id

    @property
    def strategy_version(self) -> str:
        """Strategy version string."""
        return self._strategy.metadata.version

    @property
    def minimum_bars(self) -> int:
        """Minimum history bars required by the bound strategy."""
        return self._capabilities.minimum_bars

    def evaluate(self, state: MarketState) -> StrategySignal:
        """Evaluate the underlying strategy against ``state``.

        Enforces the minimum-history gate defined by the strategy's capabilities
        before delegating to ``strategy.evaluate``. Raises
        ``InsufficientHistoryError`` when the bar count is too low.
        """
        if state.bar_count < self._capabilities.minimum_bars:
            raise InsufficientHistoryError(
                f"strategy {self._strategy.metadata.reference} requires "
                f">= {self._capabilities.minimum_bars} bars "
                f"(state has {state.bar_count} for {state.symbol}@{state.timeframe})"
            )
        return self._strategy.evaluate(state)

    def __call__(self, state: MarketState) -> StrategySignal:
        """Alias for ``evaluate`` -- enables ``runtime(state)`` syntax."""
        return self.evaluate(state)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"StrategyRuntime(strategy_id={self._strategy.metadata.strategy_id!r}, "
            f"version={self._strategy.metadata.version!r}, "
            f"context={self._context.instrument!r}@{self._context.timeframe!r}, "
            f"compatible={self._compat.compatible})"
        )


def create_strategy_runtime(
    strategy_id: str,
    context: Union[MarketContext, dict[str, Any]],
    *,
    version: Optional[str] = None,
    parameters: Optional[dict[str, Any]] = None,
    registry: Optional[Union[StrategyRegistry, Any]] = None,
    strict_compatibility: bool = True,
) -> StrategyRuntime:
    """Construct a ``StrategyRuntime`` by resolving a strategy from a catalog/registry.

    Resolution order:
      1. If ``registry`` is a ``StrategyRegistry``, look up via ``registry.get``.
      2. If ``registry`` is a ``Catalog``, delegate to ``catalog.get``.
      3. If ``registry`` is ``None``, a fresh empty ``StrategyRegistry`` is used
         (will raise ``KeyError`` unless a pre-registered instance is supplied).

    Parameters:
      strategy_id:      Canonical strategy id (e.g. ``"ema_crossover"``).
      context:           ``MarketContext`` or dict accepted by ``MarketContext``.
      version:           Optional semantic version; highest registered when omitted.
      parameters:        Optional parameter overrides validated against the
                         strategy's parameter schema.
      registry:          A ``StrategyRegistry`` or ``Catalog`` to resolve from.
      strict_compatibility: When True (default), incompatibility raises at
                         construction. When False, the runtime is still created
                         (compatibility report available via the property).
    """
    from .catalog import Catalog

    if registry is None:
        reg = StrategyRegistry()
    elif isinstance(registry, StrategyRegistry):
        reg = registry
    elif isinstance(registry, Catalog):
        return StrategyRuntime(
            registry.get(strategy_id, version),
            context,
            strict_compatibility=strict_compatibility,
        )
    else:
        reg = registry

    strategy = reg.get(strategy_id, version)

    if parameters:
        validated = strategy.metadata.parameter_schema.validate_values(parameters)
        strategy = type(strategy)(**validated)

    return StrategyRuntime(
        strategy,
        context,
        strict_compatibility=strict_compatibility,
    )


__all__ = [
    "StrategyRuntime",
    "create_strategy_runtime",
]
