"""Reproduction tests for the option EXIT-path symbol-resolution bugs.

These tests use production-realistic instruments — provider_symbol is the
Upstox/NSE wire token (e.g. "NSE_OPT|NIFTY25DEC24800CE") and the InternalSymbol
symbol is the YYMONDD token — so that the EXIT path's reconstruction is
exercised under the same format mismatch that occurs in production.
"""
from __future__ import annotations

from datetime import date, timedelta, datetime, timezone
from types import SimpleNamespace

import pytest

from trading_system.autonomous.bot_config import (
    AutonomousBotConfig, BotMode, Source, TradingMode, UserConstraints,
)
from trading_system.autonomous.controller import AutonomousController
from trading_system.autonomous.options.discovery import CurrentOptionDiscoverer
from trading_system.india.instrument_repository import InstrumentRepository
from trading_system.india.instruments import (
    Instrument, InstrumentType, InternalSymbol,
)
from trading_system.india.option_quotes import OptionQuote
from trading_system.paper.deployment import PaperDeploymentConfig
from trading_system.strategy_factory.contract import SignalAction

UTC = timezone.utc


# --- helpers -----------------------------------------------------------------

def _make_realistic_instrument(
    *, underlying="NIFTY", option_type="CE", strike=25000.0,
    expiry=None, lot_size=50,
):
    """Build an Instrument with a realistic Upstox wire-format symbol.

    The InternalSymbol symbol uses YYMONDD (e.g. "NIFTY25DEC24800CE") — this is
    what Upstox / NSE F&O instrument masters actually use.  The provider_symbol
    is the full Upstox wire key (e.g. "NSE_OPT|NIFTY25DEC24800CE").
    """
    if expiry is None:
        expiry = (date.today() + timedelta(days=30)).isoformat()
    # YYMONDD expiry format (Upstox/NSE convention)
    d = date.fromisoformat(expiry)
    yy = f"{d.year % 100:02d}"
    mon = d.strftime("%b").upper()
    dd = f"{d.day:02d}"
    token = f"{underlying}{yy}{mon}{int(strike)}{option_type}"
    instr = Instrument(
        internal=InternalSymbol(exchange="NSE", symbol=token),
        instrument_type=(InstrumentType.OPTION_CE if option_type == "CE"
                         else InstrumentType.OPTION_PE),
        name=underlying,
    )
    instr.provider_symbol = f"NSE_OPT|{token}"
    instr.underlying = underlying
    instr.expiry = expiry
    instr.strike = float(strike)
    instr.option_type = option_type
    instr.lot_size = lot_size
    instr.exchange_full = "NSE"
    return instr


def _build_repo():
    repo = InstrumentRepository()
    for strike in (24800, 25000, 25200):
        for ot in ("CE", "PE"):
            repo.register(_make_realistic_instrument(
                strike=float(strike), option_type=ot,
            ))
    return repo


class ValidatingQuoteProvider:
    """Quote provider that only returns a quote when provider_symbol matches
    one of the registered instruments' wire symbols.  This mirrors the real
    ``CurrentOptionQuoteProvider`` which calls the Upstox API with
    ``instrument.provider_symbol``."""

    def __init__(self, repo):
        self.repo = repo
        self.last_provider_symbol = None
        self.last_instrument_key = None

    def get_quote(self, instrument):
        self.last_provider_symbol = instrument.provider_symbol
        self.last_instrument_key = instrument.key
        # Only accept the real Upstox wire symbol (e.g. "NSE_OPT|NIFTY25DEC24800CE")
        if instrument.provider_symbol and "|" in instrument.provider_symbol:
            # Validate: the provider_symbol must be a registered wire symbol
            for reg_instr in self.repo.registry._by_contract.values():
                if reg_instr.provider_symbol == instrument.provider_symbol:
                    return OptionQuote(
                        instrument=instrument,
                        ltp=185.0 if instrument.option_type == "CE" else 210.0,
                        timestamp=datetime.now(UTC),
                        fetched_at=datetime.now(UTC),
                        bid=184.5,
                        ask=185.5,
                        source_symbol=instrument.provider_symbol,
                    )
        return None

    def is_fresh(self, quote, max_age_seconds=None):
        return True


def _build_center():
    from sqlalchemy import create_engine
    from trading_system.research.evidence import (
        EvidenceStore, EvidenceType, StrategyEvidence, StrategyStatus,
    )
    from trading_system.research.strategy_intelligence import (
        EvidenceFreshnessConfig, EvidenceRequirement, StrategyIntelligence,
    )
    from trading_system.paper.gate import DeploymentGate
    from trading_system.research.strategy_registry import (
        StrategyRegistry, evidence_identity as _ev,
    )
    from trading_system.research.strategy_lab.spec import (
        StrategySpec, field_operand, indicator_operand, make_condition,
    )
    from trading_system.paper.control import PaperTradingControlCenter

    engine = create_engine("sqlite://")
    store = EvidenceStore(engine)
    registry = StrategyRegistry(store)
    intelligence = StrategyIntelligence(registry)
    gate = DeploymentGate(
        intelligence=intelligence,
        requirement=EvidenceRequirement(
            require_walk_forward=False, require_validation=False,
            require_recent_evidence=False, min_validation_trades=0,
        ),
        freshness_config=EvidenceFreshnessConfig(max_age_days=180),
    )
    spec = StrategySpec(
        name="exit-bug-repro", symbol="NSE:NIFTY", timeframe="1d",
        indicators=[{"name": "sma", "params": {"window": 5}}],
        entry=make_condition(field_operand("close"), ">", indicator_operand("sma_5")),
        generated_by="exit-bug-repro",
    )
    strategy = registry.register_strategy(spec)
    sid = strategy.strategy_id
    registry.update_strategy_status(sid, StrategyStatus.WALK_FORWARD_VALIDATED)
    fresh = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    registry.record_evidence(StrategyEvidence(
        evidence_id=_ev(sid, EvidenceType.RESEARCH, "ds-exitbug", {"k": 1}),
        strategy_id=sid, strategy_spec_hash=strategy.spec_hash,
        evidence_type=EvidenceType.RESEARCH, dataset_id="ds-exitbug",
        configuration_json={"k": 1},
        metrics_json={"n_trades": 25},
        created_at=fresh,
    ))
    center = PaperTradingControlCenter(
        registry=registry, intelligence=intelligence, gate=gate,
    )
    return center, spec, sid


def _build_controller(center, bot_id):
    config = AutonomousBotConfig(
        bot_id=bot_id, name=f"ExitBug {bot_id}",
        mode=BotMode.AUTONOMOUS, trading_mode=TradingMode.PAPER,
        enabled=True,
        user_constraints=UserConstraints(
            allowed_symbols=frozenset({"NSE:NIFTY"}),
            allowed_strategy_ids=frozenset(),
            allowed_timeframes=frozenset({"1d"}),
            allowed_option_underlyings=frozenset({"NIFTY"}),
        ),
        max_simultaneous_positions=5, source=Source.AUTONOMOUS,
    )
    return AutonomousController(config=config, control_center=center)


def _deploy(controller, center, bot_id, strategy_id, spec):
    cfg = PaperDeploymentConfig(
        execution_mode="paper", initial_cash=100_000.0,
        allow_short=False, options_enabled=True,
        allowed_option_types=["CE", "PE"], max_options_contracts_per_trade=1,
    )
    from trading_system.autonomous.coordinator import (
        AutonomousDeploymentCoordinator, DeploymentCreationResult,
    )
    coord = AutonomousDeploymentCoordinator(
        config=controller.config, control_center=controller.control_center,
    )
    result, dep = coord.create_autonomous_deployment(
        symbol="NSE:NIFTY", strategy_id=strategy_id, timeframe="1d",
        strategy_spec=spec, deployment_config=cfg,
    )
    assert result == DeploymentCreationResult.SUCCESS
    return dep


def _buy_decision(strategy_id):
    from trading_system.strategy_factory.contract import StrategySignal
    sig = StrategySignal(
        action=SignalAction.BUY, strategy_id=strategy_id,
        timestamp=datetime.now(UTC), symbol="NSE:NIFTY",
        reference_price=25000.0, confidence=0.9,
        reason="buy for exit test", option_intent="CE",
    )
    return SimpleNamespace(
        decision_id="exit-bug-buy",
        opportunity_symbol="NSE:NIFTY",
        selected_configuration=SimpleNamespace(strategy_id=strategy_id, timeframe="1d"),
        signal=sig, action="buy",
    )


def _sell_decision(strategy_id):
    return SimpleNamespace(
        decision_id="exit-bug-sell",
        opportunity_symbol="NSE:NIFTY",
        selected_configuration=SimpleNamespace(strategy_id=strategy_id, timeframe="1d"),
        action="sell",
        signal=SimpleNamespace(
            action="sell", reference_price=25000.0, option_intent="CE",
        ),
    )


# --- tests -------------------------------------------------------------------

class TestExitSymbolResolution:
    """Bugs 1 & 2: exit quote provider_symbol + broker position key mismatch."""

    def setup_method(self):
        self.center, self.spec, self.strategy_id = _build_center()
        self.controller = _build_controller(self.center, "exit-bug-bot")
        self.dep = _deploy(
            self.controller, self.center, "exit-bug-bot", self.strategy_id, self.spec,
        )
        self.sid = self.center.find_session_for_deployment(self.dep.deployment_id)
        self.repo = _build_repo()
        self.qp = ValidatingQuoteProvider(self.repo)
        self.controller.set_option_discoverer(
            CurrentOptionDiscoverer(repository=self.repo), repository=self.repo,
        )
        self.controller.set_quote_provider(self.qp)

    def test_exit_uses_original_wire_symbol_for_quote(self):
        """Bug 1: EXIT provider_symbol must be the Upstox wire symbol, not
        the canonical contract_id."""
        # --- BUY ---
        buy_result = self.controller.execute_option_order(
            decision=_buy_decision(self.strategy_id),
            spot_price=25000.0,
            deployment_id=self.dep.deployment_id,
            session_id=self.sid,
            order_quantity=1,
            options_deployment_config=self.dep.config,
            explicit_option_type="CE",
            client_order_id="buy-exit-test-001",
        )
        assert buy_result is not None, "BUY should succeed"
        assert buy_result.status == "FILLED"
        buy_symbol = buy_result.symbol  # e.g. "NSE:NIFTY26OCT24800CE"

        # --- EXIT ---
        # The quote provider records what provider_symbol it receives on EXIT.
        # Bug: it would receive the canonical contract_id ("NSE:NIFTY|...|CE")
        # instead of the wire symbol ("NSE_OPT|NIFTY26OCT24800CE").
        self.qp.last_provider_symbol = None  # reset
        self.qp.last_instrument_key = None
        exit_result = self.controller.execute_option_order(
            decision=_sell_decision(self.strategy_id),
            spot_price=25000.0,
            deployment_id=self.dep.deployment_id,
            session_id=self.sid,
            order_quantity=1,
            options_deployment_config=self.dep.config,
            explicit_option_type="CE",
            existing_position=self._get_open_position(),
            client_order_id="sell-exit-test-001",
        )
        # The quote provider should have received the wire symbol
        assert self.qp.last_provider_symbol is not None, \
            "EXIT quote fetch was never called"
        assert "|" in self.qp.last_provider_symbol, \
            f"EXIT provider_symbol should be Upstox wire format, got {self.qp.last_provider_symbol!r}"
        # It must be the SAME wire symbol as the BUY instrument
        assert self.qp.last_provider_symbol == \
            self.repo.registry._by_contract[buy_result.options_contract_id].provider_symbol, \
            f"EXIT provider_symbol {self.qp.last_provider_symbol!r} != " \
            f"BUY wire symbol"

    def test_exit_broker_position_key_matches_buy(self):
        """Bug 2: EXIT instrument.key must match the BUY position's key so the
        short-selling guard can find the existing position."""
        buy_result = self.controller.execute_option_order(
            decision=_buy_decision(self.strategy_id),
            spot_price=25000.0, deployment_id=self.dep.deployment_id,
            session_id=self.sid, order_quantity=1,
            options_deployment_config=self.dep.config,
            explicit_option_type="CE", client_order_id="buy-key-test-001",
        )
        assert buy_result is not None
        buy_key = buy_result.symbol

        # The EXIT path reconstructs the instrument key.  Under the bug, the
        # reconstructed key uses YYYYMMDD while the BUY used YYMONDD.
        exit_result = self.controller.execute_option_order(
            decision=_sell_decision(self.strategy_id),
            spot_price=25000.0, deployment_id=self.dep.deployment_id,
            session_id=self.sid, order_quantity=1,
            options_deployment_config=self.dep.config,
            explicit_option_type="CE",
            existing_position=self._get_open_position(),
            client_order_id="sell-key-test-001",
        )
        # The EXIT instrument key must match the BUY key so the broker can
        # find the position for its short-selling guard.
        assert self.qp.last_instrument_key == buy_key, \
            f"EXIT key {self.qp.last_instrument_key!r} != BUY key {buy_key!r} — " \
            "short-selling guard will reject the exit"

    def test_full_exit_closes_position(self):
        """Full integration: BUY then EXIT must close the position with
        positive cash from the round-trip."""
        buy_result = self.controller.execute_option_order(
            decision=_buy_decision(self.strategy_id),
            spot_price=25000.0, deployment_id=self.dep.deployment_id,
            session_id=self.sid, order_quantity=1,
            options_deployment_config=self.dep.config,
            explicit_option_type="CE", client_order_id="buy-integ-001",
        )
        assert buy_result is not None and buy_result.status == "FILLED"

        # Set a higher exit premium
        class ExitQuoteProvider:
            def get_quote(self, instrument):
                return OptionQuote(
                    instrument=instrument, ltp=200.0,
                    timestamp=datetime.now(UTC), fetched_at=datetime.now(UTC),
                    bid=199.5, ask=200.5,
                    source_symbol=instrument.provider_symbol,
                )
            def is_fresh(self, quote, max_age_seconds=None):
                return True

        self.controller.set_quote_provider(ExitQuoteProvider())
        runner = self.center.get_runner(self.sid)
        position = runner.broker.get_position(buy_result.symbol)
        assert position is not None and position.is_open

        exit_result = self.controller.execute_option_order(
            decision=_sell_decision(self.strategy_id),
            spot_price=25000.0, deployment_id=self.dep.deployment_id,
            session_id=self.sid, order_quantity=1,
            options_deployment_config=self.dep.config,
            explicit_option_type="CE",
            existing_position=position,
            client_order_id="sell-integ-001",
        )
        assert exit_result is not None, f"EXIT failed: {exit_result}"
        assert exit_result.status == "FILLED", f"EXIT not filled: {exit_result.status}"

        # Position should be flat
        runner = self.center.get_runner(self.sid)
        position = runner.broker.get_position(buy_result.symbol)
        assert position.qty == 0
        assert position.is_open is False

    def _get_open_position(self):
        runner = self.center.get_runner(self.sid)
        positions = runner.broker.positions()
        for pos in positions.values():
            if pos.is_option and pos.is_open and pos.option_type == "CE":
                return pos
        return None


class TestExplicitSideOverride:
    """Bug fix: explicit_side parameter now controls the OrderIntent side."""

    def setup_method(self):
        self.center, self.spec, self.strategy_id = _build_center()
        self.controller = _build_controller(self.center, "explicit-side-bot")
        self.dep = _deploy(
            self.controller, self.center, "explicit-side-bot",
            self.strategy_id, self.spec,
        )
        self.sid = self.center.find_session_for_deployment(self.dep.deployment_id)
        self.repo = _build_repo()
        self.qp = ValidatingQuoteProvider(self.repo)
        self.controller.set_option_discoverer(
            CurrentOptionDiscoverer(repository=self.repo), repository=self.repo,
        )
        self.controller.set_quote_provider(self.qp)

    def _buy_one(self, cid):
        from trading_system.strategy_factory.contract import StrategySignal
        sig = StrategySignal(
            action=SignalAction.BUY, strategy_id=self.strategy_id,
            timestamp=datetime.now(UTC), symbol="NSE:NIFTY",
            reference_price=25000.0, confidence=0.9,
            reason="buy", option_intent="CE",
        )
        decision = SimpleNamespace(
            decision_id=f"buy-{cid}",
            opportunity_symbol="NSE:NIFTY",
            selected_configuration=SimpleNamespace(
                strategy_id=self.strategy_id, timeframe="1d",
            ),
            signal=sig, action="buy",
        )
        result = self.controller.execute_option_order(
            decision=decision,
            spot_price=25000.0,
            deployment_id=self.dep.deployment_id,
            session_id=self.sid,
            order_quantity=1,
            options_deployment_config=self.dep.config,
            explicit_option_type="CE",
            client_order_id=f"buy-{cid}",
        )
        assert result is not None and result.status == "FILLED"
        return result

    def test_explicit_side_overrides_decision_action(self):
        """With existing_position (EXIT path), explicit_side='sell' must
        produce a SELL order even when decision.action='buy'."""
        buy_result = self._buy_one("override")
        runner = self.center.get_runner(self.sid)
        pos = runner.broker.get_position(buy_result.symbol)
        assert pos is not None and pos.qty == 1

        # EXIT path but with decision.action="buy" (simulating a caller that
        # sets action differently from the intended side).
        class FakeExitQP:
            def get_quote(self, instrument):
                return OptionQuote(
                    instrument=instrument, ltp=200.0,
                    timestamp=datetime.now(UTC), fetched_at=datetime.now(UTC),
                    source_symbol=instrument.provider_symbol,
                )
            def is_fresh(self, quote, max_age_seconds=None):
                return True
        self.controller.set_quote_provider(FakeExitQP())

        decision = SimpleNamespace(
            decision_id="exit-override",
            opportunity_symbol="NSE:NIFTY",
            selected_configuration=SimpleNamespace(
                strategy_id=self.strategy_id, timeframe="1d",
            ),
            action="buy",  # would normally make side=BUY
            signal=SimpleNamespace(
                action="buy", reference_price=25000.0, option_intent="CE",
            ),
        )
        result = self.controller.execute_option_order(
            decision=decision,
            spot_price=25000.0,
            deployment_id=self.dep.deployment_id,
            session_id=self.sid,
            order_quantity=1,
            options_deployment_config=self.dep.config,
            explicit_option_type="CE",
            explicit_side="sell",  # override → should be SELL
            existing_position=pos,
            client_order_id="exit-override-001",
        )
        assert result is not None, "EXIT with explicit_side should succeed"
        assert result.side == "SELL", \
            f"expected SELL from explicit_side, got {result.side!r}"
        # Position should be flat (closed), not increased to 2
        runner = self.center.get_runner(self.sid)
        flat_pos = runner.broker.get_position(buy_result.symbol)
        assert flat_pos.qty == 0


class TestExitLIFOMatching:
    """Exit matching: when multiple CE positions are open, close the most
    recently opened (LIFO) rather than the oldest."""

    def setup_method(self):
        self.center, self.spec, self.strategy_id = _build_center()
        self.controller = _build_controller(self.center, "lifo-bot")
        self.dep = _deploy(
            self.controller, self.center, "lifo-bot",
            self.strategy_id, self.spec,
        )
        self.sid = self.center.find_session_for_deployment(self.dep.deployment_id)
        self.repo = _build_repo()
        self.qp = ValidatingQuoteProvider(self.repo)
        self.controller.set_option_discoverer(
            CurrentOptionDiscoverer(repository=self.repo), repository=self.repo,
        )
        self.controller.set_quote_provider(self.qp)

    def _buy_at(self, spot, cid):
        from trading_system.strategy_factory.contract import StrategySignal
        sig = StrategySignal(
            action=SignalAction.BUY, strategy_id=self.strategy_id,
            timestamp=datetime.now(UTC), symbol="NSE:NIFTY",
            reference_price=spot, confidence=0.9,
            reason="buy", option_intent="CE",
        )
        decision = SimpleNamespace(
            decision_id=f"buy-{cid}",
            opportunity_symbol="NSE:NIFTY",
            selected_configuration=SimpleNamespace(
                strategy_id=self.strategy_id, timeframe="1d",
            ),
            signal=sig, action="buy",
        )
        result = self.controller.execute_option_order(
            decision=decision,
            spot_price=spot,
            deployment_id=self.dep.deployment_id,
            session_id=self.sid,
            order_quantity=1,
            options_deployment_config=self.dep.config,
            explicit_option_type="CE",
            client_order_id=f"buy-{cid}",
        )
        assert result is not None and result.status == "FILLED"
        return result

    def test_exit_closes_most_recent_position(self):
        """Open two CE positions (different strikes), then SELL one.
        The EXIT must close the most recently opened position (LIFO)."""
        # First BUY — ATM near 25000 → strike 25000
        r1 = self._buy_at(25000.0, "old")
        # Second BUY — ATM near 24800 → strike 24800
        r2 = self._buy_at(24800.0, "new")

        # Collect open positions before EXIT
        runner = self.center.get_runner(self.sid)
        before = {
            p.symbol: p for p in runner.broker.positions().values()
            if p.is_option and p.is_open
        }
        assert len(before) == 2, f"expected 2 open positions, got {len(before)}"

        from backend.autonomous_scheduler import _execute_one_option_decision
        from tests.test_autonomous_scheduler_hardening import _FakeSpecLookup
        from tests.test_f2_option_lifecycle import _build_spec_for_test

        spec = _build_spec_for_test()
        exit_decision = SimpleNamespace(
            decision_id="lifo-exit",
            opportunity_symbol="NSE:NIFTY",
            selected_configuration=SimpleNamespace(
                strategy_id=self.strategy_id, timeframe="1d",
            ),
            action="sell",
            signal=SimpleNamespace(
                action="sell", reference_price=25000.0, option_intent="CE",
            ),
        )
        with _FakeSpecLookup(spec):
            result = _execute_one_option_decision(
                self.controller, exit_decision,
                spot_price=25000.0, target_qty=1,
            )
        assert result["result"] == "submitted", \
            f"EXIT failed: {result}"

        # After EXIT: the most recent position (r2 symbol) should be closed;
        # the older one (r1 symbol) should remain open.
        runner = self.center.get_runner(self.sid)
        old_pos = runner.broker.get_position(r1.symbol)
        new_pos = runner.broker.get_position(r2.symbol)
        assert old_pos is not None and old_pos.qty == 1, \
            "older position should remain open"
        assert new_pos is not None and new_pos.qty == 0, \
            f"most recent position should be closed (LIFO), got qty={new_pos.qty}"
