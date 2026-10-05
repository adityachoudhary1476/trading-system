"""Live paper book â€” restart recovery.

Covers the persistence gap that let open positions live only in scheduler
memory: a restart rebuilt an empty broker while positions were still open.
PAPER-only; no network, no live broker.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from trading_system.execution.orders import OrderStatus
from trading_system.paper.book import (
    apply_book_to_broker,
    book_from_broker,
)
from trading_system.paper.session import PaperSessionStore
from trading_system.execution.paper_broker import PaperBroker, SlippageConfig
from trading_system.paper_trading import Position
from trading_system.storage.database import Base


def _open_option(broker, symbol, contract_id, qty, entry, price, contract_size=65):
    """Open a real position through a real fill (the production path).

    The broker has no ``add_position``: positions only ever appear as a side
    effect of a fill, which is also what moves cash and realized P&L. Building
    them any other way would not reproduce a real book.
    """
    option_type = "PE" if contract_id.endswith("|PE") else "CE"
    strike = float(contract_id.split("|")[2])
    expiry = contract_id.split("|")[1]
    broker.submit_order(
        symbol=symbol,
        side="BUY",
        quantity=qty,
        order_type="MARKET",
        current_price=entry,
        options_contract_id=contract_id,
        strike=strike,
        expiry=expiry,
        option_type=option_type,
        contract_size=contract_size,
    )
    broker.update_market_price(symbol, price)
    return broker.positions()[symbol]


def test_book_captures_every_position_not_just_one():
    """A multi-contract book must persist in full.

    The checkpoint path reduced the book to a single
    ``get_position(deployment.symbol)`` lookup, so it could never hold a book
    with more than one contract â€” which is exactly the production case.
    """
    broker = PaperBroker(initial_cash=100_000.0)
    _open_option(
        broker, "NSE:NSE_FO|40716", "NSE:NIFTY50|2026-10-06|22800|PE",
        2.0, 199.06615, 190.2,
    )
    _open_option(
        broker, "NSE:NSE_FO|40712", "NSE:NIFTY|2026-10-06|22750|CE",
        1.0, 171.335625, 181.3,
    )

    book = book_from_broker(
        broker=broker, session_id="sess-1", deployment_id="dep-1"
    )

    assert len(book.positions) == 2
    assert {p["symbol"] for p in book.positions} == {
        "NSE:NSE_FO|40716", "NSE:NSE_FO|40712"
    }
    # Option contract metadata is what lets the exit path resolve the
    # underlying after a restart, so it must survive.
    pe = next(p for p in book.positions if p["symbol"] == "NSE:NSE_FO|40716")
    assert pe["options_contract_id"] == "NSE:NIFTY50|2026-10-06|22800|PE"
    assert pe["contract_size"] == 65
    assert pe["option_type"] == "PE"


def test_book_does_not_round_values():
    """Persistence must not inherit the display rounding of as_dict().

    Asserted against the *actual* stored entry price (slippage included) so
    the test is about round-tripping precision, not about the fill model.
    """
    broker = PaperBroker(initial_cash=100_000.0)
    _open_option(
        broker, "NSE:NSE_FO|1", "NSE:NIFTY|2026-10-06|22750|CE",
        3.0, 171.333333333, 181.7777777,
    )
    book = book_from_broker(broker=broker, session_id="s", deployment_id="d")
    pos = book.positions[0]
    live = broker.positions()["NSE:NSE_FO|1"]
    # Exact equality against the live broker: no display rounding anywhere.
    assert pos["avg_entry_price"] == live.avg_entry_price
    assert pos["current_price"] == live.current_price
    # Sanity: these are genuinely unrounded values, not 4dp display values.
    assert len(str(pos["avg_entry_price"]).split(".")[-1]) > 4, pos["avg_entry_price"]


def test_round_trip_restores_positions_cash_and_realized_pnl():
    broker = PaperBroker(initial_cash=100_000.0)
    _open_option(
        broker, "NSE:NSE_FO|40716", "NSE:NIFTY50|2026-10-06|22800|PE",
        2.0, 199.06615, 190.2,
    )
    _open_option(
        broker, "NSE:NSE_FO|40712", "NSE:NIFTY|2026-10-06|22750|CE",
        1.0, 171.335625, 181.3,
    )
    broker._realized_pnl = -166.69  # realized on a leg closed earlier

    book = book_from_broker(broker=broker, session_id="s", deployment_id="d")

    fresh = PaperBroker(initial_cash=100_000.0)
    assert fresh.positions() == {}, "precondition: the new broker is flat"
    restored = apply_book_to_broker(fresh, book)

    assert restored == 2
    assert set(fresh.positions()) == {
        "NSE:NSE_FO|40716", "NSE:NSE_FO|40712"
    }
    assert fresh._cash == pytest.approx(book.cash)
    assert fresh._realized_pnl == pytest.approx(-166.69), (
        "realized P&L is folded into the broker on close; restoring positions "
        "without it silently rewrites reported P&L"
    )
    pe = fresh.positions()["NSE:NSE_FO|40716"]
    assert pe.options_contract_id == "NSE:NIFTY50|2026-10-06|22800|PE"
    assert pe.contract_size == 65


def test_round_trip_preserves_the_fill_ledger():
    """Reporting reads broker._orders; it must survive a restart too."""
    from trading_system.execution.orders import Order, OrderType, Side

    broker = PaperBroker(initial_cash=100_000.0)
    broker.submit_order(
        symbol="NSE:NSE_FO|40716",
        side="BUY",
        quantity=2.0,
        order_type="MARKET",
        current_price=199.06615,
        options_contract_id="NSE:NIFTY50|2026-10-06|22800|PE",
        strike=22800.0,
        expiry="2026-10-06",
        option_type="PE",
        contract_size=65,
    )
    book = book_from_broker(broker=broker, session_id="s", deployment_id="d")
    assert book.orders, "an order was filled, so the ledger must be captured"

    fresh = PaperBroker(initial_cash=100_000.0)
    apply_book_to_broker(fresh, book)

    assert len(fresh._orders) == 1
    order = next(iter(fresh._orders.values()))
    assert order.status == OrderStatus.FILLED
    assert order.filled_quantity == pytest.approx(2.0)
    assert len(order.fills) == 1
    assert order.fills[0].symbol == "NSE:NSE_FO|40716"


def test_malformed_ledger_row_does_not_cost_the_book():
    """One bad ledger row must not cost the positions that protect capital."""
    broker = PaperBroker(initial_cash=100_000.0)
    _open_option(
        broker, "NSE:NSE_FO|40716", "NSE:NIFTY50|2026-10-06|22800|PE",
        2.0, 199.06615, 190.2,
    )
    book = book_from_broker(broker=broker, session_id="s", deployment_id="d")
    book.orders = [{"symbol": "X", "side": "NOT_A_SIDE"}]  # unparseable

    fresh = PaperBroker(initial_cash=100_000.0)
    restored = apply_book_to_broker(fresh, book)

    assert restored == 1
    assert "NSE:NSE_FO|40716" in fresh.positions()
    assert fresh._orders == {}


# --------------------------------------------------------------------------- #
# Store: mutable, unlike the immutable checkpoint
# --------------------------------------------------------------------------- #
@pytest.fixture()
def store(tmp_path):
    from sqlalchemy import create_engine
    return PaperSessionStore(create_engine(f"sqlite:///{tmp_path/'book.db'}"))


def test_existing_book_table_is_widened_on_open(tmp_path):
    """A table created before a column existed must gain it, with data intact.

    ``create_all`` only creates missing *tables*. An environment that already
    has ``paper_books`` from an earlier release would otherwise fail every
    save with "no such column: paper_books.last_processed_bar_timestamp" --
    i.e. the live book would stop persisting the moment the column was added.
    """
    from sqlalchemy import create_engine, inspect, text

    db = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db}")

    # Recreate the pre-cursor schema by hand: same table, columns missing.
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for column in (
            "last_processed_bar_timestamp",
            "bar_count",
            "schema_version",
        ):
            conn.execute(text(f"ALTER TABLE paper_books DROP COLUMN {column}"))
        # An old-format row that must survive the upgrade.
        conn.execute(
            text(
                "INSERT INTO paper_books (session_id, deployment_id, initial_cash,"
                " cash, realized_pnl, positions_json, orders_json, state_hash,"
                " created_at, updated_at) VALUES"
                " ('legacy','d1','100000.0','100000.0','0.0','[]','[]','h1',"
                " '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
            )
        )

    before = {c["name"] for c in inspect(engine).get_columns("paper_books")}
    assert "last_processed_bar_timestamp" not in before

    # Opening the store is what must reconcile the schema.
    store = PaperSessionStore(engine)

    after = {c["name"] for c in inspect(engine).get_columns("paper_books")}
    assert "last_processed_bar_timestamp" in after
    assert "bar_count" in after
    assert "schema_version" in after

    # The pre-existing row is still readable, with safe defaults.
    legacy = store.get_book("legacy")
    assert legacy is not None
    assert legacy.last_processed_bar_timestamp is None
    assert legacy.bar_count == 0
    assert legacy.state_hash == "h1"

    # And the table is writable again.
    broker = PaperBroker(initial_cash=100_000.0)
    book = book_from_broker(
        broker=broker, session_id="legacy", deployment_id="d1",
        last_processed_bar="2026-01-02T00:00:00+00:00", bar_count=7,
    )
    store.save_book(book)
    assert store.get_book("legacy").bar_count == 7


def test_book_schema_version_is_bumped_for_the_cursor_fields():
    """The book structure changed, so the recorded version must say so."""
    from trading_system.paper.book import BOOK_SCHEMA_VERSION

    assert BOOK_SCHEMA_VERSION >= 2


def test_save_book_is_upsert_not_refused(store):
    """The checkpoint store refuses a changed checkpoint; the book must not.

    That refusal is the defect: checkpoint_id hashes the state, so a changed
    book can never be written again, and open positions are lost on restart.
    """
    broker = PaperBroker(initial_cash=100_000.0)
    book = book_from_broker(broker=broker, session_id="s", deployment_id="d")
    store.save_book(book)
    assert store.get_book("s") is not None

    _open_option(
        broker, "NSE:NSE_FO|1", "NSE:NIFTY|2026-10-06|22750|CE", 1.0, 100.0, 110.0
    )
    updated = book_from_broker(broker=broker, session_id="s", deployment_id="d")
    store.save_book(updated)  # must NOT raise

    reloaded = store.get_book("s")
    assert len(reloaded.positions) == 1
    assert reloaded.state_hash == updated.state_hash


def test_save_book_preserves_created_at_across_updates(store):
    broker = PaperBroker(initial_cash=100_000.0)
    first = book_from_broker(broker=broker, session_id="s", deployment_id="d")
    store.save_book(first)
    created = store.get_book("s").created_at

    _open_option(
        broker, "NSE:NSE_FO|1", "NSE:NIFTY|2026-10-06|22750|CE", 1.0, 100.0, 110.0
    )
    store.save_book(book_from_broker(broker=broker, session_id="s", deployment_id="d"))

    assert store.get_book("s").created_at == created


def test_get_book_returns_none_for_unknown_session(store):
    assert store.get_book("nope") is None


# --------------------------------------------------------------------------- #
# Restart recovery through the real control center
# --------------------------------------------------------------------------- #
def _center_over(engine):
    """A real control center over ``engine`` (no in-memory runner state shared)."""
    from trading_system.paper.control import PaperTradingControlCenter
    from trading_system.research.evidence import EvidenceStore
    from trading_system.research.strategy_intelligence import (
        EvidenceFreshnessConfig,
        EvidenceRequirement,
        StrategyIntelligence,
    )
    from trading_system.research.strategy_registry import StrategyRegistry
    from trading_system.paper import DeploymentGate

    store = EvidenceStore(engine)
    registry = StrategyRegistry(store)
    intelligence = StrategyIntelligence(registry)
    gate = DeploymentGate(
        intelligence=intelligence,
        requirement=EvidenceRequirement(
            require_walk_forward=False, require_validation=False,
            require_recent_evidence=False, min_validation_trades=0,
        ),
        freshness_config=EvidenceFreshnessConfig(max_age_days=180),
    )
    return PaperTradingControlCenter(registry=registry, intelligence=intelligence, gate=gate)


def test_open_positions_survive_a_restart(tmp_path):
    """The production defect: a restart rebuilt a flat book with positions open.

    Before this, ``ensure_live_session`` restored cash only. Every restart
    silently reopened the deployment with zero positions while the live book
    had open ones, so the bot could not manage or report them.
    """
    from sqlalchemy import create_engine
    from trading_system.paper import PaperCircuitBreaker
    import sys
    sys.path.insert(0, "tests")
    from test_phase20_control_center import _build_eligible, _spec

    engine = create_engine(f"sqlite:///{tmp_path/'restart.db'}")

    # --- process 1: create a deployment, open positions, persist the book ---
    center = _center_over(engine)
    spec = _spec(name="Book restart spec")
    _strategy, deployment, _ds = _build_eligible(
        center.registry.store, center.registry, center.intelligence,
        center.gate, spec,
    )
    with center.registry.store._Session() as s:
        s.merge(deployment.as_record())
        s.commit()

    broker = PaperBroker(initial_cash=deployment.config.initial_cash)
    from trading_system.paper import PaperStrategyRunner
    runner = PaperStrategyRunner(
        deployment=deployment, broker=broker, spec=spec,
        circuit_breaker=PaperCircuitBreaker(),
    )
    sid = center.attach_runner(deployment.deployment_id, runner)
    _open_option(
        broker, "NSE:NSE_FO|40716", "NSE:NIFTY50|2026-10-06|22800|PE",
        2.0, 199.06615, 190.2,
    )
    _open_option(
        broker, "NSE:NSE_FO|40712", "NSE:NIFTY|2026-10-06|22750|CE",
        1.0, 171.335625, 181.3,
    )
    assert center.save_live_book(sid) is True
    expected_cash = broker.account().cash

    # --- process 2: a brand-new control center, i.e. a restart ---
    restarted = _center_over(engine)
    assert restarted._runners == {}, "precondition: no live runner after restart"

    restored_sid = restarted.ensure_live_session(deployment.deployment_id)
    assert restored_sid == sid

    restored_runner = restarted.get_runner(restored_sid)
    assert restored_runner is not None
    live = restored_runner.broker.positions()
    assert set(live) == {"NSE:NSE_FO|40716", "NSE:NSE_FO|40712"}, (
        f"positions were lost across the restart; restored {sorted(live)}"
    )
    assert restored_runner.broker.account().cash == pytest.approx(expected_cash)
    assert (
        live["NSE:NSE_FO|40716"].options_contract_id
        == "NSE:NIFTY50|2026-10-06|22800|PE"
    )


def test_save_live_book_is_a_noop_without_a_live_runner():
    """No runner -> nothing to persist; must not raise."""
    from sqlalchemy import create_engine
    center = _center_over(create_engine("sqlite://"))
    assert center.save_live_book("no-such-session") is False


# --------------------------------------------------------------------------- #
# Scheduler: the book is written every tick
# --------------------------------------------------------------------------- #
def test_scheduler_persists_live_books_every_tick():
    """The tick must write the book, or the table stays empty forever.

    Nothing else in the autonomous path persists the book: ``save_session``
    writes an immutable checkpoint that cannot track a live book, so without
    this call open positions remain memory-only.
    """
    from backend import autonomous_scheduler

    class _Runner:
        deployment = type("D", (), {"deployment_id": "dep-1"})()

    class _Center:
        def __init__(self, failing=()):
            self._runners = {"s1": _Runner(), "s2": _Runner(), "bad": _Runner()}
            self._failing = set(failing)
            self.saved = []

        def save_live_book(self, sid):
            if sid in self._failing:
                raise RuntimeError("db down")
            self.saved.append(sid)
            return True

    controller = type("C", (), {"control_center": _Center()})()
    saved = autonomous_scheduler._persist_live_books(controller)
    assert set(saved) == {"s1", "s2", "bad"}
    assert set(controller.control_center.saved) == {"s1", "s2", "bad"}


def test_book_persistence_failure_does_not_abort_the_tick():
    """A DB failure must not stop trading -- but it must not be silent either."""
    from backend import autonomous_scheduler

    class _Runner:
        deployment = type("D", (), {"deployment_id": "dep-1"})()

    class _Center:
        def __init__(self):
            self._runners = {"ok": _Runner(), "bad": _Runner()}

        def save_live_book(self, sid):
            if sid == "bad":
                raise RuntimeError("db down")
            return True

    controller = type("C", (), {"control_center": _Center()})()
    saved = autonomous_scheduler._persist_live_books(controller)
    assert saved == ["ok"], "one failing session must not lose the others"


def test_persist_live_books_tolerates_a_control_center_without_support():
    from backend import autonomous_scheduler

    controller = type("C", (), {"control_center": object()})()
    assert autonomous_scheduler._persist_live_books(controller) == []


def test_book_is_persisted_even_when_the_tick_raises(monkeypatch):
    """A crash after an order is submitted must not lose the book.

    This is the window that actually loses money: the order is already in the
    broker, but a save placed after the tick would never be reached.
    """
    from backend import autonomous_scheduler

    from trading_system.paper.book import book_from_broker

    # A real broker with a real open position, as if an order had just filled.
    broker = PaperBroker(initial_cash=100000.0)
    _open_option(
        broker, "NSE:NSE_FO|1", "NSE:NIFTY|2026-10-06|22800|PE", 2, 100.0, 101.0
    )
    book = book_from_broker(
        broker=broker, session_id="s1", deployment_id="dep-1"
    )
    assert len(book.positions) == 1, "precondition: the broker holds a position"

    persisted = []

    class _Center:
        def __init__(self):
            self._runners = {"s1": runner}

        def list_deployments(self):
            return []

        def save_live_book(self, sid):
            persisted.append(
                book_from_broker(
                    broker=runner.broker, session_id=sid, deployment_id="dep-1"
                )
            )
            return True

    runner = type(
        "R", (), {"broker": broker, "deployment": type("D", (), {"deployment_id": "dep-1"})()}
    )()
    center = _Center()

    # Force every gate open so the tick reaches the portfolio stage regardless
    # of wall-clock time, and make the portfolio tick mutate the book and then
    # explode. The mutation is the point: an order that filled just before the
    # crash is exactly the position a post-tick-only save would lose.
    monkeypatch.setattr(autonomous_scheduler, "_is_regular_session", lambda ts: True)
    monkeypatch.setattr(autonomous_scheduler, "_has_fresh_data", lambda *a, **k: True)
    monkeypatch.setattr(autonomous_scheduler, "_sweep_sl_tp_positions", lambda *a, **k: [])
    monkeypatch.setattr(autonomous_scheduler, "_run_phase22_integration", lambda *a, **k: {})

    def _fill_then_crash(*_a, **_k):
        _open_option(
            broker, "NSE:NSE_FO|2", "NSE:NIFTY|2026-10-06|22750|CE", 1, 50.0, 51.0
        )
        raise RuntimeError("tick exploded after submitting the order")

    monkeypatch.setattr(autonomous_scheduler, "_run_portfolio_tick", _fill_then_crash)

    class _Constraints:
        allowed_symbols = ["NSE:SBIN"]
        allowed_timeframes = ["1d"]

    controller = type(
        "C",
        (),
        {
            "control_center": center,
            "config": type("Cfg", (), {"bot_id": "b1", "user_constraints": _Constraints()})(),
            "is_halted": False,
            "scan_market": lambda self, timeframe=None: type(
                "Scan", (), {"candidates": [object()]}
            )(),
            "rank_candidates": lambda self, scan: scan,
            "evaluate_strategy_compatibility": lambda self, ranking: ranking,
            "generate_strategy_decisions": lambda self, compat: type(
                "D", (), {"decisions": []}
            )(),
            "update_scheduler_heartbeat": lambda *a, **k: None,
        },
    )()

    with pytest.raises(RuntimeError):
        autonomous_scheduler._run_one_tick(controller)

    assert persisted, "the book must be written even when the tick raises"
    # The second position was opened *inside* the crashing tick, so only the
    # save in the `finally` can have captured it.
    assert [len(b.positions) for b in persisted][-1] == 2, (
        "the position filled during the crashing tick was lost: "
        f"{[len(b.positions) for b in persisted]}"
    )


# --------------------------------------------------------------------------- #
# API: a repeated POST must not wipe a live book
# --------------------------------------------------------------------------- #
def test_repeated_deployment_post_does_not_wipe_open_positions(tmp_path):
    """A repeat POST must not replace the live runner.

    ``create_deployment`` is idempotent on (dataset, config), so posting the
    same body twice returns the SAME deployment and the SAME session id. The
    old handler then built a second ``PaperBroker`` and attached it over the
    live runner, wiping every open position -- while still answering 201.
    """
    import json

    from sqlalchemy import create_engine

    from trading_system.paper_api.router import PaperAPIRouter

    import sys
    sys.path.insert(0, "tests")
    from test_phase20_control_center import _spec

    engine = create_engine(f"sqlite:///{tmp_path/'router.db'}")
    center = _center_over(engine)
    router = PaperAPIRouter(center)

    spec = _spec(name="Router book spec")
    body = json.dumps({"spec": spec.model_dump(mode="json")})

    first = router.dispatch("POST", "/deployments", raw_body=body)
    assert first.status in (200, 201), first.body
    first_body = first.body.get("body", first.body)
    sid = first_body["session_id"]

    # The bot opens a position on the live runner.
    live = center.get_runner(sid)
    _open_option(
        live.broker, "NSE:NSE_FO|40716", "NSE:NIFTY50|2026-10-06|22800|PE",
        2.0, 199.06615, 190.2,
    )
    assert set(live.broker.positions()) == {"NSE:NSE_FO|40716"}

    # A client retries the POST (a double-click, a flaky network retry).
    second = router.dispatch("POST", "/deployments", raw_body=body)
    assert second.status in (200, 201), second.body
    second_body = second.body.get("body", second.body)

    # Idempotency is the premise of the test, not a coincidence.
    assert second_body["deployment"]["deployment_id"] == (
        first_body["deployment"]["deployment_id"]
    ), "precondition: the repeat POST should target the same deployment"
    assert second_body["session_id"] == sid, (
        "precondition: the repeat POST should target the same session"
    )

    after = center.get_runner(sid)
    assert after is live, "the repeat POST replaced the live runner object"
    assert set(after.broker.positions()) == {"NSE:NSE_FO|40716"}, (
        "open positions were wiped by a repeat POST; now "
        f"{sorted(after.broker.positions())}"
    )


# --------------------------------------------------------------------------- #
# Restart consistency invariants
# --------------------------------------------------------------------------- #
def _process_bars(runner, n=30):
    """Feed a deterministic uptrend so the bar cursor advances and orders fill."""
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(1)
    idx = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    close = 100 + np.cumsum(rng.normal(0.3, 0.6, n))
    for ts, c in zip(idx, close):
        runner.process_bar({
            "timestamp": pd.Timestamp(ts),
            "open": float(c) + 0.1, "high": float(c) + 1.0,
            "low": float(c) - 1.0, "close": float(c),
            "volume": 500.0,
        })


def _equity(runner):
    return runner.broker.account().equity


def test_restart_preserves_equity_and_never_reprocesses_a_handled_bar(tmp_path):
    """The invariant that makes a restart safe: nothing observable may change.

    Two properties, both of which are currently broken:

    1. Equity and the position set must be identical either side of a restart.
    2. The bar cursor must survive. ``save_checkpoint`` is INSERT-only, so the
       checkpoint's ``last_processed_bar_timestamp`` is frozen at creation and
       is ``None`` for any deployment built by the API. On restart the runner
       therefore believes it has processed nothing, and every bar the data
       provider replays is treated as new -- so a position already on the book
       gets re-entered.
    """
    from sqlalchemy import create_engine
    from trading_system.paper import PaperCircuitBreaker, PaperStrategyRunner
    import sys
    sys.path.insert(0, "tests")
    from test_phase20_control_center import _build_eligible, _spec

    engine = create_engine(f"sqlite:///{tmp_path/'consistency.db'}")

    center = _center_over(engine)
    spec = _spec(name="Restart consistency spec")
    _strategy, deployment, _ds = _build_eligible(
        center.registry.store, center.registry, center.intelligence,
        center.gate, spec,
    )
    with center.registry.store._Session() as s:
        s.merge(deployment.as_record())
        s.commit()
    # process_bar is a no-op unless the deployment is ACTIVE, so the cursor
    # would never advance and the test would prove nothing. Note that
    # activate_deployment() mutates a freshly-loaded copy, not the object we
    # hold, so the ACTIVE deployment has to be re-read.
    center.activate_deployment(deployment.deployment_id)
    active = center.get_deployment(deployment.deployment_id)
    assert active is not None and active.status.value == "active", (
        f"deployment was not activated: {getattr(active, 'status', None)}"
    )

    broker = PaperBroker(initial_cash=deployment.config.initial_cash)
    runner = PaperStrategyRunner(
        deployment=active, broker=broker, spec=spec,
        circuit_breaker=PaperCircuitBreaker(),
    )
    sid = center.attach_runner(deployment.deployment_id, runner)
    _process_bars(runner, 30)
    assert center.save_live_book(sid) is True

    pre_equity = _equity(runner)
    pre_positions = set(broker.positions())
    pre_cursor = runner._last_processed_bar
    assert pre_cursor is not None, "precondition: bars were processed"

    # --- restart ---
    restarted = _center_over(engine)
    assert restarted._runners == {}
    restored_sid = restarted.ensure_live_session(deployment.deployment_id)
    assert restored_sid == sid
    restored = restarted.get_runner(restored_sid)

    # 1. Nothing observable changed.
    assert _equity(restored) == pytest.approx(pre_equity), (
        f"equity drifted across the restart: {pre_equity} -> {_equity(restored)}"
    )
    assert set(restored.broker.positions()) == pre_positions, (
        "position set drifted across the restart: "
        f"{sorted(pre_positions)} -> {sorted(restored.broker.positions())}"
    )

    # 2. The bar cursor survived -- otherwise every replayed bar re-trades.
    assert restored._last_processed_bar is not None, (
        "the bar cursor was lost across the restart; the runner will treat "
        "every replayed bar as new and re-enter positions it already holds"
    )
    assert restored._last_processed_bar == pre_cursor, (
        f"bar cursor regressed: {pre_cursor} -> {restored._last_processed_bar}"
    )


# --------------------------------------------------------------------------- #
# The bar watermark
# --------------------------------------------------------------------------- #
def _bar(ts, close=100.0):
    import pandas as pd

    return {
        "timestamp": pd.Timestamp(ts, tz="UTC"),
        "open": close, "high": close + 1.0, "low": close - 1.0,
        "close": close, "volume": 500.0,
    }


def _warm_runner():
    from trading_system.paper import PaperCircuitBreaker, PaperStrategyRunner

    spec = _bare_spec()
    deployment = _bare_deployment()
    # The runner refuses a deployment whose bound spec identity does not match,
    # so bind the real hash rather than a placeholder.
    from trading_system.paper.runner import _spec_identity

    deployment.strategy_spec_hash = _spec_identity(spec)
    return PaperStrategyRunner(
        deployment=deployment,
        broker=PaperBroker(initial_cash=100000.0),
        spec=spec,
        circuit_breaker=PaperCircuitBreaker(),
    )


def _bare_spec():
    import sys
    sys.path.insert(0, "tests")
    from test_phase20_control_center import _spec

    return _spec(name="watermark spec")


def _bare_deployment():
    from trading_system.paper.deployment import (
        PaperDeployment,
        PaperDeploymentConfig,
        PaperDeploymentStatus,
    )

    return PaperDeployment(
        deployment_id="dep-wm",
        strategy_id="s-wm",
        strategy_spec_hash="h-wm",
        symbol="NSE:SBIN",
        timeframe="1d",
        dataset_id="market_data",
        config=PaperDeploymentConfig(),
        status=PaperDeploymentStatus.ACTIVE,
    )


def test_watermark_rejects_older_bars_not_just_the_same_one():
    """A restarted runner replays a lookback window, not a single bar.

    Equality-only idempotency rejects the newest bar and nothing else, so the
    whole replayed window would be re-traded. The watermark must reject
    anything at or before the last processed bar.
    """
    from trading_system.paper.runner import SignalType

    runner = _warm_runner()
    for i in range(1, 11):
        runner.process_bar(_bar(f"2024-01-{i:02d}"))
    cursor = runner._last_processed_bar
    assert cursor is not None
    window_before = len(runner._window)
    count_before = runner._bar_count

    # The same bar and every older bar in the replay window are no-ops.
    assert runner.process_bar(_bar("2024-01-10")) == SignalType.NO_ACTION
    assert runner.process_bar(_bar("2024-01-05")) == SignalType.NO_ACTION
    assert runner.process_bar(_bar("2024-01-01")) == SignalType.NO_ACTION
    assert runner._bar_count == count_before
    assert len(runner._window) == window_before

    # A genuinely newer bar is still processed.
    runner.process_bar(_bar("2024-01-11"))
    assert runner._bar_count == count_before + 1
    assert runner._last_processed_bar > cursor


def test_parse_book_cursor_refuses_to_invent_a_timestamp():
    """A corrupt cursor must not become 'now', which would skip live bars."""
    from trading_system.paper.book import parse_book_cursor

    assert parse_book_cursor(None) is None
    assert parse_book_cursor("not-a-timestamp") is None
    assert parse_book_cursor("") is None

    good = parse_book_cursor("2024-01-10T00:00:00+00:00")
    assert good is not None and good.year == 2024
    # Naive values are localized so comparisons cannot blow up later.
    naive = parse_book_cursor("2024-01-10T00:00:00")
    assert naive is not None and naive.tzinfo is not None


def test_malformed_fill_timestamp_is_dropped_not_backdated(caplog):
    """A corrupt fill time is dropped, never rewritten to 'now'.

    Backdating would make an old fill look fresh and defeat any age-based
    logic reading the ledger.
    """
    import logging

    from trading_system.paper.book import PaperBook, apply_book_to_broker

    broker = PaperBroker(initial_cash=100000.0)
    # Enum values are upper-case (Side.BUY == "BUY"); a lower-case payload is
    # exactly the kind of corruption this path has to survive.
    good = {
        "order_id": "ok-1", "symbol": "NSE:SBIN", "side": "BUY", "quantity": 1.0,
        "order_type": "MARKET", "status": "FILLED", "filled_quantity": 1.0,
        "avg_fill_price": 10.0, "limit_price": None,
        "fills": [{
            "fill_id": "f-ok", "order_id": "ok-1", "symbol": "NSE:SBIN",
            "side": "BUY", "quantity": 1.0, "price": 10.0,
            "timestamp": "2024-01-10T00:00:00+00:00", "fee": 0.0, "note": "",
        }],
        "created_at": "2024-01-10T00:00:00+00:00",
        "updated_at": "2024-01-10T00:00:00+00:00",
        "reject_reason": "",
    }
    bad = dict(good, order_id="bad-1", fills=[{
        "fill_id": "f-bad", "order_id": "bad-1", "symbol": "NSE:SBIN",
        "side": "BUY", "quantity": 1.0, "price": 10.0,
        "timestamp": "corrupted", "fee": 0.0, "note": "",
    }])

    book = PaperBook(
        session_id="s1", deployment_id="d1", initial_cash=100000.0,
        cash=100000.0, realized_pnl=0.0, positions=[], orders=[good, bad],
    )
    with caplog.at_level(logging.WARNING, logger="trading_system.paper.book"):
        apply_book_to_broker(broker, book)

    assert set(broker._orders) == {"ok-1"}, broker._orders
    assert any("dropped 1 unparseable order row" in r.message for r in caplog.records), (
        "dropping a ledger row must be visible, not silent: "
        f"{[r.message for r in caplog.records]}"
    )


# --------------------------------------------------------------------- #
# Position age: a time stop is impossible without it
# --------------------------------------------------------------------- #


def test_opening_a_position_stamps_opened_at():
    broker = PaperBroker(initial_cash=100_000.0)
    pos = _open_option(broker, "NSE:SBIN", "NSE:X|2030-01-29|400|CE", 1, 100.0, 100.0)
    assert pos.opened_at, "a new position must know when it was opened"


def test_opened_at_survives_a_book_round_trip():
    """Restart must not reset the age clock.

    If opened_at were lost on restore, a position restored mid-thesis would
    look brand new and its time stop would restart from zero on every
    restart, making the rule unenforceable exactly when it is needed.
    """
    broker = PaperBroker(initial_cash=100_000.0)
    pos = _open_option(broker, "NSE:SBIN", "NSE:X|2030-01-29|400|CE", 1, 100.0, 100.0)

    book = book_from_broker(broker=broker, session_id="s1", deployment_id="d1")
    fresh = PaperBroker(initial_cash=100_000.0)
    apply_book_to_broker(fresh, book)

    restored = fresh.positions()["NSE:SBIN"]
    assert restored.opened_at == pos.opened_at


def test_increasing_a_position_does_not_restart_the_age():
    broker = PaperBroker(initial_cash=100_000.0)
    pos = _open_option(broker, "NSE:SBIN", "NSE:X|2030-01-29|400|CE", 1, 100.0, 100.0)
    opened_at = pos.opened_at
    _open_option(broker, "NSE:SBIN", "NSE:X|2030-01-29|400|CE", 1, 110.0, 110.0)
    assert broker.positions()["NSE:SBIN"].opened_at == opened_at, (
        "adding to a position must not restart the clock on capital already at risk"
    )


def test_closing_a_position_clears_opened_at():
    broker = PaperBroker(initial_cash=100_000.0)
    symbol = "NSE:SBIN"
    pos = _open_option(broker, symbol, "NSE:X|2030-01-29|400|CE", 1, 100.0, 100.0)
    assert pos.opened_at
    broker.submit_order(
        symbol=symbol, side="SELL", quantity=1.0, order_type="MARKET",
        current_price=110.0, options_contract_id="NSE:X|2030-01-29|400|CE",
        strike=400.0, expiry="2030-01-29", option_type="CE", contract_size=65,
    )
    assert broker.positions()[symbol].opened_at is None


def test_holding_seconds_is_unknown_not_zero_when_opened_at_is_missing():
    """Unknown age must be distinguishable from a brand-new position.

    A time stop that reads a missing opened_at as age 0 would either never
    fire or, worse, be treated as "old enough" by an inverted check.
    """
    broker = PaperBroker(initial_cash=100_000.0)
    pos = _open_option(broker, "NSE:SBIN", "NSE:X|2030-01-29|400|CE", 1, 100.0, 100.0)
    pos.opened_at = None
    assert pos.holding_seconds() is None


# --------------------------------------------------------------------- #
# Expiry settlement: the only way an expired contract can be retired
# --------------------------------------------------------------------- #


def test_settle_expired_long_call_pays_intrinsic_and_keeps_books_consistent():
    broker = PaperBroker(initial_cash=1_000_000.0, slippage=SlippageConfig(slippage_bps=0.0))
    symbol = "NSE:X"
    _open_option(broker, symbol, "NSE:X|2030-01-29|400|CE", 2, 100.0, 100.0, contract_size=50)
    cash_before = broker._cash

    # Spot 450 vs strike 400 -> intrinsic 50.
    outcome = broker.settle_expired_position(symbol, 450.0)

    assert outcome is not None
    assert outcome["intrinsic_price"] == pytest.approx(50.0)
    # 2 contracts * 50 intrinsic * 50 contract size
    assert outcome["cash_delta"] == pytest.approx(5000.0)
    assert broker._cash == pytest.approx(cash_before + 5000.0)
    # Realized: (50 - 100) * 2 * 50
    assert outcome["realized_pnl"] == pytest.approx(-5000.0)
    assert broker._realized_pnl == pytest.approx(-5000.0)
    assert broker.positions()[symbol].qty == 0.0


def test_settle_expired_out_of_the_money_long_call_raises_no_cash():
    """Worthless at expiry: the capital is simply gone, not held in limbo."""
    broker = PaperBroker(initial_cash=1_000_000.0, slippage=SlippageConfig(slippage_bps=0.0))
    symbol = "NSE:X"
    _open_option(broker, symbol, "NSE:X|2030-01-29|400|CE", 1, 100.0, 100.0, contract_size=50)
    cash_before = broker._cash

    outcome = broker.settle_expired_position(symbol, 300.0)

    assert outcome is not None
    assert outcome["intrinsic_price"] == pytest.approx(0.0)
    assert outcome["cash_delta"] == pytest.approx(0.0)
    assert broker._cash == pytest.approx(cash_before)
    # Full premium lost
    assert outcome["realized_pnl"] == pytest.approx(-5000.0)
    assert broker.positions()[symbol].qty == 0.0


def test_settle_expired_put_uses_put_intrinsic():
    broker = PaperBroker(initial_cash=1_000_000.0, slippage=SlippageConfig(slippage_bps=0.0))
    symbol = "NSE:X"
    _open_option(broker, symbol, "NSE:X|2030-01-29|400|PE", 1, 100.0, 100.0, contract_size=50)

    outcome = broker.settle_expired_position(symbol, 350.0)

    assert outcome is not None
    assert outcome["intrinsic_price"] == pytest.approx(50.0)


def test_settle_expired_refuses_without_a_usable_spot():
    """A missing official close must not be invented."""
    broker = PaperBroker(initial_cash=1_000_000.0, slippage=SlippageConfig(slippage_bps=0.0))
    symbol = "NSE:X"
    _open_option(broker, symbol, "NSE:X|2030-01-29|400|CE", 1, 100.0, 100.0)

    for bad in (0.0, -1.0, float("nan"), None, "not a number"):
        assert broker.settle_expired_position(symbol, bad) is None, bad
    # and the position is untouched
    assert broker.positions()[symbol].qty == 1.0


def test_settle_expired_is_a_noop_for_unknown_or_flat():
    broker = PaperBroker(initial_cash=1_000_000.0, slippage=SlippageConfig(slippage_bps=0.0))
    assert broker.settle_expired_position("NSE:NOPE", 450.0) is None


# --------------------------------------------------------------------------- #
# Greeks anchors on Position: persistence and the decay-ratio contract
# --------------------------------------------------------------------------- #
def _future_expiry(days: int = 60) -> str:
    """A date far enough ahead that the test cannot expire as the clock moves.

    The broker refuses to mark-to-market an expired option, so a hard-coded
    expiry would turn this into a date bomb.
    """
    return (date.today() + timedelta(days=days)).isoformat()


class TestPositionGreeksAnchors:
    """``entry_delta`` is a one-time measurement taken at the fill. It has to
    survive a restart, because a delta-decay test compares two points in time
    and an anchor reconstructed after the restart would be measured against
    today's market, reporting no decay ever.
    """

    def test_defaults_are_none_so_greeks_read_as_unknown(self):
        pos = Position(symbol="NSE:NIFTY", qty=1, avg_entry_price=100.0)
        assert pos.entry_delta is None
        assert pos.last_delta is None
        assert pos.has_greeks() is False
        assert pos.delta_retention() is None

    def test_has_greeks_needs_both_points(self):
        pos = Position(symbol="X", qty=1, entry_delta=0.5)
        assert pos.has_greeks() is False
        assert pos.delta_retention() is None
        pos.last_delta = 0.25
        assert pos.has_greeks() is True

    def test_delta_retention_is_ratio_of_absolute_deltas(self):
        pos = Position(symbol="X", qty=1, entry_delta=0.50, last_delta=0.25)
        assert pos.delta_retention() == pytest.approx(0.5)

    def test_delta_retention_handles_negative_deltas(self):
        """A long put's delta is negative; the ratio is about magnitude."""
        pos = Position(symbol="X", qty=1, entry_delta=-0.40, last_delta=-0.20)
        assert pos.delta_retention() == pytest.approx(0.5)

    def test_zero_anchor_does_not_divide_by_zero(self):
        pos = Position(symbol="X", qty=1, entry_delta=0.0, last_delta=0.2)
        assert pos.has_greeks() is True
        assert pos.delta_retention() is None

    def test_as_dict_omits_unset_greeks(self):
        pos = Position(symbol="X", qty=1)
        payload = pos.as_dict()
        assert "entry_delta" not in payload
        assert "last_delta" not in payload

    def test_as_dict_includes_set_greeks(self):
        pos = Position(
            symbol="X", qty=1, entry_delta=0.5, last_delta=0.2,
            entry_iv=0.15, last_iv=0.18, greeks_as_of="2026-01-08T09:30:00+00:00",
        )
        payload = pos.as_dict()
        assert payload["entry_delta"] == pytest.approx(0.5)
        assert payload["last_delta"] == pytest.approx(0.2)
        assert payload["entry_iv"] == pytest.approx(0.15)
        assert payload["greeks_as_of"] == "2026-01-08T09:30:00+00:00"

    def test_greeks_survive_a_book_round_trip(self, store):
        broker = PaperBroker(initial_cash=1_000_000.0)
        pos = _open_option(
            broker, "NSE:NIFTY", f"NSE:OPTIDX|{_future_expiry()}|25000.0|CE",
            1, 120.0, 100.0,
        )
        pos.entry_delta = 0.52
        pos.last_delta = 0.21
        pos.entry_iv = 0.145
        pos.last_iv = 0.181
        pos.greeks_as_of = "2026-01-08T09:30:00+00:00"

        book = book_from_broker(
            broker=broker, session_id="s", deployment_id="dep-greeks"
        )
        store.save_book(book)

        saved = store.get_book("s")
        assert saved is not None
        fresh = PaperBroker(initial_cash=1_000_000.0)
        apply_book_to_broker(fresh, saved)
        restored = fresh.positions()[pos.symbol]
        assert restored.entry_delta == pytest.approx(0.52)
        assert restored.last_delta == pytest.approx(0.21)
        assert restored.entry_iv == pytest.approx(0.145)
        assert restored.last_iv == pytest.approx(0.181)
        assert restored.greeks_as_of == "2026-01-08T09:30:00+00:00"
        assert restored.delta_retention() == pytest.approx(0.21 / 0.52)

    def test_book_written_before_greeks_existed_reads_as_unknown(self, store):
        """Backward compatibility: a book with no greeks keys must load, and the
        position must read as unknown rather than as zero exposure."""
        broker = PaperBroker(initial_cash=1_000_000.0)
        pos = _open_option(
            broker, "NSE:NIFTY", f"NSE:OPTIDX|{_future_expiry()}|25000.0|CE",
            1, 120.0, 100.0,
        )
        book = book_from_broker(
            broker=broker, session_id="s", deployment_id="dep-legacy"
        )
        for raw in book.positions:
            for key in (
                "entry_delta", "last_delta", "entry_iv", "last_iv", "greeks_as_of",
            ):
                raw.pop(key, None)

        store.save_book(book)
        saved = store.get_book("s")
        fresh = PaperBroker(initial_cash=1_000_000.0)
        apply_book_to_broker(fresh, saved)
        restored = fresh.positions()[pos.symbol]
        assert restored.has_greeks() is False
        assert restored.delta_retention() is None
