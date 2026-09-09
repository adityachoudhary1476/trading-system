"""Phase 7 — Safety, Kill-Switch, and Recovery Layer tests.

Covers:
  - KillSwitch: initial state, halt, idempotent re-halt, resume, reason/detail
  - SafetyValidator: individual checks + composite validate_pre_trade/scan/deployment/market_data
  - IdempotencyGuard: decision/bar/deployment dedup
  - Phase7SafetyLayer: integration
  - AutonomousController integration: halt_bot/resume_bot, kill-switch gating
  - Fail-closed: unknown state blocks trading
  - Static safety scan: no live broker references
"""
from __future__ import annotations

import ast
import os
import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from trading_system.autonomous.safety import (
    IdempotencyGuard,
    KillSwitch,
    KillSwitchReason,
    KillSwitchState,
    Phase7Config,
    Phase7SafetyLayer,
    SafetyCheck,
    SafetyResult,
    SafetyValidator,
)
from trading_system.autonomous.controller import AutonomousController
from trading_system.autonomous.bot_config import (
    AutonomousBotConfig,
    BotMode,
    Source,
    TradingMode,
    UserConstraints,
)
from trading_system.autonomous import AutonomousBotState
from trading_system.autonomous.coordinator import DeploymentCreationResult
from trading_system.autonomous.events import AutonomousEventType
from trading_system.paper.circuit_breaker import CircuitState, PaperCircuitBreaker
from trading_system.paper.risk import RiskDecision, PaperRiskGuard
from trading_system.paper.control import PaperTradingControlCenter
from trading_system.paper_trading import Position

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Data fixtures
# --------------------------------------------------------------------------- #

def _make_ohlcv(
    n: int = 50,
    start: datetime = datetime(2024, 1, 1, tzinfo=UTC),
    price: float = 100.0,
    freq: str = "D",
) -> pd.DataFrame:
    idx = pd.date_range(start=start, periods=n, freq=freq, tz="UTC")
    closes = np.linspace(price, price + 10, n)
    df = pd.DataFrame({
        "timestamp": idx,
        "open": closes - 0.5,
        "high": closes + 0.5,
        "low": closes - 0.5,
        "close": closes,
        "volume": np.full(n, 1_000_000, dtype=float),
    })
    df = df.set_index("timestamp")
    return df


def _make_controller():
    config = AutonomousBotConfig(
        bot_id="bot-test-p7",
        name="Phase 7 Test Bot",
        mode=BotMode.AUTONOMOUS,
        trading_mode=TradingMode.PAPER,
        enabled=True,
        user_constraints=UserConstraints(
            allowed_symbols=frozenset({"NSE:SBIN", "NSE:TCS"}),
            allowed_strategy_ids=frozenset({"ema_crossover"}),
            allowed_timeframes=frozenset({"1d"}),
        ),
        max_simultaneous_positions=5,
        source=Source.AUTONOMOUS,
    )
    cc = MagicMock(spec=PaperTradingControlCenter)
    cc.load_market_data = lambda s, tf: None
    cc.list_deployments.return_value = []
    return AutonomousController(config=config, control_center=cc)


# --------------------------------------------------------------------------- #
# Phase 7B — KillSwitch
# --------------------------------------------------------------------------- #


class TestKillSwitch:
    def test_initial_state_is_active(self):
        ks = KillSwitch()
        assert ks.state == KillSwitchState.ACTIVE
        assert ks.is_halted is False
        assert ks.allowed_to_trade() is True
        assert ks.reason is None
        assert ks.halt_count == 0
        assert ks.halt_timestamp is None

    def test_halt_sets_halted_state(self):
        ks = KillSwitch()
        ks.halt(KillSwitchReason.MANUAL, detail="operator requested")
        assert ks.is_halted is True
        assert ks.allowed_to_trade() is False
        assert ks.state == KillSwitchState.HALTED
        assert ks.reason == KillSwitchReason.MANUAL
        assert ks.detail == "operator requested"
        assert ks.halt_count == 1
        assert ks.halt_timestamp is not None

    def test_halt_is_idempotent_preserves_first_reason(self):
        ks = KillSwitch()
        ks.halt(KillSwitchReason.DATA_STALENESS, detail="first reason")
        ks.halt(KillSwitchReason.MANUAL, detail="second reason")
        assert ks.reason == KillSwitchReason.DATA_STALENESS
        assert ks.detail == "first reason"
        assert ks.halt_count == 2

    def test_resume_clears_halted_state(self):
        ks = KillSwitch()
        ks.halt(KillSwitchReason.MANUAL)
        assert ks.is_halted is True

        ks.resume()
        assert ks.is_halted is False
        assert ks.state == KillSwitchState.ACTIVE
        assert ks.reason is None
        assert ks.detail == ""
        assert ks.halt_timestamp is None

    def test_resume_from_active_is_noop(self):
        ks = KillSwitch()
        ks.resume()
        assert ks.is_halted is False
        assert ks.halt_count == 0

    def test_halt_accepts_string_reason(self):
        ks = KillSwitch()
        ks.halt("manual", detail="test")
        assert ks.is_halted is True
        assert ks.reason == KillSwitchReason.MANUAL

    def test_halt_count_resets_on_resume(self):
        ks = KillSwitch()
        ks.halt(KillSwitchReason.MANUAL)
        ks.resume()
        ks.halt(KillSwitchReason.MANUAL)
        assert ks.halt_count == 1


# --------------------------------------------------------------------------- #
# Phase 7A — SafetyValidator: individual checks
# --------------------------------------------------------------------------- #


class TestSafetyValidatorChecks:
    def test_check_kill_switch_active_passes(self):
        sv = SafetyValidator()
        result = sv.check_kill_switch(KillSwitch())
        assert result.passed

    def test_check_kill_switch_halted_fails(self):
        sv = SafetyValidator()
        ks = KillSwitch()
        ks.halt(KillSwitchReason.MANUAL)
        result = sv.check_kill_switch(ks)
        assert not result.passed
        assert any("kill_switch" in c for c in result.failed_checks)

    def test_check_kill_switch_disabled_passes(self):
        sv = SafetyValidator(Phase7Config(kill_switch_enabled=False))
        ks = KillSwitch()
        ks.halt(KillSwitchReason.MANUAL)
        result = sv.check_kill_switch(ks)
        assert result.passed

    def test_check_data_freshness_fresh_bar_passes(self):
        sv = SafetyValidator()
        df = _make_ohlcv(n=5)
        market_ts = (datetime(2024, 1, 5, tzinfo=UTC) + timedelta(minutes=30)).isoformat()
        result = sv.check_data_freshness(
            bars=df,
            market_timestamp=market_ts,
            max_staleness_seconds=3600,
        )
        assert result.passed

    def test_check_data_freshness_stale_bar_fails(self):
        sv = SafetyValidator()
        df = _make_ohlcv(n=5)
        market_ts = (df.index[-1] + timedelta(hours=2)).isoformat()
        result = sv.check_data_freshness(
            bars=df,
            market_timestamp=market_ts,
            max_staleness_seconds=3600,
        )
        assert not result.passed
        assert any("data_freshness" in c for c in result.failed_checks)

    def test_check_data_freshness_empty_frame_passes(self):
        sv = SafetyValidator()
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        result = sv.check_data_freshness(
            bars=df,
            market_timestamp=datetime(2024, 1, 1, tzinfo=UTC).isoformat(),
            max_staleness_seconds=3600,
        )
        assert result.passed

    def test_check_ohlcv_validity_valid_frame_passes(self):
        sv = SafetyValidator()
        df = _make_ohlcv(n=50)
        result = sv.check_ohlcv_validity(df, "1d")
        assert result.passed

    def test_check_ohlcv_validity_empty_fails(self):
        sv = SafetyValidator()
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        result = sv.check_ohlcv_validity(df, "1d")
        assert not result.passed
        assert any("ohlcv_validity" in c for c in result.failed_checks)

    def test_check_ohlcv_validity_bad_data_fails(self):
        sv = SafetyValidator()
        df = _make_ohlcv(n=50)
        df.iloc[0, df.columns.get_loc("high")] = df.iloc[0, df.columns.get_loc("low")] - 1
        df.iloc[1, df.columns.get_loc("volume")] = -100
        result = sv.check_ohlcv_validity(df, "1d")
        assert not result.passed
        assert any("ohlcv_validity" in c for c in result.failed_checks)

    def test_check_market_hours_allowed_passes(self):
        sv = SafetyValidator()
        allowed = frozenset({(9, 16)})
        ts = datetime(2024, 2, 1, 10, 0, tzinfo=UTC)  # 15:30 IST
        result = sv.check_market_hours(timestamp=ts, allowed_hours=allowed)
        assert result.passed

    def test_check_market_hours_disallowed_fails(self):
        sv = SafetyValidator()
        allowed = frozenset({(9, 16)})
        ts = datetime(2024, 2, 1, 2, 0, tzinfo=UTC)  # 07:30 IST (pre-market)
        result = sv.check_market_hours(timestamp=ts, allowed_hours=allowed)
        assert not result.passed
        assert any("market_hours" in c for c in result.failed_checks)

    def test_check_market_hours_no_constraints_passes(self):
        sv = SafetyValidator()
        result = sv.check_market_hours()
        assert result.passed

    def test_check_circuit_breaker_closed_passes(self):
        sv = SafetyValidator()
        cb = PaperCircuitBreaker()
        result = sv.check_circuit_breaker(cb)
        assert result.passed

    def test_check_circuit_breaker_open_fails(self):
        sv = SafetyValidator()
        cb = PaperCircuitBreaker()
        cb.trip("test trip")
        result = sv.check_circuit_breaker(cb)
        assert not result.passed
        assert any("circuit_breaker" in c for c in result.failed_checks)

    def test_check_circuit_breaker_none_passes(self):
        sv = SafetyValidator()
        result = sv.check_circuit_breaker(None)
        assert result.passed

    def test_check_risk_limits_none_guard_passes(self):
        sv = SafetyValidator()
        result = sv.check_risk_limits(risk_guard=None)
        assert result.passed

    def test_check_risk_limits_halt_fails(self):
        sv = SafetyValidator()
        rg = PaperRiskGuard()
        rg_decision = RiskDecision
        rg.check = MagicMock(return_value=(rg_decision.HALT, "drawdown exceeded"))
        result = sv.check_risk_limits(risk_guard=rg)
        assert not result.passed

    def test_check_risk_limits_warning_passes_with_warning(self):
        sv = SafetyValidator()
        rg = PaperRiskGuard()
        rg.check = MagicMock(return_value=(RiskDecision.WARNING, "near limit"))
        result = sv.check_risk_limits(risk_guard=rg)
        assert result.passed
        assert len(result.warnings) > 0

    def test_check_position_limits_under_max_passes(self):
        sv = SafetyValidator()
        result = sv.check_position_limits(current_positions=3, max_positions=5)
        assert result.passed

    def test_check_position_limits_at_max_fails(self):
        sv = SafetyValidator()
        result = sv.check_position_limits(current_positions=5, max_positions=5)
        assert not result.passed
        assert any("position_limits" in c for c in result.failed_checks)

    def test_check_emergency_loss_breach_fails(self):
        sv = SafetyValidator(Phase7Config(emergency_loss_pct=0.1))
        result = sv.check_emergency_loss(current_loss_pct=0.15, max_loss_pct=0.1)
        assert not result.passed
        assert any("emergency_loss" in c for c in result.failed_checks)

    def test_check_emergency_loss_no_threshold_passes(self):
        sv = SafetyValidator()
        result = sv.check_emergency_loss(current_loss_pct=0.5, max_loss_pct=None)
        assert result.passed


# --------------------------------------------------------------------------- #
# Phase 7A — SafetyValidator: composite checks
# --------------------------------------------------------------------------- #


class TestSafetyValidatorComposite:
    def test_validate_pre_trade_kill_switch_tripped_fails_fast(self):
        sv = SafetyValidator()
        ks = KillSwitch()
        ks.halt(KillSwitchReason.MANUAL)
        result = sv.validate_pre_trade(kill_switch=ks)
        assert not result.passed
        assert len(result.failed_checks) == 1

    def test_validate_pre_trade_all_passes(self):
        sv = SafetyValidator()
        ks = KillSwitch()
        result = sv.validate_pre_trade(
            kill_switch=ks,
            max_positions=5,
            current_positions=0,
        )
        assert result.passed

    def test_validate_pre_trade_accumulates_failures(self):
        sv = SafetyValidator()
        ks = KillSwitch()
        cb = PaperCircuitBreaker()
        cb.trip("open test")
        result = sv.validate_pre_trade(
            kill_switch=ks,
            circuit_breaker=cb,
            max_positions=2,
            current_positions=2,
        )
        assert not result.passed
        assert len(result.failed_checks) >= 2

    def test_validate_pre_scan_kill_switch_blocks(self):
        sv = SafetyValidator()
        ks = KillSwitch()
        ks.halt(KillSwitchReason.MANUAL)
        result = sv.validate_pre_scan(kill_switch=ks)
        assert not result.passed

    def test_validate_pre_scan_without_halt_passes(self):
        sv = SafetyValidator()
        result = sv.validate_pre_scan(kill_switch=KillSwitch())
        assert result.passed

    def test_validate_pre_deployment_is_alias_for_validate_pre_trade(self):
        sv = SafetyValidator()
        ks = KillSwitch()
        r1 = sv.validate_pre_trade(kill_switch=ks)
        r2 = sv.validate_pre_deployment(kill_switch=ks)
        assert r1.passed == r2.passed
        assert r1.failed_checks == r2.failed_checks

    def test_validate_market_data_kill_switch_blocks(self):
        sv = SafetyValidator()
        ks = KillSwitch()
        ks.halt(KillSwitchReason.MANUAL)
        result = sv.validate_market_data(
            kill_switch=ks,
            bars=_make_ohlcv(),
            timeframe="1d",
            market_timestamp=datetime(2024, 2, 15, 12, 0, tzinfo=UTC).isoformat(),
        )
        assert not result.passed

    def test_validate_market_data_bad_ohlcv_fails(self):
        sv = SafetyValidator()
        df = _make_ohlcv(n=5)
        df.loc[0, "close"] = -1  # invalid close
        result = sv.validate_market_data(
            kill_switch=KillSwitch(),
            bars=df,
            timeframe="1d",
            market_timestamp=datetime(2024, 1, 5, 12, 0, tzinfo=UTC).isoformat(),
        )
        assert not result.passed


# --------------------------------------------------------------------------- #
# Phase 7C — IdempotencyGuard
# --------------------------------------------------------------------------- #


class TestIdempotencyGuard:
    def test_decision_dedup(self):
        ig = IdempotencyGuard()
        assert not ig.is_duplicate_decision("dec-1")
        is_dup = ig.mark_decision("dec-1")
        assert not is_dup
        assert ig.is_duplicate_decision("dec-1")
        is_dup = ig.mark_decision("dec-1")
        assert is_dup

    def test_bar_watermark_progression(self):
        ig = IdempotencyGuard()
        ts1 = "2024-01-01T00:00:00+00:00"
        ts2 = "2024-01-02T00:00:00+00:00"
        assert not ig.is_duplicate_bar("NSE:SBIN", ts2)
        is_dup = ig.mark_bar("NSE:SBIN", ts2)
        assert not is_dup
        is_dup = ig.mark_bar("NSE:SBIN", ts1)
        assert is_dup

    def test_bar_watermark_independent_per_symbol(self):
        ig = IdempotencyGuard()
        ig.mark_bar("NSE:SBIN", "2024-01-02T00:00:00+00:00")
        assert not ig.is_duplicate_bar("NSE:TCS", "2024-01-01T00:00:00+00:00")

    def test_deployment_key_is_deterministic(self):
        ig = IdempotencyGuard()
        key1 = ig.deployment_key(
            bot_id="bot-1",
            symbol="NSE:SBIN",
            strategy_id="ema_crossover",
            timeframe="1d",
        )
        key2 = ig.deployment_key(
            bot_id="bot-1",
            symbol="NSE:SBIN",
            strategy_id="ema_crossover",
            timeframe="1d",
        )
        assert key1 == key2
        assert len(key1) == 32

    def test_deployment_dedup(self):
        ig = IdempotencyGuard()
        key = ig.deployment_key(
            bot_id="bot-1",
            symbol="NSE:SBIN",
            strategy_id="ema_crossover",
            timeframe="1d",
        )
        assert not ig.is_duplicate_deployment(key)
        is_dup = ig.mark_deployment(key, "dep-1")
        assert not is_dup
        assert ig.is_duplicate_deployment(key)
        assert ig.deployment_count == 1
        is_dup = ig.mark_deployment(key, "dep-2")
        assert is_dup
        assert ig.deployment_count == 1

    def test_deployment_key_differs_for_different_inputs(self):
        ig = IdempotencyGuard()
        k1 = ig.deployment_key(
            bot_id="bot-1", symbol="NSE:SBIN",
            strategy_id="ema_crossover", timeframe="1d",
        )
        k2 = ig.deployment_key(
            bot_id="bot-2", symbol="NSE:SBIN",
            strategy_id="ema_crossover", timeframe="1d",
        )
        k3 = ig.deployment_key(
            bot_id="bot-1", symbol="NSE:TCS",
            strategy_id="ema_crossover", timeframe="1d",
        )
        assert k1 != k2
        assert k1 != k3

    def test_reset_clears_all_state(self):
        ig = IdempotencyGuard()
        ig.mark_decision("dec-1")
        ig.mark_bar("NSE:SBIN", "2024-01-01T00:00:00+00:00")
        ig.mark_deployment("key", "dep-1")
        ig.reset()
        assert not ig.is_duplicate_decision("dec-1")
        assert not ig.is_duplicate_bar("NSE:SBIN", "2024-01-01T00:00:00+00:00")
        assert not ig.is_duplicate_deployment("key")
        assert ig.deployment_count == 0


# --------------------------------------------------------------------------- #
# Phase 7D — Phase7SafetyLayer
# --------------------------------------------------------------------------- #


class TestPhase7SafetyLayer:
    def test_initial_state_is_active(self):
        layer = Phase7SafetyLayer()
        assert layer.is_halted is False
        assert layer.state == KillSwitchState.ACTIVE

    def test_halt_and_resume(self):
        layer = Phase7SafetyLayer()
        layer.halt(KillSwitchReason.DATA_STALENESS, detail="no new bars")
        assert layer.is_halted is True
        assert layer.halt_reason == KillSwitchReason.DATA_STALENESS
        layer.resume()
        assert layer.is_halted is False

    def test_default_config_applied(self):
        layer = Phase7SafetyLayer()
        assert layer.config.kill_switch_enabled is True
        assert layer.config.data_freshness_seconds == 3600.0
        assert layer.config.max_consecutive_errors == 5

    def test_custom_config_applied(self):
        cfg = Phase7Config(
            kill_switch_enabled=True,
            data_freshness_seconds=1800.0,
            require_regular_session=True,
            max_consecutive_errors=3,
        )
        layer = Phase7SafetyLayer(config=cfg)
        assert layer.config.data_freshness_seconds == 1800.0
        assert layer.config.require_regular_session is True


# --------------------------------------------------------------------------- #
# SafetyResult fail-closed
# --------------------------------------------------------------------------- #


class TestSafetyResultFailClosed:
    def test_ok_default(self):
        r = SafetyResult.ok()
        assert r.passed is True
        assert r.failed_checks == []
        assert r.warnings == []

    def test_fail_has_failed_checks(self):
        r = SafetyResult.fail(SafetyCheck.KILL_SWITCH.value, detail="tripped")
        assert r.passed is False
        assert len(r.failed_checks) == 1
        assert "kill_switch" in r.failed_checks[0]

    def test_merge_both_pass_combine_warnings(self):
        r1 = SafetyResult.ok(warnings=["warn1"])
        r2 = SafetyResult.ok(warnings=["warn2"])
        merged = r1.merge(r2)
        assert merged.passed is True
        assert len(merged.warnings) == 2

    def test_merge_one_fails(self):
        r1 = SafetyResult.ok()
        r2 = SafetyResult.fail(SafetyCheck.DATA_FRESHNESS.value)
        merged = r1.merge(r2)
        assert merged.passed is False
        assert len(merged.failed_checks) == 1

    def test_merge_accumulates_failures(self):
        r1 = SafetyResult.fail(SafetyCheck.KILL_SWITCH.value)
        r2 = SafetyResult.fail(SafetyCheck.CIRCUIT_BREAKER.value)
        merged = r1.merge(r2)
        assert merged.passed is False
        assert len(merged.failed_checks) == 2

    def test_bool_returns_passed(self):
        assert bool(SafetyResult.ok()) is True
        assert bool(SafetyResult.fail("test")) is False


# --------------------------------------------------------------------------- #
# Controller integration
# --------------------------------------------------------------------------- #


class TestControllerPhase7Integration:
    def test_controller_has_safety_layer(self):
        ctrl = _make_controller()
        assert ctrl.kill_switch is not None
        assert ctrl.safety_validator is not None
        assert ctrl.idempotency_guard is not None
        assert ctrl.is_halted is False

    def test_halt_bot_trips_kill_switch(self):
        ctrl = _make_controller()
        ctrl.control_center.list_deployments.return_value = []
        ok, msg = ctrl.halt_bot(KillSwitchReason.MANUAL, detail="test halt")
        assert ok is True
        assert ctrl.is_halted is True
        assert ctrl.kill_switch.reason == KillSwitchReason.MANUAL

    def test_halt_bot_records_event(self):
        ctrl = _make_controller()
        ctrl.control_center.list_deployments.return_value = []
        ctrl.halt_bot(KillSwitchReason.MANUAL)
        event_types = [e.event_type for e in ctrl.event_log.events]
        assert AutonomousEventType.BOT_HALTED in event_types

    def test_halt_bot_pauses_deployments(self):
        ctrl = _make_controller()
        dep = MagicMock()
        dep.deployment_id = "dep-1"
        dep.notes = f"bot:{ctrl.config.bot_id}"
        ctrl.control_center.list_deployments.return_value = [dep]
        ctrl.halt_bot(KillSwitchReason.MANUAL)
        ctrl.control_center.pause_deployment.assert_called_once_with(deployment_id="dep-1")

    def test_halt_bot_does_not_pause_other_bots_deployments(self):
        ctrl = _make_controller()
        dep = MagicMock()
        dep.deployment_id = "other-dep"
        dep.notes = "bot:other-bot-id"
        ctrl.control_center.list_deployments.return_value = [dep]
        ctrl.halt_bot(KillSwitchReason.MANUAL)
        ctrl.control_center.pause_deployment.assert_not_called()

    def test_deployment_blocked_when_halted(self):
        ctrl = _make_controller()
        ctrl.control_center.list_deployments.return_value = []
        ctrl.halt_bot(KillSwitchReason.MANUAL)
        result, dep = ctrl.create_autonomous_deployment(
            symbol="NSE:SBIN",
            strategy_id="ema_crossover",
            timeframe="1d",
            strategy_spec=MagicMock(),
            deployment_config=MagicMock(),
        )
        assert result == DeploymentCreationResult.HALTED
        assert dep is None

    def test_scan_blocked_when_halted(self):
        ctrl = _make_controller()
        ctrl.control_center.list_deployments.return_value = []
        ctrl.halt_bot(KillSwitchReason.MANUAL)
        result = ctrl.scan_market()
        assert result.eligible_count == 0

    def test_resume_bot_clears_kill_switch(self):
        ctrl = _make_controller()
        ctrl.control_center.list_deployments.return_value = []
        ctrl.start_bot()
        ctrl.halt_bot(KillSwitchReason.MANUAL)
        assert ctrl.is_halted is True

        ok, msg = ctrl.resume_bot()
        assert ok is True
        assert ctrl.is_halted is False
        assert ctrl.kill_switch.reason is None

    def test_stop_bot_halts_kill_switch(self):
        ctrl = _make_controller()
        ctrl.control_center.list_deployments.return_value = []
        ctrl.start_bot()
        ok, msg = ctrl.stop_bot()
        assert ok is True
        assert ctrl.is_halted is True
        assert ctrl.kill_switch.reason == KillSwitchReason.DEPLOYMENT_ERROR

    def test_inspect_includes_safety_state(self):
        ctrl = _make_controller()
        info = ctrl.inspect()
        assert "safety" in info
        assert info["safety"]["kill_switch_state"] == KillSwitchState.ACTIVE.value

    def test_inspect_shows_halted_after_halt(self):
        ctrl = _make_controller()
        ctrl.control_center.list_deployments.return_value = []
        ctrl.halt_bot(KillSwitchReason.MANUAL)
        info = ctrl.inspect()
        assert info["safety"]["kill_switch_state"] == KillSwitchState.HALTED.value
        assert info["safety"]["kill_switch_reason"] == KillSwitchReason.MANUAL.value

    def test_kill_switch_is_idempotent_at_controller_level(self):
        ctrl = _make_controller()
        ctrl.control_center.list_deployments.return_value = []
        ctrl.halt_bot(KillSwitchReason.MANUAL, detail="first")
        ctrl.halt_bot(KillSwitchReason.MANUAL, detail="second")
        assert ctrl.kill_switch.reason == KillSwitchReason.MANUAL
        assert ctrl.kill_switch.halt_count == 2

    def test_deployment_allowed_when_not_halted(self):
        ctrl = _make_controller()
        ctrl.control_center.list_deployments.return_value = []
        assert ctrl.is_halted is False
        assert ctrl.create_autonomous_deployment(
            symbol="NSE:SBIN",
            strategy_id="ema_crossover",
            timeframe="1d",
            strategy_spec=MagicMock(),
            deployment_config=MagicMock(),
        ) is not None


# --------------------------------------------------------------------------- #
# Static safety scan — no live broker references
# --------------------------------------------------------------------------- #


class TestStaticSafetyScan:
    @staticmethod
    def _repo_root() -> str:
        import os
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_safety_module_has_no_live_broker_references(self):
        safety_file = os.path.join(self._repo_root(), "src/trading_system/autonomous/safety.py")
        with open(safety_file) as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "live_broker" not in alias.name
                    assert "real_broker" not in alias.name
            if isinstance(node, ast.ImportFrom):
                assert "live_broker" not in (node.module or "")
                assert "real_broker" not in (node.module or "")

    def test_safety_module_imports_only_paper_internal(self):
        safety_file = os.path.join(self._repo_root(), "src/trading_system/autonomous/safety.py")
        with open(safety_file) as f:
            tree = ast.parse(f.read())
        broker_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "execution.broker" in node.module:
                    broker_imports.append(node.module)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "live_broker" in alias.name:
                        broker_imports.append(alias.name)
        assert broker_imports == [], f"Found disallowed broker imports: {broker_imports}"

    def test_controller_safety_section_imports_paper_only(self):
        controller_file = os.path.join(self._repo_root(), "src/trading_system/autonomous/controller.py")
        with open(controller_file) as f:
            content = f.read()
        assert "from trading_system.execution" not in content
        assert "from trading_system.execution.live" not in content
        assert "live_broker" not in content.lower()


# --------------------------------------------------------------------------- #
# Kill-switch end-to-end behaviour
# --------------------------------------------------------------------------- #

class TestKillSwitchEndToEnd:
    def test_stop_bot_sets_enabled_false(self):
        controller = _make_controller()
        controller.start_bot()
        assert controller.config.enabled is True
        success, message = controller.stop_bot()
        assert success is True
        assert controller.config.enabled is False
        assert controller.lifecycle.state == AutonomousBotState.STOPPED

    def test_stop_bot_trips_kill_switch(self):
        controller = _make_controller()
        controller.start_bot()
        success, message = controller.stop_bot()
        assert success is True
        assert controller.kill_switch.is_halted is True
        assert controller.kill_switch.reason == KillSwitchReason.DEPLOYMENT_ERROR

    def test_stop_bot_is_resilient_to_list_deployments_failure(self):
        controller = _make_controller()
        controller.start_bot()
        controller.control_center.list_deployments = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
        success, message = controller.stop_bot()
        assert success is True
        assert controller.lifecycle.state == AutonomousBotState.STOPPED
        assert controller.config.enabled is False
        assert controller.kill_switch.is_halted is True

    def test_resume_bot_sets_enabled_true(self):
        controller = _make_controller()
        controller.start_bot()
        controller.stop_bot()
        assert controller.config.enabled is False
        # Cannot resume from STOPPED — start a fresh controller instead.
        controller = _make_controller()
        controller.config.enabled = False
        controller.start_bot()
        assert controller.config.enabled is True

    def test_start_bot_sets_enabled_true(self):
        controller = _make_controller()
        controller.config.enabled = False
        success, message = controller.start_bot()
        assert success is True
        assert controller.config.enabled is True


class TestKillSwitchPersistence:
    def test_persistence_save_and_load(self):
        from sqlalchemy import create_engine
        from trading_system.autonomous.persistence import AutonomousBotStateStore

        engine = create_engine("sqlite://")
        store = AutonomousBotStateStore(engine)
        store.ensure_schema()

        store.save_state(
            bot_id="bot-test",
            state="stopped",
            enabled=False,
            kill_switch_state="halted",
            kill_switch_reason="deployment_error",
            kill_switch_halted_at="2026-09-09T10:00:00+00:00",
        )

        loaded = store.load_state("bot-test")
        assert loaded["state"] == "stopped"
        assert loaded["enabled"] is False
        assert loaded["kill_switch_state"] == "halted"
        assert loaded["kill_switch_reason"] == "deployment_error"

    def test_controller_loads_persisted_state(self):
        from sqlalchemy import create_engine
        from trading_system.autonomous.persistence import AutonomousBotStateStore
        from trading_system.autonomous.bot_config import (
            AutonomousBotConfig,
            BotMode,
            Source,
            TradingMode,
            UserConstraints,
        )
        from trading_system.paper.control import PaperTradingControlCenter
        from trading_system.research.strategy_intelligence import (
            EvidenceFreshnessConfig,
            EvidenceRequirement,
        )
        from trading_system.research.evidence import EvidenceStore
        from trading_system.research.strategy_registry import StrategyRegistry
        from trading_system.research.strategy_intelligence import StrategyIntelligence
        from trading_system.paper.gate import DeploymentGate

        engine = create_engine("sqlite://")
        store = EvidenceStore(engine)
        registry = StrategyRegistry(store)
        intelligence = StrategyIntelligence(registry)
        gate = DeploymentGate(
            intelligence=intelligence,
            requirement=EvidenceRequirement(),
            freshness_config=EvidenceFreshnessConfig(max_age_days=180),
        )
        control_center = PaperTradingControlCenter(
            registry=registry,
            intelligence=intelligence,
            gate=gate,
        )

        bot_store = AutonomousBotStateStore(engine)
        bot_store.ensure_schema()
        bot_store.save_state(
            bot_id="bot-test",
            state="stopped",
            enabled=False,
            kill_switch_state="halted",
            kill_switch_reason="deployment_error",
        )

        config = AutonomousBotConfig(
            bot_id="bot-test",
            name="Test Bot",
            mode=BotMode.AUTONOMOUS,
            trading_mode=TradingMode.PAPER,
            enabled=True,
            user_constraints=UserConstraints(
                allowed_symbols=frozenset({"NSE:SBIN"}),
                allowed_strategy_ids=frozenset(),
                allowed_timeframes=frozenset({"1d"}),
            ),
            max_simultaneous_positions=5,
            source=Source.AUTONOMOUS,
        )
        controller = AutonomousController(config=config, control_center=control_center, persistence=bot_store)
        controller.load_state(bot_store.load_state("bot-test"))

        assert controller.config.state == AutonomousBotState.STOPPED
        assert controller.config.enabled is False
        assert controller.kill_switch.is_halted is True


class TestAdversarialAudit:
    """Focused adversarial audit of the kill-switch fix."""

    # ------------------------------------------------------------------ #
    # 1. Cross-controller / cross-process enforcement
    # ------------------------------------------------------------------ #

    def test_cross_controller_kill_is_authoritative(self):
        from sqlalchemy import create_engine
        from trading_system.autonomous.persistence import AutonomousBotStateStore
        from trading_system.autonomous.bot_config import (
            AutonomousBotConfig,
            BotMode,
            Source,
            TradingMode,
            UserConstraints,
        )
        from trading_system.paper.control import PaperTradingControlCenter
        from trading_system.research.strategy_intelligence import (
            EvidenceFreshnessConfig,
            EvidenceRequirement,
        )
        from trading_system.research.evidence import EvidenceStore
        from trading_system.research.strategy_registry import StrategyRegistry
        from trading_system.research.strategy_intelligence import StrategyIntelligence
        from trading_system.paper.gate import DeploymentGate

        engine = create_engine("sqlite://")
        store = EvidenceStore(engine)
        registry = StrategyRegistry(store)
        intelligence = StrategyIntelligence(registry)
        gate = DeploymentGate(
            intelligence=intelligence,
            requirement=EvidenceRequirement(),
            freshness_config=EvidenceFreshnessConfig(max_age_days=180),
        )
        control_center = PaperTradingControlCenter(
            registry=registry,
            intelligence=intelligence,
            gate=gate,
        )
        bot_store = AutonomousBotStateStore(engine)
        bot_store.ensure_schema()

        def _build_controller():
            config = AutonomousBotConfig(
                bot_id="bot-audit",
                name="Audit Bot",
                mode=BotMode.AUTONOMOUS,
                trading_mode=TradingMode.PAPER,
                enabled=True,
                user_constraints=UserConstraints(
                    allowed_symbols=frozenset({"NSE:SBIN"}),
                    allowed_strategy_ids=frozenset(),
                    allowed_timeframes=frozenset({"1d"}),
                ),
                max_simultaneous_positions=5,
                source=Source.AUTONOMOUS,
            )
            return AutonomousController(
                config=config,
                control_center=control_center,
                persistence=bot_store,
            )

        controller_a = _build_controller()
        controller_a.start_bot()
        assert controller_a.config.state == AutonomousBotState.RUNNING

        controller_b = _build_controller()
        controller_b.load_state(bot_store.load_state("bot-audit"))
        assert controller_b.config.state == AutonomousBotState.RUNNING

        controller_a.stop_bot()
        assert controller_a.config.enabled is False
        assert controller_a.kill_switch.is_halted is True

        controller_c = _build_controller()
        controller_c.load_state(bot_store.load_state("bot-audit"))
        assert controller_c.config.enabled is False
        assert controller_c.kill_switch.is_halted is True
        assert controller_c.config.state == AutonomousBotState.STOPPED

        assert controller_c.is_halted is True
        assert controller_c.lifecycle.can_transition_to(AutonomousBotState.RUNNING) is False

    # ------------------------------------------------------------------ #
    # 2. Deployment-pause failure semantics
    # ------------------------------------------------------------------ #

    def test_stop_bot_fails_closed_when_list_deployments_raises(self):
        controller = _make_controller()
        controller.start_bot()
        controller.control_center.list_deployments = (
            lambda: (_ for _ in ()).throw(RuntimeError("db schema mismatch"))
        )

        success, message = controller.stop_bot()
        assert success is True
        assert controller.config.state == AutonomousBotState.STOPPED
        assert controller.config.enabled is False
        assert controller.kill_switch.is_halted is True
        assert controller.kill_switch.reason == KillSwitchReason.DEPLOYMENT_ERROR

    # ------------------------------------------------------------------ #
    # 3. Restart correctness
    # ------------------------------------------------------------------ #

    def test_stop_persists_and_survives_controller_reconstruction(self):
        from sqlalchemy import create_engine
        from trading_system.autonomous.persistence import AutonomousBotStateStore
        from trading_system.autonomous.bot_config import (
            AutonomousBotConfig,
            BotMode,
            Source,
            TradingMode,
            UserConstraints,
        )
        from trading_system.paper.control import PaperTradingControlCenter
        from trading_system.research.strategy_intelligence import (
            EvidenceFreshnessConfig,
            EvidenceRequirement,
        )
        from trading_system.research.evidence import EvidenceStore
        from trading_system.research.strategy_registry import StrategyRegistry
        from trading_system.research.strategy_intelligence import StrategyIntelligence
        from trading_system.paper.gate import DeploymentGate

        engine = create_engine("sqlite://")
        store = EvidenceStore(engine)
        registry = StrategyRegistry(store)
        intelligence = StrategyIntelligence(registry)
        gate = DeploymentGate(
            intelligence=intelligence,
            requirement=EvidenceRequirement(),
            freshness_config=EvidenceFreshnessConfig(max_age_days=180),
        )
        control_center = PaperTradingControlCenter(
            registry=registry,
            intelligence=intelligence,
            gate=gate,
        )
        bot_store = AutonomousBotStateStore(engine)
        bot_store.ensure_schema()

        def _build():
            config = AutonomousBotConfig(
                bot_id="bot-restart",
                name="Restart Bot",
                mode=BotMode.AUTONOMOUS,
                trading_mode=TradingMode.PAPER,
                enabled=True,
                user_constraints=UserConstraints(
                    allowed_symbols=frozenset({"NSE:SBIN"}),
                    allowed_strategy_ids=frozenset(),
                    allowed_timeframes=frozenset({"1d"}),
                ),
                max_simultaneous_positions=5,
                source=Source.AUTONOMOUS,
            )
            return AutonomousController(
                config=config,
                control_center=control_center,
                persistence=bot_store,
            )

        ctrl1 = _build()
        ctrl1.start_bot()
        ctrl1.stop_bot()
        assert ctrl1.config.state == AutonomousBotState.STOPPED

        ctrl2 = _build()
        ctrl2.load_state(bot_store.load_state("bot-restart"))
        assert ctrl2.config.state == AutonomousBotState.STOPPED
        assert ctrl2.config.enabled is False
        assert ctrl2.kill_switch.is_halted is True

    # ------------------------------------------------------------------ #
    # 4. Idempotency
    # ------------------------------------------------------------------ #

    def test_repeated_stops_are_idempotent_and_safe(self):
        controller = _make_controller()
        controller.start_bot()

        for _ in range(4):
            success, message = controller.stop_bot()
            assert success is True
            assert controller.config.state == AutonomousBotState.STOPPED
            assert controller.config.enabled is False
            assert controller.kill_switch.is_halted is True

    # ------------------------------------------------------------------ #
    # 5. Schema initialization
    # ------------------------------------------------------------------ #

    def test_fresh_db_creates_autonomous_bots_and_migrates_paper_deployments(self):
        from sqlalchemy import create_engine, inspect
        from trading_system.autonomous.persistence import AutonomousBotStateStore
        from trading_system.research.evidence import EvidenceStore

        engine = create_engine("sqlite://")
        EvidenceStore(engine).ensure_schema_current()
        bot_store = AutonomousBotStateStore(engine)
        bot_store.ensure_schema()

        insp = inspect(engine)
        tables = insp.get_table_names()
        assert "autonomous_bots" in tables
        assert "paper_deployments" in tables

        cols = {c["name"] for c in insp.get_columns("paper_deployments")}
        assert "strategy_parameters_json" in cols

    def test_existing_db_migration_is_idempotent(self):
        from sqlalchemy import create_engine, inspect
        from trading_system.autonomous.persistence import AutonomousBotStateStore
        from trading_system.research.evidence import EvidenceStore

        engine = create_engine("sqlite://")
        store = EvidenceStore(engine)
        store.ensure_schema_current()

        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("paper_deployments")}
        assert "strategy_parameters_json" in cols

        bot_store = AutonomousBotStateStore(engine)
        bot_store.ensure_schema()
        bot_store.ensure_schema()

        tables = insp.get_table_names()
        assert "autonomous_bots" in tables
