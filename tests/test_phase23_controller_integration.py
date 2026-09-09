"""Test Phase23 integration in AutonomousController."""
from unittest.mock import MagicMock, patch

from trading_system.autonomous.controller import AutonomousController
from trading_system.autonomous.bot_config import AutonomousBotConfig, BotMode, TradingMode, Source, UserConstraints
from trading_system.paper.control import PaperTradingControlCenter


def test_controller_accepts_phase23_registry():
    """Controller should accept phase23_registry parameter."""
    config = AutonomousBotConfig(
        bot_id="test-bot",
        name="Test Bot",
        mode=BotMode.AUTONOMOUS,
        trading_mode=TradingMode.PAPER,
        source=Source.AUTONOMOUS,
        user_constraints=UserConstraints(
            allowed_symbols=frozenset({"NSE:NIFTY"}),
            allowed_strategy_ids=frozenset(),
            allowed_timeframes=frozenset({"1d"}),
        ),
    )
    center = MagicMock(spec=PaperTradingControlCenter)
    phase23 = MagicMock()

    controller = AutonomousController(
        config=config,
        control_center=center,
        phase23_registry=phase23,
    )

    assert controller._phase23_registry is phase23


def test_ensure_deployments_no_registry():
    """_ensure_autonomous_deployments should be no-op without registry."""
    config = AutonomousBotConfig(
        bot_id="test-bot",
        name="Test Bot",
        mode=BotMode.AUTONOMOUS,
        trading_mode=TradingMode.PAPER,
        source=Source.AUTONOMOUS,
        user_constraints=UserConstraints(
            allowed_symbols=frozenset({"NSE:NIFTY"}),
            allowed_strategy_ids=frozenset(),
            allowed_timeframes=frozenset({"1d"}),
        ),
    )
    center = MagicMock(spec=PaperTradingControlCenter)
    controller = AutonomousController(config=config, control_center=center)

    # Should not raise
    controller._ensure_autonomous_deployments()


def test_ensure_deployments_with_registry_no_approved():
    """_ensure_autonomous_deployments should be no-op when no PAPER_APPROVED strategies."""
    config = AutonomousBotConfig(
        bot_id="test-bot",
        name="Test Bot",
        mode=BotMode.AUTONOMOUS,
        trading_mode=TradingMode.PAPER,
        source=Source.AUTONOMOUS,
        user_constraints=UserConstraints(
            allowed_symbols=frozenset({"NSE:NIFTY"}),
            allowed_strategy_ids=frozenset(),
            allowed_timeframes=frozenset({"1d"}),
        ),
    )
    center = MagicMock(spec=PaperTradingControlCenter)

    phase23 = MagicMock()
    discovery = MagicMock()
    discovery.discover.return_value = []
    with patch("trading_system.research.phase23.discovery.Phase23Discovery", return_value=discovery):
        controller = AutonomousController(
            config=config,
            control_center=center,
            phase23_registry=phase23,
        )
        controller._ensure_autonomous_deployments()

    # discover should be called, but list_deployments should NOT be called
    # because approved list is empty and we return early
    discovery.discover.assert_called_once_with(max_candidates=10)
    center.list_deployments.assert_not_called()


def test_ensure_deployments_creates_deployments():
    """_ensure_autonomous_deployments should create deployments for PAPER_APPROVED strategies."""
    config = AutonomousBotConfig(
        bot_id="test-bot",
        name="Test Bot",
        mode=BotMode.AUTONOMOUS,
        trading_mode=TradingMode.PAPER,
        source=Source.AUTONOMOUS,
        user_constraints=UserConstraints(
            allowed_symbols=frozenset({"NSE:NIFTY"}),
            allowed_strategy_ids=frozenset(),
            allowed_timeframes=frozenset({"1d"}),
        ),
    )
    center = MagicMock(spec=PaperTradingControlCenter)
    center.list_deployments.return_value = []

    phase23 = MagicMock()
    discovery = MagicMock()

    # Mock PAPER_APPROVED strategy
    approved_item = MagicMock()
    approved_item.candidate_id = "test-candidate"
    approved_item.strategy_id = "test-strategy"
    approved_item.symbol = "NSE:NIFTY"
    approved_item.timeframe = "1d"
    discovery.discover.return_value = [approved_item]

    # Mock candidate with spec_builder
    candidate = MagicMock()
    candidate.spec_builder.return_value = {
        "name": "Test Strategy",
        "symbol": "NSE:NIFTY",
        "timeframe": "1d",
    }

    with patch("trading_system.research.phase23.discovery.Phase23Discovery", return_value=discovery):
        with patch("trading_system.research.phase23.strategies.get_strategy_candidate", return_value=candidate):
            with patch("trading_system.autonomous.controller.StrategySpec"):
                controller = AutonomousController(
                    config=config,
                    control_center=center,
                    phase23_registry=phase23,
                )
                controller._ensure_autonomous_deployments()

    discovery.discover.assert_called_once_with(max_candidates=10)
    candidate.spec_builder.assert_called_once_with("NSE:NIFTY", "1d")
