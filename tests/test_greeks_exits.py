"""Phase 5 -- greeks exit-rule engine: gating, fail-open and determinism.

Pins the pure ``evaluate_greeks_exit`` seam and its env helpers:

* D1 fail-open: anything missing, non-finite or unusable yields ``None`` and
  nothing ever raises -- unknown is never coerced to a value (D7).
* D2 default OFF: the rule ships dark, both env switches are required, and
  each helper mirrors the ``AUTONOMOUS_DELTA_DECAY_*`` parsing convention.
* Strict ``<`` on the IV-crush floor, caller-supplied holding age (no clock),
  and byte-identical output for identical input.

No live data, no I/O, no clock: every fixture is an ``ExitGreeksInput``
built in-process.
"""
from __future__ import annotations

import json
import logging

import pytest

from trading_system.autonomous.greeks_exits import (
    ExitGreeksInput,
    GreeksExit,
    GreeksExitConfig,
    evaluate_greeks_exit,
    greeks_exits_enabled,
    iv_crush_floor,
    iv_crush_min_holding_seconds,
)

MODULE_LOGGER = "trading_system.autonomous.greeks_exits"
MASTER_SWITCH = "AUTONOMOUS_GREEKS_ENABLED"
PHASE_SWITCH = "AUTONOMOUS_GREEKS_EXITS_ENABLED"
FLOOR_VAR = "AUTONOMOUS_IV_CRUSH_FLOOR"
MIN_HOLDING_VAR = "AUTONOMOUS_IV_CRUSH_MIN_HOLDING_SECONDS"
ALL_VARS = (MASTER_SWITCH, PHASE_SWITCH, FLOOR_VAR, MIN_HOLDING_VAR)
TRUTHY_SPELLINGS = ("1", "true", "yes", "on")
FALSEY_SPELLINGS = ("0", "false", "no", "off", "ture", "")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ALL_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def crushed() -> ExitGreeksInput:
    return ExitGreeksInput(
        is_option=True,
        entry_iv=0.6,
        last_iv=0.2,
        holding_seconds=600.0,
    )


@pytest.fixture
def enabled() -> GreeksExitConfig:
    return GreeksExitConfig(iv_crush_enabled=True)


@pytest.fixture
def warning_records():
    target = logging.getLogger(MODULE_LOGGER)
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Capture()
    previous_level = target.level
    target.addHandler(handler)
    target.setLevel(logging.WARNING)
    yield records
    target.removeHandler(handler)
    target.setLevel(previous_level)


def _set_env(monkeypatch, name: str, value: object) -> None:
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, str(value))


def _configured(**overrides) -> GreeksExitConfig:
    return GreeksExitConfig(iv_crush_enabled=True, **overrides)


def test_default_config_returns_none(crushed):
    assert evaluate_greeks_exit(greeks=crushed, config=GreeksExitConfig()) is None


def test_disabled_rule_ignores_a_large_crush():
    greeks = ExitGreeksInput(
        is_option=True,
        entry_iv=0.9,
        last_iv=0.05,
        holding_seconds=600.0,
    )
    config = GreeksExitConfig(iv_crush_enabled=False, iv_crush_floor=0.9)
    assert evaluate_greeks_exit(greeks=greeks, config=config) is None


def test_enabled_rule_fires_below_floor(crushed, enabled):
    outcome = evaluate_greeks_exit(greeks=crushed, config=enabled)
    assert isinstance(outcome, GreeksExit)
    assert outcome.reason == "iv_crush"


def test_fire_detail_is_exact(crushed, enabled):
    outcome = evaluate_greeks_exit(greeks=crushed, config=enabled)
    assert outcome is not None
    assert outcome.detail == {
        "entry_iv": 0.6,
        "last_iv": 0.2,
        "ratio": 0.2 / 0.6,
        "floor": 0.5,
    }
    assert set(outcome.detail) == {"entry_iv", "last_iv", "ratio", "floor"}
    assert all(isinstance(value, float) for value in outcome.detail.values())


def test_ratio_exactly_at_floor_does_not_fire(enabled):
    greeks = ExitGreeksInput(
        is_option=True,
        entry_iv=0.5,
        last_iv=0.25,
        holding_seconds=600.0,
    )
    assert 0.25 / 0.5 == enabled.iv_crush_floor
    assert evaluate_greeks_exit(greeks=greeks, config=enabled) is None


def test_ratio_above_floor_does_not_fire(enabled):
    greeks = ExitGreeksInput(
        is_option=True,
        entry_iv=0.5,
        last_iv=0.4,
        holding_seconds=600.0,
    )
    assert evaluate_greeks_exit(greeks=greeks, config=enabled) is None


def test_iv_collapsed_to_zero_fires(enabled):
    greeks = ExitGreeksInput(
        is_option=True,
        entry_iv=0.5,
        last_iv=0.0,
        holding_seconds=600.0,
    )
    outcome = evaluate_greeks_exit(greeks=greeks, config=enabled)
    assert outcome is not None
    assert outcome.reason == "iv_crush"
    assert outcome.detail["ratio"] == 0.0


def test_non_option_never_fires(enabled):
    greeks = ExitGreeksInput(
        is_option=False,
        entry_iv=0.6,
        last_iv=0.0,
        holding_seconds=600.0,
    )
    assert evaluate_greeks_exit(greeks=greeks, config=enabled) is None


def test_is_option_none_never_fires(enabled):
    greeks = ExitGreeksInput(
        is_option=None,
        entry_iv=0.6,
        last_iv=0.0,
        holding_seconds=600.0,
    )
    assert evaluate_greeks_exit(greeks=greeks, config=enabled) is None


def test_missing_entry_iv_never_fires(enabled):
    greeks = ExitGreeksInput(
        is_option=True,
        last_iv=0.1,
        holding_seconds=600.0,
    )
    assert evaluate_greeks_exit(greeks=greeks, config=enabled) is None


def test_missing_last_iv_never_fires(enabled):
    greeks = ExitGreeksInput(
        is_option=True,
        entry_iv=0.6,
        holding_seconds=600.0,
    )
    assert evaluate_greeks_exit(greeks=greeks, config=enabled) is None


@pytest.mark.parametrize("entry_iv", [0.0, -0.25])
def test_entry_iv_must_be_strictly_positive(enabled, entry_iv):
    greeks = ExitGreeksInput(
        is_option=True,
        entry_iv=entry_iv,
        last_iv=0.0,
        holding_seconds=600.0,
    )
    assert evaluate_greeks_exit(greeks=greeks, config=enabled) is None


def test_last_iv_must_be_non_negative(enabled):
    greeks = ExitGreeksInput(
        is_option=True,
        entry_iv=0.5,
        last_iv=-0.1,
        holding_seconds=600.0,
    )
    assert evaluate_greeks_exit(greeks=greeks, config=enabled) is None


def test_non_finite_iv_never_fires(enabled):
    cases = (
        (float("nan"), 0.1),
        (float("inf"), 0.1),
        (float("-inf"), 0.1),
        (0.5, float("nan")),
        (0.5, float("inf")),
        (0.5, float("-inf")),
    )
    for entry_iv, last_iv in cases:
        greeks = ExitGreeksInput(
            is_option=True,
            entry_iv=entry_iv,
            last_iv=last_iv,
            holding_seconds=600.0,
        )
        assert evaluate_greeks_exit(greeks=greeks, config=enabled) is None, (
            entry_iv,
            last_iv,
        )


@pytest.mark.parametrize(
    "floor",
    [0.0, 1.0, -0.5, float("nan"), float("inf")],
)
def test_unusable_floor_never_fires(crushed, floor):
    config = _configured(iv_crush_floor=floor)
    assert evaluate_greeks_exit(greeks=crushed, config=config) is None


def test_high_valid_floor_fires_when_ratio_is_below_it():
    config = _configured(iv_crush_floor=0.95)
    greeks = ExitGreeksInput(
        is_option=True,
        entry_iv=1.0,
        last_iv=0.9,
        holding_seconds=60.0,
    )
    outcome = evaluate_greeks_exit(greeks=greeks, config=config)
    assert outcome is not None
    assert outcome.detail["ratio"] == 0.9
    assert outcome.detail["floor"] == 0.95


def test_min_holding_below_gate_does_not_fire(enabled):
    config = _configured(min_holding_seconds=600.0)
    greeks = ExitGreeksInput(
        is_option=True,
        entry_iv=0.5,
        last_iv=0.2,
        holding_seconds=300.0,
    )
    assert evaluate_greeks_exit(greeks=greeks, config=config) is None


def test_min_holding_at_gate_fires(enabled):
    config = _configured(min_holding_seconds=600.0)
    greeks = ExitGreeksInput(
        is_option=True,
        entry_iv=0.5,
        last_iv=0.2,
        holding_seconds=600.0,
    )
    assert evaluate_greeks_exit(greeks=greeks, config=config) is not None


def test_min_holding_above_gate_fires(enabled):
    config = _configured(min_holding_seconds=600.0)
    greeks = ExitGreeksInput(
        is_option=True,
        entry_iv=0.5,
        last_iv=0.2,
        holding_seconds=60_000.0,
    )
    assert evaluate_greeks_exit(greeks=greeks, config=config) is not None


def test_unknown_holding_bypasses_the_min_gate():
    config = _configured(min_holding_seconds=10_000.0)
    greeks = ExitGreeksInput(
        is_option=True,
        entry_iv=0.5,
        last_iv=0.2,
        holding_seconds=None,
    )
    assert evaluate_greeks_exit(greeks=greeks, config=config) is not None


@pytest.mark.parametrize("holding", [float("nan"), float("inf")])
def test_non_finite_holding_never_fires(enabled, holding):
    greeks = ExitGreeksInput(
        is_option=True,
        entry_iv=0.5,
        last_iv=0.2,
        holding_seconds=holding,
    )
    assert evaluate_greeks_exit(greeks=greeks, config=enabled) is None


def test_delta_fields_do_not_drive_the_rule(enabled):
    greeks = ExitGreeksInput(
        is_option=True,
        entry_delta=0.62,
        last_delta=0.03,
        holding_seconds=600.0,
    )
    assert evaluate_greeks_exit(greeks=greeks, config=enabled) is None


def test_output_is_byte_identical_for_identical_input():
    builds = [
        ExitGreeksInput(
            is_option=True,
            entry_iv=0.6,
            last_iv=0.2,
            holding_seconds=600.0,
        )
        for _ in range(2)
    ]
    outcomes = [
        evaluate_greeks_exit(greeks=greeks, config=_configured())
        for greeks in builds
    ]
    assert outcomes[0] is not None
    assert outcomes[0] == outcomes[1]
    assert repr(outcomes[0]) == repr(outcomes[1])
    assert json.dumps(outcomes[0].detail, sort_keys=True) == json.dumps(
        outcomes[1].detail,
        sort_keys=True,
    )


def test_greeks_exits_require_both_switches(monkeypatch):
    negative = (
        (None, None),
        ("1", None),
        (None, "1"),
        ("true", "0"),
        ("0", "true"),
        ("ture", "on"),
    )
    for master, phase in negative:
        _set_env(monkeypatch, MASTER_SWITCH, master)
        _set_env(monkeypatch, PHASE_SWITCH, phase)
        assert greeks_exits_enabled() is False, (master, phase)
    _set_env(monkeypatch, MASTER_SWITCH, "1")
    _set_env(monkeypatch, PHASE_SWITCH, "on")
    assert greeks_exits_enabled() is True


@pytest.mark.parametrize("spelling", TRUTHY_SPELLINGS)
def test_truthy_spellings_turn_both_switches_on(monkeypatch, spelling):
    _set_env(monkeypatch, MASTER_SWITCH, spelling)
    _set_env(monkeypatch, PHASE_SWITCH, spelling)
    assert greeks_exits_enabled() is True


def test_falsey_and_typo_spellings_keep_the_switches_off(monkeypatch):
    for spelling in FALSEY_SPELLINGS:
        _set_env(monkeypatch, MASTER_SWITCH, spelling)
        _set_env(monkeypatch, PHASE_SWITCH, "1")
        assert greeks_exits_enabled() is False, spelling
        _set_env(monkeypatch, MASTER_SWITCH, "1")
        _set_env(monkeypatch, PHASE_SWITCH, spelling)
        assert greeks_exits_enabled() is False, spelling
    _set_env(monkeypatch, MASTER_SWITCH, " 1 ")
    _set_env(monkeypatch, PHASE_SWITCH, " Yes\t")
    assert greeks_exits_enabled() is True


def test_iv_crush_floor_defaults_when_unset_or_empty(monkeypatch, warning_records):
    assert iv_crush_floor() == 0.5
    _set_env(monkeypatch, FLOOR_VAR, "")
    assert iv_crush_floor() == 0.5
    assert warning_records == []


def test_iv_crush_floor_parses_valid_values(monkeypatch, warning_records):
    cases = {"0.3": 0.3, " 0.25 ": 0.25, "0.999": 0.999, "1e-3": 0.001}
    for raw, expected in cases.items():
        _set_env(monkeypatch, FLOOR_VAR, raw)
        assert iv_crush_floor() == expected, raw
    assert warning_records == []


@pytest.mark.parametrize("raw", ["0", "1", "-0.5", "nan", "inf"])
def test_iv_crush_floor_out_of_range_warns_and_falls_back(
    raw, monkeypatch, warning_records
):
    _set_env(monkeypatch, FLOOR_VAR, raw)
    assert iv_crush_floor() == 0.5
    assert any(
        "AUTONOMOUS_IV_CRUSH_FLOOR" in record.getMessage()
        for record in warning_records
    )


def test_iv_crush_floor_unparseable_warns_and_falls_back(
    monkeypatch, warning_records
):
    _set_env(monkeypatch, FLOOR_VAR, "half")
    assert iv_crush_floor() == 0.5
    assert len(warning_records) == 1
    assert "AUTONOMOUS_IV_CRUSH_FLOOR" in warning_records[0].getMessage()


def test_min_holding_defaults_when_unset_or_empty(monkeypatch, warning_records):
    assert iv_crush_min_holding_seconds() == 0.0
    _set_env(monkeypatch, MIN_HOLDING_VAR, "")
    assert iv_crush_min_holding_seconds() == 0.0
    assert warning_records == []


def test_min_holding_parses_valid_values(monkeypatch, warning_records):
    cases = {"900": 900.0, " 12.5 ": 12.5, "0": 0.0}
    for raw, expected in cases.items():
        _set_env(monkeypatch, MIN_HOLDING_VAR, raw)
        assert iv_crush_min_holding_seconds() == expected, raw
    assert warning_records == []


@pytest.mark.parametrize("raw", ["-1", "abc", "nan"])
def test_min_holding_invalid_warns_and_falls_back(raw, monkeypatch, warning_records):
    _set_env(monkeypatch, MIN_HOLDING_VAR, raw)
    assert iv_crush_min_holding_seconds() == 0.0
    assert any(
        "AUTONOMOUS_IV_CRUSH_MIN_HOLDING_SECONDS" in record.getMessage()
        for record in warning_records
    )


def test_env_helpers_build_a_firing_config(monkeypatch, crushed):
    _set_env(monkeypatch, MASTER_SWITCH, "1")
    _set_env(monkeypatch, PHASE_SWITCH, "true")
    _set_env(monkeypatch, FLOOR_VAR, "0.9")
    _set_env(monkeypatch, MIN_HOLDING_VAR, "60")
    config = GreeksExitConfig(
        iv_crush_enabled=greeks_exits_enabled(),
        iv_crush_floor=iv_crush_floor(),
        min_holding_seconds=iv_crush_min_holding_seconds(),
    )
    assert config.iv_crush_enabled is True
    assert config.iv_crush_floor == 0.9
    assert config.min_holding_seconds == 60.0
    outcome = evaluate_greeks_exit(greeks=crushed, config=config)
    assert outcome is not None
    assert outcome.reason == "iv_crush"


def test_env_helpers_keep_the_rule_off_when_a_switch_is_missing(
    monkeypatch, crushed
):
    _set_env(monkeypatch, MASTER_SWITCH, "1")
    config = GreeksExitConfig(
        iv_crush_enabled=greeks_exits_enabled(),
        iv_crush_floor=iv_crush_floor(),
        min_holding_seconds=iv_crush_min_holding_seconds(),
    )
    assert config.iv_crush_enabled is False
    assert evaluate_greeks_exit(greeks=crushed, config=config) is None
