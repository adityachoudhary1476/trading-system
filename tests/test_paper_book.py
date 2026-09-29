"""Live paper book â€” restart recovery.

Covers the persistence gap that let open positions live only in scheduler
memory: a restart rebuilt an empty broker while positions were still open.
PAPER-only; no network, no live broker.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading_system.execution.orders import OrderStatus
from trading_system.paper.book import (
    apply_book_to_broker,
    book_from_broker,
)
from trading_system.paper.session import PaperSessionStore
from trading_system.execution.paper_broker import PaperBroker


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
