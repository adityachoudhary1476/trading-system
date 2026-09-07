"""Phase B — Options provider wiring tests for the autonomous scheduler.

The autonomous PAPER-only scheduler must:

  * Wire the REAL ``CurrentOptionDiscoverer``,
    ``CurrentOptionQuoteProvider``, ``UpstoxInstrumentDiscovery``, and
    ``InstrumentRepository`` for options-enabled deployments.
  * NEVER use ``InMemoryOptionsChainProvider`` (synthetic, test-only).
  * NEVER call ``execute_option_order`` or ``submit_order_intent`` for
    options in Phase B.
  * Fail closed (no crash) when providers are unavailable, when quotes
    are stale, or when no contracts exist for the underlying.
  * Leave the equity path completely unchanged when
    ``options_enabled = false``.

No live network, no real Upstox tokens, no broker endpoints. Every
provider is a deterministic test double constructed around the real
provider interface.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from trading_system.autonomous.bot_config import (
    AutonomousBotConfig,
    BotMode,
    Source,
    TradingMode,
    UserConstraints,
)
from trading_system.autonomous.controller import AutonomousController
from trading_system.autonomous.options.discovery import (
    CurrentOptionDiscoverer,
    DiscoveryConfig,
)
from trading_system.autonomous.options.model import OptionDirection
from trading_system.autonomous.options_contract import InMemoryOptionsChainProvider
from trading_system.autonomous.options.selector import OptionsContractSelector
from trading_system.india.instrument_repository import InstrumentRepository
from trading_system.india.instruments import (
    Instrument,
    InstrumentType,
)
from trading_system.india.option_quotes import CurrentOptionQuoteProvider, OptionQuote
from trading_system.paper.deployment import (
    PaperDeploymentConfig,
    PaperDeploymentStatus,
)

from backend.autonomous_scheduler import (
    _build_controller,
    _build_options_wiring,
    _env_option_underlyings,
    _env_max_option_quote_age,
    _list_options_enabled_deployments,
    _run_phase_b_validation,
    _attach_options_wiring,
)
from backend.options_phase_b import (
    EVENT_CAPABILITY_READY,
    EVENT_DISCOVERY_FAILED,
    EVENT_DISCOVERY_OK,
    EVENT_PHASE_B_NO_EXECUTION,
    EVENT_PROVIDER_UNAVAILABLE,
    EVENT_QUOTE_OK,
    EVENT_QUOTE_STALE,
    EVENT_QUOTE_UNAVAILABLE,
    OptionsPhaseBWiring,
    PhaseBObservation,
)


UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Fakes (real-interface test doubles, never network)
# --------------------------------------------------------------------------- #
class _FakeUpstoxProvider:
    """Stand-in for ``UpstoxMarketDataProvider`` used by
    ``CurrentOptionQuoteProvider``. Implements the minimal surface the
    quote provider touches (``is_authenticated``, ``_upstox_symbol``,
    ``_get``). Never makes a network call.
    """

    def __init__(
        self,
        *,
        authenticated: bool = True,
        quotes: Optional[dict[str, dict]] = None,
    ) -> None:
        self._authenticated = authenticated
        self._quotes: dict[str, dict] = quotes or {}

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    def _upstox_symbol(self, key: str) -> str:
        return key

    def _get(self, path: str, *, params: Optional[dict] = None) -> dict:
        symbol = (params or {}).get("symbol") or ""
        record = self._quotes.get(symbol)
        if record is None:
            return {"data": {}}
        return {"data": {symbol: record}}


def _make_quote_record(ltp: float, age_seconds: float) -> dict:
    """Build a single Upstox-shaped quote record aged ``age_seconds`` ago.

    The timestamp is supplied as an ISO 8601 string because
    ``normalize_timestamp_ms`` only accepts ISO strings for string
    values; numeric epoch strings are not parsed.
    """
    ts = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
    return {
        "last_price": float(ltp),
        "last_traded_timestamp": ts,
        "buy_price": float(ltp) * 0.999,
        "sell_price": float(ltp) * 1.001,
        "oi": 1000.0,
        "volume": 500.0,
    }


def _make_nifty_call_instrument(
    *, strike: float = 25000.0, expiry: Optional[str] = None
) -> Instrument:
    if expiry is None:
        expiry = (datetime.now(UTC) + timedelta(days=14)).date().isoformat()
    return Instrument.option(
        "NSE", "NIFTY", expiry, float(strike), "CE",
        provider_symbol="NSE_FO|NIFTY26SEP25000CE",
    )


def _make_nifty_put_instrument(
    *, strike: float = 25000.0, expiry: Optional[str] = None
) -> Instrument:
    if expiry is None:
        expiry = (datetime.now(UTC) + timedelta(days=14)).date().isoformat()
    return Instrument.option(
        "NSE", "NIFTY", expiry, float(strike), "PE",
        provider_symbol="NSE_FO|NIFTY26SEP25000PE",
    )


def _build_wiring(
    *,
    authenticated: bool = True,
    quotes: Optional[dict[str, dict]] = None,
    repo: Optional[InstrumentRepository] = None,
    seed_instruments: Optional[list[Instrument]] = None,
    max_quote_age_seconds: float = 300.0,
) -> OptionsPhaseBWiring:
    """Build a Phase B wiring with fake but real-interface providers."""
    repository = repo or InstrumentRepository()
    for instr in seed_instruments or []:
        repository.register(instr)
    discoverer = CurrentOptionDiscoverer(repository=repository)
    fake_provider = _FakeUpstoxProvider(
        authenticated=authenticated, quotes=quotes or {}
    )
    quote_provider = CurrentOptionQuoteProvider(
        provider=fake_provider,
        max_quote_age_seconds=max_quote_age_seconds,
    )
    return OptionsPhaseBWiring(
        repository=repository,
        discoverer=discoverer,
        quote_provider=quote_provider,
        discovery=None,
    )


def _build_pure_equity_controller(bot_id: str = "bot-equity-only") -> AutonomousController:
    """Equity-only controller: no option underlyings allowed."""
    from trading_system.paper.control import PaperTradingControlCenter
    from trading_system.research.evidence import EvidenceStore
    from trading_system.research.strategy_intelligence import StrategyIntelligence
    from trading_system.research.strategy_registry import StrategyRegistry

    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    store = EvidenceStore(engine)
    registry = StrategyRegistry(store)
    intelligence = StrategyIntelligence(registry)
    center = PaperTradingControlCenter(
        registry=registry, intelligence=intelligence,
    )
    return _build_controller(
        center=center, md_provider=None, market_data_callable=None,
        bot_id=bot_id, option_underlyings=frozenset(),
    )


def _build_options_enabled_controller(
    *,
    bot_id: str = "bot-options-b",
    underlyings: frozenset[str] = frozenset({"NIFTY"}),
) -> AutonomousController:
    """Options-enabled controller (still constructed against an empty
    in-memory center — no real network)."""
    from trading_system.paper.control import PaperTradingControlCenter
    from trading_system.research.evidence import EvidenceStore
    from trading_system.research.strategy_intelligence import StrategyIntelligence
    from trading_system.research.strategy_registry import StrategyRegistry

    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    store = EvidenceStore(engine)
    registry = StrategyRegistry(store)
    intelligence = StrategyIntelligence(registry)
    center = PaperTradingControlCenter(
        registry=registry, intelligence=intelligence,
    )
    return _build_controller(
        center=center, md_provider=None, market_data_callable=None,
        bot_id=bot_id, option_underlyings=underlyings,
    )


# --------------------------------------------------------------------------- #
# Test 1 — options_enabled = false
# --------------------------------------------------------------------------- #
def test_options_disabled_preserves_equity_behavior(monkeypatch) -> None:
    """When no option underlyings are configured, the scheduler remains
    equity-only and never wires option providers.
    """
    monkeypatch.delenv("AUTONOMOUS_OPTION_UNDERLYINGS", raising=False)
    monkeypatch.delenv("AUTONOMOUS_OPTION_SEED_UNDERLYINGS", raising=False)
    monkeypatch.delenv("UPSTOX_SERVICE_ACCOUNT_TOKEN", raising=False)
    assert _env_option_underlyings() == frozenset()

    controller = _build_pure_equity_controller()
    # No option discoverer / quote provider attached.
    assert controller._option_discoverer is None
    assert controller._quote_provider is None
    # Equity universe unchanged.
    assert controller.config.user_constraints.allowed_symbols == frozenset(
        {"NSE:SBIN", "NSE:TCS", "NSE:INFY"}
    )
    # Underlyings empty.
    assert controller.config.user_constraints.allowed_option_underlyings == frozenset()
    # Phase B validation returns the no-op observation (no exec).
    results = _run_phase_b_validation(
        controller, _build_wiring(),
        max_quote_age_seconds=300.0,
    )
    assert any(
        r.get("event") == EVENT_PHASE_B_NO_EXECUTION and r.get("underlying") is None
        for r in results
    )


# --------------------------------------------------------------------------- #
# Test 2 — options_enabled = true wires real providers
# --------------------------------------------------------------------------- #
def test_options_enabled_attaches_real_providers() -> None:
    """Options-enabled controller: real providers attached, no synthetic."""
    controller = _build_options_enabled_controller()
    call = _make_nifty_call_instrument()
    repo = InstrumentRepository()
    repo.register(call)
    wiring = _build_wiring(repo=repo, seed_instruments=[call], authenticated=True)

    ok = _attach_options_wiring(controller, wiring)
    assert ok is True

    # Discoverer is a real CurrentOptionDiscoverer.
    assert isinstance(controller._option_discoverer, CurrentOptionDiscoverer)
    # Quote provider is the real CurrentOptionQuoteProvider.
    assert isinstance(controller._quote_provider, CurrentOptionQuoteProvider)
    # No synthetic chain provider.
    assert controller._chain_provider is None
    # Quote provider is authenticated in this test.
    assert controller._quote_provider.is_authenticated is True


# --------------------------------------------------------------------------- #
# Test 3 — provider unavailable (no auth / no providers)
# --------------------------------------------------------------------------- #
def test_provider_unavailable_clean_degraded_state() -> None:
    """When the quote provider is not authenticated, the wiring reports
    ``options_provider_unavailable`` and does NOT raise. Scheduler does
    not crash.
    """
    controller = _build_options_enabled_controller()
    call = _make_nifty_call_instrument()
    wiring = _build_wiring(
        repo=InstrumentRepository(), seed_instruments=[call],
        authenticated=False,
    )

    # The attach helper is defensive — must not raise.
    ok = _attach_options_wiring(controller, wiring)
    assert ok is True  # attachment itself succeeds
    # But the controller's quote provider is now detached (unavailable).
    assert controller._quote_provider is None

    # Validation pass produces a typed observation and no crash.
    observations = wiring.verify_deployment_capability(
        underlying="NIFTY",
        direction=OptionDirection.CALL,
        spot_price=25000.0,
    )
    assert len(observations) == 1
    assert observations[0].event == EVENT_PROVIDER_UNAVAILABLE
    assert observations[0].detail  # has a non-empty explanation


# --------------------------------------------------------------------------- #
# Test 4 — contract discovery resolves a real option contract
# --------------------------------------------------------------------------- #
def test_contract_discovery_resolves_real_instrument() -> None:
    """Discovery returns a real (CE/PE, strike, expiry, instrument_id,
    symbol) option contract sourced from the real InstrumentRepository.
    """
    expiry = (datetime.now(UTC) + timedelta(days=14)).date().isoformat()
    call = _make_nifty_call_instrument(strike=25000.0, expiry=expiry)
    put = _make_nifty_put_instrument(strike=25000.0, expiry=expiry)
    repo = InstrumentRepository()
    repo.register(call)
    repo.register(put)
    wiring = _build_wiring(repo=repo, seed_instruments=[call, put])

    # Discover CE.
    result_ce = wiring._discoverer.discover(
        underlying="NIFTY",
        direction=OptionDirection.CALL,
        spot_price=25000.0,
        config=DiscoveryConfig(allowed_option_types=["CE", "PE"]),
    )
    assert result_ce.has_selection
    selected = result_ce.selected
    assert selected is not None
    instr = selected.instrument
    assert instr.option_type == "CE"
    assert float(instr.strike) == 25000.0
    assert instr.expiry == expiry
    assert instr.contract_id
    assert instr.key  # NSE-style symbol

    # Discover PE.
    result_pe = wiring._discoverer.discover(
        underlying="NIFTY",
        direction=OptionDirection.PUT,
        spot_price=25000.0,
    )
    assert result_pe.has_selection
    assert result_pe.selected.instrument.option_type == "PE"


# --------------------------------------------------------------------------- #
# Test 5 — quote provider returns premium, not underlying spot
# --------------------------------------------------------------------------- #
def test_quote_provider_returns_premium_not_spot() -> None:
    """``CurrentOptionQuoteProvider.get_quote`` returns the actual
    option premium, NEVER the underlying NIFTY spot.
    """
    call = _make_nifty_call_instrument(strike=25000.0)
    repo = InstrumentRepository()
    repo.register(call)
    fake_quotes = {
        call.provider_symbol: _make_quote_record(ltp=147.50, age_seconds=5.0),
    }
    wiring = _build_wiring(
        repo=repo, seed_instruments=[call],
        quotes=fake_quotes, authenticated=True,
    )

    quote = wiring._quote_provider.get_quote(call)
    assert quote is not None
    assert quote.ltp == 147.50
    # Underlying spot (25000) must NOT equal the option premium.
    assert quote.ltp != 25000.0
    # Premium is much smaller than the underlying, as expected for a far-OTM call.
    assert quote.ltp < 25000.0


# --------------------------------------------------------------------------- #
# Test 6 — stale quote fails closed (no execution)
# --------------------------------------------------------------------------- #
def test_stale_quote_skips_no_execution() -> None:
    """A quote older than the max age is reported as
    ``options_quote_stale`` and the scheduler does NOT submit any order.
    """
    call = _make_nifty_call_instrument(strike=25000.0)
    repo = InstrumentRepository()
    repo.register(call)
    # Age the quote by 10 minutes — beyond the 60s default we pass.
    fake_quotes = {
        call.provider_symbol: _make_quote_record(ltp=120.0, age_seconds=600.0),
    }
    wiring = _build_wiring(
        repo=repo, seed_instruments=[call],
        quotes=fake_quotes, authenticated=True,
        max_quote_age_seconds=60.0,
    )
    controller = _build_options_enabled_controller()
    _attach_options_wiring(controller, wiring)

    observations = wiring.verify_deployment_capability(
        underlying="NIFTY",
        direction=OptionDirection.CALL,
        spot_price=25000.0,
        max_quote_age_seconds=60.0,
    )
    events = [o.event for o in observations]
    assert EVENT_DISCOVERY_OK in events
    assert EVENT_QUOTE_STALE in events
    # CRITICAL: no order execution event anywhere.
    assert EVENT_QUOTE_OK not in events
    assert EVENT_CAPABILITY_READY not in events


# --------------------------------------------------------------------------- #
# Test 7 — synthetic provider is rejected in production scheduler wiring
# --------------------------------------------------------------------------- #
def test_synthetic_provider_cannot_become_production_capability() -> None:
    """``InMemoryOptionsChainProvider`` is rejected by the production
    wiring — both via the explicit guard and by the fact that the
    scheduler's wiring NEVER instantiates it.
    """
    controller = _build_options_enabled_controller()
    # Attach a synthetic chain provider the wrong way (manually).
    synthetic = InMemoryOptionsChainProvider()
    controller.set_chain_provider(synthetic)

    # The Phase A capability endpoint already rejects this. The Phase B
    # wiring's ``attach_to_controller`` also refuses to attach when a
    # synthetic chain provider is already in the controller slot.
    call = _make_nifty_call_instrument()
    wiring = _build_wiring(repo=InstrumentRepository(), seed_instruments=[call])
    ok = wiring.attach_to_controller(controller)
    assert ok is False

    # The real provider wiring never instantiates InMemoryOptionsChainProvider.
    import backend.options_phase_b as ph_b
    src = ph_b.__file__ or ""
    with open(src, "r", encoding="utf-8") as fh:
        body = fh.read()
    # Synthetic class is only referenced as a guard, never as construction.
    assert "InMemoryOptionsChainProvider(" not in body


# --------------------------------------------------------------------------- #
# Test 8 — no execution paths are invoked during Phase B
# --------------------------------------------------------------------------- #
def test_no_option_execution_invoked_in_phase_b(monkeypatch) -> None:
    """Patch every conceivable execution entry point and verify none is
    called during the Phase B validation pass.
    """
    call = _make_nifty_call_instrument()
    repo = InstrumentRepository()
    repo.register(call)
    fake_quotes = {
        call.provider_symbol: _make_quote_record(ltp=200.0, age_seconds=2.0),
    }
    wiring = _build_wiring(
        repo=repo, seed_instruments=[call],
        quotes=fake_quotes, authenticated=True,
    )
    controller = _build_options_enabled_controller()
    _attach_options_wiring(controller, wiring)

    # Spy on every execution entry point. AutonomousController is a
    # frozen pydantic model so we patch the class methods directly.
    submit_calls: list[tuple] = []
    execute_calls: list[tuple] = []

    def _spy_submit(session_id, intent):  # type: ignore[no-untyped-def]
        submit_calls.append((session_id, intent))
        raise AssertionError("submit_order_intent must NOT be called in Phase B")

    def _spy_execute(*args, **kwargs):  # type: ignore[no-untyped-def]
        execute_calls.append((args, kwargs))
        raise AssertionError("execute_option_order must NOT be called in Phase B")

    monkeypatch.setattr(
        AutonomousController, "execute_option_order", _spy_execute
    )
    monkeypatch.setattr(
        controller.control_center, "submit_order_intent", _spy_submit
    )

    observations = wiring.verify_deployment_capability(
        underlying="NIFTY",
        direction=OptionDirection.CALL,
        spot_price=25000.0,
    )
    # Capability ready event confirms the stack validated.
    events = [o.event for o in observations]
    assert EVENT_CAPABILITY_READY in events
    # No execution attempts.
    assert submit_calls == []
    assert execute_calls == []


# --------------------------------------------------------------------------- #
# Test 9 — equity regression (options_enabled = false leaves equity alone)
# --------------------------------------------------------------------------- #
def test_equity_regression_options_disabled(monkeypatch) -> None:
    """An equity-only bot's behaviour is byte-for-byte unchanged: the
    scheduler does not touch option provider state and the controller's
    option slots remain None.
    """
    monkeypatch.delenv("AUTONOMOUS_OPTION_UNDERLYINGS", raising=False)
    controller = _build_pure_equity_controller()

    # No providers, ever.
    assert controller._option_discoverer is None
    assert controller._quote_provider is None
    assert controller._chain_provider is None
    # No NIFTY in the equity universe.
    assert "NSE:NIFTY" not in controller.config.user_constraints.allowed_symbols
    assert "NIFTY" not in controller.config.user_constraints.allowed_symbols
    # No option underlyings either.
    assert controller.config.user_constraints.allowed_option_underlyings == frozenset()

    # And the Phase B validation is a no-op (no eligible deployments).
    results = _run_phase_b_validation(
        controller, _build_wiring(), max_quote_age_seconds=300.0,
    )
    assert results == [
        {
            "event": EVENT_PHASE_B_NO_EXECUTION,
            "underlying": None,
            "detail": "no options-enabled deployments",
        }
    ]


# --------------------------------------------------------------------------- #
# Test 10 — NIFTY option eligibility via configuration, not hard-coding
# --------------------------------------------------------------------------- #
def test_nifty_option_eligibility_via_configuration() -> None:
    """NIFTY becomes option-eligible through
    ``UserConstraints.allowed_option_underlyings``, NOT through
    ``allowed_symbols``. The two sets are disjoint.
    """
    controller = _build_options_enabled_controller(
        underlyings=frozenset({"NIFTY"}),
    )
    # Disjoint sets — NIFTY in options, never in equity.
    assert "NIFTY" in controller.config.user_constraints.allowed_option_underlyings
    assert "NIFTY" not in controller.config.user_constraints.allowed_symbols
    assert "NSE:NIFTY" not in controller.config.user_constraints.allowed_symbols

    # Discovery is invoked via the real selector using the real repo.
    expiry = (datetime.now(UTC) + timedelta(days=14)).date().isoformat()
    call = _make_nifty_call_instrument(strike=25000.0, expiry=expiry)
    put = _make_nifty_put_instrument(strike=25000.0, expiry=expiry)
    repo = InstrumentRepository()
    repo.register(call)
    repo.register(put)
    wiring = _build_wiring(repo=repo, seed_instruments=[call, put])
    _attach_options_wiring(controller, wiring)

    selector = OptionsContractSelector(repository=repo)
    ce = selector.select(
        underlying="NIFTY", direction=OptionDirection.CALL,
        spot_price=25000.0, as_of=datetime.now(UTC).date().isoformat(),
    )
    pe = selector.select(
        underlying="NIFTY", direction=OptionDirection.PUT,
        spot_price=25000.0, as_of=datetime.now(UTC).date().isoformat(),
    )
    assert ce is not None
    assert pe is not None
    assert ce.option_type == "CE"
    assert pe.option_type == "PE"
    assert ce.instrument_id is not None  # resolved via repository


# --------------------------------------------------------------------------- #
# Test 11 — full integration: discovery -> quote -> capability ready -> NO ORDER
# --------------------------------------------------------------------------- #
def test_full_phase_b_flow_no_order() -> None:
    """End-to-end deterministic test of the Phase B flow:

        options-enabled deployment
            -> scheduler provider initialization
            -> NIFTY option eligibility
            -> contract discovery
            -> option quote lookup
            -> valid premium
            -> phase-B success
            -> NO ORDER

    The test MUST terminate before any execution.
    """
    expiry = (datetime.now(UTC) + timedelta(days=14)).date().isoformat()
    call = _make_nifty_call_instrument(strike=25000.0, expiry=expiry)
    put = _make_nifty_put_instrument(strike=25000.0, expiry=expiry)
    repo = InstrumentRepository()
    repo.register(call)
    repo.register(put)
    fake_quotes = {
        call.provider_symbol: _make_quote_record(ltp=185.50, age_seconds=3.0),
        put.provider_symbol: _make_quote_record(ltp=172.25, age_seconds=3.0),
    }
    wiring = _build_wiring(
        repo=repo, seed_instruments=[call, put],
        quotes=fake_quotes, authenticated=True,
        max_quote_age_seconds=60.0,
    )
    controller = _build_options_enabled_controller(
        underlyings=frozenset({"NIFTY"}),
    )
    assert _attach_options_wiring(controller, wiring) is True

    observations = wiring.verify_deployment_capability(
        underlying="NIFTY",
        direction=OptionDirection.CALL,
        spot_price=25000.0,
        max_quote_age_seconds=60.0,
    )
    events = [o.event for o in observations]
    # The full chain must be visible in the event log.
    assert EVENT_DISCOVERY_OK in events
    assert EVENT_QUOTE_OK in events
    assert EVENT_CAPABILITY_READY in events
    assert EVENT_PHASE_B_NO_EXECUTION not in events  # this is added by scheduler, not wiring

    # The premium is the OPTION premium, not the underlying spot.
    ready_obs = [o for o in observations if o.event == EVENT_CAPABILITY_READY][0]
    assert ready_obs.premium == 185.50
    assert ready_obs.instrument_id == call.contract_id

    # And the controller's order-execution surface is still untouched.
    assert controller._option_discoverer is not None
    assert controller._quote_provider is not None


# --------------------------------------------------------------------------- #
# Test 12 — phase B validation discovers options-enabled deployments
# --------------------------------------------------------------------------- #
def test_phase_b_validation_lists_options_deployments(monkeypatch) -> None:
    """The scheduler's ``_list_options_enabled_deployments`` returns
    only deployments whose ``options_enabled`` is True AND whose
    underlying is in ``allowed_option_underlyings``.
    """
    from sqlalchemy import create_engine
    from trading_system.paper.control import PaperTradingControlCenter
    from trading_system.paper.deployment import (
        PaperDeploymentConfig,
        PaperDeploymentStatus,
    )
    from trading_system.research.evidence import EvidenceStore
    from trading_system.research.strategy_intelligence import StrategyIntelligence
    from trading_system.research.strategy_registry import StrategyRegistry

    engine = create_engine("sqlite://")
    store = EvidenceStore(engine)
    registry = StrategyRegistry(store)
    intelligence = StrategyIntelligence(registry)
    center = PaperTradingControlCenter(
        registry=registry, intelligence=intelligence,
    )
    controller = _build_options_enabled_controller(
        underlyings=frozenset({"NIFTY"}),
    )
    # Override control_center on the controller without re-running __init__.
    object.__setattr__(controller, "control_center", center)

    # Create a fake options-enabled deployment by inserting a record.
    # The PaperDeploymentRecord stores options config in dedicated columns;
    # the remaining deployment fields live in config_json.
    from trading_system.paper.deployment import PaperDeploymentRecord
    cfg = PaperDeploymentConfig(
        execution_mode="paper",
        initial_cash=100000.0,
        allow_short=False,
        options_enabled=True,
        allowed_option_types=["CE", "PE"],
        max_options_contracts_per_trade=1,
    )
    rec = PaperDeploymentRecord(
        deployment_id="dep-nifty-opts",
        strategy_id="sma",
        strategy_spec_hash="hash",
        symbol="NSE:NIFTY",
        timeframe="1d",
        dataset_id="ds",
        config_json=cfg.model_dump_json(),
        status=PaperDeploymentStatus.ACTIVE.value,
        evidence_ids_json="[]",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        notes="bot:bot-options-b",
        options_enabled=True,
        allowed_option_types_json='["CE","PE"]',
        max_options_contracts_per_trade=1,
    )
    with registry.store._Session() as s:
        s.add(rec)
        s.commit()

    deps = _list_options_enabled_deployments(
        controller, frozenset({"NIFTY"}),
    )
    assert len(deps) == 1
    assert deps[0].deployment_id == "dep-nifty-opts"
    assert deps[0].config.options_enabled is True


# --------------------------------------------------------------------------- #
# Test 13 — env config helpers
# --------------------------------------------------------------------------- #
def test_env_helpers_default_to_safe(monkeypatch) -> None:
    monkeypatch.delenv("AUTONOMOUS_OPTION_UNDERLYINGS", raising=False)
    monkeypatch.delenv("AUTONOMOUS_MAX_OPTION_QUOTE_AGE_SECONDS", raising=False)
    assert _env_option_underlyings() == frozenset()
    assert _env_max_option_quote_age() == 300.0

    monkeypatch.setenv("AUTONOMOUS_OPTION_UNDERLYINGS", "NIFTY,BANKNIFTY")
    assert _env_option_underlyings() == frozenset({"NIFTY", "BANKNIFTY"})

    monkeypatch.setenv("AUTONOMOUS_MAX_OPTION_QUOTE_AGE_SECONDS", "45")
    assert _env_max_option_quote_age() == 45.0

    monkeypatch.setenv("AUTONOMOUS_MAX_OPTION_QUOTE_AGE_SECONDS", "garbage")
    assert _env_max_option_quote_age() == 300.0  # falls back to default
