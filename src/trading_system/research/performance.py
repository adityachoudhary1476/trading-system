"""Deterministic performance analytics (Day 7 research).

All metrics are derived from the trade ledger and equity curve. Small-sample
metrics are flagged as unreliable rather than reported as proof of profitability.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import numpy as np
import pandas as pd

from .backtester import Trade, BacktestResult
from ..analysis.quant import TRADING_PERIODS


@dataclass
class PerformanceReport:
    initial_capital: float
    final_capital: float
    net_pnl: float
    total_return: float
    n_trades: int
    winning: int
    losing: int
    win_rate: float
    avg_win: float
    avg_loss: float
    largest_win: float
    largest_loss: float
    profit_factor: float
    max_drawdown: float
    avg_trade_return: float
    exposure_pct: float          # fraction of bars in market
    sharpe: float
    sortino: float
    reliable: bool               # False when the sample is too small to trust
    notes: List[str] = field(default_factory=list)


def compute_performance(result: BacktestResult) -> PerformanceReport:
    trades = result.trades
    eq = result.equity_curve
    init = result.initial_capital
    final = result.final_capital

    n = len(trades)
    nets = [t.net_pnl for t in trades]
    wins = [t.net_pnl for t in trades if t.net_pnl > 0]
    losses = [t.net_pnl for t in trades if t.net_pnl <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None if gross_win > 0 else 0.0

    # Max drawdown from equity curve.
    if len(eq):
        peak = eq["equity"].cummax()
        dd = eq["equity"] / peak - 1.0
        max_dd = float(dd.min())
    else:
        max_dd = 0.0

    # Per-bar returns for Sharpe/Sortino from equity curve.
    if len(eq) > 1:
        rets = eq["equity"].pct_change().dropna()
        sd = rets.std(ddof=0)
        mean = rets.mean()
        sharpe = float(mean / sd * np.sqrt(TRADING_PERIODS.get(result.dataset.timeframe, 252))) if sd > 0 else 0.0
        downside = rets[rets < 0].std(ddof=0)
        sortino = float(mean / downside * np.sqrt(TRADING_PERIODS.get(result.dataset.timeframe, 252))) if downside > 0 else 0.0
    else:
        sharpe = sortino = 0.0

    exposure = 0.0
    if len(result.equity_curve):
        # fraction of bars holding a position (approx via trade coverage)
        total_bars = max(1, len(result.equity_curve))
        held = sum(max(1, t.bars_held) for t in trades)
        exposure = min(1.0, held / total_bars)

    # Reliability: warn on tiny samples.
    notes: List[str] = []
    reliable = True
    if n < 10:
        reliable = False
        notes.append(f"Only {n} trades — metrics are NOT statistically meaningful.")
    if abs(max_dd) > 0.9:
        notes.append("Extreme drawdown detected; review position sizing/costs.")
    if result.quality.duplicate_bars > 0:
        notes.append("Dataset contained duplicate bars; results may be unreliable.")
    if result.quality.rows < 30:
        reliable = False
        notes.append("Dataset too small (<30 bars); treat output as a smoke test only.")

    return PerformanceReport(
        initial_capital=init, final_capital=final, net_pnl=final - init,
        total_return=(final - init) / init if init else 0.0,
        n_trades=n, winning=len(wins), losing=len(losses),
        win_rate=(len(wins) / n) if n else 0.0,
        avg_win=(sum(wins) / len(wins)) if wins else 0.0,
        avg_loss=(sum(losses) / len(losses)) if losses else 0.0,
        largest_win=max(wins) if wins else 0.0,
        largest_loss=min(losses) if losses else 0.0,
        profit_factor=profit_factor,
        max_drawdown=max_dd,
        avg_trade_return=(sum(t.ret for t in trades) / n) if n else 0.0,
        exposure_pct=exposure,
        sharpe=sharpe, sortino=sortino,
        reliable=reliable, notes=notes,
    )
