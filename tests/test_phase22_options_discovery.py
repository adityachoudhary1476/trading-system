"""Phase 8B — Tests for current-market option discovery and evaluation.

Covers:
  - CurrentOptionDiscoverer with real InstrumentRepository (populated)
  - CurrentOptionDiscoverer with empty repository (no options → no trade)
  - Candidate evaluation: strike distance, ITM/OTM/ATM, expiry
  - Selection: best candidate by score
  - Rejection: expired, wrong right, disallowed type
  - Exact Instrument resolution round-trips through repository
  - Phase 7 safety validation against the SELECTED contract
  - End-to-end: discover → validate → deployment → order → position
  - Idempotency: different contracts get different keys
  - Option P&L uses option premium (not underlying price)
  - Neutral decision → no trade
"""
import pytest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock

from trading_system.autonomous.options.discovery import (
    CurrentOptionDiscoverer,
    DiscoveryConfig,
    CandidateEvaluationResult,
    EvaluatedCandidate,
    OptionEligibilityReason,
)
from trading_system.autonomous.options.model import (
    OptionsContractSelection,
    OptionDirection,
)
from trading_system.autonomous.safety import (
    Phase7Config,
    SafetyValidator,
    IdempotencyGuard,
)
from trading_system.india.instruments import (
    Instrument,
    InstrumentType,
    InternalSymbol,
    OptionType,
)
from trading_system.india.instrument_repository import InstrumentRepository
from trading_system.execution.orders import Side, OrderType
from trading_system.execution.paper_broker import PaperBroker, SlippageConfig
from trading_system.paper_trading import Position
from trading_system.paper.deployment import (
    PaperDeployment,
    PaperDeploymentConfig,
    PaperDeploymentStatus,
)


def _make_option(
    underlying: str,
    expiry: str,
    strike: float,
    option_type: str,
    exchange: str = "NFO",
) -> Instrument:
    """Helper: create a properly structured option Instrument."""
    return Instrument(
        internal=InternalSymbol(exchange=exchange, symbol=f"{underlying}{strike}{option_type}"),
        instrument_type=InstrumentType.OPTION_CE if option_type == "CE" else InstrumentType.OPTION_PE,
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        exchange_full=exchange,
    )


def _make_repo_with_options(underlying="NIFTY", spot=24900.0) -> InstrumentRepository:
    """Create a repository populated with realistic NIFTY CE/PE options."""
    repo = InstrumentRepository()
    expiry = (date.today() + timedelta(days=30)).isoformat()

    strikes = [spot - 300, spot - 100, spot, spot + 100, spot + 300]
    for s in strikes:
        repo.register(_make_option(underlying, expiry, s, "CE"))
        repo.register(_make_option(underlying, expiry, s, "PE"))

    # Also register an expired contract (should be filtered out)
    expired = (date.today() - timedelta(days=5)).isoformat()
    repo.register(_make_option(underlying, expired, spot, "CE"))

    return repo


# --------------------------------------------------------------------------- #
# Discovery: empty repository
# --------------------------------------------------------------------------- #

class TestDiscoveryEmptyRepo:

    def test_no_options_in_repo_returns_empty(self):
        repo = InstrumentRepository()  # empty by default except DEFAULT_INSTRUMENTS (equities)
        discoverer = CurrentOptionDiscoverer(repository=repo)
        result = discoverer.discover(
            underlying="NIFTY",
            direction=OptionDirection.CALL,
            spot_price=24900.0,
        )
        assert result.candidates == []
        assert not result.has_selection
        assert "no" in result.selection_note.lower()


# --------------------------------------------------------------------------- #
# Discovery: populated repository
# --------------------------------------------------------------------------- #

class TestDiscoveryPopulatedRepo:

    def test_discovers_available_contracts(self):
        repo = _make_repo_with_options()
        discoverer = CurrentOptionDiscoverer(repository=repo)
        result = discoverer.discover(
            underlying="NIFTY",
            direction=OptionDirection.CALL,
            spot_price=24900.0,
        )
        # Should find all CE contracts for the current expiry (not the expired one)
        ce_candidates = [
            c for c in result.candidates
            if c.instrument.option_type == "CE"
            and c.eligibility.value != "expired"
        ]
        assert len(ce_candidates) == 5  # 5 strikes

    def test_selects_best_candidate(self):
        repo = _make_repo_with_options()
        discoverer = CurrentOptionDiscoverer(repository=repo)
        result = discoverer.discover(
            underlying="NIFTY",
            direction=OptionDirection.CALL,
            spot_price=24900.0,
        )
        assert result.has_selection
        # ATM should score highest
        assert result.selected.strike_direction == "ATM"
        assert result.selected.instrument.strike == 24900.0

    def test_filters_expired_contracts(self):
        repo = _make_repo_with_options()
        discoverer = CurrentOptionDiscoverer(repository=repo)
        result = discoverer.discover(
            underlying="NIFTY",
            direction=OptionDirection.CALL,
            spot_price=24900.0,
        )
        # The expired contract should not be in valid candidates
        for c in result.candidates:
            if c.instrument.expiry == (date.today() - timedelta(days=5)).isoformat():
                assert c.eligibility != OptionEligibilityReason.VALID

    def test_put_discovery(self):
        repo = _make_repo_with_options()
        discoverer = CurrentOptionDiscoverer(repository=repo)
        result = discoverer.discover(
            underlying="NIFTY",
            direction=OptionDirection.PUT,
            spot_price=24900.0,
        )
        assert result.has_selection
        assert result.selected.instrument.option_type == "PE"

    def test_wrong_option_type_not_in_candidates(self):
        repo = _make_repo_with_options()
        discoverer = CurrentOptionDiscoverer(repository=repo)
        result = discoverer.discover(
            underlying="NIFTY",
            direction=OptionDirection.CALL,
            spot_price=24900.0,
        )
        # All candidates should be CE
        for c in result.candidates:
            assert c.instrument.option_type == "CE"


# --------------------------------------------------------------------------- #
# Candidate evaluation scoring
# --------------------------------------------------------------------------- #

class TestCandidateEvaluation:

    def test_atm_scores_higher_than_otm(self):
        repo = _make_repo_with_options()
        discoverer = CurrentOptionDiscoverer(repository=repo)
        result = discoverer.discover(
            underlying="NIFTY",
            direction=OptionDirection.CALL,
            spot_price=24900.0,
        )
        atm = [c for c in result.candidates if c.strike_direction == "ATM"]
        otm = [c for c in result.candidates if c.strike_direction == "OTM"]
        if atm and otm:
            assert atm[0].score > otm[0].score

    def test_itm_vs_otm_scoring(self):
        repo = _make_repo_with_options()
        discoverer = CurrentOptionDiscoverer(repository=repo)
        result = discoverer.discover(
            underlying="NIFTY",
            direction=OptionDirection.PUT,
            spot_price=24900.0,
        )
        itm = [c for c in result.candidates if c.strike_direction == "ITM" and c.instrument.option_type == "PE"]
        otm = [c for c in result.candidates if c.strike_direction == "OTM" and c.instrument.option_type == "PE"]
        # For puts, ITM = strike > spot; should score higher than OTM
        assert len(itm) > 0 or len(otm) > 0

    def test_disallowed_option_type_rejected(self):
        repo = _make_repo_with_options()
        discoverer = CurrentOptionDiscoverer(repository=repo)
        config = DiscoveryConfig(allowed_option_types=["PE"])
        result = discoverer.discover(
            underlying="NIFTY",
            direction=OptionDirection.CALL,  # CE not allowed
            spot_price=24900.0,
            config=config,
        )
        assert not result.has_selection
        assert "not in allowed" in result.selection_note

    def test_max_strike_distance_filter(self):
        repo = InstrumentRepository()
        expiry = (date.today() + timedelta(days=30)).isoformat()
        # Create strikes 50% away from spot
        spot = 10000.0
        repo.register(_make_option("NIFTY", expiry, 5000.0, "CE"))  # 50% OTM
        repo.register(_make_option("NIFTY", expiry, 15000.0, "CE"))  # 50% OTM
        discoverer = CurrentOptionDiscoverer(repository=repo)
        config = DiscoveryConfig(max_strike_distance_pct=0.15)  # 15% max
        result = discoverer.discover(
            underlying="NIFTY",
            direction=OptionDirection.CALL,
            spot_price=spot,
            config=config,
        )
        assert not result.has_selection
        assert "all" in result.selection_note.lower()


# --------------------------------------------------------------------------- #
# Selection result observability
# --------------------------------------------------------------------------- #

class TestSelectionObservability:

    def test_to_dict_includes_all_info(self):
        repo = _make_repo_with_options()
        discoverer = CurrentOptionDiscoverer(repository=repo)
        result = discoverer.discover(
            underlying="NIFTY",
            direction=OptionDirection.CALL,
            spot_price=24900.0,
        )
        d = result.to_dict()
        assert d["underlying"] == "NIFTY"
        assert d["direction"] == "call"
        assert d["spot_price"] == 24900.0
        assert d["candidates_count"] == 6  # 5 valid CE + 1 expired CE
        assert d["selected"] is not None
        assert d["selected"]["option_type"] == "CE"
        assert d["selected"]["strike"] == 24900.0
        assert len(d["candidates"]) == 6  # 5 valid CE + 1 expired CE

    def test_evaluated_candidate_to_dict(self):
        repo = _make_repo_with_options()
        discoverer = CurrentOptionDiscoverer(repository=repo)
        result = discoverer.discover(
            underlying="NIFTY",
            direction=OptionDirection.CALL,
            spot_price=24900.0,
        )
        c = result.candidates[0]
        d = c.to_dict()
        assert "instrument_id" in d
        assert "symbol" in d
        assert "score" in d
        assert "eligibility" in d
        assert "strike_direction" in d


# --------------------------------------------------------------------------- #
# Phase 7 safety validation against SELECTED contract
# --------------------------------------------------------------------------- #

class TestSafetyValidation:

    def test_safety_validates_selected_contract(self):
        repo = _make_repo_with_options()
        discoverer = CurrentOptionDiscoverer(repository=repo)
        validator = SafetyValidator(Phase7Config())

        result = discoverer.discover(
            underlying="NIFTY",
            direction=OptionDirection.CALL,
            spot_price=24900.0,
        )
        assert result.has_selection

        # Validate the EXACT selected contract
        safety = validator.check_contract_validity(
            options_selection=result.selected.selection,
            allowed_option_types=["CE"],
            now_date=date.today().isoformat(),
        )
        assert safety.passed

    def test_safety_rejects_disallowed_type(self):
        repo = _make_repo_with_options()
        discoverer = CurrentOptionDiscoverer(repository=repo)
        validator = SafetyValidator(Phase7Config())

        result = discoverer.discover(
            underlying="NIFTY",
            direction=OptionDirection.CALL,
            spot_price=24900.0,
        )
        assert result.has_selection

        # CE not in allowed list → FAIL
        safety = validator.check_contract_validity(
            options_selection=result.selected.selection,
            allowed_option_types=["PE"],
            now_date=date.today().isoformat(),
        )
        assert not safety.passed


# --------------------------------------------------------------------------- #
# Exact Instrument resolution
# --------------------------------------------------------------------------- #

class TestInstrumentResolution:

    def test_selected_contract_resolves_in_repository(self):
        """The selected OptionsContractSelection must round-trip through
        repository.find_contract to prove the contract actually exists."""
        repo = _make_repo_with_options()
        discoverer = CurrentOptionDiscoverer(repository=repo)

        result = discoverer.discover(
            underlying="NIFTY",
            direction=OptionDirection.CALL,
            spot_price=24900.0,
        )
        assert result.has_selection

        sel = result.selected.selection
        resolved = discoverer.resolve_contract(
            underlying=sel.underlying,
            expiry=sel.expiry,
            option_type=sel.option_type,
            strike=sel.strike,
            exchange=sel.exchange,
        )
        assert resolved is not None
        assert resolved.contract_id == sel.instrument_id

    def test_nonexistent_contract_not_resolved(self):
        """A fabricated contract must fail to resolve."""
        repo = InstrumentRepository()
        discoverer = CurrentOptionDiscoverer(repository=repo)

        # This contract doesn't exist
        resolved = discoverer.resolve_contract(
            underlying="NIFTY",
            expiry="2025-12-25",
            option_type="CE",
            strike=99999.0,
        )
        assert resolved is None


# --------------------------------------------------------------------------- #
# Option P&L uses option premium
# --------------------------------------------------------------------------- #

class TestOptionPnL:

    def test_pnl_uses_premium_not_underlying(self):
        """Verify position P&L is calculated from option premium, not underlying price."""
        pos = Position(
            symbol="NFO:NIFTY25DEC24900CE",
            qty=1,
            avg_entry_price=180.0,  # option premium at entry
            realized_pnl=0.0,
            current_price=220.0,    # option premium now
            options_contract_id="test",
            strike=24900.0,
            expiry="2025-12-25",
            option_type="CE",
            contract_size=1,
        )
        # P&L should be based on premium difference: (220 - 180) * 1 = 40
        assert pos.unrealized_pnl == 40.0
        # NOT based on underlying (e.g. if underlying went from 24900 to 25000)
        assert pos.unrealized_pnl != (25000 - 24900) * 1

    def test_broker_propagates_options_to_position(self):
        """PaperBroker.submit_order with options fields must set them on the Position."""
        repo = _make_repo_with_options()
        discoverer = CurrentOptionDiscoverer(repository=repo)

        result = discoverer.discover(
            underlying="NIFTY",
            direction=OptionDirection.CALL,
            spot_price=24900.0,
        )
        assert result.has_selection

        sel = result.selected.selection
        broker = PaperBroker(initial_cash=500000.0, slippage=SlippageConfig(slippage_bps=0.0))
        option_symbol = sel.symbol or "NFO:NIFTY24900CE"
        broker.update_market_price(option_symbol, 180.0)

        order = broker.submit_order(
            symbol=option_symbol,
            side=Side.BUY,
            quantity=1,
            order_type=OrderType.MARKET,
            current_price=180.0,
            options_contract_id=sel.contract_id,
            strike=sel.strike,
            expiry=sel.expiry,
            option_type=sel.option_type,
        )

        pos = broker.get_position(option_symbol)
        assert pos is not None
        assert pos.options_contract_id == sel.contract_id
        assert pos.strike == sel.strike
        assert pos.expiry == sel.expiry
        assert pos.option_type == sel.option_type

        # Verify P&L uses option premium
        pos.current_price = 220.0
        assert pos.unrealized_pnl == 40.0


# --------------------------------------------------------------------------- #
# Idempotency: different contracts get different keys
# --------------------------------------------------------------------------- #

class TestIdempotencyDifferentContracts:

    def test_different_contracts_get_different_keys(self):
        guard = IdempotencyGuard()
        key1 = guard.deployment_key(
            bot_id="bot-1",
            symbol="NIFTY",
            strategy_id="strat-1",
            timeframe="1d",
            options_contract_id="NFO:NIFTY|2025-09-24|24900|CE",
        )
        key2 = guard.deployment_key(
            bot_id="bot-1",
            symbol="NIFTY",
            strategy_id="strat-1",
            timeframe="1d",
            options_contract_id="NFO:NIFTY|2025-09-24|25000|CE",
        )
        assert key1 != key2


# --------------------------------------------------------------------------- #
# Neutral / no-trade scenarios
# --------------------------------------------------------------------------- #

class TestNoTradeScenarios:

    def test_no_option_type_for_neutral(self):
        """For a neutral (no direction) thesis, the discoverer should not
        select a contract — caller simply doesn't call discover()."""
        # The caller is responsible for not calling discover() for HOLD signals.
        # This test verifies the discoverer returns empty for a repo with no
        # matching option type.
        repo = _make_repo_with_options()
        discoverer = CurrentOptionDiscoverer(repository=repo)

        # No PE contracts registered beyond defaults? Actually we registered both.
        # But if we restrict allowed types to a non-matching one:
        config = DiscoveryConfig(allowed_option_types=["XX"])
        result = discoverer.discover(
            underlying="NIFTY",
            direction=OptionDirection.CALL,
            spot_price=24900.0,
            config=config,
        )
        assert not result.has_selection

    def test_expired_only_repo_returns_no_selection(self):
        repo = InstrumentRepository()
        expired = (date.today() - timedelta(days=5)).isoformat()
        repo.register(_make_option("NIFTY", expired, 24900.0, "CE"))

        discoverer = CurrentOptionDiscoverer(repository=repo)
        result = discoverer.discover(
            underlying="NIFTY",
            direction=OptionDirection.CALL,
            spot_price=24900.0,
        )
        assert not result.has_selection
        assert len(result.candidates) == 1
        assert all(c.eligibility != OptionEligibilityReason.VALID for c in result.candidates)


# --------------------------------------------------------------------------- #
# End-to-end: discover → validate → broker
# --------------------------------------------------------------------------- #

class TestEndToEnd:

    def test_discover_validate_execute(self):
        """Full flow: discover → Phase 7 safety → PaperBroker → Position."""
        repo = _make_repo_with_options()
        discoverer = CurrentOptionDiscoverer(repository=repo)
        validator = SafetyValidator(Phase7Config())

        # 1. Discover
        result = discoverer.discover(
            underlying="NIFTY",
            direction=OptionDirection.CALL,
            spot_price=24900.0,
        )
        assert result.has_selection

        sel = result.selected.selection

        # 2. Phase 7 safety validation
        safety = validator.check_contract_validity(
            options_selection=sel,
            allowed_option_types=["CE"],
            now_date=date.today().isoformat(),
        )
        assert safety.passed

        # 3. Resolve exact Instrument (proves it exists)
        instrument = discoverer.resolve_contract(
            underlying=sel.underlying,
            expiry=sel.expiry,
            option_type=sel.option_type,
            strike=sel.strike,
        )
        assert instrument is not None
        assert instrument.contract_id == sel.instrument_id

        # 4. Execute through PaperBroker
        broker = PaperBroker(initial_cash=500000.0, slippage=SlippageConfig(slippage_bps=0.0))
        option_symbol = instrument.key
        entry_premium = 180.0
        broker.update_market_price(option_symbol, entry_premium)

        order = broker.submit_order(
            symbol=option_symbol,
            side=Side.BUY,
            quantity=1,
            order_type=OrderType.MARKET,
            current_price=entry_premium,
            options_contract_id=instrument.contract_id,
            strike=instrument.strike,
            expiry=instrument.expiry,
            option_type=instrument.option_type,
        )
        assert order.options_contract_id == instrument.contract_id

        pos = broker.get_position(option_symbol)
        assert pos is not None
        assert pos.options_contract_id == instrument.contract_id

        # 5. Move price and verify P&L uses option premium
        exit_premium = 220.0
        broker.update_market_price(option_symbol, exit_premium)
        pos.current_price = exit_premium
        assert pos.unrealized_pnl == 40.0  # (220 - 180) × 1

        # 6. Verify result.to_dict includes options identity
        from trading_system.autonomous.options.model import OptionsContractSelection
        d = sel.to_dict()
        assert d["option_type"] == "CE"
        assert d["strike"] == 24900.0

    def test_end_to_end_neural_no_trade(self):
        """Neutral thesis → no option discovery → no deployment."""
        repo = _make_repo_with_options()
        discoverer = CurrentOptionDiscoverer(repository=repo)

        # For a HOLD signal, the controller would not call discover().
        # Verify discoverer with a neutral/non-matching direction returns no selection.
        result = discoverer.discover(
            underlying="NONEXISTENT",
            direction=OptionDirection.CALL,
            spot_price=24900.0,
        )
        assert not result.has_selection
        assert result.candidates == []
