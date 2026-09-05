"""Strategy Factory -- canonical, extensible strategy architecture (Phase 1).

This package defines the Strategy Factory contract that sits above the existing
research/backtest stack and feeds the paper-trading orchestrator and the future
AI strategy-generation pipeline.

The canonical architecture implemented here is::

    Strategy Metadata (StrategyMetadata, StrategyFamily, StrategyVersion)
        -> Strategy Parameters (ParameterSchema / ParameterDefinition)
        -> Strategy Contract  (Strategy.evaluate -> StrategySignal)
        -> Strategy Registry   (in-memory, duplicate-rejected, filtered discovery)
        -> Strategy Validation (validate_strategy / require_valid_strategy)
        -> Capabilities       (typed data/indicator/timeframe/history requirements)
        -> Compatibility      (pure, side-effect-free capability match)
        -> Fingerprint         (deterministic configuration identity)
        -> Catalog            (read-only introspection facade)

It COMPOSES with (not replaces) the existing stack:
  * indicator math      -> trading_system.indicators (reused, not duplicated)
  * signal directions    -> trading_system.research.strategies.Signal (+1/0/-1)
  * declarative spec     -> trading_system.research.strategy_lab.StrategySpec
  * persistence registry -> trading_system.research.strategy_registry.StrategyRegistry

A strategy evaluates a look-ahead-safe ``MarketState`` and returns a pure
``StrategySignal`` (NO broker, NO database, NO network, NO HTTP).
"""
from __future__ import annotations

from .catalog import Catalog, CATALOG_SCHEMA_VERSION
from .capability import (
    Capabilities,
    CompatibilityReport,
    DataRequirement,
    MarketContext,
    capabilities_from,
    is_compatible,
)
from .contract import (
    MarketState,
    PositionState,
    SignalAction,
    Strategy,
    StrategySignal,
)
from .discovery import (
    DiscoveryError,
    build_from_discovery,
    clear_discovery,
    discover,
    get_strategy_class,
    registered_strategy_ids,
    register_strategy,
)
from .exceptions import (
    DuplicateStrategyError,
    InsufficientHistoryError,
    StrategyFactoryError,
    StrategyValidationError,
)
from .fingerprint import configuration_fingerprint, contract_schema, strategy_identity
from .runtime import StrategyRuntime, create_strategy_runtime
from .metadata import (
    StrategyFamily,
    StrategyMetadata,
    StrategyReference,
    StrategyVersion,
)
from .parameters import (
    ParameterDefinition,
    ParameterSchema,
    ParameterType,
)
from .registry import StrategyRegistry
from .validation import (
    require_valid_metadata,
    require_valid_signal,
    require_valid_strategy,
    validate_metadata,
    validate_parameter_schema,
    validate_parameter_values,
    validate_signal,
    validate_strategy,
)

__version__ = "0.4.0"

__all__ = [
    "MarketState",
    "PositionState",
    "SignalAction",
    "Strategy",
    "StrategySignal",
    "StrategyFamily",
    "StrategyMetadata",
    "StrategyReference",
    "StrategyVersion",
    "ParameterDefinition",
    "ParameterSchema",
    "ParameterType",
    "StrategyRegistry",
    "Catalog",
    "CATALOG_SCHEMA_VERSION",
    "Capabilities",
    "DataRequirement",
    "MarketContext",
    "CompatibilityReport",
    "is_compatible",
    "capabilities_from",
    "strategy_identity",
    "configuration_fingerprint",
    "contract_schema",
    "StrategyRuntime",
    "create_strategy_runtime",
    "register_strategy",
    "registered_strategy_ids",
    "get_strategy_class",
    "discover",
    "build_from_discovery",
    "clear_discovery",
    "DiscoveryError",
    "DuplicateStrategyError",
    "InsufficientHistoryError",
    "StrategyFactoryError",
    "StrategyValidationError",
    "validate_metadata",
    "validate_parameter_schema",
    "validate_parameter_values",
    "validate_signal",
    "validate_strategy",
    "require_valid_strategy",
    "require_valid_metadata",
    "require_valid_signal",
    "__version__",
]