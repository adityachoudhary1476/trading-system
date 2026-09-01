"""Phase 13 safety tests: the AI research layer is DATA-ONLY, never execution.

Guarantees pinned here:
  * No eval/exec/compile/__import__/dynamic-code anywhere in strategy_lab
    (verified by AST scan — source is parsed, never executed by the scan).
  * No strategy_lab module imports the execution layer, FYERS, or anything
    that could place orders.
  * A fresh interpreter importing the AI provider layer pulls in NO module
    from trading_system.execution (no Broker, no PaperBroker).
  * StrategySpec cannot smuggle callables, extra code fields, or unknown
    nested structures.
  * Invalid strategies never reach the backtester from the research engine.

Note: importing `trading_system.research` transitively imports the FYERS
client MODULE because the pre-existing `india` package __init__ does so
(Day 8 architecture). Module import makes no network connection and is not
an execution capability — the AST scan below proves strategy_lab itself
never imports it.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

import trading_system.research.strategy_lab as strategy_lab
from trading_system.research.backtester import BacktestConfig
from trading_system.research.strategy_lab.engine import (
    ResearchConfig,
    StrategyResearchEngine,
)
from trading_system.research.strategy_lab.providers import (
    DeterministicStrategyProvider,
    GenerationContext,
    StrategyProposalProvider,
)
from trading_system.research.strategy_lab.spec import (
    StrategySpec,
    const_operand,
    field_operand,
    make_condition,
)
from trading_system.research.strategy_lab.validation import require_valid

STRATEGY_LAB_DIR = Path(strategy_lab.__file__).parent
PY_FILES = sorted(STRATEGY_LAB_DIR.glob("*.py"))

FORBIDDEN_CALL_NAMES = {
    "eval", "exec", "compile", "__import__", "globals", "locals",
    "vars", "breakpoint", "open", "system", "popen",
}
FORBIDDEN_IMPORT_SUBSTRINGS = (
    "execution", "fyers", "ctypes", "importlib", "marshal", "pickle",
    "subprocess", "socket", "http",
)
FORBIDDEN_ATTRIBUTE_CALLS = {
    "place_order", "submit", "cancel_order", "modify_order",
    "connect", "login", "authenticate",
}


def _ast_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_strategy_lab_modules_exist():
    names = {p.name for p in PY_FILES}
    expected = {
        "__init__.py", "dsl.py", "spec.py", "validation.py", "interpreter.py",
        "providers.py", "evaluation.py", "filters.py", "ranking.py", "engine.py",
    }
    assert expected <= names


def test_no_dynamic_execution_calls_in_implementation():
    for path in PY_FILES:
        tree = _ast_tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in FORBIDDEN_CALL_NAMES, (
                    f"{path.name}:{node.lineno} calls {node.func.id}() — the "
                    "research layer must never execute dynamic code"
                )


def _assert_clean_module(path: Path, lineno: int, module: str) -> None:
    lowered = module.lower()
    for token in FORBIDDEN_IMPORT_SUBSTRINGS:
        assert token not in lowered, (
            f"{path.name}:{lineno} imports {module!r} — the AI research layer "
            f"must never touch {token}"
        )


def test_no_forbidden_broker_or_network_imports():
    for path in PY_FILES:
        tree = _ast_tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    _assert_clean_module(path, node.lineno, alias.name)
            elif isinstance(node, ast.ImportFrom):
                _assert_clean_module(path, node.lineno, node.module or "")


def test_no_broker_method_calls():
    for path in PY_FILES:
        tree = _ast_tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in FORBIDDEN_ATTRIBUTE_CALLS, (
                    f"{path.name}:{node.lineno} calls .{node.func.attr}()"
                )


def test_importing_ai_provider_layer_loads_no_execution_modules():
    """Subprocess check: fresh interpreter, no test-order contamination."""
    src_dir = Path(strategy_lab.__file__).parents[3]  # .../src
    code = (
        "import sys; sys.path.insert(0, r'%s'); "
        "import trading_system.research.strategy_lab.providers; "
        "print(','.join(sorted("
        "m for m in sys.modules if m.startswith('trading_system.execution'))))"
        % src_dir
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        "AI provider layer must not load Broker/PaperBroker modules: "
        f"got {result.stdout!r}"
    )


# --------------------------------------------------------------------------- #
# StrategySpec cannot carry code
# --------------------------------------------------------------------------- #
def _valid_payload(**overrides) -> dict:
    payload = {
        "name": "Safety probe",
        "description": "probe",
        "symbol": "NSE:A",
        "timeframe": "1d",
        "indicators": [{"name": "sma", "params": {"window": 20}}],
        "entry": make_condition(field_operand("close"), ">", const_operand(1.0)),
    }
    payload.update(overrides)
    return payload


def test_spec_rejects_callable_payload():
    with pytest.raises(ValidationError):
        StrategySpec(**_valid_payload(extra_field=lambda: "boom"))


def test_spec_rejects_code_field_in_json():
    payload = _valid_payload()
    payload["code"] = "import os; os.system('x')"
    with pytest.raises(ValidationError):
        StrategySpec(**payload)


def test_spec_rejects_smuggled_operand_keys():
    payload = _valid_payload()
    payload["entry"]["left"]["smuggled"] = "os.system"
    with pytest.raises(ValidationError):
        StrategySpec(**payload)


def test_spec_rejects_unknown_condition_type():
    payload = _valid_payload()
    payload["entry"] = {"type": "exec_block", "code": "import os"}
    with pytest.raises(ValidationError):
        StrategySpec(**payload)


def test_spec_dump_is_pure_json_data():
    spec = StrategySpec(**_valid_payload())
    dumped = json.dumps(spec.model_dump(mode="json"))
    assert "callable" not in dumped
    assert "function" not in dumped
    # And it round-trips through strict JSON parsing.
    restored = StrategySpec.model_validate(json.loads(dumped))
    assert restored == spec


# --------------------------------------------------------------------------- #
# Invalid strategies cannot reach execution (the backtester)
# --------------------------------------------------------------------------- #
class _InvalidSpecProvider(StrategyProposalProvider):
    name = "safety-invalid"

    def generate_strategy(self, context):
        spec = DeterministicStrategyProvider().generate_strategy(context)
        # Semantically invalid: proposes for an instrument the dataset is not.
        return spec.model_copy(update={"symbol": "NSE:NOT_IN_DATASET"})


def _mini_dataset():
    import numpy as np
    import pandas as pd
    from trading_system.research.dataset import HistoricalDataset

    rng = np.random.default_rng(1)
    idx = pd.date_range("2024-01-01", periods=300, freq="1D", tz="UTC")
    close = 100 + np.cumsum(rng.normal(0, 1, 300))
    df = pd.DataFrame(
        {
            "open": close + 0.1,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": rng.integers(100, 1000, 300).astype(float),
        },
        index=idx,
    )
    return HistoricalDataset(symbol="NSE:SBIN", timeframe="1d", data=df)


def test_engine_blocks_invalid_specs_from_backtester():
    engine = StrategyResearchEngine(_InvalidSpecProvider(), ResearchConfig())
    ctx = GenerationContext(symbol="NSE:SBIN", timeframe="1d")
    report = engine.research_candidates(
        dataset=_mini_dataset(), generation_context=ctx,
        candidate_count=1, backtest_config=BacktestConfig(),
    )
    cand = report.candidates[0]
    assert cand.status == "invalid"
    assert cand.backtest is None and cand.evaluation is None


def test_require_valid_raises_on_symbol_mismatch():
    # Spec proposes for one instrument, dataset carries another.
    spec = DeterministicStrategyProvider().generate_strategy(
        GenerationContext(symbol="NSE:OTHER", timeframe="1d")
    )
    with pytest.raises(Exception) as excinfo:
        require_valid(spec, _mini_dataset())
    assert "symbol" in str(excinfo.value)

