"""In-memory Strategy Registry for the Strategy Factory (Phase 1).

Assembly/discovery layer: holds validated strategy instances keyed by their
canonical identity ``strategy_id@version``. Lightweight and non-persistent --
persistence lives in the existing research module
``trading_system.research.strategy_registry``.

Rules:
  * ``require_valid_strategy`` runs on every registration (malformed strategies
    can never enter).
  * A second registration of the SAME identity raises
    ``DuplicateStrategyError`` (never silent overwrite). Use ``overwrite=True``
    to replace, or bump the version to register a new parametrization.
  * Each entry records a content hash for reproducibility audits.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Optional

from .contract import Strategy
from .exceptions import DuplicateStrategyError
from .metadata import StrategyReference, StrategyVersion
from .validation import require_valid_strategy


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _RegistryEntry:
    __slots__ = ("strategy", "content_hash", "registered_at", "reference")

    def __init__(self, strategy: Strategy, content_hash: str, reference: str) -> None:
        self.strategy = strategy
        self.content_hash = content_hash
        self.registered_at = _utcnow_iso()
        self.reference = reference


class StrategyRegistry:
    """Canonical in-memory registry for Strategy Factory strategies."""

    def __init__(self) -> None:
        self._entries: dict[str, _RegistryEntry] = {}

    @staticmethod
    def _identity(strategy: Strategy) -> StrategyReference:
        return strategy.metadata.reference

    @staticmethod
    def _content_hash(strategy: Strategy) -> str:
        payload = {
            "metadata": strategy.metadata.to_dict(),
            "parameters": strategy.parameters,
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:64]

    def register(self, strategy: Strategy, *, overwrite: bool = False) -> Strategy:
        """Validate and register a strategy instance.

        Raises ``StrategyValidationError`` for invalid strategies and
        ``DuplicateStrategyError`` when the identity already exists (unless
        ``overwrite=True``).
        """
        require_valid_strategy(strategy)
        ref = str(self._identity(strategy))
        existing = self._entries.get(ref)
        if existing is not None and not overwrite:
            if existing.content_hash == self._content_hash(strategy):
                raise DuplicateStrategyError(
                    f"strategy {ref!r} is already registered identically; "
                    "registration is explicit -- use overwrite=True to replace, "
                    "or bump the version"
                )
            raise DuplicateStrategyError(
                f"strategy {ref} already registered with a different definition; "
                "bump the version (or pass overwrite=True to replace)"
            )
        self._entries[ref] = _RegistryEntry(strategy, self._content_hash(strategy), ref)
        return strategy

    def get(self, strategy_id: str, version: Optional[str] = None) -> Strategy:
        """Retrieve a strategy by id (and optional version).

        When ``version`` is omitted and multiple versions exist, the highest
        semantic version is returned.
        """
        matched = self._match(strategy_id, version)
        if matched is None:
            raise KeyError(
                f"no registered strategy {strategy_id!r}"
                + (f"@{version}" if version else "")
            )
        return matched

    def get_by_reference(self, reference: str) -> Strategy:
        entry = self._entries.get(reference)
        if entry is None:
            raise KeyError(f"unknown strategy reference {reference!r}")
        return entry.strategy

    def exists(self, strategy_id: str, version: Optional[str] = None) -> bool:
        return self._match_entry(strategy_id, version) is not None

    def list(self, **filters) -> list[Strategy]:
        """Return registered strategies, optionally filtered by metadata.

        Supported filters: family, timeframe, instrument, tag.
        """
        family = filters.get("family")
        timeframe = filters.get("timeframe")
        instrument = filters.get("instrument")
        tag = filters.get("tag")

        results: list[Strategy] = []
        for entry in self._entries.values():
            meta = entry.strategy.metadata
            if family is not None and meta.family.value != _coerce(family):
                continue
            if timeframe is not None and timeframe not in meta.timeframes:
                continue
            if instrument is not None:
                supported = meta.supported_instruments
                if "*" not in supported and instrument not in supported:
                    continue
            if tag is not None and tag not in meta.tags:
                continue
            results.append(entry.strategy)
        return results

    def identities(self) -> list[str]:
        return sorted(self._entries.keys())

    def content_hash(self, reference: str) -> str:
        entry = self._entries.get(reference)
        if entry is None:
            raise KeyError(f"unknown strategy reference {reference!r}")
        return entry.content_hash

    def registered_at(self, reference: str) -> str:
        entry = self._entries.get(reference)
        if entry is None:
            raise KeyError(f"unknown strategy reference {reference!r}")
        return entry.registered_at

    def unregister(self, strategy_id: str, version: Optional[str] = None) -> None:
        entry = self._match_entry(strategy_id, version)
        if entry is None:
            raise KeyError(
                f"no registered strategy {strategy_id!r}"
                + (f"@{version}" if version else "")
            )
        del self._entries[entry.reference]

    def clear(self) -> None:
        self._entries.clear()

    @property
    def count(self) -> int:
        return len(self._entries)

    def _match_entry(self, strategy_id: str, version: Optional[str]) -> Optional[_RegistryEntry]:
        if version is None:
            candidates = [
                entry for ref, entry in self._entries.items()
                if ref.split("@", 1)[0] == strategy_id
            ]
            if not candidates:
                return None
            return max(
                candidates,
                key=lambda e: StrategyVersion.parse(e.reference.split("@", 1)[1]).tuple,
            )
        ref = f"{strategy_id}@{version}"
        return self._entries.get(ref)

    def _match(self, strategy_id: str, version: Optional[str]) -> Optional[Strategy]:
        entry = self._match_entry(strategy_id, version)
        return entry.strategy if entry is not None else None


def _coerce(value) -> str:
    if hasattr(value, "value"):
        return value.value
    return str(value)


__all__ = [
    "StrategyRegistry",
    "DuplicateStrategyError",
]
