"""Phase 8 — Options Contract Selection Layer tests.

Covers:
  - OptionsInstrument: symbol/contract_id formatting, frozen, equality, hashing
  - OptionQuote: mid property
  - OptionsChain: get_quote, nearest_strike, strikes_above/below
  - InMemoryOptionsChainProvider: deterministic chains, spot management, expiry logic
  - OptionContractResolver: resolve_call/put/vertical
  - OptionsStructureBuilder: signal mapping, strategy builders, fail-closed behavior
  - OptionsTradePlan: to_order_intents, to_dict, is_empty
  - StrategyMapping customization
  - Static safety scan: no live broker references in options_contract.py
"""
from __future__ import annotations

import ast
import os
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from trading_system.execution.orders import OrderIntent, OrderType, Side
from trading_system.india.instruments import OptionType
from trading_system.autonomous.decision import TradingDecision
from trading_system.autonomous.options_contract import (
    DEFAULT_OPTIONS_CONFIG,
    InMemoryOptionsChainProvider,
    InMemoryOptionsChainProvider as IC,
    OptionContractResolver,
    OptionLeg,
    OptionsChain,
    OptionsInstrument,
    OptionsStructureBuilder,
    OptionsStrategy,
    OptionsTradeConfig,
    OptionsTradePlan,
    ResolveResult,
    StrategyMapping,
    OptionQuote,
    OptionsChainProvider,
)

from trading_system.strategy_factory.contract import SignalAction, StrategySignal

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _make_signal(
    action: SignalAction = SignalAction.BUY,
    symbol: str = "NSE:SBIN",
    reference_price: float = 100.0,
    confidence: float = 0.85,
    strategy_id: str = "ema_crossover",
) -> StrategySignal:
    ts = datetime(2024, 1, 15, 10, 30, tzinfo=UTC)
    return StrategySignal(
        action=action,
        strategy_id=strategy_id,
        timestamp=ts,
        symbol=symbol,
        reference_price=reference_price,
        confidence=confidence,
    )


def _make_decision(
    action: SignalAction = SignalAction.BUY,
    symbol: str = "NSE:SBIN",
    reference_price: float = 100.0,
    decision_id: str = "dec-001",
) -> TradingDecision:
    sig = _make_signal(action=action, symbol=symbol, reference_price=reference_price)
    return TradingDecision(
        decision_id=decision_id,
        opportunity_symbol=symbol,
        opportunity_rank=1,
        market_timestamp=datetime(2024, 1, 15, 10, 30, tzinfo=UTC).isoformat(),
        snapshot_identity="snap-001",
        signal=sig,
        is_valid=True,
        status="valid",
        decision_timestamp=datetime(2024, 1, 15, 10, 31, tzinfo=UTC).isoformat(),
    )


def _make_provider(
    spot: float = 100.0,
    underlying: str = "NSE:SBIN",
    vol: float = 0.20,
    ref_date: date = date(2024, 1, 15),
) -> InMemoryOptionsChainProvider:
    provider = InMemoryOptionsChainProvider(
        default_volatility=vol,
        reference_date=ref_date,
    )
    provider.set_spot(underlying, spot)
    return provider


# --------------------------------------------------------------------------- #
# OptionsInstrument
# --------------------------------------------------------------------------- #

class TestOptionsInstrument:
    def test_symbol_format_NSE_underlying(self):
        inst = OptionsInstrument(underlying="NSE:SBIN", expiry="2025-12-19", strike=700, option_type=OptionType.CE)
        assert inst.symbol == "NFO:SBIN25DEC700CE"

    def test_symbol_format_bare_underlying(self):
        inst = OptionsInstrument(underlying="SBIN", expiry="2025-01-17", strike=100, option_type=OptionType.PE)
        assert inst.symbol == "NFO:SBIN25JAN100PE"

    def test_contract_id_format(self):
        inst = OptionsInstrument(underlying="NSE:SBIN", expiry="2025-12-19", strike=700, option_type=OptionType.CE)
        assert inst.contract_id == "NFO:SBIN|2025-12-19|700|CE|american"

    def test_frozen_dataclass(self):
        inst = OptionsInstrument(underlying="NSE:SBIN", expiry="2025-12-19", strike=700, option_type=OptionType.CE)
        with pytest.raises(Exception):
            inst.strike = 800

    def test_equality_same_contract_id(self):
        a = OptionsInstrument(underlying="NSE:SBIN", expiry="2025-12-19", strike=700, option_type=OptionType.CE)
        b = OptionsInstrument(underlying="SBIN", expiry="2025-12-19", strike=700, option_type=OptionType.CE)
        assert a == b

    def test_inequality_different_strike(self):
        a = OptionsInstrument(underlying="NSE:SBIN", expiry="2025-12-19", strike=700, option_type=OptionType.CE)
        b = OptionsInstrument(underlying="NSE:SBIN", expiry="2025-12-19", strike=750, option_type=OptionType.CE)
        assert a != b

    def test_hashable(self):
        inst = OptionsInstrument(underlying="NSE:SBIN", expiry="2025-12-19", strike=700, option_type=OptionType.CE)
        assert hash(inst) == hash(inst.contract_id)

    def test_to_dict(self):
        inst = OptionsInstrument(underlying="NSE:SBIN", expiry="2025-12-19", strike=700, option_type=OptionType.CE)
        d = inst.to_dict()
        assert d["underlying"] == "NSE:SBIN"
        assert d["expiry"] == "2025-12-19"
        assert d["strike"] == 700
        assert d["option_type"] == "CE"
        assert d["exchange"] == "NFO"
        assert d["style"] == "american"
        assert d["contract_id"] == inst.contract_id
        assert d["symbol"] == inst.symbol

    def test_default_exchange_is_NFO(self):
        inst = OptionsInstrument(underlying="NSE:SBIN", expiry="2025-12-19", strike=700, option_type=OptionType.CE)
        assert inst.exchange == "NFO"

    def test_default_style_is_american(self):
        inst = OptionsInstrument(underlying="NSE:SBIN", expiry="2025-12-19", strike=700, option_type=OptionType.CE)
        assert inst.style.value == "american"


# --------------------------------------------------------------------------- #
# OptionQuote
# --------------------------------------------------------------------------- #

class TestOptionQuote:
    def test_mid_calculates_from_bid_ask(self):
        q = OptionQuote(strike=100, bid=2.0, ask=2.1)
        assert q.mid == 2.05

    def test_mid_zero_when_bid_or_ask_zero(self):
        q = OptionQuote(strike=100, bid=0, ask=2.1)
        assert q.mid == 0.0

    def test_defaults(self):
        q = OptionQuote(strike=100, bid=1.0, ask=1.1)
        assert q.last is None
        assert q.volume == 0
        assert q.open_interest == 0


# --------------------------------------------------------------------------- #
# OptionsChain
# --------------------------------------------------------------------------- #

class TestOptionsChain:
    def test_get_quote_call(self):
        chain = OptionsChain(underlying="NSE:SBIN", expiry="2025-12-19", spot_price=100, strike_interval=5,
                             strikes=[95, 100, 105],
                             call_quotes={100: OptionQuote(100, 1.0, 1.1)},
                             put_quotes={100: OptionQuote(100, 1.0, 1.1)})
        q = chain.get_quote(100, OptionType.CE)
        assert q is not None
        assert q.strike == 100

    def test_get_quote_put(self):
        chain = OptionsChain(underlying="NSE:SBIN", expiry="2025-12-19", spot_price=100, strike_interval=5,
                             strikes=[95, 100, 105],
                             call_quotes={},
                             put_quotes={100: OptionQuote(100, 1.0, 1.1)})
        q = chain.get_quote(100, OptionType.PE)
        assert q is not None

    def test_get_quote_missing_returns_none(self):
        chain = OptionsChain(underlying="NSE:SBIN", expiry="2025-12-19", spot_price=100, strike_interval=5,
                             strikes=[100], call_quotes={}, put_quotes={})
        assert chain.get_quote(100, OptionType.CE) is None

    def test_nearest_strike(self):
        strikes = [95, 100, 105, 110]
        assert OptionsChain.nearest_strike(strikes, 98) == 100
        assert OptionsChain.nearest_strike(strikes, 108) == 110

    def test_nearest_strike_empty_returns_none(self):
        assert OptionsChain.nearest_strike([], 100) is None

    def test_strikes_above(self):
        strikes = [95, 100, 105, 110]
        result = OptionsChain.strikes_above(strikes, 100)
        assert result == [105, 110]

    def test_strikes_below(self):
        strikes = [95, 100, 105, 110]
        result = OptionsChain.strikes_below(strikes, 105)
        assert result == [100, 95]


# --------------------------------------------------------------------------- #
# InMemoryOptionsChainProvider
# --------------------------------------------------------------------------- #

class TestInMemoryOptionsChainProvider:
    def test_get_chain_returns_none_without_spot(self):
        provider = InMemoryOptionsChainProvider()
        assert provider.get_chain("NSE:UNKNOWN") is None

    def test_get_chain_returns_chain_with_spot(self):
        provider = _make_provider(spot=100.0)
        chain = provider.get_chain("NSE:SBIN")
        assert chain is not None
        assert chain.spot_price == 100.0
        assert chain.underlying == "NSE:SBIN"
        assert len(chain.strikes) > 0

    def test_chain_is_deterministic(self):
        provider = _make_provider(spot=100.0, ref_date=date(2024, 1, 15))
        chain1 = provider.get_chain("NSE:SBIN")
        chain2 = provider.get_chain("NSE:SBIN")
        assert chain1.strikes == chain2.strikes
        assert chain1.call_quotes.keys() == chain2.call_quotes.keys()

    def test_set_spot_invalidates_cache(self):
        provider = _make_provider(spot=100.0)
        chain1 = provider.get_chain("NSE:SBIN")
        provider.set_spot("NSE:SBIN", 200.0)
        chain2 = provider.get_chain("NSE:SBIN")
        assert chain1.spot_price != chain2.spot_price
        assert chain2.spot_price == 200.0

    def test_get_spot(self):
        provider = _make_provider(spot=100.0)
        assert provider.get_spot("NSE:SBIN") == 100.0
        assert provider.get_spot("NSE:UNKNOWN") is None

    def test_strike_interval(self):
        assert IC._strike_interval(30) == 1.0
        assert IC._strike_interval(100) == 2.5
        assert IC._strike_interval(500) == 5.0
        assert IC._strike_interval(2000) == 10.0
        assert IC._strike_interval(15000) == 25.0
        assert IC._strike_interval(30000) == 50.0

    def test_generate_strikes_range(self):
        strikes = IC._generate_strikes(100.0, 5.0)
        assert min(strikes) <= 50.0
        assert max(strikes) >= 150.0
        assert all(s % 5 == 0 for s in strikes)

    def test_estimate_premium_positive(self):
        p = IC._estimate_premium(100, 100, True, 30 / 365.0, 0.20)
        assert p > 0

    def test_estimate_premium_increases_with_volatility(self):
        low = IC._estimate_premium(100, 100, True, 30 / 365.0, 0.10)
        high = IC._estimate_premium(100, 100, True, 30 / 365.0, 0.50)
        assert high > low

    def test_nearest_expiry_finds_future_monthly(self):
        provider = InMemoryOptionsChainProvider(reference_date=date(2024, 1, 15))
        exp = provider._nearest_expiry("NSE:SBIN", date(2024, 1, 15))
        assert exp is not None
        # Expiry should be in the future
        assert date.fromisoformat(exp) > date(2024, 1, 15)

    def test_next_monthly_expiry_third_friday(self):
        # 3rd Friday of December 2024 = Dec 20
        exp = IC._next_monthly_expiry(date(2024, 12, 1))
        assert exp == date(2024, 12, 20)
        # 3rd Friday of January 2025 = Jan 17
        exp2 = IC._next_monthly_expiry(date(2025, 1, 1))
        assert exp2 == date(2025, 1, 17)

    def test_nth_weekday(self):
        # 3rd Friday of Jan 2025
        result = IC._nth_weekday(2025, 1, 4, 3)
        assert result == date(2025, 1, 17)

    def test_call_quotes_populated(self):
        provider = _make_provider(spot=100.0)
        chain = provider.get_chain("NSE:SBIN")
        assert len(chain.call_quotes) > 0

    def test_put_quotes_populated(self):
        provider = _make_provider(spot=100.0)
        chain = provider.get_chain("NSE:SBIN")
        assert len(chain.put_quotes) > 0

    def test_call_premium_higher_for_ITM(self):
        provider = _make_provider(spot=100.0)
        chain = provider.get_chain("NSE:SBIN")
        itm_strike = 95  # ITM call
        otm_strike = 105  # OTM call
        assert chain.call_quotes[itm_strike].mid > chain.call_quotes[otm_strike].mid

    def test_put_premium_higher_for_ITM(self):
        provider = _make_provider(spot=100.0)
        chain = provider.get_chain("NSE:SBIN")
        itm_strike = 105  # ITM put
        otm_strike = 95  # OTM put
        assert chain.put_quotes[itm_strike].mid > chain.put_quotes[otm_strike].mid


# --------------------------------------------------------------------------- #
# OptionContractResolver
# --------------------------------------------------------------------------- #

class TestOptionContractResolver:
    def test_resolve_call_atm(self):
        provider = _make_provider(spot=100.0)
        chain = provider.get_chain("NSE:SBIN")
        resolver = OptionContractResolver()
        inst = resolver.resolve_call(chain=chain, moneyness_offset=0.0)
        assert inst is not None
        assert inst.option_type == OptionType.CE
        assert inst.underlying == "NSE:SBIN"

    def test_resolve_put_atm(self):
        provider = _make_provider(spot=100.0)
        chain = provider.get_chain("NSE:SBIN")
        resolver = OptionContractResolver()
        inst = resolver.resolve_put(chain=chain, moneyness_offset=0.0)
        assert inst is not None
        assert inst.option_type == OptionType.PE

    def test_resolve_call_otm(self):
        provider = _make_provider(spot=100.0)
        chain = provider.get_chain("NSE:SBIN")
        resolver = OptionContractResolver()
        inst = resolver.resolve_call(chain=chain, moneyness_offset=0.02)
        assert inst is not None
        assert inst.strike >= chain.spot_price

    def test_resolve_put_otm(self):
        provider = _make_provider(spot=100.0)
        chain = provider.get_chain("NSE:SBIN")
        resolver = OptionContractResolver()
        inst = resolver.resolve_put(chain=chain, moneyness_offset=0.02)
        assert inst is not None
        assert inst.strike <= chain.spot_price

    def test_resolve_vertical_calls(self):
        provider = _make_provider(spot=100.0)
        chain = provider.get_chain("NSE:SBIN")
        resolver = OptionContractResolver()
        near, far = resolver.resolve_vertical(chain=chain, is_call=True, moneyness_offset=0.0)
        assert near is not None
        assert far is not None
        assert far.strike > near.strike
        assert near.option_type == OptionType.CE
        assert far.option_type == OptionType.CE

    def test_resolve_vertical_puts(self):
        provider = _make_provider(spot=100.0)
        chain = provider.get_chain("NSE:SBIN")
        resolver = OptionContractResolver()
        near, far = resolver.resolve_vertical(chain=chain, is_call=False, moneyness_offset=0.0)
        assert near is not None
        assert far is not None
        assert far.strike < near.strike
        assert near.option_type == OptionType.PE
        assert far.option_type == OptionType.PE

    def test_resolve_vertical_with_custom_width(self):
        provider = _make_provider(spot=100.0)
        chain = provider.get_chain("NSE:SBIN")
        resolver = OptionContractResolver()
        near, far = resolver.resolve_vertical(chain=chain, is_call=True, width=25.0)
        assert near is not None
        assert far is not None
        assert (far.strike - near.strike) == pytest.approx(25.0, abs=chain.strike_interval * 2)


# --------------------------------------------------------------------------- #
# OptionsStructureBuilder — signal mapping
# --------------------------------------------------------------------------- #

class TestOptionsStructureBuilderSignalMapping:
    def test_buy_maps_to_long_call_by_default(self):
        cfg = OptionsTradeConfig(strategy_mapping=StrategyMapping(buy_strategy=OptionsStrategy.LONG_CALL))
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.BUY)
        provider = _make_provider(spot=100.0)
        plan = builder.build(decision=decision, chain_provider=provider, config=cfg)
        assert plan is not None
        assert plan.options_strategy == OptionsStrategy.LONG_CALL

    def test_sell_maps_to_long_put_by_default(self):
        cfg = OptionsTradeConfig(strategy_mapping=StrategyMapping(sell_strategy=OptionsStrategy.LONG_PUT))
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.SELL)
        provider = _make_provider(spot=100.0)
        plan = builder.build(decision=decision, chain_provider=provider, config=cfg)
        assert plan is not None
        assert plan.options_strategy == OptionsStrategy.LONG_PUT

    def test_hold_returns_none(self):
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.HOLD)
        provider = _make_provider(spot=100.0)
        plan = builder.build(decision=decision, chain_provider=provider)
        assert plan is None

    def test_exit_without_mapping_builds_exit(self):
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.EXIT)
        provider = _make_provider(spot=100.0)
        plan = builder.build(decision=decision, chain_provider=provider)
        assert plan is not None
        assert plan.signal_action == "exit"

    def test_buy_maps_to_custom_strategy(self):
        cfg = OptionsTradeConfig(strategy_mapping=StrategyMapping(buy_strategy=OptionsStrategy.STRADDLE))
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.BUY)
        provider = _make_provider(spot=100.0)
        plan = builder.build(decision=decision, chain_provider=provider, config=cfg)
        assert plan is not None
        assert plan.options_strategy == OptionsStrategy.STRADDLE

    def test_sell_maps_to_custom_strategy(self):
        cfg = OptionsTradeConfig(strategy_mapping=StrategyMapping(sell_strategy=OptionsStrategy.STRANGLE))
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.SELL)
        provider = _make_provider(spot=100.0)
        plan = builder.build(decision=decision, chain_provider=provider, config=cfg)
        assert plan is not None
        assert plan.options_strategy == OptionsStrategy.STRANGLE

    def test_strategy_none_returns_none(self):
        cfg = OptionsTradeConfig(strategy_mapping=StrategyMapping(buy_strategy=None))
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.BUY)
        provider = _make_provider(spot=100.0)
        plan = builder.build(decision=decision, chain_provider=provider, config=cfg)
        assert plan is None


# --------------------------------------------------------------------------- #
# OptionsStructureBuilder — fail-closed behavior
# --------------------------------------------------------------------------- #

class TestOptionsStructureBuilderFailClosed:
    def test_no_signal_returns_none(self):
        builder = OptionsStructureBuilder()
        decision = TradingDecision(
            decision_id="dec-fail",
            opportunity_symbol="NSE:SBIN",
            opportunity_rank=1,
            market_timestamp="2024-01-15T10:30:00+00:00",
            snapshot_identity="snap",
            signal=None,
            is_valid=True,
            status="valid",
            decision_timestamp="2024-01-15T10:31:00+00:00",
        )
        provider = _make_provider(spot=100.0)
        assert builder.build(decision=decision, chain_provider=provider) is None

    def test_invalid_decision_returns_none(self):
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.BUY)
        decision = decision.model_copy(update={"is_valid": False})
        provider = _make_provider(spot=100.0)
        assert builder.build(decision=decision, chain_provider=provider) is None

    def test_no_chain_returns_none(self):
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.BUY)
        provider = InMemoryOptionsChainProvider()
        assert builder.build(decision=decision, chain_provider=provider) is None

    def test_spot_mismatch_returns_none(self):
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.BUY, reference_price=100.0)
        provider = _make_provider(spot=200.0)
        # 100% spot difference > 15% threshold
        assert builder.build(decision=decision, chain_provider=provider) is None

    def test_zero_reference_price_returns_none(self):
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.BUY)
        # StrategySignal validates reference_price > 0, so we mock to test the builder's guard
        decision = decision.model_copy(update={"signal": MagicMock(reference_price=0.0, action=SignalAction.BUY, symbol="NSE:SBIN")})
        provider = _make_provider(spot=100.0)
        assert builder.build(decision=decision, chain_provider=provider) is None

    def test_negative_reference_price_returns_none(self):
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.BUY)
        decision = decision.model_copy(update={"signal": MagicMock(reference_price=-10.0, action=SignalAction.BUY, symbol="NSE:SBIN")})
        provider = _make_provider(spot=100.0)
        assert builder.build(decision=decision, chain_provider=provider) is None


# --------------------------------------------------------------------------- #
# OptionsStructureBuilder — individual strategy builders
# --------------------------------------------------------------------------- #

class TestOptionsStructureBuilderStrategies:
    def test_long_call(self):
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.BUY)
        provider = _make_provider(spot=100.0)
        cfg = OptionsTradeConfig(strategy_mapping=StrategyMapping(buy_strategy=OptionsStrategy.LONG_CALL))
        plan = builder.build(decision=decision, chain_provider=provider, config=cfg)
        assert plan is not None
        assert len(plan.legs) == 1
        assert plan.legs[0].side == Side.BUY
        assert plan.max_profit is None  # unlimited
        assert plan.max_loss == plan.estimated_cost

    def test_long_put(self):
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.SELL)
        provider = _make_provider(spot=100.0)
        cfg = OptionsTradeConfig(strategy_mapping=StrategyMapping(sell_strategy=OptionsStrategy.LONG_PUT))
        plan = builder.build(decision=decision, chain_provider=provider, config=cfg)
        assert plan is not None
        assert len(plan.legs) == 1
        assert plan.legs[0].side == Side.BUY
        assert plan.max_profit is not None
        assert plan.max_loss == plan.estimated_cost

    def test_bull_call_spread(self):
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.BUY)
        provider = _make_provider(spot=100.0)
        cfg = OptionsTradeConfig(
            strategy_mapping=StrategyMapping(buy_strategy=OptionsStrategy.BULL_CALL_SPREAD),
            spread_width_pct=0.10,
        )
        plan = builder.build(decision=decision, chain_provider=provider, config=cfg)
        assert plan is not None
        assert len(plan.legs) == 2
        assert plan.legs[0].side == Side.BUY  # buy near
        assert plan.legs[1].side == Side.SELL  # sell far
        assert plan.max_loss == plan.estimated_cost
        assert plan.max_profit is not None

    def test_bear_call_spread(self):
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.SELL)
        provider = _make_provider(spot=100.0)
        cfg = OptionsTradeConfig(
            strategy_mapping=StrategyMapping(sell_strategy=OptionsStrategy.BEAR_CALL_SPREAD),
            spread_width_pct=0.10,
        )
        plan = builder.build(decision=decision, chain_provider=provider, config=cfg)
        assert plan is not None
        assert len(plan.legs) == 2
        assert plan.legs[0].side == Side.SELL  # sell near
        assert plan.legs[1].side == Side.BUY  # buy far
        assert plan.max_profit == -plan.estimated_cost  # credit = negative cost

    def test_bull_put_spread(self):
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.BUY)
        provider = _make_provider(spot=100.0)
        cfg = OptionsTradeConfig(
            strategy_mapping=StrategyMapping(buy_strategy=OptionsStrategy.BULL_PUT_SPREAD),
            spread_width_pct=0.10,
        )
        plan = builder.build(decision=decision, chain_provider=provider, config=cfg)
        assert plan is not None
        assert len(plan.legs) == 2
        assert plan.legs[0].side == Side.SELL  # sell near put
        assert plan.legs[1].side == Side.BUY  # buy far put
        assert plan.max_profit == -plan.estimated_cost

    def test_bear_put_spread(self):
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.SELL)
        provider = _make_provider(spot=100.0)
        cfg = OptionsTradeConfig(
            strategy_mapping=StrategyMapping(sell_strategy=OptionsStrategy.BEAR_PUT_SPREAD),
            spread_width_pct=0.10,
        )
        plan = builder.build(decision=decision, chain_provider=provider, config=cfg)
        assert plan is not None
        assert len(plan.legs) == 2
        assert plan.legs[0].side == Side.BUY  # buy near put
        assert plan.legs[1].side == Side.SELL  # sell far put

    def test_straddle(self):
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.BUY)
        provider = _make_provider(spot=100.0)
        cfg = OptionsTradeConfig(strategy_mapping=StrategyMapping(buy_strategy=OptionsStrategy.STRADDLE))
        plan = builder.build(decision=decision, chain_provider=provider, config=cfg)
        assert plan is not None
        assert len(plan.legs) == 2
        assert all(l.side == Side.BUY for l in plan.legs)
        assert plan.legs[0].instrument.option_type == OptionType.CE
        assert plan.legs[1].instrument.option_type == OptionType.PE

    def test_strangle(self):
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.BUY)
        provider = _make_provider(spot=100.0)
        cfg = OptionsTradeConfig(
            strategy_mapping=StrategyMapping(buy_strategy=OptionsStrategy.STRANGLE),
            moneyness_offset=0.02,
        )
        plan = builder.build(decision=decision, chain_provider=provider, config=cfg)
        assert plan is not None
        assert len(plan.legs) == 2
        assert all(l.side == Side.BUY for l in plan.legs)
        assert plan.legs[0].instrument.option_type == OptionType.CE
        assert plan.legs[1].instrument.option_type == OptionType.PE

    def test_exit_with_existing_positions(self):
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.EXIT)
        provider = _make_provider(spot=100.0)
        inst = OptionsInstrument(underlying="NSE:SBIN", expiry="2025-12-19", strike=100, option_type=OptionType.CE)
        existing = [OptionLeg(inst, Side.BUY, 1.0, price=2.0)]
        plan = builder.build(decision=decision, chain_provider=provider, existing_positions=existing)
        assert plan is not None
        assert len(plan.legs) == 1
        assert plan.legs[0].side == Side.SELL  # close long
        assert plan.legs[0].instrument == inst

    def test_exit_with_short_position(self):
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.EXIT)
        provider = _make_provider(spot=100.0)
        inst = OptionsInstrument(underlying="NSE:SBIN", expiry="2025-12-19", strike=100, option_type=OptionType.PE)
        existing = [OptionLeg(inst, Side.SELL, 1.0, price=2.0)]
        plan = builder.build(decision=decision, chain_provider=provider, existing_positions=existing)
        assert plan is not None
        assert len(plan.legs) == 1
        assert plan.legs[0].side == Side.BUY  # close short

    def test_exit_without_existing_positions(self):
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.EXIT)
        provider = _make_provider(spot=100.0)
        plan = builder.build(decision=decision, chain_provider=provider, existing_positions=None)
        assert plan is not None
        assert len(plan.legs) == 0
        assert "no known existing positions" in plan.notes


# --------------------------------------------------------------------------- #
# OptionsTradePlan
# --------------------------------------------------------------------------- #

class TestOptionsTradePlan:
    def _make_plan(self) -> OptionsTradePlan:
        inst = OptionsInstrument(underlying="NSE:SBIN", expiry="2025-12-19", strike=100, option_type=OptionType.CE)
        leg = OptionLeg(inst, Side.BUY, 1.0, price=2.5)
        return OptionsTradePlan(
            underlying_symbol="NSE:SBIN",
            options_strategy=OptionsStrategy.LONG_CALL,
            expiry="2025-12-19",
            legs=[leg],
            estimated_cost=2.5,
            max_loss=2.5,
            max_profit=None,
            break_even_points=[102.5],
            decision_id="dec-001",
            confidence=0.85,
            signal_action="buy",
        )

    def test_to_order_intents_buy_leg(self):
        plan = self._make_plan()
        intents = plan.to_order_intents()
        assert len(intents) == 1
        assert intents[0].symbol == plan.legs[0].instrument.symbol
        assert intents[0].side == Side.BUY
        assert intents[0].quantity == 1.0
        # BUY leg uses LIMIT when price is known
        assert intents[0].order_type == OrderType.LIMIT
        assert intents[0].limit_price == 2.5
        assert intents[0].client_order_id is not None

    def test_to_order_intents_no_price_uses_market(self):
        inst = OptionsInstrument(underlying="NSE:SBIN", expiry="2025-12-19", strike=100, option_type=OptionType.CE)
        leg = OptionLeg(inst, Side.BUY, 1.0, price=None)
        plan = OptionsTradePlan(
            underlying_symbol="NSE:SBIN",
            options_strategy=OptionsStrategy.LONG_CALL,
            expiry="2025-12-19",
            legs=[leg],
            estimated_cost=0.0,
            max_loss=None,
            max_profit=None,
            break_even_points=[],
            decision_id="dec-002",
            confidence=0.5,
            signal_action="buy",
        )
        intents = plan.to_order_intents()
        assert len(intents) == 1
        assert intents[0].order_type == OrderType.MARKET
        assert intents[0].limit_price is None

    def test_to_order_intents_client_order_id_is_deterministic(self):
        plan = self._make_plan()
        intents1 = plan.to_order_intents()
        intents2 = plan.to_order_intents()
        assert intents1[0].client_order_id == intents2[0].client_order_id
        assert len(intents1[0].client_order_id) == 48  # sha256 hexdigest[:48]

    def test_to_order_intents_different_legs_different_ids(self):
        inst1 = OptionsInstrument(underlying="NSE:SBIN", expiry="2025-12-19", strike=100, option_type=OptionType.CE)
        inst2 = OptionsInstrument(underlying="NSE:SBIN", expiry="2025-12-19", strike=110, option_type=OptionType.CE)
        plan = OptionsTradePlan(
            underlying_symbol="NSE:SBIN",
            options_strategy=OptionsStrategy.BULL_CALL_SPREAD,
            expiry="2025-12-19",
            legs=[
                OptionLeg(inst1, Side.BUY, 1.0, price=2.0),
                OptionLeg(inst2, Side.SELL, 1.0, price=1.0),
            ],
            estimated_cost=1.0,
            max_loss=1.0,
            max_profit=None,
            break_even_points=[101.0],
            decision_id="dec-003",
            confidence=0.7,
            signal_action="buy",
        )
        intents = plan.to_order_intents()
        assert len(intents) == 2
        assert intents[0].client_order_id != intents[1].client_order_id

    def test_to_dict(self):
        plan = self._make_plan()
        d = plan.to_dict()
        assert d["underlying_symbol"] == "NSE:SBIN"
        assert d["options_strategy"] == "long_call"
        assert d["expiry"] == "2025-12-19"
        assert len(d["legs"]) == 1
        assert d["legs"][0]["side"] == "BUY"
        assert d["legs"][0]["quantity"] == 1.0
        assert d["estimated_cost"] == 2.5
        assert d["max_loss"] == 2.5
        assert d["max_profit"] is None
        assert d["break_even_points"] == [102.5]
        assert d["decision_id"] == "dec-001"
        assert d["confidence"] == 0.85
        assert d["signal_action"] == "buy"

    def test_is_empty_true_for_no_legs(self):
        plan = OptionsTradePlan(
            underlying_symbol="NSE:SBIN",
            options_strategy=OptionsStrategy.LONG_CALL,
            expiry="2025-12-19",
            legs=[],
            estimated_cost=0.0,
            max_loss=None,
            max_profit=None,
            break_even_points=[],
            decision_id="dec-004",
            confidence=0.0,
            signal_action="exit",
        )
        assert plan.is_empty is True

    def test_is_empty_false_for_legs(self):
        plan = self._make_plan()
        assert plan.is_empty is False

    def test_to_order_intents_returns_order_intent_objects(self):
        plan = self._make_plan()
        intents = plan.to_order_intents()
        assert all(isinstance(i, OrderIntent) for i in intents)


# --------------------------------------------------------------------------- #
# OptionsTradeConfig
# --------------------------------------------------------------------------- #

class TestOptionsTradeConfig:
    def test_default_config(self):
        cfg = OptionsTradeConfig()
        assert cfg.moneyness_offset == 0.02
        assert cfg.min_days_to_expiry == 7
        assert cfg.max_contracts_per_leg == 1.0
        assert cfg.contract_multiplier == 1
        assert cfg.spread_width_pct is None

    def test_default_module_config(self):
        assert DEFAULT_OPTIONS_CONFIG is not None
        assert DEFAULT_OPTIONS_CONFIG.moneyness_offset == 0.02

    def test_custom_config(self):
        cfg = OptionsTradeConfig(
            moneyness_offset=0.05,
            min_days_to_expiry=14,
            max_contracts_per_leg=2.0,
            contract_multiplier=100,
            spread_width_pct=0.10,
        )
        assert cfg.moneyness_offset == 0.05
        assert cfg.min_days_to_expiry == 14
        assert cfg.max_contracts_per_leg == 2.0
        assert cfg.contract_multiplier == 100
        assert cfg.spread_width_pct == 0.10

    def test_custom_strategy_mapping(self):
        mapping = StrategyMapping(buy_strategy=OptionsStrategy.STRADDLE, sell_strategy=OptionsStrategy.LONG_CALL)
        cfg = OptionsTradeConfig(strategy_mapping=mapping)
        assert cfg.strategy_mapping.buy_strategy == OptionsStrategy.STRADDLE
        assert cfg.strategy_mapping.sell_strategy == OptionsStrategy.LONG_CALL


# --------------------------------------------------------------------------- #
# OptionLeg
# --------------------------------------------------------------------------- #

class TestOptionLeg:
    def test_repr(self):
        inst = OptionsInstrument(underlying="NSE:SBIN", expiry="2025-12-19", strike=100, option_type=OptionType.CE)
        leg = OptionLeg(inst, Side.BUY, 1.0, price=2.5)
        r = repr(leg)
        assert "BUY" in r
        assert "1.0" in r

    def test_defaults(self):
        inst = OptionsInstrument(underlying="NSE:SBIN", expiry="2025-12-19", strike=100, option_type=OptionType.CE)
        leg = OptionLeg(inst, Side.BUY, 1.0)
        assert leg.price is None


# --------------------------------------------------------------------------- #
# Integration: Builder with provider chain
# --------------------------------------------------------------------------- #

class TestOptionsStructureBuilderIntegration:
    def test_full_buy_workflow(self):
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.BUY, reference_price=100.0)
        provider = _make_provider(spot=100.0)
        cfg = OptionsTradeConfig()
        plan = builder.build(decision=decision, chain_provider=provider, config=cfg)
        assert plan is not None
        assert plan.underlying_symbol == "NSE:SBIN"
        assert plan.expiry is not None
        assert plan.confidence == 0.85
        assert plan.signal_action == "buy"
        assert len(plan.legs) == 1
        # Order intents should be convertible
        intents = plan.to_order_intents()
        assert len(intents) == 1
        assert intents[0].symbol.startswith("NFO:")

    def test_full_sell_workflow(self):
        builder = OptionsStructureBuilder()
        decision = _make_decision(action=SignalAction.SELL, reference_price=100.0)
        provider = _make_provider(spot=100.0)
        cfg = OptionsTradeConfig()
        plan = builder.build(decision=decision, chain_provider=provider, config=cfg)
        assert plan is not None
        assert len(plan.legs) == 1
        assert plan.signal_action == "sell"


# --------------------------------------------------------------------------- #
# Static safety scan — no live broker references
# --------------------------------------------------------------------------- #

class TestStaticSafetyScan:
    @staticmethod
    def _repo_root() -> str:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_options_contract_no_live_broker_references(self):
        opts_file = os.path.join(self._repo_root(), "src/trading_system/autonomous/options_contract.py")
        with open(opts_file) as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "live_broker" not in alias.name
                    assert "real_broker" not in alias.name
            if isinstance(node, ast.ImportFrom):
                assert "live_broker" not in (node.module or "")
                assert "real_broker" not in (node.module or "")

    def test_options_contract_imports_paper_or_internal_only(self):
        opts_file = os.path.join(self._repo_root(), "src/trading_system/autonomous/options_contract.py")
        with open(opts_file) as f:
            content = f.read()
        assert "from trading_system.execution" not in content.replace(
            "from trading_system.execution.orders import OrderIntent, OrderType, Side",
            ""
        )
        assert "live_broker" not in content.lower()
        assert "real_broker" not in content.lower()

    def test_options_contract_imports_from_paper_internal(self):
        opts_file = os.path.join(self._repo_root(), "src/trading_system/autonomous/options_contract.py")
        with open(opts_file) as f:
            tree = ast.parse(f.read())
        execution_broker_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "execution.broker" in node.module:
                    execution_broker_imports.append(node.module)
        assert execution_broker_imports == [], f"Found disallowed broker imports: {execution_broker_imports}"

    def test_module_does_not_import_live_brokers(self):
        opts_file = os.path.join(self._repo_root(), "src/trading_system/autonomous/options_contract.py")
        with open(opts_file) as f:
            content = f.read()
        assert "from trading_system.execution.live" not in content
        assert "import live_broker" not in content
