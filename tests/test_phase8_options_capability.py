"""Phase 8 — Options capability surface (Phase A) tests.

Phase A is strictly observational. These tests verify that
``GET /deployments/{deployment_id}/options-capability``:

  * Reports the deployment's actual options configuration
    (``options_enabled``, ``allowed_option_types``,
    ``max_contracts_per_trade``) verbatim — no hard-coded values.
  * Reports the controller's option-provider status accurately. The
    ``InMemoryOptionsChainProvider`` must NEVER be advertised as
    production capability.
  * Returns 404 for unknown deployments (matching the rest of the
    deployment API).
  * Never enables or executes an option order. No network calls, no
    real Upstox traffic, no synthetic data leaking into prod.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from trading_system.autonomous.options.discovery import CurrentOptionDiscoverer
from trading_system.autonomous.options_contract import InMemoryOptionsChainProvider
from trading_system.execution.paper_broker import PaperBroker
from trading_system.paper import (
    DeploymentGate,
    PaperStrategyRunner,
)
from trading_system.paper_api import PaperAPIRouter
from trading_system.paper.deployment import PaperDeploymentConfig
from trading_system.research.evidence import (
    EvidenceStore,
    EvidenceType,
    StrategyEvidence,
    StrategyStatus,
)
from trading_system.research.strategy_intelligence import (
    EvidenceFreshnessConfig,
    EvidenceRequirement,
    StrategyIntelligence,
)
from trading_system.research.strategy_lab.spec import (
    StrategySpec,
    field_operand,
    indicator_operand,
    make_condition,
)
from trading_system.research.strategy_registry import (
    StrategyRegistry,
    evidence_identity,
)


def _spec(name="PhaseA spec", symbol="NSE:SBIN"):
    return StrategySpec(
        name=name,
        description="phase a capability test",
        symbol=symbol,
        timeframe="1d",
        indicators=[{"name": "sma", "params": {"window": 5}}],
        entry=make_condition(field_operand("close"), ">", indicator_operand("sma_5")),
        generated_by="test",
    )


def _build_eligible_deployment(engine, *, options_enabled=False, allowed=None, max_contracts=None):
    store = EvidenceStore(engine)
    registry = StrategyRegistry(store)
    intelligence = StrategyIntelligence(registry)
    # Phase 21 dev defaults — relaxed so deployment creation succeeds
    # without walk-forward / validation evidence.
    gate = DeploymentGate(
        intelligence=intelligence,
        requirement=EvidenceRequirement(
            require_walk_forward=False,
            require_validation=False,
            require_recent_evidence=False,
            min_validation_trades=0,
        ),
        freshness_config=EvidenceFreshnessConfig(max_age_days=180),
    )
    spec = _spec()
    strategy = registry.register_strategy(spec)
    registry.update_strategy_status(strategy.strategy_id, StrategyStatus.WALK_FORWARD_VALIDATED)
    ds_id = "ds-phasea"
    fresh = "2024-06-01T00:00:00+00:00"
    registry.record_evidence(StrategyEvidence(
        evidence_id=evidence_identity(strategy.strategy_id, EvidenceType.RESEARCH, ds_id, {"k": 1}),
        strategy_id=strategy.strategy_id, strategy_spec_hash=strategy.spec_hash,
        evidence_type=EvidenceType.RESEARCH, dataset_id=ds_id,
        configuration_json={"k": 1},
        metrics_json={"rows": 400, "candidates": [{
            "variant_index": 0, "status": "evaluated",
            "spec_name": strategy.name, "spec_errors": [], "error": "",
            "evaluation": {"total_return": 0.10, "profit_factor": 1.5,
                            "max_drawdown": -0.05, "n_trades": 25},
            "filter_passed": True, "filter_reasons": [],
        }], "ranking": [], "notes": []},
        created_at=fresh,
    ))
    cfg = PaperDeploymentConfig()
    # Override the relevant fields AFTER default construction so that
    # the Pydantic validators run with the same shape as a real API
    # request.
    overrides = {}
    if options_enabled:
        overrides["options_enabled"] = True
    if allowed is not None:
        overrides["allowed_option_types"] = list(allowed)
    if max_contracts is not None:
        overrides["max_options_contracts_per_trade"] = int(max_contracts)
    if overrides:
        cfg = PaperDeploymentConfig(**{**cfg.model_dump(), **overrides})
    decision = gate.evaluate(
        strategy_id=strategy.strategy_id, spec=spec,
        symbol=spec.symbol, timeframe=spec.timeframe,
        dataset_id=ds_id, config=cfg,
    )
    assert decision.passed, decision.reasons
    dep = decision.deployment
    # Persist (migrations ensured by EvidenceStore.ensure_schema_current).
    store.ensure_schema_current()
    with store._Session() as s:
        s.merge(dep.as_record())
        s.commit()
    return store, registry, intelligence, gate, dep


@pytest.fixture
def engine():
    return create_engine("sqlite://")


def _router_with(engine, *, controller=None):
    """Build a PaperAPIRouter against the given engine."""
    store, registry, intelligence, gate, dep = _build_eligible_deployment(engine)
    from trading_system.paper.control import PaperTradingControlCenter
    center = PaperTradingControlCenter(
        registry=registry, intelligence=intelligence, gate=gate,
    )
    return PaperAPIRouter(center, controller=controller), dep


# --------------------------------------------------------------------------- #
# Test 1 — options disabled
# --------------------------------------------------------------------------- #
class TestOptionsDisabled:
    def test_capability_reports_disabled(self, engine):
        router, dep = _router_with(engine)
        env = router.dispatch(
            "GET", f"/deployments/{dep.deployment_id}/options-capability"
        )
        assert env.status == 200
        body = env.body
        assert body["enabled"] is False
        assert body["capable"] is False
        # No provider should be advertised as production-capable.
        for name, prov in body["providers"].items():
            assert prov["status"] in ("disabled", "not_configured", "unavailable"), (
                f"provider {name!r} unexpectedly advertised as {prov['status']!r}"
            )
            assert prov["status"] != "available"
        # Phase A explicitly never enables autonomous execution.
        assert body["autonomous_execution_active"] is False
        assert body["execution_phase"] == "single_leg_capability_surface"
        # Verbatim config values from the deployment's own config.
        assert body["allowed_option_types"] == ["CE", "PE"]
        assert body["max_contracts_per_trade"] is None


# --------------------------------------------------------------------------- #
# Test 2 — options enabled + real providers available
# --------------------------------------------------------------------------- #
class TestOptionsEnabledCapable:
    def test_capability_reports_capable_when_providers_attached(self, engine):
        # Build a minimal controller stand-in with the attributes the
        # router's capability probe reads.
        repo = SimpleNamespace()
        discoverer = CurrentOptionDiscoverer(repository=repo)
        quote_provider = SimpleNamespace(is_authenticated=True)
        controller = SimpleNamespace(
            _option_discoverer=discoverer,
            _quote_provider=quote_provider,
            _chain_provider=None,
        )
        store, registry, intelligence, gate, dep = _build_eligible_deployment(
            engine, options_enabled=True, allowed=["CE", "PE"], max_contracts=2
        )
        from trading_system.paper.control import PaperTradingControlCenter
        center = PaperTradingControlCenter(
            registry=registry, intelligence=intelligence, gate=gate,
        )
        router = PaperAPIRouter(center, controller=controller)
        env = router.dispatch(
            "GET", f"/deployments/{dep.deployment_id}/options-capability"
        )
        assert env.status == 200
        body = env.body
        assert body["enabled"] is True
        assert body["capable"] is True
        assert body["providers"]["discoverer"]["status"] == "available"
        assert body["providers"]["quote"]["status"] == "available"
        assert body["providers"]["chain"]["status"] == "disabled"
        assert body["allowed_option_types"] == ["CE", "PE"]
        assert body["max_contracts_per_trade"] == 2
        assert body["autonomous_execution_active"] is False


# --------------------------------------------------------------------------- #
# Test 3 — options enabled but discoverer unavailable
# --------------------------------------------------------------------------- #
class TestDiscovererUnavailable:
    def test_capable_false_when_no_discoverer(self, engine):
        # No discoverer attached.
        controller = SimpleNamespace(
            _option_discoverer=None,
            _quote_provider=SimpleNamespace(is_authenticated=True),
            _chain_provider=None,
        )
        store, registry, intelligence, gate, dep = _build_eligible_deployment(
            engine, options_enabled=True
        )
        from trading_system.paper.control import PaperTradingControlCenter
        center = PaperTradingControlCenter(
            registry=registry, intelligence=intelligence, gate=gate,
        )
        router = PaperAPIRouter(center, controller=controller)
        env = router.dispatch(
            "GET", f"/deployments/{dep.deployment_id}/options-capability"
        )
        assert env.status == 200
        body = env.body
        assert body["enabled"] is True
        assert body["capable"] is False
        assert body["providers"]["discoverer"]["status"] == "disabled"
        assert body["providers"]["quote"]["status"] == "available"


# --------------------------------------------------------------------------- #
# Test 4 — options enabled but quote provider unavailable
# --------------------------------------------------------------------------- #
class TestQuoteProviderUnavailable:
    def test_capable_false_when_quote_not_authenticated(self, engine):
        controller = SimpleNamespace(
            _option_discoverer=CurrentOptionDiscoverer(repository=SimpleNamespace()),
            _quote_provider=SimpleNamespace(is_authenticated=False),
            _chain_provider=None,
        )
        store, registry, intelligence, gate, dep = _build_eligible_deployment(
            engine, options_enabled=True
        )
        from trading_system.paper.control import PaperTradingControlCenter
        center = PaperTradingControlCenter(
            registry=registry, intelligence=intelligence, gate=gate,
        )
        router = PaperAPIRouter(center, controller=controller)
        env = router.dispatch(
            "GET", f"/deployments/{dep.deployment_id}/options-capability"
        )
        assert env.status == 200
        body = env.body
        assert body["enabled"] is True
        assert body["capable"] is False
        assert body["providers"]["discoverer"]["status"] == "available"
        assert body["providers"]["quote"]["status"] == "unavailable"

    def test_capable_false_when_quote_provider_missing(self, engine):
        controller = SimpleNamespace(
            _option_discoverer=CurrentOptionDiscoverer(repository=SimpleNamespace()),
            _quote_provider=None,
            _chain_provider=None,
        )
        store, registry, intelligence, gate, dep = _build_eligible_deployment(
            engine, options_enabled=True
        )
        from trading_system.paper.control import PaperTradingControlCenter
        center = PaperTradingControlCenter(
            registry=registry, intelligence=intelligence, gate=gate,
        )
        router = PaperAPIRouter(center, controller=controller)
        env = router.dispatch(
            "GET", f"/deployments/{dep.deployment_id}/options-capability"
        )
        assert env.status == 200
        body = env.body
        assert body["enabled"] is True
        assert body["capable"] is False
        assert body["providers"]["quote"]["status"] == "disabled"


# --------------------------------------------------------------------------- #
# Test 5 — synthetic chain provider is NOT advertised as production capability
# --------------------------------------------------------------------------- #
class TestSyntheticChainProviderRejected:
    def test_inmemory_chain_reported_as_unavailable(self, engine):
        controller = SimpleNamespace(
            _option_discoverer=CurrentOptionDiscoverer(repository=SimpleNamespace()),
            _quote_provider=SimpleNamespace(is_authenticated=True),
            _chain_provider=InMemoryOptionsChainProvider(),
        )
        store, registry, intelligence, gate, dep = _build_eligible_deployment(
            engine, options_enabled=True
        )
        from trading_system.paper.control import PaperTradingControlCenter
        center = PaperTradingControlCenter(
            registry=registry, intelligence=intelligence, gate=gate,
        )
        router = PaperAPIRouter(center, controller=controller)
        env = router.dispatch(
            "GET", f"/deployments/{dep.deployment_id}/options-capability"
        )
        assert env.status == 200
        body = env.body
        # The synthetic provider must never be reported as "available".
        assert body["providers"]["chain"]["status"] == "unavailable"
        # Discoverer + quote are still available, so capable stays True
        # for the single-leg surface. But the synthetic chain incident
        # is surfaced via last_error so the UI can warn the operator.
        assert body["last_error"] is not None
        assert "synthetic" in body["last_error"].lower()


# --------------------------------------------------------------------------- #
# Test 6 — deployment not found
# --------------------------------------------------------------------------- #
class TestDeploymentNotFound:
    def test_unknown_deployment_returns_404(self, engine):
        router, _dep = _router_with(engine)
        env = router.dispatch("GET", "/deployments/does-not-exist/options-capability")
        assert env.status == 404
        assert env.body["error"]["code"] == "unknown_deployment"


# --------------------------------------------------------------------------- #
# Test 7 — config values are returned verbatim
# --------------------------------------------------------------------------- #
class TestConfiguredOptionTypes:
    def test_returns_deployment_actual_config(self, engine):
        # CE-only with 7 contracts/trade.
        store, registry, intelligence, gate, dep = _build_eligible_deployment(
            engine,
            options_enabled=True,
            allowed=["CE"],
            max_contracts=7,
        )
        from trading_system.paper.control import PaperTradingControlCenter
        center = PaperTradingControlCenter(
            registry=registry, intelligence=intelligence, gate=gate,
        )
        router = PaperAPIRouter(center)
        env = router.dispatch(
            "GET", f"/deployments/{dep.deployment_id}/options-capability"
        )
        assert env.status == 200
        body = env.body
        assert body["allowed_option_types"] == ["CE"]
        assert body["max_contracts_per_trade"] == 7
        # Verify it is NOT silently coerced to the default.
        assert body["allowed_option_types"] != ["CE", "PE"]


# --------------------------------------------------------------------------- #
# Test 8 — capability probe never executes an order
# --------------------------------------------------------------------------- #
class TestNoOrderExecution:
    def test_capability_probe_does_not_create_positions(self, engine):
        store, registry, intelligence, gate, dep = _build_eligible_deployment(
            engine, options_enabled=True, allowed=["CE", "PE"], max_contracts=1
        )
        # Attach a runner so we can inspect post-probe state.
        from trading_system.paper.control import PaperTradingControlCenter
        center = PaperTradingControlCenter(
            registry=registry, intelligence=intelligence, gate=gate,
        )
        # Activate and attach a runner.
        center.activate_deployment(dep.deployment_id)
        broker = PaperBroker(initial_cash=100_000.0)
        runner = PaperStrategyRunner(
            deployment=dep,
            broker=broker,
            spec=_spec(),
        )
        sid = center.attach_runner(dep.deployment_id, runner)
        positions_before = broker.positions()
        cash_before = broker.account().cash

        # Build a controller with everything available.
        controller = SimpleNamespace(
            _option_discoverer=CurrentOptionDiscoverer(repository=SimpleNamespace()),
            _quote_provider=SimpleNamespace(is_authenticated=True),
            _chain_provider=None,
        )
        router = PaperAPIRouter(center, controller=controller)
        env = router.dispatch(
            "GET", f"/deployments/{dep.deployment_id}/options-capability"
        )
        assert env.status == 200
        assert env.body["capable"] is True

        # No order was placed; nothing changed in the broker.
        assert broker.positions() == positions_before
        assert broker.account().cash == cash_before
        assert sid is not None
        # Order count is unchanged.
        assert len(broker._orders) == 0

    def test_method_not_allowed_on_post(self, engine):
        router, dep = _router_with(engine)
        env = router.dispatch(
            "POST", f"/deployments/{dep.deployment_id}/options-capability"
        )
        assert env.status == 405


# --------------------------------------------------------------------------- #
# Test 9 — autonomous_execution_active is always False in Phase A
# --------------------------------------------------------------------------- #
class TestAutonomousExecutionAlwaysFalse:
    def test_never_reports_autonomous_execution_active(self, engine):
        # Fully wired, options enabled, capable = True.
        controller = SimpleNamespace(
            _option_discoverer=CurrentOptionDiscoverer(repository=SimpleNamespace()),
            _quote_provider=SimpleNamespace(is_authenticated=True),
            _chain_provider=None,
        )
        store, registry, intelligence, gate, dep = _build_eligible_deployment(
            engine, options_enabled=True
        )
        from trading_system.paper.control import PaperTradingControlCenter
        center = PaperTradingControlCenter(
            registry=registry, intelligence=intelligence, gate=gate,
        )
        router = PaperAPIRouter(center, controller=controller)
        env = router.dispatch(
            "GET", f"/deployments/{dep.deployment_id}/options-capability"
        )
        assert env.status == 200
        assert env.body["autonomous_execution_active"] is False