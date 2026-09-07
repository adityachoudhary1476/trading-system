"""Phase 8 — Tests for options contract selection and integration.

Covers:
  - OptionsContractSelection model (validation, from_instrument)
  - OptionsContractSelector (ATM/ITM/OTM, expiry policies)
  - OptionsContractSelection on StrategySignal
  - OptionsContractSelection on TradingDecision
  - PaperDeploymentConfig options fields
  - Order/OrderIntent/OrderResult options fields
  - Position options metadata propagation
  - SafetyValidator.check_contract_validity
  - IdempotencyGuard deployment_key with options
  - PaperBroker submit_order with options
"""
import pytest

from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock

from trading_system.autonomous.options.model import (
    OptionsContractSelection,
    OptionDirection,
    StrikeSelectionPolicy,
    ExpirySelectionPolicy,
)
from trading_system.autonomous.options.selector import (
    OptionsContractSelector,
)
from trading_system.autonomous.safety import (
    IdempotencyGuard,
    Phase7Config,
    SafetyValidator,
)
from trading_system.execution.orders import (
    Order, OrderIntent, OrderResult, OrderStatus, OrderType, Side,
)
from trading_system.paper_trading import Position
from trading_system.paper.deployment import (
    PaperDeploymentConfig,
    PaperDeploymentStatus,
    PaperDeployment,
)


# --------------------------------------------------------------------------- #
# OptionsContractSelection model tests
# --------------------------------------------------------------------------- #

class TestOptionsContractSelectionModel:

    def test_valid_call_selection(self):
        sel = OptionsContractSelection(
            underlying="NIFTY",
            option_type="CE",
            strike=21000.0,
            expiry="2025-12-25",
            instrument_type="option_ce",
            exchange="NFO",
        )
        assert sel.option_type == "CE"
        assert sel.instrument_type == "option_ce"
        assert sel.strike == 21000.0
        assert sel.contract_id is not None

    def test_valid_put_selection(self):
        sel = OptionsContractSelection(
            underlying="NIFTY",
            option_type="PE",
            strike=21000.0,
            expiry="2025-12-25",
            instrument_type="option_pe",
            exchange="NFO",
        )
        assert sel.option_type == "PE"
        assert sel.instrument_type == "option_pe"

    def test_invalid_option_type(self):
        with pytest.raises(Exception):
            OptionsContractSelection(
                underlying="NIFTY",
                option_type="XX",
                strike=21000.0,
                expiry="2025-12-25",
                instrument_type="option_ce",
                exchange="NFO",
            )

    def test_cross_check_mismatch(self):
        with pytest.raises(Exception):
            OptionsContractSelection(
                underlying="NIFTY",
                option_type="CE",
                strike=21000.0,
                expiry="2025-12-25",
                instrument_type="option_pe",
                exchange="NFO",
            )

    def test_to_dict(self):
        sel = OptionsContractSelection(
            underlying="NIFTY",
            option_type="CE",
            strike=21000.0,
            expiry="2025-12-25",
            instrument_type="option_ce",
            exchange="NFO",
            instrument_id="NFO:NIFTY|2025-12-25|21000|CE",
            symbol="NFO:NIFTY25DEC21000CE",
        )
        d = sel.to_dict()
        assert d["option_type"] == "CE"
        assert d["instrument_id"] == "NFO:NIFTY|2025-12-25|21000|CE"
        assert d["symbol"] == "NFO:NIFTY25DEC21000CE"

    def test_option_direction(self):
        assert OptionDirection.CALL.option_type == "CE"
        assert OptionDirection.PUT.option_type == "PE"


# --------------------------------------------------------------------------- #
# OptionsContractSelector tests
# --------------------------------------------------------------------------- #

class TestOptionsContractSelector:

    def _make_mock_repo(self, expiries, strikes_by_type):
        """Create a mock InstrumentRepository."""
        repo = Mock()
        repo.get_expiries = Mock(return_value=expiries)
        repo.list_options = Mock(side_effect=lambda underlying, expiry, option_type:
            strikes_by_type.get(option_type, []))
        repo.find_contract = Mock(return_value=None)
        return repo

    def test_select_returns_none_when_no_expiries(self):
        repo = Mock()
        repo.get_expiries = Mock(return_value=[])
        selector = OptionsContractSelector(repository=repo, spot_price_provider=lambda u: 21000.0)
        sel = selector.select(
            underlying="NIFTY",
            direction=OptionDirection.CALL,
            spot_price=21000.0,
        )
        assert sel is None

    def test_select_returns_none_when_no_spot(self):
        repo = Mock()
        repo.get_expiries = Mock(return_value=["2025-12-25"])
        selector = OptionsContractSelector(repository=repo)
        sel = selector.select(
            underlying="NIFTY",
            direction=OptionDirection.CALL,
        )
        assert sel is None

    def test_select_finds_atm_strike(self):
        from trading_system.india.instruments import Instrument, InstrumentType, InternalSymbol

        exp = (date.today() + timedelta(days=30)).isoformat()
        instruments = []
        for strike in [20800, 20900, 21000, 21100, 21200]:
            instr = Instrument(
                internal=InternalSymbol(exchange="NFO", symbol=f"NIFTY{exp[:4]}{strike}CE"),
                instrument_type=InstrumentType.OPTION_CE,
                underlying="NIFTY",
                expiry=exp,
                strike=float(strike),
                option_type="CE",
            )
            instruments.append(instr)

        repo = Mock()
        repo.get_expiries = Mock(return_value=[exp])
        repo.list_options = Mock(return_value=instruments)
        repo.find_contract = Mock(return_value=instruments[2])  # ATM = 21000

        selector = OptionsContractSelector(
            repository=repo,
            spot_price_provider=lambda u: 21000.0,
        )
        sel = selector.select(
            underlying="NIFTY",
            direction=OptionDirection.CALL,
        )
        assert sel is not None
        assert sel.option_type == "CE"
        assert sel.strike == 21000.0
        assert sel.instrument_id is not None

    def test_can_select_returns_false_when_empty_repo(self):
        repo = Mock()
        repo.get_expiries = Mock(return_value=[])
        selector = OptionsContractSelector(repository=repo, spot_price_provider=lambda u: 21000.0)
        assert selector.can_select(
            underlying="NIFTY",
            direction=OptionDirection.CALL,
            spot_price=21000.0,
        ) is False


# --------------------------------------------------------------------------- #
# StrategySignal integration tests
# --------------------------------------------------------------------------- #

class TestStrategySignalOptions:

    def test_signal_with_options_selection(self):
        from trading_system.strategy_factory.contract import SignalAction, StrategySignal
        sel = OptionsContractSelection(
            underlying="NIFTY",
            option_type="CE",
            strike=21000.0,
            expiry="2025-12-25",
            instrument_type="option_ce",
            exchange="NFO",
        )
        signal = StrategySignal(
            strategy_id="test-strategy",
            action=SignalAction.BUY,
            symbol="NFO:NIFTY25DEC21000CE",
            reference_price=150.0,
            confidence=0.85,
            reason="test reason",
            target_position=1,
            options_selection=sel,
            timestamp=datetime(2025, 9, 1, 9, 15, 0, tzinfo=timezone.utc),
        )
        d = signal.to_dict()
        assert d["options_selection"] is not None
        assert d["options_selection"]["option_type"] == "CE"
        assert d["options_selection"]["strike"] == 21000.0

    def test_signal_without_options_selection(self):
        from trading_system.strategy_factory.contract import SignalAction, StrategySignal
        signal = StrategySignal(
            strategy_id="test-strategy",
            action=SignalAction.BUY,
            symbol="NSE:NIFTY",
            reference_price=21000.0,
            confidence=0.85,
            reason="test reason",
            target_position=1,
            timestamp=datetime(2025, 9, 1, 9, 15, 0, tzinfo=timezone.utc),
        )
        d = signal.to_dict()
        assert d.get("options_selection") is None


# --------------------------------------------------------------------------- #
# TradingDecision integration tests
# --------------------------------------------------------------------------- #

class TestTradingDecisionOptions:

    def test_decision_with_options_contract(self):
        from trading_system.autonomous.decision import TradingDecision
        sel = OptionsContractSelection(
            underlying="NIFTY",
            option_type="PE",
            strike=20500.0,
            expiry="2025-12-25",
            instrument_type="option_pe",
            exchange="NFO",
        )
        d = TradingDecision(
            decision_id="dec-001",
            opportunity_symbol="NIFTY",
            opportunity_rank=1,
            market_timestamp="2025-09-01T09:15:00+00:00",
            snapshot_identity="snap-001",
            selected_configuration=None,
            selection_factors=[],
            signal=None,
            evaluation_error=None,
            exclusion_reason=None,
            exclusion_detail="",
            is_valid=True,
            status="valid",
            decision_timestamp="2025-09-01T09:15:00+00:00",
            config_snapshot={},
            options_contract=sel,
        )
        d_dict = d.to_dict()
        assert d_dict["options_contract"] is not None
        assert d_dict["options_contract"]["option_type"] == "PE"
        assert d_dict["options_contract"]["strike"] == 20500.0


# --------------------------------------------------------------------------- #
# PaperDeploymentConfig options tests
# --------------------------------------------------------------------------- #

class TestPaperDeploymentConfigOptions:

    def test_default_options_disabled(self):
        config = PaperDeploymentConfig()
        assert config.options_enabled is False
        assert config.allowed_option_types == ["CE", "PE"]

    def test_options_enabled_config(self):
        config = PaperDeploymentConfig(
            options_enabled=True,
            allowed_option_types=["CE"],
        )
        assert config.options_enabled is True
        assert config.allowed_option_types == ["CE"]

    def test_max_contracts_per_trade(self):
        config = PaperDeploymentConfig(
            options_enabled=True,
            max_options_contracts_per_trade=5,
        )
        assert config.max_options_contracts_per_trade == 5

    def test_invalid_option_type_rejected(self):
        with pytest.raises(Exception):
            PaperDeploymentConfig(
                options_enabled=True,
                allowed_option_types=["XX"],
            )

    def test_deployment_options_properties(self):
        config = PaperDeploymentConfig(
            options_enabled=True,
        )
        dep = PaperDeployment(
            deployment_id="dep-001",
            strategy_id="strat-001",
            strategy_spec_hash="hash123",
            symbol="NSE:NIFTY",
            timeframe="1d",
            dataset_id="ds-001",
            config=config,
            status=PaperDeploymentStatus.ACTIVE,
            created_at="2025-09-01T09:00:00+00:00",
            updated_at="2025-09-01T09:00:00+00:00",
        )
        assert dep.config.options_enabled is True
        assert dep.options_enabled is True
        assert dep.allowed_option_types == ["CE", "PE"]


# --------------------------------------------------------------------------- #
# Order / OrderIntent / OrderResult options tests
# --------------------------------------------------------------------------- #

class TestOrderOptionsFields:

    def test_order_with_options_fields(self):
        order = Order(
            symbol="NFO:NIFTY25DEC21000CE",
            side=Side.BUY,
            quantity=1,
            order_type=OrderType.MARKET,
            options_contract_id="NFO:NIFTY|2025-12-25|21000|CE",
            strike=21000.0,
            expiry="2025-12-25",
            option_type="CE",
        )
        assert order.options_contract_id == "NFO:NIFTY|2025-12-25|21000|CE"
        assert order.strike == 21000.0
        assert order.expiry == "2025-12-25"
        assert order.option_type == "CE"

    def test_order_without_options_fields(self):
        order = Order(
            symbol="NSE:NIFTY",
            side=Side.BUY,
            quantity=10,
            order_type=OrderType.MARKET,
        )
        assert order.options_contract_id is None
        assert order.strike is None

    def test_order_intent_with_options(self):
        intent = OrderIntent(
            symbol="NFO:NIFTY25DEC21000CE",
            side=Side.BUY,
            quantity=1,
            order_type=OrderType.LIMIT,
            limit_price=150.0,
            current_price=152.0,
            options_contract_id="NFO:NIFTY|2025-12-25|21000|CE",
            strike=21000.0,
            expiry="2025-12-25",
            option_type="CE",
        )
        assert intent.options_contract_id == "NFO:NIFTY|2025-12-25|21000|CE"
        assert intent.strike == 21000.0

    def test_order_result_to_dict_includes_options(self):
        result = OrderResult(
            order_id="ord-001",
            client_order_id=None,
            symbol="NFO:NIFTY25DEC21000CE",
            side=Side.BUY,
            quantity=1,
            order_type=OrderType.MARKET,
            limit_price=None,
            status=OrderStatus.FILLED,
            filled_quantity=1.0,
            avg_fill_price=152.0,
            fills=[],
            cash_after=98000.0,
            equity_after=100000.0,
            realized_pnl_after=0.0,
            unrealized_pnl_after=500.0,
            position_qty_after=1.0,
            options_contract_id="NFO:NIFTY|2025-12-25|21000|CE",
            strike=21000.0,
            expiry="2025-12-25",
            option_type="CE",
        )
        d = result.to_dict()
        assert d["options_contract_id"] == "NFO:NIFTY|2025-12-25|21000|CE"
        assert d["strike"] == 21000.0
        assert d["option_type"] == "CE"


# --------------------------------------------------------------------------- #
# Position options tests
# --------------------------------------------------------------------------- #

class TestPositionOptionsFields:

    def test_position_with_options_metadata(self):
        pos = Position(
            symbol="NFO:NIFTY25DEC21000CE",
            qty=1,
            avg_entry_price=150.0,
            realized_pnl=0.0,
            current_price=155.0,
            options_contract_id="NFO:NIFTY|2025-12-25|21000|CE",
            strike=21000.0,
            expiry="2025-12-25",
            option_type="CE",
            contract_size=1,
        )
        assert pos.options_contract_id == "NFO:NIFTY|2025-12-25|21000|CE"
        d = pos.as_dict()
        assert d["options_contract_id"] == "NFO:NIFTY|2025-12-25|21000|CE"
        assert d["strike"] == 21000.0

    def test_position_without_options_metadata(self):
        pos = Position(
            symbol="NSE:NIFTY",
            qty=10,
            avg_entry_price=21000.0,
            realized_pnl=0.0,
            current_price=21100.0,
        )
        d = pos.as_dict()
        assert "options_contract_id" not in d

    def test_position_options_pnl(self):
        """Verify options position correctly tracks P&L."""
        pos = Position(
            symbol="NFO:NIFTY25DEC21000CE",
            qty=1,
            avg_entry_price=100.0,
            realized_pnl=0.0,
            current_price=150.0,
            options_contract_id="test",
            strike=21000.0,
            expiry="2025-12-25",
            option_type="CE",
            contract_size=1,
        )
        assert pos.unrealized_pnl == 50.0


# --------------------------------------------------------------------------- #
# SafetyValidator.check_contract_validity tests
# --------------------------------------------------------------------------- #

class TestSafetyValidatorContractValidity:

    def setup_method(self):
        self.validator = SafetyValidator(Phase7Config())

    def test_no_selection_passes(self):
        result = self.validator.check_contract_validity()
        assert result.passed

    def test_valid_future_contract(self):
        future = (date.today() + timedelta(days=30)).isoformat()
        sel = OptionsContractSelection(
            underlying="NIFTY",
            option_type="CE",
            strike=21000.0,
            expiry=future,
            instrument_type="option_ce",
            exchange="NFO",
        )
        result = self.validator.check_contract_validity(
            options_selection=sel,
        )
        assert result.passed, result.failed_checks

    def test_expired_contract_fails(self):
        sel = OptionsContractSelection(
            underlying="NIFTY",
            option_type="CE",
            strike=21000.0,
            expiry="2020-01-01",
            instrument_type="option_ce",
            exchange="NFO",
        )
        result = self.validator.check_contract_validity(
            options_selection=sel,
            now_date=date.today().isoformat(),
        )
        assert not result.passed
        assert any("past" in c for c in result.failed_checks)

    def test_disallowed_option_type_fails(self):
        future = (date.today() + timedelta(days=30)).isoformat()
        sel = OptionsContractSelection(
            underlying="NIFTY",
            option_type="PE",
            strike=21000.0,
            expiry=future,
            instrument_type="option_pe",
            exchange="NFO",
        )
        result = self.validator.check_contract_validity(
            options_selection=sel,
            allowed_option_types=["CE"],
        )
        assert not result.passed
        assert any("not in allowed" in c for c in result.failed_checks)

    def test_allowed_option_type_passes(self):
        future = (date.today() + timedelta(days=30)).isoformat()
        sel = OptionsContractSelection(
            underlying="NIFTY",
            option_type="CE",
            strike=21000.0,
            expiry=future,
            instrument_type="option_ce",
            exchange="NFO",
        )
        result = self.validator.check_contract_validity(
            options_selection=sel,
            allowed_option_types=["CE"],
        )
        assert result.passed


# --------------------------------------------------------------------------- #
# IdempotencyGuard options tests
# --------------------------------------------------------------------------- #

class TestIdempotencyGuardOptions:

    def test_deployment_key_without_options(self):
        guard = IdempotencyGuard()
        key = guard.deployment_key(
            bot_id="bot-001",
            symbol="NSE:NIFTY",
            strategy_id="strat-001",
            timeframe="1d",
        )
        assert len(key) > 0

    def test_deployment_key_with_options(self):
        guard = IdempotencyGuard()
        key_no_opt = guard.deployment_key(
            bot_id="bot-001",
            symbol="NSE:NIFTY",
            strategy_id="strat-001",
            timeframe="1d",
        )
        key_with_opt = guard.deployment_key(
            bot_id="bot-001",
            symbol="NSE:NIFTY",
            strategy_id="strat-001",
            timeframe="1d",
            options_contract_id="NFO:NIFTY|2025-12-25|21000|CE",
        )
        assert key_no_opt != key_with_opt


# --------------------------------------------------------------------------- #
# PaperBroker submit_order with options
# --------------------------------------------------------------------------- #

class TestPaperBrokerOptions:

    def test_submit_order_with_options(self):
        from trading_system.execution.paper_broker import PaperBroker
        broker = PaperBroker(initial_cash=100000.0)
        broker.update_market_price("NFO:NIFTY25DEC21000CE", 150.0)

        order = broker.submit_order(
            symbol="NFO:NIFTY25DEC21000CE",
            side=Side.BUY,
            quantity=1,
            order_type=OrderType.MARKET,
            current_price=150.0,
            options_contract_id="NFO:NIFTY|2025-12-25|21000|CE",
            strike=21000.0,
            expiry="2025-12-25",
            option_type="CE",
        )

        assert order.options_contract_id == "NFO:NIFTY|2025-12-25|21000|CE"
        assert order.strike == 21000.0
        assert order.option_type == "CE"

        pos = broker.get_position("NFO:NIFTY25DEC21000CE")
        assert pos is not None
        assert pos.options_contract_id == "NFO:NIFTY|2025-12-25|21000|CE"
        assert pos.strike == 21000.0
        assert pos.expiry == "2025-12-25"
        assert pos.option_type == "CE"

    def test_submit_order_without_options(self):
        from trading_system.execution.paper_broker import PaperBroker
        broker = PaperBroker(initial_cash=100000.0)
        broker.update_market_price("NSE:NIFTY", 21000.0)

        order = broker.submit_order(
            symbol="NSE:NIFTY",
            side=Side.BUY,
            quantity=10,
            order_type=OrderType.MARKET,
            current_price=21000.0,
        )

        assert order.options_contract_id is None
        assert order.strike is None
