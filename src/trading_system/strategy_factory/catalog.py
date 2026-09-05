"""Read-only catalog facade over a ``StrategyRegistry``.

Maintains the architectural separation described in the contract:

  * Discovery       (``discovery.py``) -- "what strategies exist?" (classes).
  * Catalog/Registry  (``StrategyRegistry``; this facade) -- "what versions and
    contracts exist?" (instances), plus read-only introspection.
  * Compatibility     (``capability.is_compatible``) -- "which strategies can run
    in this context?".
  * Selection          intentionally NOT implemented here (no ranking/optimizer).

This facade is read-only: it never mutates the underlying registry. Registration
and discovery lifecycle remain the responsibility of ``StrategyRegistry`` and
``discovery`` respectively.
"""
from __future__ import annotations

from typing import Any, Optional, Union

from .capability import MarketContext, capabilities_from, is_compatible
from .contract import Strategy
from .fingerprint import configuration_fingerprint, contract_schema, strategy_identity
from .metadata import StrategyMetadata, StrategyVersion
from .registry import StrategyRegistry

CATALOG_SCHEMA_VERSION = "1.0"


class Catalog:
    """Read-only introspection over a registered strategy instance set."""

    def __init__(self, registry: Optional[StrategyRegistry] = None) -> None:
        self._registry: StrategyRegistry = registry if registry is not None else StrategyRegistry()

    @property
    def registry(self) -> StrategyRegistry:
        return self._registry

    def list(self) -> list[Strategy]:
        return self._registry.list()

    def get(self, strategy_id: str, version: Optional[str] = None) -> Strategy:
        return self._registry.get(strategy_id, version)

    def exists(self, strategy_id: str, version: Optional[str] = None) -> bool:
        return self._registry.exists(strategy_id, version)

    def versions(self, strategy_id: str) -> list[str]:
        """All versions registered for ``strategy_id`` in ascending semver order."""
        out: list[str] = []
        for ref in self._registry.identities():
            sid, _, ver = ref.rpartition("@")
            if sid == strategy_id:
                out.append(ver)
        return sorted(out, key=lambda v: StrategyVersion.parse(v).tuple)

    def metadata(self, strategy_id: str, version: Optional[str] = None) -> StrategyMetadata:
        return self.get(strategy_id, version).metadata

    def compatible(self, market_context: Union[MarketContext, dict]) -> list[Strategy]:
        """Return registered strategies compatible with ``market_context``."""
        if isinstance(market_context, MarketContext):
            ctx = market_context
        elif isinstance(market_context, dict):
            ctx = MarketContext(**market_context)
        else:
            raise TypeError("market_context must be a MarketContext or a dict of its fields")
        return [s for s in self.list() if is_compatible(s, ctx).compatible]

    def describe(self) -> dict[str, Any]:
        """Deterministic, JSON-serializable catalog manifest (read-only).

        Aggregated, stable introspection of every registered strategy suitable
        for downstream consumers (paper orchestrator, frontend, audit logs). The
        returned dict contains no Python object references, memory addresses,
        filesystem paths, or secrets, and strategies are sorted by reference
        (``strategy_id@version``) so output is stable across processes.
        """
        entries = sorted(
            self.list(),
            key=lambda s: str(s.metadata.reference),
        )
        strategies = [
            {
                "identity": strategy_identity(s),
                "reference": str(s.metadata.reference),
                "fingerprint": configuration_fingerprint(s),
                "capabilities": capabilities_from(s.metadata).to_dict(),
                "contract": contract_schema(s),
            }
            for s in entries
        ]
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "count": len(strategies),
            "strategies": strategies,
        }

    @property
    def count(self) -> int:
        return self._registry.count


__all__ = [
    "Catalog",
    "CATALOG_SCHEMA_VERSION",
]