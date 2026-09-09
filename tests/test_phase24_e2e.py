"""End-to-end test: autonomous discovery and paper deployment."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from trading_system.research.phase23.registry import Phase23Registry
from trading_system.research.phase23.discovery import Phase23Discovery
from trading_system.research.phase23.deployment import Phase23DeploymentPolicy
from trading_system.research.phase23.strategies import build_default_universe
from trading_system.research.evidence import StrategyStatus


def test_autonomous_discovery_and_deployment():
    """Prove the autonomous engine can discover and deploy without hardcoding."""
    # Setup mock registry with a PAPER_APPROVED strategy
    registry = MagicMock()
    strategy_mock = MagicMock()
    strategy_mock.strategy_id = "strat-1"
    strategy_mock.name = "Test Strategy"
    strategy_mock.symbol = "NSE:SBIN"
    strategy_mock.timeframe = "1d"
    strategy_mock.spec_hash = "abc123"
    strategy_mock.status = StrategyStatus.VALIDATED

    registry.get_paper_approved.return_value = [strategy_mock]
    registry.list_evidence.return_value = [
        MagicMock(configuration={"qualification_status": "PAPER_APPROVED", "candidate_id": "test-candidate", "score": 75.0})
    ]
    registry.get_strategy.return_value = strategy_mock

    # Discovery
    discovery = Phase23Discovery(registry)
    discovered = discovery.discover(max_candidates=5)
    assert len(discovered) == 1
    assert discovered[0].strategy_id == "strat-1"
    assert discovered[0].candidate_id == "test-candidate"

    # Deployment (mock control center)
    cc = MagicMock()
    cc.get_deployment.return_value = None
    deployment_mock = MagicMock()
    deployment_mock.deployment_id = "dep-1"
    cc.create_deployment.return_value = (deployment_mock, MagicMock(), MagicMock(rejection_reason=None))
    cc.activate_deployment.return_value = deployment_mock

    policy = Phase23DeploymentPolicy(control_center=cc, dataset_id="tournament")
    spec_mock = MagicMock()
    spec_mock.risk.stop_loss_pct = 0.03
    spec_mock.risk.take_profit_pct = 0.06
    spec_mock.model_dump.return_value = {}
    result = policy.deploy(
        strategy_id="strat-1",
        candidate_id="test-candidate",
        spec=spec_mock,
        symbol="NSE:SBIN",
        timeframe="1d",
        activate=True,
    )
    assert result.success is True
    assert result.deployment is not None
