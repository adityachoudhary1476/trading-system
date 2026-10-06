"""Phase 2/3 — target-delta selection and risk sizing wired into the builder.

These tests pin how the Phase 2 (``select_by_target_delta``) and Phase 3
(``size_for_risk_budget``) engines are spent by ``OptionsStructureBuilder`` and
``AutonomousController.resolve_options_plan``.

The load-bearing invariants mirror Phases 0/1:

  * D2 (default OFF): with ``target_delta`` / ``risk_sizing`` unset the plan is
    byte-identical to the legacy path and no engine runs.
  * D1 (fail OPEN): an unusable selection or an unbounded size falls back to the
    legacy strike and quantity — never refuses the trade.
  * D6 (accounting): every selection and size is attributed through
    ``GreeksMetrics`` so a silently inert phase is detectable.
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
from trading_system.autonomous.greeks_metrics import GreeksMetrics
from trading_system.autonomous.greeks_sizing import BOUND_BY_MAX_CONTRACTS
from trading_system.autonomous.options_contract import (
    DEFAULT_OPTIONS_CONFIG,
    InMemoryOptionsChainProvider,
    OptionsChain,
    OptionsStructureBuilder,
    OptionsTradeConfig,
    OptionType,
)
from trading_system.paper.control import PaperTradingControlCenter
from trading_system.research.evidence import EvidenceStore
from trading_system.research.strategy_intelligence import StrategyIntelligence
from trading_system.research.strategy_registry import StrategyRegistry
from trading_system.strategy_factory.contract import SignalAction, StrategySignal

UTC = timezone.utc
SYMBOL = "NSE:SBIN"
SPOT = 100.0
REF_DATE = date(2024, 1, 15)
NOW = datetime(2024, 1, 15, 10, 30, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

def _make_decision(
    action: SignalAction = SignalAction.BUY,
    strategy_id: str = "sma_trend",
) -> TradingDecision:
    signal = StrategySignal(
        action=action,
        strategy_id=strategy_id,
        timestamp=NOW,
        symbol=SYMBOL,
        reference_price=SPOT,
        confidence=0.8,
    )
    return TradingDecision(
        decision_id="dec-phase23",
        opportunity_symbol=SYMBOL,
        opportunity_rank=1,
        market_timestamp=NOW.isoformat(),
        snapshot_identity="snap-phase23",
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


class _StaticChainProvider:
    """Minimal non-synthetic provider (the controller rejects InMemory)."""

    def __init__(self, chain: OptionsChain) -> None:
        self._chain = chain

    def get_chain(self, underlying: str, expiry=None) -> OptionsChain:
        return self._chain


def _build(chain: OptionsChain, config=None, **kwargs):
    return OptionsStructureBuilder().build(
        decision=_make_decision(),
        chain_provider=_StaticChainProvider(chain),
        config=config or DEFAULT_OPTIONS_CONFIG,
        **kwargs,
    )


def _controller() -> AutonomousController:
    engine = create_engine("sqlite://")
    center = PaperTradingControlCenter(
        registry=StrategyRegistry(EvidenceStore(engine)),
        intelligence=StrategyIntelligence(StrategyRegistry(EvidenceStore(engine))),
    )
    config = AutonomousBotConfig(
        bot_id="bot-phase23-test",
        name="phase 2/3 test bot",
        mode=BotMode.AUTONOMOUS,
        trading_mode=TradingMode.PAPER,
        enabled=True,
        user_constraints=UserConstraints(),
        source=Source.AUTONOMOUS,
        max_simultaneous_positions=5,
    )
    return AutonomousController(config=config, control_center=center)


# --------------------------------------------------------------------------- #
# D2 — default OFF
# --------------------------------------------------------------------------- #

class TestPhase2_3Off:
    def test_unset_target_delta_keeps_legacy_strike(self) -> None:
        chain = _chain()
        legacy = _build(chain)
        explicit_none = _build(chain, target_delta=None, now=NOW)
        assert legacy is not None and explicit_none is not None
        assert (
            legacy.legs[0].instrument.strike
            == explicit_none.legs[0].instrument.strike
        )

    def test_risk_sizing_off_keeps_legacy_quantity(self) -> None:
        cfg = OptionsTradeConfig(max_contracts_per_leg=7.0)
        plan = _build(_chain(), config=cfg, now=NOW)
        assert plan is not None
        assert plan.legs[0].quantity == 7.0

    def test_off_engines_touch_no_metrics(self) -> None:
        metrics = GreeksMetrics()
        cfg = OptionsTradeConfig(max_contracts_per_leg=7.0)
        _build(
            _chain(),
            config=cfg,
            greeks_metrics=metrics,
            now=NOW,
            target_delta=None,
            risk_sizing=False,
            risk_budget=1000.0,
            notional_cap=1000.0,
        )
        assert metrics.snapshot() == GreeksMetrics().snapshot()


# --------------------------------------------------------------------------- #
# Phase 2 — target-delta selection
# --------------------------------------------------------------------------- #

class TestPhase2TargetDelta:
    def test_target_delta_changes_the_strike(self) -> None:
        chain = _chain()
        legacy = _build(chain)
        selected = _build(chain, target_delta=0.5, now=NOW)
        assert legacy is not None and selected is not None
        assert legacy.legs[0].instrument.strike == 102.5  # 2% OTM
        assert selected.legs[0].instrument.strike == 100.0  # ~ATM (delta 0.5)
        assert (
            selected.legs[0].instrument.strike
            != legacy.legs[0].instrument.strike
        )

    def test_target_delta_without_reference_time_falls_back(self) -> None:
        chain = _chain()
        legacy = _build(chain)
        fallback = _build(chain, target_delta=0.5, now=None)
        assert legacy is not None and fallback is not None
        assert (
            fallback.legs[0].instrument.strike
            == legacy.legs[0].instrument.strike
        )

    def test_impossible_band_falls_back(self) -> None:
        chain = _chain()
        legacy = _build(chain)
        # A zero-width band around 0.99 admits no listed strike.
        fallback = _build(
            chain, target_delta=0.99, delta_tolerance=0.0, now=NOW
        )
        assert legacy is not None and fallback is not None
        assert (
            fallback.legs[0].instrument.strike
            == legacy.legs[0].instrument.strike
        )

    def test_selection_records_metric(self) -> None:
        metrics = GreeksMetrics()
        _build(_chain(), target_delta=0.5, now=NOW, greeks_metrics=metrics)
        assert metrics.snapshot()["strike_selected_by"] == {"target_delta": 1}

    def test_fallback_records_legacy_metric(self) -> None:
        metrics = GreeksMetrics()
        _build(_chain(), target_delta=0.5, now=None, greeks_metrics=metrics)
        assert metrics.snapshot()["strike_selected_by"] == {"legacy": 1}

    def test_put_side_selects_independently(self) -> None:
        decision = _make_decision(action=SignalAction.SELL)
        chain = _chain()
        selected = OptionsStructureBuilder().build(
            decision=decision,
            chain_provider=_StaticChainProvider(chain),
            config=DEFAULT_OPTIONS_CONFIG,
            target_delta=-0.5,
            now=NOW,
        )
        assert selected is not None
        # Sign is a magnitude; the put near leg lands near ATM.
        assert selected.legs[0].instrument.strike == 100.0
        assert selected.legs[0].instrument.option_type == OptionType.PE


# --------------------------------------------------------------------------- #
# Phase 3 — risk sizing
# --------------------------------------------------------------------------- #

class TestPhase3RiskSizing:
    def _cfg(self) -> OptionsTradeConfig:
        return OptionsTradeConfig(max_contracts_per_leg=100.0)

    def test_budget_sets_quantity(self) -> None:
        cfg = self._cfg()
        plan = _build(
            _chain(),
            config=cfg,
            risk_sizing=True,
            risk_budget=30.0,
            notional_cap=1_000_000.0,
            now=NOW,
        )
        assert plan is not None
        # premium * qty must stay within the budget (contract_multiplier=1).
        premium = plan.legs[0].price
        assert premium is not None
        assert plan.legs[0].quantity * premium <= 30.0 + premium
        assert plan.legs[0].quantity > 1.0

    def test_max_contracts_cap_binds(self) -> None:
        cfg = self._cfg()
        plan = _build(
            _chain(),
            config=cfg,
            risk_sizing=True,
            risk_budget=1_000_000.0,
            notional_cap=1_000_000.0,
            now=NOW,
        )
        assert plan is not None
        assert plan.legs[0].quantity == cfg.max_contracts_per_leg

    def test_zero_budget_fails_open_to_legacy(self) -> None:
        cfg = self._cfg()
        plan = _build(
            _chain(),
            config=cfg,
            risk_sizing=True,
            risk_budget=0.0,
            notional_cap=0.0,
            now=NOW,
        )
        assert plan is not None
        assert plan.legs[0].quantity == cfg.max_contracts_per_leg

    def test_missing_reference_time_fails_open(self) -> None:
        cfg = self._cfg()
        plan = _build(
            _chain(),
            config=cfg,
            risk_sizing=True,
            risk_budget=30.0,
            notional_cap=1_000_000.0,
            now=None,
        )
        assert plan is not None
        assert plan.legs[0].quantity == cfg.max_contracts_per_leg

    def test_sizing_metric_recorded(self) -> None:
        metrics = GreeksMetrics()
        _build(
            _chain(),
            config=self._cfg(),
            risk_sizing=True,
            risk_budget=1_000_000.0,
            notional_cap=1_000_000.0,
            now=NOW,
            greeks_metrics=metrics,
        )
        assert metrics.snapshot()["sizing_bound_by"] == {
            BOUND_BY_MAX_CONTRACTS: 1
        }

    def test_sizing_not_applied_to_multileg(self) -> None:
        from trading_system.autonomous.options_contract import (
            OptionsStrategy,
            StrategyMapping,
        )

        cfg = OptionsTradeConfig(
            max_contracts_per_leg=100.0,
            strategy_mapping=StrategyMapping(buy_strategy=OptionsStrategy.STRADDLE),
        )
        plan = _build(
            _chain(),
            config=cfg,
            risk_sizing=True,
            risk_budget=30.0,
            notional_cap=1_000_000.0,
            now=NOW,
        )
        assert plan is not None
        assert len(plan.legs) == 2
        # Multi-leg structures keep the legacy fixed quantity.
        assert all(leg.quantity == cfg.max_contracts_per_leg for leg in plan.legs)


# --------------------------------------------------------------------------- #
# Controller wiring
# --------------------------------------------------------------------------- #

class TestControllerPhaseInputs:
    def test_target_delta_by_strategy_overrides_global(self) -> None:
        controller = _controller()
        policy = GreeksPolicyConfig(
            enabled=True, target_delta=0.30, target_delta_by_strategy={"sma_trend": 0.55}
        )
        decision = _make_decision(strategy_id="sma_trend")
        assert controller._target_delta_for(decision, policy) == 0.55

    def test_target_delta_unknown_family_falls_back_to_global(self) -> None:
        controller = _controller()
        policy = GreeksPolicyConfig(
            enabled=True, target_delta=0.30, target_delta_by_strategy={"other": 0.55}
        )
        decision = _make_decision(strategy_id="sma_trend")
        assert controller._target_delta_for(decision, policy) == 0.30

    def test_risk_budget_defaults_to_position_allocation(self) -> None:
        controller = _controller()
        risk_budget, notional_cap = controller._risk_budgets(
            GreeksPolicyConfig(risk_sizing=True)
        )
        capital = controller.config.capital_allocation
        max_alloc = controller.config.max_position_allocation_pct
        assert risk_budget == capital * max_alloc
        assert notional_cap == capital * max_alloc

    def test_explicit_risk_budget_pct_wins(self) -> None:
        controller = _controller()
        risk_budget, _ = controller._risk_budgets(
            GreeksPolicyConfig(risk_sizing=True, risk_budget_pct=0.02)
        )
        assert risk_budget == controller.config.capital_allocation * 0.02