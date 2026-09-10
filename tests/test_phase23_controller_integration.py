"""Test Phase23 integration in AutonomousController."""
from unittest.mock import MagicMock, patch

from trading_system.autonomous.controller import AutonomousController
from trading_system.autonomous.bot_config import AutonomousBotConfig, BotMode, TradingMode, Source, UserConstraints
from trading_system.autonomous.bot_lifecycle import AutonomousBotLifecycle, AutonomousBotState
from trading_system.autonomous.coordinator import DeploymentCreationResult
from trading_system.autonomous.safety import KillSwitchReason, KillSwitchState
from trading_system.paper.control import PaperTradingControlCenter
from trading_system.paper.deployment import PaperDeploymentStatus


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

    discovery.discover.assert_called_once_with(max_candidates=50, include_experimental=True)
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

    approved_item = MagicMock()
    approved_item.candidate_id = "test-candidate"
    approved_item.strategy_id = "test-strategy"
    approved_item.symbol = "NSE:NIFTY"
    approved_item.timeframe = "1d"
    discovery.discover.return_value = [approved_item]

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

    discovery.discover.assert_called_once_with(max_candidates=50, include_experimental=True)
    candidate.spec_builder.assert_called_once_with("NSE:NIFTY", "1d")


# --------------------------------------------------------------------------- #
# Recovery and safety tests
# --------------------------------------------------------------------------- #

def test_error_state_recovery_via_start():
    """A bot in ERROR state can recover to RUNNING via start_bot()."""
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
    center.list_deployments.return_value = [
        MagicMock(notes="bot:test-bot", status=PaperDeploymentStatus.ACTIVE)
    ]

    controller = AutonomousController(config=config, control_center=center)

    controller.lifecycle = AutonomousBotLifecycle(initial_state=AutonomousBotState.ERROR)
    controller.config.state = AutonomousBotState.ERROR
    controller._safety_layer.kill_switch.halt(
        KillSwitchReason.NORMAL_STOP,
        detail="bot stopped by operator",
    )

    success, message = controller.start_bot()
    assert success is True
    assert controller.lifecycle.state == AutonomousBotState.RUNNING
    assert controller._safety_layer.kill_switch.state == KillSwitchState.ACTIVE


def test_error_state_recovery_via_stop():
    """A bot in ERROR state can recover to STOPPED via stop_bot()."""
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

    controller.lifecycle = AutonomousBotLifecycle(initial_state=AutonomousBotState.ERROR)
    controller.config.state = AutonomousBotState.ERROR

    success, message = controller.stop_bot()
    assert success is True
    assert controller.lifecycle.state == AutonomousBotState.STOPPED
    assert controller._safety_layer.kill_switch.state == KillSwitchState.HALTED
    assert controller._safety_layer.kill_switch.reason == KillSwitchReason.NORMAL_STOP


def test_genuine_safety_halt_not_silently_bypassed():
    """A bot halted for DEPLOYMENT_ERROR cannot auto-resume via start_bot()."""
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
    center.list_deployments.return_value = [
        MagicMock(notes="bot:test-bot", status=PaperDeploymentStatus.ACTIVE)
    ]

    controller = AutonomousController(config=config, control_center=center)

    controller.lifecycle = AutonomousBotLifecycle(initial_state=AutonomousBotState.ERROR)
    controller.config.state = AutonomousBotState.ERROR
    controller._safety_layer.kill_switch.halt(
        KillSwitchReason.DEPLOYMENT_ERROR,
        detail="deployment failed",
    )

    success, message = controller.start_bot()
    assert success is False
    assert "kill switch halted" in message.lower()
    assert "manual resume" in message.lower()
    assert controller.lifecycle.state == AutonomousBotState.ERROR


def test_resume_clears_genuine_safety_halt():
    """resume_bot() explicitly clears any kill switch halt and recovers from ERROR."""
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

    controller.lifecycle = AutonomousBotLifecycle(initial_state=AutonomousBotState.ERROR)
    controller.config.state = AutonomousBotState.ERROR
    controller._safety_layer.kill_switch.halt(
        KillSwitchReason.DEPLOYMENT_ERROR,
        detail="deployment failed",
    )

    success, message = controller.resume_bot()
    assert success is True
    assert controller._safety_layer.kill_switch.state == KillSwitchState.ACTIVE
    assert controller.lifecycle.state == AutonomousBotState.STOPPED


def test_bot_id_consistency_between_ui_and_scheduler():
    """UI and scheduler should use the same bot ID."""
    from backend.autonomous_scheduler import _env_bot_id, DEFAULT_BOT_ID

    assert DEFAULT_BOT_ID == "bot-nifty-options"
    assert _env_bot_id() == DEFAULT_BOT_ID


def test_start_creates_deployments_for_error_bot():
    """start_bot() creates deployments when none exist, even from ERROR state."""
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
    approved_item = MagicMock()
    approved_item.candidate_id = "test-candidate"
    approved_item.strategy_id = "test-strategy"
    approved_item.symbol = "NSE:NIFTY"
    approved_item.timeframe = "1d"
    discovery.discover.return_value = [approved_item]

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
                controller.lifecycle = AutonomousBotLifecycle(initial_state=AutonomousBotState.ERROR)
                controller.config.state = AutonomousBotState.ERROR

                success, message = controller.start_bot()

    discovery.discover.assert_called_once_with(max_candidates=50, include_experimental=True)
    candidate.spec_builder.assert_called_once_with("NSE:NIFTY", "1d")


def test_ensure_deployments_records_diagnostic_when_no_approved():
    """When no PAPER_APPROVED strategies exist, a diagnostic event is recorded."""
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

    discovery.discover.assert_called_once_with(max_candidates=50, include_experimental=True)
    # Verify diagnostic event was recorded
    events = controller._event_log.events
    assert len(events) == 1
    assert "no PAPER_APPROVED/PAPER_EXPERIMENTAL strategies found" in events[0].message


def test_ensure_deployments_records_diagnostic_on_creation_failure():
    """When deployment creation fails, the exact failure is recorded."""
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
    approved_item = MagicMock()
    approved_item.candidate_id = "test-candidate"
    approved_item.strategy_id = "test-strategy"
    approved_item.symbol = "NSE:NIFTY"
    approved_item.timeframe = "1d"
    discovery.discover.return_value = [approved_item]

    candidate = MagicMock()
    candidate.spec_builder.return_value = {
        "name": "Test Strategy",
        "symbol": "NSE:NIFTY",
        "timeframe": "1d",
    }

    with patch("trading_system.research.phase23.discovery.Phase23Discovery", return_value=discovery):
        with patch("trading_system.research.phase23.strategies.get_strategy_candidate", return_value=candidate):
            with patch("trading_system.research.strategy_lab.spec.StrategySpec") as mock_spec:
                mock_spec.model_validate.return_value = MagicMock()
                controller = AutonomousController(
                    config=config,
                    control_center=center,
                    phase23_registry=phase23,
                )
                # Mock create_autonomous_deployment to fail
                with patch(
                    "trading_system.autonomous.controller.AutonomousController.create_autonomous_deployment",
                    return_value=(DeploymentCreationResult.FAILURE, None),
                ):
                    controller._ensure_autonomous_deployments()

    # Verify diagnostic event was recorded for the failure
    events = controller._event_log.events
    failure_events = [e for e in events if "deployment creation failed" in e.message]
    assert len(failure_events) == 1
    assert "test-strategy" in failure_events[0].message
    assert "failure" in failure_events[0].message.lower()
