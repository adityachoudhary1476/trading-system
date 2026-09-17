#!/usr/bin/env python3
"""Round 4: Final summary - detailed analysis of the top 10 strategies across all rounds.

Runs the best strategies with full trade logging for the top 5 performers.
"""
from __future__ import annotations

import warnings
import numpy as np
import pandas as pd
from trading_system.research.backtester import BacktestConfig, run_backtest, Trade
from trading_system.research.costs import IndiaTransactionCostModel, Segment as CostSegment
from trading_system.research.dataset import HistoricalDataset
from trading_system.research.performance import compute_performance
from trading_system.research.strategies import (
    Strategy, Signal, StrategyMeta,
    EMATrendStrategy,
)
from trading_system.research.strategy_lab.spec import (
    StrategySpec, const_operand, field_operand,
    indicator_operand, logic, make_condition,
)
from trading_system.research.strategy_lab.interpreter import build_strategy
from trading_system.research.strategy_lab.engine import merged_backtest_config
from trading_system.research.risk import RiskConfig
from tabulate import tabulate

warnings.filterwarnings("ignore", category=FutureWarning)

CSV_PATH = "nifty-daily-moves-and-gaps/data/nifty50.csv"
INITIAL_CAPITAL = 100_000.0


def load_nifty_dataset():
    df = pd.read_csv(CSV_PATH, parse_dates=["Date"])
    df.index = pd.DatetimeIndex(df["Date"])
    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"
    })
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    df = df.tz_localize("UTC")
    return HistoricalDataset(symbol="NSE:NIFTY", timeframe="1d", data=df, contract_id="NSE:NIFTY")


def make_risk_config(stop_loss_pct=None, take_profit_pct=None, max_alloc=0.95):
    return RiskConfig(
        max_allocation_pct=max_alloc,
        allow_short=False,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
    )


def run_backtest_builtin(dataset, strat, wb, stop=None, take=None):
    cfg = BacktestConfig(
        initial_capital=INITIAL_CAPITAL,
        slippage_pct=0.001,
        cost_model=IndiaTransactionCostModel(),
        cost_segment=CostSegment.EQUITY_FUTURE.value,
        warmup_bars=wb,
        risk=make_risk_config(stop, take),
    )
    result = run_backtest(dataset, strat, cfg)
    perf = compute_performance(result)
    return perf, result, cfg


def run_backtest_spec(dataset, spec, wb):
    base = BacktestConfig(
        initial_capital=INITIAL_CAPITAL,
        slippage_pct=0.001,
        cost_model=IndiaTransactionCostModel(),
        cost_segment=CostSegment.EQUITY_FUTURE.value,
        warmup_bars=wb,
    )
    cfg = merged_backtest_config(spec, base)
    strategy = build_strategy(spec)
    result = run_backtest(dataset, strategy, cfg)
    perf = compute_performance(result)
    return perf, result, cfg


def ema_cross_spec(fast, slow, stop=None, take=None, allow_short=False):
    fk, sk = f"ema_{fast}", f"ema_{slow}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    payload = {
        "name": f"EMA {fast}-{slow} cross",
        "description": f"LONG when EMA({fast}) crosses above EMA({slow}); exit on cross below.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [
            {"name": "ema", "params": {"window": fast}},
            {"name": "ema", "params": {"window": slow}},
        ],
        "entry": make_condition(indicator_operand(fk), "crosses_above", indicator_operand(sk)),
        "exit": make_condition(indicator_operand(fk), "crosses_below", indicator_operand(sk)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }
    if allow_short:
        payload["entry_short"] = make_condition(indicator_operand(fk), "crosses_below", indicator_operand(sk))
        payload["risk"]["allow_short"] = True
    return StrategySpec.from_model_json(payload, model="round4-script")


def sma_cross_spec(fast, slow, stop=None, take=None):
    fk, sk = f"sma_{fast}", f"sma_{slow}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"SMA {fast}-{slow} cross",
        "description": f"LONG when SMA({fast}) crosses above SMA({slow}); exit on cross below.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [
            {"name": "sma", "params": {"window": fast}},
            {"name": "sma", "params": {"window": slow}},
        ],
        "entry": make_condition(indicator_operand(fk), "crosses_above", indicator_operand(sk)),
        "exit": make_condition(indicator_operand(fk), "crosses_below", indicator_operand(sk)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round4-script")


def print_trade_log(perf, result, max_trades=30):
    """Print a formatted trade log for a backtest result."""
    trades = result.trades
    print(f"\n  Exit reason breakdown:")
    from collections import Counter
    reasons = Counter(t.exit_reason for t in trades)
    for reason, count in sorted(reasons.items()):
        r_trades = [t for t in trades if t.exit_reason == reason]
        r_wins = sum(1 for t in r_trades if t.net_pnl > 0)
        r_losses = sum(1 for t in r_trades if t.net_pnl <= 0)
        r_net = sum(t.net_pnl for t in r_trades)
        print(f"        {reason}: {count:4d} trades (W:{r_wins} L:{r_losses})  Net: Rs. {r_net:>12,.0f}")

    print(f"\n  Top {min(max_trades, len(trades))} trades by net PnL:")
    sorted_trades = sorted(trades, key=lambda t: t.net_pnl, reverse=True)
    print(f"  {'#':>3}  {'Entry':<12} {'Exit':<12} {'Dir':<4} {'EntryPx':>10} {'ExitPx':>10} {'Qty':>6} "
          f"{'Gross':>12} {'Costs':>8} {'Net':>12} {'Bars':>5} {'Reason':<14}")
    print(f"  {'---':>3}  {'------------':<12} {'------------':<12} {'----':<4} {'----------':>10} {'----------':>10} {'------':>6} "
          f"{'------------':>12} {'--------':>8} {'------------':>12} {'-----':>5} {'--------------':<14}")
    for i, t in enumerate(sorted_trades[:max_trades], 1):
        print(f"  {i:>3}  {t.entry_ts.date()}  {t.exit_ts.date()}  {'LONG' if t.direction > 0 else 'SHORT':<4} "
              f"{t.entry_price:>10.2f} {t.exit_price:>10.2f} {t.quantity:>6.0f} "
              f"{t.gross_pnl:>12,.0f} {t.costs:>8,.0f} {t.net_pnl:>12,.0f} {t.bars_held:>5d} {t.exit_reason:<14}")


def main():
    print("Loading Nifty 50 dataset...")
    ds = load_nifty_dataset()
    print(f"Dataset: {ds.symbol} {ds.timeframe}, {len(ds.data)} rows")
    print(f"Date range: {ds.data.index[0].date()} to {ds.data.index[-1].date()}\n")

    # === Top strategies from all rounds ===
    top_strategies = [
        # 1. EMA 21-55 (best Sharpe, best Return in Round 1+2+3)
        ("EMA 21-55 cross", "builtin",
         EMATrendStrategy(fast=21, slow=55), 70,
         {"stop_loss_pct": 0.10, "take_profit_pct": 0.20}),
        # 2. EMA 26-52 (2nd best in Round 3)
        ("EMA 26-52 cross", "builtin",
         EMATrendStrategy(fast=26, slow=52), 70,
         {"stop_loss_pct": 0.10, "take_profit_pct": 0.20}),
        # 3. SMA 50-100 (best SMA cross)
        ("SMA 50-100 cross", "spec",
         sma_cross_spec(50, 100, stop=0.10, take=0.20), 120,
         None),
        # 4. EMA 10-30 (best mid-range EMA cross)
        ("EMA 10-30 cross", "builtin",
         EMATrendStrategy(fast=10, slow=30), 40,
         {"stop_loss_pct": 0.10, "take_profit_pct": 0.20}),
        # 5. EMA 15-45
        ("EMA 15-45 cross", "builtin",
         EMATrendStrategy(fast=15, slow=45), 60,
         {"stop_loss_pct": 0.10, "take_profit_pct": 0.20}),
        # 6. EMA 21-55 SL5 TP10 (tight risk)
        ("EMA 21-55 tight SL5 TP10", "builtin",
         EMATrendStrategy(fast=21, slow=55), 70,
         {"stop_loss_pct": 0.05, "take_profit_pct": 0.10}),
        # 7. EMA 21-55 SL10 TP25
        ("EMA 21-55 SL10 TP25", "builtin",
         EMATrendStrategy(fast=21, slow=55), 70,
         {"stop_loss_pct": 0.10, "take_profit_pct": 0.25}),
        # 8. SMA 100 trend filter
        ("SMA 100 trend filter", "spec",
         None, 120, None),  # built below
        # 9. EMA 12-26 long-short
        ("EMA 12-26 long-short", "spec",
         ema_cross_spec(12, 26, stop=0.10, take=0.20, allow_short=True), 40,
         None),
        # 10. EMA 20-55
        ("EMA 20-55 cross", "builtin",
         EMATrendStrategy(fast=20, slow=55), 70,
         {"stop_loss_pct": 0.10, "take_profit_pct": 0.20}),
    ]

    # Build SMA 100 trend filter spec
    top_strategies[7] = ("SMA 100 trend filter", "spec",
        StrategySpec.from_model_json({
            "name": "SMA 100 trend filter",
            "description": "LONG while close > SMA 100; exit below.",
            "symbol": "NSE:NIFTY", "timeframe": "1d",
            "indicators": [{"name": "sma", "params": {"window": 100}}],
            "entry": make_condition(field_operand("close"), ">", indicator_operand("sma_100")),
            "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_100")),
            "position_sizing": {"max_allocation_pct": 0.95},
            "risk": {"stop_loss_pct": 0.05, "take_profit_pct": 0.10},
        }, model="round4-script"), 120, None)

    results = []
    for name, strat_type, strat_or_spec, wb, risk_dict in top_strategies:
        print(f"--- {name} (warmup={wb}) ---", end=" ")
        if strat_type == "builtin":
            perf, result, cfg = run_backtest_builtin(
                ds, strat_or_spec, wb,
                risk_dict.get("stop_loss_pct") if risk_dict else None,
                risk_dict.get("take_profit_pct") if risk_dict else None,
            )
        else:
            perf, result, cfg = run_backtest_spec(ds, strat_or_spec, wb)
        results.append((name, perf, result, cfg))
        print(f"Return={perf.total_return*100:.1f}% Sharpe={perf.sharpe:.2f} "
              f"MaxDD={perf.max_drawdown*100:.1f}% Trades={perf.n_trades} "
              f"Win%={perf.win_rate*100:.0f}% PF={perf.profit_factor or 0:.2f}")

    # Print summary table
    rows = []
    for name, perf, result, cfg in results:
        rows.append([
            name,
            f"{perf.total_return*100:.1f}%",
            f"{perf.sharpe:.2f}" if np.isfinite(perf.sharpe) else "N/A",
            f"{perf.sortino:.2f}" if np.isfinite(perf.sortino) else "N/A",
            f"{perf.max_drawdown*100:.1f}%",
            str(perf.n_trades),
            f"{perf.win_rate*100:.0f}%",
            f"{perf.profit_factor:.2f}" if perf.profit_factor and np.isfinite(perf.profit_factor) else "N/A",
        ])

    headers = ["Strategy", "Return", "Sharpe", "Sortino", "MaxDD", "Trades", "Win%", "PF"]
    print("\n" + "=" * 140)
    print("NIFTY 50 Top 10 Strategies - Detailed Analysis")
    print(f"Capital: Rs.{INITIAL_CAPITAL:,.0f}  Data: {CSV_PATH}")
    print("=" * 140)
    print(tabulate(rows, headers=headers, tablefmt="grid"))
    print("=" * 140)

    # Print ranking
    valid = [(n, p) for n, p, _, _ in results if p is not None]
    print("\nRanking by Sharpe Ratio:")
    for i, (n, p) in enumerate(sorted(valid, key=lambda x: x[1].sharpe, reverse=True), 1):
        print(f"  {i}. {n}: Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
              f"MaxDD={p.max_drawdown*100:.1f}% Trades={p.n_trades} PF={p.profit_factor or 0:.2f}")

    print("\nRanking by Total Return:")
    for i, (n, p) in enumerate(sorted(valid, key=lambda x: x[1].total_return, reverse=True), 1):
        print(f"  {i}. {n}: Return={p.total_return*100:.1f}% "
              f"MaxDD={p.max_drawdown*100:.1f}% Trades={p.n_trades} PF={p.profit_factor or 0:.2f}")

    # Print detailed trade logs for top 5 by Sharpe
    print("\n" + "=" * 140)
    print("DETAILED TRADE LOGS FOR TOP 5 STRATEGIES (by Sharpe)")
    print("=" * 140)
    top5 = sorted(valid, key=lambda x: x[1].sharpe, reverse=True)[:5]
    result_map = {n: r for n, p, r, c in results}
    for n, p in top5:
        print(f"\n{'='*70}")
        print(f"  {n}")
        print(f"  Return: {p.total_return*100:.1f}% | Sharpe: {p.sharpe:.2f} | "
              f"Sortino: {p.sortino:.2f} | MaxDD: {p.max_drawdown*100:.1f}% | "
              f"Trades: {p.n_trades}")
        print(f"  Win: {p.winning} | Lose: {p.losing} | Avg Win: Rs.{p.avg_win:,.0f} | "
              f"Avg Loss: Rs.{p.avg_loss:,.0f} | PF: {p.profit_factor:.2f}")
        print(f"  Best: Rs.{p.largest_win:,.0f} | Worst: Rs.{p.largest_loss:,.0f}")
        print(f"  Exposure: {p.exposure_pct*100:.0f}% | Avg Trade Return: {p.avg_trade_return*100:.2f}%")
        print(f"  Notes: {p.notes}" if p.notes else "")
        print_trade_log(p, result_map[n])


if __name__ == "__main__":
    main()
