"""Phase 13 tests: StrategySpec validation, DSL constraints, serialization.

Offline and deterministic. A StrategySpec is DATA, never code — these tests
pin down every rejection path (unknown indicators, bad operators, malformed
conditions, invalid numerics, code-like payloads) and the JSON round-trip.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from trading_system.research.strategy_lab.dsl import (
    ComparisonOp,
    IndicatorName,
    indicator_key,
    validate_indicator_params,
    warmup_bars_for,
)
from trading_system.research.strategy_lab.spec import (
    IndicatorDef,
    StrategySpec,
    const_operand,
    field_operand,
    indicator_operand,
    logic,
    make_condition,
    not_,
)


def _entry() -> dict:
    return make_condition(field_operand("close"), ">", const_operand(10.0))


def _payload(**overrides) -> dict:
    payload = {
        "name": "Test strategy",
        "description": "A research hypothesis.",
        "symbol": "NSE:SBIN",
        "timeframe": "1d",
        "indicators": [{"name": "sma", "params": {"window": 20}}],
        "entry": _entry(),
        "risk": {"stop_loss_pct": 0.05},
        "generated_by": "unit-test",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# Valid specifications
# --------------------------------------------------------------------------- #
def test_valid_spec_parses():
    spec = StrategySpec(**_payload())
    assert spec.name == "Test strategy"
    assert spec.indicator_keys() == ["sma_20"]
    assert spec.allow_long is True
    assert spec.risk.allow_short is False


def test_spec_defaults_are_conservative():
    spec = StrategySpec(**_payload())
    assert spec.position_sizing.max_allocation_pct == 1.0
    assert spec.risk.allow_short is False
    assert spec.risk.stop_loss_pct == 0.05


def test_all_registry_indicators_accepted():
    for name in IndicatorName:
        spec = StrategySpec(**_payload(indicators=[{"name": name.value, "params": {}}]))
        assert spec.indicators[0].name == name


def test_indicator_param_defaults_applied():
    spec = StrategySpec(**_payload())
    assert spec.indicators[0].params == {"window": 20}


# --------------------------------------------------------------------------- #
# Structural rejections
# --------------------------------------------------------------------------- #
def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        StrategySpec(**_payload(python_code="import os"))


def test_unknown_indicator_rejected():
    with pytest.raises(ValidationError):
        StrategySpec(**_payload(indicators=[{"name": "super_indicator", "params": {}}]))


def test_unknown_indicator_param_rejected():
    with pytest.raises(ValidationError):
        StrategySpec(**_payload(indicators=[{"name": "sma", "params": {"windows": 20}}]))


def test_zero_indicator_window_rejected():
    with pytest.raises(ValidationError):
        StrategySpec(**_payload(indicators=[{"name": "sma", "params": {"window": 0}}]))


def test_macd_fast_ge_slow_rejected():
    with pytest.raises(ValidationError):
        StrategySpec(
            **_payload(indicators=[
                {"name": "macd", "params": {"fast": 26, "slow": 12, "signal": 9}},
            ])
        )


def test_invalid_operator_rejected():
    bad = make_condition(field_operand("close"), "divides", const_operand(1.0))
    with pytest.raises(ValidationError):
        StrategySpec(**_payload(entry=bad))


def test_malformed_condition_missing_type_rejected():
    bad = {"left": field_operand("close"), "op": ">", "right": const_operand(1.0)}
    with pytest.raises(ValidationError):
        StrategySpec(**_payload(entry=bad))


def test_comparison_of_two_constants_rejected():
    bad = make_condition(const_operand(1.0), ">", const_operand(0.0))
    with pytest.raises(ValidationError):
        StrategySpec(**_payload(entry=bad))


def test_crosses_above_with_constant_left_rejected():
    bad = make_condition(const_operand(1.0), "crosses_above", field_operand("close"))
    with pytest.raises(ValidationError):
        StrategySpec(**_payload(entry=bad))


def test_empty_logic_node_rejected():
    bad = logic("AND")
    with pytest.raises(ValidationError):
        StrategySpec(**_payload(entry=bad))


def test_nested_logic_is_accepted():
    node = logic(
        "OR",
        make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
        not_(make_condition(field_operand("volume"), "<", const_operand(100.0))),
    )
    spec = StrategySpec(**_payload(entry=node))
    assert spec.referenced_indicators() == ["sma_20"]


def test_invalid_numeric_constant_rejected():
    bad = make_condition(
        field_operand("close"), ">", {"kind": "constant", "constant": "abc"}
    )
    with pytest.raises(ValidationError):
        StrategySpec(**_payload(entry=bad))


def test_impossible_stop_loss_rejected():
    with pytest.raises(ValidationError):
        StrategySpec(**_payload(risk={"stop_loss_pct": 1.0}))


def test_negative_take_profit_rejected():
    with pytest.raises(ValidationError):
        StrategySpec(**_payload(risk={"take_profit_pct": -0.1}))


def test_invalid_position_size_rejected():
    with pytest.raises(ValidationError):
        StrategySpec(**_payload(position_sizing={"max_allocation_pct": 1.5}))


def test_invalid_symbol_rejected():
    with pytest.raises(ValidationError):
        StrategySpec(**_payload(symbol="has spaces"))


def test_invalid_timeframe_rejected():
    with pytest.raises(ValidationError):
        StrategySpec(**_payload(timeframe="1x"))


# --------------------------------------------------------------------------- #
# Long/short configuration
# --------------------------------------------------------------------------- #
def test_short_enabled_requires_entry_short():
    payload = _payload(risk={"allow_short": True})
    with pytest.raises(ValidationError):
        StrategySpec(**payload)


def test_short_enabled_with_entry_short_ok():
    payload = _payload(
        indicators=[
            {"name": "ema", "params": {"window": 12}},
            {"name": "ema", "params": {"window": 26}},
        ],
        entry=make_condition(
            indicator_operand("ema_12"), "crosses_above", indicator_operand("ema_26")
        ),
        entry_short=make_condition(
            indicator_operand("ema_12"), "crosses_below", indicator_operand("ema_26")
        ),
        risk={"allow_short": True},
    )
    spec = StrategySpec(**payload)
    assert spec.risk.allow_short is True


def test_no_direction_allowed_rejected():
    payload = _payload(allow_long=False)
    with pytest.raises(ValidationError):
        StrategySpec(**payload)


def test_duplicate_indicator_declarations_rejected():
    dupes = [{"name": "sma", "params": {"window": 20}}] * 2
    with pytest.raises(ValidationError):
        StrategySpec(**_payload(indicators=dupes))


def test_undeclared_indicator_reference_rejected_in_validation():
    from trading_system.research.strategy_lab.validation import validate_spec

    spec = StrategySpec(**_payload(
        entry=make_condition(field_operand("close"), ">", indicator_operand("ema_12"))
    ))
    errors = validate_spec(spec)
    assert any("undeclared indicator" in e for e in errors)


def test_ambiguous_indicator_reference_rejected():
    from trading_system.research.strategy_lab.validation import validate_spec

    spec = StrategySpec(**_payload(
        indicators=[
            {"name": "sma", "params": {"window": 20}},
            {"name": "sma", "params": {"window": 50}},
        ],
        entry=make_condition(field_operand("close"), ">", indicator_operand("sma")),
    ))
    errors = validate_spec(spec)
    assert any("ambiguous" in e for e in errors)


def test_bare_indicator_name_resolves_when_unique():
    spec = StrategySpec(**_payload(
        entry=make_condition(field_operand("close"), ">", indicator_operand("sma"))
    ))
    assert spec.referenced_indicators() == ["sma_20"]


# --------------------------------------------------------------------------- #
# Code-payload rejection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "payload_text",
    [
        "import os; os.system('rm -rf /')",
        "eval('1+1')",
        "exec('x=1')",
        "__import__('os')",
        "lambda: print(1)",
        "open('/etc/passwd')",
        "subprocess.run(['ls'])",
        "base64.b64decode('..')",
    ],
)
def test_code_like_descriptions_rejected(payload_text):
    with pytest.raises(ValidationError):
        StrategySpec(**_payload(description=payload_text))


def test_code_like_name_rejected():
    with pytest.raises(ValidationError):
        StrategySpec(**_payload(name="import sys"))


def test_prose_with_word_execution_is_allowed():
    spec = StrategySpec(**_payload(description="Clean trend execution rules."))
    assert "execution" in spec.description


def test_prose_with_word_important_is_allowed():
    spec = StrategySpec(**_payload(description="Momentum is important here."))
    assert "important" in spec.description


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def test_json_round_trip_lossless():
    spec = StrategySpec(**_payload(
        risk={"stop_loss_pct": 0.05, "take_profit_pct": 0.1}
    ))
    payload = json.loads(spec.to_json())
    restored = StrategySpec.from_model_json(payload, model="unit-test")
    assert restored.model_dump(mode="json") == spec.model_dump(mode="json")


def test_model_validate_json_round_trip():
    spec = StrategySpec(**_payload())
    restored = StrategySpec.from_json(spec.to_json())
    assert restored == spec


def test_from_model_json_sets_provenance():
    payload = {k: v for k, v in _payload().items() if k != "generated_by"}
    spec = StrategySpec.from_model_json(payload, model="mock-model")
    assert spec.generated_by == "mock-model"


def test_from_model_json_keeps_existing_provenance():
    spec = StrategySpec.from_model_json(_payload(), model="mock-model")
    assert spec.generated_by == "unit-test"


def test_from_model_json_rejects_non_dict():
    with pytest.raises(TypeError):
        StrategySpec.from_model_json("just a string")


def test_from_model_json_rejects_partial_output():
    with pytest.raises(ValidationError):
        StrategySpec.from_model_json({"name": "Half spec"})


# --------------------------------------------------------------------------- #
# DSL registry helpers
# --------------------------------------------------------------------------- #
def test_indicator_key_formatting():
    assert indicator_key("sma", {"window": 20}) == "sma_20"
    assert indicator_key("macd", {"fast": 12, "slow": 26, "signal": 9}) == "macd_12_26_9"
    assert indicator_key("bb_upper", {"window": 20, "num_std": 2.0}) == "bb_upper_20_2"


def test_warmup_bars_for_macd_includes_signal():
    assert warmup_bars_for("macd", {"fast": 12, "slow": 26, "signal": 9}) == 35
    assert warmup_bars_for("sma", {"window": 20}) == 20


def test_validate_indicator_params_range_check():
    with pytest.raises(ValueError):
        validate_indicator_params("sma", {"window": 99999})


def test_comparison_op_values():
    assert ComparisonOp.CROSSES_ABOVE.value == "crosses_above"
    assert ComparisonOp.GTE.value == ">="


def test_indicator_def_key_property():
    ind = IndicatorDef(name=IndicatorName.EMA, params={"window": 12})
    assert ind.key == "ema_12"

