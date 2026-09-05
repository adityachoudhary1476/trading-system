"""Phase 3 production-hardening tests for the Strategy Factory.

Coverage areas (each maps to a Phase 3 objective):

  * Canonical identity (3A)           -- ``strategy_identity`` determinism
  * Configuration normalization (3B)  -- ``configuration_fingerprint`` canonicalization
  * Versioning contract (3C)          -- valid/invalid/unknown/duplicate/multiple
  * Registry pollution defense (3D)   -- idempotent discovery, no accumulation
  * Catalog contract (3E)             -- ``Catalog.describe`` (ordered, JSON-safe, no secrets)
  * Selection vs discovery (3F)       -- compatibility/compatible are side-effect free
  * Compatibility matrix (3G)         -- supported/unsupported/boundary/malformed
  * Failure semantics (3H)            -- deterministic exception types/messages
  * AST/static safety (3I)            -- no dynamic execution, no broker, no live trading
  * Fresh-process (3K)                -- subprocess determinism + no forbidden modules
  * Isolation (3L)                    -- no external consumers of the Factory
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from trading_system import strategy_factory as factory_pkg
from trading_system.strategy_factory import (
    CATALOG_SCHEMA_VERSION,
    Capabilities,
    Catalog,
    CompatibilityReport,
    DataRequirement,
    MarketContext,
    ParameterDefinition,
    ParameterSchema,
    ParameterType,
    Strategy,
    StrategyFamily,
    StrategyMetadata,
    StrategyRegistry,
    StrategyVersion,
    capabilities_from,
    clear_discovery,
    configuration_fingerprint,
    contract_schema,
    discover,
    get_strategy_class,
    is_compatible,
    registered_strategy_ids,
    strategy_identity,
    DuplicateStrategyError,
    StrategyValidationError,
)
from trading_system.strategy_factory.builtin import (
    EMACrossoverStrategy,
    RSIMeanReversionStrategy,
)
from trading_system.strategy_factory.validation import validate_metadata

FACTORY_DIR = Path(factory_pkg.__file__).resolve().parent
FACTORY_PY = sorted(p for p in FACTORY_DIR.rglob("*.py") if "__pycache__" not in str(p))
# parents of .../src/trading_system/strategy_factory:
#   [0]=strategy_factory  [1]=trading_system  [2]=src  [3]=repo_root
PY_PATH_ENTRY = str(FACTORY_DIR.parents[2])
REPO_ROOT = str(FACTORY_DIR.parents[3])
BUILTIN_MODULES = ["trading_system.strategy_factory.builtin"]
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _subprocess_env() -> dict:
    return {**os.environ, "PYTHONPATH": PY_PATH_ENTRY}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _meta(**overrides) -> StrategyMetadata:
    base = dict(
        strategy_id="stub",
        name="Stub",
        version="1.0.0",
        family=StrategyFamily.TREND,
        description="a deterministic reference",
        timeframes=["1d"],
        supported_instruments=["*"],
        required_data=["ohlcv"],
        required_indicators=["ema"],
        parameter_schema=ParameterSchema(parameters=[
            ParameterDefinition(
                name="window", type=ParameterType.INTEGER, default=20,
                minimum=2, maximum=500, step=1,
            ),
        ]),
    )
    base.update(overrides)
    return StrategyMetadata(**base)


class _StubStrategy(Strategy):
    """Concrete strategy with caller-supplied metadata + params dict.

    Subclass-agnostic: ``parameters`` returns exactly what was supplied (the
    schema fills defaults during validation), so any metadata schema can be
    paired with matching parameter values.
    """

    def __init__(self, metadata: StrategyMetadata, params: dict | None = None) -> None:
        self._meta = metadata
        self._params = dict(params or {})

    @property
    def metadata(self) -> StrategyMetadata:
        return self._meta

    @property
    def parameters(self) -> dict:
        return dict(self._params)

    def evaluate(self, state):  # pragma: no cover - not exercised by identity tests
        raise NotImplementedError


class _ParameterlessStrategy(Strategy):
    """Strategy whose metadata carries an empty parameter schema."""

    def __init__(self, params: dict | None = None) -> None:
        self._meta = EMACrossoverStrategy.metadata.model_copy(
            update={"strategy_id": "paramless", "parameter_schema": ParameterSchema()}
        )
        self._params = dict(params or {})

    @property
    def metadata(self) -> StrategyMetadata:
        return self._meta

    @property
    def parameters(self) -> dict:
        return dict(self._params)

    def evaluate(self, state):  # pragma: no cover
        raise NotImplementedError


def _fresh_catalog() -> Catalog:
    reg = StrategyRegistry()
    reg.register(EMACrossoverStrategy())
    reg.register(RSIMeanReversionStrategy())
    return Catalog(reg)


# --------------------------------------------------------------------------- #
# Phase 3A -- Canonical strategy identity
# --------------------------------------------------------------------------- #
class TestCanonicalIdentity:
    def test_strategy_identity_is_deterministic(self):
        assert strategy_identity(EMACrossoverStrategy()) == strategy_identity(EMACrossoverStrategy())

    def test_identity_is_sha256_hex(self):
        assert _HEX64.match(strategy_identity(EMACrossoverStrategy())) is not None

    def test_identity_derived_from_id_and_version_only(self):
        # Different parameter bindings -> same identity (definition unchanged).
        assert strategy_identity(EMACrossoverStrategy()) == \
            strategy_identity(EMACrossoverStrategy(fast_period=5, slow_period=20))

    def test_identity_differs_across_versions(self):
        a = strategy_identity(_StubStrategy(_meta(strategy_id="v", version="1.0.0")))
        b = strategy_identity(_StubStrategy(_meta(strategy_id="v", version="1.1.0")))
        assert a != b

    def test_identity_differs_across_ids(self):
        a = strategy_identity(_StubStrategy(_meta(strategy_id="aa")))
        b = strategy_identity(_StubStrategy(_meta(strategy_id="bb")))
        assert a != b

    def test_identity_independent_of_fingerprint(self):
        a = EMACrossoverStrategy()
        b = EMACrossoverStrategy(fast_period=5, slow_period=20)
        assert strategy_identity(a) == strategy_identity(b)
        assert configuration_fingerprint(a) != configuration_fingerprint(b)

    def test_identity_stable_across_processes(self):
        local = strategy_identity(EMACrossoverStrategy())
        script = (
            "from trading_system.strategy_factory import strategy_identity;"
            "from trading_system.strategy_factory.builtin import EMACrossoverStrategy;"
            "print(strategy_identity(EMACrossoverStrategy()))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=240, env=_subprocess_env(),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == local

    def test_no_metadata_raises_typeerror(self):
        class _NoMeta:
            metadata = ("not", "metadata")

            @property
            def parameters(self):
                return {}

        with pytest.raises(TypeError):
            strategy_identity(_NoMeta())


# --------------------------------------------------------------------------- #
# Phase 3B -- Configuration normalization
# --------------------------------------------------------------------------- #
class TestConfigurationNormalization:
    def test_int_vs_float_collapse(self):
        m = _meta(strategy_id="norm")
        assert configuration_fingerprint(_StubStrategy(m, {"window": 12})) == \
            configuration_fingerprint(_StubStrategy(m, {"window": 12.0}))

    def test_explicit_default_equals_omitted_default(self):
        m = _meta(strategy_id="norm")
        assert configuration_fingerprint(_StubStrategy(m)) == \
            configuration_fingerprint(_StubStrategy(m, {"window": 20}))

    def test_defaults_filled_deterministically(self):
        m = _meta(strategy_id="norm")
        assert configuration_fingerprint(_StubStrategy(m, {})) == \
            configuration_fingerprint(_StubStrategy(m, {"window": 20}))

    def test_different_param_values_differ(self):
        m = _meta(strategy_id="norm")
        a = configuration_fingerprint(_StubStrategy(m, {"window": 12}))
        b = configuration_fingerprint(_StubStrategy(m, {"window": 50}))
        assert a != b

    def test_no_schema_tuple_list_equivalence(self):
        assert configuration_fingerprint(_ParameterlessStrategy({"items": [1, 2, 3]})) == \
            configuration_fingerprint(_ParameterlessStrategy({"items": (1, 2, 3)}))

    def test_no_schema_nested_dict_ordering_invariant(self):
        a = _ParameterlessStrategy({"nested": {"x": 1, "y": 2}})
        b = _ParameterlessStrategy({"nested": {"y": 2, "x": 1}})
        assert configuration_fingerprint(a) == configuration_fingerprint(b)

    def test_no_schema_booleans_and_none_stable(self):
        a = _ParameterlessStrategy({"flag": True, "absent": None})
        b = _ParameterlessStrategy({"absent": None, "flag": True})
        assert configuration_fingerprint(a) == configuration_fingerprint(b)

    def test_unsupported_param_type_fails_clearly(self):
        class _Obj:
            pass

        strat = _ParameterlessStrategy({"x": _Obj()})
        with pytest.raises(TypeError):
            configuration_fingerprint(strat)

    def test_non_dict_parameters_rejected(self):
        class _BadParams(_ParameterlessStrategy):
            @property
            def parameters(self):
                return [1, 2, 3]

        with pytest.raises(TypeError):
            configuration_fingerprint(_BadParams())


# --------------------------------------------------------------------------- #
# Phase 3C -- Versioning contract
# --------------------------------------------------------------------------- #
class TestVersioningContract:
    def test_valid_version(self):
        v = StrategyVersion.parse("1.0.0")
        assert v.tuple == (1, 0, 0)
        assert str(v) == "1.0.0"

    @pytest.mark.parametrize("bad", ["1.2", "v1.0.0", "1.0", "", "abc", "1.0.a"])
    def test_invalid_version_rejected(self, bad):
        with pytest.raises((ValidationError, ValueError)):
            StrategyMetadata(
                strategy_id="stub", name="S", version=bad, family=StrategyFamily.TREND,
                description="d", timeframes=["1d"], supported_instruments=["*"],
                required_data=["ohlcv"], required_indicators=["ema"],
            )

    def test_unknown_version_fails_deterministically(self):
        reg = StrategyRegistry()
        reg.register(EMACrossoverStrategy())
        with pytest.raises(KeyError):
            reg.get("ema_crossover", "9.9.9")

    def test_duplicate_version_rejected(self):
        reg = StrategyRegistry()
        reg.register(EMACrossoverStrategy())
        with pytest.raises(DuplicateStrategyError):
            reg.register(EMACrossoverStrategy())

    def test_multiple_versions_coexist(self):
        reg = StrategyRegistry()
        m1 = _meta(strategy_id="multi", version="1.0.0")
        m2 = _meta(strategy_id="multi", version="1.1.0")
        reg.register(_StubStrategy(m1, {"window": 20}))
        reg.register(_StubStrategy(m2, {"window": 20}))
        assert reg.identities() == ["multi@1.0.0", "multi@1.1.0"]

    def test_get_returns_highest_version_when_omitted(self):
        reg = StrategyRegistry()
        reg.register(_StubStrategy(_meta(strategy_id="multi", version="1.0.0"), {"window": 20}))
        reg.register(_StubStrategy(_meta(strategy_id="multi", version="2.0.0"), {"window": 20}))
        assert reg.get("multi").metadata.version == "2.0.0"


# --------------------------------------------------------------------------- #
# Phase 3D -- Registry immutability / pollution defense
# --------------------------------------------------------------------------- #
class TestRegistryPollutionDefense:
    def test_repeated_discovery_does_not_accumulate(self):
        clear_discovery()
        discover(BUILTIN_MODULES, reload=True)
        assert discover(BUILTIN_MODULES, reload=True) == 0
        assert discover(BUILTIN_MODULES, reload=True) == 0
        assert len(registered_strategy_ids()) == 2

    def test_clear_then_discover_repopulates_deterministically(self):
        clear_discovery()
        discover(BUILTIN_MODULES, reload=True)
        ids = list(registered_strategy_ids())
        assert ids == ["ema_crossover", "rsi_mean_reversion"]
        clear_discovery()
        assert registered_strategy_ids() == []
        assert discover(BUILTIN_MODULES, reload=True) == 2
        assert list(registered_strategy_ids()) == ids

    def test_discovery_idempotent_across_many_cycles(self):
        clear_discovery()
        for _ in range(5):
            discover(BUILTIN_MODULES, reload=True)
        assert len(registered_strategy_ids()) == 2

    def test_registry_register_does_not_mutate_discovery(self):
        clear_discovery()
        reg = StrategyRegistry()
        reg.register(EMACrossoverStrategy())
        assert "ema_crossover" not in registered_strategy_ids()

    def test_fresh_process_discovery_is_deterministic(self):
        script = (
            "import json;"
            "from trading_system.strategy_factory import ("
            "  discover, registered_strategy_ids, strategy_identity, clear_discovery);"
            "clear_discovery();"
            "n=discover(['trading_system.strategy_factory.builtin'], reload=True);"
            "from trading_system.strategy_factory.builtin import EMACrossoverStrategy;"
            "print(json.dumps({'n':n,'ids':sorted(registered_strategy_ids()),"
            "'identity':strategy_identity(EMACrossoverStrategy())}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=240, env=_subprocess_env(),
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout.strip())
        assert data["n"] == 2
        assert data["ids"] == ["ema_crossover", "rsi_mean_reversion"]
        assert _HEX64.match(data["identity"])


# --------------------------------------------------------------------------- #
# Phase 3E -- Catalog contract
# --------------------------------------------------------------------------- #
class TestCatalogContract:
    def test_describe_has_schema_version(self):
        assert _fresh_catalog().describe()["schema_version"] == CATALOG_SCHEMA_VERSION

    def test_describe_is_json_serializable(self):
        json.dumps(_fresh_catalog().describe())

    def test_describe_count_matches(self):
        d = _fresh_catalog().describe()
        assert d["count"] == 2 and len(d["strategies"]) == 2

    def test_describe_ordering_is_deterministic(self):
        a = _fresh_catalog().describe()
        b = _fresh_catalog().describe()
        assert a == b
        refs = [s["reference"] for s in a["strategies"]]
        assert refs == sorted(refs)

    def test_describe_entries_have_canonical_fields(self):
        for entry in _fresh_catalog().describe()["strategies"]:
            assert _HEX64.match(entry["identity"])
            assert _HEX64.match(entry["fingerprint"])
            assert "@" in entry["reference"]
            assert "contract" in entry and "capabilities" in entry

    def test_describe_no_memory_addresses_or_internal_paths(self):
        blob = json.dumps(_fresh_catalog().describe())
        assert "0x" not in blob
        assert "object at" not in blob
        assert "__file__" not in blob
        assert "SUPABASE" not in blob
        assert "service_role" not in blob.lower()

    def test_describe_exposes_capabilities(self):
        by_id = {s["reference"].split("@")[0]: s for s in _fresh_catalog().describe()["strategies"]}
        ema = by_id["ema_crossover"]
        caps = ema["capabilities"]
        assert "ohlcv" in caps["required_data"]
        assert caps["minimum_bars"] == 26
        assert "1d" in caps["supported_timeframes"]

    def test_describe_empty_registry(self):
        d = Catalog(StrategyRegistry()).describe()
        assert d["count"] == 0 and d["strategies"] == []
        assert d["schema_version"] == CATALOG_SCHEMA_VERSION

    def test_describe_reflects_registry_read_through(self):
        reg = StrategyRegistry()
        cat = Catalog(reg)
        assert cat.describe()["count"] == 0
        reg.register(EMACrossoverStrategy())
        assert cat.describe()["count"] == 1

    def test_describe_is_read_only(self):
        cat = Catalog(StrategyRegistry())
        assert not hasattr(cat, "register")
        assert not hasattr(cat, "discover")

    def test_contract_schema_is_json_safe_and_idempotent(self):
        a = contract_schema(EMACrossoverStrategy())
        b = contract_schema(EMACrossoverStrategy())
        json.dumps(a)
        assert a == b


# --------------------------------------------------------------------------- #
# Phase 3F -- Selection vs discovery (side-effect free)
# --------------------------------------------------------------------------- #
class TestSelectionSideEffectFree:
    def test_is_compatible_does_not_mutate_strategy(self):
        s = EMACrossoverStrategy()
        before = s.parameters
        ctx = MarketContext(
            instrument="NSE:NIFTY", timeframe="1d", bar_count=300,
            data_available=[DataRequirement.OHLCV],
        )
        is_compatible(s, ctx)
        assert s.parameters == before

    def test_compatible_does_not_mutate_registry(self):
        cat = _fresh_catalog()
        ids_before = list(cat.registry.identities())
        ctx = MarketContext(
            instrument="NSE:NIFTY", timeframe="1d", bar_count=300,
            data_available=[DataRequirement.OHLCV],
        )
        cat.compatible(ctx)
        cat.compatible(ctx)
        assert list(cat.registry.identities()) == ids_before

    def test_compatible_does_not_import_forbidden_modules(self):
        cat = Catalog(StrategyRegistry())
        cat.registry.register(EMACrossoverStrategy())
        cat.registry.register(RSIMeanReversionStrategy())
        before = set(sys.modules)
        ctx = MarketContext(
            instrument="NSE:NIFTY", timeframe="1d", bar_count=300,
            data_available=[DataRequirement.OHLCV],
        )
        _ = cat.compatible(ctx)
        leaked = {m for m in (set(sys.modules) - before)
                  if m.startswith("trading_system.execution")
                  or m.startswith("trading_system.paper")}
        assert leaked == set(), f"selection imported: {leaked}"


# --------------------------------------------------------------------------- #
# Phase 3G -- Compatibility matrix
# --------------------------------------------------------------------------- #
class TestCompatibilityMatrix:
    def _ema(self):
        return EMACrossoverStrategy()

    def _ctx(self, **kw):
        base = dict(instrument="NSE:NIFTY", timeframe="1d", bar_count=300,
                    data_available=[DataRequirement.OHLCV])
        base.update(kw)
        return MarketContext(**base)

    @pytest.mark.parametrize(
        "timeframe,bar_count,available,ok",
        [
            ("1d", 300, [DataRequirement.OHLCV], True),       # fully supported
            ("1d", 26, [DataRequirement.OHLCV], True),        # boundary: exactly minimum (26)
            ("1d", 25, [DataRequirement.OHLCV], False),       # boundary: below minimum
            ("5m", 300, [DataRequirement.OHLCV], True),       # alternate supported timeframe
            ("1w", 300, [DataRequirement.OHLCV], False),      # unsupported timeframe
            ("1d", 300, [], False),                           # missing data
            ("1d", 300, [DataRequirement.OHLC], False),      # partial required data
        ],
    )
    def test_matrix(self, timeframe, bar_count, available, ok):
        rep = is_compatible(self._ema(), self._ctx(
            timeframe=timeframe, bar_count=bar_count, data_available=available))
        assert rep.compatible is ok
        assert isinstance(rep, CompatibilityReport)

    def test_missing_requirements_yield_structured_reasons(self):
        rep = is_compatible(self._ema(), self._ctx(timeframe="1w", bar_count=5, data_available=[]))
        assert rep.compatible is False
        joined = " | ".join(rep.reasons)
        assert "timeframe" in joined and "bars" in joined and "data" in joined

    def test_exact_supported_values_compatible(self):
        for tf in EMACrossoverStrategy.metadata.timeframes:
            rep = is_compatible(self._ema(), self._ctx(timeframe=tf))
            assert rep.compatible is True, tf

    def test_malformed_context_rejected(self):
        with pytest.raises((ValidationError, ValueError)):
            MarketContext(instrument="", timeframe="1d", bar_count=300)
        with pytest.raises(ValidationError):
            MarketContext(instrument="X", timeframe="1x", bar_count=300)


# --------------------------------------------------------------------------- #
# Phase 3H -- Failure semantics
# --------------------------------------------------------------------------- #
class TestFailureSemantics:
    def test_strategy_not_found_keyerror(self):
        with pytest.raises(KeyError):
            get_strategy_class("nonexistent")
        with pytest.raises(KeyError):
            StrategyRegistry().get("nonexistent")

    def test_version_not_found_keyerror(self):
        reg = StrategyRegistry()
        reg.register(EMACrossoverStrategy())
        with pytest.raises(KeyError):
            reg.get("ema_crossover", "9.9.9")

    def test_duplicate_registration_duplicateerror(self):
        reg = StrategyRegistry()
        reg.register(EMACrossoverStrategy())
        with pytest.raises(DuplicateStrategyError):
            reg.register(EMACrossoverStrategy())

    def test_invalid_configuration_valueerror(self):
        with pytest.raises(ValueError):
            EMACrossoverStrategy(fast_period=30, slow_period=20)

    def test_invalid_parameter_value_validation_error(self):
        with pytest.raises(StrategyValidationError):
            EMACrossoverStrategy(fast_period=1)  # below minimum (2)

    def test_malformed_metadata_flagged_by_validator(self):
        meta = StrategyMetadata.model_construct(
            strategy_id="stub", name="S", version="1.0.0",
            family=StrategyFamily.TREND, description="d",
            timeframes=["1d"], supported_instruments=["*"],
            required_data=[], required_indicators=["ema"],
        )
        errors = validate_metadata(meta)
        assert any("required_data" in e for e in errors)

    def test_registry_rejects_invalid_strategy(self):
        with pytest.raises(StrategyValidationError):
            StrategyRegistry().register(_StubStrategy(_meta(description="")))

    def test_missing_metadata_attribute_raises(self):
        class _Bare:
            @property
            def parameters(self):
                return {}

        with pytest.raises((AttributeError, TypeError)):
            configuration_fingerprint(_Bare())


# --------------------------------------------------------------------------- #
# Phase 3I -- AST / static safety
# --------------------------------------------------------------------------- #
FORBIDDEN_CALLS = {
    "eval", "exec", "compile", "__import__", "globals", "locals",
    "getattr", "setattr", "vars", "breakpoint", "open", "system", "popen",
    "marshal", "pickle",
}
FORBIDDEN_IMPORT_TOKENS = (
    "execution", "fyers", "upstox", "paper", "india", "socket",
    "http", "requests", "subprocess", "ctypes", "marshal", "pickle",
    "os.system",
)
FORBIDDEN_BROKER_METHODS = {
    "place_order", "submit", "cancel_order", "modify_order",
    "connect", "login", "authenticate",
}


def _call_names(tree: ast.Module) -> list[str]:
    return [node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]


class TestStaticSafety:
    def test_no_forbidden_dynamic_execution(self):
        offenders = [(p.name, n) for p in FACTORY_PY
                     for n in _call_names(ast.parse(p.read_text(encoding="utf-8"), filename=str(p)))
                     if n in FORBIDDEN_CALLS]
        assert not offenders, f"forbidden calls: {offenders}"

    def test_no_forbidden_imports(self):
        offenders = []
        for p in FACTORY_PY:
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        low = alias.name.lower()
                        for tok in FORBIDDEN_IMPORT_TOKENS:
                            if tok in low:
                                offenders.append((p.name, alias.name, tok))
                elif isinstance(node, ast.ImportFrom):
                    module = (node.module or "").lower()
                    for tok in FORBIDDEN_IMPORT_TOKENS:
                        if tok in module:
                            offenders.append((p.name, node.module, tok))
        assert not offenders, f"forbidden imports: {offenders}"

    def test_no_forbidden_broker_methods(self):
        offenders = []
        for p in FACTORY_PY:
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in FORBIDDEN_BROKER_METHODS:
                        offenders.append((p.name, node.func.attr))
        assert not offenders, f"broker methods: {offenders}"

    def test_no_hardcoded_secrets(self):
        for p in FACTORY_PY:
            text = p.read_text(encoding="utf-8")
            assert "SUPABASE_SERVICE_ROLE_KEY" not in text
            assert "service_role" not in text.lower()

    def test_importlib_confined_to_discovery(self):
        users = [p.name for p in FACTORY_PY
                 if "importlib" in p.read_text(encoding="utf-8")]
        assert users == ["discovery.py"], users


# --------------------------------------------------------------------------- #
# Phase 3K -- fresh-process end-to-end determinism
# --------------------------------------------------------------------------- #
class TestFreshProcessEndToEnd:
    def test_fresh_process_discovery_deterministic(self):
        script = (
            "import json;"
            "from trading_system.strategy_factory import ("
            "  discover, registered_strategy_ids, strategy_identity, clear_discovery);"
            "clear_discovery();"
            "n=discover(['trading_system.strategy_factory.builtin'], reload=True);"
            "from trading_system.strategy_factory.builtin import EMACrossoverStrategy;"
            "print(json.dumps({'n':n,'ids':sorted(registered_strategy_ids()),"
            "'identity':strategy_identity(EMACrossoverStrategy())}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=240, env=_subprocess_env(),
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout.strip())
        assert data["n"] == 2
        assert data["ids"] == ["ema_crossover", "rsi_mean_reversion"]
        assert _HEX64.match(data["identity"])

    def test_fresh_process_identity_matches_in_process(self):
        in_proc = strategy_identity(EMACrossoverStrategy())
        script = (
            "from trading_system.strategy_factory import strategy_identity;"
            "from trading_system.strategy_factory.builtin import EMACrossoverStrategy;"
            "print(strategy_identity(EMACrossoverStrategy()))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=240, env=_subprocess_env(),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == in_proc

    def test_fresh_process_loads_no_broker_or_paper_modules(self):
        # Mirrors tests/test_strategy_factory_integration.py: importing the
        # Factory may transitively load the `india` package (incl. its
        # fyers/upstox wrappers -- documented, network-free) but must never load
        # the execution or paper-trading broker/ordering layers.
        script = (
            "import sys, trading_system.strategy_factory;"
            "bad=[m for m in sys.modules"
            " if m.startswith('trading_system.execution')"
            " or m.startswith('trading_system.paper')];"
            "print(','.join(sorted(bad)))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=240, env=_subprocess_env(),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "", f"broker/paper modules loaded: {result.stdout!r}"


# --------------------------------------------------------------------------- #
# Phase 3L -- isolation: no external consumers
# --------------------------------------------------------------------------- #
class TestNoExternalConsumers:
    def test_backend_does_not_reference_strategy_factory(self):
        backend = Path(REPO_ROOT) / "backend"
        hits = [str(p) for p in backend.rglob("*.py")
                if "strategy_factory" in p.read_text(encoding="utf-8")]
        assert hits == [], f"unexpected backend consumer: {hits}"
