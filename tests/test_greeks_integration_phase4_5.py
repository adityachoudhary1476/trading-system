"""Phase 4/5 — portfolio greek limits and greeks-aware exits wired in.

These tests pin how the Phase 4 (``check_portfolio_limits``) and Phase 5
(``evaluate_greeks_exit``) engines are spent by ``AutonomousController`` and
``AutonomousPortfolio``.

The load-bearing invariants mirror Phases 0-3:

  * D2 (default OFF): the master switch off keeps both phases inert, and the
    exit path returns nothing rather than a greeks reason.
  * D1 (fail OPEN): an unpriceable candidate or an unmeasurable position never
    blocks a trade or forces an exit.
  * D6 (accounting): a breached cap is attributed through ``GreeksMetrics``.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from sqlalchemy import create_engine

from trading_system.autonomous.bot_config import (
    AutonomousBotConfig,
    BotMode,
    GreeksPolicyConfig,
    Source,
    TradingMode,
    UserConstraints,
)
from trading_system.autonomous.controller import AutonomousController
from trading_system.autonomous.portfolio import AutonomousPortfolio
from trading_system.paper.control import PaperTradingControlCenter
from trading_system.research.evidence import EvidenceStore
from trading_system.research.strategy_intelligence import StrategyIntelligence
from trading_system.research.strategy_registry import StrategyRegistry

NOW = datetime(2024, 1, 15, 10, 30, tzinfo=timezone.utc)
REF_DATE = date(2024, 1, 15)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

def _controller() -> AutonomousController:
    engine = create_engine("sqlite://")
    center = PaperTradingControlCenter(
        registry=StrategyRegistry(EvidenceStore(engine)),
        intelligence=StrategyIntelligence(StrategyRegistry(EvidenceStore(engine))),
    )
    config = AutonomousBotConfig(
        bot_id="bot-phase45-test",
        name="phase 4/5 test bot",
        mode=BotMode.AUTONOMOUS,
        trading_mode=TradingMode.PAPER,
        enabled=True,
        user_constraints=UserConstraints(),
        source=Source.AUTONOMOUS,
        max_simultaneous_positions=5,
    )
    return AutonomousController(config=config, control_center=center)


class _Instrument:
    def __init__(self, *, strike=100.0, expiry="2024-06-27", lot_size=1):
        self.strike = strike
        self.expiry = expiry
        self.option_type = "CE"
        self.contract_id = "NFO:NIFTY|2024-06-27|100|CE"
        self.key = self.contract_id
        self.lot_size = lot_size


class _Quote:
    def __init__(self, *, ltp=100.0, implied_vol=0.15):
        self.ltp = ltp
        self.implied_vol = implied_vol


class _PositionRow:
    def __init__(self, *, qty, delta, contract_size=1):
        self.qty = qty
        self.contract_size = contract_size
        self.options_contract_id = "NFO:NIFTY|2024-06-27|100|CE"
        self.last_delta = delta
        self.entry_delta = delta


def _breach(positions, *, spot=100.0, instrument=None, quote=None, signed_qty=1.0, **limits):
    policy = GreeksPolicyConfig(portfolio_limits=True, **limits)
    return _controller()._portfolio_greeks_breach(
        positions=positions,
        spot_price=spot,
        instrument=instrument or _Instrument(),
        quote=quote or _Quote(),
        signed_quantity=signed_qty,
        option_type="CE",
        policy=policy,
        now=NOW,
    )


# --------------------------------------------------------------------------- #
# Phase 4 — controller._portfolio_greeks_breach
# --------------------------------------------------------------------------- #

class TestPhase4BreachCheck:
    def test_empty_book_candidate_beyond_cap_breaches(self) -> None:
        breach = _breach([], max_net_delta=0.10)
        assert breach is not None
        assert breach.limit_name == "max_net_delta"

    def test_empty_book_candidate_within_cap_allowed(self) -> None:
        assert _breach([], max_net_delta=100.0) is None

    def test_no_cap_configured_allows(self) -> None:
        assert _breach([]) is None

    def test_open_book_delta_is_counted(self) -> None:
        positions = [_PositionRow(qty=1.0, delta=1.5)]
        breach = _breach(positions, max_net_delta=0.10)
        assert breach is not None
        assert breach.current == pytest.approx(1.5)

    def test_unpriceable_candidate_fails_open(self) -> None:
        # No spot -> cannot price the candidate -> the layer must not decide.
        assert _breach([], spot=None, max_net_delta=0.0) is None

    def test_bad_strike_fails_open(self) -> None:
        assert _breach([], instrument=_Instrument(strike=None), max_net_delta=0.0) is None

    def test_reducing_order_is_not_blocked(self) -> None:
        # A large short candidate against a large long book reduces |net|.
        positions = [_PositionRow(qty=1.0, delta=100.0)]
        breach = _breach(positions, signed_qty=-1.0, max_net_delta=0.10)
        assert breach is None


# --------------------------------------------------------------------------- #
# Phase 4 — policy plumbing (static config + env overlay)
# --------------------------------------------------------------------------- #

class TestPhase4PolicyPlumbing:
    def test_config_fields_default_none(self) -> None:
        policy = GreeksPolicyConfig()
        assert policy.portfolio_limits is False
        assert policy.max_net_delta is None
        assert policy.max_net_gamma is None
        assert policy.max_net_theta is None
        assert policy.max_net_vega is None

    def test_env_overlay_wins_over_static(self, monkeypatch) -> None:
        ctrl = _controller()
        ctrl.config.greeks_policy = GreeksPolicyConfig(
            enabled=True, portfolio_limits=True, max_net_delta=1.0
        )
        monkeypatch.setenv("AUTONOMOUS_GREEKS_MAX_NET_DELTA", "9.0")
        policy = ctrl._effective_greeks_policy()
        assert policy.max_net_delta == pytest.approx(9.0)

    def test_static_stands_when_env_absent(self, monkeypatch) -> None:
        ctrl = _controller()
        ctrl.config.greeks_policy = GreeksPolicyConfig(
            enabled=True, portfolio_limits=True, max_net_delta=1.0
        )
        monkeypatch.delenv("AUTONOMOUS_GREEKS_MAX_NET_DELTA", raising=False)
        policy = ctrl._effective_greeks_policy()
        assert policy.max_net_delta == pytest.approx(1.0)

    def test_master_or_env_enables_layer(self, monkeypatch) -> None:
        ctrl = _controller()
        monkeypatch.setenv("AUTONOMOUS_GREEKS_ENABLED", "true")
        monkeypatch.setenv("AUTONOMOUS_GREEKS_PORTFOLIO_LIMITS", "true")
        policy = ctrl._effective_greeks_policy()
        assert policy.enabled is True
        assert policy.portfolio_limits is True


# --------------------------------------------------------------------------- #
# Phase 5 — portfolio._greeks_exit_reason
# --------------------------------------------------------------------------- #

class _PortfolioStub:
    def _now(self):
        return NOW


class _ExitPosition:
    is_option = True

    def __init__(self, *, entry_iv, last_iv, holding_seconds=3600.0):
        self.entry_delta = -0.4
        self.last_delta = -0.3
        self.entry_iv = entry_iv
        self.last_iv = last_iv
        self._holding = holding_seconds

    def holding_seconds(self, now=None):
        return self._holding


def _exit_reason(position):
    return AutonomousPortfolio._greeks_exit_reason(_PortfolioStub(), position)


class TestPhase5ExitWiring:
    def test_disabled_is_inert(self, monkeypatch) -> None:
        monkeypatch.delenv("AUTONOMOUS_GREEKS_ENABLED", raising=False)
        monkeypatch.delenv("AUTONOMOUS_GREEKS_EXITS_ENABLED", raising=False)
        # Even a perfect iv-crush setup must not fire while the layer is off.
        assert _exit_reason(_ExitPosition(entry_iv=0.20, last_iv=0.01)) is None

    def test_requires_both_switches(self, monkeypatch) -> None:
        monkeypatch.setenv("AUTONOMOUS_GREEKS_ENABLED", "true")
        monkeypatch.delenv("AUTONOMOUS_GREEKS_EXITS_ENABLED", raising=False)
        assert _exit_reason(_ExitPosition(entry_iv=0.20, last_iv=0.01)) is None

    def test_iv_crush_fires(self, monkeypatch) -> None:
        monkeypatch.setenv("AUTONOMOUS_GREEKS_ENABLED", "true")
        monkeypatch.setenv("AUTONOMOUS_GREEKS_EXITS_ENABLED", "true")
        monkeypatch.setenv("AUTONOMOUS_IV_CRUSH_FLOOR", "0.5")
        assert _exit_reason(_ExitPosition(entry_iv=0.20, last_iv=0.05)) == "iv_crush"

    def test_no_crush_above_floor_held(self, monkeypatch) -> None:
        monkeypatch.setenv("AUTONOMOUS_GREEKS_ENABLED", "true")
        monkeypatch.setenv("AUTONOMOUS_GREEKS_EXITS_ENABLED", "true")
        monkeypatch.setenv("AUTONOMOUS_IV_CRUSH_FLOOR", "0.5")
        assert _exit_reason(_ExitPosition(entry_iv=0.20, last_iv=0.19)) is None

    def test_missing_iv_held(self, monkeypatch) -> None:
        monkeypatch.setenv("AUTONOMOUS_GREEKS_ENABLED", "true")
        monkeypatch.setenv("AUTONOMOUS_GREEKS_EXITS_ENABLED", "true")
        assert _exit_reason(_ExitPosition(entry_iv=None, last_iv=0.01)) is None

    def test_floor_out_of_range_held(self, monkeypatch) -> None:
        monkeypatch.setenv("AUTONOMOUS_GREEKS_ENABLED", "true")
        monkeypatch.setenv("AUTONOMOUS_GREEKS_EXITS_ENABLED", "true")
        monkeypatch.setenv("AUTONOMOUS_IV_CRUSH_FLOOR", "1.5")
        # Out-of-range floor falls back to the 0.5 default; 0.19/0.20 > 0.5.
        assert _exit_reason(_ExitPosition(entry_iv=0.20, last_iv=0.19)) is None

    def test_min_holding_gate_blocks_early(self, monkeypatch) -> None:
        monkeypatch.setenv("AUTONOMOUS_GREEKS_ENABLED", "true")
        monkeypatch.setenv("AUTONOMOUS_GREEKS_EXITS_ENABLED", "true")
        monkeypatch.setenv("AUTONOMOUS_IV_CRUSH_MIN_HOLDING_SECONDS", "86400")
        assert _exit_reason(
            _ExitPosition(entry_iv=0.20, last_iv=0.01, holding_seconds=60.0)
        ) is None