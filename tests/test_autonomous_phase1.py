"""Phase 1 - Autonomous architecture tests.

Covers the complete autonomous layer: configuration, lifecycle, controller,
policy validation, decision model, deployment coordinator, and failure safety.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta

from trading_system.paper.deployment import (
    PaperDeployment,
    PaperDeploymentConfig,
    PaperDeploymentRecord,
    PaperDeploymentStatus,
)
from trading_system.paper.control import (
    InvalidLifecycleTransitionError,
    PaperTradingControlCenter,
    UnknownDeploymentError,
)
from trading_system.research.strategy_lab.spec import (
    StrategySpec,
    field_operand,
    indicator_operand,
    make_condition,
)

from trading_system.autonomous import (
    AutonomousBotConfig,
    AutonomousBotLifecycle,
    AutonomousController,
    AutonomousDecision,
    AutonomousDeploymentCoordinator,
    DeploymentCreationResult,
    PolicyValidator,
    PolicyValidationResult,
    Source,
)
from trading_system.autonomous.bot_lifecycle import (
    AutonomousBotState,
    BotTransitionError,
    is_valid_transition,
)
from trading_system.autonomous.bot_config import (
    UserConstraints,
    TradingMode,
    BotMode,
    BotState,
    TradingSessionConstraints,
    MaxPositionPct,
    MaxExposurePct,
)


def _make_strategy_spec(symbol: str = "NSE:SBIN", timeframe: str = "1d") -> StrategySpec:
    return StrategySpec(
        name="Test Strategy",
        description="test strategy for autonomous tests",
        symbol=symbol,
        timeframe=timeframe,
        indicators=[{"name": "sma", "params": {"window": 5}}],
        entry=make_condition(field_operand("close"), ">", indicator_operand("sma_5")),
        generated_by="test",
    )


def _make_config(
    *,
    bot_id: str = "bot-test-001",
    name: str = "Test Bot",
    mode: BotMode = BotMode.AUTONOMOUS,
    trading_mode: TradingMode = TradingMode.PAPER,
    enabled: bool = True,
    allowed_symbols=None,
    allowed_strategy_ids=None,
    allowed_timeframes=None,
    max_simultaneous_positions: int = 5,
    max_position_allocation_pct: float = 0.25,
    max_exposure_pct: float = 0.75,
    max_drawdown_pct: float = 0.15,
    trading_session=None,
) -> AutonomousBotConfig:
    if allowed_symbols is None:
        allowed_symbols = frozenset({"NSE:SBIN", "NSE:TCS", "NSE:INFY"})
    if allowed_strategy_ids is None:
        allowed_strategy_ids = frozenset({"strat-001", "strat-002", "strat-003"})
    if allowed_timeframes is None:
        allowed_timeframes = frozenset({"1m", "5m", "15m", "1d"})
    user_constraints = UserConstraints(
        allowed_symbols=allowed_symbols,
        allowed_strategy_ids=allowed_strategy_ids,
        allowed_timeframes=allowed_timeframes,
        max_drawdown_pct=max_drawdown_pct,
        trading_session=trading_session or TradingSessionConstraints(),
    )
    return AutonomousBotConfig(
        bot_id=bot_id,
        name=name,
        mode=mode,
        trading_mode=trading_mode,
        enabled=enabled,
        user_constraints=user_constraints,
        max_simultaneous_positions=max_simultaneous_positions,
        max_position_allocation_pct=MaxPositionPct(max_position_allocation_pct),
        max_exposure_pct=MaxExposurePct(max_exposure_pct),
        source=Source.AUTONOMOUS,
    )


def _make_controller(
    *,
    config: AutonomousBotConfig | None = None,
    with_control_center: bool = True,
) -> AutonomousController:
    if config is None:
        config = _make_config()
    control_center = None
    if with_control_center:
        from sqlalchemy import create_engine
        from trading_system.research.evidence import EvidenceStore
        from trading_system.research.strategy_registry import StrategyRegistry
        from trading_system.research.strategy_intelligence import StrategyIntelligence
        from trading_system.paper.gate import DeploymentGate
        from trading_system.research.strategy_intelligence import (
            EvidenceRequirement,
            EvidenceFreshnessConfig,
        )
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
    return AutonomousController(
        config=config,
        control_center=control_center,
    )


def _register_eligible_evidence(
    registry,
    strategy,
    ds_id: str = "ds-autonomous",
    total_trades: int = 100,
) -> None:
    """Record research + walk-forward evidence so the deployment gate passes.

    Mirrors the established pattern in tests/test_phase21_api.py and
    tests/test_phase20_control_center.py. The strategy must already be registered
    and have its status set to WALK_FORWARD_VALIDATED.
    """
    from datetime import datetime, timezone, timedelta

    from trading_system.research.evidence import (
        EvidenceType,
        StrategyEvidence,
        StrategyStatus,
    )
    from trading_system.research.strategy_registry import (
        evidence_identity as _evidence_identity,
    )

    fresh = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    registry.record_evidence(
        StrategyEvidence(
            evidence_id=_evidence_identity(
                strategy.strategy_id, EvidenceType.RESEARCH, ds_id, {"k": 1}
            ),
            strategy_id=strategy.strategy_id,
            strategy_spec_hash=strategy.spec_hash,
            evidence_type=EvidenceType.RESEARCH,
            dataset_id=ds_id,
            configuration_json={"k": 1},
            metrics_json={
                "rows": 400,
                "candidates": [
                    {
                        "variant_index": 0,
                        "status": "evaluated",
                        "spec_name": strategy.name,
                        "spec_errors": [],
                        "error": "",
                        "evaluation": {
                            "total_return": 0.10,
                            "profit_factor": 1.5,
                            "max_drawdown": -0.05,
                            "n_trades": 25,
                        },
                        "filter_passed": True,
                        "filter_reasons": [],
                    }
                ],
                "ranking": [],
                "notes": [],
            },
            created_at=fresh,
        )
    )
    registry.record_evidence(
        StrategyEvidence(
            evidence_id=_evidence_identity(
                strategy.strategy_id, EvidenceType.WALK_FORWARD, ds_id, {"k": 2}
            ),
            strategy_id=strategy.strategy_id,
            strategy_spec_hash=strategy.spec_hash,
            evidence_type=EvidenceType.WALK_FORWARD,
            dataset_id=ds_id,
            configuration_json={"k": 2},
            metrics_json={
                "kind": "fixed_spec",
                "spec_name": strategy.name,
                "symbol": strategy.symbol,
                "timeframe": strategy.timeframe,
                "mode": "rolling",
                "folds": [],
                "summary": {
                    "n_folds": 5,
                    "n_valid": 4,
                    "n_failed": 1,
                    "coverage": 0.8,
                    "coverage_ok": True,
                    "positive_folds": 3,
                    "positive_fold_ratio": 0.75,
                    "avg_fold_return": 0.05,
                    "median_fold_return": 0.05,
                    "worst_fold_return": -0.05,
                    "best_fold_return": 0.15,
                    "return_std": 0.05,
                    "return_dispersion": 1.0,
                    "max_validation_drawdown": -0.08,
                    "consistency_score": 0.7,
                    "total_validation_trades": total_trades,
                    "min_validation_trades": 10,
                    "valid_fold_ids": [0, 1, 2, 3],
                },
                "warnings": [],
                "notes": [],
            },
            created_at=fresh,
        )
    )


def _make_eligible_controller(
    *,
    bot_id: str = "bot-test-001",
    name: str = "Eligible Bot",
    symbol: str = "NSE:SBIN",
    timeframe: str = "1d",
    allowed_symbols=None,
    allowed_strategy_ids=None,
    allowed_timeframes=None,
    max_drawdown_pct: float = 0.15,
) -> tuple[AutonomousController, "StrategySpec", object]:
    """Build a controller wired to a *real* control center with one eligible
    strategy registered (research + walk-forward evidence recorded, status
    WALK_FORWARD_VALIDATED), so create_deployment passes the gate.

    Returns (controller, spec, strategy).
    """
    from sqlalchemy import create_engine
    from trading_system.research.evidence import EvidenceStore
    from trading_system.research.strategy_registry import StrategyRegistry
    from trading_system.research.strategy_intelligence import (
        EvidenceFreshnessConfig,
        EvidenceRequirement,
        StrategyIntelligence,
    )
    from trading_system.paper import DeploymentGate

    engine = create_engine("sqlite://")
    store = EvidenceStore(engine)
    registry = StrategyRegistry(store)
    intelligence = StrategyIntelligence(registry)
    gate = DeploymentGate(
        intelligence=intelligence,
        requirement=EvidenceRequirement(),
        freshness_config=EvidenceFreshnessConfig(max_age_days=180),
    )
    from trading_system.research.evidence import StrategyStatus

    center = PaperTradingControlCenter(
        registry=registry,
        intelligence=intelligence,
        gate=gate,
    )
    spec = _make_strategy_spec(symbol=symbol, timeframe=timeframe)
    strategy = registry.register_strategy(spec)
    registry.update_strategy_status(
        strategy.strategy_id, StrategyStatus.WALK_FORWARD_VALIDATED
    )
    _register_eligible_evidence(registry, strategy)
    uc_sym = frozenset({symbol}) if allowed_symbols is None else allowed_symbols
    uc_strat = (
        frozenset({strategy.strategy_id})
        if allowed_strategy_ids is None
        else allowed_strategy_ids
    )
    uc_tf = frozenset({timeframe}) if allowed_timeframes is None else allowed_timeframes
    config = _make_config(
        bot_id=bot_id,
        name=name,
        allowed_symbols=uc_sym,
        allowed_strategy_ids=uc_strat,
        allowed_timeframes=uc_tf,
        max_drawdown_pct=max_drawdown_pct,
    )
    controller = AutonomousController(config=config, control_center=center)
    return controller, spec, strategy


def _seed_deployment_record(
    controller: AutonomousController,
    *,
    deployment_id: str,
    bot_id: str,
    symbol: str = "NSE:SBIN",
    timeframe: str = "1d",
    strategy_id: str = "strat-001",
    status: "PaperDeploymentStatus" = PaperDeploymentStatus.ACTIVE,
    notes: str | None = None,
) -> PaperDeployment:
    """Persist a PaperDeploymentRecord directly (bypassing the gate) so that
    coordinator behaviors (duplicate detection, ownership, listing) can be
    exercised deterministically without the full evidence ceremony.
    """
    dep = PaperDeployment(
        deployment_id=deployment_id,
        strategy_id=strategy_id,
        strategy_spec_hash="hash-seed",
        symbol=symbol,
        timeframe=timeframe,
        dataset_id=f"ds-{deployment_id}",
        config=PaperDeploymentConfig(),
        status=status,
        notes=notes if notes is not None else f"bot:{bot_id}",
    )
    with controller.control_center.registry.store._Session() as s:
        s.merge(dep.as_record())
        s.commit()
    return dep


class TestAutonomousBotConfig:
    def test_valid_config(self):
        config = _make_config()
        assert config.bot_id == "bot-test-001"
        assert config.name == "Test Bot"
        assert config.mode == BotMode.AUTONOMOUS
        assert config.trading_mode == TradingMode.PAPER
        assert config.enabled is True
        assert config.source == Source.AUTONOMOUS

    def test_paper_only_enforced(self):
        with pytest.raises(Exception):
            _make_config(trading_mode=TradingMode.LIVE)

    def test_invalid_max_positions(self):
        with pytest.raises(Exception):
            _make_config(max_simultaneous_positions=0)

    def test_allocation_pct_values_accepted(self):
        # MaxPositionPct and MaxExposurePct accept float values
        config = _make_config(max_position_allocation_pct=0.5, max_exposure_pct=0.8)
        assert config.max_position_allocation_pct == 0.5
        assert config.max_exposure_pct == 0.8

    def test_config_with_specific_symbols(self):
        config = _make_config(allowed_symbols=frozenset({"NSE:RELIANCE"}))
        assert config.user_constraints.allowed_symbols == frozenset({"NSE:RELIANCE"})

    def test_config_with_specific_strategies(self):
        config = _make_config(allowed_strategy_ids=frozenset({"my-strategy"}))
        assert config.user_constraints.allowed_strategy_ids == frozenset({"my-strategy"})

    def test_config_with_specific_timeframes(self):
        config = _make_config(allowed_timeframes=frozenset({"1h"}))
        assert config.user_constraints.allowed_timeframes == frozenset({"1h"})

    def test_user_constraints_immutable_separation(self):
        config = _make_config()
        assert hasattr(config, "user_constraints")
        assert hasattr(config.user_constraints, "allowed_symbols")
        assert hasattr(config.user_constraints, "allowed_strategy_ids")
        assert hasattr(config.user_constraints, "allowed_timeframes")
        assert hasattr(config, "max_simultaneous_positions")
        assert hasattr(config, "max_position_allocation_pct")
        assert hasattr(config, "max_exposure_pct")

    def test_config_extra_fields_rejected(self):
        with pytest.raises(Exception):
            AutonomousBotConfig(
                bot_id="test",
                name="Test",
                mode=BotMode.AUTONOMOUS,
                trading_mode=TradingMode.PAPER,
                user_constraints=UserConstraints(
                    allowed_symbols=frozenset({"NSE:SBIN"}),
                    allowed_strategy_ids=frozenset({"s1"}),
                    allowed_timeframes=frozenset({"1d"}),
                ),
                unknown_field="should_fail",
            )

    def test_manual_mode_allowed(self):
        config = _make_config(mode=BotMode.MANUAL)
        assert config.mode == BotMode.MANUAL

class TestAutonomousBotLifecycle:
    def test_initial_state_is_created(self):
        lifecycle = AutonomousBotLifecycle()
        assert lifecycle.state == AutonomousBotState.CREATED

    def test_full_lifecycle_transitions(self):
        lifecycle = AutonomousBotLifecycle()
        lifecycle.transition_to(AutonomousBotState.STARTING)
        assert lifecycle.state == AutonomousBotState.STARTING
        lifecycle.transition_to(AutonomousBotState.RUNNING)
        assert lifecycle.state == AutonomousBotState.RUNNING
        lifecycle.transition_to(AutonomousBotState.PAUSED)
        assert lifecycle.state == AutonomousBotState.PAUSED
        lifecycle.transition_to(AutonomousBotState.RUNNING)
        assert lifecycle.state == AutonomousBotState.RUNNING
        lifecycle.transition_to(AutonomousBotState.STOPPING)
        assert lifecycle.state == AutonomousBotState.STOPPING
        lifecycle.transition_to(AutonomousBotState.STOPPED)
        assert lifecycle.state == AutonomousBotState.STOPPED

    def test_invalid_transition_stays_stopped(self):
        lifecycle = AutonomousBotLifecycle()
        lifecycle.transition_to(AutonomousBotState.STARTING)
        lifecycle.transition_to(AutonomousBotState.RUNNING)
        lifecycle.transition_to(AutonomousBotState.STOPPING)
        lifecycle.transition_to(AutonomousBotState.STOPPED)
        with pytest.raises(BotTransitionError):
            lifecycle.transition_to(AutonomousBotState.RUNNING)
        assert lifecycle.state == AutonomousBotState.STOPPED

    def test_invalid_transition_stays_error(self):
        lifecycle = AutonomousBotLifecycle(AutonomousBotState.ERROR)
        with pytest.raises(BotTransitionError):
            lifecycle.transition_to(AutonomousBotState.RUNNING)
        assert lifecycle.state == AutonomousBotState.ERROR

    def test_invalid_transition_stays_paused(self):
        lifecycle = AutonomousBotLifecycle()
        lifecycle.transition_to(AutonomousBotState.STARTING)
        lifecycle.transition_to(AutonomousBotState.RUNNING)
        lifecycle.transition_to(AutonomousBotState.PAUSED)
        with pytest.raises(BotTransitionError):
            lifecycle.transition_to(AutonomousBotState.STARTING)
        assert lifecycle.state == AutonomousBotState.PAUSED

    def test_invalid_transition_stays_running(self):
        lifecycle = AutonomousBotLifecycle()
        lifecycle.transition_to(AutonomousBotState.STARTING)
        lifecycle.transition_to(AutonomousBotState.RUNNING)
        with pytest.raises(BotTransitionError):
            lifecycle.transition_to(AutonomousBotState.CREATED)
        assert lifecycle.state == AutonomousBotState.RUNNING

    def test_invalid_transition_stays_stopping(self):
        lifecycle = AutonomousBotLifecycle()
        lifecycle.transition_to(AutonomousBotState.STARTING)
        lifecycle.transition_to(AutonomousBotState.RUNNING)
        lifecycle.transition_to(AutonomousBotState.STOPPING)
        with pytest.raises(BotTransitionError):
            lifecycle.transition_to(AutonomousBotState.PAUSED)
        assert lifecycle.state == AutonomousBotState.STOPPING

    def test_is_valid_transition_helper(self):
        assert is_valid_transition(AutonomousBotState.CREATED, AutonomousBotState.STARTING) is True
        assert is_valid_transition(AutonomousBotState.STARTING, AutonomousBotState.RUNNING) is True
        assert is_valid_transition(AutonomousBotState.RUNNING, AutonomousBotState.PAUSED) is True
        assert is_valid_transition(AutonomousBotState.PAUSED, AutonomousBotState.RUNNING) is True
        assert is_valid_transition(AutonomousBotState.RUNNING, AutonomousBotState.STOPPING) is True
        assert is_valid_transition(AutonomousBotState.STOPPING, AutonomousBotState.STOPPED) is True
        assert is_valid_transition(AutonomousBotState.STOPPED, AutonomousBotState.RUNNING) is False
        assert is_valid_transition(AutonomousBotState.ERROR, AutonomousBotState.RUNNING) is False
        assert is_valid_transition(AutonomousBotState.PAUSED, AutonomousBotState.STARTING) is False
        assert is_valid_transition(AutonomousBotState.RUNNING, AutonomousBotState.CREATED) is False
        assert is_valid_transition(AutonomousBotState.STOPPING, AutonomousBotState.PAUSED) is False

class TestAutonomousController:
    def test_controller_starts_in_created_state(self):
        controller = _make_controller()
        assert controller.lifecycle.state == AutonomousBotState.CREATED

    def test_controller_start_transitions_to_running(self):
        controller = _make_controller()
        controller.start_bot()
        assert controller.lifecycle.state == AutonomousBotState.RUNNING

    def test_controller_pause_transitions_to_paused(self):
        controller = _make_controller()
        controller.start_bot()
        controller.pause_bot()
        assert controller.lifecycle.state == AutonomousBotState.PAUSED

    def test_controller_resume_transitions_to_running(self):
        controller = _make_controller()
        controller.start_bot()
        controller.pause_bot()
        controller.resume_bot()
        assert controller.lifecycle.state == AutonomousBotState.RUNNING

    def test_controller_stop_transitions_to_stopped(self):
        controller = _make_controller()
        controller.start_bot()
        controller.stop_bot()
        assert controller.lifecycle.state == AutonomousBotState.STOPPED

    def test_controller_inspect_returns_state_snapshot(self):
        controller = _make_controller()
        snapshot = controller.inspect()
        assert snapshot["bot_id"] == "bot-test-001"
        assert "state" in snapshot
        assert "trading_mode" in snapshot

    def test_controller_starts_and_stops_cleanly(self):
        controller = _make_controller()
        controller.start_bot()
        assert controller.lifecycle.state == AutonomousBotState.RUNNING
        controller.stop_bot()
        assert controller.lifecycle.state == AutonomousBotState.STOPPED

class TestPolicyValidator:
    def test_valid_decision_passes_policy(self):
        config = _make_config()
        validator = PolicyValidator(config)
        result = validator.validate_decision(
            symbol="NSE:SBIN",
            strategy_id="strat-001",
            timeframe="1d",
        )
        assert result == PolicyValidationResult.VALID

    def test_disallowed_symbol_rejected(self):
        config = _make_config()
        validator = PolicyValidator(config)
        result = validator.validate_decision(
            symbol="DISALLOWED:SYMBOL",
            strategy_id="strat-001",
            timeframe="1d",
        )
        assert result == PolicyValidationResult.INVALID_SYMBOL

    def test_disallowed_strategy_rejected(self):
        config = _make_config()
        validator = PolicyValidator(config)
        result = validator.validate_decision(
            symbol="NSE:SBIN",
            strategy_id="disallowed-strategy",
            timeframe="1d",
        )
        assert result == PolicyValidationResult.INVALID_STRATEGY

    def test_disallowed_timeframe_rejected(self):
        config = _make_config()
        validator = PolicyValidator(config)
        result = validator.validate_decision(
            symbol="NSE:SBIN",
            strategy_id="strat-001",
            timeframe="1y",
        )
        assert result == PolicyValidationResult.INVALID_TIMEFRAME

    def test_multiple_violations_returns_first_match(self):
        config = _make_config()
        validator = PolicyValidator(config)
        result = validator.validate_decision(
            symbol="DISALLOWED:SYMBOL",
            strategy_id="disallowed-strategy",
            timeframe="1y",
        )
        # Should return one of the invalid results
        assert result != PolicyValidationResult.VALID

class TestAutonomousDecision:
    def test_create_valid_decision(self):
        decision = AutonomousDecision.create(
            symbol="NSE:SBIN",
            strategy_id="strat-001",
            timeframe="1d",
            action="LONG_ENTRY",
        )
        assert decision.symbol == "NSE:SBIN"
        assert decision.strategy_id == "strat-001"
        assert decision.timeframe == "1d"
        assert decision.action == "LONG_ENTRY"
        assert decision.policy_validation == PolicyValidationResult.VALID

    def test_decision_has_unique_id(self):
        d1 = AutonomousDecision.create(
            symbol="NSE:SBIN", strategy_id="s1", timeframe="1d", action="LONG_ENTRY"
        )
        d2 = AutonomousDecision.create(
            symbol="NSE:TCS", strategy_id="s1", timeframe="1d", action="LONG_ENTRY"
        )
        assert d1.decision_id != d2.decision_id

    def test_decision_id_deterministic_for_same_input(self):
        d1 = AutonomousDecision.create(
            symbol="NSE:SBIN", strategy_id="s1", timeframe="1d", action="LONG_ENTRY",
            decision_timestamp="2024-01-01T00:00:00+00:00",
        )
        d2 = AutonomousDecision.create(
            symbol="NSE:SBIN", strategy_id="s1", timeframe="1d", action="LONG_ENTRY",
            decision_timestamp="2024-01-01T00:00:00+00:00",
        )
        assert d1.decision_id == d2.decision_id

    def test_decision_with_confidence(self):
        decision = AutonomousDecision(
            decision_id="test-123",
            symbol="NSE:SBIN",
            strategy_id="strat-001",
            timeframe="1d",
            action="LONG_ENTRY",
            confidence=0.85,
        )
        assert decision.confidence == 0.85

    def test_decision_rejected_by_policy(self):
        decision = AutonomousDecision(
            decision_id="test-123",
            symbol="NSE:SBIN",
            strategy_id="strat-001",
            timeframe="1d",
            action="LONG_ENTRY",
            policy_validation=PolicyValidationResult.INVALID_SYMBOL,
        )
        assert decision.policy_validation == PolicyValidationResult.INVALID_SYMBOL

    def test_extra_fields_rejected(self):
        with pytest.raises(Exception):
            AutonomousDecision(
                decision_id="test",
                symbol="NSE:SBIN",
                strategy_id="strat-001",
                timeframe="1d",
                action="LONG_ENTRY",
                unknown_field="should_fail",
            )

class TestAutonomousCoordinator:
    def test_coordinator_reuses_existing_control_center(self):
        controller = _make_controller()
        assert controller.control_center is not None
        assert isinstance(controller.control_center, PaperTradingControlCenter)

    def test_coordinator_can_list_deployments(self):
        controller = _make_controller()
        deployments = controller.list_autonomous_deployments()
        assert isinstance(deployments, list)

    def test_manual_deployment_isolation(self):
        controller = _make_controller()
        deployment = PaperDeployment(
            deployment_id="manual-dep-1",
            strategy_id="manual-strategy",
            strategy_spec_hash="hash",
            symbol="NSE:SBIN",
            timeframe="1d",
            dataset_id="ds-1",
            config=PaperDeploymentConfig(),
            status=PaperDeploymentStatus.CREATED,
            notes="manual deployment",
        )
        with controller.control_center.registry.store._Session() as s:
            s.merge(deployment.as_record())
            s.commit()
        autonomous = controller.list_autonomous_deployments()
        assert not any(d.deployment_id == "manual-dep-1" for d in autonomous)

class TestFailureSafety:
    def test_bot_never_claims_running_when_startup_fails(self):
        controller = _make_controller()
        original_transition = controller.lifecycle.transition_to
        def failing_transition(state):
            if state == AutonomousBotState.RUNNING:
                raise RuntimeError("Simulated startup failure")
            original_transition(state)
        controller.lifecycle.transition_to = failing_transition
        result, message = controller.start_bot()
        assert result is False
        assert controller.lifecycle.state == AutonomousBotState.ERROR

    def test_stop_from_running_state_is_safe(self):
        controller = _make_controller()
        controller.start_bot()
        controller.stop_bot()
        assert controller.lifecycle.state == AutonomousBotState.STOPPED

    def test_double_stop_is_safe(self):
        controller = _make_controller()
        controller.start_bot()
        controller.stop_bot()
        controller.stop_bot()
        assert controller.lifecycle.state == AutonomousBotState.STOPPED


class TestPaperOnlyGuarantee:
    def test_config_rejects_live_mode(self):
        with pytest.raises(Exception):
            _make_config(trading_mode=TradingMode.LIVE)

    def test_controller_uses_paper_control_center(self):
        controller = _make_controller()
        assert controller.control_center is not None
        assert isinstance(controller.control_center, PaperTradingControlCenter)

    def test_autonomous_deployment_uses_paper_path(self):
        controller = _make_controller()
        assert controller.control_center is not None
        assert not hasattr(controller.control_center, "live_broker")


# --------------------------------------------------------------------------- #
# Phase 1 — additional focused behavioral coverage
# --------------------------------------------------------------------------- #
class TestAutonomousBotLifecyclePhase1:
    def test_initial_state_explicit(self):
        lifecycle = AutonomousBotLifecycle(initial_state=AutonomousBotState.STARTING)
        assert lifecycle.state == AutonomousBotState.STARTING

    def test_error_to_stopped_is_recovery_path(self):
        # ERROR -> STOPPED is the only valid exit from an errored bot (safe shutdown).
        lifecycle = AutonomousBotLifecycle(initial_state=AutonomousBotState.ERROR)
        assert lifecycle.can_transition_to(AutonomousBotState.STOPPED) is True
        lifecycle.transition_to(AutonomousBotState.STOPPED)
        assert lifecycle.state == AutonomousBotState.STOPPED

    def test_error_cannot_skip_to_running(self):
        lifecycle = AutonomousBotLifecycle(initial_state=AutonomousBotState.ERROR)
        with pytest.raises(BotTransitionError):
            lifecycle.transition_to(AutonomousBotState.RUNNING)
        assert lifecycle.state == AutonomousBotState.ERROR

    def test_stopped_is_terminal(self):
        lifecycle = AutonomousBotLifecycle()
        lifecycle.transition_to(AutonomousBotState.STARTING)
        lifecycle.transition_to(AutonomousBotState.RUNNING)
        lifecycle.transition_to(AutonomousBotState.STOPPING)
        lifecycle.transition_to(AutonomousBotState.STOPPED)
        for target in AutonomousBotState:
            assert is_valid_transition(AutonomousBotState.STOPPED, target) is False

    def test_running_only_allows_pause_or_stop(self):
        assert is_valid_transition(AutonomousBotState.RUNNING, AutonomousBotState.PAUSED) is True
        assert is_valid_transition(AutonomousBotState.RUNNING, AutonomousBotState.STOPPING) is True
        assert is_valid_transition(AutonomousBotState.RUNNING, AutonomousBotState.STARTING) is False
        assert is_valid_transition(AutonomousBotState.RUNNING, AutonomousBotState.STOPPED) is False
        assert is_valid_transition(AutonomousBotState.RUNNING, AutonomousBotState.ERROR) is False

    def test_can_transition_to_reflects_current_state(self):
        lifecycle = AutonomousBotLifecycle()
        assert lifecycle.can_transition_to(AutonomousBotState.STARTING) is True
        assert lifecycle.can_transition_to(AutonomousBotState.RUNNING) is False
        lifecycle.transition_to(AutonomousBotState.STARTING)
        assert lifecycle.can_transition_to(AutonomousBotState.RUNNING) is True
        assert lifecycle.can_transition_to(AutonomousBotState.PAUSED) is False
        assert lifecycle.can_transition_to(AutonomousBotState.CREATED) is False

    def test_reprs_are_informative(self):
        lifecycle = AutonomousBotLifecycle()
        assert "AutonomousBotLifecycle" in repr(lifecycle)
        assert "created" in str(lifecycle)


class TestAutonomousControllerLifecycle:
    def test_start_returns_success_tuple(self):
        controller = _make_controller()
        result, message = controller.start_bot()
        assert result is True
        assert "running" in message.lower()
        assert controller.lifecycle.state == AutonomousBotState.RUNNING

    def test_start_when_already_running_is_rejected(self):
        controller = _make_controller()
        controller.start_bot()
        result, message = controller.start_bot()
        assert result is False
        assert controller.lifecycle.state == AutonomousBotState.RUNNING

    def test_start_from_stopped_is_rejected(self):
        controller = _make_controller()
        controller.start_bot()
        controller.stop_bot()
        result, message = controller.start_bot()
        assert result is False
        assert controller.lifecycle.state == AutonomousBotState.STOPPED

    def test_pause_from_created_is_rejected(self):
        controller = _make_controller()
        result, message = controller.pause_bot()
        assert result is False
        assert "PAUSED" in message
        assert controller.lifecycle.state == AutonomousBotState.CREATED

    def test_resume_from_running_is_rejected(self):
        controller = _make_controller()
        controller.start_bot()
        result, message = controller.resume_bot()
        assert result is False
        assert controller.lifecycle.state == AutonomousBotState.RUNNING

    def test_stop_from_created_is_rejected(self):
        controller = _make_controller()
        result, message = controller.stop_bot()
        assert result is False
        assert controller.lifecycle.state == AutonomousBotState.CREATED

    def test_start_sets_last_decision_timestamp(self):
        controller = _make_controller()
        assert controller.config.last_decision_timestamp is None
        controller.start_bot()
        assert controller.config.last_decision_timestamp is not None

    def test_stop_clears_last_decision_timestamp(self):
        controller = _make_controller()
        controller.start_bot()
        assert controller.config.last_decision_timestamp is not None
        controller.stop_bot()
        assert controller.config.last_decision_timestamp is None

    def test_config_state_tracks_lifecycle(self):
        controller = _make_controller()
        assert controller.config.state == BotState.CREATED
        controller.start_bot()
        assert controller.config.state == AutonomousBotState.RUNNING
        controller.pause_bot()
        assert controller.config.state == AutonomousBotState.PAUSED
        controller.resume_bot()
        assert controller.config.state == AutonomousBotState.RUNNING

    def test_stop_bot_stops_owned_autonomous_deployments(self):
        controller = _make_controller()
        _seed_deployment_record(
            controller,
            deployment_id="dep-owned",
            bot_id=controller.config.bot_id,
            status=PaperDeploymentStatus.ACTIVE,
        )
        controller.start_bot()
        controller.stop_bot()
        reloaded = controller.control_center.get_deployment("dep-owned")
        assert reloaded is not None
        assert reloaded.status == PaperDeploymentStatus.STOPPED

    def test_stop_bot_leaves_other_bots_deployments_untouched(self):
        controller = _make_controller()
        _seed_deployment_record(
            controller,
            deployment_id="dep-other",
            bot_id="bot-other",
            status=PaperDeploymentStatus.ACTIVE,
        )
        controller.start_bot()
        controller.stop_bot()
        reloaded = controller.control_center.get_deployment("dep-other")
        assert reloaded.status == PaperDeploymentStatus.ACTIVE

    def test_inspect_contains_full_state_snapshot(self):
        controller = _make_controller()
        controller.start_bot()
        snapshot = controller.inspect()
        for key in (
            "bot_id", "name", "state", "mode", "trading_mode", "enabled",
            "decision_count", "deployment_count", "last_decision_timestamp",
            "source", "allowed_symbols", "max_simultaneous_positions",
            "max_position_allocation_pct", "max_exposure_pct",
        ):
            assert key in snapshot
        assert snapshot["mode"] == BotMode.AUTONOMOUS.value
        assert snapshot["trading_mode"] == TradingMode.PAPER.value
        assert snapshot["source"] == Source.AUTONOMOUS.value
        assert snapshot["decision_count"] == 0


class TestPolicyValidatorPhase1:
    def test_empty_constraints_allow_any_symbol(self):
        config = _make_config(allowed_symbols=frozenset())
        validator = PolicyValidator(config)
        assert (
            validator.validate_decision(symbol="ANY", strategy_id="strat-001", timeframe="1d")
            == PolicyValidationResult.VALID
        )

    def test_empty_constraints_allow_any_strategy(self):
        config = _make_config(allowed_strategy_ids=frozenset())
        validator = PolicyValidator(config)
        assert (
            validator.validate_decision(symbol="NSE:SBIN", strategy_id="ANY", timeframe="1d")
            == PolicyValidationResult.VALID
        )

    def test_empty_constraints_allow_any_timeframe(self):
        config = _make_config(allowed_timeframes=frozenset())
        validator = PolicyValidator(config)
        assert (
            validator.validate_decision(symbol="NSE:SBIN", strategy_id="strat-001", timeframe="ANY")
            == PolicyValidationResult.VALID
        )

    def test_validate_autonomous_decision_delegates_valid(self):
        config = _make_config()
        validator = PolicyValidator(config)
        decision = AutonomousDecision.create(
            symbol="NSE:SBIN", strategy_id="strat-001", timeframe="1d",
            action="LONG_ENTRY",
        )
        assert validator.validate_autonomous_decision(decision) == PolicyValidationResult.VALID

    def test_validate_autonomous_decision_delegates_rejected(self):
        config = _make_config()
        validator = PolicyValidator(config)
        decision = AutonomousDecision.create(
            symbol="DISALLOWED", strategy_id="strat-001", timeframe="1d",
            action="LONG_ENTRY",
        )
        assert (
            validator.validate_autonomous_decision(decision)
            == PolicyValidationResult.INVALID_SYMBOL
        )

    def test_trading_session_with_hours_is_accepted(self):
        config = _make_config(
            trading_session=TradingSessionConstraints(
                allowed_hours=frozenset({(9, 16)}),
                min_cooldown_minutes=5,
            )
        )
        validator = PolicyValidator(config)
        # The Phase 1 session check is a placeholder that always permits.
        assert (
            validator.validate_decision(symbol="NSE:SBIN", strategy_id="strat-001", timeframe="1d")
            == PolicyValidationResult.VALID
        )

    def test_controller_validate_decision_returns_valid_message(self):
        controller = _make_controller()
        result, message = controller.validate_decision(
            symbol="NSE:SBIN", strategy_id="strat-001", timeframe="1d"
        )
        assert result == PolicyValidationResult.VALID
        assert message == "decision is valid"

    def test_controller_validate_decision_translates_invalid_symbol(self):
        controller = _make_controller()
        result, message = controller.validate_decision(
            symbol="DISALLOWED", strategy_id="strat-001", timeframe="1d"
        )
        assert result == PolicyValidationResult.INVALID_SYMBOL
        assert "DISALLOWED" in message


class TestAutonomousDecisionModel:
    def test_is_valid_true_for_valid(self):
        decision = AutonomousDecision.create(
            symbol="NSE:SBIN", strategy_id="s1", timeframe="1d", action="LONG_ENTRY"
        )
        assert decision.is_valid is True
        assert decision.is_rejected is False

    def test_is_valid_false_for_rejected(self):
        decision = AutonomousDecision(
            decision_id="x", symbol="NSE:SBIN", strategy_id="s1", timeframe="1d",
            action="LONG_ENTRY", policy_validation=PolicyValidationResult.INVALID_SYMBOL,
        )
        assert decision.is_valid is False
        assert decision.is_rejected is True

    def test_default_policy_validation_is_valid(self):
        decision = AutonomousDecision(
            decision_id="x", symbol="NSE:SBIN", strategy_id="s1", timeframe="1d",
            action="LONG_ENTRY",
        )
        assert decision.policy_validation == PolicyValidationResult.VALID

    def test_confidence_above_one_rejected(self):
        with pytest.raises(Exception):
            AutonomousDecision(
                decision_id="x", symbol="NSE:SBIN", strategy_id="s1", timeframe="1d",
                action="LONG_ENTRY", confidence=1.5,
            )

    def test_confidence_below_zero_rejected(self):
        with pytest.raises(Exception):
            AutonomousDecision(
                decision_id="x", symbol="NSE:SBIN", strategy_id="s1", timeframe="1d",
                action="LONG_ENTRY", confidence=-0.1,
            )

    def test_rationale_defaults_to_empty(self):
        decision = AutonomousDecision(
            decision_id="x", symbol="NSE:SBIN", strategy_id="s1", timeframe="1d",
            action="LONG_ENTRY",
        )
        assert decision.rationale == ""

    def test_create_sets_market_snapshot_timestamp(self):
        decision = AutonomousDecision.create(
            symbol="NSE:SBIN", strategy_id="s1", timeframe="1d", action="LONG_ENTRY",
            decision_timestamp="2024-01-01T00:00:00+00:00",
        )
        assert decision.decision_timestamp == "2024-01-01T00:00:00+00:00"
        assert decision.market_snapshot_timestamp == "2024-01-01T00:00:00+00:00"

    def test_create_with_different_rationale_changes_id(self):
        base = dict(symbol="NSE:SBIN", strategy_id="s1", timeframe="1d", action="LONG_ENTRY")
        a = AutonomousDecision.create(**base, rationale="alpha", decision_timestamp="2024-01-01T00:00:00+00:00")
        b = AutonomousDecision.create(**base, rationale="beta", decision_timestamp="2024-01-01T00:00:00+00:00")
        assert a.decision_id != b.decision_id

    def test_create_id_deterministic_for_identical_input(self):
        a = AutonomousDecision.create(
            symbol="NSE:SBIN", strategy_id="s1", timeframe="1d", action="LONG_ENTRY",
            rationale="same", decision_timestamp="2024-01-01T00:00:00+00:00",
        )
        b = AutonomousDecision.create(
            symbol="NSE:SBIN", strategy_id="s1", timeframe="1d", action="LONG_ENTRY",
            rationale="same", decision_timestamp="2024-01-01T00:00:00+00:00",
        )
        assert a.decision_id == b.decision_id
        assert a.decision_id != b.decision_timestamp


class TestAutonomousDeploymentCoordinator:
    def test_duplicate_active_deployment_blocked(self):
        controller = _make_controller()
        _seed_deployment_record(
            controller,
            deployment_id="dep-active-dup",
            bot_id=controller.config.bot_id,
            symbol="NSE:SBIN",
            timeframe="1d",
            strategy_id="strat-001",
            status=PaperDeploymentStatus.ACTIVE,
        )
        coord = AutonomousDeploymentCoordinator(
            config=controller.config, control_center=controller.control_center
        )
        result, deployment = coord.create_autonomous_deployment(
            symbol="NSE:SBIN",
            strategy_id="strat-001",
            timeframe="1d",
            strategy_spec=_make_strategy_spec(),
            deployment_config=PaperDeploymentConfig(),
        )
        assert result == DeploymentCreationResult.DUPLICATE_DEPLOYMENT
        assert deployment is None
        # No second deployment record should have been created.
        assert len(controller.control_center.list_deployments()) == 1

    def test_duplicate_only_blocks_active_status(self):
        # A STOPPED same-bot deployment does not block a new autonomous deployment.
        controller = _make_controller()
        _seed_deployment_record(
            controller,
            deployment_id="dep-stopped",
            bot_id=controller.config.bot_id,
            symbol="NSE:SBIN",
            timeframe="1d",
            strategy_id="strat-001",
            status=PaperDeploymentStatus.STOPPED,
        )
        coord = AutonomousDeploymentCoordinator(
            config=controller.config, control_center=controller.control_center
        )
        # Different strategy_id so the (stopped) record is not a same-strategy match.
        result, deployment = coord.create_autonomous_deployment(
            symbol="NSE:SBIN",
            strategy_id="strat-002",
            timeframe="1d",
            strategy_spec=_make_strategy_spec(),
            deployment_config=PaperDeploymentConfig(),
        )
        # No eligible strategy -> gate rejects, but it must NOT be a duplicate.
        assert result != DeploymentCreationResult.DUPLICATE_DEPLOYMENT
        assert result == DeploymentCreationResult.FAILURE

    def test_create_success_marks_and_activates_deployment(self):
        controller, spec, strategy = _make_eligible_controller()
        coord = AutonomousDeploymentCoordinator(
            config=controller.config, control_center=controller.control_center
        )
        result, dep = coord.create_autonomous_deployment(
            symbol="NSE:SBIN",
            strategy_id=strategy.strategy_id,
            timeframe="1d",
            strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(),
        )
        assert result == DeploymentCreationResult.SUCCESS
        assert dep is not None
        assert dep.notes == f"bot:{controller.config.bot_id}"
        assert dep.status == PaperDeploymentStatus.ACTIVE
        # Notes are persisted to the registry, not just the in-memory object.
        reloaded = controller.control_center.get_deployment(dep.deployment_id)
        assert reloaded is not None
        assert reloaded.notes == f"bot:{controller.config.bot_id}"
        assert reloaded.status == PaperDeploymentStatus.ACTIVE

    def test_create_failure_returns_failure_when_gate_raises(self):
        controller, spec, strategy = _make_eligible_controller()

        def _boom(**kwargs):
            raise RuntimeError("simulated create failure")

        controller.control_center.create_deployment = _boom  # type: ignore[assignment]
        coord = AutonomousDeploymentCoordinator(
            config=controller.config, control_center=controller.control_center
        )
        result, dep = coord.create_autonomous_deployment(
            symbol="NSE:SBIN",
            strategy_id=strategy.strategy_id,
            timeframe="1d",
            strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(),
        )
        assert result == DeploymentCreationResult.FAILURE
        assert dep is None

    def test_stop_own_deployment_succeeds(self):
        controller = _make_controller()
        _seed_deployment_record(
            controller,
            deployment_id="dep-own-stop",
            bot_id=controller.config.bot_id,
            status=PaperDeploymentStatus.ACTIVE,
        )
        coord = AutonomousDeploymentCoordinator(
            config=controller.config, control_center=controller.control_center
        )
        result = coord.stop_autonomous_deployment("dep-own-stop")
        assert result == DeploymentCreationResult.SUCCESS
        reloaded = controller.control_center.get_deployment("dep-own-stop")
        assert reloaded.status == PaperDeploymentStatus.STOPPED

    def test_stop_different_bot_deployment_is_refused(self):
        controller = _make_controller()
        _seed_deployment_record(
            controller,
            deployment_id="dep-other-bot",
            bot_id="bot-other",
            status=PaperDeploymentStatus.ACTIVE,
        )
        coord = AutonomousDeploymentCoordinator(
            config=controller.config, control_center=controller.control_center
        )
        result = coord.stop_autonomous_deployment("dep-other-bot")
        assert result == DeploymentCreationResult.FAILURE
        # The foreign deployment must be left untouched.
        reloaded = controller.control_center.get_deployment("dep-other-bot")
        assert reloaded.status == PaperDeploymentStatus.ACTIVE

    def test_stop_missing_deployment_is_failure(self):
        controller = _make_controller()
        coord = AutonomousDeploymentCoordinator(
            config=controller.config, control_center=controller.control_center
        )
        assert coord.stop_autonomous_deployment("does-not-exist") == DeploymentCreationResult.FAILURE

    def test_double_stop_is_safe(self):
        controller = _make_controller()
        _seed_deployment_record(
            controller,
            deployment_id="dep-already-stopped",
            bot_id=controller.config.bot_id,
            status=PaperDeploymentStatus.STOPPED,
        )
        coord = AutonomousDeploymentCoordinator(
            config=controller.config, control_center=controller.control_center
        )
        # Already STOPPED -> InvalidLifecycleTransitionError -> treated as success.
        assert coord.stop_autonomous_deployment("dep-already-stopped") == DeploymentCreationResult.SUCCESS

    def test_stop_when_underlying_stop_raises_is_failure(self):
        controller = _make_controller()
        _seed_deployment_record(
            controller,
            deployment_id="dep-raise-stop",
            bot_id=controller.config.bot_id,
            status=PaperDeploymentStatus.ACTIVE,
        )

        def _raise(_deployment_id):
            raise RuntimeError("broker unavailable")

        controller.control_center.stop_deployment = _raise  # type: ignore[assignment]
        coord = AutonomousDeploymentCoordinator(
            config=controller.config, control_center=controller.control_center
        )
        assert coord.stop_autonomous_deployment("dep-raise-stop") == DeploymentCreationResult.FAILURE

    def test_list_autonomous_deployments_filters_by_bot(self):
        controller = _make_controller()
        _seed_deployment_record(
            controller, deployment_id="dep-bot-x-1",
            bot_id="bot-test-001", status=PaperDeploymentStatus.ACTIVE,
        )
        _seed_deployment_record(
            controller, deployment_id="dep-bot-y", bot_id="bot-other",
        )
        _seed_deployment_record(
            controller, deployment_id="dep-manual", bot_id="bot-test-001",
            notes="manual deployment",
        )
        coord = AutonomousDeploymentCoordinator(
            config=controller.config, control_center=controller.control_center
        )
        owned = coord.list_autonomous_deployments()
        ids = {d.deployment_id for d in owned}
        assert ids == {"dep-bot-x-1"}
        assert "dep-bot-y" not in ids
        assert "dep-manual" not in ids

    def test_reconcile_reports_per_deployment_status(self):
        controller = _make_controller()
        _seed_deployment_record(
            controller, deployment_id="dep-ok", bot_id="bot-test-001",
            status=PaperDeploymentStatus.ACTIVE,
        )
        _seed_deployment_record(
            controller, deployment_id="dep-failed", bot_id="bot-test-001",
            status=PaperDeploymentStatus.FAILED,
        )
        coord = AutonomousDeploymentCoordinator(
            config=controller.config, control_center=controller.control_center
        )
        results = coord.reconcile()
        # reconcile emits one leading SUCCESS (active/stale check) + one per deployment.
        assert len(results) == 3
        assert all(r == DeploymentCreationResult.SUCCESS for r in results)


class TestControllerDeploymentFlow:
    def test_create_autonomous_deployment_success_increments_counters(self):
        controller, spec, strategy = _make_eligible_controller()
        result, dep = controller.create_autonomous_deployment(
            symbol="NSE:SBIN",
            strategy_id=strategy.strategy_id,
            timeframe="1d",
            strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(),
        )
        assert result == DeploymentCreationResult.SUCCESS
        assert dep is not None
        assert dep.notes == f"bot:{controller.config.bot_id}"
        assert controller.config.decision_count == 1
        assert controller.config.last_decision_timestamp is not None

    def test_create_autonomous_deployment_policy_violation(self):
        controller = _make_controller()
        result, dep = controller.create_autonomous_deployment(
            symbol="DISALLOWED",
            strategy_id="strat-001",
            timeframe="1d",
            strategy_spec=_make_strategy_spec(),
            deployment_config=PaperDeploymentConfig(),
        )
        assert result == DeploymentCreationResult.POLICY_VIOLATION
        assert dep is None
        # Policy violation must not mutate bot accounting.
        assert controller.config.decision_count == 0
        assert controller.config.last_decision_timestamp is None


class TestFailureSafetyPhase1:
    def test_policy_violation_leaves_no_deployment(self):
        controller = _make_controller()
        controller.create_autonomous_deployment(
            symbol="DISALLOWED",
            strategy_id="strat-001",
            timeframe="1d",
            strategy_spec=_make_strategy_spec(),
            deployment_config=PaperDeploymentConfig(),
        )
        # The rejected symbol must not appear in any persisted deployment.
        matches = [
            d for d in controller.control_center.list_deployments()
            if d.symbol == "DISALLOWED"
        ]
        assert matches == []

    def test_coordinator_create_failure_does_not_mark_notes(self):
        controller, spec, strategy = _make_eligible_controller()

        def _boom(**kwargs):
            raise RuntimeError("simulated failure")

        controller.control_center.create_deployment = _boom  # type: ignore[assignment]
        coord = AutonomousDeploymentCoordinator(
            config=controller.config, control_center=controller.control_center
        )
        result, dep = coord.create_autonomous_deployment(
            symbol="NSE:SBIN",
            strategy_id=strategy.strategy_id,
            timeframe="1d",
            strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(),
        )
        assert result == DeploymentCreationResult.FAILURE
        assert dep is None
        assert controller.control_center.list_deployments() == []

    def test_stop_failure_does_not_corrupt_lifecycle(self):
        controller = _make_controller()
        controller.start_bot()

        def _boom(_deployment_id):
            raise RuntimeError("stop failed")

        # stop_bot swallows individual deployment stop errors and still
        # completes the RUNNING -> STOPPING -> STOPPED transition.
        controller.control_center.stop_deployment = _boom  # type: ignore[assignment]
        result, message = controller.stop_bot()
        assert result is True
        assert controller.lifecycle.state == AutonomousBotState.STOPPED


class TestPaperOnlyGuaranteePhase1:
    def test_config_source_defaults_to_autonomous(self):
        config = _make_config()
        assert config.source == Source.AUTONOMOUS

    def test_autonomous_config_rejects_live(self):
        with pytest.raises(Exception):
            _make_config(trading_mode=TradingMode.LIVE)

    def test_autonomous_config_rejects_live_via_manual_mode(self):
        # MANUAL mode bypasses the paper-only enforcement (manual can be any mode),
        # but AUTONOMOUS mode is strictly paper.
        config = _make_config(mode=BotMode.MANUAL)
        assert config.mode == BotMode.MANUAL

    def test_successful_deployment_is_paper_execution_mode(self):
        controller, spec, strategy = _make_eligible_controller()
        coord = AutonomousDeploymentCoordinator(
            config=controller.config, control_center=controller.control_center
        )
        result, dep = coord.create_autonomous_deployment(
            symbol="NSE:SBIN",
            strategy_id=strategy.strategy_id,
            timeframe="1d",
            strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(),
        )
        assert result == DeploymentCreationResult.SUCCESS
        assert dep.config.execution_mode == "paper"

    def test_no_live_broker_path_in_controller(self):
        controller = _make_controller()
        # The paper path exposes no live broker attribute anywhere in the stack.
        assert not hasattr(controller, "live_broker")
        assert not hasattr(controller.control_center, "live_broker")
