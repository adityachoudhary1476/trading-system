"""Tests for the greeks policy configuration (decision D2).

Every default must equal today's behaviour: the layer ships dark, so an
environment with no AUTONOMOUS_GREEKS_* variable set has to produce a fully
off config and a bot constructed without thinking about greeks must carry
that same off config. The env tests are the rollback story — one variable
switches a phase on, unsetting it switches it back off, and anything
unrecognised or malformed fails safe to off instead of raising. PAPER only.
"""
from __future__ import annotations

import pytest

from trading_system.autonomous.bot_config import (
    AutonomousBotConfig,
    GreeksPolicyConfig,
    UserConstraints,
    greeks_policy_from_env,
)

GREEKS_ENV_VARS = (
    "AUTONOMOUS_GREEKS_ENABLED",
    "AUTONOMOUS_GREEKS_SHADOW",
    "AUTONOMOUS_GREEKS_TARGET_DELTA",
    "AUTONOMOUS_GREEKS_RISK_SIZING",
    "AUTONOMOUS_GREEKS_PORTFOLIO_LIMITS",
    "AUTONOMOUS_GREEKS_MAX_NET_DELTA",
    "AUTONOMOUS_GREEKS_MAX_NET_GAMMA",
    "AUTONOMOUS_GREEKS_MAX_NET_THETA",
    "AUTONOMOUS_GREEKS_MAX_NET_VEGA",
)

FLAG_ATTRS = ("enabled", "shadow", "risk_sizing", "portfolio_limits")


@pytest.fixture(autouse=True)
def _clean_greeks_env(monkeypatch):
    """Strip every greeks env var so no test can inherit another's state."""
    for name in GREEKS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class TestDefaults:
    def test_every_field_defaults_to_todays_behaviour(self):
        cfg = GreeksPolicyConfig()
        assert cfg.enabled is False
        assert cfg.shadow is False
        assert cfg.target_delta is None
        assert cfg.delta_tolerance == pytest.approx(0.10)
        assert cfg.risk_sizing is False
        assert cfg.portfolio_limits is False
        assert cfg.max_net_delta is None
        assert cfg.max_net_gamma is None
        assert cfg.max_net_theta is None
        assert cfg.max_net_vega is None

    def test_bot_config_wires_greeks_policy_off_by_default(self):
        cfg = AutonomousBotConfig(
            bot_id="bot-1",
            name="Test Bot",
            user_constraints=UserConstraints(),
        )
        assert cfg.greeks_policy == GreeksPolicyConfig()
        assert cfg.greeks_policy.enabled is False


class TestEnvFlags:
    def test_no_vars_set_returns_all_off(self):
        cfg = greeks_policy_from_env()
        assert cfg == GreeksPolicyConfig()
        assert all(getattr(cfg, attr) is False for attr in FLAG_ATTRS)
        assert cfg.target_delta is None
        assert cfg.delta_tolerance == pytest.approx(0.10)

    @pytest.mark.parametrize(
        "spelling", ["1", "true", "TRUE", "yes", "Yes", "on", "ON", "  on  "]
    )
    def test_truthy_spellings_enable_the_master_switch(self, monkeypatch, spelling):
        monkeypatch.setenv("AUTONOMOUS_GREEKS_ENABLED", spelling)
        assert greeks_policy_from_env().enabled is True

    @pytest.mark.parametrize(
        "spelling", ["0", "false", "FALSE", "no", "No", "off", "", "  ", "maybe", "2"]
    )
    def test_falsy_or_unknown_spellings_stay_off(self, monkeypatch, spelling):
        monkeypatch.setenv("AUTONOMOUS_GREEKS_ENABLED", spelling)
        assert greeks_policy_from_env().enabled is False

    @pytest.mark.parametrize(
        ("var_name", "attr"),
        [
            ("AUTONOMOUS_GREEKS_ENABLED", "enabled"),
            ("AUTONOMOUS_GREEKS_SHADOW", "shadow"),
            ("AUTONOMOUS_GREEKS_RISK_SIZING", "risk_sizing"),
            ("AUTONOMOUS_GREEKS_PORTFOLIO_LIMITS", "portfolio_limits"),
        ],
    )
    def test_each_flag_reads_only_its_own_env_var(self, monkeypatch, var_name, attr):
        monkeypatch.setenv(var_name, "true")
        cfg = greeks_policy_from_env()
        assert getattr(cfg, attr) is True
        for other in FLAG_ATTRS:
            if other != attr:
                assert getattr(cfg, other) is False


class TestTargetDeltaEnv:
    def test_valid_float_parses(self, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_GREEKS_TARGET_DELTA", "0.25")
        assert greeks_policy_from_env().target_delta == pytest.approx(0.25)

    def test_surrounding_whitespace_is_tolerated(self, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_GREEKS_TARGET_DELTA", "  0.4  ")
        assert greeks_policy_from_env().target_delta == pytest.approx(0.4)

    def test_malformed_float_returns_none_without_raising(self, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_GREEKS_TARGET_DELTA", "abc")
        assert greeks_policy_from_env().target_delta is None

    def test_blank_value_returns_none(self, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_GREEKS_TARGET_DELTA", "   ")
        assert greeks_policy_from_env().target_delta is None

    def test_non_finite_value_returns_none(self, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_GREEKS_TARGET_DELTA", "nan")
        assert greeks_policy_from_env().target_delta is None

    def test_absent_value_returns_none(self):
        assert greeks_policy_from_env().target_delta is None


class TestPortfolioLimitEnv:
    @pytest.mark.parametrize(
        ("var_name", "attr"),
        [
            ("AUTONOMOUS_GREEKS_MAX_NET_DELTA", "max_net_delta"),
            ("AUTONOMOUS_GREEKS_MAX_NET_GAMMA", "max_net_gamma"),
            ("AUTONOMOUS_GREEKS_MAX_NET_THETA", "max_net_theta"),
            ("AUTONOMOUS_GREEKS_MAX_NET_VEGA", "max_net_vega"),
        ],
    )
    def test_each_cap_reads_only_its_own_env_var(self, monkeypatch, var_name, attr):
        monkeypatch.setenv(var_name, "150.0")
        cfg = greeks_policy_from_env()
        assert getattr(cfg, attr) == pytest.approx(150.0)
        for other in ("max_net_delta", "max_net_gamma", "max_net_theta", "max_net_vega"):
            if other != attr:
                assert getattr(cfg, other) is None

    def test_all_four_caps_parse(self, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_GREEKS_MAX_NET_DELTA", "12.5")
        monkeypatch.setenv("AUTONOMOUS_GREEKS_MAX_NET_GAMMA", "0.5")
        monkeypatch.setenv("AUTONOMOUS_GREEKS_MAX_NET_THETA", "300")
        monkeypatch.setenv("AUTONOMOUS_GREEKS_MAX_NET_VEGA", "80")
        cfg = greeks_policy_from_env()
        assert cfg.max_net_delta == pytest.approx(12.5)
        assert cfg.max_net_gamma == pytest.approx(0.5)
        assert cfg.max_net_theta == pytest.approx(300.0)
        assert cfg.max_net_vega == pytest.approx(80.0)

    def test_zero_is_a_real_cap(self, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_GREEKS_MAX_NET_DELTA", "0")
        assert greeks_policy_from_env().max_net_delta == pytest.approx(0.0)

    def test_malformed_cap_returns_none(self, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_GREEKS_MAX_NET_DELTA", "wide")
        assert greeks_policy_from_env().max_net_delta is None

    def test_non_finite_cap_returns_none(self, monkeypatch):
        monkeypatch.setenv("AUTONOMOUS_GREEKS_MAX_NET_VEGA", "inf")
        assert greeks_policy_from_env().max_net_vega is None


class TestProductionDefaults:
    """The production worker/API default the layer on without overriding the operator.

    ``apply_greeks_production_defaults`` runs at startup for both the scheduler
    worker and the FastAPI API, and must fill only unset variables: an operator
    setting any AUTONOMOUS_GREEKS_* value (including a falsy one) keeps it, so
    the rollout stays reversible.
    """

    @staticmethod
    def _isolate(monkeypatch):
        import os

        monkeypatch.setattr(os, "environ", dict(os.environ))

    def test_fills_the_full_behavioural_baseline(self, monkeypatch):
        import os

        from trading_system.autonomous.bot_config import (
            GREEKS_PRODUCTION_DEFAULTS,
            apply_greeks_production_defaults,
        )

        self._isolate(monkeypatch)
        for name in GREEKS_PRODUCTION_DEFAULTS:
            os.environ.pop(name, None)

        apply_greeks_production_defaults()

        policy = greeks_policy_from_env()
        assert policy.enabled is True
        assert policy.shadow is False
        assert policy.target_delta == pytest.approx(0.50)
        assert policy.risk_sizing is True
        assert policy.portfolio_limits is True
        assert policy.max_net_delta == pytest.approx(5.0)
        assert os.environ["AUTONOMOUS_GREEKS_EXITS_ENABLED"].strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    def test_operator_value_is_never_overridden(self, monkeypatch):
        from trading_system.autonomous.bot_config import (
            apply_greeks_production_defaults,
        )

        self._isolate(monkeypatch)
        monkeypatch.setenv("AUTONOMOUS_GREEKS_ENABLED", "false")
        monkeypatch.setenv("AUTONOMOUS_GREEKS_TARGET_DELTA", "0.30")

        apply_greeks_production_defaults()

        policy = greeks_policy_from_env()
        assert policy.enabled is False
        assert policy.target_delta == pytest.approx(0.30)