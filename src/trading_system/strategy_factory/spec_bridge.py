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

import hashlib
import logging
import re
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
# Strategy-id mapping
#
# ``StrategyMetadata.strategy_id`` must match ``^[a-z][a-z0-9_]{0,63}$``,
# but the research registry keys strategies on a 64-char SHA-256 digest
# (e.g. ``9a3b16b2...``) that starts with a digit. Handing that digest
# straight to the metadata raised ValidationError, and the per-strategy
# ``except: continue`` in ``register_db_spec_strategies`` swallowed it - so
# the catalog came back empty, ``build_from_discovery`` could not resolve any
# decision, and the scheduler reported ``unknown_strategy`` for every signal
# while still stamping a healthy heartbeat. No order was ever placed.
#
# Non-conforming ids are therefore mapped to ``db_<192-bit digest>``:
# deterministic (stable across restarts and replicas, which the signal-identity
# contract depends on), always valid, and never silently truncating. Ids that
# already conform are passed through unchanged.
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

_FACTORY_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FACTORY_ID_PREFIX = "db_"
_FACTORY_ID_DIGEST_LEN = 48  # 192 bits; "db_" + 48 = 51 chars, well under 64

# factory_id -> research-registry strategy_id, for diagnostics and reverse lookup.
_FACTORY_ID_TO_DB_ID: dict[str, str] = {}


def factory_id_for_db_id(db_strategy_id: str) -> str:
    """Return the valid factory identifier for a research-registry id.

    Conforming ids are returned unchanged. Anything else is hashed to
    ``db_<48 hex>``. Pure and deterministic - it never consults process state,
    so a restarted or replicated scheduler derives the same factory id and can
    still resolve previously-registered decisions.
    """
    candidate = (db_strategy_id or "").strip()
    if _FACTORY_ID_RE.match(candidate):
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:_FACTORY_ID_DIGEST_LEN]
    return _FACTORY_ID_PREFIX + digest


def db_id_for_factory_id(factory_id: str) -> Optional[str]:
    """Reverse of :func:`factory_id_for_db_id` using this process's registry.

    Returns ``None`` when the factory id was never registered here. Callers
    that must work on a cold process should instead compare
    ``factory_id_for_db_id(s.strategy_id)`` per registered strategy, which
    needs no process state.
    """
    return _FACTORY_ID_TO_DB_ID.get(factory_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_family(spec: StrategySpec) -> StrategyFamily:
    name = (spec.name or "").lower()
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
    # StrategySpec stores parameters inside the indicator definitions, not at top level.
    for ind in (spec.indicators or []):
        params_dict = ind.parameters if hasattr(ind, "parameters") else {}
        for key, val in params_dict.items():
            params.append(ParameterDefinition(
                name=key,
                type=ParameterType.FLOAT,
                default=float(val) if isinstance(val, (int, float)) else 0.0,
                description=f"Auto-bridged parameter from {spec.name}",
            ))
    # Also pull any top-level numeric fields that look like parameters.
    for field_name in ("stop_loss", "take_profit", "trailing_stop"):
        val = spec.stop_loss if field_name == "stop_loss" and hasattr(spec, "stop_loss") else None
        if field_name == "take_profit":
            val = spec.take_profit if hasattr(spec, "take_profit") else None
        if field_name == "trailing_stop":
            val = spec.trailing_stop if hasattr(spec, "trailing_stop") else None
        if val is not None:
            params.append(ParameterDefinition(
                name=field_name,
                type=ParameterType.FLOAT,
                default=float(val) if isinstance(val, (int, float)) else 0.0,
                description=f"Auto-bridged parameter from {spec.name}",
            ))
    return ParameterSchema(parameters=params)


def _spec_to_metadata(spec: StrategySpec, *, strategy_id: Optional[str] = None) -> StrategyMetadata:
    return StrategyMetadata(
        strategy_id=strategy_id or f"spec_{spec.name}",
        name=spec.name or "DB Strategy",
        version="1.0.0",
        family=_infer_family(spec),
        description=spec.description or f"Auto-bridged spec: {spec.name}",
        timeframes=[spec.timeframe] if spec.timeframe else ["1d"],
        supported_instruments=[spec.symbol] if spec.symbol else ["*"],
        required_data=["ohlcv"],
        # Must be derived from the spec. It used to be hardcoded to [], but
        # ``validate_metadata`` rejects metadata that declares no indicators,
        # so every bridged spec failed compatibility with
        # STRATEGY_NOT_FOUND and never produced a decision. Deriving the real
        # names keeps the factory contract honest as well as satisfiable.
        required_indicators=[ind.name for ind in (spec.indicators or [])],
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


def _make_factory_strategy_class(spec: StrategySpec, *, strategy_id: Optional[str] = None) -> type[Strategy]:
    """Return (and cache) a factory ``Strategy`` subclass wrapping *spec*."""
    sid = strategy_id or f"spec_{spec.name}"
    if sid in _STREAM_CACHE:
        return _STREAM_CACHE[sid]

    metadata_obj = _spec_to_metadata(spec, strategy_id=strategy_id)

    class _SpecFactoryStrategy(Strategy):
        """Deterministic factory Strategy wrapping a persisted StrategySpec.

        ``evaluate`` runs the spec through the Phase-13 interpreter
        (``SpecStrategy.generate``) restricted to the closed bars in *state*,
        then translates the resulting target position (+1/0/-1) into a
        ``StrategySignal``.
        """

        metadata = metadata_obj

        def __init__(self, **params: Any) -> None:
            # Ignore caller overrides; the spec is the source of truth.
            self._spec = spec
            self._research = SpecStrategy(spec)

        @property
        def parameters(self) -> dict[str, Any]:
            """Concrete parameter values bound to this instance.

            Required by the ``Strategy`` ABC. The class was previously missing
            it, so it stayed abstract: registration succeeded (metadata is a
            class attribute) but every ``build_from_discovery`` call raised
            ``TypeError: Can't instantiate abstract class``, so no bridged DB
            spec could ever produce a signal.

            Keys are the bare parameter names used by
            ``_spec_params_to_schema`` so that this stays consistent with the
            schema ``build_from_discovery`` validates caller params against.
            """
            bound: dict[str, Any] = {}
            for ind in (spec.indicators or []):
                for key, val in (ind.params or {}).items():
                    bound[key] = val
            return bound

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
                    strategy_id=metadata_obj.strategy_id,
                    timestamp=state.timestamp,
                    symbol=state.symbol,
                    reference_price=state.latest_close,
                    confidence=0.5 if target != 0 else 0.0,
                    reason=f"Spec[{spec.name}] target={target}",
                    target_position=target,
                    version=metadata_obj.version,
                )
            except Exception as exc:  # noqa: BLE001
                return self._hold(state, f"spec evaluation error: {exc}")

        def _hold(self, state: MarketState, reason: str) -> StrategySignal:
            return StrategySignal(
                action=SignalAction.HOLD,
                strategy_id=metadata_obj.strategy_id,
                timestamp=state.timestamp,
                symbol=state.symbol,
                reference_price=state.latest_close,
                confidence=0.0,
                reason=reason,
                target_position=0,
                version=metadata_obj.version,
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
        Sorted **factory** strategy IDs that were registered (or were already
        present). These are what ``build_from_discovery`` is called with; they
        may differ from the research-registry ids (see
        :func:`factory_id_for_db_id`).
    """
    registered: list[str] = []
    failed: list[tuple[str, str]] = []

    try:
        research_registry: ResearchRegistry = center.registry  # type: ignore[assignment]
    except AttributeError:
        return registered

    try:
        strategies = research_registry.list_strategies()
    except Exception as exc:  # noqa: BLE001
        # Surfaced rather than silently returned: an unreadable registry means
        # an empty catalog, which means the scheduler can never trade.
        logger.error("spec_bridge: cannot list strategies from registry: %s", exc)
        return registered

    for strategy in strategies:
        spec_json = strategy.spec_json if hasattr(strategy, "spec_json") else None
        if not spec_json:
            continue
        try:
            spec: Optional[StrategySpec] = StrategySpec.model_validate_json(spec_json)
        except Exception as exc:  # noqa: BLE001
            sid = strategy.strategy_id if hasattr(strategy, "strategy_id") else "<unknown>"
            logger.warning(
                "spec_bridge: strategy %s has an unreadable spec_json: %s",
                sid,
                exc,
            )
            failed.append((str(sid), "unreadable_spec"))
            continue

        db_id = strategy.strategy_id or f"spec_{spec.name}"
        factory_id = factory_id_for_db_id(db_id)
        if factory_id in registered_strategy_ids():
            _FACTORY_ID_TO_DB_ID[factory_id] = db_id
            registered.append(factory_id)
            continue

        try:
            factory_cls = _make_factory_strategy_class(spec, strategy_id=factory_id)
            register_strategy(factory_cls)
            _FACTORY_ID_TO_DB_ID[factory_id] = db_id
            registered.append(factory_id)
        except Exception as exc:  # noqa: BLE001
            # Logged, not swallowed. This path used to ``continue`` in silence,
            # which is how an entirely empty catalog looked like a healthy
            # scheduler for months.
            logger.warning(
                "spec_bridge: failed to register strategy %s as %s: %s",
                db_id,
                factory_id,
                exc,
            )
            failed.append((db_id, type(exc).__name__))

    if failed:
        logger.error(
            "spec_bridge: %d of %d DB strategies could NOT be registered; "
            "their signals will resolve to unknown_strategy and never execute",
            len(failed),
            len(strategies),
        )
    return sorted(set(registered))
