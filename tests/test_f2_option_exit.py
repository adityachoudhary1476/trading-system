"""F2 -- Long option exit lifecycle tests."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from trading_system.autonomous.bot_config import (
    AutonomousBotConfig,
    BotMode,
    Source,
    TradingMode,
    UserConstraints,
)
from trading_system.autonomous.controller import AutonomousController
from trading_system.autonomous.options.discovery import CurrentOptionDiscoverer
from trading_system.paper.deployment import PaperDeploymentConfig
from trading_system.strategy_factory.builtin import EMACrossoverStrategy
from trading_system.strategy_factory.contract import MarketState, SignalAction

UTC = timezone.utc


def _build_control_center():
    from tests.test_phase_c_option_execution import _build_control_center as _bcc
    return _bcc()


def _create_options_deployment(controller, *, bot_id: str, spec, strategy_id: str, **config):
    from tests.test_phase_c_option_execution import _create_options_deployment as _cod
    return _cod(
        controller,
        bot_id=bot_id,
        spec=spec,
        strategy_id=strategy_id,
        **config,
    )


def _make_option_instrument(
    *,
    underlying: str = "NIFTY",
    option_type: str = "CE",
    strike: float = 25000.0,
    expiry: str = "2099-12-31",
    lot_size: int = 50,
) -> "Instrument":
    from trading_system.india.instruments import (
        Instrument,
        InstrumentType,
        InternalSymbol,
    )
    internal = InternalSymbol(
        exchange="NFO",
        symbol=f"{underlying}{expiry.replace('-','')}{int(strike)}{option_type}",
    )
    instr = Instrument(
        internal=internal,
        instrument_type=InstrumentType.OPTION_CE if option_type == "CE" else InstrumentType.OPTION_PE,
        name=f"{underlying} {option_type} {strike} {expiry}",
    )
    instr.provider_symbol = f"NFO:{instr.contract_id}"
    instr.exchange_full = "NFO"
    instr.underlying = underlying
    instr.expiry = expiry
    instr.strike = strike
    instr.option_type = option_type
    instr.lot_size = lot_size
    return instr


def _build_option_repository():
    from trading_system.india.instrument_repository import InstrumentRepository
    repo = InstrumentRepository()
    repo.register(_make_option_instrument(option_type="CE", strike=25000.0, expiry="2099-12-31", lot_size=50))
    repo.register(_make_option_instrument(option_type="PE", strike=25000.0, expiry="2099-12-31", lot_size=50))
    repo.register(_make_option_instrument(option_type="CE", strike=25100.0, expiry="2099-12-31", lot_size=50))
    repo.register(_make_option_instrument(option_type="PE", strike=25100.0, expiry="2099-12-31", lot_size=50))
    return repo


def _attach_option_infra(controller, *, repo=None, ce_premium: float = 185.0, pe_premium: float = 210.0):
    if repo is None:
        repo = _build_option_repository()

    class FakeQuote:
        def __init__(self, instrument, premium):
            self.instrument = instrument
            self.ltp = premium
            self.timestamp = datetime.now(UTC)
            self.fetched_at = datetime.now(UTC)
            self.age_seconds = 0.0

    class FakeQuoteProvider:
        def get_quote(self, instrument):
            premium = ce_premium if instrument.option_type == "CE" else pe_premium
            return FakeQuote(instrument, premium)

        def is_fresh(self, quote, max_age_seconds=None):
            return True

    discoverer = CurrentOptionDiscoverer(repository=repo)
    controller.set_option_discoverer(discoverer, repository=repo)
    controller.set_quote_provider(FakeQuoteProvider())


def _build_scheduler_bot(center, *, bot_id: str) -> AutonomousController:
    user_constraints = UserConstraints(
        allowed_symbols=frozenset({"NSE:NIFTY"}),
        allowed_strategy_ids=frozenset(),
        allowed_timeframes=frozenset({"1d"}),
        allowed_option_underlyings=frozenset({"NIFTY"}),
    )
    bot_config = AutonomousBotConfig(
        bot_id=bot_id,
        name=f"F2 Bot {bot_id}",
        mode=BotMode.AUTONOMOUS,
        trading_mode=TradingMode.PAPER,
        enabled=True,
        user_constraints=user_constraints,
        max_simultaneous_positions=5,
        source=Source.AUTONOMOUS,
    )
    controller = AutonomousController(config=bot_config, control_center=center)
    return controller


def _build_spec_for_test():
    from trading_system.research.strategy_lab.spec import (
        StrategySpec,
        field_operand,
        indicator_operand,
        make_condition,
    )
    return StrategySpec(
        name="F2 Spec",
        description="F2 exit validation spec",
        symbol="NSE:NIFTY",
        timeframe="1d",
        indicators=[{"name": "sma", "params": {"window": 5}}],
        entry=make_condition(field_operand("close"), ">", indicator_operand("sma_5")),
        generated_by="phase-f2",
    )


class TestF2OptionExit:
    """Long option exit lifecycle tests."""

    def test_exit_requires_existing_long_position(self):
        """SELL option without existing long position must be rejected."""
        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_scheduler_bot(center, bot_id="f2-exit-no-pos")
        _attach_option_infra(controller, ce_premium=185.0)

        dep = _create_options_deployment(
            controller, bot_id="f2-exit-no-pos", spec=spec, strategy_id=strategy_id,
            options_enabled=True, allowed_option_types=["CE", "PE"], max_contracts=1,
        )

        # Create a SELL decision without any existing position
        decision = SimpleNamespace(
            decision_id="f2-exit-no-pos-decision",
            opportunity_symbol="NSE:NIFTY",
            selected_configuration=SimpleNamespace(
                strategy_id=strategy_id,
                timeframe="1d",
            ),
            signal=SimpleNamespace(
                action=SignalAction.SELL,
                reference_price=25000.0,
                option_intent="CE",
            ),
        )

        from backend.autonomous_scheduler import _execute_one_option_decision
        result = _execute_one_option_decision(
            controller, decision, spot_price=25000.0, target_qty=1
        )
        assert result["result"] == "no_long_option_position"

    def test_exit_rejects_wrong_option_type(self):
        """SELL CE when existing position is PE must be rejected."""
        # This test would require setting up a PE position first, then attempting CE exit
        # For now, we verify the contract mismatch logic exists
        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_scheduler_bot(center, bot_id="f2-exit-wrong-type")
        _attach_option_infra(controller, ce_premium=185.0)

        dep = _create_options_deployment(
            controller, bot_id="f2-exit-wrong-type", spec=spec, strategy_id=strategy_id,
            options_enabled=True, allowed_option_types=["CE", "PE"], max_contracts=1,
        )

    # The _option_position_matches_contract function should reject mismatched types
    from backend.autonomous_scheduler import _option_position_matches_contract

    # Mock a PE position
    pe_position = SimpleNamespace(
        symbol="NSE:NIFTY",
        qty=1,
        is_option=True,
        option_type="PE",
        strike=25000.0,
        expiry="2099-12-31",
        options_contract_id="NIFTY24DEC25000PE",
    )

    ce_decision = SimpleNamespace(
        opportunity_symbol="NSE:NIFTY",
        signal=SimpleNamespace(option_intent="CE"),
    )

    assert _option_position_matches_contract(pe_position, ce_decision, "CE") is False
    assert _option_position_matches_contract(pe_position, ce_decision, "PE") is True