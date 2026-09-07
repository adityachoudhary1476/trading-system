"""Phase 8 — Options Contract Selection Layer.

This module provides the architectural boundary that converts underlying-based
strategy signals (Phase 5 ``TradingDecision``) into **options contract trade
plans**. It closes the gap between "the strategy says BUY the underlying" and
"what specific option contract or multi-leg structure expresses that view."

It operates entirely in paper mode — no live broker, no real market data
required. An ``InMemoryOptionsChainProvider`` generates deterministic synthetic
chains from a spot price for testing and paper-trading workflows.

Design
-------
    Phase 5 TradingDecision  →  OptionsStructureBuilder
        ├─ StrategySignal (BUY/SELL/EXIT/HOLD, symbol, reference_price)
        ├─ OptionsChainProvider (chain data)
        └─ OptionsTradeConfig (selection params)
        ↓
    OptionsTradePlan
        ├─ legs: list[OptionLeg]  (instrument + side + quantity + price)
        ├─ risk metrics (max_loss, max_profit, break_evens)
        └─ to_order_intents() → list[OrderIntent] for the PaperBroker
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from math import sqrt, exp
from typing import Any, Optional, Protocol

from trading_system.execution.orders import OrderIntent, OrderType, Side
from trading_system.india.instruments import OptionType
from trading_system.strategy_factory.contract import SignalAction


# --------------------------------------------------------------------------- #
# OptionStyle (exercise style — Indian options are AMERICAN)
# --------------------------------------------------------------------------- #

class OptionStyle(str, Enum):
    AMERICAN = "american"
    EUROPEAN = "european"


# --------------------------------------------------------------------------- #
# OptionsStrategy — the set of multi-leg structures Phase 7 can produce
# --------------------------------------------------------------------------- #

class OptionsStrategy(str, Enum):
    """Options strategies that can express a directional or volatility view."""

    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    BULL_CALL_SPREAD = "bull_call_spread"
    BEAR_CALL_SPREAD = "bear_call_spread"
    BULL_PUT_SPREAD = "bull_put_spread"
    BEAR_PUT_SPREAD = "bear_put_spread"
    STRADDLE = "straddle"
    STRANGLE = "strangle"
    CALENDAR_CALL = "calendar_call"
    CALENDAR_PUT = "calendar_put"
    IRON_CONDOR = "iron_condor"
    CUSTOM = "custom"


# --------------------------------------------------------------------------- #
# Strategy mapping — maps SignalAction to a default OptionsStrategy
# --------------------------------------------------------------------------- #

@dataclass
class StrategyMapping:
    """Default strategy selection per SignalAction."""

    buy_strategy: OptionsStrategy = OptionsStrategy.LONG_CALL
    sell_strategy: OptionsStrategy = OptionsStrategy.LONG_PUT
    hold_strategy: Optional[OptionsStrategy] = None  # None = no plan
    exit_strategy: Optional[OptionsStrategy] = None  # None = close existing


# --------------------------------------------------------------------------- #
# Month abbreviations for NSE/NFO contract tokens
# --------------------------------------------------------------------------- #

_MONTH_ABBR = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)


# --------------------------------------------------------------------------- #
# OptionsChain data model
# --------------------------------------------------------------------------- #

@dataclass
class OptionQuote:
    """Bid/ask/quote for a single strike."""

    strike: float
    bid: float
    ask: float
    last: Optional[float] = None
    volume: int = 0
    open_interest: int = 0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0 if self.bid > 0 and self.ask > 0 else 0.0


@dataclass
class OptionsChain:
    """Full option chain for an underlying at a specific expiry."""

    underlying: str
    expiry: str  # ISO date, e.g. "2025-12-19"
    spot_price: float
    strike_interval: float
    strikes: list[float] = field(default_factory=list)
    call_quotes: dict[float, OptionQuote] = field(default_factory=dict)
    put_quotes: dict[float, OptionQuote] = field(default_factory=dict)

    def get_quote(self, strike: float, option_type: OptionType) -> Optional[OptionQuote]:
        if option_type == OptionType.CE:
            return self.call_quotes.get(strike)
        return self.put_quotes.get(strike)

    @staticmethod
    def nearest_strike(strikes: list[float], target: float) -> Optional[float]:
        if not strikes:
            return None
        return min(strikes, key=lambda s: abs(s - target))

    @staticmethod
    def strikes_above(strikes: list[float], target: float) -> list[float]:
        return sorted(s for s in strikes if s > target)

    @staticmethod
    def strikes_below(strikes: list[float], target: float) -> list[float]:
        return sorted((s for s in strikes if s < target), reverse=True)


# --------------------------------------------------------------------------- #
# OptionsInstrument — a specific option contract
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class OptionsInstrument:
    """A specific option contract identified by underlying + expiry + strike + type.

    ``symbol`` produces the NSE/NFO-style trading token (e.g.
    ``NFO:SBIN25DEC700CE``); ``contract_id`` produces a stable identity that
    is unambiguous across expiries/strikes.
    """

    underlying: str   # e.g. "NSE:SBIN" or bare "SBIN"
    expiry: str       # ISO date, e.g. "2025-12-19"
    strike: float
    option_type: OptionType  # CE or PE
    exchange: str = "NFO"
    style: OptionStyle = OptionStyle.AMERICAN

    @property
    def contract_id(self) -> str:
        underlying = self.underlying.split(":")[-1] if ":" in self.underlying else self.underlying
        return (
            f"{self.exchange}:{underlying}|{self.expiry}|"
            f"{self.strike}|{self.option_type.value}|{self.style.value}"
        )

    @property
    def symbol(self) -> str:
        """NSE/NFO-style trading symbol, e.g. ``NFO:SBIN25DEC700CE``."""
        underlying = self.underlying.split(":")[-1] if ":" in self.underlying else self.underlying
        exp_date = date.fromisoformat(self.expiry)
        yy = exp_date.strftime("%y")
        mon = _MONTH_ABBR[exp_date.month - 1]
        strike_str = str(int(self.strike))
        return f"{self.exchange}:{underlying}{yy}{mon}{strike_str}{self.option_type.value}"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, OptionsInstrument):
            return NotImplemented
        return self.contract_id == other.contract_id

    def __hash__(self) -> int:
        return hash(self.contract_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "expiry": self.expiry,
            "strike": self.strike,
            "option_type": self.option_type.value,
            "exchange": self.exchange,
            "style": self.style.value,
            "contract_id": self.contract_id,
            "symbol": self.symbol,
        }


# --------------------------------------------------------------------------- #
# OptionLeg — one leg of a multi-leg trade plan
# --------------------------------------------------------------------------- #

@dataclass
class OptionLeg:
    """One leg of a multi-leg options trade plan."""

    instrument: OptionsInstrument
    side: Side  # BUY or SELL
    quantity: float  # number of contracts
    price: Optional[float] = None  # reference premium (ask for BUY, bid for SELL)

    def __repr__(self) -> str:
        return (
            f"OptionLeg({self.side.value} {self.quantity}x "
            f"{self.instrument.symbol})"
        )


# --------------------------------------------------------------------------- #
# OptionsTradePlan — complete plan with legs + risk metrics
# --------------------------------------------------------------------------- #

@dataclass
class OptionsTradePlan:
    """Complete, paper-only options trade plan.

    Derived from a ``TradingDecision`` (Phase 5) + an options chain. Contains
    the leg list, risk metrics, and a deterministic ``to_order_intents()``
    that produces ``OrderIntent`` objects consumable by the ``PaperBroker``.
    """

    underlying_symbol: str
    options_strategy: OptionsStrategy
    expiry: str
    legs: list[OptionLeg]
    estimated_cost: float        # total premium (positive = net debit, negative = net credit)
    max_loss: Optional[float]    # None = unlimited
    max_profit: Optional[float]  # None = unlimited
    break_even_points: list[float]
    decision_id: str             # links back to the TradingDecision
    confidence: float
    signal_action: str           # "buy" / "sell" / "exit" / "hold"
    notes: str = ""

    def to_order_intents(self) -> list[OrderIntent]:
        """Convert legs to ``OrderIntent`` objects for the ``PaperBroker``.

        BUY legs use LIMIT orders at the ask price (if known) or MARKET;
        SELL legs use LIMIT at the bid price (if known) or MARKET.
        ``client_order_id`` is a deterministic identity derived from
        ``decision_id + leg index`` so retries are idempotent.
        """
        intents: list[OrderIntent] = []
        for i, leg in enumerate(self.legs):
            has_price = leg.price is not None and leg.price > 0
            order_type = OrderType.LIMIT if has_price else OrderType.MARKET
            cid = hashlib.sha256(
                f"{self.decision_id}:{i}:{leg.instrument.contract_id}:{leg.side.value}".encode("utf-8")
            ).hexdigest()[:48]
            intents.append(OrderIntent(
                symbol=leg.instrument.symbol,
                side=leg.side,
                quantity=leg.quantity,
                order_type=order_type,
                limit_price=leg.price if has_price else None,
                client_order_id=cid,
                current_price=leg.price if has_price else None,
            ))
        return intents

    def to_dict(self) -> dict[str, Any]:
        return {
            "underlying_symbol": self.underlying_symbol,
            "options_strategy": self.options_strategy.value,
            "expiry": self.expiry,
            "legs": [
                {
                    "symbol": leg.instrument.symbol,
                    "contract_id": leg.instrument.contract_id,
                    "side": leg.side.value,
                    "quantity": leg.quantity,
                    "price": leg.price,
                    "strike": leg.instrument.strike,
                    "option_type": leg.instrument.option_type.value,
                    "underlying": leg.instrument.underlying,
                }
                for leg in self.legs
            ],
            "estimated_cost": round(self.estimated_cost, 4),
            "max_loss": round(self.max_loss, 4) if self.max_loss is not None else None,
            "max_profit": round(self.max_profit, 4) if self.max_profit is not None else None,
            "break_even_points": [round(b, 4) for b in self.break_even_points],
            "decision_id": self.decision_id,
            "confidence": self.confidence,
            "signal_action": self.signal_action,
            "notes": self.notes,
        }

    @property
    def is_empty(self) -> bool:
        return len(self.legs) == 0


# --------------------------------------------------------------------------- #
# OptionsTradeConfig
# --------------------------------------------------------------------------- #

@dataclass
class OptionsTradeConfig:
    """Configuration for options contract selection (Phase 8).

    All defaults are conservative and paper-only. The config is pure data —
    no I/O, no market data, no broker interaction.
    """

    moneyness_offset: float = 0.02        # 2% OTM by default (strike/spot = 1.02)
    min_days_to_expiry: int = 7           # skip expiries too close to expiry
    max_contracts_per_leg: float = 1.0    # max contracts per leg
    contract_multiplier: int = 1          # shares per contract (1 for India, 100 for US)
    strategy_mapping: StrategyMapping = field(default_factory=StrategyMapping)
    spread_width_pct: Optional[float] = None  # e.g. 0.05 = 5% of spot as spread width


# --------------------------------------------------------------------------- #
# OptionsChainProvider interface + InMemory implementation
# --------------------------------------------------------------------------- #

class OptionsChainProvider(Protocol):
    """Interface for providing option chain data."""

    def get_chain(
        self,
        underlying: str,
        expiry: Optional[str] = None,
    ) -> Optional[OptionsChain]:
        """Return the option chain for ``underlying``, optionally filtered to ``expiry``."""
        ...


class InMemoryOptionsChainProvider:
    """Generates deterministic synthetic option chains from a spot price.

    Does NOT require live market data. Uses a simplified volatility-based
    pricing model. Spot prices are set explicitly via ``set_spot`` (or
    inferred from the underlying prefix for known symbols).

    The chain generation is deterministic: same spot + same reference date
    always produces the same chain.
    """

    def __init__(
        self,
        *,
        default_volatility: float = 0.20,
        min_premium: float = 0.01,
        reference_date: Optional[date] = None,
    ) -> None:
        self._volatility = float(default_volatility)
        self._min_premium = float(min_premium)
        self._reference_date = reference_date or date.today()
        self._cache: dict[str, OptionsChain] = {}
        self._spot_cache: dict[str, float] = {}

    # -- spot management --------------------------------------------------

    def set_spot(self, underlying: str, spot: float) -> None:
        """Set the spot price for an underlying. Clears the chain cache for it."""
        self._spot_cache[underlying] = float(spot)
        # Invalidate cached chains for this underlying
        keys_to_remove = [k for k in self._cache if k.startswith(f"{underlying}:")]
        for k in keys_to_remove:
            del self._cache[k]

    def get_spot(self, underlying: str) -> Optional[float]:
        return self._spot_cache.get(underlying)

    # -- chain retrieval ---------------------------------------------------

    def get_chain(
        self,
        underlying: str,
        expiry: Optional[str] = None,
    ) -> Optional[OptionsChain]:
        spot = self._spot_cache.get(underlying)
        if spot is None or spot <= 0:
            return None

        cache_key = f"{underlying}:{expiry or 'nearest'}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        if expiry is None:
            expiry = self._nearest_expiry(underlying, self._reference_date)
        if expiry is None:
            return None

        days = self._days_to_expiry(expiry)
        T = max(days, 1) / 365.0

        interval = self._strike_interval(spot)
        strikes = self._generate_strikes(spot, interval)

        call_quotes: dict[float, OptionQuote] = {}
        put_quotes: dict[float, OptionQuote] = {}
        for strike in strikes:
            call_p = self._estimate_premium(spot, strike, True, T, self._volatility)
            put_p = self._estimate_premium(spot, strike, False, T, self._volatility)
            call_quotes[strike] = OptionQuote(strike, call_p * 0.98, call_p * 1.02, call_p)
            put_quotes[strike] = OptionQuote(strike, put_p * 0.98, put_p * 1.02, put_p)

        chain = OptionsChain(
            underlying=underlying,
            expiry=expiry,
            spot_price=spot,
            strike_interval=interval,
            strikes=strikes,
            call_quotes=call_quotes,
            put_quotes=put_quotes,
        )
        self._cache[cache_key] = chain
        return chain

    # -- expiry helpers ----------------------------------------------------

    def _available_expiries(self) -> list[str]:
        """Generate the next 12 standard monthly expiry dates (3rd Friday)."""
        expiries: list[str] = []
        d = self._reference_date
        for _ in range(12):
            d = self._next_monthly_expiry(d)
            expiries.append(d.isoformat())
        return sorted(expiries)

    def _nearest_expiry(self, underlying: str, ref_date: date) -> Optional[str]:
        expiries = self._available_expiries()
        future = [e for e in expiries if date.fromisoformat(e) > ref_date]
        if not future:
            return expiries[-1]
        return future[0]

    @staticmethod
    def _next_monthly_expiry(d: date) -> date:
        """Find the 3rd Friday of the month containing or after ``d``."""
        year, month = d.year, d.month
        for _ in range(3):
            exp = InMemoryOptionsChainProvider._nth_weekday(year, month, 4, 3)  # 3rd Friday (weekday=4)
            if exp >= d:
                return exp
            month += 1
            if month > 12:
                year += 1
                month = 1
        return exp

    @staticmethod
    def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
        """Return the nth ``weekday`` (0=Mon) of ``month`` in ``year``."""
        first = date(year, month, 1)
        days_ahead = (weekday - first.weekday()) % 7
        target = first + timedelta(days=days_ahead)
        result = target + timedelta(weeks=n - 1)
        if result.month != month:
            result -= timedelta(weeks=1)
        return result

    def _days_to_expiry(self, expiry: str) -> int:
        exp_date = date.fromisoformat(expiry)
        return (exp_date - self._reference_date).days

    # -- strike generation -------------------------------------------------

    @staticmethod
    def _strike_interval(spot: float) -> float:
        if spot < 50:
            return 1.0
        elif spot < 200:
            return 2.5
        elif spot < 1000:
            return 5.0
        elif spot < 5000:
            return 10.0
        elif spot < 20000:
            return 25.0
        else:
            return 50.0

    @staticmethod
    def _generate_strikes(spot: float, interval: float) -> list[float]:
        """Generate strikes from ``spot * 0.5`` to ``spot * 1.5``."""
        min_strike = round(spot * 0.5 / interval) * interval
        max_strike = round(spot * 1.5 / interval) * interval
        n = int(round((max_strike - min_strike) / interval))
        return [round(min_strike + i * interval, 2) for i in range(n + 1)]

    # -- premium estimation (simplified, deterministic) -------------------

    @staticmethod
    def _estimate_premium(
        spot: float,
        strike: float,
        is_call: bool,
        T: float,
        vol: float,
    ) -> float:
        """Simplified BSM-inspired option premium estimate.

        Premium = intrinsic_value + time_value, where time_value decays
        exponentially with distance from ATM. Fully deterministic.
        """
        if spot <= 0:
            return 0.01

        if is_call:
            intrinsic = max(0.0, spot - strike)
        else:
            intrinsic = max(0.0, strike - spot)

        # ATM premium: vol * spot * sqrt(T) * (1/sqrt(2*pi))
        # 1/sqrt(2*pi) ≈ 0.3989
        atm_value = vol * spot * (T ** 0.5) * 0.3989

        distance = abs(spot - strike) / spot
        time_value = atm_value * exp(-2.0 * distance)

        premium = intrinsic + time_value
        return max(premium, 0.01)


# --------------------------------------------------------------------------- #
# Option Contract Resolver
# --------------------------------------------------------------------------- #

@dataclass
class ResolveResult:
    """Result of resolving instruments for a strategy."""

    instruments: list[OptionsInstrument]
    strikes: list[float]


class OptionContractResolver:
    """Selects option contracts based on moneyness and market conditions.

    All methods are deterministic pure functions of the chain + parameters.
    No I/O, no wall-clock reads.
    """

    def resolve_call(
        self,
        *,
        chain: OptionsChain,
        moneyness_offset: float = 0.0,
    ) -> Optional[OptionsInstrument]:
        """Resolve a call option at ATM (``offset=0``) or OTM (``offset>0``)."""
        target = chain.spot_price * (1.0 + moneyness_offset)
        strike = OptionsChain.nearest_strike(chain.strikes, target)
        if strike is None:
            return None
        return OptionsInstrument(
            underlying=chain.underlying,
            expiry=chain.expiry,
            strike=strike,
            option_type=OptionType.CE,
            exchange="NFO",
        )

    def resolve_put(
        self,
        *,
        chain: OptionsChain,
        moneyness_offset: float = 0.0,
    ) -> Optional[OptionsInstrument]:
        """Resolve a put option at ATM (``offset=0``) or OTM (``offset>0``)."""
        target = chain.spot_price * (1.0 - moneyness_offset)
        strike = OptionsChain.nearest_strike(chain.strikes, target)
        if strike is None:
            return None
        return OptionsInstrument(
            underlying=chain.underlying,
            expiry=chain.expiry,
            strike=strike,
            option_type=OptionType.PE,
            exchange="NFO",
        )

    def resolve_vertical(
        self,
        *,
        chain: OptionsChain,
        is_call: bool,
        moneyness_offset: float = 0.0,
        width: Optional[float] = None,
    ) -> tuple[Optional[OptionsInstrument], Optional[OptionsInstrument]]:
        """Resolve a vertical spread pair (near + far strike).

        ``width`` is the absolute strike width (e.g. 50.0). If None, uses
        ``chain.strike_interval`` as the width.
        """
        w = width if width is not None else chain.strike_interval
        if is_call:
            near = self.resolve_call(chain=chain, moneyness_offset=moneyness_offset)
            if near is None:
                return None, None
            far_target = near.strike + w
            far_strike = OptionsChain.nearest_strike(chain.strikes, far_target)
            if far_strike is None or far_strike <= near.strike:
                return near, None
        else:
            near = self.resolve_put(chain=chain, moneyness_offset=moneyness_offset)
            if near is None:
                return None, None
            far_target = near.strike - w
            far_strike = OptionsChain.nearest_strike(chain.strikes, far_target)
            if far_strike is None or far_strike >= near.strike:
                return near, None

        far = OptionsInstrument(
            underlying=chain.underlying,
            expiry=chain.expiry,
            strike=far_strike,
            option_type=OptionType.CE if is_call else OptionType.PE,
            exchange="NFO",
        )
        return near, far


# --------------------------------------------------------------------------- #
# Options Structure Builder — main Phase 8 entry point
# --------------------------------------------------------------------------- #

class OptionsStructureBuilder:
    """Converts a ``TradingDecision`` into an ``OptionsTradePlan``.

    Fail-closed design:
      - Returns ``None`` for HOLD signals.
      - Returns ``None`` when no chain data is available for the underlying.
      - Returns ``None`` when required strikes/quotes are missing.
      - Never raises on bad inputs — callers get ``None`` + a note.
    """

    def __init__(self) -> None:
        self._resolver = OptionContractResolver()

    def build(
        self,
        *,
        decision: "TradingDecision",
        chain_provider: OptionsChainProvider,
        config: Optional[OptionsTradeConfig] = None,
        existing_positions: Optional[list[OptionLeg]] = None,
    ) -> Optional[OptionsTradePlan]:
        """Build an ``OptionsTradePlan`` from a Phase 5 ``TradingDecision``.

        Parameters
        ----------
        decision
            The Phase 5 structured decision containing the ``StrategySignal``.
        chain_provider
            A ``OptionsChainProvider`` supplying option chain data.
        config
            Selection configuration (moneyness, expiry, strategy mapping).
            Defaults to ``OptionsTradeConfig()``.
        existing_positions
            Existing option legs for EXIT scenarios (Phase 8+). If ``None``
            and the signal is EXIT, an empty closing plan is returned.
        """
        cfg = config or OptionsTradeConfig()

        signal = decision.signal
        if signal is None or not decision.is_valid:
            return None

        action = signal.action
        if action == SignalAction.HOLD:
            return None

        underlying = decision.opportunity_symbol or signal.symbol
        spot = float(signal.reference_price)
        if spot <= 0:
            return None

        chain = chain_provider.get_chain(underlying, expiry=None)
        if chain is None:
            return None

        # Sanity: reject if the chain spot is wildly different from the signal price
        if abs(chain.spot_price - spot) / spot > 0.15:
            return None

        mapping = cfg.strategy_mapping
        if action == SignalAction.BUY:
            strategy = mapping.buy_strategy
        elif action == SignalAction.SELL:
            strategy = mapping.sell_strategy
        elif action == SignalAction.EXIT:
            if mapping.exit_strategy:
                strategy = mapping.exit_strategy
            else:
                return self._build_exit(decision, chain, cfg, existing_positions)
        else:
            return None

        if strategy is None:
            return None

        builder_map = {
            OptionsStrategy.LONG_CALL: self._build_long_call,
            OptionsStrategy.LONG_PUT: self._build_long_put,
            OptionsStrategy.BULL_CALL_SPREAD: self._build_bull_call_spread,
            OptionsStrategy.BEAR_CALL_SPREAD: self._build_bear_call_spread,
            OptionsStrategy.BEAR_PUT_SPREAD: self._build_bear_put_spread,
            OptionsStrategy.BULL_PUT_SPREAD: self._build_bull_put_spread,
            OptionsStrategy.STRADDLE: self._build_straddle,
            OptionsStrategy.STRANGLE: self._build_strangle,
        }

        builder = builder_map.get(strategy)
        if builder is None:
            return None

        plan = builder(decision, chain, cfg, spot)
        return plan

    # ---------------------------------------------------------------- #
    # Single-leg strategies
    # ---------------------------------------------------------------- #

    def _build_long_call(
        self,
        decision: "TradingDecision",
        chain: OptionsChain,
        cfg: OptionsTradeConfig,
        spot: float,
    ) -> Optional[OptionsTradePlan]:
        inst = self._resolver.resolve_call(
            chain=chain, moneyness_offset=cfg.moneyness_offset
        )
        if inst is None:
            return None
        quote = chain.get_quote(inst.strike, OptionType.CE)
        if quote is None:
            return None

        qty = cfg.max_contracts_per_leg
        premium = quote.ask
        cost = premium * qty

        return OptionsTradePlan(
            underlying_symbol=decision.opportunity_symbol,
            options_strategy=OptionsStrategy.LONG_CALL,
            expiry=chain.expiry,
            legs=[OptionLeg(inst, Side.BUY, qty, price=premium)],
            estimated_cost=cost,
            max_loss=cost,
            max_profit=None,
            break_even_points=[inst.strike + premium],
            decision_id=decision.decision_id,
            confidence=self._confidence(decision),
            signal_action=decision.action or "buy",
            notes=f"LONG_CALL {inst.strike} CE @ {premium:.2f} x{qty}",
        )

    def _build_long_put(
        self,
        decision: "TradingDecision",
        chain: OptionsChain,
        cfg: OptionsTradeConfig,
        spot: float,
    ) -> Optional[OptionsTradePlan]:
        inst = self._resolver.resolve_put(
            chain=chain, moneyness_offset=cfg.moneyness_offset
        )
        if inst is None:
            return None
        quote = chain.get_quote(inst.strike, OptionType.PE)
        if quote is None:
            return None

        qty = cfg.max_contracts_per_leg
        premium = quote.ask
        cost = premium * qty

        return OptionsTradePlan(
            underlying_symbol=decision.opportunity_symbol,
            options_strategy=OptionsStrategy.LONG_PUT,
            expiry=chain.expiry,
            legs=[OptionLeg(inst, Side.BUY, qty, price=premium)],
            estimated_cost=cost,
            max_loss=cost,
            max_profit=(inst.strike - premium) * qty if inst.strike > 0 else None,
            break_even_points=[inst.strike - premium],
            decision_id=decision.decision_id,
            confidence=self._confidence(decision),
            signal_action=decision.action or "sell",
            notes=f"LONG_PUT {inst.strike} PE @ {premium:.2f} x{qty}",
        )

    # ---------------------------------------------------------------- #
    # Vertical spread strategies (calls)
    # ---------------------------------------------------------------- #

    def _build_bull_call_spread(
        self,
        decision: "TradingDecision",
        chain: OptionsChain,
        cfg: OptionsTradeConfig,
        spot: float,
    ) -> Optional[OptionsTradePlan]:
        """Buy call (near) + Sell call (far) — bullish debit spread."""
        near = self._resolver.resolve_call(chain=chain, moneyness_offset=cfg.moneyness_offset)
        if near is None:
            return None
        near_q = chain.get_quote(near.strike, OptionType.CE)
        if near_q is None:
            return None

        w = cfg.spread_width_pct
        width = (w * spot) if w is not None else chain.strike_interval
        far_target = near.strike + width
        far_strike = OptionsChain.nearest_strike(chain.strikes, far_target)
        if far_strike is None or far_strike <= near.strike:
            return None
        far = OptionsInstrument(
            underlying=chain.underlying, expiry=chain.expiry,
            strike=far_strike, option_type=OptionType.CE, exchange="NFO",
        )
        far_q = chain.get_quote(far.strike, OptionType.CE)
        if far_q is None:
            return None

        qty = cfg.max_contracts_per_leg
        debit = (near_q.ask - far_q.bid) * qty
        if debit <= 0:
            return None
        max_profit = (far.strike - near.strike) * qty - debit

        return OptionsTradePlan(
            underlying_symbol=decision.opportunity_symbol,
            options_strategy=OptionsStrategy.BULL_CALL_SPREAD,
            expiry=chain.expiry,
            legs=[
                OptionLeg(near, Side.BUY, qty, price=near_q.ask),
                OptionLeg(far, Side.SELL, qty, price=far_q.bid),
            ],
            estimated_cost=debit,
            max_loss=debit,
            max_profit=max_profit,
            break_even_points=[near.strike + debit / qty if qty > 0 else near.strike],
            decision_id=decision.decision_id,
            confidence=self._confidence(decision),
            signal_action=decision.action or "buy",
            notes=(
                f"BULL_CALL_SPREAD buy {near.strike} CE @ {near_q.ask:.2f}, "
                f"sell {far.strike} CE @ {far_q.bid:.2f} x{qty}"
            ),
        )

    def _build_bear_call_spread(
        self,
        decision: "TradingDecision",
        chain: OptionsChain,
        cfg: OptionsTradeConfig,
        spot: float,
    ) -> Optional[OptionsTradePlan]:
        """Sell call (near) + Buy call (far) — bearish credit spread."""
        near = self._resolver.resolve_call(chain=chain, moneyness_offset=cfg.moneyness_offset)
        if near is None:
            return None
        near_q = chain.get_quote(near.strike, OptionType.CE)
        if near_q is None:
            return None

        w = cfg.spread_width_pct
        width = (w * spot) if w is not None else chain.strike_interval
        far_target = near.strike + width
        far_strike = OptionsChain.nearest_strike(chain.strikes, far_target)
        if far_strike is None or far_strike <= near.strike:
            return None
        far = OptionsInstrument(
            underlying=chain.underlying, expiry=chain.expiry,
            strike=far_strike, option_type=OptionType.CE, exchange="NFO",
        )
        far_q = chain.get_quote(far.strike, OptionType.CE)
        if far_q is None:
            return None

        qty = cfg.max_contracts_per_leg
        credit = (near_q.bid - far_q.ask) * qty
        if credit <= 0:
            return None
        max_loss = (far.strike - near.strike) * qty - credit

        return OptionsTradePlan(
            underlying_symbol=decision.opportunity_symbol,
            options_strategy=OptionsStrategy.BEAR_CALL_SPREAD,
            expiry=chain.expiry,
            legs=[
                OptionLeg(near, Side.SELL, qty, price=near_q.bid),
                OptionLeg(far, Side.BUY, qty, price=far_q.ask),
            ],
            estimated_cost=-credit,
            max_loss=max_loss,
            max_profit=credit,
            break_even_points=[near.strike + credit / qty if qty > 0 else near.strike],
            decision_id=decision.decision_id,
            confidence=self._confidence(decision),
            signal_action=decision.action or "sell",
            notes=(
                f"BEAR_CALL_SPREAD sell {near.strike} CE @ {near_q.bid:.2f}, "
                f"buy {far.strike} CE @ {far_q.ask:.2f} x{qty}"
            ),
        )

    # ---------------------------------------------------------------- #
    # Vertical spread strategies (puts)
    # ---------------------------------------------------------------- #

    def _build_bull_put_spread(
        self,
        decision: "TradingDecision",
        chain: OptionsChain,
        cfg: OptionsTradeConfig,
        spot: float,
    ) -> Optional[OptionsTradePlan]:
        """Sell put (near) + Buy put (far) — bullish credit spread."""
        near = self._resolver.resolve_put(chain=chain, moneyness_offset=cfg.moneyness_offset)
        if near is None:
            return None
        near_q = chain.get_quote(near.strike, OptionType.PE)
        if near_q is None:
            return None

        w = cfg.spread_width_pct
        width = (w * spot) if w is not None else chain.strike_interval
        far_target = near.strike - width
        far_strike = OptionsChain.nearest_strike(chain.strikes, far_target)
        if far_strike is None or far_strike >= near.strike:
            return None
        far = OptionsInstrument(
            underlying=chain.underlying, expiry=chain.expiry,
            strike=far_strike, option_type=OptionType.PE, exchange="NFO",
        )
        far_q = chain.get_quote(far.strike, OptionType.PE)
        if far_q is None:
            return None

        qty = cfg.max_contracts_per_leg
        credit = (near_q.bid - far_q.ask) * qty
        if credit <= 0:
            return None
        max_loss = (near.strike - far.strike) * qty - credit

        return OptionsTradePlan(
            underlying_symbol=decision.opportunity_symbol,
            options_strategy=OptionsStrategy.BULL_PUT_SPREAD,
            expiry=chain.expiry,
            legs=[
                OptionLeg(near, Side.SELL, qty, price=near_q.bid),
                OptionLeg(far, Side.BUY, qty, price=far_q.ask),
            ],
            estimated_cost=-credit,
            max_loss=max_loss,
            max_profit=credit,
            break_even_points=[far.strike + credit / qty if qty > 0 else far.strike],
            decision_id=decision.decision_id,
            confidence=self._confidence(decision),
            signal_action=decision.action or "buy",
            notes=(
                f"BULL_PUT_SPREAD sell {near.strike} PE @ {near_q.bid:.2f}, "
                f"buy {far.strike} PE @ {far_q.ask:.2f} x{qty}"
            ),
        )

    def _build_bear_put_spread(
        self,
        decision: "TradingDecision",
        chain: OptionsChain,
        cfg: OptionsTradeConfig,
        spot: float,
    ) -> Optional[OptionsTradePlan]:
        """Buy put (near) + Sell put (far) — bearish debit spread."""
        near = self._resolver.resolve_put(chain=chain, moneyness_offset=cfg.moneyness_offset)
        if near is None:
            return None
        near_q = chain.get_quote(near.strike, OptionType.PE)
        if near_q is None:
            return None

        w = cfg.spread_width_pct
        width = (w * spot) if w is not None else chain.strike_interval
        far_target = near.strike - width
        far_strike = OptionsChain.nearest_strike(chain.strikes, far_target)
        if far_strike is None or far_strike >= near.strike:
            return None
        far = OptionsInstrument(
            underlying=chain.underlying, expiry=chain.expiry,
            strike=far_strike, option_type=OptionType.PE, exchange="NFO",
        )
        far_q = chain.get_quote(far.strike, OptionType.PE)
        if far_q is None:
            return None

        qty = cfg.max_contracts_per_leg
        debit = (near_q.ask - far_q.bid) * qty
        if debit <= 0:
            return None
        max_profit = (near.strike - far.strike) * qty - debit

        return OptionsTradePlan(
            underlying_symbol=decision.opportunity_symbol,
            options_strategy=OptionsStrategy.BEAR_PUT_SPREAD,
            expiry=chain.expiry,
            legs=[
                OptionLeg(near, Side.BUY, qty, price=near_q.ask),
                OptionLeg(far, Side.SELL, qty, price=far_q.bid),
            ],
            estimated_cost=debit,
            max_loss=debit,
            max_profit=max_profit,
            break_even_points=[near.strike - debit / qty if qty > 0 else near.strike],
            decision_id=decision.decision_id,
            confidence=self._confidence(decision),
            signal_action=decision.action or "sell",
            notes=(
                f"BEAR_PUT_SPREAD buy {near.strike} PE @ {near_q.ask:.2f}, "
                f"sell {far.strike} PE @ {near_q.bid:.2f} x{qty}"
            ),
        )

    # ---------------------------------------------------------------- #
    # Volatility strategies
    # ---------------------------------------------------------------- #

    def _build_straddle(
        self,
        decision: "TradingDecision",
        chain: OptionsChain,
        cfg: OptionsTradeConfig,
        spot: float,
    ) -> Optional[OptionsTradePlan]:
        """Buy ATM call + ATM put — volatility play (long straddle)."""
        call_inst = self._resolver.resolve_call(chain=chain, moneyness_offset=0.0)
        put_inst = self._resolver.resolve_put(chain=chain, moneyness_offset=0.0)
        if call_inst is None or put_inst is None:
            return None
        c_q = chain.get_quote(call_inst.strike, OptionType.CE)
        p_q = chain.get_quote(put_inst.strike, OptionType.PE)
        if c_q is None or p_q is None:
            return None

        qty = cfg.max_contracts_per_leg
        total_cost = (c_q.ask + p_q.ask) * qty
        be = total_cost / 2.0

        return OptionsTradePlan(
            underlying_symbol=decision.opportunity_symbol,
            options_strategy=OptionsStrategy.STRADDLE,
            expiry=chain.expiry,
            legs=[
                OptionLeg(call_inst, Side.BUY, qty, price=c_q.ask),
                OptionLeg(put_inst, Side.BUY, qty, price=p_q.ask),
            ],
            estimated_cost=total_cost,
            max_loss=total_cost,
            max_profit=None,
            break_even_points=[spot + be, spot - be],
            decision_id=decision.decision_id,
            confidence=self._confidence(decision),
            signal_action=decision.action or "buy",
            notes=f"STRADDLE ATM call {call_inst.strike} CE @ {c_q.ask:.2f}, put {put_inst.strike} PE @ {p_q.ask:.2f} x{qty}",
        )

    def _build_strangle(
        self,
        decision: "TradingDecision",
        chain: OptionsChain,
        cfg: OptionsTradeConfig,
        spot: float,
    ) -> Optional[OptionsTradePlan]:
        """Buy OTM call + OTM put — volatility play (long strangle)."""
        offset = cfg.moneyness_offset
        call_inst = self._resolver.resolve_call(chain=chain, moneyness_offset=offset)
        put_inst = self._resolver.resolve_put(chain=chain, moneyness_offset=offset)
        if call_inst is None or put_inst is None:
            return None
        c_q = chain.get_quote(call_inst.strike, OptionType.CE)
        p_q = chain.get_quote(put_inst.strike, OptionType.PE)
        if c_q is None or p_q is None:
            return None

        qty = cfg.max_contracts_per_leg
        total_cost = (c_q.ask + p_q.ask) * qty
        be_up = call_inst.strike + total_cost / 2.0
        be_down = put_inst.strike - total_cost / 2.0

        return OptionsTradePlan(
            underlying_symbol=decision.opportunity_symbol,
            options_strategy=OptionsStrategy.STRANGLE,
            expiry=chain.expiry,
            legs=[
                OptionLeg(call_inst, Side.BUY, qty, price=c_q.ask),
                OptionLeg(put_inst, Side.BUY, qty, price=p_q.ask),
            ],
            estimated_cost=total_cost,
            max_loss=total_cost,
            max_profit=None,
            break_even_points=[be_up, be_down],
            decision_id=decision.decision_id,
            confidence=self._confidence(decision),
            signal_action=decision.action or "buy",
            notes=f"STRANGLE OTM call {call_inst.strike} CE @ {c_q.ask:.2f}, put {put_inst.strike} PE @ {p_q.ask:.2f} x{qty}",
        )

    # ---------------------------------------------------------------- #
    # Exit (closing) strategy
    # ---------------------------------------------------------------- #

    def _build_exit(
        self,
        decision: "TradingDecision",
        chain: OptionsChain,
        cfg: OptionsTradeConfig,
        existing_positions: Optional[list[OptionLeg]] = None,
    ) -> Optional[OptionsTradePlan]:
        """Generate closing legs for an EXIT signal.

        Each existing position is closed with an opposite-side leg.
        If no existing positions are known, returns an empty plan with
        a note directing the operator to close manually.
        """
        if not existing_positions:
            return OptionsTradePlan(
                underlying_symbol=decision.opportunity_symbol,
                options_strategy=OptionsStrategy.CUSTOM,
                expiry=chain.expiry,
                legs=[],
                estimated_cost=0.0,
                max_loss=None,
                max_profit=None,
                break_even_points=[],
                decision_id=decision.decision_id,
                confidence=self._confidence(decision),
                signal_action="exit",
                notes="EXIT signal — no known existing positions to close",
            )

        closing_legs: list[OptionLeg] = []
        for pos in existing_positions:
            closing_legs.append(OptionLeg(
                instrument=pos.instrument,
                side=Side.BUY if pos.side == Side.SELL else Side.SELL,
                quantity=pos.quantity,
                price=None,
            ))

        return OptionsTradePlan(
            underlying_symbol=decision.opportunity_symbol,
            options_strategy=OptionsStrategy.CUSTOM,
            expiry=chain.expiry,
            legs=closing_legs,
            estimated_cost=0.0,
            max_loss=None,
            max_profit=None,
            break_even_points=[],
            decision_id=decision.decision_id,
            confidence=self._confidence(decision),
            signal_action="exit",
            notes=f"EXIT — closing {len(closing_legs)} existing leg(s)",
        )

    # ---------------------------------------------------------------- #
    # Helpers
    # ---------------------------------------------------------------- #

    @staticmethod
    def _confidence(decision: "TradingDecision") -> float:
        sig = decision.signal
        if sig is not None and hasattr(sig, "confidence"):
            return float(sig.confidence)
        return 0.0


# --------------------------------------------------------------------------- #
# Default config + module exports
# --------------------------------------------------------------------------- #

DEFAULT_OPTIONS_CONFIG = OptionsTradeConfig()

__all__ = [
    "OptionStyle",
    "OptionsStrategy",
    "OptionQuote",
    "OptionsChain",
    "OptionsInstrument",
    "OptionLeg",
    "OptionsTradePlan",
    "StrategyMapping",
    "OptionsTradeConfig",
    "OptionsChainProvider",
    "InMemoryOptionsChainProvider",
    "OptionContractResolver",
    "ResolveResult",
    "OptionsStructureBuilder",
    "DEFAULT_OPTIONS_CONFIG",
]
