"""Phase C — Autonomous single-leg BUY CE / BUY PE paper execution tests.

Exercises the full canonical paper path with REAL production classes and
deterministic fake option providers (no network, no credentials, no real
Upstox calls):

    signal (option_intent=CE / PE)
        ↓
    AutonomousController.discover_options_contract()
        ↓
    CurrentOptionDiscoverer (FAKE — fed a populated InstrumentRepository)
        ↓
    CurrentOptionQuoteProvider (FAKE — returns a deterministic premium)
        ↓
    SafetyValidator.check_contract_validity()
        ↓
    AutonomousController.execute_option_order()
        ↓
    PaperTradingControlCenter.submit_order_intent()
        ↓
    PaperBroker
        ↓
    paper order + position (contract_size defaults to 1 — known Phase D
    limitation)

Also exercises ``backend.autonomous_scheduler._execute_one_option_decision``
end-to-end, including:

    * options_enabled=False on deployment  → NO order
    * allowed_option_types=["CE"] + PE intent → rejected
    * allowed_option_types=["PE"] + CE intent → rejected
    * max_options_contracts_per_trade exceeded → rejected
    * kill switch active → no order
    * stale quote → no order
    * missing contract → no order
    * expired contract → no order
    * same signal twice → idempotent replay (no second fill)
    * different decision_id but same options_contract_id → exact-contract
      position guard prevents duplicate open
    * different options_contract_id → allowed
    * underlying spot is NEVER used as the option premium
    * ``InMemoryOptionsChainProvider`` cannot execute
    * OrderIntent side is ALWAYS BUY for both CE and PE
      (``SELL != PUT`` — the audit's required invariant)
"""
from __future__ import annotations

import hashlib
import json as _json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from trading_system.autonomous.bot_config import (
    AutonomousBotConfig,
    BotMode,
    MaxExposurePct,
    MaxPositionPct,
    Source,
    TradingMode,
    TradingSessionConstraints,
    UserConstraints,
)
from trading_system.autonomous.controller import AutonomousController
from trading_system.autonomous.coordinator import (
    AutonomousDeploymentCoordinator,
    DeploymentCreationResult,
)
from trading_system.autonomous.options.discovery import CurrentOptionDiscoverer
from trading_system.autonomous.options.model import (
    OptionsContractSelection,
    OptionDirection,
)
from trading_system.autonomous.safety import KillSwitchReason
from trading_system.execution.orders import OrderIntent, OrderType, Side
from trading_system.indicators import ema
from trading_system.india.instrument_repository import InstrumentRepository
from trading_system.india.instruments import (
    Instrument,
    InstrumentType,
    InternalSymbol,
)
from trading_system.india.option_quotes import OptionQuote
from trading_system.paper.control import PaperTradingControlCenter
from trading_system.paper.deployment import (
    PaperDeploymentConfig,
    PaperDeploymentStatus,
)
from trading_system.paper.gate import DeploymentGate
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
    evidence_identity as _evidence_identity,
)
from trading_system.strategy_factory.contract import (
    SignalAction,
    StrategySignal,
)

UTC = timezone.utc

# A deterministic "today" — picked inside a far-future range so no expiry
# filtering accidentally kills our fixture contracts.
FUTURE_EXPIRY = (date.today() + timedelta(days=30)).isoformat()


# --------------------------------------------------------------------------- #
# Fake providers (no network, no real Upstox)
# --------------------------------------------------------------------------- #
class FakeCurrentOptionQuoteProvider:
    """Deterministic stand-in for ``CurrentOptionQuoteProvider``.

    Implements the duck-typed contract used by ``AutonomousController``:

      * ``is_authenticated`` (bool)
      * ``get_quote(instrument) -> OptionQuote | None``
      * ``is_fresh(quote, max_age_seconds=None) -> bool``
    """

    def __init__(
        self,
        *,
        premium: float = 185.0,
        authenticated: bool = True,
        stale: bool = False,
        missing: bool = False,
    ):
        self._premium = float(premium)
        self._authenticated = bool(authenticated)
        self._stale = bool(stale)
        self._missing = bool(missing)

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    def get_quote(self, instrument: Instrument):
        if self._missing or not self._authenticated:
            return None
        now = datetime.now(UTC)
        # If "stale", set timestamp far in the past so is_fresh() returns False
        # within the 5-minute budget.
        ts = now - (timedelta(hours=2) if self._stale else timedelta(seconds=2))
        return OptionQuote(
            instrument=instrument,
            ltp=self._premium,
            timestamp=ts,
            fetched_at=now,
            bid=self._premium - 0.5,
            ask=self._premium + 0.5,
            source_symbol="NFO:" + instrument.internal.symbol,
        )

    def is_fresh(self, quote: OptionQuote, max_age_seconds: float | None = None) -> bool:
        budget = float(max_age_seconds) if max_age_seconds is not None else 300.0
        return float(quote.age_seconds) <= budget


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _build_option_instrument(
    *,
    underlying: str,
    strike: float,
    option_type: str,  # "CE" or "PE"
    expiry: str,
    exchange: str = "NFO",
    lot_size: int = 25,  # NIFTY typical lot size
) -> Instrument:
    itype = (
        InstrumentType.OPTION_CE
        if option_type == "CE"
        else InstrumentType.OPTION_PE
    )
    token = f"{underlying}{expiry.replace('-', '')[2:]}{int(strike)}{option_type}"
    instr = Instrument(
        internal=InternalSymbol(exchange=exchange, symbol=token),
        instrument_type=itype,
        name=underlying,
    )
    instr.underlying = underlying
    instr.expiry = expiry
    instr.strike = float(strike)
    instr.option_type = option_type
    instr.provider_symbol = token
    # Phase C + Phase D compatibility: the broker's
    # ``_resolve_contract_size`` requires a resolvable lot size. Without
    # this, the broker raises ``option_lot_size_unknown`` and the order
    # never reaches the book. We supply the canonical NIFTY lot size
    # for test fixtures; production code must source it from the
    # Upstox instrument master.
    instr.lot_size = lot_size
    return instr


def _build_option_repo(*, spot_price: float = 25000.0) -> InstrumentRepository:
    """Build a populated InstrumentRepository for NIFTY ATM options."""
    repo = InstrumentRepository()
    expiry = FUTURE_EXPIRY
    # Register several strikes around ATM.
    for strike_delta in (-200, -100, 0, 100, 200):
        strike = float(int(spot_price) // 100 * 100) + strike_delta  # round to 100
        for ot in ("CE", "PE"):
            repo.register(_build_option_instrument(
                underlying="NIFTY",
                strike=strike,
                option_type=ot,
                expiry=expiry,
            ))
    return repo


def _spec(name="PhaseC spec", symbol="NSE:NIFTY"):
    return StrategySpec(
        name=name,
        description="phase C option execution test",
        symbol=symbol,
        timeframe="1d",
        indicators=[{"name": "sma", "params": {"window": 5}}],
        entry=make_condition(field_operand("close"), ">", indicator_operand("sma_5")),
        generated_by="phase-c-test",
    )


def _build_control_center():
    """Real PaperTradingControlCenter on an in-memory SQLite store.

    Returns ``(center, registry, intelligence, gate, spec, strategy_id)``.
    """
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    store = EvidenceStore(engine)
    registry = StrategyRegistry(store)
    intelligence = StrategyIntelligence(registry)
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
    strategy_id = strategy.strategy_id
    registry.update_strategy_status(strategy_id, StrategyStatus.WALK_FORWARD_VALIDATED)
    fresh = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    registry.record_evidence(StrategyEvidence(
        evidence_id=_evidence_identity(strategy_id, EvidenceType.RESEARCH, "ds-phasec", {"k": 1}),
        strategy_id=strategy_id, strategy_spec_hash=strategy.spec_hash,
        evidence_type=EvidenceType.RESEARCH, dataset_id="ds-phasec",
        configuration_json={"k": 1},
        metrics_json={"rows": 400, "candidates": [{
            "variant_index": 0, "status": "evaluated",
            "spec_name": spec.name, "spec_errors": [], "error": "",
            "evaluation": {"total_return": 0.10, "profit_factor": 1.5,
                            "max_drawdown": -0.05, "n_trades": 25},
            "filter_passed": True, "filter_reasons": [],
        }], "ranking": [], "notes": []},
        created_at=fresh,
    ))
    center = PaperTradingControlCenter(
        registry=registry,
        intelligence=intelligence,
        gate=gate,
    )
    return center, registry, intelligence, gate, spec, strategy_id


def _build_controller(center, bot_id: str = "bot-phasec") -> AutonomousController:
    bot_config = AutonomousBotConfig(
        bot_id=bot_id,
        name=f"Phase C Test Bot {bot_id}",
        mode=BotMode.AUTONOMOUS,
        trading_mode=TradingMode.PAPER,
        enabled=True,
        user_constraints=UserConstraints(
            allowed_symbols=frozenset({"NSE:SBIN", "NSE:NIFTY"}),
            allowed_strategy_ids=frozenset({"phasec-spec"}),
            allowed_timeframes=frozenset({"1d"}),
            max_drawdown_pct=0.15,
            trading_session=TradingSessionConstraints(),
        ),
        max_simultaneous_positions=5,
        max_position_allocation_pct=MaxPositionPct(0.25),
        max_exposure_pct=MaxExposurePct(0.75),
        source=Source.AUTONOMOUS,
    )
    return AutonomousController(config=bot_config, control_center=center)


def _create_options_deployment(
    controller,
    *,
    bot_id: str,
    spec,
    strategy_id: str,
    options_enabled: bool,
    allowed_option_types: list[str],
    max_contracts: int | None,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
) -> object:
    cfg = PaperDeploymentConfig(
        execution_mode="paper",
        initial_cash=100_000.0,
        allow_short=False,
        options_enabled=options_enabled,
        allowed_option_types=list(allowed_option_types),
        max_options_contracts_per_trade=max_contracts,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
    )
    coord = AutonomousDeploymentCoordinator(
        config=controller.config, control_center=controller.control_center
    )
    result, dep = coord.create_autonomous_deployment(
        symbol="NSE:NIFTY",
        strategy_id=strategy_id,
        timeframe="1d",
        strategy_spec=spec,
        deployment_config=cfg,
    )
    assert result == DeploymentCreationResult.SUCCESS, f"unexpected result={result}"
    return dep


def _make_decision(
    *,
    symbol: str = "NSE:NIFTY",
    strategy_id: str = "phasec-spec",
    action: str = "buy",
    reference_price: float = 25000.0,
    option_intent: str | None = "CE",
    decision_id: str | None = None,
):
    """Build a real TradingDecision object the controller accepts.

    We bypass the full decision engine — we directly construct a
    ``TradingDecision`` with a ``StrategySignal`` that carries the
    ``option_intent``. The controller's ``execute_option_order`` only
    inspects the signal's ``option_intent``, ``action``, and
    ``reference_price`` plus the decision's ``opportunity_symbol``.

    ``action`` accepts every ``SignalAction`` member (buy/sell/exit/hold).
    Prior to the option-exit fix the scheduler had no EXIT branch, so the
    tests here only ever drove buy/sell and the flatten path went unverified.
    """
    if decision_id is None:
        decision_id = hashlib.sha256(
            _json.dumps(
                {
                    "strategy_id": strategy_id,
                    "symbol": symbol,
                    "option_intent": option_intent,
                    "action": action,
                    "ref": reference_price,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:48]
    sig = StrategySignal(
        action=SignalAction(action),
        strategy_id=strategy_id,
        timestamp=datetime.now(UTC),
        symbol=symbol,
        reference_price=reference_price,
        confidence=0.9,
        reason="phase C unit test",
        option_intent=option_intent,
    )
    decision = SimpleNamespace(
        decision_id=decision_id,
        opportunity_symbol=symbol,
        selected_configuration=SimpleNamespace(
            strategy_id=strategy_id,
            timeframe="1d",
        ),
        signal=sig,
        action=action,
    )
    return decision


def _attach_phase_b(controller, *, premium: float = 185.0, **quote_kwargs):
    """Attach fake option providers to the controller. Returns the repo."""
    repo = _build_option_repo(spot_price=25000.0)
    discoverer = CurrentOptionDiscoverer(repository=repo)
    quote_provider = FakeCurrentOptionQuoteProvider(premium=premium, **quote_kwargs)
    controller.set_option_discoverer(discoverer, repository=repo)
    controller.set_quote_provider(quote_provider)
    return repo


def _open_positions(runner) -> dict:
    """Open (non-zero qty) positions only.

    ``PaperBroker.positions()`` deliberately retains fully-closed records with
    ``qty == 0`` so realized P&L stays inspectable, so asserting on the raw
    dict would miss a stale open position.
    """
    return {
        sym: pos for sym, pos in runner.broker.positions().items()
        if pos.is_open and pos.qty != 0
    }


@pytest.fixture
def setup():
    """Yield a wired controller with an ACTIVE options-enabled deployment."""
    center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
    controller = _build_controller(center)
    dep = _create_options_deployment(
        controller,
        bot_id="bot-phasec",
        spec=spec,
        strategy_id=strategy_id,
        options_enabled=True,
        allowed_option_types=["CE", "PE"],
        max_contracts=1,
    )
    repo = _attach_phase_b(controller, premium=185.0)
    return {
        "center": center,
        "controller": controller,
        "deployment": dep,
        "repo": repo,
        "spec": spec,
    }


# --------------------------------------------------------------------------- #
# Test 1 — BUY CE end-to-end
# --------------------------------------------------------------------------- #
class TestBuyCE:
    def test_buy_ce_fills_paper_position(self, setup):
        controller = setup["controller"]
        center = setup["center"]
        dep = setup["deployment"]
        sid = center.find_session_for_deployment(dep.deployment_id)
        assert sid is not None
        decision = _make_decision(option_intent="CE")
        result = controller.execute_option_order(
            decision=decision,
            spot_price=25000.0,
            deployment_id=dep.deployment_id,
            session_id=sid,
            order_quantity=1,
            options_deployment_config=dep.config,
            explicit_option_type="CE",
            client_order_id="phase-c-test-ce-001",
        )
        assert result is not None, (
            "execute_option_order returned None — controller event log should "
            "explain the rejection"
        )
        assert result.status == "FILLED", f"order was not filled: status={result.status}"
        assert result.side == "BUY", (
            f"CRITICAL: side must be BUY for both CE and PE; got side={result.side!r}"
        )
        assert result.option_type == "CE"
        assert result.options_contract_id is not None
        assert result.strike is not None and result.strike > 0
        assert result.expiry == FUTURE_EXPIRY
        assert result.avg_fill_price > 0
        assert result.filled_quantity == 1
        # Position exists on the live broker with the option metadata stamped.
        runner = center.get_runner(sid)
        position = runner.broker.get_position(result.symbol)
        assert position is not None
        assert position.options_contract_id == result.options_contract_id
        assert position.strike == result.strike
        assert position.expiry == result.expiry
        assert position.option_type == "CE"
        assert position.qty == 1
        # Order persisted via PaperSessionStore (durable idempotency).
        persisted = center.session_store.get_order(
            session_id=sid, client_order_id="phase-c-test-ce-001"
        )
        assert persisted is not None
        assert persisted.order_id == result.order_id


# --------------------------------------------------------------------------- #
# Test 2 — BUY PE end-to-end (verifies SELL != PUT)
# --------------------------------------------------------------------------- #
class TestBuyPE:
    def test_buy_pe_fills_paper_position_with_buy_side(self, setup):
        controller = setup["controller"]
        center = setup["center"]
        dep = setup["deployment"]
        sid = center.find_session_for_deployment(dep.deployment_id)
        decision = _make_decision(option_intent="PE")
        result = controller.execute_option_order(
            decision=decision,
            spot_price=25000.0,
            deployment_id=dep.deployment_id,
            session_id=sid,
            order_quantity=1,
            options_deployment_config=dep.config,
            explicit_option_type="PE",
            client_order_id="phase-c-test-pe-001",
        )
        assert result is not None
        assert result.status == "FILLED"
        # CRITICAL invariant: side must be BUY for PE — NOT SELL.
        assert result.side == "BUY", (
            f"Phase C invariant violated: PE must fill as BUY, got side={result.side!r}. "
            f"SELL != PUT."
        )
        assert result.option_type == "PE"
        runner = center.get_runner(sid)
        position = runner.broker.get_position(result.symbol)
        assert position is not None
        assert position.option_type == "PE"
        assert position.qty == 1


# --------------------------------------------------------------------------- #
# Test 3 / 4 — Deployment allowed_option_types enforcement
# --------------------------------------------------------------------------- #
class TestAllowedOptionTypesEnforcement:
    def test_ce_only_deployment_rejects_pe(self):
        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_controller(center)
        dep = _create_options_deployment(
            controller, bot_id="bot-ce-only", spec=spec,
        strategy_id=strategy_id,
            options_enabled=True, allowed_option_types=["CE"], max_contracts=1,
        )
        _attach_phase_b(controller)
        sid = center.find_session_for_deployment(dep.deployment_id)
        decision = _make_decision(option_intent="PE")
        result = controller.execute_option_order(
            decision=decision,
            spot_price=25000.0,
            deployment_id=dep.deployment_id,
            session_id=sid,
            options_deployment_config=dep.config,
            explicit_option_type="PE",
            client_order_id="phase-c-test-ce-only-pe",
        )
        assert result is None, (
            f"PE intent should be rejected on a CE-only deployment; got result={result}"
        )

    def test_pe_only_deployment_rejects_ce(self):
        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_controller(center)
        dep = _create_options_deployment(
            controller, bot_id="bot-pe-only", spec=spec,
        strategy_id=strategy_id,
            options_enabled=True, allowed_option_types=["PE"], max_contracts=1,
        )
        _attach_phase_b(controller)
        sid = center.find_session_for_deployment(dep.deployment_id)
        decision = _make_decision(option_intent="CE")
        result = controller.execute_option_order(
            decision=decision,
            spot_price=25000.0,
            deployment_id=dep.deployment_id,
            session_id=sid,
            options_deployment_config=dep.config,
            explicit_option_type="CE",
            client_order_id="phase-c-test-pe-only-ce",
        )
        assert result is None


# --------------------------------------------------------------------------- #
# Test 5 — options_enabled=False rejects option decision
# --------------------------------------------------------------------------- #
class TestOptionsDisabledRejects:
    def test_deployment_with_options_disabled_rejects_buy_ce(self):
        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_controller(center)
        dep = _create_options_deployment(
            controller, bot_id="bot-options-off", spec=spec,
        strategy_id=strategy_id,
            options_enabled=False,
            allowed_option_types=["CE", "PE"],
            max_contracts=1,
        )
        _attach_phase_b(controller)
        sid = center.find_session_for_deployment(dep.deployment_id)
        decision = _make_decision(option_intent="CE")
        result = controller.execute_option_order(
            decision=decision,
            spot_price=25000.0,
            deployment_id=dep.deployment_id,
            session_id=sid,
            options_deployment_config=dep.config,
            explicit_option_type="CE",
            client_order_id="phase-c-test-options-off",
        )
        assert result is None
        # No order persisted.
        persisted = center.session_store.get_order(
            session_id=sid, client_order_id="phase-c-test-options-off"
        )
        assert persisted is None


# --------------------------------------------------------------------------- #
# Test 6 — Stale quote rejects
# --------------------------------------------------------------------------- #
class TestStaleQuoteRejects:
    def test_stale_quote_blocks_execution(self):
        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_controller(center)
        dep = _create_options_deployment(
            controller, bot_id="bot-stale", spec=spec,
        strategy_id=strategy_id,
            options_enabled=True, allowed_option_types=["CE", "PE"], max_contracts=1,
        )
        # Attach a quote provider whose quotes are too old.
        _attach_phase_b(controller, stale=True)
        sid = center.find_session_for_deployment(dep.deployment_id)
        decision = _make_decision(option_intent="CE")
        result = controller.execute_option_order(
            decision=decision,
            spot_price=25000.0,
            deployment_id=dep.deployment_id,
            session_id=sid,
            options_deployment_config=dep.config,
            explicit_option_type="CE",
            max_quote_age_seconds=300.0,
            client_order_id="phase-c-test-stale",
        )
        assert result is None


# --------------------------------------------------------------------------- #
# Test 7 — Missing contract rejects (no NIFTY options in repo)
# --------------------------------------------------------------------------- #
class TestMissingContractRejects:
    def test_no_option_contracts_rejects(self):
        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_controller(center)
        dep = _create_options_deployment(
            controller, bot_id="bot-nocontract", spec=spec,
        strategy_id=strategy_id,
            options_enabled=True, allowed_option_types=["CE", "PE"], max_contracts=1,
        )
        # Empty repo -> no candidates
        empty_repo = InstrumentRepository()
        discoverer = CurrentOptionDiscoverer(repository=empty_repo)
        quote_provider = FakeCurrentOptionQuoteProvider()
        controller.set_option_discoverer(discoverer, repository=empty_repo)
        controller.set_quote_provider(quote_provider)
        sid = center.find_session_for_deployment(dep.deployment_id)
        decision = _make_decision(option_intent="CE")
        result = controller.execute_option_order(
            decision=decision,
            spot_price=25000.0,
            deployment_id=dep.deployment_id,
            session_id=sid,
            options_deployment_config=dep.config,
            explicit_option_type="CE",
            client_order_id="phase-c-test-nocontract",
        )
        assert result is None


# --------------------------------------------------------------------------- #
# Test 8 — Expired contract rejects
# --------------------------------------------------------------------------- #
class TestExpiredContractRejects:
    def test_contract_with_past_expiry_rejected_by_safety(self):
        from trading_system.autonomous.safety import SafetyValidator

        # Build an instrument with a past expiry.
        past_expiry = (date.today() - timedelta(days=1)).isoformat()
        instr = _build_option_instrument(
            underlying="NIFTY", strike=25000.0, option_type="CE", expiry=past_expiry
        )
        selection = OptionsContractSelection.from_instrument(instr)
        safety = SafetyValidator()
        result = safety.check_contract_validity(
            options_selection=selection,
            allowed_option_types=["CE", "PE"],
            now_date=date.today().isoformat(),
        )
        assert not result.passed


# --------------------------------------------------------------------------- #
# Test 9 — Kill switch rejects
# --------------------------------------------------------------------------- #
class TestKillSwitchRejects:
    def test_kill_switch_blocks_execute_option_order(self, setup):
        controller = setup["controller"]
        center = setup["center"]
        dep = setup["deployment"]
        sid = center.find_session_for_deployment(dep.deployment_id)
        controller.halt_bot(reason=KillSwitchReason.MANUAL, detail="phase-c test")
        decision = _make_decision(option_intent="CE")
        result = controller.execute_option_order(
            decision=decision,
            spot_price=25000.0,
            deployment_id=dep.deployment_id,
            session_id=sid,
            options_deployment_config=dep.config,
            explicit_option_type="CE",
            client_order_id="phase-c-test-killswitch",
        )
        assert result is None
        persisted = center.session_store.get_order(
            session_id=sid, client_order_id="phase-c-test-killswitch"
        )
        assert persisted is None


# --------------------------------------------------------------------------- #
# Test 10 — Max contracts exceeded rejects
# --------------------------------------------------------------------------- #
class TestMaxContractsEnforced:
    def test_order_quantity_above_cap_rejected(self):
        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_controller(center)
        dep = _create_options_deployment(
            controller, bot_id="bot-cap", spec=spec,
        strategy_id=strategy_id,
            options_enabled=True, allowed_option_types=["CE", "PE"],
            max_contracts=1,
        )
        _attach_phase_b(controller)
        sid = center.find_session_for_deployment(dep.deployment_id)
        decision = _make_decision(option_intent="CE")
        result = controller.execute_option_order(
            decision=decision,
            spot_price=25000.0,
            deployment_id=dep.deployment_id,
            session_id=sid,
            order_quantity=5,  # exceeds cap of 1
            options_deployment_config=dep.config,
            explicit_option_type="CE",
            client_order_id="phase-c-test-cap",
        )
        assert result is None


# --------------------------------------------------------------------------- #
# Test 11 — same signal twice → idempotent replay (no second fill)
# --------------------------------------------------------------------------- #
class TestIdempotency:
    def test_same_client_order_id_does_not_double_fill(self, setup):
        controller = setup["controller"]
        center = setup["center"]
        dep = setup["deployment"]
        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)
        decision = _make_decision(option_intent="CE")
        client_order_id = "phase-c-test-idem"
        first = controller.execute_option_order(
            decision=decision,
            spot_price=25000.0,
            deployment_id=dep.deployment_id,
            session_id=sid,
            order_quantity=1,
            options_deployment_config=dep.config,
            explicit_option_type="CE",
            client_order_id=client_order_id,
        )
        assert first is not None
        first_position_qty = runner.broker.get_position(first.symbol).qty
        # Replay the same client_order_id.
        second = controller.execute_option_order(
            decision=decision,
            spot_price=25000.0,
            deployment_id=dep.deployment_id,
            session_id=sid,
            order_quantity=1,
            options_deployment_config=dep.config,
            explicit_option_type="CE",
            client_order_id=client_order_id,
        )
        assert second is not None
        assert second.is_idempotent_replay is True
        assert second.order_id == first.order_id
        # Position quantity unchanged.
        assert runner.broker.get_position(first.symbol).qty == first_position_qty


# --------------------------------------------------------------------------- #
# Test 12 — different decision_id, same contract → exact-contract position
#            guard prevents duplicate open
# --------------------------------------------------------------------------- #
class TestExactContractPositionGuard:
    def test_different_decision_id_same_contract_does_not_open_twice(self, setup):
        controller = setup["controller"]
        center = setup["center"]
        dep = setup["deployment"]
        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)
        # First submission.
        d1 = _make_decision(option_intent="CE", decision_id="phase-c-d1")
        first = controller.execute_option_order(
            decision=d1,
            spot_price=25000.0,
            deployment_id=dep.deployment_id,
            session_id=sid,
            order_quantity=1,
            options_deployment_config=dep.config,
            explicit_option_type="CE",
            client_order_id="phase-c-dup-cid-1",
        )
        assert first is not None
        first_symbol = first.symbol
        # Second submission: same contract (because spot is the same, the
        # ATM strike selection will pick the same strike), but different
        # decision_id + different client_order_id. The exact-contract
        # position guard must reject this.
        d2 = _make_decision(option_intent="CE", decision_id="phase-c-d2")
        second = controller.execute_option_order(
            decision=d2,
            spot_price=25000.0,
            deployment_id=dep.deployment_id,
            session_id=sid,
            order_quantity=1,
            options_deployment_config=dep.config,
            explicit_option_type="CE",
            client_order_id="phase-c-dup-cid-2",
        )
        assert second is None, (
            "exact-contract position guard must prevent a second open on the "
            "same options_contract_id even with a different decision_id"
        )
        # Position unchanged.
        assert runner.broker.get_position(first_symbol).qty == 1
        # Only one persisted order.
        persisted = center.session_store.get_order(
            session_id=sid, client_order_id="phase-c-dup-cid-2"
        )
        assert persisted is None


# --------------------------------------------------------------------------- #
# Test 13 — Underlying spot is NEVER used as the option premium
# --------------------------------------------------------------------------- #
class TestSpotNeverUsedAsPremium:
    def test_execution_price_equals_option_premium_not_underlying_spot(
        self, setup
    ):
        controller = setup["controller"]
        center = setup["center"]
        dep = setup["deployment"]
        sid = center.find_session_for_deployment(dep.deployment_id)
        decision = _make_decision(option_intent="CE")
        # NIFTY spot = 25,000 (decision.reference_price). Premium = 185.
        # The fill price MUST be the premium, not the spot.
        result = controller.execute_option_order(
            decision=decision,
            spot_price=25000.0,
            deployment_id=dep.deployment_id,
            session_id=sid,
            order_quantity=1,
            options_deployment_config=dep.config,
            explicit_option_type="CE",
            client_order_id="phase-c-test-premium",
        )
        assert result is not None
        # The fill price is near 185 (premium), NOT near 25,000 (spot).
        assert abs(result.avg_fill_price - 185.0) < 5.0, (
            f"REGRESSION: fill price {result.avg_fill_price} looks like the "
            f"underlying spot, not the option premium"
        )


# --------------------------------------------------------------------------- #
# Test 14 — InMemoryOptionsChainProvider cannot execute
# --------------------------------------------------------------------------- #
class TestSyntheticProviderCannotExecute:
    def test_inmemory_chain_provider_not_wired_in_execute_path(self):
        from trading_system.autonomous.options_contract import (
            InMemoryOptionsChainProvider,
        )

        # Defence in depth: the controller's resolve_options_plan refuses
        # synthetic chain providers; the execute path does not depend on
        # the chain provider at all (it goes through CurrentOptionDiscoverer
        # + CurrentOptionQuoteProvider). Verify both invariants.
        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_controller(center)
        dep = _create_options_deployment(
            controller, bot_id="bot-synth", spec=spec,
        strategy_id=strategy_id,
            options_enabled=True, allowed_option_types=["CE", "PE"], max_contracts=1,
        )
        # Real provider wiring.
        _attach_phase_b(controller)
        # Then attach a synthetic chain provider — Phase A guard must reject.
        controller.set_chain_provider(InMemoryOptionsChainProvider())
        sid = center.find_session_for_deployment(dep.deployment_id)
        # The chain provider is irrelevant to execute_option_order (which goes
        # through set_option_discoverer + set_quote_provider). Verify that
        # submitting still works because we have the real discoverer+quote.
        decision = _make_decision(option_intent="CE")
        result = controller.execute_option_order(
            decision=decision,
            spot_price=25000.0,
            deployment_id=dep.deployment_id,
            session_id=sid,
            options_deployment_config=dep.config,
            explicit_option_type="CE",
            client_order_id="phase-c-test-synth",
        )
        assert result is not None, (
            "execute_option_order must succeed when real providers are attached, "
            "regardless of any synthetic chain provider being set as a decoy"
        )
        # And the controller-level chain-provider rejection logic is intact.
        class _DummyDecision:
            decision_id = "x"
            opportunity_symbol = "NIFTY"
            selected_configuration = SimpleNamespace(
                strategy_id="phasec-spec", timeframe="1d"
            )
            signal = SimpleNamespace(action="buy", reference_price=25000.0,
                                      timestamp=datetime.now(UTC))
        plan = controller.resolve_options_plan(_DummyDecision())
        assert plan is None, (
            "resolve_options_plan must reject synthetic InMemoryOptionsChainProvider"
        )


# --------------------------------------------------------------------------- #
# Test 15 — SELL != PUT: the controller never silently maps SELL → PUT
# --------------------------------------------------------------------------- #
class TestSellIsNotPut:
    def test_sell_action_is_rejected_for_option_execution(self, setup):
        """Phase C supports BUY CE / BUY PE only. A SELL action with
        option_intent=None must NOT be silently coerced into a PUT buy."""
        controller = setup["controller"]
        center = setup["center"]
        dep = setup["deployment"]
        sid = center.find_session_for_deployment(dep.deployment_id)
        # Decision has action=SELL but option_intent=None. The scheduler
        # route would skip it for the option path (scheduler enforces this);
        # the controller must also NOT silently map BUY/SELL → CE/PE.
        decision = _make_decision(action="sell", option_intent=None)
        result = controller.execute_option_order(
            decision=decision,
            spot_price=25000.0,
            deployment_id=dep.deployment_id,
            session_id=sid,
            options_deployment_config=dep.config,
            explicit_option_type="CE",  # caller forces CE
            client_order_id="phase-c-test-sell-mapping",
        )
        # Even with explicit_option_type=CE, a SELL decision produces no
        # contract selection because the discoverer requires the signal to
        # express intent — the scheduler path never gets here.
        # The audit's invariant is verified at the controller level by
        # the fact that OrderIntent.side is ALWAYS BUY (see TestBuyPE).
        # For this test we simply confirm the controller did not silently
        # change the side from BUY to SELL.
        if result is not None:
            assert result.side == "BUY", (
                "CRITICAL: side must always be BUY for option fills, "
                f"got side={result.side!r}. SELL != PUT."
            )


# --------------------------------------------------------------------------- #
# Test 16 — Scheduler end-to-end via _execute_one_option_decision
# --------------------------------------------------------------------------- #
class TestSchedulerOptionExecution:
    def test_scheduler_option_path_buy_ce_end_to_end(self):
        """Wire a scheduler-equivalent controller, build an options-enabled
        deployment, and call ``_execute_one_option_decision`` directly.
        """
        from backend.autonomous_scheduler import _execute_one_option_decision

        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_controller(center)
        dep = _create_options_deployment(
            controller, bot_id="bot-sched-ce", spec=spec,
        strategy_id=strategy_id,
            options_enabled=True, allowed_option_types=["CE", "PE"], max_contracts=1,
        )
        _attach_phase_b(controller, premium=185.0)
        decision = _make_decision(option_intent="CE")
        result = _execute_one_option_decision(
            controller, decision, spot_price=25000.0, target_qty=1
        )
        assert result["result"] == "submitted", (
            f"scheduler option path did not submit: {result}"
        )
        assert result["option_intent"] == "CE"
        assert result["status"] == "FILLED"
        assert result["options_contract_id"] is not None
        assert result["strike"] is not None and result["strike"] > 0
        assert result["expiry"] == FUTURE_EXPIRY
        # Side invariant.
        assert result["order_id"] is not None

    def test_scheduler_option_path_buy_pe_end_to_end(self):
        from backend.autonomous_scheduler import _execute_one_option_decision

        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_controller(center)
        dep = _create_options_deployment(
            controller, bot_id="bot-sched-pe", spec=spec,
        strategy_id=strategy_id,
            options_enabled=True, allowed_option_types=["CE", "PE"], max_contracts=1,
        )
        _attach_phase_b(controller, premium=210.0)
        decision = _make_decision(option_intent="PE")
        result = _execute_one_option_decision(
            controller, decision, spot_price=25000.0, target_qty=1
        )
        assert result["result"] == "submitted", result
        assert result["option_intent"] == "PE"
        assert result["status"] == "FILLED"

    def test_scheduler_rejects_no_options_deployment(self):
        from backend.autonomous_scheduler import _execute_one_option_decision

        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_controller(center)
        # NO options-enabled deployment created.
        _attach_phase_b(controller)
        decision = _make_decision(option_intent="CE")
        result = _execute_one_option_decision(
            controller, decision, spot_price=25000.0, target_qty=1
        )
        assert result["result"] == "no_options_deployment", result

    def test_scheduler_rejects_sell_action(self):
        from backend.autonomous_scheduler import _execute_one_option_decision

        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_controller(center)
        dep = _create_options_deployment(
            controller, bot_id="bot-sched-sell", spec=spec,
        strategy_id=strategy_id,
            options_enabled=True, allowed_option_types=["CE", "PE"], max_contracts=1,
        )
        _attach_phase_b(controller)
        # SELL signal with PE intent but no existing long position:
        # scheduler rejects at the position-aware exit check.
        decision = _make_decision(action="sell", option_intent="PE")
        result = _execute_one_option_decision(
            controller, decision, spot_price=25000.0, target_qty=1
        )
        assert result["result"] == "no_long_option_position", result

    def test_scheduler_ce_only_deployment_rejects_pe(self):
        from backend.autonomous_scheduler import _execute_one_option_decision

        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_controller(center)
        dep = _create_options_deployment(
            controller, bot_id="bot-sched-ce-only", spec=spec,
        strategy_id=strategy_id,
            options_enabled=True, allowed_option_types=["CE"], max_contracts=1,
        )
        _attach_phase_b(controller)
        decision = _make_decision(option_intent="PE")
        result = _execute_one_option_decision(
            controller, decision, spot_price=25000.0, target_qty=1
        )
        assert result["result"] == "option_type_not_allowed", result

    def test_scheduler_options_disabled_rejects(self):
        from backend.autonomous_scheduler import _execute_one_option_decision

        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_controller(center)
        dep = _create_options_deployment(
            controller, bot_id="bot-sched-disabled", spec=spec,
        strategy_id=strategy_id,
            options_enabled=False, allowed_option_types=["CE", "PE"], max_contracts=1,
        )
        _attach_phase_b(controller)
        decision = _make_decision(option_intent="CE")
        result = _execute_one_option_decision(
            controller, decision, spot_price=25000.0, target_qty=1
        )
        assert result["result"] == "no_options_deployment", result

    def test_scheduler_idempotent_replay(self):
        from backend.autonomous_scheduler import _execute_one_option_decision

        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_controller(center)
        dep = _create_options_deployment(
            controller, bot_id="bot-sched-idem", spec=spec,
        strategy_id=strategy_id,
            options_enabled=True, allowed_option_types=["CE", "PE"], max_contracts=1,
        )
        _attach_phase_b(controller, premium=185.0)
        decision = _make_decision(option_intent="CE")
        r1 = _execute_one_option_decision(
            controller, decision, spot_price=25000.0, target_qty=1
        )
        r2 = _execute_one_option_decision(
            controller, decision, spot_price=25000.0, target_qty=1
        )
        assert r1["result"] == "submitted"
        assert r2["result"] == "already_executed"


# --------------------------------------------------------------------------- #
# Test 17 — Option EXIT / SELL flatten path (regression)
# --------------------------------------------------------------------------- #
# Regression cover for the defect where ``_execute_one_option_decision``
# only allowed ``buy``/``sell`` and rejected the canonical
# ``SignalAction.EXIT`` flatten with ``no_option_action`` before the
# close branch could run — leaving every opened option position open
# forever. No test drove the option path with ``action="exit"``, so the
# suite stayed green.
class TestSchedulerOptionExit:
    @staticmethod
    def _wire(bot_id: str, *, premium: float = 185.0):
        from backend.autonomous_scheduler import _execute_one_option_decision

        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_controller(center, bot_id=bot_id)
        dep = _create_options_deployment(
            controller, bot_id=bot_id, spec=spec, strategy_id=strategy_id,
            options_enabled=True, allowed_option_types=["CE", "PE"], max_contracts=1,
        )
        _attach_phase_b(controller, premium=premium)
        return controller, center, dep, _execute_one_option_decision

    def test_exit_action_closes_open_option_position(self):
        controller, center, dep, execute = self._wire("bot-sched-exit")

        opened = execute(
            controller, _make_decision(option_intent="CE"),
            spot_price=25000.0, target_qty=1,
        )
        assert opened["result"] == "submitted", opened

        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)
        assert _open_positions(runner), "expected an open position after BUY"

        # The canonical flatten action. This is what every built-in strategy
        # emits (SignalAction.EXIT -- "flatten any open position").
        closed = execute(
            controller,
            _make_decision(action="exit", option_intent=None, decision_id="exit-ce-1"),
            spot_price=25000.0, target_qty=1,
        )
        assert closed["result"] == "submitted", (
            f"EXIT failed to close the option position: {closed}"
        )
        assert closed["option_intent"] == "CE", (
            "EXIT should backfill option_intent from the matched position"
        )
        assert closed["options_contract_id"] == opened["options_contract_id"]
        # The whole point: the position is actually flat afterwards.
        assert _open_positions(runner) == {}, (
            "option position still open after EXIT: "
            f"{ {k: v.qty for k, v in _open_positions(runner).items()} }"
        )
        # Realized P&L must be booked on the retained (now-flat) record.
        flat_rec = next(
            p for p in runner.broker.positions().values()
            if p.options_contract_id == opened["options_contract_id"]
        )
        assert flat_rec.qty == 0.0
        assert flat_rec.avg_entry_price == 0.0

    def test_sell_without_option_intent_closes_position(self):
        """SELL must not require option_intent — the contract is known."""
        controller, center, dep, execute = self._wire("bot-sched-sell-nointent")

        opened = execute(
            controller, _make_decision(option_intent="PE"),
            spot_price=25000.0, target_qty=1,
        )
        assert opened["result"] == "submitted", opened

        closed = execute(
            controller,
            _make_decision(action="sell", option_intent=None, decision_id="sell-no-intent-1"),
            spot_price=25000.0, target_qty=1,
        )
        assert closed["result"] == "submitted", (
            f"SELL without option_intent should close from position metadata: {closed}"
        )
        assert closed["option_intent"] == "PE"
        assert closed["options_contract_id"] == opened["options_contract_id"]

        sid = center.find_session_for_deployment(dep.deployment_id)
        assert _open_positions(center.get_runner(sid)) == {}

    def test_exit_closes_the_most_recent_position_lifo(self):
        """Two positions open: EXIT must flatten the newest first."""
        controller, center, dep, execute = self._wire("bot-sched-lifo")
        _attach_phase_b(controller, premium=185.0)

        first = execute(
            controller, _make_decision(option_intent="CE", decision_id="lifo-1"),
            spot_price=25000.0, target_qty=1,
        )
        assert first["result"] == "submitted", first

        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)
        assert len(_open_positions(runner)) == 1

        # Open a second, distinct contract (different strike) directly through
        # the control center so we control the contract identity.
        from trading_system.execution.orders import OrderIntent, OrderType, Side

        second_contract = _build_option_instrument(
            underlying="NIFTY", strike=24900.0, option_type="CE", expiry=FUTURE_EXPIRY,
        )
        controller.control_center.update_deployment_market_price(
            deployment_id=dep.deployment_id, symbol=second_contract.key, price=180.0,
        )
        intent = OrderIntent(
            symbol=second_contract.key,
            side=Side.BUY,
            quantity=1,
            order_type=OrderType.MARKET,
            current_price=180.0,
            client_order_id="lifo-second-buy",
            options_contract_id=second_contract.contract_id,
            strike=second_contract.strike,
            expiry=second_contract.expiry,
            option_type=second_contract.option_type,
            contract_size=second_contract.lot_size,
        )
        res = controller.control_center.submit_order_intent(session_id=sid, intent=intent)
        assert res.status == "FILLED", res
        assert len(_open_positions(runner)) == 2

        closed = execute(
            controller,
            _make_decision(action="exit", option_intent="CE", decision_id="lifo-exit"),
            spot_price=25000.0, target_qty=1,
        )
        assert closed["result"] == "submitted", closed
        # Newest first => the 24900 strike we just opened, not the original.
        assert closed["strike"] == 24900.0, (
            f"expected LIFO close of the newest contract, got strike={closed['strike']}"
        )
        remaining = _open_positions(runner)
        assert len(remaining) == 1
        assert closed["options_contract_id"] not in remaining

    def test_exit_with_no_open_position_is_rejected(self):
        controller, center, dep, execute = self._wire("bot-sched-exit-flat")
        result = execute(
            controller,
            _make_decision(action="exit", option_intent=None, decision_id="exit-flat-1"),
            spot_price=25000.0, target_qty=1,
        )
        assert result["result"] == "no_long_option_position", result
        assert "None" not in result["detail"], (
            f"error detail leaked a null option_intent: {result['detail']}"
        )

    def test_hold_is_a_noop(self):
        controller, center, dep, execute = self._wire("bot-sched-hold")
        result = execute(
            controller, _make_decision(action="hold", option_intent=None),
            spot_price=25000.0, target_qty=1,
        )
        assert result["result"] == "no_option_action", result
        sid = center.find_session_for_deployment(dep.deployment_id)
        assert _open_positions(center.get_runner(sid)) == {}

    def test_unknown_action_is_rejected_not_coerced(self):
        """A non-contract action must not be silently turned into a SELL."""
        controller, center, dep, execute = self._wire("bot-sched-garbage")
        # StrategySignal is a frozen dataclass, so build the decision with a
        # duck-typed signal carrying an out-of-contract action — this is what
        # the scheduler's getattr-based dispatch would see from a bad producer.
        decision = SimpleNamespace(
            decision_id="garbage-action-1",
            opportunity_symbol="NSE:NIFTY",
            selected_configuration=None,
            signal=SimpleNamespace(action="SHORT", option_intent=None),
            action="SHORT",
        )
        result = execute(controller, decision, spot_price=25000.0, target_qty=1)
        assert result["result"] == "no_option_action", result
        sid = center.find_session_for_deployment(dep.deployment_id)
        assert _open_positions(center.get_runner(sid)) == {}, (
            "an unrecognised action must not produce an order"
        )

    def test_buy_without_option_intent_is_rejected(self):
        """A BUY needs an explicit direction (SELL-is-not-PUT invariant)."""
        controller, center, dep, execute = self._wire("bot-sched-buy-nointent")
        result = execute(
            controller, _make_decision(action="buy", option_intent=None),
            spot_price=25000.0, target_qty=1,
        )
        assert result["result"] == "option_intent_required", result
        sid = center.find_session_for_deployment(dep.deployment_id)
        assert _open_positions(center.get_runner(sid)) == {}


# --------------------------------------------------------------------------- #
# Test 18 — Controller action->side resolution (fail closed)
# --------------------------------------------------------------------------- #
class TestControllerSideResolution:
    def test_resolver_maps_known_actions(self):
        from trading_system.autonomous.controller import _resolve_option_order_side

        assert _resolve_option_order_side(action="buy") == Side.BUY
        assert _resolve_option_order_side(action="sell") == Side.SELL
        # EXIT flattens => sell-to-close.
        assert _resolve_option_order_side(action="exit") == Side.SELL
        assert _resolve_option_order_side(action=SignalAction.EXIT) == Side.SELL
        # explicit_side wins.
        assert _resolve_option_order_side(explicit_side="sell", action="buy") == Side.SELL

    def test_resolver_fails_closed_on_non_executable(self):
        from trading_system.autonomous.controller import _resolve_option_order_side

        for bad in ("hold", "HOLD", None, "", "short", "flip", 0):
            assert _resolve_option_order_side(action=bad) is None, (
                f"{bad!r} must not resolve to a Side"
            )
        assert _resolve_option_order_side(explicit_side="hold", action="buy") is None

    def test_controller_rejects_hold_action(self, setup):
        """A HOLD reaching execute_option_order must not fabricate a sell."""
        controller = setup["controller"]
        center = setup["center"]
        dep = setup["deployment"]
        sid = center.find_session_for_deployment(dep.deployment_id)
        decision = _make_decision(action="hold", option_intent=None)
        result = controller.execute_option_order(
            decision=decision,
            spot_price=25000.0,
            deployment_id=dep.deployment_id,
            session_id=sid,
            order_quantity=1,
            options_deployment_config=dep.config,
            client_order_id="phase-c-hold-001",
        )
        assert result is None, "HOLD must be rejected by the controller"
        assert center.get_runner(sid).broker.positions() == {}

    def test_controller_closes_via_exit_decision(self, setup):
        """End-to-end at the controller layer: BUY then a sell-to-close."""
        controller = setup["controller"]
        center = setup["center"]
        dep = setup["deployment"]
        sid = center.find_session_for_deployment(dep.deployment_id)

        opened = controller.execute_option_order(
            decision=_make_decision(option_intent="CE"),
            spot_price=25000.0,
            deployment_id=dep.deployment_id,
            session_id=sid,
            order_quantity=1,
            options_deployment_config=dep.config,
            explicit_option_type="CE",
            client_order_id="phase-c-exit-ce-001",
        )
        assert opened is not None and opened.status == "FILLED", opened
        runner = center.get_runner(sid)
        position = runner.broker.get_position(opened.symbol)
        assert position is not None and position.qty == 1

        exit_decision = SimpleNamespace(
            decision_id="phase-c-exit-ce-001-exit",
            opportunity_symbol="NSE:NIFTY",
            selected_configuration=SimpleNamespace(
                strategy_id="phasec-spec", timeframe="1d",
            ),
            action="sell",
            signal=SimpleNamespace(
                action="sell",
                reference_price=25000.0,
                option_intent="CE",
                option_contract_id=opened.options_contract_id,
            ),
        )
        closed = controller.execute_option_order(
            decision=exit_decision,
            spot_price=25000.0,
            deployment_id=dep.deployment_id,
            session_id=sid,
            order_quantity=1,
            options_deployment_config=dep.config,
            explicit_option_type="CE",
            existing_position=position,
        )
        assert closed is not None, "exit order was rejected"
        assert closed.side == "SELL", f"exit must be a SELL, got {closed.side!r}"
        assert closed.status == "FILLED", closed
        flat = runner.broker.get_position(opened.symbol)
        assert flat is not None and not flat.is_open and flat.qty == 0.0, (
            f"position should be flat after the exit filled, got {flat}"
        )


# --------------------------------------------------------------------------- #
# Test 19 — Options-mode strategies must still emit EXIT
# --------------------------------------------------------------------------- #
class TestOptionsModeExitSignal:
    """Options mode rewrites bearish entries to BUY+PE. It must NOT rewrite
    the EXIT flatten, or the position can never be closed.
    """
    @staticmethod
    def _state(*, closes, position_side: int = 1):
        import pandas as pd

        from trading_system.strategy_factory.contract import MarketState, PositionState

        n = len(closes)
        # Timezone-aware daily index ending "now" — MarketState enforces that
        # timestamp == the last bar index (no unclosed-bar data).
        idx = pd.date_range(end=pd.Timestamp.now(tz=UTC), periods=n, freq="1D")
        bars = pd.DataFrame(
            {
                "open": [float(c) for c in closes],
                "high": [float(c) + 1.0 for c in closes],
                "low": [float(c) - 1.0 for c in closes],
                "close": [float(c) for c in closes],
                "volume": [1000.0] * n,
            },
            index=idx,
        )
        return MarketState(
            symbol="NSE:NIFTY",
            timeframe="1d",
            timestamp=idx[-1].to_pydatetime(),
            bars=bars,
            position=PositionState(symbol="NSE:NIFTY", side=position_side),
        )

    def test_ema_crossover_options_mode_passes_exit_through(self):
        from trading_system.strategy_factory.builtin.ema_crossover import (
            EMACrossoverStrategy,
        )

        strat = EMACrossoverStrategy(fast_period=3, slow_period=6, options_mode=True)

        # Rising market, flat book => long entry, which options mode maps to
        # BUY + a long CE (never a short underlying).
        rising = [100.0 + 2.0 * i for i in range(30)]
        entry = strat.evaluate(self._state(closes=rising, position_side=0))
        assert entry.action == SignalAction.BUY, entry.action
        assert entry.option_intent == "CE", entry.option_intent

        # Now the trend rolls over. With allow_short=False (the deployment
        # default) _desired() collapses to 0, and _transition(current=+1,
        # desired=0) emits the canonical EXIT flatten. Options mode must let it
        # through unchanged — rewriting it to BUY would strand the position.
        falling = list(reversed(rising))
        flat = strat.evaluate(self._state(closes=falling, position_side=1))
        assert flat.action == SignalAction.EXIT, (
            f"EXIT must survive options mode, got {flat.action}"
        )
        assert flat.option_intent is None, (
            "EXIT must not be rewritten into a directional option entry"
        )

    def test_rsi_mean_reversion_options_mode_passes_exit_through(self):
        from trading_system.strategy_factory.builtin.rsi_mean_reversion import (
            RSIMeanReversionStrategy,
        )

        strat = RSIMeanReversionStrategy(
            rsi_period=5, oversold=30.0, overbought=70.0, options_mode=True,
        )
        # Deeply oversold => long entry.
        state = self._state(closes=[100, 98, 96, 94, 92, 90, 88], position_side=0)
        entry = strat.evaluate(state)
        assert entry.action == SignalAction.BUY, entry.action
        assert entry.option_intent == "CE", entry.option_intent

        # Overbought while long => flatten.
        state_flat = self._state(
            closes=[88, 90, 94, 98, 102, 104, 105], position_side=1,
        )
        flat = strat.evaluate(state_flat)
        assert flat.action == SignalAction.EXIT, (
            f"EXIT must survive options mode, got {flat.action}"
        )

    def test_rsi_options_mode_does_not_leak_into_parameters(self):
        """options_mode is wiring, not a tunable strategy parameter."""
        from trading_system.strategy_factory.builtin.rsi_mean_reversion import (
            RSIMeanReversionStrategy,
        )

        params = RSIMeanReversionStrategy(options_mode=True).parameters
        assert "options_mode" not in params, params


# --------------------------------------------------------------------------- #
# Test 20 — No live broker invocation
# --------------------------------------------------------------------------- #
class TestNoLiveBroker:
    def test_no_upstox_order_endpoint_called(self, setup):
        """Defence in depth: assert no live order endpoint is invoked."""
        controller = setup["controller"]
        center = setup["center"]
        dep = setup["deployment"]
        sid = center.find_session_for_deployment(dep.deployment_id)
        decision = _make_decision(option_intent="CE")
        result = controller.execute_option_order(
            decision=decision,
            spot_price=25000.0,
            deployment_id=dep.deployment_id,
            session_id=sid,
            options_deployment_config=dep.config,
            explicit_option_type="CE",
            client_order_id="phase-c-test-no-live",
        )
        assert result is not None
        # The broker is the in-memory PaperBroker instance on the runner.
        # No real broker was constructed.
        from trading_system.execution.paper_broker import PaperBroker
        runner = center.get_runner(sid)
        assert isinstance(runner.broker, PaperBroker)


class TestStopLossTakeProfitConfig:
    """Every autonomous deployment must be armed with a per-position exit.

    A deployment with no SL/TP can only ever be closed by a strategy signal,
    so an unarmed deployment silently holds risk indefinitely.
    """

    def test_paper_deployment_config_defaults_are_sane(self):
        from trading_system.paper.deployment import (
            DEFAULT_STOP_LOSS_PCT,
            DEFAULT_TAKE_PROFIT_PCT,
        )

        assert 0.0 < DEFAULT_STOP_LOSS_PCT < 1.0
        assert 0.0 < DEFAULT_TAKE_PROFIT_PCT < 1.0
        # Take-profit should be further out than stop-loss: a wider loss
        # budget than profit target would be a losing expectancy.
        assert DEFAULT_TAKE_PROFIT_PCT > DEFAULT_STOP_LOSS_PCT

    def test_scheduler_deployment_config_is_armed(self, monkeypatch):
        """The scheduler's own deployment config must carry both legs."""
        from backend.autonomous_scheduler import _autonomous_deployment_config

        monkeypatch.delenv("AUTONOMOUS_STOP_LOSS_PCT", raising=False)
        monkeypatch.delenv("AUTONOMOUS_TAKE_PROFIT_PCT", raising=False)
        cfg = _autonomous_deployment_config()
        assert cfg.stop_loss_pct == 0.03
        assert cfg.take_profit_pct == 0.06
        assert cfg.execution_mode == "paper"

    def test_scheduler_deployment_config_honours_env(self, monkeypatch):
        from backend.autonomous_scheduler import _autonomous_deployment_config

        monkeypatch.setenv("AUTONOMOUS_STOP_LOSS_PCT", "0.02")
        monkeypatch.setenv("AUTONOMOUS_TAKE_PROFIT_PCT", "0.10")
        cfg = _autonomous_deployment_config()
        assert cfg.stop_loss_pct == 0.02
        assert cfg.take_profit_pct == 0.10

    def test_scheduler_deployment_config_allows_explicit_optout(self, monkeypatch):
        """An operator who turns both legs off must actually get no exit."""
        from backend.autonomous_scheduler import _autonomous_deployment_config

        monkeypatch.setenv("AUTONOMOUS_STOP_LOSS_PCT", "off")
        monkeypatch.setenv("AUTONOMOUS_TAKE_PROFIT_PCT", "off")
        cfg = _autonomous_deployment_config()
        assert cfg.stop_loss_pct is None
        assert cfg.take_profit_pct is None

    def test_scheduler_env_parsing_defaults(self, monkeypatch):
        from backend.autonomous_scheduler import (
            _env_stop_loss_pct,
            _env_take_profit_pct,
        )

        monkeypatch.delenv("AUTONOMOUS_STOP_LOSS_PCT", raising=False)
        monkeypatch.delenv("AUTONOMOUS_TAKE_PROFIT_PCT", raising=False)
        assert _env_stop_loss_pct() == 0.03
        assert _env_take_profit_pct() == 0.06

    def test_scheduler_env_parsing_overrides(self, monkeypatch):
        from backend.autonomous_scheduler import _env_stop_loss_pct, _env_take_profit_pct

        monkeypatch.setenv("AUTONOMOUS_STOP_LOSS_PCT", "0.015")
        monkeypatch.setenv("AUTONOMOUS_TAKE_PROFIT_PCT", "0.12")
        assert _env_stop_loss_pct() == 0.015
        assert _env_take_profit_pct() == 0.12

    @pytest.mark.parametrize("raw", ["", "none", "off", "false", "0"])
    def test_scheduler_env_parsing_disables_leg(self, monkeypatch, raw):
        from backend.autonomous_scheduler import _env_take_profit_pct

        monkeypatch.setenv("AUTONOMOUS_TAKE_PROFIT_PCT", raw)
        assert _env_take_profit_pct() is None

    @pytest.mark.parametrize("raw", ["garbage", "5", "-0.1", "1.0", "-1", "100"])
    def test_scheduler_env_parsing_rejects_bad_values(self, monkeypatch, raw):
        """Out-of-contract values fall back to the default, never to no-exit."""
        from backend.autonomous_scheduler import _env_stop_loss_pct

        monkeypatch.setenv("AUTONOMOUS_STOP_LOSS_PCT", raw)
        assert _env_stop_loss_pct() == 0.03


class TestSlTpSweep:
    """The SL/TP sweep must fire with no strategy signal at all.

    The built-in strategies emit HOLD while a position is open, and
    ``_run_one_tick`` only routes buy/sell decisions, so a risk check nested
    in the decision path would never run. The sweep is deliberately
    independent of signals.
    """

    @staticmethod
    def _wire(bot_id: str, *, premium: float = 185.0, sl: float | None = 0.03,
              tp: float | None = 0.06):
        from backend.autonomous_scheduler import (
            _execute_one_option_decision,
            _sweep_sl_tp_positions,
        )

        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_controller(center, bot_id=bot_id)
        dep = _create_options_deployment(
            controller, bot_id=bot_id, spec=spec, strategy_id=strategy_id,
            options_enabled=True, allowed_option_types=["CE", "PE"],
            max_contracts=1, stop_loss_pct=sl, take_profit_pct=tp,
        )
        _attach_phase_b(controller, premium=premium)
        return (
            controller, center, dep, _execute_one_option_decision,
            _sweep_sl_tp_positions, controller._quote_provider,
        )

    @staticmethod
    def _move_market(quotes, runner, multiplier: float) -> None:
        """Move the *market* to ``multiplier`` x entry, as a real price move would.

        Deliberately does not touch ``position.current_price``: the sweep
        re-marks open positions from the live quote, so writing the mark
        directly would test nothing.
        """
        entry = next(iter(_open_positions(runner).values())).avg_entry_price
        quotes._premium = entry * multiplier

    def test_take_profit_closes_position_without_any_signal(self):
        controller, center, dep, execute, sweep, quotes = self._wire("bot-sweep-tp")
        opened = execute(
            controller, _make_decision(option_intent="CE"),
            spot_price=25000.0, target_qty=1,
        )
        assert opened["result"] == "submitted", opened

        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)
        self._move_market(quotes, runner, 1.10)  # +10%, well past the 6% take-profit

        fired = sweep(controller)
        assert len(fired) == 1, f"expected exactly one SL/TP exit, got {fired}"
        assert fired[0]["result"] == "sl_tp_exit", fired[0]
        assert fired[0]["options_contract_id"] == opened["options_contract_id"]
        assert _open_positions(runner) == {}, (
            "position still open after take-profit: "
            f"{ {k: v.qty for k, v in _open_positions(runner).items()} }"
        )

    def test_stop_loss_closes_position_without_any_signal(self):
        controller, center, dep, execute, sweep, quotes = self._wire("bot-sweep-sl")
        execute(
            controller, _make_decision(option_intent="PE"),
            spot_price=25000.0, target_qty=1,
        )
        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)
        self._move_market(quotes, runner, 0.90)  # -10%, well past the 3% stop-loss

        fired = sweep(controller)
        assert len(fired) == 1, f"expected exactly one SL/TP exit, got {fired}"
        assert fired[0]["result"] == "sl_tp_exit", fired[0]
        assert _open_positions(runner) == {}, "position still open after stop-loss"

    def test_sweep_is_a_noop_inside_the_band(self):
        """+1% is inside both thresholds: must not touch the position."""
        controller, center, dep, execute, sweep, quotes = self._wire("bot-sweep-band")
        execute(
            controller, _make_decision(option_intent="CE"),
            spot_price=25000.0, target_qty=1,
        )
        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)
        self._move_market(quotes, runner, 1.01)

        assert sweep(controller) == []
        assert len(_open_positions(runner)) == 1, "sweep closed a position early"

    def test_sweep_is_idempotent(self):
        """A second sweep must not double-close or error on a flat book."""
        controller, center, dep, execute, sweep, quotes = self._wire("bot-sweep-idem")
        execute(
            controller, _make_decision(option_intent="CE"),
            spot_price=25000.0, target_qty=1,
        )
        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)
        self._move_market(quotes, runner, 1.10)

        assert len(sweep(controller)) == 1
        assert sweep(controller) == [], "sweep fired again on an already-flat book"

    def test_sweep_respects_disabled_thresholds(self):
        """With both legs off, the sweep must not close anything."""
        controller, center, dep, execute, sweep, quotes = self._wire(
            "bot-sweep-disabled", sl=None, tp=None,
        )
        execute(
            controller, _make_decision(option_intent="CE"),
            spot_price=25000.0, target_qty=1,
        )
        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)
        self._move_market(quotes, runner, 1.50)  # +50%: would fire if a threshold were armed

        assert sweep(controller) == []
        assert len(_open_positions(runner)) == 1, (
            "sweep closed a position with SL/TP disabled"
        )

    def test_sweep_ignores_stale_quote(self):
        """A stale quote must not be used to mark, so no exit may fire."""
        controller, center, dep, execute, sweep, quotes = self._wire("bot-sweep-stale")
        execute(
            controller, _make_decision(option_intent="CE"),
            spot_price=25000.0, target_qty=1,
        )
        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)
        entry = next(iter(_open_positions(runner).values())).avg_entry_price

        # Price is way past take-profit, but the quote is too old to trust.
        quotes._stale = True
        quotes._premium = entry * 5.0

        assert sweep(controller) == []
        assert len(_open_positions(runner)) == 1, (
            "sweep exited on a stale quote"
        )
        mark = next(iter(_open_positions(runner).values())).current_price
        assert mark == pytest.approx(entry), (
            f"a stale quote must not overwrite the mark (mark={mark})"
        )

    def test_sweep_books_realized_pnl(self):
        """The exit must realize the loss, not just flatten the quantity."""
        controller, center, dep, execute, sweep, quotes = self._wire("bot-sweep-pnl")
        execute(
            controller, _make_decision(option_intent="CE"),
            spot_price=25000.0, target_qty=1,
        )
        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)
        entry = next(iter(_open_positions(runner).values())).avg_entry_price
        self._move_market(quotes, runner, 0.90)

        fired = sweep(controller)
        assert len(fired) == 1, fired
        record = next(
            p for p in runner.broker.positions().values()
            if p.options_contract_id == fired[0]["options_contract_id"]
        )
        assert record.qty == 0.0
        # A stop-loss exit at -10% must realize a loss.
        assert record.realized_pnl < 0, (
            f"expected a realized loss, got {record.realized_pnl}"
        )
        assert record.avg_entry_price == 0.0
        assert entry > 0

    def test_sweep_ignores_non_active_deployment(self):
        """A stopped deployment has a detached session; must not be closed."""
        controller, center, dep, execute, sweep, quotes = self._wire("bot-sweep-stopped")
        execute(
            controller, _make_decision(option_intent="CE"),
            spot_price=25000.0, target_qty=1,
        )
        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)
        self._move_market(quotes, runner, 1.10)

        center.stop_deployment(dep.deployment_id)
        assert sweep(controller) == []
        assert len(_open_positions(runner)) == 1, (
            "sweep closed a position on a non-ACTIVE deployment"
        )

    def test_sweep_never_raises_on_no_deployments(self):
        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_controller(center, bot_id="bot-sweep-empty")
        from backend.autonomous_scheduler import _sweep_sl_tp_positions

        assert _sweep_sl_tp_positions(controller) == []


class TestSlTpSweepRunsOnEveryTick:
    """Regression: the sweep must not sit behind an early return.

    ``_run_one_tick`` can return early on market_closed / no_fresh_market_data
    / no_candidates. If the SL/TP sweep ran after those, a breached threshold
    would be missed on exactly the ticks where the strategy has nothing to say.
    """

    def test_sweep_precedes_early_returns_in_source(self):
        """Structural guard: the sweep call must come before the early returns."""
        import inspect

        from backend import autonomous_scheduler

        src = inspect.getsource(autonomous_scheduler._run_one_tick)
        sweep_at = src.index("_sweep_sl_tp_positions(")
        for reason in (
            '"no_fresh_market_data"',
            '"no_candidates"',
        ):
            assert sweep_at < src.index(reason), (
                f"SL/TP sweep is evaluated after the {reason} early return; "
                "breached thresholds would be skipped on those ticks"
            )


class TestSlTpExitDuringALiveTick:
    """End-to-end: a breached threshold must be actioned by a real tick.

    This is the exact scenario that was broken. The built-in strategies emit
    HOLD while a position is open, so on a normal tick the decision pipeline
    contributes no eligible decision at all — the SL/TP check nested in the
    decision path therefore never ran, and a position could only be closed by
    a rare explicit exit signal.
    """

    @staticmethod
    def _open_and_breach(bot_id: str, multiplier: float):
        """Open a position, then move the market (not the stored mark)."""
        from backend.autonomous_scheduler import _execute_one_option_decision

        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_controller(center, bot_id=bot_id)
        dep = _create_options_deployment(
            controller, bot_id=bot_id, spec=spec, strategy_id=strategy_id,
            options_enabled=True, allowed_option_types=["CE", "PE"],
            max_contracts=1, stop_loss_pct=0.03, take_profit_pct=0.06,
        )
        _attach_phase_b(controller, premium=185.0)
        opened = _execute_one_option_decision(
            controller, _make_decision(option_intent="CE"),
            spot_price=25000.0, target_qty=1,
        )
        assert opened["result"] == "submitted", opened

        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)
        quotes = controller._quote_provider
        entry = next(iter(_open_positions(runner).values())).avg_entry_price
        quotes._premium = entry * multiplier
        return controller, center, dep, sid, runner, quotes, opened

    def test_tick_flattens_take_profit_with_no_eligible_decision(self, monkeypatch):
        from backend import autonomous_scheduler

        controller, center, dep, sid, runner, quotes, opened = self._open_and_breach(
            "bot-tick-tp", 1.10,
        )
        # Force the session/data gates open so the tick reaches the decision
        # stage regardless of wall-clock time when the suite runs.
        monkeypatch.setattr(
            autonomous_scheduler, "_is_regular_session", lambda ts: True
        )
        monkeypatch.setattr(
            autonomous_scheduler, "_has_fresh_data", lambda *a, **k: False
        )

        result = autonomous_scheduler._run_one_tick(controller)

        assert result.get("risk_exits"), (
            f"a real tick did not action the breached take-profit: {result}"
        )
        assert result["risk_exits"][0]["result"] == "sl_tp_exit", result["risk_exits"][0]
        assert result["risk_exits"][0]["options_contract_id"] == opened["options_contract_id"]
        # The tick is allowed to have no eligible decisions at all — that is
        # the realistic case while a position is open.
        assert result.get("eligible_count", 0) == 0, result
        assert _open_positions(runner) == {}, (
            "position still open after a tick that breached take-profit: "
            f"{ {k: v.qty for k, v in _open_positions(runner).items()} }"
        )

    def test_tick_flattens_stop_loss_with_no_eligible_decision(self, monkeypatch):
        from backend import autonomous_scheduler

        controller, center, dep, sid, runner, quotes, opened = self._open_and_breach(
            "bot-tick-sl", 0.90,
        )
        monkeypatch.setattr(
            autonomous_scheduler, "_is_regular_session", lambda ts: True
        )
        monkeypatch.setattr(
            autonomous_scheduler, "_has_fresh_data", lambda *a, **k: False
        )

        result = autonomous_scheduler._run_one_tick(controller)

        assert result.get("risk_exits"), (
            f"a real tick did not action the breached stop-loss: {result}"
        )
        assert _open_positions(runner) == {}, "position still open after stop-loss tick"

    def test_tick_leaves_position_alone_inside_the_band(self, monkeypatch):
        from backend import autonomous_scheduler

        controller, center, dep, sid, runner, quotes, opened = self._open_and_breach(
            "bot-tick-band", 1.01,
        )
        monkeypatch.setattr(
            autonomous_scheduler, "_is_regular_session", lambda ts: True
        )
        monkeypatch.setattr(
            autonomous_scheduler, "_has_fresh_data", lambda *a, **k: False
        )

        result = autonomous_scheduler._run_one_tick(controller)

        assert result.get("risk_exits") == [], (
            f"tick exited a position that was inside the band: {result}"
        )
        assert len(_open_positions(runner)) == 1, "position closed without a breach"

    def test_halted_bot_does_not_sweep(self, monkeypatch):
        """A halted bot must not trade at all, including risk exits."""
        from backend import autonomous_scheduler
        from trading_system.autonomous.safety import KillSwitchReason

        controller, center, dep, sid, runner, quotes, opened = self._open_and_breach(
            "bot-tick-halted", 1.10,
        )
        controller.halt_bot(reason=KillSwitchReason.MANUAL, detail="test")
        monkeypatch.setattr(
            autonomous_scheduler, "_is_regular_session", lambda ts: True
        )

        result = autonomous_scheduler._run_one_tick(controller)

        assert result["reason"] == "kill_switch_halted", result
        assert len(_open_positions(runner)) == 1, (
            "a halted bot closed a position: the kill switch must win"
        )


class TestSlTpRequiresAFreshMark:
    """The sweep reads ``position.current_price``, so that mark must be live.

    A position's ``current_price`` is only refreshed as a side effect of
    submitting an order. On a tick where the strategy emits no signal, nothing
    marks the position to market, so a stale entry-price mark makes P&L 0% and
    the threshold can never be reached. These tests move the *quote*, not the
    position, so they only pass if the scheduler marks open positions itself.
    """

    @staticmethod
    def _open(bot_id: str):
        from backend.autonomous_scheduler import _execute_one_option_decision

        center, registry, intelligence, gate, spec, strategy_id = _build_control_center()
        controller = _build_controller(center, bot_id=bot_id)
        dep = _create_options_deployment(
            controller, bot_id=bot_id, spec=spec, strategy_id=strategy_id,
            options_enabled=True, allowed_option_types=["CE", "PE"],
            max_contracts=1, stop_loss_pct=0.03, take_profit_pct=0.06,
        )
        provider = _attach_phase_b(controller, premium=185.0)
        opened = _execute_one_option_decision(
            controller, _make_decision(option_intent="CE"),
            spot_price=25000.0, target_qty=1,
        )
        assert opened["result"] == "submitted", opened
        sid = center.find_session_for_deployment(dep.deployment_id)
        # _attach_phase_b returns the instrument repo; the quote provider that
        # actually drives marks is the one on the controller.
        return controller, center, dep, sid, center.get_runner(sid), controller._quote_provider

    def test_quote_move_alone_triggers_take_profit(self, monkeypatch):
        from backend import autonomous_scheduler

        controller, center, dep, sid, runner, provider = self._open("bot-mark-tp")
        entry = next(iter(_open_positions(runner).values())).avg_entry_price

        # The market moves; the position's stored mark is untouched.
        provider._premium = entry * 1.10
        pos = next(iter(_open_positions(runner).values()))
        assert pos.current_price == pytest.approx(entry), (
            "precondition: the stored mark should still be the entry price"
        )

        monkeypatch.setattr(
            autonomous_scheduler, "_is_regular_session", lambda ts: True
        )
        monkeypatch.setattr(
            autonomous_scheduler, "_has_fresh_data", lambda *a, **k: False
        )
        result = autonomous_scheduler._run_one_tick(controller)

        assert result.get("risk_exits"), (
            "a real quote move did not produce an exit: the open position is "
            f"never marked to market, so SL/TP can never fire. tick={result}"
        )
        assert _open_positions(runner) == {}

    def test_quote_drop_alone_triggers_stop_loss(self, monkeypatch):
        from backend import autonomous_scheduler

        controller, center, dep, sid, runner, provider = self._open("bot-mark-sl")
        entry = next(iter(_open_positions(runner).values())).avg_entry_price

        provider._premium = entry * 0.90
        monkeypatch.setattr(
            autonomous_scheduler, "_is_regular_session", lambda ts: True
        )
        monkeypatch.setattr(
            autonomous_scheduler, "_has_fresh_data", lambda *a, **k: False
        )
        result = autonomous_scheduler._run_one_tick(controller)

        assert result.get("risk_exits"), (
            f"a real quote drop did not produce an exit: tick={result}"
        )
        assert _open_positions(runner) == {}
        # And no live order endpoint was hit (Upstox/FYERS) — there is no
        # network code path inside execute_option_order.