"""Phase 1 integration + safety tests for the Strategy Factory.

Integration flow (Step 15)::

    Create -> Validate -> Register -> Discover -> Retrieve -> Evaluate -> Signal

plus an explicit proof that NO execution/broker/network call occurs during
evaluation (the strategy returns pure data).

Safety posture mirrors ``tests/test_strategy_safety.py``: the Strategy Factory's
OWN source is AST-scanned for forbidden imports/calls. As with the existing
strategy_lab safety tests, importing the Factory reuses ``trading_system.research``,
which (per Day 8 architecture) transitively imports the ``india`` package on
import -- with NO network connection. The subprocess guard below therefore
asserts that the execution/broker and paper-trading layers are never loaded.
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading_system import strategy_factory as factory_pkg
from trading_system.strategy_factory import (
    MarketState,
    PositionState,
    SignalAction,
    Strategy,
    StrategyRegistry,
    StrategySignal,
    clear_discovery,
    discover,
    get_strategy_class,
    registered_strategy_ids,
    require_valid_strategy,
    validate_strategy,
)
from trading_system.strategy_factory.builtin import EMACrossoverStrategy, RSIMeanReversionStrategy

FACTORY_DIR = Path(factory_pkg.__file__).parent
FACTORY_PY = sorted(FACTORY_DIR.rglob("*.py"))

FORBIDDEN_CALLS = {
    "eval", "exec", "compile", "__import__", "globals", "locals",
    "getattr", "setattr", "vars", "breakpoint", "open", "system", "popen",
}
FORBIDDEN_IMPORT_TOKENS = (
    "execution", "fyers", "upstox", "paper", "india", "socket",
    "http", "requests", "subprocess", "os.system", "ctypes",
    "marshal", "pickle",
)
FORBIDDEN_BROKER_METHODS = {
    "place_order", "submit", "cancel_order", "modify_order",
    "connect", "login", "authenticate",
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _bars(n=60, seed=1, trend=0.3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    close = 100 + np.cumsum(rng.normal(trend, 1.0, n))
    return pd.DataFrame(
        {"open": close, "high": close + 1.0, "low": close - 1.0,
         "close": close, "volume": 100.0}, index=idx,
    )


# --------------------------------------------------------------------------- #
# Integration flow: Create -> Validate -> Register -> Discover -> Retrieve -> Evaluate -> Signal
# --------------------------------------------------------------------------- #
def test_full_integration_flow():
    # 1. Create
    strategy = EMACrossoverStrategy(fast_period=12, slow_period=26)

    # 2. Validate
    assert validate_strategy(strategy) == []
    require_valid_strategy(strategy)

    # 3. Register
    reg = StrategyRegistry()
    reg.register(strategy)
    assert reg.exists("ema_crossover", "1.0.0")

    # 4. Discover (class-level discovery catalog)
    clear_discovery()
    added = discover(["trading_system.strategy_factory.builtin"], reload=True)
    assert "ema_crossover" in registered_strategy_ids()
    assert added >= 1
    discovered_cls = get_strategy_class("ema_crossover")
    assert discovered_cls is EMACrossoverStrategy

    # 5. Retrieve
    retrieved = reg.get("ema_crossover", "1.0.0")
    assert retrieved is strategy

    # 6. Evaluate
    df = _bars()
    state = MarketState(symbol="NSE:SBIN", timeframe="1d", timestamp=df.index[-1], bars=df)
    signal = retrieved.evaluate(state)

    # 7. Return StrategySignal
    assert isinstance(signal, StrategySignal)
    assert signal.action in (SignalAction.BUY, SignalAction.SELL, SignalAction.EXIT, SignalAction.HOLD)
    assert signal.strategy_id == "ema_crossover"
    assert signal.symbol == "NSE:SBIN"
    assert signal.version == "1.0.0"
    assert signal.reference_price > 0
    assert 0.0 <= signal.confidence <= 1.0
    assert signal.timestamp == state.timestamp
    json.dumps(signal.to_dict())


def test_signal_carries_no_direct_broker_reference():
    df = _bars()
    state = MarketState(symbol="NSE:SBIN", timeframe="1d", timestamp=df.index[-1], bars=df)
    strategy = EMACrossoverStrategy()
    require_valid_strategy(strategy)
    signal = strategy.evaluate(state)

    assert not hasattr(signal, "broker")
    assert not hasattr(signal, "exchange")
    assert not hasattr(signal, "client")
    assert not hasattr(signal, "place_order")
    assert signal.__class__.__name__ == "StrategySignal"
    for value in signal.to_dict().values():
        if isinstance(value, dict):
            for leaf in value.values():
                assert not hasattr(leaf, "place_order")

    assert not hasattr(strategy, "broker")
    assert not hasattr(strategy, "paper_broker")


def test_evaluate_is_pure_and_deterministic():
    df = _bars(seed=4, trend=0.4)
    state = MarketState(symbol="NSE:SBIN", timeframe="1d", timestamp=df.index[-1], bars=df)
    strategy = EMACrossoverStrategy()
    a = strategy.evaluate(state)
    b = strategy.evaluate(state)
    assert a.action == b.action
    assert a == b
    assert state.bars is not None


def test_reference_strategy_source_has_no_broker_calls():
    source = Path(inspect.getfile(EMACrossoverStrategy)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in FORBIDDEN_BROKER_METHODS, (
                f"reference strategy calls forbidden broker method {node.func.attr}"
            )
    source2 = Path(inspect.getfile(RSIMeanReversionStrategy)).read_text(encoding="utf-8")
    tree2 = ast.parse(source2)
    for node in ast.walk(tree2):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in FORBIDDEN_BROKER_METHODS


# --------------------------------------------------------------------------- #
# Safety: AST scan of the Factory's OWN source
# --------------------------------------------------------------------------- #
def test_factory_source_has_no_forbidden_imports():
    for path in FACTORY_PY:
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for token in FORBIDDEN_IMPORT_TOKENS:
                        assert token not in alias.name.lower(), (
                            f"{path.name} imports {alias.name!r} (forbidden: {token})"
                        )
            elif isinstance(node, ast.ImportFrom):
                module = (node.module or "").lower()
                for token in FORBIDDEN_IMPORT_TOKENS:
                    assert token not in module, (
                        f"{path.name} imports from {node.module!r} (forbidden: {token})"
                    )


def test_factory_source_has_no_dynamic_execution_calls():
    for path in FACTORY_PY:
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in FORBIDDEN_CALLS, (
                    f"{path.name}:{node.lineno} calls {node.func.id}()"
                )


def test_factory_source_has_no_broker_method_calls():
    for path in FACTORY_PY:
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in FORBIDDEN_BROKER_METHODS, (
                    f"{path.name}:{node.lineno} calls .{node.func.attr}()"
                )


def test_importing_factory_loads_no_execution_or_paper_modules():
    """A fresh interpreter importing the Factory must not load the execution or
    paper-trading broker layers. (The pre-existing ``india`` package is imported
    transitively by ``trading_system.research``, exactly as documented in
    ``tests/test_strategy_safety.py``; that import makes no network connection.)"""
    src_path = str(Path(factory_pkg.__file__).resolve().parents[1])
    code = (
        "import sys, trading_system.strategy_factory; "
        "bad=[m for m in sys.modules "
        "if m.startswith('trading_system.execution') "
        "or m.startswith('trading_system.paper')]; "
        "print(','.join(sorted(bad)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=240,
        env={**os.environ, "PYTHONPATH": src_path},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        "Importing the Strategy Factory must not load execution/paper modules; "
        f"got: {result.stdout!r}"
    )


def test_factory_has_no_live_trading_capability():
    for name in factory_pkg.__all__:
        obj = getattr(factory_pkg, name)
        assert "broker" not in type(obj).__name__.lower()
        assert "upstox" not in type(obj).__name__.lower()
        assert "paper" not in type(obj).__name__.lower()
