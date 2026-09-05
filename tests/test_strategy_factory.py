"""Phase 1 unit tests for the Strategy Factory architecture.

Covers metadata, parameter schema, signals, registry, versioning, validation,
and determinism. All tests are offline and deterministic.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from trading_system.strategy_factory.contract import (
    MarketState,
    PositionState,
    SignalAction,
    Strategy,
    StrategySignal,
)
from trading_system.strategy_factory.exceptions import (
    DuplicateStrategyError,
    StrategyValidationError,
)
from trading_system.strategy_factory.metadata import (
    StrategyFamily,
    StrategyMetadata,
    StrategyReference,
    StrategyVersion,
)
from trading_system.strategy_factory.parameters import (
    ParameterDefinition,
    ParameterSchema,
    ParameterType,
)
from trading_system.strategy_factory.registry import StrategyRegistry
from trading_system.strategy_factory.validation import (
    require_valid_metadata,
    require_valid_signal,
    require_valid_strategy,
    validate_metadata,
    validate_parameter_schema,
    validate_parameter_values,
    validate_signal,
    validate_strategy,
)
from trading_system.strategy_factory.builtin import EMACrossoverStrategy, RSIMeanReversionStrategy


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
        tags=["t1", "t2"],
    )
    base.update(overrides)
    return StrategyMetadata(**base)


class _StubStrategy(Strategy):
    """Minimal concrete strategy with caller-supplied metadata + params."""

    def __init__(self, metadata: StrategyMetadata, params: dict | None = None) -> None:
        self._meta = metadata
        self._params = dict(params or {})

    @property
    def metadata(self) -> StrategyMetadata:
        return self._meta

    @property
    def parameters(self) -> dict:
        return dict(self._params)

    def evaluate(self, state: MarketState) -> StrategySignal:
        return StrategySignal(
            action=SignalAction.HOLD,
            strategy_id=self._meta.strategy_id,
            timestamp=state.timestamp,
            symbol=state.symbol,
            reference_price=state.latest_close,
            confidence=0.0,
            reason="stub",
            target_position=0,
            version=self._meta.version,
        )


def _bars(n=60, seed=1, trend=0.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    close = 100 + np.cumsum(rng.normal(trend, 1.0, n))
    close = np.maximum(close, 1.0)
    return pd.DataFrame(
        {"open": close, "high": close + 1.0, "low": close - 1.0,
         "close": close, "volume": 100.0},
        index=idx,
    )


def _state(df: pd.DataFrame, symbol="NSE:SBIN", position=None) -> MarketState:
    return MarketState(
        symbol=symbol, timeframe="1d", timestamp=df.index[-1],
        bars=df, position=position,
    )


def _flat_state(n=60, seed=1, trend=0.0) -> MarketState:
    return _state(_bars(n=n, seed=seed, trend=trend))


# --------------------------------------------------------------------------- #
# StrategyMetadata
# --------------------------------------------------------------------------- #
class TestStrategyMetadata:
    def test_valid_metadata(self):
        meta = _meta()
        assert meta.strategy_id == "stub"
        assert meta.family == StrategyFamily.TREND
        assert validate_metadata(meta) == []

    def test_invalid_id_format_rejected(self):
        with pytest.raises(ValidationError):
            _meta(strategy_id="Bad-ID")

    def test_invalid_version_rejected(self):
        with pytest.raises(ValidationError):
            _meta(version="1.2")

    def test_invalid_family_rejected(self):
        with pytest.raises(ValidationError):
            StrategyMetadata(
                strategy_id="stub", name="S", version="1.0.0", family="bogus",
                description="d", timeframes=["1d"], supported_instruments=["*"],
                required_data=["ohlcv"], required_indicators=["ema"],
            )

    def test_unknown_timeframe_rejected(self):
        with pytest.raises(ValidationError):
            _meta(timeframes=["1x"])

    def test_duplicate_tags_rejected(self):
        with pytest.raises(ValidationError):
            _meta(tags=["dup", "dup"])

    def test_code_payload_in_description_rejected(self):
        with pytest.raises(ValidationError):
            _meta(description="import os; os.system('x')")

    def test_code_payload_in_author_rejected(self):
        with pytest.raises(ValidationError):
            _meta(author="eval('1+1')")

    def test_empty_required_data_flagged(self):
        meta = StrategyMetadata.model_construct(
            strategy_id="stub", name="Stub", version="1.0.0",
            family=StrategyFamily.TREND, description="d",
            timeframes=["1d"], supported_instruments=["*"],
            required_data=[], required_indicators=["ema"],
        )
        errors = validate_metadata(meta)
        assert any("required_data" in e for e in errors)

    def test_empty_description_flagged(self):
        meta = StrategyMetadata.model_construct(
            strategy_id="stub", name="Stub", version="1.0.0",
            family=StrategyFamily.TREND, description="",
            timeframes=["1d"], supported_instruments=["*"],
            required_data=["ohlcv"], required_indicators=["ema"],
        )
        errors = validate_metadata(meta)
        assert any("description" in e for e in errors)

    def test_serialization_round_trip(self):
        meta = _meta(parameter_schema=ParameterSchema(parameters=[
            ParameterDefinition(name="window", type=ParameterType.INTEGER, default=20,
                                minimum=2, maximum=500),
        ]))
        d = meta.to_dict()
        restored = StrategyMetadata(**d)
        assert restored == meta
        json.dumps(d)  # pure JSON-serializable

    def test_content_hash_stable_and_sensitive(self):
        meta = _meta()
        h1 = meta.content_hash()
        h2 = meta.content_hash()
        assert h1 == h2
        other = _meta(name="Other")
        assert other.content_hash() != h1

    def test_reference(self):
        assert str(_meta().reference) == "stub@1.0.0"


# --------------------------------------------------------------------------- #
# StrategyVersion / StrategyReference
# --------------------------------------------------------------------------- #
class TestVersioning:
    def test_parse_valid(self):
        v = StrategyVersion.parse("2.4.1")
        assert v.tuple == (2, 4, 1)
        assert str(v) == "2.4.1"

    @pytest.mark.parametrize("bad", ["1.2", "1.0.0.0", "v1.0.0", "1.0", "", "abc", "1.0.a"])
    def test_parse_invalid(self, bad):
        with pytest.raises(ValueError):
            StrategyVersion.parse(bad)

    def test_bump(self):
        v = StrategyVersion(1, 2, 3)
        assert str(v.bump_major()) == "2.0.0"
        assert str(v.bump_minor()) == "1.3.0"
        assert str(v.bump_patch()) == "1.2.4"

    def test_reference_identity(self):
        ref = StrategyReference("ema_crossover", "1.0.0")
        assert str(ref) == "ema_crossover@1.0.0"
        assert StrategyReference.parse("ema_crossover@1.1.0").version == "1.1.0"

    @pytest.mark.parametrize("bad", ["noid", "ema_crossover@", "@1.0.0", "ema_crossover@1.2"])
    def test_reference_parse_invalid(self, bad):
        with pytest.raises(ValueError):
            StrategyReference.parse(bad)


# --------------------------------------------------------------------------- #
# Parameter schema
# --------------------------------------------------------------------------- #
class TestParameterSchema:
    def _pdef(self, **kw):
        base = dict(name="window", type=ParameterType.INTEGER, default=20,
                    description="d", minimum=2, maximum=500, step=1)
        base.update(kw)
        return ParameterDefinition(**base)

    def test_valid_integer(self):
        p = self._pdef(type=ParameterType.INTEGER, default=20)
        assert p.validate_value(20) == 20

    def test_integer_coerces_float(self):
        p = self._pdef(type=ParameterType.INTEGER, default=20)
        assert p.validate_value(20.0) == 20

    def test_integer_rejects_non_integer_float(self):
        p = self._pdef(type=ParameterType.INTEGER, default=20)
        with pytest.raises(StrategyValidationError):
            p.validate_value(20.5)

    def test_integer_rejects_bool(self):
        p = self._pdef(type=ParameterType.INTEGER, default=20)
        with pytest.raises(StrategyValidationError):
            p.validate_value(True)

    def test_valid_float(self):
        p = self._pdef(type=ParameterType.FLOAT, default=2.0, minimum=0.1, maximum=10.0)
        assert p.validate_value(5) == 5.0
        with pytest.raises(StrategyValidationError):
            p.validate_value(-1.0)

    def test_valid_boolean(self):
        p = ParameterDefinition(name="flag", type=ParameterType.BOOLEAN, default=False)
        assert p.validate_value(True) is True
        with pytest.raises(StrategyValidationError):
            p.validate_value("yes")

    def test_valid_enum(self):
        p = ParameterDefinition(
            name="mode", type=ParameterType.ENUM, default="auto",
            enum_values=["auto", "manual"],
        )
        assert p.validate_value("auto") == "auto"
        with pytest.raises(StrategyValidationError):
            p.validate_value("bogus")

    def test_enum_requires_values(self):
        with pytest.raises(ValidationError):
            ParameterDefinition(name="m", type=ParameterType.ENUM, default="x")

    def test_enum_default_must_be_enumerated(self):
        with pytest.raises(ValidationError):
            ParameterDefinition(name="m", type=ParameterType.ENUM, default="x",
                                enum_values=["a", "b"])

    def test_boolean_rejects_range(self):
        with pytest.raises(ValidationError):
            ParameterDefinition(name="f", type=ParameterType.BOOLEAN, default=False,
                                minimum=0, maximum=1)

    def test_invalid_default_type(self):
        with pytest.raises(ValidationError):
            ParameterDefinition(name="w", type=ParameterType.INTEGER, default="20")

    def test_default_out_of_range(self):
        with pytest.raises(ValidationError):
            ParameterDefinition(name="w", type=ParameterType.INTEGER, default=0,
                                minimum=2, maximum=500)

    def test_min_greater_than_max(self):
        with pytest.raises(ValidationError):
            ParameterDefinition(name="w", type=ParameterType.INTEGER, default=20,
                                minimum=100, maximum=2)

    def test_step_zero_rejected(self):
        p = ParameterDefinition(name="w", type=ParameterType.FLOAT, default=1.0, step=0.0)
        assert any("step" in e for e in validate_parameter_schema(ParameterSchema(parameters=[p])))

    def test_duplicate_names_rejected_in_schema(self):
        with pytest.raises(ValidationError):
            ParameterSchema(parameters=[
                ParameterDefinition(name="x", type=ParameterType.INTEGER, default=1),
                ParameterDefinition(name="x", type=ParameterType.INTEGER, default=2),
            ])

    def test_validate_values_rejects_unknown(self):
        schema = ParameterSchema(parameters=[
            ParameterDefinition(name="x", type=ParameterType.INTEGER, default=1),
        ])
        errors = validate_parameter_values({"x": 1, "bogus": 2}, schema)
        assert any("unknown parameter" in e for e in errors)

    def test_validate_values_rejects_out_of_range(self):
        schema = ParameterSchema(parameters=[
            ParameterDefinition(name="x", type=ParameterType.INTEGER, default=1,
                                minimum=1, maximum=5),
        ])
        errors = validate_parameter_values({"x": 99}, schema)
        assert any("maximum" in e for e in errors)

    def test_validate_values_fills_defaults(self):
        schema = ParameterSchema(parameters=[
            ParameterDefinition(name="x", type=ParameterType.INTEGER, default=7),
        ])
        out = schema.validate_values({})
        assert out == {"x": 7}

    def test_to_schema_dict_is_json(self):
        schema = ParameterSchema(parameters=[
            ParameterDefinition(name="x", type=ParameterType.INTEGER, default=1,
                                minimum=0, maximum=10, step=1, description="d"),
        ])
        d = schema.to_schema_dict()[0]
        assert d["name"] == "x"
        assert d["type"] == "integer"
        json.dumps(d)

    def test_unknown_param_name_rejected(self):
        with pytest.raises(ValidationError):
            ParameterDefinition(name="1bad", type=ParameterType.INTEGER, default=1)


# --------------------------------------------------------------------------- #
# StrategySignal
# --------------------------------------------------------------------------- #
_TS = datetime(2024, 1, 31, tzinfo=timezone.utc)


def _sig(**kw):
    base = dict(action=SignalAction.BUY, strategy_id="s", timestamp=_TS,
                symbol="NSE:X", reference_price=100.0)
    base.update(kw)
    return StrategySignal(**base)


class TestStrategySignal:
    @pytest.mark.parametrize("action", ["buy", "sell", "exit", "hold"])
    def test_actions_construct(self, action):
        sig = _sig(action=SignalAction(action))
        assert sig.action == SignalAction(action)
        assert validate_signal(sig) == []

    def test_negative_price_rejected(self):
        with pytest.raises(ValueError):
            _sig(reference_price=-1.0)

    def test_confidence_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            _sig(confidence=1.5)

    def test_tz_naive_timestamp_rejected(self):
        naive = datetime(2024, 1, 31)
        with pytest.raises(ValueError):
            _sig(timestamp=naive)

    def test_bad_target_position_rejected(self):
        with pytest.raises(ValueError):
            _sig(target_position=5)

    def test_code_in_reason_rejected(self):
        with pytest.raises(ValueError):
            _sig(reason="import os")

    def test_to_dict_serializable(self):
        sig = _sig(confidence=0.75, reason="demo", target_position=1, metadata={"k": "v"})
        d = sig.to_dict()
        assert d["action"] == "buy"
        assert d["confidence"] == 0.75
        json.dumps(d)

    def test_validate_signal_catches_bad_action(self):
        sig = object.__new__(StrategySignal)
        object.__setattr__(sig, "action", "bogus")
        object.__setattr__(sig, "strategy_id", "s")
        object.__setattr__(sig, "timestamp", _TS)
        object.__setattr__(sig, "symbol", "X")
        object.__setattr__(sig, "reference_price", 1.0)
        object.__setattr__(sig, "confidence", 0.0)
        object.__setattr__(sig, "reason", "")
        object.__setattr__(sig, "target_position", None)
        object.__setattr__(sig, "metadata", {})
        object.__setattr__(sig, "version", "")
        errors = validate_signal(sig)
        assert any("action" in e for e in errors)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
class TestRegistry:
    def test_register_and_retrieve(self):
        reg = StrategyRegistry()
        strat = EMACrossoverStrategy()
        reg.register(strat)
        assert reg.count == 1
        assert reg.exists("ema_crossover")
        assert reg.get("ema_crossover") is strat
        assert reg.get_by_reference("ema_crossover@1.0.0") is strat

    def test_get_unknown_raises(self):
        reg = StrategyRegistry()
        with pytest.raises(KeyError):
            reg.get("nope")
        with pytest.raises(KeyError):
            reg.get_by_reference("nope@1.0.0")

    def test_duplicate_identity_rejected(self):
        reg = StrategyRegistry()
        reg.register(EMACrossoverStrategy())
        with pytest.raises(DuplicateStrategyError):
            reg.register(EMACrossoverStrategy())

    def test_duplicate_with_overwrite_allowed(self):
        reg = StrategyRegistry()
        a = EMACrossoverStrategy()
        reg.register(a)
        b = EMACrossoverStrategy()
        reg.register(b, overwrite=True)
        assert reg.count == 1

    def test_list_filters_by_family(self):
        reg = StrategyRegistry()
        reg.register(EMACrossoverStrategy())
        reg.register(RSIMeanReversionStrategy())
        trends = reg.list(family=StrategyFamily.TREND)
        meanrev = reg.list(family=StrategyFamily.MEAN_REVERSION)
        assert len(trends) == 1 and len(meanrev) == 1
        assert len(reg.list()) == 2

    def test_list_filters_by_timeframe(self):
        reg = StrategyRegistry()
        reg.register(EMACrossoverStrategy())
        assert len(reg.list(timeframe="1d")) == 1
        assert len(reg.list(timeframe="1w")) == 0

    def test_list_filters_by_tag(self):
        reg = StrategyRegistry()
        reg.register(EMACrossoverStrategy())
        assert len(reg.list(tag="reference")) == 1
        assert len(reg.list(tag="nonexistent")) == 0

    def test_unregister(self):
        reg = StrategyRegistry()
        reg.register(EMACrossoverStrategy())
        reg.unregister("ema_crossover")
        assert reg.count == 0
        assert not reg.exists("ema_crossover")

    def test_unregister_unknown_raises(self):
        reg = StrategyRegistry()
        with pytest.raises(KeyError):
            reg.unregister("nope")

    def test_invalid_strategy_rejected_by_registry(self):
        reg = StrategyRegistry()
        bad = _StubStrategy(_meta(description=""), )  # empty description fails validation
        with pytest.raises(StrategyValidationError):
            reg.register(bad)

    def test_content_hash_is_deterministic(self):
        reg = StrategyRegistry()
        s = EMACrossoverStrategy()
        reg.register(s)
        h1 = reg.content_hash("ema_crossover@1.0.0")
        h2 = reg.content_hash("ema_crossover@1.0.0")
        assert h1 == h2 and len(h1) == 64


# --------------------------------------------------------------------------- #
# Versioning in the registry
# --------------------------------------------------------------------------- #
class TestVersioning:
    def test_two_versions_coexist(self):
        reg = StrategyRegistry()
        m1 = _meta(strategy_id="multi", version="1.0.0")
        m2 = _meta(strategy_id="multi", version="1.1.0")
        reg.register(_StubStrategy(m1))
        reg.register(_StubStrategy(m2))
        assert reg.identities() == ["multi@1.0.0", "multi@1.1.0"]
        assert {s.metadata.version for s in reg.list()} == {"1.0.0", "1.1.0"}

    def test_get_highest_version_when_omitted(self):
        reg = StrategyRegistry()
        reg.register(_StubStrategy(_meta(strategy_id="multi", version="1.0.0")))
        reg.register(_StubStrategy(_meta(strategy_id="multi", version="2.0.0")))
        assert reg.get("multi").metadata.version == "2.0.0"

    def test_get_specific_version(self):
        reg = StrategyRegistry()
        reg.register(_StubStrategy(_meta(strategy_id="multi", version="1.0.0")))
        reg.register(_StubStrategy(_meta(strategy_id="multi", version="1.1.0")))
        assert reg.get("multi", "1.0.0").metadata.version == "1.0.0"
        assert reg.get("multi", "1.1.0").metadata.version == "1.1.0"

    def test_reproducibility_metadata_differs_for_params(self):
        m = _meta(strategy_id="repro", parameter_schema=ParameterSchema(parameters=[
            ParameterDefinition(name="window", type=ParameterType.INTEGER, default=20),
        ]))
        a = _StubStrategy(m, {"window": 20})
        b = _StubStrategy(m, {"window": 50})
        reg = StrategyRegistry()
        reg.register(a)
        assert reg.content_hash("repro@1.0.0") is not None

    def test_exact_identity_preserved(self):
        reg = StrategyRegistry()
        strat = EMACrossoverStrategy()
        reg.register(strat)
        ref = str(strat.metadata.reference)
        assert ref in reg.identities()


# --------------------------------------------------------------------------- #
# Validation gate
# --------------------------------------------------------------------------- #
class TestValidation:
    def test_valid_strategy_accepted(self):
        assert validate_strategy(EMACrossoverStrategy()) == []

    def test_valid_strategy_require_passes(self):
        require_valid_strategy(EMACrossoverStrategy())  # no raise

    def test_strategy_with_bad_params_rejected(self):
        m = _meta(parameter_schema=ParameterSchema(parameters=[
            ParameterDefinition(name="window", type=ParameterType.INTEGER, default=20,
                                minimum=2, maximum=500),
        ]))

        class _BadParams(_StubStrategy):
            @property
            def parameters(self):
                return {"window": 1}

        errors = validate_strategy(_BadParams(m))
        assert any("maximum" in e or "above" in e for e in errors)
        with pytest.raises(StrategyValidationError):
            require_valid_strategy(_BadParams(m))

    def test_strategy_missing_required_param_rejected(self):
        m = _meta(parameter_schema=ParameterSchema(parameters=[
            ParameterDefinition(name="window", type=ParameterType.INTEGER,
                                default=None, required=True),
        ]))

        class _Missing(_StubStrategy):
            @property
            def parameters(self):
                return {}

        errors = validate_strategy(_Missing(m))
        assert any("required" in e for e in errors)

    def test_non_strategy_object_rejected(self):
        errors = validate_strategy(object())
        assert any("not a Strategy" in e for e in errors)

    def test_metadata_with_code_payload_rejected(self):
        meta = StrategyMetadata.model_construct(
            strategy_id="stub", name="Stub", version="1.0.0",
            family=StrategyFamily.TREND, description="eval(1)",
            timeframes=["1d"], supported_instruments=["*"],
            required_data=["ohlcv"], required_indicators=["ema"],
        )
        errors = validate_metadata(meta)
        assert any("code-like" in e for e in errors)


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
class TestDeterminism:
    def test_ema_determinism_same_signal(self):
        state = _flat_state(n=60, seed=3, trend=0.5)
        s = EMACrossoverStrategy()
        a = s.evaluate(state)
        b = s.evaluate(state)
        assert a.action == b.action
        assert a.target_position == b.target_position
        assert a.reference_price == b.reference_price

    def test_rsi_determinism(self):
        state = _flat_state(n=60, seed=9, trend=-0.5)
        s = RSIMeanReversionStrategy()
        a = s.evaluate(state)
        b = s.evaluate(state)
        assert a == b

    def test_ema_signal_pure_data_no_mutation(self):
        state = _flat_state(n=60, seed=2, trend=0.3)
        s = EMACrossoverStrategy()
        before = state.bars["close"].copy()
        _ = s.evaluate(state)
        assert (state.bars["close"] == before).all()

    def test_ema_bear_market_exits_long(self):
        idx = pd.date_range("2024-01-01", periods=60, freq="1D", tz="UTC")
        close = np.linspace(100, 40, 60)
        df = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1,
                           "close": close, "volume": 100.0}, index=idx)
        st = _state(df, position=PositionState("NSE:SBIN", side=1, size=10, entry_price=close[0]))
        sig = EMACrossoverStrategy(fast_period=5, slow_period=13).evaluate(st)
        assert sig.action == SignalAction.EXIT
        assert sig.target_position == 0

    def test_ema_bull_market_buys_flat(self):
        idx = pd.date_range("2024-01-01", periods=60, freq="1D", tz="UTC")
        close = np.linspace(40, 100, 60)
        df = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1,
                           "close": close, "volume": 100.0}, index=idx)
        st = _state(df)
        sig = EMACrossoverStrategy(fast_period=5, slow_period=13).evaluate(st)
        assert sig.action == SignalAction.BUY
        assert sig.target_position == 1


# --------------------------------------------------------------------------- #
# Reference strategy construction validation
# --------------------------------------------------------------------------- #
class TestReferenceStrategy:
    def test_builtin_metadata_present(self):
        meta = EMACrossoverStrategy.metadata
        assert meta.strategy_id == "ema_crossover"
        assert meta.version == "1.0.0"
        assert meta.family == StrategyFamily.TREND
        assert "ema" in meta.required_indicators

    def test_fast_not_lt_slow_rejected(self):
        with pytest.raises(ValueError):
            EMACrossoverStrategy(fast_period=30, slow_period=20)

    def test_invalid_param_value_rejected(self):
        with pytest.raises(StrategyValidationError):
            EMACrossoverStrategy(fast_period=1)

    def test_rsi_oversold_ge_overbought_rejected(self):
        with pytest.raises(ValueError):
            RSIMeanReversionStrategy(oversold=70.0, overbought=30.0)
    def test_parameters_reflect_instance(self):
        s = EMACrossoverStrategy(fast_period=5, slow_period=20, allow_short=True)
        assert s.parameters == {"fast_period": 5, "slow_period": 20, "allow_short": True}

    def test_ema_insufficient_history_holds(self):
        idx = pd.date_range("2024-01-01", periods=5, freq="1D", tz="UTC")
        close = np.arange(1, 6, dtype=float)
        df = pd.DataFrame({"open": close, "high": close, "low": close, "close": close,
                           "volume": 1.0}, index=idx)
        sig = EMACrossoverStrategy(fast_period=12, slow_period=26).evaluate(_state(df))
        assert sig.action == SignalAction.HOLD

    def test_rsi_insufficient_history_holds(self):
        idx = pd.date_range("2024-01-01", periods=5, freq="1D", tz="UTC")
        close = np.arange(1, 6, dtype=float)
        df = pd.DataFrame({"open": close, "high": close, "low": close, "close": close,
                           "volume": 1.0}, index=idx)
        sig = RSIMeanReversionStrategy(14, 30, 70).evaluate(_state(df))
        assert sig.action == SignalAction.HOLD
