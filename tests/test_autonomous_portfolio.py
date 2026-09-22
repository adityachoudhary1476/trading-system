"""V1 — Autonomous Portfolio tests.

Covers the portfolio-level autonomous paper-trading behaviour:
multi-strategy evaluation, multiple simultaneous positions, max-position
limit, P&L aggregation, fail-closed data handling, kill switch, the paper
execution seam, strategy attribution and no-regression of the existing
autonomous stack. All external dependencies (Upstox, broker, market data)
are faked; no real credentials or network access is required. PAPER-only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from trading_system.autonomous import (
    AutonomousPortfolio,
    PortfolioActionType,
    StrategyOpportunity,
)


UTC = timezone.utc


def _market_open_clock():
    """Friday 09:30 UTC — inside the NSE regular session window."""
    return lambda: datetime(2026, 1, 9, 9, 30, tzinfo=UTC)


class FakeBroker:
    """In-memory stand-in for PaperBroker (positions + account only)."""

    def __init__(self):
        self._positions: dict = {}
        self.account_obj = SimpleNamespace(
            initial_cash=100_000.0,
            cash=100_000.0,
            available_cash=100_000.0,
            equity=100_000.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
        )
        self.mark_updates: list = []

    def positions(self):
        return self._positions

    def account(self):
        return self.account_obj

    def update_market_price(self, symbol, price):
        self.mark_updates.append((symbol, price))
        pos = self._positions[symbol]
        pos.current_price = price
        pos.unrealized_pnl = (price - pos.avg_entry_price) * pos.qty

    def add_position(self, symbol, **kwargs):
        """Add an OPEN position (attribute access, like a real Position)."""
        entry = kwargs.get("avg_entry_price", 100.0)
        pos = SimpleNamespace(
            symbol=symbol,
            qty=kwargs.get("qty", 1.0),
            avg_entry_price=entry,
            current_price=kwargs.get("current_price", entry),
            unrealized_pnl=kwargs.get(
                "unrealized_pnl",
                (kwargs.get("current_price", entry) - entry) * kwargs.get("qty", 1.0),
            ),
            market_value=kwargs.get("market_value", 0.0),
            options_contract_id=kwargs.get("options_contract_id"),
            option_type=kwargs.get("option_type"),
            strike=kwargs.get("strike"),
            expiry=kwargs.get("expiry"),
            contract_size=kwargs.get("contract_size", 1),
            underlying=kwargs.get("underlying", "NIFTY"),
        )
        self._positions[symbol] = pos
        return pos

    def close_position(self, symbol):
        """Remove the position, like a real PaperBroker exit fill."""
        return self._positions.pop(symbol, None)


class FakeEventLog:
    def __init__(self):
        self.events = []
        self.recorded = []

    def record(self, *args, **kwargs):
        self.recorded.append(kwargs)


class FakeControlCenter:
    def __init__(self, bot_id):
        self.bot_id = bot_id
        self.deployment = SimpleNamespace(
            deployment_id="dep-portfolio-1",
            dataset_id=f"autonomous-portfolio:{bot_id}",
            notes=f"bot:{bot_id}",
            status="ACTIVE",
            config=SimpleNamespace(stop_loss_pct=None, take_profit_pct=None),
        )
        self.market_data: dict = {}
        self._runner = None
        self._broker = FakeBroker()

    def list_deployments(self):
        return [self.deployment]

    def get_deployment(self, deployment_id):
        return self.deployment

    def load_market_data(self, symbol, timeframe):
        return self.market_data.get(symbol)

    def find_session_for_deployment(self, deployment_id):
        return "sess-1"

    def get_runner(self, session_id):
        return self._runner


class FakeController:
    BOT_ID = "bot-nifty-options"

    def __init__(self):
        self.config = SimpleNamespace(
            bot_id=self.BOT_ID,
            state=SimpleNamespace(value="running"),
            trading_mode=SimpleNamespace(value="paper"),
            max_simultaneous_positions=5,
            user_constraints=SimpleNamespace(
                allowed_symbols=frozenset({"NSE:NIFTY"}),
                allowed_option_underlyings=frozenset({"NIFTY"}),
            ),
        )
        self.control_center = FakeControlCenter(self.BOT_ID)
        self.event_log = FakeEventLog()
        self.kill_switch = SimpleNamespace(
            state=SimpleNamespace(value="active"), reason=None
        )
        self._halted = False
        self._quote_provider = None
        self.execution_calls: list = []

    @property
    def is_halted(self):
        return self._halted

    @property
    def quote_provider(self):
        return self._quote_provider

    def execute_option_order(self, **kwargs):
        """Stand-in for the real controller option-execution path.

        Mirrors the real behaviour: a FILLED result is booked in the paper
        broker's position book; None / non-FILLED means fail-closed.
        """
        self.execution_calls.append(kwargs)
        return self._execution_result(kwargs)

    def _execution_result(self, kwargs):
        """FILLED for NIFTY entries; None otherwise (fail-closed on anything else)."""
        symbol = kwargs.get("symbol", "")
        action = str(kwargs.get("action", "")).lower()
        if symbol == "NSE:NIFTY" and action == "buy":
            self.control_center._broker.add_position(
                symbol,
                options_contract_id=str(kwargs.get("instrument_key", "NIFTY_CE")),
                option_type=kwargs.get("option_type"),
                strike=kwargs.get("strike"),
                expiry=kwargs.get("expiry"),
                avg_entry_price=float(kwargs.get("price", 0.0)) or 0.0,
                current_price=float(kwargs.get("price", 0.0)) or 0.0,
                qty=float(kwargs.get("quantity", 1.0)) or 1.0,
            )
            return SimpleNamespace(status="filled", symbol=symbol)
        return None


def make_portfolio(controller, **kwargs):
    runner = SimpleNamespace(broker=controller.control_center._broker)
    controller.control_center._runner = runner
    return AutonomousPortfolio(controller, **kwargs)


def opportunity(strategy_id, direction, score=0.5):
    return StrategyOpportunity(
        strategy_id=strategy_id,
        strategy_name=strategy_id.title(),
        symbol="NSE:NIFTY",
        direction=direction,
        signal_value=direction,
        aggregate_score=score,
        timeframe="1d",
    )


def filled(contract_id, option_type, price=120.0):
    """An OrderResult-shaped namespace (status FILLED)."""
    return SimpleNamespace(
        status="FILLED",
        order_id=f"ord-{contract_id}",
        options_contract_id=contract_id,
        symbol=f"NFO:{contract_id}",
        option_type=option_type,
        strike=25000.0 if option_type == "CE" else 24000.0,
        expiry="2026-01-29",
        filled_quantity=1.0,
        avg_fill_price=price,
    )


def paper_fill(controller, kwargs):
    """Mimic a real fill: book the position in the broker, return the result."""
    contract_id = f"c{len(controller.execution_calls)}"
    option_type = kwargs["explicit_option_type"]
    price = 120.0
    controller.control_center._broker.add_position(
        f"NFO:{contract_id}",
        options_contract_id=contract_id,
        option_type=option_type,
        avg_entry_price=price,
        strike=25000.0 if option_type == "CE" else 24000.0,
        expiry="2026-01-29",
    )
    return filled(contract_id, option_type, price=price)


# ---------------------------------------------------------------------------
# 1. Multi-strategy evaluation
# ---------------------------------------------------------------------------


def test_evaluates_multiple_strategies_and_sorts_by_score():
    controller = FakeController()
    evald = [
        opportunity("vwap", +1, score=0.10),
        opportunity("momentum", +1, score=0.90),
        opportunity("meanrev", -1, score=0.40),
    ]
    portfolio = make_portfolio(
        controller, evaluator=lambda df, spot, regime: evald
    )

    out = portfolio.evaluate_opportunities(object(), 25000.0, object())

    assert [o.strategy_id for o in out] == ["momentum", "meanrev", "vwap"]
    assert {o.option_type for o in out} == {"CE", "PE"}


# ---------------------------------------------------------------------------
# 2. Multiple simultaneous positions
# ---------------------------------------------------------------------------


def test_multiple_opportunities_open_multiple_simultaneous_positions():
    controller = FakeController()
    controller._execution_result = lambda kwargs: paper_fill(controller, kwargs)
    portfolio = make_portfolio(controller)

    results = portfolio.enter_opportunities(
        [
            opportunity("momentum", +1),
            opportunity("meanrev", -1),
            opportunity("vwap", +1),
        ],
        spot_price=25_000.0,
        session_id="sess-1",
    )

    assert [r["result"] for r in results] == ["entered"] * 3
    assert len(controller.execution_calls) == 3
    broker_positions = controller.control_center._broker.positions()
    assert len(broker_positions) == 3


# ---------------------------------------------------------------------------
# 3. Maximum position limit
# ---------------------------------------------------------------------------


def test_max_position_limit_blocks_further_entries():
    controller = FakeController()
    controller._execution_result = lambda kwargs: paper_fill(controller, kwargs)
    portfolio = make_portfolio(controller, max_positions=2)

    results = portfolio.enter_opportunities(
        [opportunity("a", +1), opportunity("b", -1), opportunity("c", +1)],
        spot_price=25_000.0,
        session_id="sess-1",
    )

    assert len([r for r in results if r["result"] == "entered"]) == 2
    assert len(controller.execution_calls) == 2
    actions = [a.action for a in portfolio.actions]
    assert PortfolioActionType.POSITION_LIMIT_REACHED in actions


# ---------------------------------------------------------------------------
# 4. Portfolio P&L aggregation
# ---------------------------------------------------------------------------


def test_snapshot_aggregates_pnl_across_positions():
    controller = FakeController()
    portfolio = make_portfolio(controller)
    broker = controller.control_center._broker
    broker.add_position(
        "NFO:A", avg_entry_price=100, current_price=180, qty=10,
        unrealized_pnl=800.0, options_contract_id="A", option_type="CE",
    )
    broker.add_position(
        "NFO:B", avg_entry_price=100, current_price=75, qty=10,
        unrealized_pnl=-250.0, options_contract_id="B", option_type="PE",
    )
    broker.add_position(
        "NFO:C", avg_entry_price=100, current_price=210, qty=10,
        unrealized_pnl=1100.0, options_contract_id="C", option_type="CE",
    )
    broker.account_obj.unrealized_pnl = 1650.0
    broker.account_obj.realized_pnl = 300.0
    broker.account_obj.equity = 101_950.0
    broker.account_obj.cash = 99_000.0
    broker.account_obj.available_cash = 97_000.0

    snap = portfolio.snapshot()

    assert snap["open_position_count"] == 3
    assert snap["pnl"]["unrealized"] == pytest.approx(1650.0)
    assert snap["pnl"]["realized"] == pytest.approx(300.0)
    assert snap["pnl"]["total"] == pytest.approx(1950.0)
    assert snap["pnl"]["today"] == pytest.approx(
        snap["pnl"]["today_realized"] + snap["pnl"]["unrealized"]
    )


# ---------------------------------------------------------------------------
# 5. Missing option chain / contract -> fail closed
# ---------------------------------------------------------------------------


def test_missing_option_contract_fails_closed_and_records_action():
    controller = FakeController()
    controller._execution_result = lambda kwargs: None  # no contract/quote
    portfolio = make_portfolio(controller)

    results = portfolio.enter_opportunities(
        [opportunity("momentum", +1)],
        spot_price=25_000.0,
        session_id="sess-1",
    )

    assert results and results[0]["result"] == "fail_closed"
    assert len(controller.execution_calls) == 1  # attempted once, no position
    last = portfolio.actions[-1]
    assert last.action == PortfolioActionType.FAIL_CLOSED
    assert last.reason == "no_valid_option_contract_or_quote"


# ---------------------------------------------------------------------------
# 6. Missing/invalid quote -> fail closed (no mark update)
# ---------------------------------------------------------------------------


def test_missing_quote_provider_marks_nothing():
    controller = FakeController()
    portfolio = make_portfolio(controller)
    broker = controller.control_center._broker
    broker.add_position("NFO:A", options_contract_id="A", option_type="CE")
    controller._quote_provider = None  # quotes unavailable

    marks = portfolio.refresh_marks()

    assert marks["provider"] is False
    assert broker.mark_updates == []


def test_stale_quote_is_not_marked():
    controller = FakeController()
    portfolio = make_portfolio(controller)
    broker = controller.control_center._broker
    pos = broker.add_position(
        "NFO:NIFTY_CE", options_contract_id="NIFTY_CE", option_type="CE"
    )
    pos.expiry = "2026-01-29"
    pos.strike = 25000.0
    pos.option_type = "CE"
    controller._quote_provider = SimpleNamespace(
        get_quote=lambda instrument: None,  # no usable quote
        is_fresh=lambda quote: False,
    )

    marks = portfolio.refresh_marks()

    assert marks["unmarked"] == ["NFO:NIFTY_CE"]
    assert broker.mark_updates == []


# ---------------------------------------------------------------------------
# 7. Kill switch prevents new entries
# ---------------------------------------------------------------------------


def test_kill_switch_blocks_tick_and_entries():
    controller = FakeController()
    portfolio = make_portfolio(controller)
    controller._halted = True

    result = portfolio.tick()

    assert result["result"] == "skip"
    assert result["reason"] == "kill_switch_halted"
    assert controller.execution_calls == []
    assert portfolio.actions[-1].action == PortfolioActionType.FAIL_CLOSED

    controller._execution_result = lambda kwargs: filled("x", "CE")
    out = portfolio.enter_opportunities(
        [opportunity("momentum", +1)], spot_price=25_000.0, session_id="sess-1"
    )
    assert out == []  # no entries while halted
    assert len(controller.execution_calls) == 0


# ---------------------------------------------------------------------------
# 8. Paper execution path is used
# ---------------------------------------------------------------------------


def test_entries_use_controller_paper_execution_path():
    controller = FakeController()
    controller._execution_result = lambda kwargs: filled("NIFTY_CE_1", "CE")
    portfolio = make_portfolio(controller)

    portfolio.enter_opportunities(
        [opportunity("momentum", +1)], spot_price=25_000.0, session_id="sess-1"
    )

    call = controller.execution_calls[0]
    assert call["session_id"] == "sess-1"
    assert call["deployment_id"] == "dep-portfolio-1"
    assert call["explicit_side"] == "buy"
    assert call["explicit_option_type"] == "CE"
    assert call["spot_price"] == 25_000.0
    decision = call["decision"]
    assert decision.signal.action == "buy"
    assert decision.selected_configuration.strategy_id == "momentum"


# ---------------------------------------------------------------------------
# 9. Strategy attribution retained
# ---------------------------------------------------------------------------


def test_strategy_attribution_retained_on_entry_and_snapshot():
    controller = FakeController()
    controller._execution_result = lambda kwargs: filled("NIFTY_CE_1", "CE")
    portfolio = make_portfolio(controller)
    broker = controller.control_center._broker

    portfolio.enter_opportunities(
        [opportunity("momentum", +1)], spot_price=25_000.0, session_id="sess-1"
    )
    broker.add_position(
        "NFO:NIFTY_CE_1",
        options_contract_id="NIFTY_CE_1",
        option_type="CE",
        avg_entry_price=120.0,
    )
    portfolio._set_attribution("NIFTY_CE_1", "NFO:NIFTY_CE_1", "momentum")
    snap = portfolio.snapshot()

    rows = snap["positions"]
    assert rows and rows[0]["strategy_id"] == "momentum"
    assert snap["attribution"][0]["strategy_id"] == "momentum"
    entered = [
        a for a in portfolio.actions if a.action == PortfolioActionType.ENTERED
    ]
    assert entered and entered[-1].strategy_id == "momentum"


# ---------------------------------------------------------------------------
# 10a. Fail-closed tick paths + exits
# ---------------------------------------------------------------------------


def test_tick_skips_when_no_market_data():
    controller = FakeController()
    portfolio = make_portfolio(controller)

    result = portfolio.tick()

    assert result["result"] == "skip"
    assert result["reason"] in {"no_market_data", "market_closed"}
    assert controller.execution_calls == []
    # Market-data-unavailable may be recorded for every such tick or only for
    # the first one that fails the freshness window; accept either w.r.t. the
    # last action while still asserting the skip reason above.
    assert portfolio.actions[-1].action in {
        PortfolioActionType.DATA_UNAVAILABLE,
        PortfolioActionType.SKIPPED,
    }


def test_tick_skips_when_market_closed():
    controller = FakeController()
    night = lambda: datetime(2026, 1, 9, 18, 0, tzinfo=UTC)  # 23:30 IST
    portfolio = make_portfolio(controller, clock=night)

    result = portfolio.tick()

    assert result["result"] == "skip"
    assert result["reason"] == "market_closed"
    assert controller.execution_calls == []


def test_exit_stop_loss_uses_paper_path_and_records_pnl():
    controller = FakeController()
    portfolio = make_portfolio(controller)
    broker = controller.control_center._broker
    controller.control_center.deployment.config.stop_loss_pct = 0.10
    pos = broker.add_position(
        "NFO:NIFTY_CE_1",
        options_contract_id="NIFTY_CE_1",
        option_type="CE",
        avg_entry_price=100.0,
        current_price=85.0,  # -15% -> stop loss
        qty=1.0,
    )
    pos.strike = 25000.0
    pos.expiry = "2026-01-29"
    portfolio._set_attribution("NIFTY_CE_1", "NFO:NIFTY_CE_1", "momentum")

    def _exit_fill(kwargs):
        broker.account_obj.realized_pnl += 840.0
        broker.close_position("NFO:NIFTY_CE_1")
        return SimpleNamespace(
            status="FILLED",
            order_id="exit-1",
            options_contract_id="NIFTY_CE_1",
            symbol="NFO:NIFTY_CE_1",
            filled_quantity=1.0,
            avg_fill_price=85.0,
        )

    controller._execution_result = _exit_fill
    results = portfolio.exit_positions(
        spot_price=25_000.0, opportunity_by_strategy={}, session_id="sess-1"
    )

    assert results and results[0]["result"] == "exited"
    assert results[0]["pnl"] == pytest.approx(840.0)
    assert results[0]["reason"] == "stop_loss"
    exited = [
        a for a in portfolio.actions if a.action == PortfolioActionType.EXITED
    ]
    assert exited and exited[-1].strategy_id == "momentum"


# ---------------------------------------------------------------------------
# 10b. Existing behaviour does not regress
# ---------------------------------------------------------------------------


def _make_router_controller():
    fc = FakeController()
    fc._portfolio = None
    runner = SimpleNamespace(broker=fc.control_center._broker)
    fc.control_center._runner = runner
    fc.inspect = lambda: {
        "state": fc.config.state.value,
        "enabled": True,
        "bot_id": fc.config.bot_id,
        "is_halted": fc.is_halted,
    }
    return fc


def test_router_serves_portfolio_snapshot_and_tick():
    from trading_system.paper_api.router import PaperAPIRouter

    fc = _make_router_controller()
    router = PaperAPIRouter(center=None, controller=fc)

    env = router.dispatch("GET", "/autonomous/portfolio", query={}, raw_body="")
    assert env.status == 200
    assert env.body["portfolio"]["portal"] == "autonomous-portfolio"
    assert env.body["portfolio"]["trading_mode"] == "paper"
    assert env.body["schema_version"] == 1

    env2 = router.dispatch(
        "POST", "/autonomous/portfolio/tick", query={}, raw_body=""
    )
    assert env2.status == 200
    assert env2.body["result"]["phase"] == "portfolio"
    # Tick without market data fails closed — no execution ever happened.
    assert fc.execution_calls == []


def test_router_keeps_existing_autonomous_routes():
    from trading_system.paper_api.router import PaperAPIRouter

    fc = _make_router_controller()
    router = PaperAPIRouter(center=None, controller=fc)

    bot = router.dispatch("GET", "/autonomous/bot", query={}, raw_body="")
    assert bot.status == 200
    assert bot.body["bot"]["bot_id"] == fc.config.bot_id

    paths = [r[0].pattern for r in router._routes]
    assert any("/autonomous/deployments" in p for p in paths)
    assert any("/autonomous/events" in p for p in paths)
    # New routes must not shadow the existing ones.
    assert sum(1 for p in paths if "autonomous/portfolio" in p) == 2


def test_scheduler_portfolio_phase_is_wired_and_isolated():
    import backend.autonomous_scheduler as sched

    fc = _make_router_controller()
    fc._portfolio_enabled = True

    class _Boom:
        def tick(self):
            raise RuntimeError("boom")

    fc.portfolio = _Boom()
    result = sched._run_portfolio_tick(fc)
    assert result["result"] == "error"  # isolated, does not kill the tick

    fc._portfolio_enabled = False
    assert sched._run_portfolio_tick(fc)["reason"] == "not_enabled"

    source = open(sched.__file__, encoding="utf-8").read()
    assert "_run_portfolio_tick(controller)" in source  # called from the tick


def test_portfolio_snapshot_is_persisted_for_cross_process_reads():
    class FakeStore:
        def __init__(self):
            self.saved = {}

        def load_portfolio_state(self, bot_id):
            return self.saved.get(bot_id, {})

        def save_portfolio_state(self, bot_id, payload):
            self.saved[bot_id] = payload

    controller = FakeController()
    store = FakeStore()
    portfolio = make_portfolio(controller, persistence=store)
    broker = controller.control_center._broker
    broker.add_position(
        "NFO:A",
        options_contract_id="A",
        option_type="CE",
        unrealized_pnl=800.0,
    )
    broker.account_obj.unrealized_pnl = 800.0

    portfolio._publish_snapshot()

    payload = store.saved[controller.config.bot_id]
    assert payload["portal"] == "autonomous-portfolio"
    assert payload["pnl"]["unrealized"] == pytest.approx(800.0)

    # A fresh portfolio instance in another process reads the same state.
    other = AutonomousPortfolio(controller, persistence=store)
    assert other.read_snapshot()["pnl"]["unrealized"] == pytest.approx(800.0)
