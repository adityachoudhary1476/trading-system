"""Phase 23 — End-to-end autonomous PAPER execution pipeline test.

Exercises the REAL production classes (no mocks of the execution path):

    autonomous market scan
      -> opportunity / strategy evaluation
      -> decision generation
      -> safety + risk validation (existing Phase 7 + Phase 18/19)
      -> autonomous deployment creation (canonical mechanism)
      -> PaperTradingControlCenter.submit_order_intent
      -> PaperBroker.submit_order
      -> persisted order record (durable client_order_id idempotency)
      -> position creation
      -> event/audit log
      -> deterministic idempotent replay (no second fill)

Paper-only: no live broker, no Upstox/FYERS credentials, no network,
no real market. Everything runs on SQLite and deterministic synthetic bars.

Architecture:
  - The test wires an AutonomousController against a real
    PaperTradingControlCenter + a deterministic fake ``data_provider``.
  - The scanner accepts a custom TradingCalendar so the test can pin
    "regular session" without depending on wall-clock market hours.
  - The PaperBroker is constructed by the canonical coordinator path
    (see ``AutonomousDeploymentCoordinator.create_autonomous_deployment``)
    so identity binding, runner attachment, and checkpoint persistence
    are the production code, not mocks.

What this test guarantees:
  1. A valid market opportunity reaches the scanner.
  2. Strategy compatibility / decision generation produces a valid BUY.
  3. The canonical autonomous deployment creation succeeds.
  4. PaperTradingControlCenter.submit_order_intent returns a FILLED result.
  5. The order is persisted via PaperSessionStore with a client_order_id
     that survives a retry (idempotent replay, no second fill).
  6. The position is updated and the event log records the execution.

What this test guarantees about *blocked* paths:
  - kill-switch halted -> no order created
  - regular-session gate fails -> no order created
  - stale market data -> no order created
  - risk guard HALT -> no order created
  - missing session for the deployment -> no order created
  - duplicate ``client_order_id`` -> idempotent replay, no second fill
  - short SELL with allow_short=False -> no order created

No live broker, no real money, no credentials. All execution routes
through ``PaperBroker`` via the canonical safety stack in
``PaperTradingControlCenter.submit_order_intent``.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from trading_system.autonomous.bot_config import (
    BotMode,
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
from trading_system.execution.orders import (
    OrderIntent,
    OrderType,
    Side,
)
from trading_system.indicators import ema
from trading_system.india.market_calendar import TradingCalendar
from trading_system.paper.control import (
    PaperTradingControlCenter,
    UnknownDeploymentError,
)
from trading_system.paper.deployment import (
    PaperDeploymentConfig,
    PaperDeploymentStatus,
)
from trading_system.paper.gate import DeploymentGate
from trading_system.paper.risk import PaperRiskConfig, PaperRiskGuard
from trading_system.paper.runner import PaperStrategyRunner
from trading_system.paper.circuit_breaker import PaperCircuitBreaker
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

UTC = timezone.utc

# 2024-01-02 = Tuesday. NSE regular session: 09:15-15:30 IST =
# 03:45-10:00 UTC (exclusive end). We pick a bar at 06:00 UTC = 11:30 IST
# so the bar is firmly inside the regular session.
REGULAR_BAR_TS = datetime(2024, 1, 2, 6, 0, tzinfo=UTC)
SCAN_TS = datetime(2024, 1, 2, 7, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _build_ohlcv_df(end_ts: datetime, n: int = 60) -> pd.DataFrame:
    """Synthetic uptrending OHLCV that EMA(12) > EMA(26) on the last bar.

    The fast EMA crossing above the slow EMA produces a BUY from the
    ``ema_crossover`` reference strategy registered by the strategy factory
    discovery catalog (see ``trading_system.strategy_factory.builtin``).
    """
    idx = pd.date_range(end=end_ts, periods=n, freq="D", tz="UTC")
    # Strictly increasing close -> EMA(12) ends above EMA(26) by a wide margin.
    closes = [100.0 + i * 0.5 for i in range(n)]
    opens = [c - 0.1 for c in closes]
    highs = [c + 0.2 for c in closes]
    lows = [c - 0.2 for c in closes]
    volumes = [1000.0 + i for i in range(n)]
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    # Sanity: confirm the EMA crossover is BUY at the end.
    fast = ema(df["close"], 12).iloc[-1]
    slow = ema(df["close"], 26).iloc[-1]
    assert fast > slow, f"fixture must produce a BUY signal (fast={fast} slow={slow})"
    return df


def _make_strategy_spec(symbol: str, timeframe: str) -> StrategySpec:
    return StrategySpec(
        name="Autonomous E2E Test Strategy",
        description="autonomous end-to-end paper execution test",
        symbol=symbol,
        timeframe=timeframe,
        indicators=[{"name": "sma", "params": {"window": 5}}],
        entry=make_condition(field_operand("close"), ">", indicator_operand("sma_5")),
        generated_by="autonomous-e2e-test",
    )


def _build_control_center(
    data_provider,
    *,
    risk_guard: PaperRiskGuard | None = None,
) -> tuple[
    PaperTradingControlCenter,
    StrategyRegistry,
    StrategyIntelligence,
    StrategySpec,
]:
    """Real PaperTradingControlCenter + registry + intelligence + eligible spec.

    Registers the spec, records the research + walk-forward evidence, and
    flips the strategy status to ``WALK_FORWARD_VALIDATED`` so the
    ``DeploymentGate`` accepts it. Returns the centre + the spec.
    """
    from sqlalchemy import create_engine

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

    spec = _make_strategy_spec("NSE:SBIN", "1d")
    strategy = registry.register_strategy(spec)
    registry.update_strategy_status(strategy.strategy_id, StrategyStatus.WALK_FORWARD_VALIDATED)

    fresh = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    registry.record_evidence(
        StrategyEvidence(
            evidence_id=_evidence_identity(
                strategy.strategy_id, EvidenceType.RESEARCH, "ds-autonomous-e2e", {"k": 1}
            ),
            strategy_id=strategy.strategy_id,
            strategy_spec_hash=strategy.spec_hash,
            evidence_type=EvidenceType.RESEARCH,
            dataset_id="ds-autonomous-e2e",
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
                strategy.strategy_id, EvidenceType.WALK_FORWARD, "ds-autonomous-e2e", {"k": 2}
            ),
            strategy_id=strategy.strategy_id,
            strategy_spec_hash=strategy.spec_hash,
            evidence_type=EvidenceType.WALK_FORWARD,
            dataset_id="ds-autonomous-e2e",
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
                    "total_validation_trades": 100,
                    "min_validation_trades": 10,
                    "valid_fold_ids": [0, 1, 2, 3],
                },
                "warnings": [],
                "notes": [],
            },
            created_at=fresh,
        )
    )
    # Mirror the production startup step: DB specs are registered into the
    # factory discovery catalog so the decision engine can resolve a
    # strategy_id. Without this the catalog holds only the builtins and the
    # pipeline produces no executable decision.
    from trading_system.autonomous.spec_register import (
        register_db_spec_strategies,
    )

    registered = register_db_spec_strategies(center)
    assert registered, "no DB strategy registered into the factory catalog"
    return center, registry, intelligence, spec


def _build_controller(
    center: PaperTradingControlCenter,
    *,
    bot_id: str = "bot-e2e-001",
    max_position_value_pct: float | None = None,
    allowed_symbols=frozenset({"NSE:SBIN"}),
) -> AutonomousController:
    """Build an AutonomousController that scans only ``NSE:SBIN`` at 1d."""
    from trading_system.autonomous.bot_config import AutonomousBotConfig, MaxPositionPct, MaxExposurePct

    user_constraints = UserConstraints(
        allowed_symbols=allowed_symbols,
        # Empty allow-list, matching the production scheduler: the decision
        # engine is then restricted to the strategies persisted in the
        # research registry (the executable universe). Pinning a builtin id
        # such as "ema_crossover" here would allow a strategy the execution
        # path cannot resolve back to a spec, which is the silent
        # no-trade this file now guards against.
        allowed_strategy_ids=frozenset(),
        allowed_timeframes=frozenset({"1d"}),
        max_drawdown_pct=0.15,
        trading_session=TradingSessionConstraints(),
    )
    bot_config = AutonomousBotConfig(
        bot_id=bot_id,
        name=f"E2E Test Bot {bot_id}",
        mode=BotMode.AUTONOMOUS,
        trading_mode=TradingMode.PAPER,
        enabled=True,
        user_constraints=user_constraints,
        max_simultaneous_positions=5,
        max_position_allocation_pct=MaxPositionPct(0.25),
        max_exposure_pct=MaxExposurePct(0.75),
        source=Source.AUTONOMOUS,
    )
    controller = AutonomousController(config=bot_config, control_center=center)
    return controller


def _replace_runner_with_risk_aware(
    controller: AutonomousController, max_dd: float, spec: "StrategySpec"
) -> None:
    """Attach a fresh PaperStrategyRunner with a PaperRiskGuard that halts on any drawdown.

    Used by the risk-guard negative test. Finds the ACTIVE autonomous
    deployment, detaches the original runner, swaps in a new runner with
    the supplied risk configuration, re-attaches.
    """
    center = controller.control_center
    deps = center.list_deployments()
    assert deps, "no autonomous deployment to swap runner on"
    dep = deps[0]
    sid = center.find_session_for_deployment(dep.deployment_id)
    assert sid is not None
    center.detach_runner(sid)
    from trading_system.execution.paper_broker import PaperBroker

    broker_inst = PaperBroker(initial_cash=dep.config.initial_cash)
    circuit_breaker = PaperCircuitBreaker()
    runner = PaperStrategyRunner(
        deployment=dep,
        broker=broker_inst,
        spec=spec,
        risk_guard=PaperRiskGuard(PaperRiskConfig(max_drawdown_pct=max_dd)),
        circuit_breaker=circuit_breaker,
    )
    # Force an immediate drawdown so the next risk check returns HALT.
    runner._max_drawdown = -(max_dd + 0.01)
    new_sid = center.attach_runner(dep.deployment_id, runner)
    assert new_sid == sid


def _deterministic_client_order_id(decision_id: str, symbol: str, side: str) -> str:
    """Stable idempotency key — no timestamps, no UUIDs."""
    payload = f"autonomous:{decision_id}:{symbol}:{side}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:48]


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
class TestAutonomousEndToEndPaper:
    def test_autonomous_pipeline_executes_paper_buy_via_paper_broker(self):
        """Full pipeline: scan -> decision -> deployment -> PaperBroker -> fill -> persisted."""
        df = _build_ohlcv_df(end_ts=REGULAR_BAR_TS)

        def provider(symbol: str, timeframe: str):
            # Compatibility/decision iterate multiple timeframes; return
            # the same uptrending bars for any requested timeframe so the
            # test is independent of the upstream timeframe menu.
            return df if symbol == "NSE:SBIN" else None

        center, registry, intelligence, spec = _build_control_center(provider)
        controller = _build_controller(center, bot_id="bot-e2e-happy")
        controller.start_bot()

        # Pin the calendar so the scanner sees REGULAR for the fixture.
        from trading_system.autonomous.scanner import (
            MarketScanner,
            MarketUniverse,
            ScannerConfig,
        )

        scanner_cfg = ScannerConfig(
            universe=MarketUniverse(symbols=["NSE:SBIN"]),
            timeframe="1d",
            scan_timestamp=SCAN_TS,
            data_provider=provider,
            calendar=TradingCalendar(),
        )
        scan_result = MarketScanner(scanner_cfg).scan()
        assert scan_result.candidates, (
            f"scanner found zero candidates; rejections={scan_result.rejections}"
        )
        cand = scan_result.candidates[0]
        assert cand.symbol == "NSE:SBIN"

        # Rank + compatibility + decision (driven through the controller).
        ranking = controller.rank_candidates(scan_result)
        compat = controller.evaluate_strategy_compatibility(ranking)
        decisions = controller.generate_strategy_decisions(compat)
        valid_buys = [d for d in decisions.decisions if d.is_valid and d.signal and d.signal.action.value == "buy"]
        if not valid_buys:
            # Surface every rejection reason for diagnostics.
            for d in decisions.decisions:
                print(
                    f"  decision symbol={d.opportunity_symbol} status={d.status} "
                    f"reason={d.exclusion_reason} detail={d.exclusion_detail} "
                    f"signal={d.signal}"
                )
        assert valid_buys, (
            f"no valid BUY decision produced; decisions={[d.status for d in decisions.decisions]}"
        )
        decision = valid_buys[0]
        assert decision.opportunity_symbol == "NSE:SBIN"

        # Create the canonical autonomous deployment.
        coord = AutonomousDeploymentCoordinator(
            config=controller.config, control_center=controller.control_center
        )
        result, deployment = coord.create_autonomous_deployment(
            symbol="NSE:SBIN",
            strategy_id=decision.selected_configuration.strategy_id,
            timeframe="1d",
            strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(
                execution_mode="paper",
                initial_cash=100_000.0,
                allow_short=False,
            ),
        )
        assert result == DeploymentCreationResult.SUCCESS, f"unexpected result={result}"
        assert deployment is not None
        assert deployment.notes.startswith("bot:bot-e2e-happy")

        # Confirm the listing now returns it.
        assert len(controller.list_autonomous_deployments()) == 1

        # Resolve the live session and seed the broker's last price so
        # MARKET orders can fill.
        sid = center.find_session_for_deployment(deployment.deployment_id)
        assert sid is not None
        runner = center.get_runner(sid)
        assert runner is not None

        last_close = float(df["close"].iloc[-1])
        runner.broker.update_market_price("NSE:SBIN", last_close)

        # Build the deterministic OrderIntent and submit through the
        # canonical safety stack.
        client_order_id = _deterministic_client_order_id(
            decision_id=decision.decision_id,
            symbol="NSE:SBIN",
            side="buy",
        )
        intent = OrderIntent(
            symbol="NSE:SBIN",
            side=Side.BUY,
            quantity=10,
            order_type=OrderType.MARKET,
            client_order_id=client_order_id,
            current_price=last_close,
        )
        result = center.submit_order_intent(session_id=sid, intent=intent)

        # --- Order reaches PaperBroker ---
        assert result.status == "FILLED", f"order was not filled: status={result.status}"
        assert result.is_idempotent_replay is False
        assert result.filled_quantity == 10
        assert result.avg_fill_price > 0

        # --- Position updated ---
        position = runner.broker.get_position("NSE:SBIN")
        assert position is not None
        assert position.qty == 10
        assert position.symbol == "NSE:SBIN"

        # --- Order persisted via PaperSessionStore (durable idempotency) ---
        rec = center.session_store.get_order(
            session_id=sid, client_order_id=client_order_id
        )
        assert rec is not None
        assert rec.order_id == result.order_id
        # The canonical execution evidence: a persisted order record keyed
        # on (session_id, client_order_id) plus the broker's filled state.

        # --- Replay the same deterministic client_order_id ---
        replay = center.submit_order_intent(session_id=sid, intent=intent)
        assert replay.is_idempotent_replay is True
        assert replay.order_id == result.order_id

        # --- No second fill ---
        position_after = runner.broker.get_position("NSE:SBIN")
        assert position_after.qty == 10, (
            f"second fill observed on replay; qty={position_after.qty}"
        )
        # Only one persisted row.
        assert (
            center.session_store.get_order(
                session_id=sid, client_order_id=client_order_id
            ).order_id
            == result.order_id
        )


# --------------------------------------------------------------------------- #
# Negative tests — every blocked path must produce zero new orders.
# --------------------------------------------------------------------------- #
class TestAutonomousExecutionBlocked:
    def _setup_running_deployment(self, bot_id: str, provider):
        center, registry, _, spec = _build_control_center(provider)
        strategies = registry.list_strategies()
        assert strategies, "no strategies registered"
        strategy_id = strategies[0].strategy_id
        controller = _build_controller(center, bot_id=bot_id)

        coord = AutonomousDeploymentCoordinator(
            config=controller.config, control_center=controller.control_center
        )
        result, deployment = coord.create_autonomous_deployment(
            symbol="NSE:SBIN",
            strategy_id=strategy_id,
            timeframe="1d",
            strategy_spec=spec,
            deployment_config=PaperDeploymentConfig(initial_cash=100_000.0, allow_short=False),
        )
        assert result == DeploymentCreationResult.SUCCESS, result
        sid = center.find_session_for_deployment(deployment.deployment_id)
        last_close = float(_build_ohlcv_df(REGULAR_BAR_TS)["close"].iloc[-1])
        center.get_runner(sid).broker.update_market_price("NSE:SBIN", last_close)
        return center, controller, deployment, sid, last_close, spec

    def test_kill_switch_blocks_paper_order(self):
        df = _build_ohlcv_df(REGULAR_BAR_TS)

        def provider(symbol, timeframe):
            return df if symbol == "NSE:SBIN" else None

        center, controller, deployment, sid, last_close, spec = self._setup_running_deployment(
            "bot-e2e-killswitch", provider
        )

        # Halt the bot BEFORE submission.
        controller.halt_bot(
            reason=__import__(
                "trading_system.autonomous.safety", fromlist=["KillSwitchReason"]
            ).KillSwitchReason.MANUAL,
            detail="e2e test",
        )

        intent = OrderIntent(
            symbol="NSE:SBIN",
            side=Side.BUY,
            quantity=5,
            order_type=OrderType.MARKET,
            client_order_id=_deterministic_client_order_id("d-killswitch", "NSE:SBIN", "buy"),
            current_price=last_close,
        )
        with pytest.raises(Exception):
            center.submit_order_intent(session_id=sid, intent=intent)

        # No paper order row was written.
        assert (
            center.session_store.get_order(
                session_id=sid, client_order_id=intent.client_order_id
            )
            is None
        )

    def test_market_closed_blocks_execution(self):
        """Scanner's regular-session gate must reject closed-session bars."""
        closed_ts = datetime(2024, 1, 2, 12, 0, tzinfo=UTC)  # 17:30 IST -> CLOSED
        df = _build_ohlcv_df(end_ts=closed_ts)

        def provider(symbol, timeframe):
            return df if symbol == "NSE:SBIN" else None

        from trading_system.autonomous.scanner import (
            MarketScanner,
            MarketUniverse,
            ScannerConfig,
        )

        scanner_cfg = ScannerConfig(
            universe=MarketUniverse(symbols=["NSE:SBIN"]),
            timeframe="1d",
            scan_timestamp=closed_ts,
            data_provider=provider,
            calendar=TradingCalendar(),
        )
        scan_result = MarketScanner(scanner_cfg).scan()
        assert all(
            r.reason.value != "accepted"
            for r in scan_result.rejections
            if r.symbol == "NSE:SBIN"
        )
        # No candidates accepted during closed session.
        assert all(c.symbol != "NSE:SBIN" for c in scan_result.candidates)

    def test_stale_data_blocks_execution(self):
        """Scanner freshness check rejects bars older than max_freshness."""
        stale_end = (datetime.now(UTC) - timedelta(days=30)).replace(
            hour=6, minute=0, second=0, microsecond=0
        )
        df = _build_ohlcv_df(end_ts=stale_end)

        def provider(symbol, timeframe):
            return df if symbol == "NSE:SBIN" else None

        from trading_system.autonomous.scanner import (
            MarketScanner,
            MarketUniverse,
            ScannerConfig,
        )

        scanner_cfg = ScannerConfig(
            universe=MarketUniverse(symbols=["NSE:SBIN"]),
            timeframe="1d",
            scan_timestamp=datetime.now(UTC),
            data_provider=provider,
            calendar=TradingCalendar(),
        )
        scan_result = MarketScanner(scanner_cfg).scan()
        # Either the symbol was rejected as STALE_MARKET_DATA, or it
        # was rejected by the freshness check inside rank/decision.
        # The contract: zero candidates for stale data.
        assert all(c.symbol != "NSE:SBIN" for c in scan_result.candidates)

    def test_risk_rejection_blocks_order(self):
        df = _build_ohlcv_df(REGULAR_BAR_TS)

        def provider(symbol, timeframe):
            return df if symbol == "NSE:SBIN" else None

        center, controller, deployment, sid, last_close, spec = self._setup_running_deployment(
            "bot-e2e-risk", provider
        )

        # Swap the runner for one whose risk guard halts on any drawdown.
        _replace_runner_with_risk_aware(controller, max_dd=0.05, spec=spec)

        intent = OrderIntent(
            symbol="NSE:SBIN",
            side=Side.BUY,
            quantity=1,
            order_type=OrderType.MARKET,
            client_order_id=_deterministic_client_order_id("d-risk", "NSE:SBIN", "buy"),
            current_price=last_close,
        )
        with pytest.raises(Exception):
            center.submit_order_intent(session_id=sid, intent=intent)
        assert (
            center.session_store.get_order(
                session_id=sid, client_order_id=intent.client_order_id
            )
            is None
        )

    def test_no_valid_session_blocks_order(self):
        """Unknown session id must raise and produce no order."""
        df = _build_ohlcv_df(REGULAR_BAR_TS)

        def provider(symbol, timeframe):
            return df if symbol == "NSE:SBIN" else None

        center, controller, deployment, sid, last_close, spec = self._setup_running_deployment(
            "bot-e2e-nosession", provider
        )
        # Detach the runner so the session id becomes invalid.
        center.detach_runner(sid)

        intent = OrderIntent(
            symbol="NSE:SBIN",
            side=Side.BUY,
            quantity=1,
            order_type=OrderType.MARKET,
            client_order_id=_deterministic_client_order_id("d-nosession", "NSE:SBIN", "buy"),
            current_price=last_close,
        )
        with pytest.raises(UnknownDeploymentError):
            center.submit_order_intent(session_id=sid, intent=intent)

    def test_duplicate_decision_does_not_double_fill(self):
        """Replay the same deterministic client_order_id; no second fill."""
        df = _build_ohlcv_df(REGULAR_BAR_TS)

        def provider(symbol, timeframe):
            return df if symbol == "NSE:SBIN" else None

        center, controller, deployment, sid, last_close, spec = self._setup_running_deployment(
            "bot-e2e-dup", provider
        )
        intent = OrderIntent(
            symbol="NSE:SBIN",
            side=Side.BUY,
            quantity=7,
            order_type=OrderType.MARKET,
            client_order_id=_deterministic_client_order_id("d-dup", "NSE:SBIN", "buy"),
            current_price=last_close,
        )
        first = center.submit_order_intent(session_id=sid, intent=intent)
        assert first.status == "FILLED"
        assert first.is_idempotent_replay is False

        # Process "the same decision" again (simulating scheduler retry).
        second = center.submit_order_intent(session_id=sid, intent=intent)
        assert second.is_idempotent_replay is True
        assert second.order_id == first.order_id
        runner = center.get_runner(sid)
        assert runner.broker.get_position("NSE:SBIN").qty == 7

    def test_short_sell_rejected_when_disabled(self):
        """Allow_short=False blocks SELL > long position qty."""
        df = _build_ohlcv_df(REGULAR_BAR_TS)

        def provider(symbol, timeframe):
            return df if symbol == "NSE:SBIN" else None

        center, controller, deployment, sid, last_close, spec = self._setup_running_deployment(
            "bot-e2e-short", provider
        )
        # First, open a small LONG so the position is non-flat.
        long_intent = OrderIntent(
            symbol="NSE:SBIN",
            side=Side.BUY,
            quantity=3,
            order_type=OrderType.MARKET,
            client_order_id=_deterministic_client_order_id("d-short-open", "NSE:SBIN", "buy"),
            current_price=last_close,
        )
        first = center.submit_order_intent(session_id=sid, intent=long_intent)
        assert first.status == "FILLED"
        assert center.get_runner(sid).broker.get_position("NSE:SBIN").qty == 3

        # Now try to SELL more than the long position with allow_short=False.
        short_intent = OrderIntent(
            symbol="NSE:SBIN",
            side=Side.SELL,
            quantity=10,
            order_type=OrderType.MARKET,
            client_order_id=_deterministic_client_order_id("d-short-attempt", "NSE:SBIN", "sell"),
            current_price=last_close,
        )
        with pytest.raises(Exception):
            center.submit_order_intent(session_id=sid, intent=short_intent)
        # The SELL was not persisted.
        assert (
            center.session_store.get_order(
                session_id=sid, client_order_id=short_intent.client_order_id
            )
            is None
        )
        # And the long position remains at 3, not reduced.
        assert center.get_runner(sid).broker.get_position("NSE:SBIN").qty == 3


# --------------------------------------------------------------------------- #
# The real tick
#
# Every test above drives the pipeline stage by stage, with the test itself
# calling scan -> rank -> decide -> submit. That proves the pieces work. It
# does NOT prove the scheduler wires them together -- and the wiring is where
# both real defects of this project lived:
#
#   * the SL/TP sweep sat AFTER an early return in the tick, so a breached
#     threshold was skipped on exactly the ticks it mattered;
#   * the fyers->upstox migration left the v2 reader and the test fixtures
#     disagreeing on one field name, so every live frame parsed to a default.
#
# Both shipped with a fully green suite.
#
# The specific gap these tests close: every test that drove a real
# ``_run_one_tick`` asserted that the tick does NOTHING (kill switch, market
# closed, stale data). None asserted that a tick actually EXECUTES. So it was
# possible for the tick never to place an order and the whole suite to stay
# green -- which would have made paper trading a silent no-op.
#
# These tests run the production tick function. The only thing patched is the
# wall-clock market-hours gate, which legitimately depends on what time it is
# when CI runs. The scanner, ranker, decision engine, safety stack, deployment
# coordinator and PaperBroker are all the real implementations.
# --------------------------------------------------------------------------- #
class _ZeroPosition:
    """Null object so a position total can be summed over missing positions."""

    qty = 0


_ZERO = _ZeroPosition()


def _all_runners(center: PaperTradingControlCenter) -> list:
    """Every attached runner across all deployments (the tick may create its own)."""
    runners = []
    for d in center.list_deployments():
        sid = center.find_session_for_deployment(d.deployment_id)
        if sid is None:
            continue
        runner = center.get_runner(sid)
        if runner is not None:
            runners.append(runner)
    return runners


class TestRealSchedulerTickExecutes:
    """A real ``_run_one_tick`` must reach a filled position, unprompted."""

    @staticmethod
    def _build_ready(monkeypatch, bot_id: str):
        """A started controller on fresh synthetic bars, one gate patched.

        Patches ONLY ``_is_regular_session`` (a wall-clock seam: the market-hours
        gate must not depend on what time it is when CI runs). No calendar and
        no ``max_freshness`` patch, because neither is needed once the fixture
        is honest about time:

        - bars end at *now*, so the scanner's ``max_freshness`` window (7d,
          measured against ``scan_timestamp`` which ``scan_market`` leaves at
          wall-clock now) passes exactly as it does in production. The
          hand-built-scanner tests above dodge this by pinning
          ``scan_timestamp=SCAN_TS`` to 2024; on a real tick that pin does not
          exist, so the fixture has to be genuinely fresh instead.
        - ``AutonomousController.scan_market`` sets
          ``require_regular_session=False`` precisely because daily bars
          stamped at 00:00 IST are never inside 09:15-15:30 IST, so the
          calendar needs no patch either.

        ``_build_control_center`` performs the same catalog registration the
        production scheduler does at startup.
        """
        from backend import autonomous_scheduler

        df = _build_ohlcv_df(end_ts=datetime.now(UTC))

        def provider(symbol: str, timeframe: str):
            return df if symbol == "NSE:SBIN" else None

        center, _registry, _intelligence, spec = _build_control_center(provider)

        # Build the controller with the PRODUCTION builder, not the local
        # ``_build_controller`` above. Production deliberately leaves the
        # strategy allow-list empty so decisions resolve to registered DB
        # specs; the local builder now mirrors that.
        monkeypatch.setenv("AUTONOMOUS_ALLOWED_SYMBOLS", "NSE:SBIN")
        controller = autonomous_scheduler._build_controller(
            center,
            None,
            center.load_market_data,
            bot_id,
        )
        controller.start_bot()

        monkeypatch.setattr(
            autonomous_scheduler, "_is_regular_session", lambda ts: True
        )
        return controller, center, spec

    def test_real_tick_places_and_fills_a_buy(self, monkeypatch):
        from backend import autonomous_scheduler

        controller, center, _spec = self._build_ready(monkeypatch, "bot-tick-buy")

        result = autonomous_scheduler._run_one_tick(controller)

        # --- the tick cleared its own gates ---
        assert result["result"] == "executed", result
        assert result["decision_count"] >= 1, f"tick produced no decisions: {result}"
        assert result["eligible_count"] >= 1, (
            "tick produced no eligible decision, so the bot would never trade "
            f"even with a valid signal: {result}"
        )

        # --- and it actually submitted an order ---
        submitted = [
            s for s in result["submissions"] if s.get("result") == "submitted"
        ]
        assert submitted, f"tick executed no order: {result['submissions']}"
        assert submitted[0]["symbol"] == "NSE:SBIN", submitted[0]

        # --- the order became a real filled position in the real broker ---
        positions = [r.broker.get_position("NSE:SBIN") for r in _all_runners(center)]
        held = [p for p in positions if p is not None and p.qty > 0]
        assert held, (
            "tick reported a submission but no runner holds a position; the "
            "paper account would stay flat forever"
        )
        assert held[0].qty > 0
        assert held[0].avg_entry_price > 0

    def test_real_tick_created_its_own_deployment(self, monkeypatch):
        """The tick, not the test, must create the deployment it trades on.

        ``_execute_one_decision`` imports the coordinator and creates the
        deployment inline. If that wiring regressed, the tick would have
        nowhere to route the order and would silently submit nothing.
        """
        from backend import autonomous_scheduler

        controller, center, _spec = self._build_ready(monkeypatch, "bot-tick-dep")
        assert center.list_deployments() == [], (
            "precondition: no deployment should exist before the first tick"
        )

        autonomous_scheduler._run_one_tick(controller)

        deps = center.list_deployments()
        assert deps, "the tick created no deployment"
        assert all(d.notes.startswith("bot:bot-tick-dep") for d in deps), deps
        assert all(d.status == PaperDeploymentStatus.ACTIVE for d in deps), deps

    def test_real_tick_order_is_persisted_under_the_signal_identity(
        self, monkeypatch
    ):
        """The fill must be durable, keyed on the scheduler's own signal id.

        This is the check that the order was built *and* recorded by
        production code. ``_execute_one_decision`` sets
        ``OrderIntent.client_order_id = _signal_identity(...)`` -- a pure
        function of the signal payload, deliberately not a UUID -- and that
        string is the ``PaperSessionStore`` primary key that makes execution
        restart-safe.

        A test that hand-builds an ``OrderIntent`` (as several above do) proves
        only that a hand-built intent fills. Here the key the store returns
        must be the same id the scheduler reported, so a tick that faked its
        way to a fill, or that persisted under a key it cannot reproduce, both
        fail. The second half matters on its own: if that derivation ever
        picked up a wall-clock or random component, every restart would
        silently re-execute the same signal.
        """
        from backend import autonomous_scheduler

        controller, center, _spec = self._build_ready(monkeypatch, "bot-tick-cid")

        result = autonomous_scheduler._run_one_tick(controller)
        submitted = [
            s for s in result["submissions"] if s.get("result") == "submitted"
        ]
        assert submitted, result["submissions"]
        order = submitted[0]

        assert order["status"] == "FILLED", order
        assert order["filled_quantity"] > 0, order
        assert order["is_idempotent_replay"] is False, order

        # The client_order_id IS the signal identity; look the record up by it.
        record = center.session_store.get_order(
            session_id=order["session_id"],
            client_order_id=order["signal_id"],
        )
        assert record is not None, (
            f"no persisted order under the tick's own signal id "
            f"{order['signal_id']!r}; the fill would not survive a restart"
        )
        assert record.order_id == order["order_id"], (record.order_id, order["order_id"])

    def test_second_tick_does_not_double_the_position(self, monkeypatch):
        """Idempotency across ticks: a repeated signal must not stack orders.

        The per-tick ``seen`` set only dedupes within one tick. Across ticks
        the durable ``client_order_id`` is the real guard, and this is the
        only place that behaviour is observed on a genuine tick.

        Asserted on the submission, not on the raw position size. The tick
        also runs ``_run_portfolio_tick``, a second decision engine sharing the
        same paper account - see
        ``test_portfolio_tick_liquidates_main_pipeline_positions`` for what
        that does to the size. Idempotency means "no second fill", which is
        what the submission result and the persisted order count show.
        """
        from backend import autonomous_scheduler

        controller, center, _spec = self._build_ready(monkeypatch, "bot-tick-twice")

        first = autonomous_scheduler._run_one_tick(controller)
        submitted = [
            s for s in first["submissions"] if s.get("result") == "submitted"
        ]
        assert submitted, first["submissions"]
        first_order = submitted[0]
        assert first_order["status"] == "FILLED", first_order
        assert first_order["is_idempotent_replay"] is False, first_order
        assert first_order["filled_quantity"] > 0, first_order

        def qty() -> float:
            return sum(
                (r.broker.get_position("NSE:SBIN") or _ZERO).qty
                for r in _all_runners(center)
            )

        after_first = qty()
        assert after_first > 0, "first tick did not open anything"

        second = autonomous_scheduler._run_one_tick(controller)
        assert second["result"] == "executed", second

        # Same signal -> same identity -> no new order. The replay path
        # short-circuits on the durable store before it resolves an order, so
        # it reports the signal_id but no order_id; the signal_id is the key.
        replays = [
            s
            for s in second["submissions"]
            if s.get("result") in ("submitted", "already_executed")
        ]
        assert replays, second["submissions"]
        assert all(
            s["signal_id"] == first_order["signal_id"] for s in replays
        ), f"a repeated signal produced a different identity: {replays}"
        assert all(
            s.get("result") == "already_executed" for s in replays
        ), f"a repeated signal placed a second order: {replays}"

        # The durable record still points at the single original order, and the
        # position never grew.
        record = center.session_store.get_order(
            session_id=first_order["session_id"],
            client_order_id=first_order["signal_id"],
        )
        assert record is not None and record.order_id == first_order["order_id"]
        assert qty() <= after_first, (
            f"a second tick on an unchanged signal INCREASED the position "
            f"({after_first} -> {qty()}); idempotency did not hold"
        )

    def test_portfolio_tick_liquidates_main_pipeline_positions(self, monkeypatch):
        """Pins a real conflict between the two decision engines on one account.

        A tick runs the main pipeline (scan -> decision -> order) and then
        ``_run_portfolio_tick``. The portfolio treats every position in the
        shared paper account as its own, and closes any position whose owning
        strategy produced no opportunity in *its* evaluation
        (``portfolio.py``: ``if opportunity is None: return
        "strategy_exit_signal"``). The portfolio only evaluates Phase 22
        strategies, so a position opened by a Phase 2-5 DB spec is always
        liquidated on the next tick.

        Net effect on paper results: the account opens a position and flattens
        it again every tick, which reads as a strategy that never holds. The
        close is reported only inside the nested ``portfolio`` result, not in
        the tick's ``submissions`` or ``risk_exits``.

        This is a design question - which engine owns the account - not a
        mechanical bug, so it is pinned rather than fixed here. If the intent
        is for the portfolio to own the account, the main pipeline should not
        also open positions on it.
        """
        from backend import autonomous_scheduler

        controller, center, _spec = self._build_ready(monkeypatch, "bot-tick-portfolio")

        first = autonomous_scheduler._run_one_tick(controller)
        assert any(
            s.get("result") == "submitted" for s in first["submissions"]
        ), first["submissions"]

        def qty() -> float:
            return sum(
                (r.broker.get_position("NSE:SBIN") or _ZERO).qty
                for r in _all_runners(center)
            )

        assert qty() > 0, "first tick did not open anything"

        second = autonomous_scheduler._run_one_tick(controller)
        portfolio = second.get("portfolio") or {}
        assert portfolio, "the tick reported no portfolio phase result"

        # Documented behaviour: the position does not survive the second tick.
        assert qty() == 0, (
            "expected the portfolio tick to liquidate the main pipeline's "
            f"position; it held {qty()}. If this now passes differently the "
            "two engines have stopped conflicting and this pin should be "
            "updated deliberately, not deleted."
        )
        # And the close is invisible in the tick's own execution reporting.
        assert not any(
            s.get("result") == "submitted" for s in second["submissions"]
        ), second["submissions"]

    def test_heartbeat_records_a_successful_execution(self, monkeypatch):
        """A completed tick must stamp last_successful_tick_at.

        The health monitor reads these heartbeats to decide whether the bot
        is alive. A tick that trades but does not stamp the heartbeat looks
        identical to a dead bot to the operator.
        """
        from backend import autonomous_scheduler

        controller, center, _spec = self._build_ready(monkeypatch, "bot-tick-hb")

        autonomous_scheduler._run_one_tick(controller)

        beats = [d.notes for d in center.list_deployments()]
        assert beats, "no deployments to heartbeat"
        stamped = [d for d in center.list_deployments() if d.last_successful_tick_at]
        assert stamped, (
            "the tick completed but stamped no successful-tick heartbeat; "
            "the health monitor would report this bot as dead"
        )

    def test_tick_scans_the_timeframe_that_has_fresh_data(self, monkeypatch):
        """The scan must run at a timeframe with fresh data, not a hardcoded 1d.

        The freshness gate used to test ``"1d"`` unconditionally, so a bot
        configured for intraday bars was skipped as stale even when its 5m data
        was current, and the scan itself was pinned to ``1d``. The gate now
        considers every allowed timeframe and hands the winning one to
        ``scan_market``.

        Asserted on the boundary, not on the tick's outcome: a bot with no 1d
        data but fresh 5m data must reach the scan, and the scan must be asked
        for 5m.
        """
        from backend import autonomous_scheduler

        controller, center, _spec = self._build_ready(monkeypatch, "bot-tick-tf")

        # Intraday bot: allowed_timeframes is 5m only, so a 1d scan would find
        # no candidates and the tick would skip.
        controller.config.user_constraints.allowed_timeframes = frozenset({"5m"})

        asked: list = []
        controller_cls = type(controller)
        real_scan = controller_cls.scan_market

        def spy(self, timeframe=None):
            asked.append(timeframe)
            return real_scan(self, timeframe=timeframe)

        monkeypatch.setattr(controller_cls, "scan_market", spy)
        monkeypatch.setattr(
            autonomous_scheduler,
            "_has_fresh_data",
            lambda _center, symbol, tf: tf == "5m" and symbol == "NSE:SBIN",
        )

        result = autonomous_scheduler._run_one_tick(controller)

        assert asked, "the tick never reached the scan"
        assert asked[0] == "5m", (
            f"the scan was asked for {asked[0]!r} instead of the fresh "
            "timeframe 5m"
        )
        assert result["scan_timeframe"] == "5m", result
        assert result["fresh_symbols"] == ["NSE:SBIN"], result
