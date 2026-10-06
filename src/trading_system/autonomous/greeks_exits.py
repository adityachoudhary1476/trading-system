"""Phase 5 -- greeks-aware exit-rule engine (pure, deterministic, paper-only).

An ordered, independently gated registry of exit rules evaluated from the
greeks snapshot the controller persists on a position (``entry_delta``,
``last_delta``, ``entry_iv``, ``last_iv``). Only implied vol and delta are ever
stored, so the one rule this phase can evaluate honestly is **IV crush**: a
long-premium thesis is spent once implied vol has collapsed to a configured
fraction of the vol it was opened at.

Design invariants:

* D1 fail-open: anything missing, non-finite or unusable yields ``None``.
  The engine never raises, never reads a clock and never blocks an exit the
  legacy thresholds in ``portfolio.py`` own; the caller decides what a fired
  ``GreeksExit`` means in the priority order.
* D2 default OFF: every rule ships dark behind its own config flag, mirroring
  the ``AUTONOMOUS_DELTA_DECAY_*`` convention -- env is read at call time by
  the helpers, truthy spellings are ``("1", "true", "yes", "on")``, and an
  unknown or absent value keeps the rule off.
* Deterministic: no clock, no I/O, no module state. ``min_holding_seconds`` is
  measured against the caller-supplied ``holding_seconds`` only, so identical
  inputs always produce byte-identical output.
* Registry over ``if`` chains: a future theta/gamma/vega rule registers itself
  in ``_RULES`` with its own gate and reason string, and the caller of
  ``evaluate_greeks_exit`` does not change.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "ExitGreeksInput",
    "GreeksExitConfig",
    "GreeksExit",
    "evaluate_greeks_exit",
]

_TRUTHY = ("1", "true", "yes", "on")
_DEFAULT_IV_CRUSH_FLOOR = 0.5
_DEFAULT_MIN_HOLDING_SECONDS = 0.0


@dataclass(frozen=True)
class ExitGreeksInput:
    """The persisted greeks facts for one held position.

    Mirrors what ``controller._capture_position_greeaks`` writes onto a
    position: there is no stored theta, gamma, vega, price or clock, and the
    holding age is supplied by the caller rather than derived here. Defaults
    describe the common case -- an equity, or an option whose greeks were never
    computable -- so an untouched input evaluates to ``None``.
    """

    is_option: bool = False
    entry_delta: Optional[float] = None
    last_delta: Optional[float] = None
    entry_iv: Optional[float] = None
    last_iv: Optional[float] = None
    holding_seconds: Optional[float] = None


@dataclass(frozen=True)
class GreeksExitConfig:
    """Opt-in gates for the greeks exit rules.

    ``iv_crush_enabled`` is False so the phase ships behaviourally inert until
    someone sets it on purpose. ``iv_crush_floor`` is the share of entry IV the
    position must retain: exit when ``last_iv / entry_iv < floor``. It is only
    meaningful inside (0, 1) -- a floor of 0 exits nothing and a floor of 1
    exits everything -- so an out-of-range floor is treated as unusable and the
    rule stays off rather than guessing.
    """

    iv_crush_enabled: bool = False
    iv_crush_floor: float = 0.5
    min_holding_seconds: float = 0.0


@dataclass(frozen=True)
class GreeksExit:
    """One fired greeks exit decision: a stable reason string and its evidence.

    ``detail`` carries plain floats only so the caller can log, persist or
    diff it without further coercion.
    """

    reason: str
    detail: dict


def greeks_exits_enabled() -> bool:
    """True only when BOTH ``AUTONOMOUS_GREEKS_ENABLED`` and
    ``AUTONOMOUS_GREEKS_EXITS_ENABLED`` are truthy (master switch + phase
    switch), so unset/empty/typo'd vars keep the rule off.

    Two switches on purpose: the master switch is the single kill for every
    greeks-driven decision, so turning the whole layer off also turns off
    anything this phase adds later, without hunting for per-rule flags.
    """
    master = os.environ.get("AUTONOMOUS_GREEKS_ENABLED", "").strip().lower()
    phase = os.environ.get("AUTONOMOUS_GREEKS_EXITS_ENABLED", "").strip().lower()
    return master in _TRUTHY and phase in _TRUTHY


def iv_crush_floor() -> float:
    """``AUTONOMOUS_IV_CRUSH_FLOOR`` as a float clamped to (0, 1), default 0.5.

    Read from the environment rather than a deployment config for the same
    reason as the delta-decay floor: any new config field changes
    ``deployment_identity`` and would orphan every live deployment. An
    unparseable or out-of-range value logs a warning and falls back to the
    default instead of disabling or, worse, arming the rule by accident.
    """
    raw = os.environ.get("AUTONOMOUS_IV_CRUSH_FLOOR", "").strip()
    if not raw:
        return _DEFAULT_IV_CRUSH_FLOOR
    try:
        value = float(raw)
    except ValueError:
        logger.warning("ignoring unparseable AUTONOMOUS_IV_CRUSH_FLOOR=%r", raw)
        return _DEFAULT_IV_CRUSH_FLOOR
    if not 0.0 < value < 1.0:
        logger.warning(
            "AUTONOMOUS_IV_CRUSH_FLOOR=%r outside (0, 1); using %s",
            raw,
            _DEFAULT_IV_CRUSH_FLOOR,
        )
        return _DEFAULT_IV_CRUSH_FLOOR
    return value


def iv_crush_min_holding_seconds() -> float:
    """``AUTONOMOUS_IV_CRUSH_MIN_HOLDING_SECONDS`` as a non-negative float.

    Default 0.0: the rule is not age-biased until someone asks for it. An
    unparseable, negative or non-finite value logs a warning and falls back to
    0.0 -- a garbage threshold must never become a reason to hold or to exit,
    so it degrades to "no minimum" rather than to an accident.
    """
    raw = os.environ.get("AUTONOMOUS_IV_CRUSH_MIN_HOLDING_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_MIN_HOLDING_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "ignoring unparseable AUTONOMOUS_IV_CRUSH_MIN_HOLDING_SECONDS=%r",
            raw,
        )
        return _DEFAULT_MIN_HOLDING_SECONDS
    if not math.isfinite(value) or value < 0.0:
        logger.warning(
            "AUTONOMOUS_IV_CRUSH_MIN_HOLDING_SECONDS=%r not a non-negative "
            "finite number; using %s",
            raw,
            _DEFAULT_MIN_HOLDING_SECONDS,
        )
        return _DEFAULT_MIN_HOLDING_SECONDS
    return value


def _finite(value: Any) -> Optional[float]:
    """The value as a finite float, or None when it is unusable.

    Absent, non-numeric and non-finite values all collapse to None so no rule
    can accidentally turn "unknown" into a number and act on it.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _usable_floor(value: Any) -> Optional[float]:
    """The configured IV-crush floor when it is usable, else None.

    A floor outside (0, 1) would exit every option or none of them regardless
    of what the market actually did, which is not the configured behaviour, so
    the rule declines to fire instead of guessing at intent.
    """
    floor = _finite(value)
    if floor is None or not 0.0 < floor < 1.0:
        return None
    return floor


def _iv_crush_exit(
    greeks: ExitGreeksInput, config: GreeksExitConfig
) -> Optional[GreeksExit]:
    """Fire when an option's implied vol has collapsed below its floor.

    IV crush is the thesis-spent rule for long premium: the position was opened
    expecting vol to work for it, and once only a fraction of the entry vol is
    left there is nothing left to be paid for holding. It is evaluated only
    from persisted entry/last IV, and returns None whenever any input is
    unknown, non-finite or unusable -- an unmeasurable option is held under the
    existing thresholds rather than liquidated on a guess.
    """
    if not config.iv_crush_enabled:
        return None
    if greeks.is_option is not True:
        return None
    floor = _usable_floor(config.iv_crush_floor)
    if floor is None:
        return None
    entry_iv = _finite(greeks.entry_iv)
    if entry_iv is None or entry_iv <= 0.0:
        return None
    last_iv = _finite(greeks.last_iv)
    if last_iv is None or last_iv < 0.0:
        return None
    if greeks.holding_seconds is not None:
        holding = _finite(greeks.holding_seconds)
        minimum = _finite(config.min_holding_seconds)
        if holding is None or minimum is None or holding < minimum:
            return None
    ratio = last_iv / entry_iv
    if not ratio < floor:
        return None
    return GreeksExit(
        reason="iv_crush",
        detail={
            "entry_iv": entry_iv,
            "last_iv": last_iv,
            "ratio": ratio,
            "floor": floor,
        },
    )


_Rule = Callable[[ExitGreeksInput, GreeksExitConfig], Optional[GreeksExit]]

_RULES: tuple[_Rule, ...] = (
    _iv_crush_exit,
)


def evaluate_greeks_exit(
    *,
    greeks: ExitGreeksInput,
    config: GreeksExitConfig,
) -> Optional[GreeksExit]:
    """Evaluate the ordered greeks exit rules and return the first that fires.

    The registry is walked in order and the first non-None decision wins, so a
    caller can reason about priority without knowing how many rules exist. Each
    rule gates itself, and a rule that cannot decide -- disabled, not an
    option, missing or unusable inputs -- returns None and lets the next rule
    (or the legacy exit ladder) decide. Never raises: an unexpected failure is
    read as "no greeks opinion", which is the fail-open contract.
    """
    for rule in _RULES:
        try:
            outcome = rule(greeks, config)
        except Exception:
            continue
        if outcome is not None:
            return outcome
    return None
