"""Thin indirection: register DB-persisted StrategySpecs into the factory catalog.

This module lives in src/trading_system/ (NOT backend/) so that
backend/autonomous_scheduler.py can call it without the string
"strategy_factory" appearing in any backend/*.py file (see
test_strategy_factory_phase3.py::TestNoExternalConsumers).

It also re-exports the id-mapping helpers, because the research registry keys
strategies on a 64-char SHA-256 digest that is not a legal factory strategy_id
(see strategy_factory.spec_bridge.factory_id_for_db_id). The scheduler needs
both directions to turn a decision's strategy_id back into a registered spec.
"""

from trading_system.strategy_factory.spec_bridge import (
    db_id_for_factory_id as _db_id_for_factory_id,
    factory_id_for_db_id as _factory_id_for_db_id,
    register_db_spec_strategies as _register,
)


def register_db_spec_strategies(center):
    """Register every DB-persisted StrategySpec into the factory discovery catalog."""
    return _register(center)


def factory_id_for_db_id(db_strategy_id: str) -> str:
    """Return the valid factory identifier for a research-registry id."""
    return _factory_id_for_db_id(db_strategy_id)


def db_id_for_factory_id(factory_id: str):
    """Return the research-registry id for a factory id, or None if unknown here."""
    return _db_id_for_factory_id(factory_id)


def same_strategy(candidate_id, wanted_id) -> bool:
    """True when two identifiers refer to the same strategy, across namespaces.

    A decision carries a *factory* strategy_id, while a deployment row stores
    the research-registry id its spec was registered under. For any registry id
    that is not already a legal factory identifier the two differ, so a direct
    ``==`` never matches.

    That silent mismatch is not cosmetic: a scheduler looking for "the
    deployment I already made" fails to find it, concludes it has none, and
    re-enters the creation path for a deployment that already exists. Because
    ``create_deployment`` is idempotent on (dataset, config), the same
    deployment comes back - and the caller then re-attaches a brand-new paper
    broker over the live one, discarding open positions and realised P&L on
    every tick. Positions could therefore never survive a tick.
    """
    if not candidate_id or not wanted_id:
        return False
    if candidate_id == wanted_id:
        return True

    mapped = _db_id_for_factory_id(wanted_id)
    if mapped is not None and mapped == candidate_id:
        return True

    return _factory_id_for_db_id(candidate_id) == wanted_id
