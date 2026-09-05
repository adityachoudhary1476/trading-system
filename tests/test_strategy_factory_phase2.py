"""Phase 2 tests for the Strategy Factory.

Covers the new contract/capability layer:
  * Capabilities (typed, validated, serializable)
  * Compatibility (pure, side-effect-free)
  * Configuration fingerprint (deterministic, normalized)
  * Stable contract serialization (contract_schema)
  * Catalog read-only introspection
  * Registration semantics (duplicate / version / overwrite)
  * Discovery lifecycle (clear / reload / idempotency)
plus regression coverage of the existing parameter-validation contract.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from trading_system.strategy_factory import (
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
    capabilities_from,
    clear_discovery,
    configuration_fingerprint,
    contract_schema,
    discover,
    get_strategy_class,
    is_compatible,
    registered_strategy_ids,
    DuplicateStrategyError,
)
from trading_system.strategy_factory.builtin import (
    EMACrossoverStrategy,
    RSIMeanReversionStrategy,
)


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
        author="tester",
        tags=["t1"],
        parameter_schema=ParameterSchema(parameters=[
            ParameterDefinition(name="window", type=ParameterType.INTEGER, default=20, minimum=2, maximum=500),
        ]),
    )
    base.update(overrides)
    return StrategyMetadata(**base)


class _StubStrategy(Strategy):
    """Minimal concrete strategy driven by an injected metadata + params dict."""

    def __init__(self, metadata: StrategyMetadata, params: dict | None = None) -> None:
        self._meta = metadata
        self._params = dict(params or {})

    @property
    def metadata(self) -> StrategyMetadata:
        return self._meta

    @property
    def parameters(self) -> dict:
        return dict(self._params)

    def evaluate(self, state):
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Phase 2 -- Capability declarations
# --------------------------------------------------------------------------- #
class TestCapabilities:
    def test_valid_declaration(self):
        c = Capabilities(
            required_data=["ohlcv"],
            required_indicators=["ema"],
            supported_timeframes=["1d"],
            minimum_bars=26,
        )
        d = c.to_dict()
        assert d["required_data"] == ["ohlcv"]
        assert d["required_indicators"] == ["ema"]
        assert d["supported_timeframes"] == ["1d"]
        assert d["minimum_bars"] == 26

    def test_from_metadata_preserves_contract(self):
        c = Capabilities.from_metadata(EMACrossoverStrategy.metadata)
        assert DataRequirement.OHLCV in c.required_data
        assert c.required_indicators == ["ema"]
        assert "1d" in c.supported_timeframes
        assert c.minimum_bars == 26

    def test_invalid_data_requirement_rejected(self):
        with pytest.raises(ValidationError):
            Capabilities(required_data=["bogus"])

    def test_invalid_timeframe_rejected(self):
        with pytest.raises(ValidationError):
            Capabilities(supported_timeframes=["1x"])

    def test_negative_minimum_bars_rejected(self):
        with pytest.raises(ValidationError):
            Capabilities(minimum_bars=-1)

    def test_indicators_normalized_dedup_sorted(self):
        c = Capabilities(required_indicators=["rsi", "ema", "ema"])
        assert c.required_indicators == ["ema", "rsi"]

    def test_timeframes_normalized_dedup_sorted(self):
        c = Capabilities(supported_timeframes=["1d", "5m", "1d"])
        assert c.supported_timeframes == ["1d", "5m"]

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            Capabilities(required_data=["ohlcv"], bogus=True)


# --------------------------------------------------------------------------- #
# Phase 3 -- Compatibility checking
# --------------------------------------------------------------------------- #
class TestCompatibility:
    def _ema(self) -> EMACrossoverStrategy:
        return EMACrossoverStrategy()

    def _ctx(self, **kw):
        base = dict(
            instrument="NSE:NIFTY",
            timeframe="1d",
            bar_count=300,
            data_available=[DataRequirement.OHLCV],
        )
        base.update(kw)
        return MarketContext(**base)

    def test_compatible(self):
        rep = is_compatible(self._ema(), self._ctx())
        assert isinstance(rep, CompatibilityReport)
        assert rep.compatible is True
        assert rep.reasons == []

    def test_unsupported_timeframe_incompatible(self):
        rep = is_compatible(self._ema(), self._ctx(timeframe="1w"))
        assert rep.compatible is False
        assert any("timeframe" in r for r in rep.reasons)

    def test_insufficient_bars_incompatible(self):
        rep = is_compatible(self._ema(), self._ctx(bar_count=5))  # min 26
        assert rep.compatible is False
        assert any("bars" in r for r in rep.reasons)

    def test_missing_data_incompatible(self):
        rep = is_compatible(self._ema(), self._ctx(data_available=[]))
        assert rep.compatible is False
        assert any("data" in r for r in rep.reasons)

    def test_compatible_accepts_dict_context(self):
        rep = is_compatible(
            self._ema(),
            {"instrument": "NSE:NIFTY", "timeframe": "1d", "bar_count": 300, "data_available": ["ohlcv"]},
        )
        assert rep.compatible is True

    def test_compatible_accepts_capabilities_directly(self):
        caps = Capabilities.from_metadata(EMACrossoverStrategy.metadata)
        assert is_compatible(caps, self._ctx()).compatible is True

    def test_compatible_accepts_metadata_directly(self):
        assert is_compatible(EMACrossoverStrategy.metadata, self._ctx()).compatible is True

    def test_rsi_requires_ohlcv(self):
        # RSI mean-reversion declares ohlcv/data + rsi indicator
        rep = is_compatible(RSIMeanReversionStrategy(), self._ctx(data_available=[]))
        assert rep.compatible is False
        assert any("data" in r for r in rep.reasons)

    def test_compatibility_is_pure_and_side_effect_free(self):
        s = self._ema()
        before = s.parameters
        ctx = self._ctx(timeframe="5m", bar_count=300)
        # 5m is supported by EMA; should be compatible
        assert is_compatible(s, ctx).compatible is True
        assert s.parameters == before  # strategy not mutated


# --------------------------------------------------------------------------- #
# Phase 5 -- Configuration fingerprint
# --------------------------------------------------------------------------- #
class TestFingerprint:
    def test_deterministic(self):
        s = EMACrossoverStrategy()
        assert configuration_fingerprint(s) == configuration_fingerprint(EMACrossoverStrategy())

    def test_is_sha256_hex(self):
        fp = configuration_fingerprint(EMACrossoverStrategy())
        assert len(fp) == 64
        int(fp, 16)  # valid hexadecimal

    def test_order_independent(self):
        m = _meta(strategy_id="ord")
        a = _StubStrategy(m, {"window": 20})
        b = _StubStrategy(m, {"window": 20})
        assert configuration_fingerprint(a) == configuration_fingerprint(b)

    def test_type_normalization_int_vs_float(self):
        m = _meta(strategy_id="norm")
        a = _StubStrategy(m, {"window": 12})
        b = _StubStrategy(m, {"window": 12.0})
        assert configuration_fingerprint(a) == configuration_fingerprint(b)

    def test_different_id_differs(self):
        a = _StubStrategy(_meta(strategy_id="aa"), {"window": 20})
        b = _StubStrategy(_meta(strategy_id="bb"), {"window": 20})
        assert configuration_fingerprint(a) != configuration_fingerprint(b)

    def test_different_version_differs(self):
        a = _StubStrategy(_meta(strategy_id="v", version="1.0.0"), {"window": 20})
        b = _StubStrategy(_meta(strategy_id="v", version="1.1.0"), {"window": 20})
        assert configuration_fingerprint(a) != configuration_fingerprint(b)

    def test_different_params_differs(self):
        m = _meta(strategy_id="p")
        assert configuration_fingerprint(_StubStrategy(m, {"window": 12})) != \
            configuration_fingerprint(_StubStrategy(m, {"window": 50}))

    def test_json_safe(self):
        json.dumps({"fingerprint": configuration_fingerprint(EMACrossoverStrategy())})


# --------------------------------------------------------------------------- #
# Phase 6 -- Stable contract serialization
# --------------------------------------------------------------------------- #
class TestSerialization:
    def test_contract_schema_shape(self):
        s = EMACrossoverStrategy()
        schema = contract_schema(s)
        assert schema["strategy_id"] == "ema_crossover"
        assert schema["version"] == "1.0.0"
        assert schema["family"] == "trend"
        entry = schema["parameters"]["fast_period"]
        assert entry["type"] == "integer"
        assert entry["minimum"] == 2
        assert entry["default"] == 12

    def test_contract_schema_json_serializable(self):
        json.dumps(contract_schema(EMACrossoverStrategy()))

    def test_deterministic_key_order(self):
        a = contract_schema(EMACrossoverStrategy())
        b = contract_schema(EMACrossoverStrategy())
        assert a == b
        assert list(a.keys()) == list(b.keys())

    def test_no_mutation_of_source(self):
        s = EMACrossoverStrategy()
        before = s.metadata.model_dump(mode="json")
        _ = contract_schema(s)
        _ = configuration_fingerprint(s)
        after = s.metadata.model_dump(mode="json")
        assert before == after

    def test_parameters_keyed_by_name(self):
        schema = contract_schema(EMACrossoverStrategy())
        assert isinstance(schema["parameters"], dict)
        for name, entry in schema["parameters"].items():
            assert isinstance(name, str)
            assert entry["type"] in {"integer", "float", "boolean", "enum", "string"}


# --------------------------------------------------------------------------- #
# Phase 4 + 7 -- Catalog read-only introspection
# --------------------------------------------------------------------------- #
class TestCatalog:
    def _reg(self) -> StrategyRegistry:
        reg = StrategyRegistry()
        reg.register(EMACrossoverStrategy())
        reg.register(RSIMeanReversionStrategy())
        return reg

    def test_list_get_exists(self):
        cat = Catalog(self._reg())
        ids = {s.metadata.strategy_id for s in cat.list()}
        assert ids == {"ema_crossover", "rsi_mean_reversion"}
        assert cat.exists("ema_crossover")
        assert cat.get("ema_crossover").metadata.strategy_id == "ema_crossover"
        assert cat.count == 2

    def test_versions_sorted(self):
        reg = self._reg()
        base = EMACrossoverStrategy.metadata.to_dict()
        base["version"] = "1.1.0"
        reg.register(_StubStrategy(StrategyMetadata(**base)))
        versions = Catalog(reg).versions("ema_crossover")
        assert versions == ["1.0.0", "1.1.0"]

    def test_metadata_accessor(self):
        cat = Catalog(self._reg())
        meta = cat.metadata("ema_crossover")
        assert meta.strategy_id == "ema_crossover"
        assert meta.version == "1.0.0"

    def test_compatible_filter_selects_subset(self):
        cat = Catalog(self._reg())
        ctx = MarketContext(instrument="NSE:NIFTY", timeframe="1d", bar_count=300,
                            data_available=[DataRequirement.OHLCV])
        assert {s.metadata.strategy_id for s in cat.compatible(ctx)} == {"ema_crossover", "rsi_mean_reversion"}

    def test_compatible_filter_excludes_incompatible(self):
        cat = Catalog(self._reg())
        ctx = MarketContext(instrument="NSE:NIFTY", timeframe="1d", bar_count=5,
                            data_available=[DataRequirement.OHLCV])
        assert cat.compatible(ctx) == []  # both need >= 26/14 bars

    def test_read_only_reflects_registry(self):
        reg = StrategyRegistry()
        cat = Catalog(reg)
        assert cat.list() == []
        reg.register(EMACrossoverStrategy())
        assert len(cat.list()) == 1
        assert not hasattr(cat, "register")

    def test_missing_version_raises(self):
        cat = Catalog(self._reg())
        with pytest.raises(KeyError):
            cat.get("ema_crossover", version="9.9.9")


# --------------------------------------------------------------------------- #
# Phase 8 -- Registration semantics
# --------------------------------------------------------------------------- #
class TestRegistrationSemantics:
    def test_same_identity_same_content_rejected(self):
        reg = StrategyRegistry()
        reg.register(EMACrossoverStrategy())
        with pytest.raises(DuplicateStrategyError):
            reg.register(EMACrossoverStrategy())

    def test_same_identity_different_content_rejected(self):
        reg = StrategyRegistry()
        m = _meta(strategy_id="dup")
        reg.register(_StubStrategy(m, {"window": 20}))
        with pytest.raises(DuplicateStrategyError):
            reg.register(_StubStrategy(m, {"window": 50}))

    def test_different_version_allowed(self):
        reg = StrategyRegistry()
        reg.register(EMACrossoverStrategy())
        base = EMACrossoverStrategy.metadata.to_dict()
        base["version"] = "1.1.0"
        reg.register(_StubStrategy(StrategyMetadata(**base)))
        assert reg.count == 2
        assert "ema_crossover@1.1.0" in reg.identities()

    def test_overwrite_replaces(self):
        reg = StrategyRegistry()
        m = _meta(strategy_id="ow")
        reg.register(_StubStrategy(m, {"window": 20}))
        reg.register(_StubStrategy(m, {"window": 50}), overwrite=True)
        assert reg.count == 1

    def test_decorated_discovery_is_idempotent(self):
        # Same class registered twice via the discovery decorator is a no-op.
        clear_discovery()
        first = discover(["trading_system.strategy_factory.builtin"], reload=True)
        second = discover(["trading_system.strategy_factory.builtin"], reload=True)
        assert first == 2
        assert second == 0  # no duplicates
        assert len(registered_strategy_ids()) == 2


# --------------------------------------------------------------------------- #
# Phase 9 -- Discovery lifecycle
# --------------------------------------------------------------------------- #
class TestDiscoveryLifecycle:
    def setup_method(self):
        clear_discovery()

    def test_clear_then_discover_repopulates(self):
        added = discover(["trading_system.strategy_factory.builtin"], reload=True)
        ids = registered_strategy_ids()
        assert added == 2
        assert "ema_crossover" in ids and "rsi_mean_reversion" in ids
        assert get_strategy_class("ema_crossover") is EMACrossoverStrategy

    def test_clear_discover_clear_discover(self):
        discover(["trading_system.strategy_factory.builtin"], reload=True)
        assert len(registered_strategy_ids()) == 2
        clear_discovery()
        assert registered_strategy_ids() == []
        added = discover(["trading_system.strategy_factory.builtin"], reload=True)
        assert added == 2
        assert len(registered_strategy_ids()) == 2

    def test_repeated_discovery_no_corruption(self):
        discover(["trading_system.strategy_factory.builtin"], reload=True)
        second = discover(["trading_system.strategy_factory.builtin"], reload=True)
        assert second == 0
        ids = registered_strategy_ids()
        assert len(ids) == 2
        assert get_strategy_class("ema_crossover") is EMACrossoverStrategy
        assert get_strategy_class("rsi_mean_reversion") is RSIMeanReversionStrategy

    def test_reload_preserves_class_identity(self):
        discover(["trading_system.strategy_factory.builtin"], reload=True)
        clear_discovery()
        discover(["trading_system.strategy_factory.builtin"], reload=True)
        assert get_strategy_class("ema_crossover") is EMACrossoverStrategy
        assert get_strategy_class("rsi_mean_reversion") is RSIMeanReversionStrategy
        assert {
            get_strategy_class(sid).metadata.version
            for sid in ("ema_crossover", "rsi_mean_reversion")
        } == {"1.0.0"}

    def test_invalid_module_raises_discovery_error(self):
        from trading_system.strategy_factory.discovery import DiscoveryError
        clear_discovery()
        with pytest.raises(DiscoveryError):
            discover(["trading_system.strategy_factory.does_not_exist"], reload=True)


# --------------------------------------------------------------------------- #
# Phase 10 -- Parameter validation regression
# --------------------------------------------------------------------------- #
class TestParameterRegression:
    def test_required_with_no_default_valid(self):
        p = ParameterDefinition(name="window", type=ParameterType.INTEGER, default=None, required=True)
        assert p.required is True and p.default is None

    def test_missing_required_value_flagged(self):
        p = ParameterDefinition(name="window", type=ParameterType.INTEGER, default=None, required=True)
        from trading_system.strategy_factory.validation import validate_parameter_values
        errors = validate_parameter_values({}, ParameterSchema(parameters=[p]))
        assert any("required" in e for e in errors)

    def test_min_max_bounds(self):
        p = ParameterDefinition(name="x", type=ParameterType.INTEGER, default=20, minimum=2, maximum=500)
        from trading_system.strategy_factory.exceptions import StrategyValidationError
        with pytest.raises(StrategyValidationError):
            p.validate_value(1)   # below min
        with pytest.raises(StrategyValidationError):
            p.validate_value(999)  # above max

    def test_bool_rejected_for_int(self):
        from trading_system.strategy_factory.exceptions import StrategyValidationError
        p = ParameterDefinition(name="x", type=ParameterType.INTEGER, default=20)
        with pytest.raises(StrategyValidationError):
            p.validate_value(True)

    def test_integer_coerces_float(self):
        p = ParameterDefinition(name="x", type=ParameterType.INTEGER, default=20)
        assert p.validate_value(5.0) == 5

    def test_unknown_param_rejected(self):
        from trading_system.strategy_factory.validation import validate_parameter_values
        s = ParameterSchema(parameters=[ParameterDefinition(name="x", type=ParameterType.INTEGER, default=1)])
        errors = validate_parameter_values({"bogus": 2}, s)
        assert any("unknown parameter" in e for e in errors)

    def test_fingerprint_normalization_regression(self):
        # Semantically-equivalent param representations hash identically.
        m = _meta(strategy_id="norm")
        a = _StubStrategy(m, {"window": 20})
        b = _StubStrategy(m, {"window": 20.0})
        assert configuration_fingerprint(a) == configuration_fingerprint(b)
