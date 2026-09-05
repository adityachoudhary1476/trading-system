"""Phase 4 tests for the Strategy Factory runtime boundary.

Covers:
  * ``StrategyRuntime`` construction with compatible / incompatible contexts
  * strict vs non-strict compatibility modes
  * minimum-history gate on ``evaluate``
  * ``create_strategy_runtime`` factory (registry resolution, version selection)
  * instance isolation / no shared mutable state
  * AST/static safety for ``runtime.py``
  * read-only surface (no broker / network / I/O side effects)
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from trading_system.strategy_factory import (
    DataRequirement,
    MarketContext,
    StrategyRegistry,
    create_strategy_runtime,
)
from trading_system.strategy_factory.builtin import (
    EMACrossoverStrategy,
    RSIMeanReversionStrategy,
)
from trading_system.strategy_factory.capability import CompatibilityReport
from trading_system.strategy_factory.contract import (
    MarketState,
    PositionState,
    StrategySignal,
)
from trading_system.strategy_factory.exceptions import (
    InsufficientHistoryError,
    StrategyFactoryError,
)
from trading_system.strategy_factory.runtime import StrategyRuntime

FACTORY_PKG = __import__("trading_system.strategy_factory", fromlist=["__file__"])
FACTORY_DIR = Path(FACTORY_PKG.__file__).resolve().parent
RUNTIME_FILE = FACTORY_DIR / "runtime.py"

REPO_ROOT = Path(__file__).resolve().parents[2]
PY_PATH_ENTRY = str(REPO_ROOT / "src")


def _make_state(symbol: str, bars: int = 300) -> MarketState:
    """Build a look-ahead-safe ``MarketState`` with enough bars for EMA crossover."""
    idx = pd.date_range("2024-01-01", periods=bars, freq="D", tz="UTC")
    close = list(range(100, 100 + bars))
    df = pd.DataFrame(
        {
            "open": close,
            "high": [c + 1 for c in close],
            "low": [c - 1 for c in close],
            "close": close,
            "volume": [1000] * bars,
        },
        index=idx,
    )
    return MarketState(
        symbol=symbol,
        timeframe="1d",
        timestamp=idx[-1],
        bars=df,
    )


def _ctx(**kw):
    base = dict(
        instrument="NSE:NIFTY",
        timeframe="1d",
        bar_count=300,
        data_available=[DataRequirement.OHLCV],
    )
    base.update(kw)
    return MarketContext(**base)


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #
class TestRuntimeConstruction:
    def test_constructed_with_compatible_strategy_and_context(self):
        s = EMACrossoverStrategy()
        rt = StrategyRuntime(s, _ctx())
        assert rt.strategy is s
        assert rt.context.timeframe == "1d"
        assert rt.compatibility_report.compatible is True

    def test_incompatible_strategy_strict_raises(self):
        s = EMACrossoverStrategy()
        ctx = _ctx(timeframe="1w")  # 1w not supported by EMA crossover
        with pytest.raises(StrategyFactoryError):
            StrategyRuntime(s, ctx, strict_compatibility=True)

    def test_incompatible_strategy_non_strict_allowed(self):
        s = EMACrossoverStrategy()
        ctx = _ctx(timeframe="1w")
        rt = StrategyRuntime(s, ctx, strict_compatibility=False)
        assert rt.compatibility_report.compatible is False

    def test_accepts_dict_context(self):
        s = EMACrossoverStrategy()
        rt = StrategyRuntime(s, {"instrument": "NSE:NIFTY", "timeframe": "1d",
                                 "bar_count": 300, "data_available": ["ohlcv"]})
        assert rt.context.instrument == "NSE:NIFTY"

    def test_non_strategy_raises_typeerror(self):
        with pytest.raises(TypeError):
            StrategyRuntime(object(), _ctx())

    def test_non_context_raises_typeerror(self):
        s = EMACrossoverStrategy()
        with pytest.raises((TypeError, Exception)):
            StrategyRuntime(s, ["not", "a", "context"])


# --------------------------------------------------------------------------- #
# Properties
# --------------------------------------------------------------------------- #
class TestRuntimeProperties:
    def test_properties_reflect_bound_strategy(self):
        s = EMACrossoverStrategy()
        rt = StrategyRuntime(s, _ctx())
        assert rt.strategy_id == "ema_crossover"
        assert rt.strategy_version == "1.0.0"
        assert rt.minimum_bars == 26
        assert rt.capabilities.minimum_bars == 26
        assert isinstance(rt.compatibility_report, CompatibilityReport)

    def test_runtime_is_read_only(self):
        s = EMACrossoverStrategy()
        rt = StrategyRuntime(s, _ctx())
        for attr in ("strategy", "context", "capabilities", "compatibility_report",
                     "strategy_id", "strategy_version", "minimum_bars"):
            assert hasattr(rt, attr)
        # properties are read-only (no setter) -> assignment raises AttributeError
        with pytest.raises(AttributeError):
            rt.strategy = s
        with pytest.raises(AttributeError):
            rt._new_attr = 1


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
class TestRuntimeEvaluate:
    def test_evaluate_returns_strategy_signal(self):
        s = EMACrossoverStrategy()
        rt = StrategyRuntime(s, _ctx(bar_count=300))
        state = _make_state("TEST", bars=50)
        sig = rt.evaluate(state)
        assert isinstance(sig, StrategySignal)
        assert sig.strategy_id == "ema_crossover"
        assert sig.symbol == "TEST"
        assert sig.timestamp == state.timestamp

    def test_call_alias_evaluates(self):
        s = EMACrossoverStrategy()
        rt = StrategyRuntime(s, _ctx(bar_count=300))
        state = _make_state("TEST", bars=50)
        sig1 = rt.evaluate(state)
        sig2 = rt(state)
        assert sig1.strategy_id == sig2.strategy_id
        assert sig1.action == sig2.action

    def test_insufficient_history_raises(self):
        s = EMACrossoverStrategy()
        rt = StrategyRuntime(s, _ctx(bar_count=300))
        state = _make_state("TEST", bars=10)  # EMA crossover needs 26
        with pytest.raises(InsufficientHistoryError):
            rt.evaluate(state)

    def test_evaluate_does_not_mutate_strategy(self):
        s = EMACrossoverStrategy()
        rt = StrategyRuntime(s, _ctx(bar_count=300))
        before_params = s.parameters
        _ = rt.evaluate(_make_state("TEST", bars=50))
        assert s.parameters == before_params

    def test_position_state_carried_through(self):
        s = EMACrossoverStrategy()
        rt = StrategyRuntime(s, _ctx(bar_count=300))
        idx = pd.date_range("2024-01-01", periods=50, freq="D", tz="UTC")
        df = pd.DataFrame(
            {"open": range(100, 150), "high": range(101, 151),
             "low": range(99, 149), "close": range(100, 150), "volume": [100] * 50},
            index=idx,
        )
        state = MarketState(
            symbol="TEST", timeframe="1d", timestamp=idx[-1],
            bars=df,
            position=PositionState(symbol="TEST", side=1, size=10, entry_price=95.0),
        )
        sig = rt.evaluate(state)
        assert isinstance(sig, StrategySignal)


# --------------------------------------------------------------------------- #
# create_strategy_runtime factory
# --------------------------------------------------------------------------- #
class TestCreateStrategyRuntime:
    def _registry(self):
        reg = StrategyRegistry()
        reg.register(EMACrossoverStrategy())
        reg.register(RSIMeanReversionStrategy())
        return reg

    def test_create_from_registry_by_id(self):
        rt = create_strategy_runtime("ema_crossover", _ctx(), registry=self._registry())
        assert rt.strategy_id == "ema_crossover"
        assert rt.compatibility_report.compatible is True

    def test_create_with_version(self):
        rt = create_strategy_runtime(
            "ema_crossover", _ctx(), registry=self._registry(), version="1.0.0"
        )
        assert rt.strategy_version == "1.0.0"

    def test_create_with_parameters(self):
        rt = create_strategy_runtime(
            "ema_crossover", _ctx(), registry=self._registry(),
            parameters={"fast_period": 5, "slow_period": 20},
        )
        assert rt.strategy.parameters["fast_period"] == 5
        assert rt.strategy.parameters["slow_period"] == 20

    def test_create_unknown_strategy_raises_keyerror(self):
        with pytest.raises(KeyError):
            create_strategy_runtime("unknown", _ctx(), registry=self._registry())

    def test_create_invalid_parameters_raises_validation(self):
        from trading_system.strategy_factory.exceptions import StrategyValidationError
        with pytest.raises(StrategyValidationError):
            create_strategy_runtime(
                "ema_crossover", _ctx(), registry=self._registry(),
                parameters={"fast_period": 1},  # below minimum (2)
            )

    def test_create_incompatible_strict_raises(self):
        with pytest.raises(StrategyFactoryError):
            create_strategy_runtime(
                "ema_crossover", _ctx(timeframe="1w"),
                registry=self._registry(), strict_compatibility=True,
            )

    def test_create_incompatible_non_strict(self):
        rt = create_strategy_runtime(
            "ema_crossover", _ctx(timeframe="1w"),
            registry=self._registry(), strict_compatibility=False,
        )
        assert rt.compatibility_report.compatible is False

    def test_create_with_none_registry_raises(self):
        with pytest.raises(KeyError):
            create_strategy_runtime("ema_crossover", _ctx(), registry=None)

    def test_create_with_catalog(self):
        from trading_system.strategy_factory import Catalog
        cat = Catalog(StrategyRegistry())
        cat.registry.register(EMACrossoverStrategy())
        rt = create_strategy_runtime("ema_crossover", _ctx(), registry=cat)
        assert rt.strategy_id == "ema_crossover"


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #
class TestRuntimeIsolation:
    def _registry(self):
        reg = StrategyRegistry()
        reg.register(EMACrossoverStrategy())
        reg.register(RSIMeanReversionStrategy())
        return reg

    def test_multiple_runtimes_independent(self):
        s1 = EMACrossoverStrategy()
        s2 = EMACrossoverStrategy(fast_period=5, slow_period=20)
        rt1 = StrategyRuntime(s1, _ctx())
        rt2 = StrategyRuntime(s2, _ctx())
        assert rt1.strategy is not rt2.strategy
        assert rt1.capabilities is not rt2.capabilities

    def test_create_does_not_mutate_registry(self):
        reg = self._registry()
        before = list(reg.identities())
        _ = create_strategy_runtime("ema_crossover", _ctx(), registry=reg)
        assert list(reg.identities()) == before


# --------------------------------------------------------------------------- #
# Static safety
# --------------------------------------------------------------------------- #
class TestRuntimeStaticSafety:
    def test_no_forbidden_dynamic_execution(self):
        tree = ast.parse(RUNTIME_FILE.read_text(encoding="utf-8"), filename=str(RUNTIME_FILE))
        calls = [
            node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        forbidden = {"eval", "exec", "compile", "__import__", "open", "system", "popen", "marshal", "pickle"}
        offenders = [c for c in calls if c in forbidden]
        assert not offenders, f"forbidden calls in runtime.py: {offenders}"

    def test_no_forbidden_imports(self):
        tree = ast.parse(RUNTIME_FILE.read_text(encoding="utf-8"), filename=str(RUNTIME_FILE))
        forbidden_tokens = ("execution", "fyers", "upstox", "paper", "india", "socket",
                            "http", "requests", "subprocess", "ctypes", "pickle")
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for tok in forbidden_tokens:
                        if tok in alias.name.lower():
                            offenders.append((alias.name, tok))
            elif isinstance(node, ast.ImportFrom):
                module = (node.module or "").lower()
                for tok in forbidden_tokens:
                    if tok in module:
                        offenders.append((node.module, tok))
        assert not offenders, f"forbidden imports in runtime.py: {offenders}"
