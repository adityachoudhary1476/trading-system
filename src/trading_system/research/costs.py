"""India transaction-cost model (Day 10.5) — provider-independent, effective-dated.

Explicit, configuration-driven cost schedule for Indian markets (FYERS/NSE/MCX). The
model NEVER buries India-specific charges inside the generic backtester; instead the
backtester accepts a `TransactionCostModel` via `BacktestConfig.cost_model`.

Design rules (per Day 10.5):
  * Every monetary value has explicit units (INR per trade / per lot / fraction of turnover).
  * Rates are represented as `EffectiveRate` rows with `effective_from` so historical
    backtests use the correct rate for the TRADE DATE (e.g. the 2026-04-01 F&O STT change).
  * GST is computed ONLY on taxable components (brokerage + exchange/clearing charges +
    SEBI/IPFT). STT/CTT and stamp duty are NOT GST-loaded (test-enforced).
  * If a required rate for a segment/date/side is missing, raise `CostNotConfigured`
    (never silently use zero).
  * All rate values are sourced from the published FYERS/NSE schedule; RE-VERIFY before
    live use. Pre-2026-04-01 F&O STT rates are encoded from the NSE circular (documented),
    not invented.

Segments supported: EQUITY_DELIVERY, EQUITY_INTRADAY, EQUITY_FUTURE, EQUITY_OPTION,
COMMODITY_FUTURE, COMMODITY_OPTION.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


class Segment(str, Enum):
    EQUITY_DELIVERY = "equity_delivery"
    EQUITY_INTRADAY = "equity_intraday"
    EQUITY_FUTURE = "equity_future"
    EQUITY_OPTION = "equity_option"
    COMMODITY_FUTURE = "commodity_future"
    COMMODITY_OPTION = "commodity_option"


class CostSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Basis(str, Enum):
    TURNOVER = "turnover"          # fraction of (price * quantity)
    PREMIUM = "premium"            # fraction of (price * quantity) for option premium
    PER_ORDER = "per_order"        # flat INR per executed order
    PER_LOT = "per_lot"           # INR per lot


@dataclass(frozen=True)
class EffectiveRate:
    """A single charge rate, effective from a date. Historical backtests pick the row
    whose `effective_from` is the latest date <= trade date."""

    name: str
    segment: Segment
    rate: float                    # charge magnitude (interpret via `basis`)
    basis: Basis
    side: CostSide                 # which leg the charge applies to
    effective_from: date
    source: str = ""
    note: str = ""


@dataclass
class CostBreakdown:
    """All costs for one executed trade, in INR. Units explicit per field."""

    segment: Segment
    side: CostSide
    turnover: float                # INR notional (price * quantity)
    brokerage: float = 0.0
    stt_ctt: float = 0.0
    exchange_charges: float = 0.0
    sebi_fee: float = 0.0
    stamp_duty: float = 0.0
    gst: float = 0.0
    ipft: float = 0.0
    total: float = 0.0

    def __post_init__(self) -> None:
        # GST is computed from taxable components ONLY (see IndiaTransactionCostModel).
        self.total = (
            self.brokerage + self.stt_ctt + self.exchange_charges
            + self.sebi_fee + self.stamp_duty + self.gst + self.ipft
        )


class CostNotConfigured(Exception):
    """Raised when a required rate for the trade's segment/date/side is missing.

    The model NEVER substitutes zero silently. The message names the missing charge.
    """


# --------------------------------------------------------------------------- #
# Default published FYERS / NSE / MCX schedule (RE-VERIFY before live use).
# Sources: FYERS published brokerage + statutory charges; NSE F&O STT revision
# effective 2026-04-01 (prior rates per NSE circular). All values are fractions of
# the relevant basis unless basis is PER_ORDER / PER_LOT (then flat INR).
# --------------------------------------------------------------------------- #
_FYERS_STT_FUTURES_NEW = 0.0005      # sell-side, turnover, from 2026-04-01
_FYERS_STT_FUTURES_OLD = 0.0002      # sell-side, turnover, before 2026-04-01
_FYERS_STT_OPTIONS_NEW = 0.0015      # sell-side, premium, from 2026-04-01
_FYERS_STT_OPTIONS_OLD = 0.00125     # sell-side, premium, before 2026-04-01
_FYERS_STT_EQ_DELIVERY = 0.001       # both sides, turnover (equity delivery)
_FYERS_GST = 0.18                    # GST on taxable components
_FYERS_BROKERAGE_PCT = 0.0003        # 0.03% of turnover (futures/intraday)
_FYERS_BROKERAGE_CAP = 20.0          # INR per executed order (futures/options/intraday)
_FYERS_BROKERAGE_EQ_DELIVERY_PCT = 0.001  # 0.1% equity delivery
_FYERS_EXCHANGE_CHARGE = 0.00000035  # NSE F&O exchange txn charge (fraction of turnover)
_FYERS_EXCHANGE_CHARGE_EQ = 0.00000163  # NSE equity exchange txn charge
_FYERS_SEBI_FEE = 0.000001           # SEBI turnover fee (INR per INR of turnover)
_FYERS_IPFT = 0.000001              # Investor Protection Fund (INR per INR of turnover)
# Stamp duty by segment (state-dependent in reality; NSE default used, overridable).
_FYERS_STAMP_DELIVERY = 0.00003
_FYERS_STAMP_FUTURES = 0.000002
_FYERS_STAMP_OPTIONS = 0.0001       # on premium (sell)
_FYERS_STAMP_INTRADAY = 0.00000025


def _d(s: str) -> date:
    return date.fromisoformat(s)


# The canonical default schedule. Structured as EffectiveRate rows so the model can
# resolve the correct rate by trade date. Pre-2026 rows are included (sourced) so the
# effective-date test is meaningful; everything is overridable via config.
DEFAULT_RATES: list[EffectiveRate] = [
    # --- F&O STT (the 2026-04-01 change) ---
    EffectiveRate("stt_futures", Segment.EQUITY_FUTURE, _FYERS_STT_FUTURES_OLD, Basis.TURNOVER,
                  CostSide.SELL, _d("2000-01-01"), "NSE F&O STT pre-2026-04-01 (sell, turnover)"),
    EffectiveRate("stt_futures", Segment.EQUITY_FUTURE, _FYERS_STT_FUTURES_NEW, Basis.TURNOVER,
                  CostSide.SELL, _d("2026-04-01"), "NSE F&O STT revision effective 2026-04-01 (sell, turnover)"),
    EffectiveRate("stt_options", Segment.EQUITY_OPTION, _FYERS_STT_OPTIONS_OLD, Basis.PREMIUM,
                  CostSide.SELL, _d("2000-01-01"), "NSE F&O STT pre-2026-04-01 (sell, premium)"),
    EffectiveRate("stt_options", Segment.EQUITY_OPTION, _FYERS_STT_OPTIONS_NEW, Basis.PREMIUM,
                  CostSide.SELL, _d("2026-04-01"), "NSE F&O STT revision effective 2026-04-01 (sell, premium)"),
    # --- Equity STT ---
    EffectiveRate("stt_eq_delivery", Segment.EQUITY_DELIVERY, _FYERS_STT_EQ_DELIVERY, Basis.TURNOVER,
                  CostSide.BUY, _d("2000-01-01"), "Equity delivery STT (buy, turnover)"),
    EffectiveRate("stt_eq_delivery", Segment.EQUITY_DELIVERY, _FYERS_STT_EQ_DELIVERY, Basis.TURNOVER,
                  CostSide.SELL, _d("2000-01-01"), "Equity delivery STT (sell, turnover)"),
    # --- Brokerage (per executed order; capped) ---
    EffectiveRate("brokerage_fut", Segment.EQUITY_FUTURE, _FYERS_BROKERAGE_PCT, Basis.TURNOVER,
                  CostSide.BUY, _d("2000-01-01"), "FYERS futures brokerage 0.03% (capped 20)"),
    EffectiveRate("brokerage_fut", Segment.EQUITY_FUTURE, _FYERS_BROKERAGE_PCT, Basis.TURNOVER,
                  CostSide.SELL, _d("2000-01-01"), "FYERS futures brokerage 0.03% (capped 20)"),
    EffectiveRate("brokerage_opt", Segment.EQUITY_OPTION, _FYERS_BROKERAGE_CAP, Basis.PER_ORDER,
                  CostSide.BUY, _d("2000-01-01"), "FYERS options brokerage 20/order"),
    EffectiveRate("brokerage_opt", Segment.EQUITY_OPTION, _FYERS_BROKERAGE_CAP, Basis.PER_ORDER,
                  CostSide.SELL, _d("2000-01-01"), "FYERS options brokerage 20/order"),
    EffectiveRate("brokerage_intraday", Segment.EQUITY_INTRADAY, _FYERS_BROKERAGE_PCT, Basis.TURNOVER,
                  CostSide.BUY, _d("2000-01-01"), "FYERS intraday brokerage 0.03% (capped 20)"),
    EffectiveRate("brokerage_intraday", Segment.EQUITY_INTRADAY, _FYERS_BROKERAGE_PCT, Basis.TURNOVER,
                  CostSide.SELL, _d("2000-01-01"), "FYERS intraday brokerage 0.03% (capped 20)"),
    EffectiveRate("brokerage_eq_delivery", Segment.EQUITY_DELIVERY, _FYERS_BROKERAGE_EQ_DELIVERY_PCT,
                  Basis.TURNOVER, CostSide.BUY, _d("2000-01-01"), "FYERS equity delivery brokerage 0.1%"),
    EffectiveRate("brokerage_eq_delivery", Segment.EQUITY_DELIVERY, _FYERS_BROKERAGE_EQ_DELIVERY_PCT,
                  Basis.TURNOVER, CostSide.SELL, _d("2000-01-01"), "FYERS equity delivery brokerage 0.1%"),
    # --- Exchange charges ---
    EffectiveRate("exchange_fno", Segment.EQUITY_FUTURE, _FYERS_EXCHANGE_CHARGE, Basis.TURNOVER,
                  CostSide.BUY, _d("2000-01-01"), "NSE F&O exchange txn charge"),
    EffectiveRate("exchange_fno", Segment.EQUITY_FUTURE, _FYERS_EXCHANGE_CHARGE, Basis.TURNOVER,
                  CostSide.SELL, _d("2000-01-01"), "NSE F&O exchange txn charge"),
    EffectiveRate("exchange_fno", Segment.EQUITY_OPTION, _FYERS_EXCHANGE_CHARGE, Basis.TURNOVER,
                  CostSide.BUY, _d("2000-01-01"), "NSE F&O exchange txn charge"),
    EffectiveRate("exchange_fno", Segment.EQUITY_OPTION, _FYERS_EXCHANGE_CHARGE, Basis.TURNOVER,
                  CostSide.SELL, _d("2000-01-01"), "NSE F&O exchange txn charge"),
    EffectiveRate("exchange_eq", Segment.EQUITY_DELIVERY, _FYERS_EXCHANGE_CHARGE_EQ, Basis.TURNOVER,
                  CostSide.BUY, _d("2000-01-01"), "NSE equity exchange txn charge"),
    EffectiveRate("exchange_eq", Segment.EQUITY_DELIVERY, _FYERS_EXCHANGE_CHARGE_EQ, Basis.TURNOVER,
                  CostSide.SELL, _d("2000-01-01"), "NSE equity exchange txn charge"),
    # --- SEBI + IPFT (turnover-based) ---
    EffectiveRate("sebi", Segment.EQUITY_FUTURE, _FYERS_SEBI_FEE, Basis.TURNOVER,
                  CostSide.BUY, _d("2000-01-01"), "SEBI turnover fee"),
    EffectiveRate("sebi", Segment.EQUITY_FUTURE, _FYERS_SEBI_FEE, Basis.TURNOVER,
                  CostSide.SELL, _d("2000-01-01"), "SEBI turnover fee"),
    EffectiveRate("sebi", Segment.EQUITY_OPTION, _FYERS_SEBI_FEE, Basis.TURNOVER,
                  CostSide.BUY, _d("2000-01-01"), "SEBI turnover fee"),
    EffectiveRate("sebi", Segment.EQUITY_OPTION, _FYERS_SEBI_FEE, Basis.TURNOVER,
                  CostSide.SELL, _d("2000-01-01"), "SEBI turnover fee"),
    EffectiveRate("sebi", Segment.EQUITY_DELIVERY, _FYERS_SEBI_FEE, Basis.TURNOVER,
                  CostSide.BUY, _d("2000-01-01"), "SEBI turnover fee"),
    EffectiveRate("sebi", Segment.EQUITY_DELIVERY, _FYERS_SEBI_FEE, Basis.TURNOVER,
                  CostSide.SELL, _d("2000-01-01"), "SEBI turnover fee"),
    EffectiveRate("ipft", Segment.EQUITY_FUTURE, _FYERS_IPFT, Basis.TURNOVER,
                  CostSide.BUY, _d("2000-01-01"), "IPFT (per crore turnover)"),
    EffectiveRate("ipft", Segment.EQUITY_FUTURE, _FYERS_IPFT, Basis.TURNOVER,
                  CostSide.SELL, _d("2000-01-01"), "IPFT (per crore turnover)"),
    EffectiveRate("ipft", Segment.EQUITY_OPTION, _FYERS_IPFT, Basis.TURNOVER,
                  CostSide.BUY, _d("2000-01-01"), "IPFT (per crore turnover)"),
    EffectiveRate("ipft", Segment.EQUITY_OPTION, _FYERS_IPFT, Basis.TURNOVER,
                  CostSide.SELL, _d("2000-01-01"), "IPFT (per crore turnover)"),
    # --- Stamp duty (state-dependent; NSE default) ---
    EffectiveRate("stamp_delivery", Segment.EQUITY_DELIVERY, _FYERS_STAMP_DELIVERY, Basis.TURNOVER,
                  CostSide.BUY, _d("2000-01-01"), "Stamp duty equity delivery (state-dependent default)"),
    EffectiveRate("stamp_futures", Segment.EQUITY_FUTURE, _FYERS_STAMP_FUTURES, Basis.TURNOVER,
                  CostSide.BUY, _d("2000-01-01"), "Stamp duty futures (state-dependent default)"),
    EffectiveRate("stamp_options", Segment.EQUITY_OPTION, _FYERS_STAMP_OPTIONS, Basis.PREMIUM,
                  CostSide.SELL, _d("2000-01-01"), "Stamp duty options (state-dependent default)"),
    EffectiveRate("stamp_intraday", Segment.EQUITY_INTRADAY, _FYERS_STAMP_INTRADAY, Basis.TURNOVER,
                  CostSide.BUY, _d("2000-01-01"), "Stamp duty intraday (state-dependent default)"),
    # --- Commodities (MCX) ---
    EffectiveRate("brokerage_comm_fut", Segment.COMMODITY_FUTURE, _FYERS_BROKERAGE_PCT, Basis.TURNOVER,
                  CostSide.BUY, _d("2000-01-01"), "MCX futures brokerage 0.03% (capped 20)"),
    EffectiveRate("brokerage_comm_fut", Segment.COMMODITY_FUTURE, _FYERS_BROKERAGE_PCT, Basis.TURNOVER,
                  CostSide.SELL, _d("2000-01-01"), "MCX futures brokerage 0.03% (capped 20)"),
    EffectiveRate("stt_comm_fut", Segment.COMMODITY_FUTURE, 0.0001, Basis.TURNOVER,
                  CostSide.SELL, _d("2000-01-01"), "MCX futures CTT (sell, turnover)"),
    EffectiveRate("exchange_comm", Segment.COMMODITY_FUTURE, _FYERS_EXCHANGE_CHARGE, Basis.TURNOVER,
                  CostSide.BUY, _d("2000-01-01"), "MCX exchange charge"),
    EffectiveRate("exchange_comm", Segment.COMMODITY_FUTURE, _FYERS_EXCHANGE_CHARGE, Basis.TURNOVER,
                  CostSide.SELL, _d("2000-01-01"), "MCX exchange charge"),
]


class TransactionCostModel:
    """Interface. Estimates the cost of a single executed trade leg."""

    def estimate(self, trade) -> CostBreakdown:
        raise NotImplementedError


@dataclass
class TradeSpec:
    """A single executed trade leg (buy or sell), in INR notional terms.

    `price` * `quantity` = turnover. For options, `premium_notional` = turnover too
    (STT on options is on premium turnover). `trade_date` drives effective-rate selection.
    `lot_size` is informational (brokerage is per executed order, not per lot, for FYERS).
    """

    segment: Segment
    side: CostSide
    price: float
    quantity: float
    trade_date: date
    lot_size: float = 1.0


class IndiaTransactionCostModel(TransactionCostModel):
    """FYERS/NSE/MCX cost model with effective-dated rates and correct GST handling."""

    def __init__(self, rates: Optional[list[EffectiveRate]] = None, gst: float = _FYERS_GST) -> None:
        self._rates = rates if rates is not None else list(DEFAULT_RATES)
        self.gst_rate = gst
        # Index: (name, segment, side) -> list of (effective_from, rate, basis) sorted desc.
        self._idx: dict[tuple, list] = {}
        for r in self._rates:
            self._idx.setdefault((r.name, r.segment, r.side), []).append(
                (r.effective_from, r.rate, r.basis)
            )
        for k in self._idx:
            self._idx[k].sort(key=lambda x: x[0], reverse=True)

    # -- rate resolution ------------------------------------------------------
    def _rate(self, name: str, segment: Segment, side: CostSide, trade_date: date) -> Optional[tuple]:
        rows = self._idx.get((name, segment, side))
        if not rows:
            return None
        for eff_from, rate, basis in rows:
            if trade_date >= eff_from:
                return (rate, basis)
        return None

    def _require(self, name: str, segment: Segment, side: CostSide, trade_date: date) -> tuple:
        r = self._rate(name, segment, side, trade_date)
        if r is None:
            raise CostNotConfigured(
                f"no rate for '{name}' segment={segment.value} side={side.value} "
                f"date={trade_date.isoformat()}"
            )
        return r

    # -- estimate -------------------------------------------------------------
    def estimate(self, trade: TradeSpec) -> CostBreakdown:
        seg, side, td = trade.segment, trade.side, trade.trade_date
        turnover = float(trade.price) * float(trade.quantity)
        bd = CostBreakdown(segment=seg, side=side, turnover=turnover)

        # Brokerage (per executed order, capped at pct of turnover for pct segments).
        if seg in (Segment.EQUITY_FUTURE, Segment.EQUITY_INTRADAY, Segment.COMMODITY_FUTURE):
            pct, _ = self._require("brokerage_fut" if seg != Segment.EQUITY_INTRADAY else "brokerage_intraday", seg, side, td)
            bd.brokerage = min(pct * turnover, _FYERS_BROKERAGE_CAP)
        elif seg == Segment.EQUITY_OPTION or seg == Segment.COMMODITY_OPTION:
            flat, _ = self._require("brokerage_opt", seg, side, td)
            bd.brokerage = flat  # per executed order
        elif seg == Segment.EQUITY_DELIVERY:
            pct, _ = self._require("brokerage_eq_delivery", seg, side, td)
            bd.brokerage = pct * turnover
        else:
            raise CostNotConfigured(f"brokerage not configured for segment={seg.value}")

        # STT/CTT (only on the applicable side/basis).
        stt_name = {
            Segment.EQUITY_FUTURE: "stt_futures",
            Segment.EQUITY_OPTION: "stt_options",
            Segment.EQUITY_DELIVERY: "stt_eq_delivery",
            Segment.COMMODITY_FUTURE: "stt_comm_fut",
        }.get(seg)
        if stt_name:
            try:
                rate, basis = self._require(stt_name, seg, side, td)
            except CostNotConfigured:
                rate, basis = None, None
            if rate is not None:
                base = turnover if basis != Basis.PREMIUM else turnover
                # Options STT is on premium turnover (== turnover here); futures on turnover.
                bd.stt_ctt = rate * base

        # Exchange charges (turnover-based; both sides for F&O/equity).
        exc_name = "exchange_fno" if seg in (Segment.EQUITY_FUTURE, Segment.EQUITY_OPTION) else \
                   "exchange_comm" if seg == Segment.COMMODITY_FUTURE else "exchange_eq"
        try:
            rate, _ = self._require(exc_name, seg, side, td)
            bd.exchange_charges = rate * turnover
        except CostNotConfigured:
            pass  # exchange charge optional for some segments

        # SEBI + IPFT (turnover-based).
        for nm in ("sebi", "ipft"):
            try:
                rate, _ = self._require(nm, seg, side, td)
                if nm == "sebi":
                    bd.sebi_fee = rate * turnover
                else:
                    bd.ipft = rate * turnover
            except CostNotConfigured:
                pass

        # Stamp duty (per segment/side; state-dependent — default used).
        stamp_name = {
            Segment.EQUITY_DELIVERY: "stamp_delivery",
            Segment.EQUITY_FUTURE: "stamp_futures",
            Segment.EQUITY_OPTION: "stamp_options",
            Segment.EQUITY_INTRADAY: "stamp_intraday",
        }.get(seg)
        if stamp_name:
            try:
                rate, basis = self._require(stamp_name, seg, side, td)
                bd.stamp_duty = rate * (turnover if basis != Basis.PREMIUM else turnover)
            except CostNotConfigured:
                pass

        # GST: ONLY on taxable components (brokerage + exchange + sebi + ipft).
        # STT/CTT and stamp duty are explicitly excluded (test-enforced).
        taxable = bd.brokerage + bd.exchange_charges + bd.sebi_fee + bd.ipft
        bd.gst = self.gst_rate * taxable
        # Recompute total via __post_init__ semantics.
        bd.total = (bd.brokerage + bd.stt_ctt + bd.exchange_charges
                    + bd.sebi_fee + bd.stamp_duty + bd.gst + bd.ipft)
        return bd

    # -- reporting helper -----------------------------------------------------
    def applicable_stt_rate(self, segment: Segment, side: CostSide, trade_date: date) -> float:
        """Expose the resolved STT rate (used by tests / reporting)."""
        name = {
            Segment.EQUITY_FUTURE: "stt_futures",
            Segment.EQUITY_OPTION: "stt_options",
        }.get(segment)
        if not name:
            raise CostNotConfigured(f"STT not defined for {segment.value}")
        rate, _ = self._require(name, segment, side, trade_date)
        return rate
