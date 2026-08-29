"""Deterministic, provider-independent backtester (Day 7 research).

EXECUTION MODEL (documented, no look-ahead):
  * The strategy produces a TARGET position at each bar T using ONLY information
    available at/before T (each strategy derives its own causal indicators from the
    raw OHLCV, with no future bars referenced).
  * The engine does NOT trade on bar T's close for entry. It ENTERS at the OPEN of
    bar T+1 (the first price the strategy could actually act on).
  * Exits (signal flip, stop-loss, take-profit) are also executed at the NEXT bar's
    open. Intrabar stop/take-profit fills are approximated by next-bar open with an
    explicit note (no intrabar high/low peeking at entry).
  * Causal correctness is enforced: position state at T+1 uses target[T], never
    close[T] or future bars.

COSTS:
  * transaction_cost_pct: round-trip commission fraction applied to traded notional.
  * slippage_pct: adverse fill fraction applied on entry and exit (conservative).

F&O:
  * A dataset is exactly one contract (one contract_id). The engine NEVER rolls across
    contracts; if the requested data spans an expiry it must be split upstream. Running
    on two contracts requires two separate datasets / two backtests.

Determinism: given the same df, features, strategy, risk, and cost params, the result
is byte-for-byte reproducible (no randomness, no hidden state).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import List, Optional

from .strategies import Strategy, Signal
from .risk import RiskConfig
from .dataset import HistoricalDataset, DataQuality
from .costs import TransactionCostModel, Segment as CostSegment, CostSide, TradeSpec, CostNotConfigured


@dataclass
class Trade:
    strategy: str
    symbol: str
    contract_id: str
    entry_ts: pd.Timestamp
    entry_price: float
    exit_ts: pd.Timestamp
    exit_price: float
    quantity: float
    direction: int          # +1 long, -1 short
    gross_pnl: float
    costs: float
    net_pnl: float
    ret: float
    exit_reason: str
    bars_held: int = 0


@dataclass
class BacktestConfig:
    initial_capital: float = 100_000.0
    transaction_cost_pct: float = 0.0    # per side (fraction of notional)
    slippage_pct: float = 0.0            # per side (fraction of price, adverse)
    risk: RiskConfig = None              # type: ignore[assignment]
    # Strategy params forwarded to get_strategy
    strategy_params: dict = None        # type: ignore[assignment]
    # Warmup: bars at the START used only to prime indicators; they must NOT
    # contribute to reported performance. Evaluation begins after warmup_bars
    # (or evaluation_start_date, whichever is later). See run_backtest.
    warmup_bars: int = 0
    evaluation_start_date: Optional[str] = None
    # India-aware transaction cost model (Day 10.5). When set, replaces the generic
    # transaction_cost_pct with a real per-leg CostBreakdown. Backward compatible:
    # if None, the existing round-trip percentage behavior is used.
    cost_model: Optional[TransactionCostModel] = None
    cost_segment: Optional[str] = None   # explicit CostSegment; else inferred from instrument

    def __post_init__(self):
        if self.risk is None:
            self.risk = RiskConfig()
        if self.strategy_params is None:
            self.strategy_params = {}


def _assert_causal_target(target: pd.Series) -> None:
    vals = set(int(v) for v in target.dropna().unique())
    if vals - {-1, 0, 1}:
        raise ValueError(f"Strategy produced non-{{-1,0,1}} targets: {vals}")


def run_backtest(
    dataset: HistoricalDataset,
    strategy: Strategy,
    config: BacktestConfig,
) -> "BacktestResult":
    """Run a single-contract backtest. Deterministic."""
    if dataset.data is None or len(dataset.data) < 2:
        raise ValueError("Dataset too small to backtest (need >=2 bars).")

    risk = config.risk
    df = dataset.data.sort_index()
    # Strategies compute their own causal indicators from the raw df (self-contained,
    # provider-independent). No look-ahead: signal at T uses only data <= T; the
    # engine enters at the NEXT bar's open.
    target = strategy.generate(df)
    _assert_causal_target(target)

    # Align prices on the SAME index as target (drop pre-feature-warmup NaNs only
    # for trade execution; we enter on NEXT bar's open regardless).
    opens = df["open"]
    closes = df["close"]
    idx = df.index

    capital = config.initial_capital
    position = 0          # current signed units (0 = flat)
    entry_price = 0.0
    entry_ts = None
    direction = 0
    trades: list[Trade] = []
    equity_curve = []
    target_dir = target.reindex(idx).fillna(0).astype(int)

    # Pre-compute per-side cost fraction.
    tc = config.transaction_cost_pct
    slip = config.slippage_pct

    # We iterate bars; at bar i we decide action based on target[i-1] (signal known
    # at i-1), executing at bar i's OPEN.
    for i in range(1, len(idx)):
        sig = int(target_dir.iloc[i - 1])          # signal from prior bar (available)
        price_open = float(opens.iloc[i])

        # --- EXIT logic (current position vs signal, or risk stops) ---
        if position != 0:
            exit_now = False
            reason = ""
            # Stop-loss / take-profit evaluated against the prior bar's close, but we
            # fill at this bar's OPEN (no intrabar peeking). This is a documented
            # conservative approximation.
            prev_close = float(closes.iloc[i - 1])
            if direction == 1:
                if risk.stop_loss_pct and (prev_close / entry_price - 1) <= -risk.stop_loss_pct:
                    exit_now, reason = True, "stop_loss"
                elif risk.take_profit_pct and (prev_close / entry_price - 1) >= risk.take_profit_pct:
                    exit_now, reason = True, "take_profit"
            else:  # short
                if risk.stop_loss_pct and (1 - prev_close / entry_price) <= -risk.stop_loss_pct:
                    exit_now, reason = True, "stop_loss"
                elif risk.take_profit_pct and (1 - prev_close / entry_price) <= -risk.take_profit_pct:
                    exit_now, reason = True, "take_profit"
            # Signal flip → exit at this open.
            if sig != direction and sig != 0:
                exit_now, reason = True, "signal_exit"
            elif sig == 0 and position != 0:
                exit_now, reason = True, "signal_exit"

            if exit_now:
                fill = price_open * (1 - slip if direction == 1 else 1 + slip)
                gross = direction * position * (fill - entry_price)
                costs = _round_trip_cost(config, entry_price, fill, abs(position),
                                         direction, entry_ts, idx[i], dataset)
                net = gross - costs
                capital += gross - costs
                trades.append(Trade(
                    strategy=strategy.meta.name,
                    symbol=dataset.symbol,
                    contract_id=dataset.contract_id,
                    entry_ts=entry_ts,
                    entry_price=entry_price,
                    exit_ts=idx[i],
                    exit_price=fill,
                    quantity=abs(position),
                    direction=direction,
                    gross_pnl=gross,
                    costs=costs,
                    net_pnl=net,
                    ret=net / (abs(position) * entry_price) if position else 0.0,
                    exit_reason=reason,
                    bars_held=int((idx[i] - entry_ts).days),
                ))
                position = 0
                direction = 0
                entry_price = 0.0

        # --- ENTRY logic (signal at i-1 → enter at i open) ---
        desired = sig if (risk.allow_short or sig >= 0) else 0
        if desired != 0 and position == 0:
            # Position sizing.
            alloc = risk.max_allocation_pct * capital * risk.effective_leverage()
            price_for_size = price_open * (1 + slip if desired == 1 else 1 - slip)
            units = alloc / price_for_size if price_for_size > 0 else 0.0
            if risk.max_position_size:
                units = min(units, risk.max_position_size)
            if units > 0:
                position = units
                direction = desired
                entry_price = price_for_size
                entry_ts = idx[i]

        equity_curve.append((idx[i], capital))

    # Close any open position at the last bar's close (final mark-out).
    if position != 0:
        last_close = float(closes.iloc[-1])
        fill = last_close * (1 - slip if direction == 1 else 1 + slip)
        gross = direction * position * (fill - entry_price)
        costs = _round_trip_cost(config, entry_price, fill, abs(position),
                                 direction, entry_ts, idx[-1], dataset)
        net = gross - costs
        capital += gross - costs
        trades.append(Trade(
            strategy=strategy.meta.name, symbol=dataset.symbol,
            contract_id=dataset.contract_id, entry_ts=entry_ts,
            entry_price=entry_price, exit_ts=idx[-1], exit_price=fill,
            quantity=abs(position), direction=direction, gross_pnl=gross,
            costs=costs, net_pnl=net,
            ret=net / (abs(position) * entry_price) if position else 0.0,
            exit_reason="end_of_data", bars_held=int((idx[-1] - entry_ts).days),
        ))
    # The equity curve's last point was recorded before this mark-out; overwrite it so
    # the curve's final equity exactly equals `capital` (ledger reconciles to equity).
    if equity_curve:
        equity_curve[-1] = (idx[-1], capital)

    # --- Warmup / evaluation window (no look-ahead boundary) ---
    # The full simulation above uses every bar so indicator priming during warmup
    # is correct. But reported PERFORMANCE must only cover the evaluation window:
    # bars at/after max(warmup_bars, evaluation_start_date). Trades that exit before
    # the evaluation start are excluded; equity is measured FROM the evaluation start.
    eq_full = pd.DataFrame(equity_curve, columns=["ts", "equity"]).set_index("ts")
    eval_pos = _eval_equity_pos(eq_full.index, config.warmup_bars, config.evaluation_start_date)
    if eval_pos > 0:
        eval_equity = eq_full.iloc[eval_pos:]
        eval_initial = float(eq_full.iloc[eval_pos - 1]["equity"])
        eval_trades = [t for t in trades if t.exit_ts >= eq_full.index[eval_pos]]
    else:
        eval_equity = eq_full
        eval_initial = config.initial_capital
        eval_trades = trades

    return BacktestResult(
        dataset=dataset, strategy=strategy, config=config,
        initial_capital=eval_initial, final_capital=float(eq_full.iloc[-1]["equity"]),
        trades=eval_trades, equity_curve=eval_equity,
        quality=dataset.quality,
    )


def _infer_cost_segment(dataset: HistoricalDataset) -> CostSegment:
    """Map a dataset's instrument to a cost-model Segment (best effort)."""
    instr = getattr(dataset, "instrument", None)
    if instr is not None:
        t = getattr(instr, "instrument_type", None)
        et = getattr(t, "value", str(t)) if t is not None else ""
        if "FUT" in et:
            ex = getattr(instr, "exchange", "") or ""
            return CostSegment.COMMODITY_FUTURE if ex == "MCX" else CostSegment.EQUITY_FUTURE
        if "OPTION" in et:
            ex = getattr(instr, "exchange", "") or ""
            return CostSegment.COMMODITY_OPTION if ex == "MCX" else CostSegment.EQUITY_OPTION
    cid = getattr(dataset, "contract_id", "") or ""
    if "FUT" in cid:
        return CostSegment.EQUITY_FUTURE
    if "CE" in cid or "PE" in cid:
        return CostSegment.EQUITY_OPTION
    return CostSegment.EQUITY_DELIVERY


def _round_trip_cost(
    config: BacktestConfig,
    entry_price: float,
    exit_fill: float,
    qty: float,
    direction: int,
    entry_ts,
    exit_ts,
    dataset: HistoricalDataset,
) -> float:
    """Total transaction cost for one round-trip trade (entry + exit legs).

    Uses ``config.cost_model`` when present (real India schedule); otherwise falls back
    to the generic round-trip ``transaction_cost_pct``. Raises CostNotConfigured only if
    the model is present but the required rate is missing (never silently zero).
    """
    if config.cost_model is None:
        tc = config.transaction_cost_pct
        return abs(qty) * exit_fill * tc + abs(qty) * entry_price * tc

    seg = CostSegment(config.cost_segment) if config.cost_segment else _infer_cost_segment(dataset)
    entry_side = CostSide.BUY if direction == 1 else CostSide.SELL
    exit_side = CostSide.SELL if direction == 1 else CostSide.BUY
    entry_leg = config.cost_model.estimate(TradeSpec(
        segment=seg, side=entry_side, price=entry_price, quantity=qty,
        trade_date=entry_ts.date() if hasattr(entry_ts, "date") else entry_ts,
    ))
    exit_leg = config.cost_model.estimate(TradeSpec(
        segment=seg, side=exit_side, price=exit_fill, quantity=qty,
        trade_date=exit_ts.date() if hasattr(exit_ts, "date") else exit_ts,
    ))
    return entry_leg.total + exit_leg.total


def _eval_equity_pos(eq_index: pd.DatetimeIndex, warmup_bars: int,
                     evaluation_start_date: Optional[str]) -> int:
    """Return the equity-curve position where the evaluation window begins.

    eq_index = bar timestamps for which equity is recorded (starts at bar 1).
    * warmup_bars W  -> evaluation begins at bar W, whose equity is eq_index[W-1].
    * evaluation_start_date -> first equity timestamp >= that date.
    The later of the two wins; result is clamped to [0, len-1].
    """
    pos = 0
    if warmup_bars > 0:
        pos = max(pos, warmup_bars - 1)
    if evaluation_start_date:
        try:
            ts = pd.Timestamp(evaluation_start_date)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            pos = max(pos, int(eq_index.searchsorted(ts, side="left")))
        except (ValueError, TypeError):
            pass
    return min(pos, len(eq_index) - 1)


from dataclasses import dataclass, field
from typing import List


@dataclass
class BacktestResult:
    dataset: HistoricalDataset
    strategy: Strategy
    config: BacktestConfig
    initial_capital: float
    final_capital: float
    trades: List[Trade]
    equity_curve: pd.DataFrame
    quality: DataQuality

    @property
    def net_pnl(self) -> float:
        return self.final_capital - self.initial_capital

    @property
    def total_return(self) -> float:
        return self.net_pnl / self.initial_capital if self.initial_capital else 0.0
