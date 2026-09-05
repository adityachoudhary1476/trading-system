"""Strategy discovery for the Strategy Factory.

Discovery mechanism: a ``Strategy`` subclass is registered into a process-local
catalog by applying the ``@register_strategy`` decorator (or by calling
``register_strategy(cls)`` programmatically). ``discover`` imports a
caller-supplied list of module names and returns how many *new* classes were
registered.

This keeps the autonomous orchestrator decoupled: adding a strategy means writing
a new module and applying the decorator -- no engine edit required. Discovery
only imports modules whose names the caller provides; it does not scan, exec,
or evaluate arbitrary source.

The decorator is idempotent: re-registering the identical class is a no-op so a
module may be safely re-imported. A *different* class claiming an existing id
raises ``DuplicateStrategyError`` (use ``overwrite=True`` to replace).
"""
from __future__ import annotations

import importlib
import sys
from typing import Optional

from .contract import Strategy
from .exceptions import DuplicateStrategyError, StrategyFactoryError
from .metadata import StrategyMetadata

_DISCOVERED: dict[str, type[Strategy]] = {}


class DiscoveryError(StrategyFactoryError):
    """Raised when a discovery import fails."""


def register_strategy(cls=None, *, strategy_id: Optional[str] = None, overwrite: bool = False):
    """Class decorator registering a strategy class into the discovery catalog.

    The class MUST expose a class-level ``metadata`` attribute that is a
    ``StrategyMetadata`` instance.
    """
    def _decorator(target: type[Strategy]) -> type[Strategy]:
        sid = strategy_id
        if sid is None:
            try:
                meta = target.metadata
            except AttributeError:
                meta = None
            if not isinstance(meta, StrategyMetadata):
                raise StrategyFactoryError(
                    f"cannot register {target.__name__}: it must expose a class-level "
                    "'metadata' (StrategyMetadata) attribute"
                )
            sid = meta.strategy_id
        existing = _DISCOVERED.get(sid)
        if existing is not None:
            if existing is target:
                return target
            if not overwrite:
                raise DuplicateStrategyError(
                    f"strategy id {sid!r} already discovered on "
                    f"{existing.__module__}.{existing.__name__}; use overwrite=True "
                    "to replace"
                )
        _DISCOVERED[sid] = target
        return target

    if cls is None:
        return _decorator
    return _decorator(cls)


def registered_strategy_ids() -> list[str]:
    """Sorted list of all discovered strategy ids."""
    return sorted(_DISCOVERED)


def get_strategy_class(strategy_id: str) -> type[Strategy]:
    """Look up a discovered strategy class by id."""
    try:
        return _DISCOVERED[strategy_id]
    except KeyError:
        raise KeyError(
            f"no discovered strategy {strategy_id!r}; "
            f"available: {registered_strategy_ids()}"
        )


def discover(modules: list[str], *, reload: bool = False) -> int:
    """Import each module name; return the count of newly-registered classes.

    ``reload=True`` forces re-execution of already-imported modules (use with
    ``clear_discovery`` in tests to re-trigger decorators). In normal usage
    ``reload`` is False and modules are imported once.
    """
    newly_registered = 0
    for modname in modules:
        before = len(_DISCOVERED)
        try:
            module = importlib.import_module(modname)
            if reload and modname in sys.modules:
                module = importlib.reload(sys.modules[modname])
        except Exception as exc:
            raise DiscoveryError(
                f"discovery failed importing {modname!r}: {exc}"
            ) from exc
        newly_registered += len(_DISCOVERED) - before
    return newly_registered


def build_from_discovery(strategy_id: str, **params) -> Strategy:
    """Instantiate a discovered strategy class and return the instance."""
    cls = get_strategy_class(strategy_id)
    try:
        meta = cls.metadata
    except AttributeError:
        meta = None
    if isinstance(meta, StrategyMetadata) and not meta.parameter_schema.is_empty():
        values = meta.parameter_schema.validate_values(params)
    else:
        values = dict(params)
    return cls(**values)


def clear_discovery() -> None:
    """Remove all discovered classes (intended for test isolation)."""
    _DISCOVERED.clear()


def discovery_count() -> int:
    return len(_DISCOVERED)


__all__ = [
    "register_strategy",
    "registered_strategy_ids",
    "get_strategy_class",
    "discover",
    "build_from_discovery",
    "clear_discovery",
    "discovery_count",
    "DiscoveryError",
]
