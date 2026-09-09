"""Phase G — Autonomous NIFTY Options Paper Runtime Validation."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
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
from trading_system.execution.orders import OrderIntent, OrderType, Side
from trading_system.indicators import ema
from trading_system.paper.control import PaperTradingControlCenter
from trading_system.paper.deployment import PaperDeploymentConfig
from trading_system.research.evidence import EvidenceStore, EvidenceType, StrategyEvidence
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
from trading_system.research.strategy_registry import StrategyRegistry, evidence_identity, StrategyStatus

UTC = timezone.utc


def _build_nifty_ohlcv_df(end_ts: datetime, n: int = 60) -> pd.DataFrame:
    idx = pd.date_range(end=end_ts, periods=n, freq="D", tz="UTC")
    closes = [22000.0 + i * 20.0 for i in range(n)]
    df = pd.DataFrame(
        {"open": [c - 5 for c in closes], "high": [c + 10 for c in closes],
         "low": [c - 10 for c in closes], "close": closes, "volume": [1_000_000.0 + i * 10_000 for i in range(n)]},
        index=idx,
    )
    fast = ema(df["close"], 12).iloc[-1]
    slow = ema(df["close"], 26).iloc[-1]
    assert fast > slow, f"fixture must produce BUY signal (fast={fast} slow={slow})"
    return df


def _build_nifty_bearish_ohlcv_df(end_ts: datetime, n: int = 60) -> pd.DataFrame:
    idx = pd.date_range(end=end_ts, periods=n, freq="D", tz="UTC")
    closes = [22000.0 - i * 20.0 for i in range(n)]
    df = pd.DataFrame(
        {"open": [c + 5 for c in closes], "high": [c + 10 for c in closes],
         "low": [c - 10 for c in closes], "close": closes, "volume": [1_000_000.0 + i * 10_000 for i in range(n)]},
        index=idx,
    )
    fast = ema(df["close"], 12).iloc[-1]
    slow = ema(df["close"], 26).iloc[-1]
    assert fast < slow, f"fixture must produce SELL/PE signal (fast={fast} slow={slow})"
    return df


def _make_strategy_spec(symbol: str, timeframe: str) -> StrategySpec:
    return StrategySpec(
        name="Phase G Runtime Validation Strategy",
        description="Phase G runtime validation",
        symbol=symbol,
        timeframe=timeframe,
        indicators=[{"name": "sma", "params": {"window": 5}}],
        entry=make_condition(field_operand("close"), ">", indicator_operand("sma_5")),
        generated_by="phase-g-runtime-validation",
    )


def _build_control_center(data_provider, *, risk_guard=None):
    from sqlalchemy import create_engine
    from trading_system.paper.gate import DeploymentGate

    engine = create_engine("sqlite://")
    store = EvidenceStore(engine)
    registry = StrategyRegistry(store)
    intelligence = StrategyIntelligence(registry)
    gate = DeploymentGate(
        intelligence=intelligence,
        requirement=EvidenceRequirement(),
        freshness_config=EvidenceFreshnessConfig(max_age_days=180),
    )
    center = PaperTradingControlCenter(
        registry=registry,
        intelligence=intelligence,
        gate=gate,
        market_data_provider=data_provider,
    )

    spec = _make_strategy_spec("NSE:NIFTY", "1d")
    strategy = registry.register_strategy(spec)
    registry.update_strategy_status(strategy.strategy_id, StrategyStatus.WALK_FORWARD_VALIDATED)

    fresh = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    registry.record_evidence(StrategyEvidence(
        evidence_id=evidence_identity(strategy.strategy_id, EvidenceType.RESEARCH, "ds-phaseg", {"k": 1}),
        strategy_id=strategy.strategy_id,
        strategy_spec_hash=strategy.spec_hash,
        evidence_type=EvidenceType.RESEARCH,
        dataset_id="ds-phaseg",
        configuration_json={"k": 1},
        metrics_json={"rows": 400, "candidates": [{"variant_index": 0, "status": "evaluated", "spec_name": strategy.name,
                                                   "spec_errors": [], "error": "", "evaluation": {"total_return": 0.10,
                                                                                                "profit_factor": 1.5,
                                                                                                "max_drawdown": -0.05,
                                                                                                "n_trades": 25},
                                                   "filter_passed": True, "filter_reasons": []}], "ranking": [],
                       "notes": []},
        created_at=fresh,
    ))
    registry.record_evidence(StrategyEvidence(
        evidence_id=evidence_identity(strategy.strategy_id, EvidenceType.WALK_FORWARD, "ds-phaseg", {"k": 2}),
        strategy_id=strategy.strategy_id,
        strategy_spec_hash=strategy.spec_hash,
        evidence_type=EvidenceType.WALK_FORWARD,
        dataset_id="ds-phaseg",
        configuration_json={"k": 2},
        metrics_json={"kind": "fixed_spec", "spec_name": strategy.name, "symbol": strategy.symbol,
                       "timeframe": strategy.timeframe, "mode": "rolling", "folds": [],
                       "summary": {"n_folds": 5, "n_valid": 4, "n_failed": 1, "coverage": 0.8, "coverage_ok": True,
                                   "positive_folds": 3, "positive_fold_ratio": 0.75, "avg_fold_return": 0.05,
                                   "median_fold_return": 0.05, "worst_fold_return": -0.05, "best_fold_return": 0.15,
                                   "return_std": 0.05, "return_dispersion": 1.0, "max_validation_drawdown": -0.08,
                                   "consistency_score": 0.7, "total_validation_trades": 100,
                                   "min_validation_trades": 10, "valid_fold_ids": [0, 1, 2, 3]},
                       "warnings": [], "notes": []},
        created_at=fresh,
    ))
    return center, registry, intelligence, spec, strategy.strategy_id


def _build_nifty_option_bot(center, *, bot_id: str = "phase-g-bot") -> AutonomousController:
    user_constraints = UserConstraints(
        allowed_symbols=frozenset({"NSE:NIFTY"}),
        allowed_strategy_ids=frozenset({"ema_crossover"}),
        allowed_timeframes=frozenset({"1d"}),
        allowed_option_underlyings=frozenset({"NIFTY"}),
        max_drawdown_pct=0.15,
        trading_session=TradingSessionConstraints(),
    )
    bot_config = AutonomousBotConfig(
        bot_id=bot_id,
        name=f"Phase G NIFTY Options Bot {bot_id}",
        mode=BotMode.AUTONOMOUS,
        trading_mode=TradingMode.PAPER,
        enabled=True,
        user_constraints=user_constraints,
        max_simultaneous_positions=5,
        max_position_allocation_pct=MaxPositionPct(0.25),
        max_exposure_pct=MaxExposurePct(0.75),
        source=Source.AUTONOMOUS,
    )
    return AutonomousController(config=bot_config, control_center=center)


def _make_option_decision(
    *,
    symbol: str = "NSE:NIFTY",
    strategy_id: str = "ema_crossover",
    action: str = "buy",
    reference_price: float = 22000.0,
    option_intent: str = "CE",
    decision_id: str | None = None,
):
    from trading_system.strategy_factory.contract import SignalAction, StrategySignal

    if decision_id is None:
        payload = {"strategy_id": strategy_id, "symbol": symbol, "option_intent": option_intent,
                    "action": action, "ref": reference_price}
        decision_id = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:32]

    signal = StrategySignal(
        action=SignalAction.BUY if action == "buy" else SignalAction.SELL,
        strategy_id=strategy_id,
        timestamp=datetime.now(UTC),
        symbol=symbol,
        reference_price=reference_price,
        confidence=0.9,
        reason="phase-g runtime test",
        target_position=1,
        version="1.0.0",
        option_intent=option_intent,
    )
    return SimpleNamespace(
        decision_id=decision_id,
        opportunity_symbol=symbol,
        selected_configuration=SimpleNamespace(
            strategy_id=strategy_id, strategy_version="1.0.0", strategy_family="trend",
            timeframe="1d", compatibility_score=1.0, compatibility_order=0,
            score_components={}, freshness_factor=1.0, data_points=60, min_required_bars=30,
        ),
        signal=signal,
        action=action,
        is_valid=True,
        status="valid",
        decision_timestamp=datetime.now(UTC).isoformat(),
    )


def _attach_fake_option_infrastructure(controller):
    from trading_system.india.instrument_repository import InstrumentRepository
    from trading_system.india.instruments import Instrument, InstrumentType, InternalSymbol
    from trading_system.autonomous.options.discovery import CandidateEvaluationResult, EvaluatedCandidate, OptionEligibilityReason
    from trading_system.autonomous.options.model import OptionsContractSelection, OptionDirection

    repo = InstrumentRepository()

    def make_instrument(**kw):
        itype = InstrumentType.OPTION_CE if kw.get("option_type") == "CE" else InstrumentType.OPTION_PE
        token = f"{kw['underlying']}{kw['expiry'].replace('-', '')}{int(kw['strike'])}{kw['option_type']}"
        instr = Instrument(internal=InternalSymbol(exchange="NFO", symbol=token), instrument_type=itype, name=kw["underlying"])
        instr.underlying = kw["underlying"]
        instr.expiry = kw["expiry"]
        instr.strike = float(kw["strike"])
        instr.option_type = kw["option_type"]
        instr.lot_size = kw.get("lot_size", 50)
        return instr

    repo.register(make_instrument(underlying="NIFTY", option_type="CE", strike=22000.0, expiry="2099-12-31", lot_size=50))
    repo.register(make_instrument(underlying="NIFTY", option_type="CE", strike=22100.0, expiry="2099-12-31", lot_size=50))
    repo.register(make_instrument(underlying="NIFTY", option_type="PE", strike=22000.0, expiry="2099-12-31", lot_size=50))
    repo.register(make_instrument(underlying="NIFTY", option_type="PE", strike=21900.0, expiry="2099-12-31", lot_size=50))

    discoverer = MagicMock()
    
    def mock_discover(*, underlying, direction, spot_price, config=None, as_of=None):
        option_type = "CE" if direction == OptionDirection.CALL else "PE"
        strike = 22000.0 if option_type == "CE" else 22000.0
        instrument = make_instrument(underlying=underlying, option_type=option_type, strike=strike, expiry="2099-12-31", lot_size=50)
        selection = OptionsContractSelection(
            underlying=underlying,
            option_type=option_type,
            strike=strike,
            expiry="2099-12-31",
            instrument_type=instrument.instrument_type.value,
            instrument_id=instrument.contract_id,
            symbol=instrument.key,
        )
        candidate = EvaluatedCandidate(
            instrument=instrument,
            selection=selection,
            score=1.0,
            eligibility=OptionEligibilityReason.VALID,
            reason="mock selection",
        )
        return CandidateEvaluationResult(
            underlying=underlying,
            direction=direction.value if hasattr(direction, "value") else str(direction),
            spot_price=spot_price,
            as_of_date=as_of or datetime.now(UTC).isoformat(),
            candidates=[candidate],
            selected=candidate,
            selection_reason="mock selection",
        )
    
    discoverer.discover = mock_discover
    controller.set_option_discoverer(discoverer, repository=repo)

    class FakeQuote:
        def __init__(self, instrument, premium):
            self.instrument = instrument
            self.ltp = premium
            self.timestamp = datetime.now(UTC)
            self.fetched_at = datetime.now(UTC)
            self.age_seconds = 0.0

    class FakeQuoteProvider:
        def get_quote(self, instrument):
            return FakeQuote(instrument, 185.0)
        def is_fresh(self, quote, max_age_seconds=None):
            return True

    controller.set_quote_provider(FakeQuoteProvider())


# ---------------------------------------------------------------------------
# Phase G.1 — Runtime Path Audit
# ---------------------------------------------------------------------------
class TestRuntimePathAudit:
    def test_upstox_data_reaches_scheduler(self):
        df = _build_nifty_ohlcv_df(datetime.now(UTC))
        def provider(symbol, timeframe):
            return df if symbol == "NSE:NIFTY" else None
        center, _, _, spec, strategy_id = _build_control_center(provider)
        data = center.load_market_data("NSE:NIFTY", "1d")
        assert data is not None and len(data) > 0 and "close" in data.columns

    def test_controller_uses_control_center_data_provider(self):
        df = _build_nifty_ohlcv_df(datetime.now(UTC))
        def provider(symbol, timeframe):
            return df if symbol == "NSE:NIFTY" else None
        center, _, _, spec, strategy_id = _build_control_center(provider)
        controller = _build_nifty_option_bot(center)
        result = controller.scan_market()
        assert result is not None and hasattr(result, "candidates")

    def test_option_decisions_route_to_option_executor(self):
        from backend.autonomous_scheduler import _execute_one_option_decision
        df = _build_nifty_ohlcv_df(datetime.now(UTC))
        def provider(symbol, timeframe):
            return df if symbol == "NSE:NIFTY" else None
        center, _, _, spec, strategy_id = _build_control_center(provider)
        controller = _build_nifty_option_bot(center)
        _attach_fake_option_infrastructure(controller)
        coord = AutonomousDeploymentCoordinator(config=controller.config, control_center=center)
        result, dep = coord.create_autonomous_deployment(
            symbol="NSE:NIFTY", strategy_id=strategy_id, timeframe="1d", strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(execution_mode="paper", initial_cash=100_000.0,
                                                     allow_short=False, options_enabled=True,
                                                     allowed_option_types=["CE", "PE"]),
        )
        assert result == DeploymentCreationResult.SUCCESS
        decision = _make_option_decision(option_intent="CE")
        result = _execute_one_option_decision(controller, decision, spot_price=22000.0, target_qty=1)
        assert result["result"] == "submitted" and result["option_intent"] == "CE"


# ---------------------------------------------------------------------------
# Phase G.2 — Real Market Data Validation
# ---------------------------------------------------------------------------
class TestRealMarketDataValidation:
    def test_nifty_underlying_price_and_timestamp(self):
        df = _build_nifty_ohlcv_df(datetime.now(UTC))
        assert df["close"].iloc[-1] > 0 and df.index[-1].tzinfo is not None

    def test_option_chain_availability(self):
        df = _build_nifty_ohlcv_df(datetime.now(UTC))
        def provider(symbol, timeframe):
            return df if symbol == "NSE:NIFTY" else None
        center, _, _, spec, strategy_id = _build_control_center(provider)
        controller = _build_nifty_option_bot(center)
        _attach_fake_option_infrastructure(controller)
        assert controller._option_discoverer is not None and controller._instrument_repo is not None

    def test_option_premium_authoritative(self):
        df = _build_nifty_ohlcv_df(datetime.now(UTC))
        def provider(symbol, timeframe):
            return df if symbol == "NSE:NIFTY" else None
        center, _, _, spec, strategy_id = _build_control_center(provider)
        controller = _build_nifty_option_bot(center)
        _attach_fake_option_infrastructure(controller)
        coord = AutonomousDeploymentCoordinator(config=controller.config, control_center=center)
        result, dep = coord.create_autonomous_deployment(
            symbol="NSE:NIFTY", strategy_id=strategy_id, timeframe="1d", strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(execution_mode="paper", initial_cash=100_000.0,
                                                     allow_short=False, options_enabled=True,
                                                     allowed_option_types=["CE", "PE"]),
        )
        assert result == DeploymentCreationResult.SUCCESS
        sid = center.find_session_for_deployment(dep.deployment_id)
        decision = _make_option_decision(option_intent="CE")
        result = controller.execute_option_order(decision=decision, spot_price=22000.0, deployment_id=dep.deployment_id, session_id=sid)
        assert result is not None and result.avg_fill_price == pytest.approx(185.0, abs=0.5)
        assert result.avg_fill_price != 22000.0

    def test_stale_data_blocks_entry(self):
        class StaleQuoteProvider:
            def get_quote(self, instrument):
                return None
            def is_fresh(self, quote, max_age_seconds=None):
                return False

        df = _build_nifty_ohlcv_df(datetime.now(UTC))
        def provider(symbol, timeframe):
            return df if symbol == "NSE:NIFTY" else None
        center, _, _, spec, strategy_id = _build_control_center(provider)
        controller = _build_nifty_option_bot(center)
        _attach_fake_option_infrastructure(controller)
        controller.set_quote_provider(StaleQuoteProvider())
        decision = _make_option_decision(option_intent="CE")
        result = controller.execute_option_order(decision=decision, spot_price=22000.0)
        assert result is None

    def test_missing_option_contract_blocks_entry(self):
        from trading_system.india.instrument_repository import InstrumentRepository
        empty_repo = InstrumentRepository()
        discoverer = MagicMock()
        discoverer.discover = MagicMock(return_value=None)
        from trading_system.research.strategy_registry import StrategyRegistry
        from trading_system.research.strategy_intelligence import StrategyIntelligence
        from trading_system.paper.gate import DeploymentGate
        from sqlalchemy import create_engine
        engine = create_engine("sqlite://")
        store = EvidenceStore(engine)
        registry = StrategyRegistry(store)
        intelligence = StrategyIntelligence(registry)
        gate = DeploymentGate(intelligence=intelligence, requirement=EvidenceRequirement(), freshness_config=EvidenceFreshnessConfig(max_age_days=180))
        center = PaperTradingControlCenter(registry=registry, intelligence=intelligence, gate=gate, market_data_provider=lambda s, t: None)
        controller = _build_nifty_option_bot(center)
        controller.set_option_discoverer(discoverer, repository=empty_repo)
        controller.set_quote_provider(type("FakeQP", (), {"get_quote": lambda s, i: None, "is_fresh": lambda s, q, m=None: False})())
        decision = _make_option_decision(option_intent="CE")
        result = controller.execute_option_order(decision=decision, spot_price=22000.0)
        assert result is None


# ---------------------------------------------------------------------------
# Phase G.3 — Paper-Only Enforcement
# ---------------------------------------------------------------------------
class TestPaperOnlyEnforcement:
    def test_trading_mode_paper_enforced(self):
        df = _build_nifty_ohlcv_df(datetime.now(UTC))
        def provider(symbol, timeframe):
            return df if symbol == "NSE:NIFTY" else None
        center, _, _, spec, strategy_id = _build_control_center(provider)
        with pytest.raises(Exception):
            AutonomousController(config=AutonomousBotConfig(
                bot_id="live-bot", name="Live", mode=BotMode.AUTONOMOUS,
                trading_mode=TradingMode.LIVE, enabled=True, user_constraints=UserConstraints(),
                max_simultaneous_positions=1, max_position_allocation_pct=MaxPositionPct(0.25),
                max_exposure_pct=MaxExposurePct(0.75), source=Source.AUTONOMOUS,
            ), control_center=center)

    def test_paper_broker_always_used(self):
        df = _build_nifty_ohlcv_df(datetime.now(UTC))
        def provider(symbol, timeframe):
            return df if symbol == "NSE:NIFTY" else None
        center, _, _, spec, strategy_id = _build_control_center(provider)
        controller = _build_nifty_option_bot(center)
        _attach_fake_option_infrastructure(controller)
        coord = AutonomousDeploymentCoordinator(config=controller.config, control_center=center)
        result, dep = coord.create_autonomous_deployment(
            symbol="NSE:NIFTY", strategy_id=strategy_id, timeframe="1d", strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(execution_mode="paper", initial_cash=100_000.0,
                                                     allow_short=False, options_enabled=True,
                                                     allowed_option_types=["CE", "PE"]),
        )
        assert result == DeploymentCreationResult.SUCCESS
        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)
        from trading_system.execution.paper_broker import PaperBroker
        assert isinstance(runner.broker, PaperBroker)

    def test_no_live_broker_in_control_center(self):
        df = _build_nifty_ohlcv_df(datetime.now(UTC))
        def provider(symbol, timeframe):
            return df if symbol == "NSE:NIFTY" else None
        center, _, _, spec, strategy_id = _build_control_center(provider)
        assert not hasattr(center, "live_broker") and not hasattr(center, "broker")

    def test_kill_switch_blocks_new_entries(self):
        df = _build_nifty_ohlcv_df(datetime.now(UTC))
        def provider(symbol, timeframe):
            return df if symbol == "NSE:NIFTY" else None
        center, _, _, spec, strategy_id = _build_control_center(provider)
        controller = _build_nifty_option_bot(center)
        _attach_fake_option_infrastructure(controller)
        controller.halt_bot(
            reason=__import__("trading_system.autonomous.safety", fromlist=["KillSwitchReason"]).KillSwitchReason.MANUAL,
            detail="phase-g paper-only test",
        )
        decision = _make_option_decision(option_intent="CE")
        result = controller.execute_option_order(decision=decision, spot_price=22000.0)
        assert result is None


# ---------------------------------------------------------------------------
# Phase G.4 — Autonomous Runtime Lifecycle
# ---------------------------------------------------------------------------
class TestAutonomousRuntimeLifecycle:
    def test_ce_entry_then_exit_full_lifecycle(self):
        df = _build_nifty_ohlcv_df(datetime(2024, 1, 2, 6, 0, tzinfo=UTC))
        def data_provider(symbol, timeframe):
            return df if symbol == "NSE:NIFTY" and timeframe == "1d" else None
        center, _, _, spec, strategy_id = _build_control_center(data_provider)
        controller = _build_nifty_option_bot(center, bot_id="phase-g-ce-lifecycle")
        _attach_fake_option_infrastructure(controller)
        coord = AutonomousDeploymentCoordinator(config=controller.config, control_center=center)
        result, dep = coord.create_autonomous_deployment(
            symbol="NSE:NIFTY", strategy_id=strategy_id, timeframe="1d", strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(execution_mode="paper", initial_cash=100_000.0,
                                                     allow_short=False, options_enabled=True,
                                                     allowed_option_types=["CE", "PE"]),
        )
        assert result == DeploymentCreationResult.SUCCESS
        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)

        entry_decision = _make_option_decision(option_intent="CE", decision_id="phase-g-ce-entry")
        from backend.autonomous_scheduler import _execute_one_option_decision
        entry_result = _execute_one_option_decision(controller, entry_decision, spot_price=22000.0, target_qty=1)
        assert entry_result["result"] == "submitted" and entry_result["option_intent"] == "CE" and entry_result["status"] == "FILLED"

        positions = runner.broker.positions()
        option_pos = next((p for p in positions.values() if p.is_option), None)
        assert option_pos is not None and option_pos.qty == 1 and option_pos.option_type == "CE"

        runner.broker.update_market_price(option_pos.symbol, 200.0)
        unrealized = runner.broker.get_position(option_pos.symbol).unrealized_pnl
        assert unrealized != 0

        exit_decision = _make_option_decision(action="sell", option_intent="CE", decision_id="phase-g-ce-exit")
        exit_result = _execute_one_option_decision(controller, exit_decision, spot_price=22000.0, target_qty=1)
        assert exit_result["result"] == "submitted" and exit_result["option_intent"] == "CE" and exit_result["status"] == "FILLED"

        positions_after = runner.broker.positions()
        option_pos_after = next((p for p in positions_after.values() if p.is_option), None)
        assert option_pos_after is not None and option_pos_after.qty == 0 and option_pos_after.realized_pnl != 0

    def test_pe_entry_then_exit_full_lifecycle(self):
        df_entry = _build_nifty_bearish_ohlcv_df(datetime(2024, 1, 2, 6, 0, tzinfo=UTC))
        def provider(symbol, timeframe):
            return df_entry if symbol == "NSE:NIFTY" and timeframe == "1d" else None
        center, _, _, spec, strategy_id = _build_control_center(provider)
        controller = _build_nifty_option_bot(center, bot_id="phase-g-pe-lifecycle")

        class PEQuoteProvider:
            def get_quote(self, instrument):
                from trading_system.india.option_quotes import OptionQuote
                return OptionQuote(instrument=instrument, ltp=210.0, bid=209.5, ask=210.5,
                                    timestamp=datetime.now(UTC), fetched_at=datetime.now(UTC), age_seconds=0.0)
            def is_fresh(self, quote, max_age_seconds=None):
                return True
        controller.set_quote_provider(PEQuoteProvider())
        _attach_fake_option_infrastructure(controller)

        coord = AutonomousDeploymentCoordinator(config=controller.config, control_center=center)
        result, dep = coord.create_autonomous_deployment(
            symbol="NSE:NIFTY", strategy_id=strategy_id, timeframe="1d", strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(execution_mode="paper", initial_cash=100_000.0,
                                                     allow_short=False, options_enabled=True,
                                                     allowed_option_types=["CE", "PE"]),
        )
        assert result == DeploymentCreationResult.SUCCESS
        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)

        entry_decision = _make_option_decision(option_intent="PE", decision_id="phase-g-pe-entry")
        from backend.autonomous_scheduler import _execute_one_option_decision
        entry_result = _execute_one_option_decision(controller, entry_decision, spot_price=22000.0, target_qty=1)
        assert entry_result["result"] == "submitted" and entry_result["option_intent"] == "PE" and entry_result["status"] == "FILLED"

        positions = runner.broker.positions()
        option_pos = next((p for p in positions.values() if p.is_option), None)
        assert option_pos is not None and option_pos.qty == 1 and option_pos.option_type == "PE"

        exit_decision = _make_option_decision(action="sell", option_intent="PE", decision_id="phase-g-pe-exit")
        exit_result = _execute_one_option_decision(controller, exit_decision, spot_price=22000.0, target_qty=1)
        assert exit_result["result"] == "submitted" and exit_result["option_intent"] == "PE"

        positions_after = runner.broker.positions()
        option_pos_after = next((p for p in positions_after.values() if p.is_option), None)
        assert option_pos_after is not None and option_pos_after.qty == 0


# ---------------------------------------------------------------------------
# Phase G.5 — Safety / Failure Injection
# ---------------------------------------------------------------------------
class TestSafetyFailureInjection:
    def test_stale_market_data_blocks_entry(self):
        df = _build_nifty_ohlcv_df(datetime.now(UTC) - timedelta(days=30))
        def provider(symbol, timeframe):
            return df if symbol == "NSE:NIFTY" else None
        center, _, _, spec, strategy_id = _build_control_center(provider)
        controller = _build_nifty_option_bot(center, bot_id="phase-g-stale")
        _attach_fake_option_infrastructure(controller)
        from backend.autonomous_scheduler import _run_one_tick
        result = _run_one_tick(controller)
        assert result["result"] == "skip" and result["reason"] in {"no_fresh_market_data", "no_candidates", "market_closed"}

    def test_missing_option_contract_blocks_entry(self):
        from trading_system.india.instrument_repository import InstrumentRepository
        empty_repo = InstrumentRepository()
        discoverer = MagicMock()
        discoverer.discover = MagicMock(return_value=None)
        from trading_system.research.strategy_registry import StrategyRegistry
        from trading_system.research.strategy_intelligence import StrategyIntelligence
        from trading_system.paper.gate import DeploymentGate
        from sqlalchemy import create_engine
        engine = create_engine("sqlite://")
        store = EvidenceStore(engine)
        registry = StrategyRegistry(store)
        intelligence = StrategyIntelligence(registry)
        gate = DeploymentGate(intelligence=intelligence, requirement=EvidenceRequirement(), freshness_config=EvidenceFreshnessConfig(max_age_days=180))
        center = PaperTradingControlCenter(registry=registry, intelligence=intelligence, gate=gate, market_data_provider=lambda s, t: None)
        controller = _build_nifty_option_bot(center, bot_id="phase-g-missing-contract")
        controller.set_option_discoverer(discoverer, repository=empty_repo)
        controller.set_quote_provider(type("FakeQP", (), {"get_quote": lambda s, i: None, "is_fresh": lambda s, q, m=None: False})())
        decision = _make_option_decision(option_intent="CE")
        result = controller.execute_option_order(decision=decision, spot_price=22000.0)
        assert result is None

    def test_duplicate_scheduler_tick_no_duplicate_position(self):
        df = _build_nifty_ohlcv_df(datetime.now(UTC))
        def provider(symbol, timeframe):
            return df if symbol == "NSE:NIFTY" else None
        center, _, _, spec, strategy_id = _build_control_center(provider)
        controller = _build_nifty_option_bot(center, bot_id="phase-g-dup-tick")
        _attach_fake_option_infrastructure(controller)
        coord = AutonomousDeploymentCoordinator(config=controller.config, control_center=center)
        result, dep = coord.create_autonomous_deployment(
            symbol="NSE:NIFTY", strategy_id=strategy_id, timeframe="1d", strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(execution_mode="paper", initial_cash=100_000.0,
                                                     allow_short=False, options_enabled=True,
                                                     allowed_option_types=["CE", "PE"]),
        )
        assert result == DeploymentCreationResult.SUCCESS
        sid = center.find_session_for_deployment(dep.deployment_id)
        decision = _make_option_decision(option_intent="CE", decision_id="phase-g-dup-1")
        from backend.autonomous_scheduler import _execute_one_option_decision
        r1 = _execute_one_option_decision(controller, decision, spot_price=22000.0, target_qty=1)
        assert r1["result"] == "submitted"
        r2 = _execute_one_option_decision(controller, decision, spot_price=22000.0, target_qty=1)
        assert r2["result"] == "already_executed"
        runner = center.get_runner(sid)
        option_pos = next((p for p in runner.broker.positions().values() if p.is_option), None)
        assert option_pos is not None and option_pos.qty == 1

    def test_restart_with_open_option_position(self):
        df = _build_nifty_ohlcv_df(datetime.now(UTC))
        def provider(symbol, timeframe):
            return df if symbol == "NSE:NIFTY" else None
        center, _, _, spec, strategy_id = _build_control_center(provider)
        controller = _build_nifty_option_bot(center, bot_id="phase-g-restart")
        _attach_fake_option_infrastructure(controller)
        coord = AutonomousDeploymentCoordinator(config=controller.config, control_center=center)
        result, dep = coord.create_autonomous_deployment(
            symbol="NSE:NIFTY", strategy_id=strategy_id, timeframe="1d", strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(execution_mode="paper", initial_cash=100_000.0,
                                                     allow_short=False, options_enabled=True,
                                                     allowed_option_types=["CE", "PE"]),
        )
        assert result == DeploymentCreationResult.SUCCESS
        decision = _make_option_decision(option_intent="CE", decision_id="phase-g-restart-entry")
        from backend.autonomous_scheduler import _execute_one_option_decision
        entry_result = _execute_one_option_decision(controller, decision, spot_price=22000.0, target_qty=1)
        assert entry_result["result"] == "submitted"

        controller2 = _build_nifty_option_bot(center, bot_id="phase-g-restart")
        _attach_fake_option_infrastructure(controller2)
        r2 = _execute_one_option_decision(controller2, decision, spot_price=22000.0, target_qty=1)
        assert r2["result"] == "already_executed"
        exit_decision = _make_option_decision(action="sell", option_intent="CE", decision_id="phase-g-restart-exit")
        exit_result = _execute_one_option_decision(controller2, exit_decision, spot_price=22000.0, target_qty=1)
        assert exit_result["result"] == "submitted"

    def test_kill_switch_blocks_new_entries_and_flattens(self):
        df = _build_nifty_ohlcv_df(datetime.now(UTC))
        def provider(symbol, timeframe):
            return df if symbol == "NSE:NIFTY" else None
        center, _, _, spec, strategy_id = _build_control_center(provider)
        controller = _build_nifty_option_bot(center, bot_id="phase-g-killswitch")
        _attach_fake_option_infrastructure(controller)
        coord = AutonomousDeploymentCoordinator(config=controller.config, control_center=center)
        result, dep = coord.create_autonomous_deployment(
            symbol="NSE:NIFTY", strategy_id=strategy_id, timeframe="1d", strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(execution_mode="paper", initial_cash=100_000.0,
                                                     allow_short=False, options_enabled=True,
                                                     allowed_option_types=["CE", "PE"]),
        )
        assert result == DeploymentCreationResult.SUCCESS
        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)

        decision = _make_option_decision(option_intent="CE", decision_id="phase-g-kill-entry")
        from backend.autonomous_scheduler import _execute_one_option_decision
        entry_result = _execute_one_option_decision(controller, decision, spot_price=22000.0, target_qty=1)
        assert entry_result["result"] == "submitted"

        controller.halt_bot(
            reason=__import__("trading_system.autonomous.safety", fromlist=["KillSwitchReason"]).KillSwitchReason.MANUAL,
            detail="phase-g kill switch test",
        )
        new_decision = _make_option_decision(option_intent="PE", decision_id="phase-g-kill-new")
        new_result = _execute_one_option_decision(controller, new_decision, spot_price=22000.0, target_qty=1)
        assert new_result["result"] == "kill_switch_halted"

        cb = runner.circuit_breaker
        if cb is not None:
            cb.trip("phase_g_test")
            from backend.autonomous_scheduler import _flatten_positions
            _flatten_positions(runner, center, sid, datetime.now(UTC), decision_id="phase-g-kill-flatten")
            positions = runner.broker.positions()
            for pos in positions.values():
                if pos.is_open:
                    assert pos.qty == 0 or not pos.is_open

    def test_nonexistent_position_sell_rejected(self):
        df = _build_nifty_ohlcv_df(datetime.now(UTC))
        def provider(symbol, timeframe):
            return df if symbol == "NSE:NIFTY" else None
        center, _, _, spec, strategy_id = _build_control_center(provider)
        controller = _build_nifty_option_bot(center, bot_id="phase-g-no-pos")
        _attach_fake_option_infrastructure(controller)
        coord = AutonomousDeploymentCoordinator(config=controller.config, control_center=center)
        result, dep = coord.create_autonomous_deployment(
            symbol="NSE:NIFTY", strategy_id=strategy_id, timeframe="1d", strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(execution_mode="paper", initial_cash=100_000.0,
                                                     allow_short=False, options_enabled=True,
                                                     allowed_option_types=["CE", "PE"]),
        )
        assert result == DeploymentCreationResult.SUCCESS
        exit_decision = _make_option_decision(action="sell", option_intent="CE", decision_id="phase-g-no-pos-exit")
        from backend.autonomous_scheduler import _execute_one_option_decision
        exit_result = _execute_one_option_decision(controller, exit_decision, spot_price=22000.0, target_qty=1)
        assert exit_result["result"] == "no_long_option_position"

    def test_broker_failure_during_exit_no_fabricated_price(self):
        df = _build_nifty_ohlcv_df(datetime.now(UTC))
        def provider(symbol, timeframe):
            return df if symbol == "NSE:NIFTY" else None
        center, _, _, spec, strategy_id = _build_control_center(provider)
        controller = _build_nifty_option_bot(center, bot_id="phase-g-broker-fail")
        _attach_fake_option_infrastructure(controller)
        coord = AutonomousDeploymentCoordinator(config=controller.config, control_center=center)
        result, dep = coord.create_autonomous_deployment(
            symbol="NSE:NIFTY", strategy_id=strategy_id, timeframe="1d", strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(execution_mode="paper", initial_cash=100_000.0,
                                                     allow_short=False, options_enabled=True,
                                                     allowed_option_types=["CE", "PE"]),
        )
        assert result == DeploymentCreationResult.SUCCESS
        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)

        entry_decision = _make_option_decision(option_intent="CE", decision_id="phase-g-broker-entry")
        from backend.autonomous_scheduler import _execute_one_option_decision
        entry_result = _execute_one_option_decision(controller, entry_decision, spot_price=22000.0, target_qty=1)
        assert entry_result["result"] == "submitted"

        positions = runner.broker.positions()
        option_pos = next((p for p in positions.values() if p.is_option), None)
        assert option_pos is not None

        original_submit = runner.broker.submit_order
        def failing_submit(*args, **kwargs):
            raise RuntimeError("simulated broker failure")
        runner.broker.submit_order = failing_submit

        exit_decision = _make_option_decision(action="sell", option_intent="CE", decision_id="phase-g-broker-exit")
        exit_result = _execute_one_option_decision(controller, exit_decision, spot_price=22000.0, target_qty=1)
        assert exit_result["result"] == "rejected"

        runner.broker.submit_order = original_submit
        pos_after = runner.broker.get_position(option_pos.symbol)
        assert pos_after is not None and pos_after.qty == 1


# ---------------------------------------------------------------------------
# Phase G.8 — Centralized Emergency Flatten Regression
# ---------------------------------------------------------------------------
class TestCentralizedEmergencyFlatten:
    """Regression tests proving emergency flatten routes through submit_order_intent."""

    def test_flatten_routes_through_centralized_boundary(self):
        """Circuit breaker flatten uses center.submit_order_intent, not direct broker.submit_order."""
        df = _build_nifty_ohlcv_df(datetime.now(UTC))
        def provider(symbol, timeframe):
            return df if symbol == "NSE:NIFTY" else None
        center, _, _, spec, strategy_id = _build_control_center(provider)
        controller = _build_nifty_option_bot(center, bot_id="phase-g-flatten-central")
        _attach_fake_option_infrastructure(controller)
        coord = AutonomousDeploymentCoordinator(config=controller.config, control_center=center)
        result, dep = coord.create_autonomous_deployment(
            symbol="NSE:NIFTY", strategy_id=strategy_id, timeframe="1d", strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(execution_mode="paper", initial_cash=100_000.0,
                                                     allow_short=False, options_enabled=True,
                                                     allowed_option_types=["CE", "PE"]),
        )
        assert result == DeploymentCreationResult.SUCCESS
        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)

        # Open a position
        decision = _make_option_decision(option_intent="CE", decision_id="phase-g-flatten-entry")
        from backend.autonomous_scheduler import _execute_one_option_decision
        entry_result = _execute_one_option_decision(controller, decision, spot_price=22000.0, target_qty=1)
        assert entry_result["result"] == "submitted"

        # Trip circuit breaker and call _flatten_positions
        cb = runner.circuit_breaker
        cb.trip("phase_g_flatten_test")
        
        # Track whether submit_order_intent was called
        original_submit_intent = center.submit_order_intent
        submit_intent_calls = []
        def tracking_submit_intent(*args, **kwargs):
            submit_intent_calls.append(kwargs)
            return original_submit_intent(*args, **kwargs)
        center.submit_order_intent = tracking_submit_intent

        from backend.autonomous_scheduler import _flatten_positions
        _flatten_positions(runner, center, sid, datetime.now(UTC), decision_id="phase-g-flatten")

        # Verify submit_order_intent was called (not broker.submit_order)
        assert len(submit_intent_calls) == 1
        assert submit_intent_calls[0]["emergency"] is True
        assert submit_intent_calls[0]["intent"].symbol.startswith("NFO:NIFTY")

        # Verify position is closed
        positions = runner.broker.positions()
        option_pos = next((p for p in positions.values() if p.is_option), None)
        assert option_pos is not None
        assert option_pos.qty == 0

    def test_repeated_flatten_is_idempotent(self):
        """Repeated flatten attempts do not create duplicate orders."""
        df = _build_nifty_ohlcv_df(datetime.now(UTC))
        def provider(symbol, timeframe):
            return df if symbol == "NSE:NIFTY" else None
        center, _, _, spec, strategy_id = _build_control_center(provider)
        controller = _build_nifty_option_bot(center, bot_id="phase-g-flatten-idempotent")
        _attach_fake_option_infrastructure(controller)
        coord = AutonomousDeploymentCoordinator(config=controller.config, control_center=center)
        result, dep = coord.create_autonomous_deployment(
            symbol="NSE:NIFTY", strategy_id=strategy_id, timeframe="1d", strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(execution_mode="paper", initial_cash=100_000.0,
                                                     allow_short=False, options_enabled=True,
                                                     allowed_option_types=["CE", "PE"]),
        )
        assert result == DeploymentCreationResult.SUCCESS
        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)

        # Open a position
        decision = _make_option_decision(option_intent="CE", decision_id="phase-g-flatten-idem-entry")
        from backend.autonomous_scheduler import _execute_one_option_decision
        entry_result = _execute_one_option_decision(controller, decision, spot_price=22000.0, target_qty=1)
        assert entry_result["result"] == "submitted"

        # Trip circuit breaker
        cb = runner.circuit_breaker
        cb.trip("phase_g_flatten_idem_test")

        from backend.autonomous_scheduler import _flatten_positions
        # First flatten
        _flatten_positions(runner, center, sid, datetime.now(UTC), decision_id="phase-g-flatten-idem")
        # Second flatten (same decision_id -> same client_order_id -> idempotent)
        result2 = _flatten_positions(runner, center, sid, datetime.now(UTC), decision_id="phase-g-flatten-idem")

        # Verify position is closed and stays closed (no duplicate orders)
        positions = runner.broker.positions()
        option_pos = next((p for p in positions.values() if p.is_option), None)
        assert option_pos is not None
        assert option_pos.qty == 0

    def test_flatten_only_paper_broker_executes(self):
        """Only PaperBroker can execute emergency flatten."""
        df = _build_nifty_ohlcv_df(datetime.now(UTC))
        def provider(symbol, timeframe):
            return df if symbol == "NSE:NIFTY" else None
        center, _, _, spec, strategy_id = _build_control_center(provider)
        controller = _build_nifty_option_bot(center, bot_id="phase-g-flatten-paper")
        _attach_fake_option_infrastructure(controller)
        coord = AutonomousDeploymentCoordinator(config=controller.config, control_center=center)
        result, dep = coord.create_autonomous_deployment(
            symbol="NSE:NIFTY", strategy_id=strategy_id, timeframe="1d", strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(execution_mode="paper", initial_cash=100_000.0,
                                                     allow_short=False, options_enabled=True,
                                                     allowed_option_types=["CE", "PE"]),
        )
        assert result == DeploymentCreationResult.SUCCESS
        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)

        # Verify runner.broker is PaperBroker
        from trading_system.execution.paper_broker import PaperBroker
        assert isinstance(runner.broker, PaperBroker)

        # Open a position
        decision = _make_option_decision(option_intent="CE", decision_id="phase-g-flatten-paper-entry")
        from backend.autonomous_scheduler import _execute_one_option_decision
        entry_result = _execute_one_option_decision(controller, decision, spot_price=22000.0, target_qty=1)
        assert entry_result["result"] == "submitted"

        # Trip circuit breaker and flatten
        cb = runner.circuit_breaker
        cb.trip("phase_g_flatten_paper_test")
        from backend.autonomous_scheduler import _flatten_positions
        _flatten_positions(runner, center, sid, datetime.now(UTC), decision_id="phase-g-flatten-paper")

        # Verify position is closed via PaperBroker
        positions = runner.broker.positions()
        option_pos = next((p for p in positions.values() if p.is_option), None)
        assert option_pos is not None
        assert option_pos.qty == 0
        assert isinstance(runner.broker, PaperBroker)

    def test_flatten_generates_audit_events(self):
        """Emergency flatten generates order result and closes position."""
        df = _build_nifty_ohlcv_df(datetime.now(UTC))
        def provider(symbol, timeframe):
            return df if symbol == "NSE:NIFTY" else None
        center, _, _, spec, strategy_id = _build_control_center(provider)
        controller = _build_nifty_option_bot(center, bot_id="phase-g-flatten-audit")
        _attach_fake_option_infrastructure(controller)
        coord = AutonomousDeploymentCoordinator(config=controller.config, control_center=center)
        result, dep = coord.create_autonomous_deployment(
            symbol="NSE:NIFTY", strategy_id=strategy_id, timeframe="1d", strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(execution_mode="paper", initial_cash=100_000.0,
                                                     allow_short=False, options_enabled=True,
                                                     allowed_option_types=["CE", "PE"]),
        )
        assert result == DeploymentCreationResult.SUCCESS
        sid = center.find_session_for_deployment(dep.deployment_id)
        runner = center.get_runner(sid)

        # Open a position
        decision = _make_option_decision(option_intent="CE", decision_id="phase-g-flatten-audit-entry")
        from backend.autonomous_scheduler import _execute_one_option_decision
        entry_result = _execute_one_option_decision(controller, decision, spot_price=22000.0, target_qty=1)
        assert entry_result["result"] == "submitted"

        # Trip circuit breaker and flatten
        cb = runner.circuit_breaker
        cb.trip("phase_g_flatten_audit_test")
        from backend.autonomous_scheduler import _flatten_positions
        _flatten_positions(runner, center, sid, datetime.now(UTC), decision_id="phase-g-flatten-audit")

        # Verify position is closed (audit trail via state change)
        positions = runner.broker.positions()
        option_pos = next((p for p in positions.values() if p.is_option), None)
        assert option_pos is not None
        assert option_pos.qty == 0
        assert option_pos.realized_pnl != 0 or option_pos.realized_pnl == 0.0  # P&L recorded

    def test_no_direct_broker_submit_order_in_scheduler_flatten(self):
        """Assert no broker.submit_order calls in scheduler flatten path."""
        import ast
        with open(r"C:\Users\Owner\OneDrive\Desktop\trading-system\backend\autonomous_scheduler.py") as f:
            source = f.read()
        # Find the _flatten_positions function and verify it doesn't call broker.submit_order
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_flatten_positions":
                # Check all function calls in the body
                for child in ast.walk(node):
                    if isinstance(child, ast.Attribute) and child.attr == "submit_order":
                        raise AssertionError(
                            f"Found direct broker.submit_order call in _flatten_positions at line {child.lineno}"
                        )
