"""F2 - End-to-end option lifecycle: entry -> exit -> realized P&L.

Deterministic, paper-only validation using real EMACrossoverStrategy through
the complete autonomous option execution path.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
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
        def __init__(self, ce_premium, pe_premium):
            self.ce_premium = ce_premium
            self.pe_premium = pe_premium

        def get_quote(self, instrument):
            premium = self.ce_premium if instrument.option_type == "CE" else self.pe_premium
            return FakeQuote(instrument, premium)

        def is_fresh(self, quote, max_age_seconds=None):
            return True

    discoverer = CurrentOptionDiscoverer(repository=repo)
    controller.set_option_discoverer(discoverer, repository=repo)
    controller.set_quote_provider(FakeQuoteProvider(ce_premium, pe_premium))


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
        name="F2 Lifecycle Spec",
        description="F2 lifecycle validation spec",
        symbol="NSE:NIFTY",
        timeframe="1d",
        indicators=[{"name": "sma", "params": {"window": 5}}],
        entry=make_condition(field_operand("close"), ">", indicator_operand("sma_5")),
        generated_by="phase-f2",
    )


def _build_bullish_df():
    """Build OHLCV that produces a BUY signal from EMA(5,13) around spot=25000."""
    idx = pd.date_range("2024-01-01", periods=60, freq="1D", tz="UTC")
    close = np.linspace(24000, 26000, 60)
    df = pd.DataFrame(
        {"open": close - 1, "high": close + 2, "low": close - 2, "close": close, "volume": 1000.0},
        index=idx,
    )
    return df


def _build_bearish_df():
    """Build OHLCV that produces a SELL signal from EMA(5,13) around spot=25000."""
    idx = pd.date_range("2024-01-01", periods=60, freq="1D", tz="UTC")
    close = np.linspace(26000, 24000, 60)
    df = pd.DataFrame(
        {"open": close - 1, "high": close + 2, "low": close - 2, "close": close, "volume": 1000.0},
        index=idx,
    )
    return df


def _make_market_state(df: pd.DataFrame, symbol="NSE:NIFTY") -> MarketState:
    return MarketState(
        symbol=symbol,
        timeframe="1d",
        timestamp=df.index[-1],
        bars=df,
        position=None,
    )


class TestF2OptionLifecycle:
    """End-to-end option lifecycle: entry -> exit -> realized P&L."""

    def test_ce_lifecycle_entry_then_exit(self):
        """CE entry at 185, exit at 200 -> positive realized P&L."""
        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_scheduler_bot(center, bot_id="f2-ce-lifecycle")

        _attach_option_infra(controller, ce_premium=185.0, pe_premium=210.0)

        dep = _create_options_deployment(
            controller, bot_id="f2-ce-lifecycle", spec=spec, strategy_id=strategy_id,
            options_enabled=True, allowed_option_types=["CE", "PE"], max_contracts=1,
        )

        df_entry = _build_bullish_df()
        state_entry = _make_market_state(df_entry)
        strategy = EMACrossoverStrategy(fast_period=5, slow_period=13, options_mode=True)
        signal_entry = strategy.evaluate(state_entry)

        assert signal_entry.action == SignalAction.BUY
        assert signal_entry.option_intent == "CE"

        entry_decision = SimpleNamespace(
            decision_id="f2-ce-entry",
            opportunity_symbol="NSE:NIFTY",
            selected_configuration=SimpleNamespace(
                strategy_id=strategy_id,
                timeframe="1d",
            ),
            signal=signal_entry,
            action="buy",
        )

        from backend.autonomous_scheduler import _execute_one_option_decision
        from tests.test_autonomous_scheduler_hardening import _FakeSpecLookup

        with _FakeSpecLookup(spec):
            entry_result = _execute_one_option_decision(
                controller, entry_decision, spot_price=float(df_entry["close"].iloc[-1]), target_qty=1
            )
        assert entry_result["result"] == "submitted"
        assert entry_result["option_intent"] == "CE"
        assert entry_result["avg_fill_price"] == pytest.approx(185.0, abs=0.2)

        class ExitQuoteProvider:
            def __init__(self):
                self.ce_premium = 200.0
                self.pe_premium = 210.0

            def get_quote(self, instrument):
                from types import SimpleNamespace
                return SimpleNamespace(
                    instrument=instrument,
                    ltp=self.ce_premium if instrument.option_type == "CE" else self.pe_premium,
                    timestamp=datetime.now(UTC),
                    fetched_at=datetime.now(UTC),
                    age_seconds=0.0,
                )

            def is_fresh(self, quote, max_age_seconds=None):
                return True

        controller.set_quote_provider(ExitQuoteProvider())

        exit_decision = SimpleNamespace(
            decision_id="f2-ce-exit",
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
            action="sell",
        )

        with _FakeSpecLookup(spec):
            exit_result = _execute_one_option_decision(
                controller, exit_decision, spot_price=25000.0, target_qty=1
            )
        assert exit_result["result"] == "submitted"
        assert exit_result["option_intent"] == "CE"
        assert exit_result["avg_fill_price"] == pytest.approx(200.0, abs=0.2)

        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)
        positions = runner.broker.positions()
        option_position = next((p for p in positions.values() if p.is_option), None)
        assert option_position is not None
        assert option_position.qty == 0
        assert option_position.realized_pnl > 0  # (200 - 185.0925) * 1 * 50 - fees approx positive

    def test_pe_lifecycle_entry_then_exit(self):
        """PE entry at 210, exit at 190 -> negative realized P&L."""
        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_scheduler_bot(center, bot_id="f2-pe-lifecycle")
        _attach_option_infra(controller, ce_premium=185.0, pe_premium=210.0)

        dep = _create_options_deployment(
            controller, bot_id="f2-pe-lifecycle", spec=spec, strategy_id=strategy_id,
            options_enabled=True, allowed_option_types=["CE", "PE"], max_contracts=1,
        )

        df_entry = _build_bearish_df()
        state_entry = _make_market_state(df_entry)
        strategy = EMACrossoverStrategy(fast_period=5, slow_period=13, allow_short=True, options_mode=True)
        signal_entry = strategy.evaluate(state_entry)

        assert signal_entry.action == SignalAction.BUY
        assert signal_entry.option_intent == "PE"

        entry_decision = SimpleNamespace(
            decision_id="f2-pe-entry",
            opportunity_symbol="NSE:NIFTY",
            selected_configuration=SimpleNamespace(
                strategy_id=strategy_id,
                timeframe="1d",
            ),
            signal=signal_entry,
            action="buy",
        )

        from backend.autonomous_scheduler import _execute_one_option_decision
        from tests.test_autonomous_scheduler_hardening import _FakeSpecLookup

        with _FakeSpecLookup(spec):
            entry_result = _execute_one_option_decision(
                controller, entry_decision, spot_price=float(df_entry["close"].iloc[-1]), target_qty=1
            )
        assert entry_result["result"] == "submitted"
        assert entry_result["option_intent"] == "PE"
        assert entry_result["avg_fill_price"] == pytest.approx(210.0, abs=0.2)

        class ExitQuoteProvider:
            def __init__(self):
                self.ce_premium = 185.0
                self.pe_premium = 190.0

            def get_quote(self, instrument):
                from types import SimpleNamespace
                return SimpleNamespace(
                    instrument=instrument,
                    ltp=self.ce_premium if instrument.option_type == "CE" else self.pe_premium,
                    timestamp=datetime.now(UTC),
                    fetched_at=datetime.now(UTC),
                    age_seconds=0.0,
                )

            def is_fresh(self, quote, max_age_seconds=None):
                return True

        controller.set_quote_provider(ExitQuoteProvider())

        exit_decision = SimpleNamespace(
            decision_id="f2-pe-exit",
            opportunity_symbol="NSE:NIFTY",
            selected_configuration=SimpleNamespace(
                strategy_id=strategy_id,
                timeframe="1d",
            ),
            signal=SimpleNamespace(
                action=SignalAction.SELL,
                reference_price=25000.0,
                option_intent="PE",
            ),
            action="sell",
        )

        with _FakeSpecLookup(spec):
            exit_result = _execute_one_option_decision(
                controller, exit_decision, spot_price=25000.0, target_qty=1
            )
        assert exit_result["result"] == "submitted"
        assert exit_result["option_intent"] == "PE"
        assert exit_result["avg_fill_price"] == pytest.approx(190.0, abs=0.2)

        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)
        positions = runner.broker.positions()
        option_position = next((p for p in positions.values() if p.is_option), None)
        assert option_position is not None
        assert option_position.qty == 0
        assert option_position.realized_pnl < 0  # (190 - 210) * 1 * 50 - fees approx negative
