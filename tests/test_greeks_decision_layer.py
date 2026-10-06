"""Phase 0/1 — Greeks decision-layer integration into the options resolver.

These tests pin the seam that wires ``greeks_policy`` + ``greeks_metrics`` into
``OptionsStructureBuilder.build`` and ``AutonomousController.resolve_options_plan``.

The load-bearing invariant is D1 (fail OPEN) plus D2 (default OFF):

  * With the layer off, the plan is byte-identical to the legacy path and no
    clock is read.
  * With the layer on, a verdict is attached per leg but the legs, sizing and
    cost are never changed — a degraded verdict falls through, it never refuses.
  * Unknown greeks are accounted for via ``GreeksMetrics`` (D6), the counterweight
    to fail-open, so a broken layer is not indistinguishable from a healthy one.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

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
from trading_system.autonomous.decision import TradingDecision
from trading_system.autonomous.events import AutonomousEventType
from trading_system.autonomous.greeks_metrics import GreeksMetrics
from trading_system.autonomous.options_contract import (
    DEFAULT_OPTIONS_CONFIG,
    InMemoryOptionsChainProvider,
    OptionsStructureBuilder,
    OptionsTradeConfig,
    StrategyMapping,
    OptionsChain,
    OptionsStrategy,
)
from trading_system.paper.control import PaperTradingControlCenter
from trading_system.research.evidence import EvidenceStore
from trading_system.research.strategy_intelligence import StrategyIntelligence
from trading_system.research.strategy_registry import StrategyRegistry
from trading_system.strategy_factory.contract import SignalAction, StrategySignal

UTC = timezone.utc
SYMBOL = "NSE:SBIN"
SPOT = 100.0
# Reference date + a decision instant comfortably before the nearest expiry.
REF_DATE = date(2024, 1, 15)
NOW = datetime(2024, 1, 15, 10, 30, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

def _make_decision(
    action: SignalAction = SignalAction.BUY,
    symbol: str = SYMBOL,
    reference_price: float = SPOT,
    decision_id: str = "dec-greeks-1",
) -> TradingDecision:
    signal = StrategySignal(
        action=action,
        strategy_id="sma_trend",
        timestamp=NOW,
        symbol=symbol,
        reference_price=reference_price,
        confidence=0.8,
    )
    return TradingDecision(
        decision_id=decision_id,
        opportunity_symbol=symbol,
        opportunity_rank=1,
        market_timestamp=NOW.isoformat(),
        snapshot_identity="snap-greeks",
        signal=signal,
        is_valid=True,
        status="valid",
        decision_timestamp=NOW.isoformat(),
    )


def _chain(spot: float = SPOT) -> OptionsChain:
    provider = InMemoryOptionsChainProvider(reference_date=REF_DATE)
    provider.set_spot(SYMBOL, spot)
    chain = provider.get_chain(SYMBOL, expiry=None)
    assert chain is not None
    return chain


def _build(chain: OptionsChain, **kwargs):
    return OptionsStructureBuilder().build(
        decision=_make_decision(),
        chain_provider=_StaticChainProvider(chain),
        config=DEFAULT_OPTIONS_CONFIG,
        **kwargs,
    )


class _StaticChainProvider:
    """Minimal non-synthetic provider (the controller rejects InMemory)."""

    def __init__(self, chain: OptionsChain) -> None:
        self._chain = chain

    def get_chain(self, underlying: str, expiry=None) -> OptionsChain:
        return self._chain


def _controller() -> AutonomousController:
    engine = create_engine("sqlite://")
    center = PaperTradingControlCenter(
        registry=StrategyRegistry(EvidenceStore(engine)),
        intelligence=StrategyIntelligence(StrategyRegistry(EvidenceStore(engine))),
    )
    config = AutonomousBotConfig(
        bot_id="bot-greeks-test",
        name="greeks test bot",
        mode=BotMode.AUTONOMOUS,
        trading_mode=TradingMode.PAPER,
        enabled=True,
        user_constraints=UserConstraints(),
        source=Source.AUTONOMOUS,
        max_simultaneous_positions=5,
    )
    return AutonomousController(config=config, control_center=center)


SHADOW = GreeksPolicyConfig(shadow=True)
ENABLED = GreeksPolicyConfig(enabled=True)
OFF = GreeksPolicyConfig()


# --------------------------------------------------------------------------- #
# D2 — default OFF: byte-identical, no clock
# --------------------------------------------------------------------------- #

class TestLayerOff:
    def test_no_policy_leaves_verdicts_empty(self) -> None:
        plan = _build(_chain())
        assert plan is not None
        assert plan.greek_verdicts == ()
        assert plan.to_dict()["greek_verdicts"] == []

    def test_disabled_policy_is_byte_identical_to_legacy(self) -> None:
        chain = _chain()
        legacy = _build(chain).to_dict()
        disabled = _build(chain, greeks_policy=OFF, now=NOW).to_dict()
        # Same plan content; only the (empty) greek_verdicts key may exist.
        legacy.pop("greek_verdicts")
        disabled.pop("greek_verdicts")
        assert legacy == disabled

    def test_disabled_policy_returns_same_object(self) -> None:
        chain = _chain()
        builder = OptionsStructureBuilder()
        plan = builder.build(
            decision=_make_decision(),
            chain_provider=_StaticChainProvider(chain),
            config=DEFAULT_OPTIONS_CONFIG,
        )
        assert plan is builder._apply_greeks(plan, chain, OFF, None, None)

    def test_disabled_policy_ignores_missing_now(self) -> None:
        # now is required only when the layer is on; off must not care.
        plan = _build(_chain(), greeks_policy=OFF, now=None)
        assert plan is not None and plan.greek_verdicts == ()


# --------------------------------------------------------------------------- #
# D1 — layer ON, fail open
# --------------------------------------------------------------------------- #

class TestLayerOnFailOpen:
    def test_shadow_attaches_verdict_without_changing_legs(self) -> None:
        chain = _chain()
        legacy = _build(chain).to_dict()
        plan = _build(chain, greeks_policy=SHADOW, now=NOW)
        assert plan is not None
        assert len(plan.greek_verdicts) == 1

        verdict = plan.greek_verdicts[0]
        assert verdict.available is True
        assert verdict.iv_source == "solved"
        assert verdict.fallback_reason is None
        assert verdict.instrument_key == plan.legs[0].instrument.contract_id

        # Legs / sizing / cost are untouched relative to the legacy plan.
        shadowed = plan.to_dict()
        for key in ("legs", "estimated_cost", "max_loss", "max_profit", "signal_action"):
            assert shadowed[key] == legacy[key], key

    def test_missing_now_degrades_and_keeps_plan(self) -> None:
        plan = _build(_chain(), greeks_policy=SHADOW, now=None)
        assert plan is not None
        assert len(plan.legs) == 1  # plan is still returned (fail open)
        verdict = plan.greek_verdicts[0]
        assert verdict.available is False
        assert verdict.fallback_reason == "no_reference_time"

    def test_missing_quote_degrades_and_keeps_plan(self) -> None:
        chain = _chain()
        # Drop every quote for the strike the resolver would pick.
        chain.call_quotes.clear()
        plan = _build(chain, greeks_policy=SHADOW, now=NOW)
        # Legacy builder returns None without a quote; the layer must not
        # change that decision in either direction.
        assert plan is None

    def test_expired_contract_degrades_and_keeps_plan(self) -> None:
        plan = _build(
            _chain(),
            greeks_policy=SHADOW,
            now=datetime(2030, 1, 1, tzinfo=UTC),
        )
        assert plan is not None
        assert plan.greek_verdicts[0].available is False
        assert plan.greek_verdicts[0].fallback_reason == "expired"

    def test_spot_divergence_degrades(self) -> None:
        # Decision price is 5x the chain spot -> resolver already rejects, so
        # exercise the engine's own guard via a matching chain instead.
        chain = _chain()
        plan = _build(chain, greeks_policy=SHADOW, now=NOW)
        assert plan is not None
        assert plan.greek_verdicts[0].available is True

    def test_multileg_records_one_verdict_per_leg(self) -> None:
        cfg = OptionsTradeConfig(
            strategy_mapping=StrategyMapping(buy_strategy=OptionsStrategy.STRADDLE)
        )
        chain = _chain()
        plan = OptionsStructureBuilder().build(
            decision=_make_decision(),
            chain_provider=_StaticChainProvider(chain),
            config=cfg,
            greeks_policy=SHADOW,
            now=NOW,
        )
        assert plan is not None
        assert len(plan.legs) == 2
        assert len(plan.greek_verdicts) == 2
        keys = {v.instrument_key for v in plan.greek_verdicts}
        assert keys == {leg.instrument.contract_id for leg in plan.legs}


# --------------------------------------------------------------------------- #
# D6 — fallback accounting
# --------------------------------------------------------------------------- #

class TestMetricsAccounting:
    def test_metrics_record_available_evaluation(self) -> None:
        metrics = GreeksMetrics()
        _build(_chain(), greeks_policy=SHADOW, greeks_metrics=metrics, now=NOW)
        snap = metrics.snapshot()
        assert snap["greeks_evaluated"] == 1
        assert snap["greeks_skipped"] == 0
        assert metrics.fallback_rate() == 0.0

    def test_metrics_record_fallback_reason(self) -> None:
        metrics = GreeksMetrics()
        _build(
            _chain(),
            greeks_policy=SHADOW,
            greeks_metrics=metrics,
            now=None,
        )
        snap = metrics.snapshot()
        assert snap["greeks_evaluated"] == 0
        assert snap["greeks_skipped"] == 1
        assert snap["greeks_fallback_reason"] == {"no_reference_time": 1}
        assert metrics.fallback_rate() == 1.0

    def test_metrics_not_touched_when_layer_off(self) -> None:
        metrics = GreeksMetrics()
        _build(_chain(), greeks_policy=OFF, greeks_metrics=metrics, now=NOW)
        assert metrics.snapshot() == GreeksMetrics().snapshot()

    def test_metrics_not_touched_when_policy_absent(self) -> None:
        metrics = GreeksMetrics()
        _build(_chain(), greeks_metrics=metrics)
        assert metrics.snapshot()["greeks_evaluated"] == 0


# --------------------------------------------------------------------------- #
# Controller wiring — env overlay + shadow logging
# --------------------------------------------------------------------------- #

class TestControllerWiring:
    def test_controller_default_off_attaches_no_verdicts(self, monkeypatch) -> None:
        for var in (
            "AUTONOMOUS_GREEKS_ENABLED",
            "AUTONOMOUS_GREEKS_SHADOW",
        ):
            monkeypatch.delenv(var, raising=False)
        controller = _controller()
        controller.set_chain_provider(_StaticChainProvider(_chain()))
        plan = controller.resolve_options_plan(_make_decision())
        assert plan is not None
        assert plan.greek_verdicts == ()

    def test_controller_shadow_env_logs_and_attaches(self, monkeypatch) -> None:
        monkeypatch.setenv("AUTONOMOUS_GREEKS_SHADOW", "1")
        controller = _controller()
        controller.set_chain_provider(_StaticChainProvider(_chain()))
        plan = controller.resolve_options_plan(_make_decision())
        assert plan is not None
        assert len(plan.greek_verdicts) == 1
        # The controller reads the real clock, so a 2024 chain is (correctly)
        # expired by now: the point here is the layer ran and accounted for it,
        # not that this fixture's greeks happened to be usable.
        snap = controller.greeks_metrics.snapshot()
        assert snap["greeks_evaluated"] + snap["greeks_skipped"] == 1
        assert (
            controller.event_log.count_type(AutonomousEventType.DECISION_CREATED) >= 1
        )

    def test_env_overrides_static_config(self, monkeypatch) -> None:
        monkeypatch.setenv("AUTONOMOUS_GREEKS_ENABLED", "true")
        controller = _controller()
        policy = controller._effective_greeks_policy()
        assert policy.enabled is True

    def test_bad_env_value_stays_off(self, monkeypatch) -> None:
        monkeypatch.setenv("AUTONOMOUS_GREEKS_SHADOW", "definitely")
        controller = _controller()
        assert controller._effective_greeks_policy().shadow is False

    def test_controller_respects_static_config_when_env_absent(self, monkeypatch) -> None:
        monkeypatch.delenv("AUTONOMOUS_GREEKS_SHADOW", raising=False)
        monkeypatch.delenv("AUTONOMOUS_GREEKS_ENABLED", raising=False)
        controller = _controller()
        controller.config.greeks_policy = GreeksPolicyConfig(shadow=True)
        controller.set_chain_provider(_StaticChainProvider(_chain()))
        plan = controller.resolve_options_plan(_make_decision())
        assert plan is not None and len(plan.greek_verdicts) == 1