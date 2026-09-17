#!/usr/bin/env python3
"""Round 17: Portfolio approach — 5 individual NIFTY 50 stocks.

Individual stocks have higher volatility (1.3-1.6% daily std) vs the index (1.3%).
With 5 stocks each trading ~0.3-0.5x/week, the portfolio should reach ~2-3 trades/day.
Tests EMA crosses, Donchian breakouts, and RSI reversals on each stock separately,
then aggregates results.
"""
from __future__ import annotations

import warnings
import numpy as np
import pandas as pd
import yfinance as yf
from trading_system.research.backtester import BacktestConfig, run_backtest
from trading_system.research.costs import IndiaTransactionCostModel, Segment as CostSegment
from trading_system.research.dataset import HistoricalDataset
from trading_system.research.performance import compute_performance
from trading_system.research.strategy_lab.spec import (
    StrategySpec, const_operand, field_operand,
    indicator_operand, logic, make_condition,
)
from trading_system.research.strategy_lab.interpreter import build_strategy
from trading_system.research.strategy_lab.engine import merged_backtest_config
from trading_system.research.risk import RiskConfig
from tabulate import tabulate

warnings.filterwarnings("ignore", category=FutureWarning)

INITIAL_CAPITAL = 20_000.0  # 20k per stock in a 100k portfolio
WEEKS_PER_YEAR = 252 / 5

STOCKS = {
    "RELIANCE": "RELIANCE.NS",
    "TCS": "TCS.NS",
    "INFY": "INFY.NS",
    "HDFCBANK": "HDFCBANK.NS",
    "SBIN": "SBIN.NS",
    "ICICIBANK": "ICICIBANK.NS",
    "HINDUNILVR": "HINDUNILVR.NS",
    "BHARTIARTL": "BHARTIARTL.NS",
    "KOTAKBANK": "KOTAKBANK.NS",
    "AXISBANK": "AXISBANK.NS",
}


def download_stock(ticker):
    df = yf.download(ticker, period="2y", interval="1d", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if len(df) == 0:
        return None
    df.index = df.index.tz_localize("UTC")
    df = df[["Open", "High", "Low", "Close", "Volume"]].rename(
        columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}
    )
    return df


def download_5min_stock(ticker):
    df = yf.download(ticker, period="60d", interval="5m", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if len(df) == 0:
        return None
    # Filter to market hours only (9:15-15:30 IST = 03:45-10:00 UTC)
    df = df.between_time("03:45", "10:00", include_end=False)
    if len(df) == 0:
        return None
    df.index = df.index.tz_convert("UTC")
    df = df[["Open", "High", "Low", "Close", "Volume"]].rename(
        columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}
    )
    return df


def make_risk_config(stop_loss_pct=None, take_profit_pct=None, max_alloc=0.95):
    return RiskConfig(max_allocation_pct=max_alloc, allow_short=False,
                       stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct)


def run_spec(dataset, spec, wb):
    base = BacktestConfig(initial_capital=INITIAL_CAPITAL, slippage_pct=0.001,
                          cost_model=IndiaTransactionCostModel(), cost_segment=CostSegment.EQUITY_FUTURE.value,
                          warmup_bars=wb)
    cfg = merged_backtest_config(spec, base)
    strategy = build_strategy(spec)
    result = run_backtest(dataset, strategy, cfg)
    perf = compute_performance(result)
    return perf, result, cfg


def ema_cross_spec(name, fast, slow, stop=None, take=None, timeframe="1d"):
    fk, sk = f"ema_{fast}", f"ema_{slow}"
    risk = {}
    if stop: risk["stop_loss_pct"] = stop
    if take: risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": name, "symbol": "NSE:NIFTY", "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": fast}},
            {"name": "ema", "params": {"window": slow}},
        ],
        "entry": make_condition(indicator_operand(fk), "crosses_above", indicator_operand(sk)),
        "exit": make_condition(indicator_operand(fk), "crosses_below", indicator_operand(sk)),
        "position_sizing": {"max_allocation_pct": 0.95}, "risk": risk,
    }, model="round17-script")


def ema_cross_trend_filtered_spec(name, fast, slow, long_ema, stop=None, take=None, timeframe="1d"):
    ema_k = f"ema_{long_ema}"
    risk = {}
    if stop: risk["stop_loss_pct"] = stop
    if take: risk["take_profit_pct"] = take
    inds = [
        {"name": "ema", "params": {"window": fast}},
        {"name": "ema", "params": {"window": slow}},
        {"name": "ema", "params": {"window": long_ema}},
    ]
    entry = logic("AND",
        make_condition(indicator_operand(f"ema_{fast}"), "crosses_above", indicator_operand(f"ema_{slow}")),
        make_condition(field_operand("close"), ">", indicator_operand(ema_k)),
    )
    exit_cond = make_condition(indicator_operand(f"ema_{fast}"), "crosses_below", indicator_operand(f"ema_{slow}"))
    return StrategySpec.from_model_json({
        "name": name, "symbol": "NSE:NIFTY", "timeframe": timeframe,
        "indicators": inds, "entry": entry, "exit": exit_cond,
        "position_sizing": {"max_allocation_pct": 0.95}, "risk": risk,
    }, model="round17-script")


def calc_trades_per_week(result):
    total_bars = len(result.equity_curve)
    weeks = total_bars / 5  # daily: 5 bars per week
    return len([t for t in result.trades]) / weeks if weeks > 0 else 0


# Strategy configs to test on each stock
STRATEGY_CONFIGS = [
    # Fast EMA crosses (baseline)
    ("EMA 2-3", 2, 3, None, None, 7),
    ("EMA 2-4", 2, 4, None, None, 8),
    ("EMA 2-5", 2, 5, None, None, 9),
    ("EMA 3-5", 3, 5, None, None, 9),
    ("EMA 3-8", 3, 8, None, None, 12),
    ("EMA 5-10", 5, 10, None, None, 14),
    ("EMA 5-13", 5, 13, None, None, 17),
    # EMA crosses with SL/TP
    ("EMA 3-5 SL3TP5", 3, 5, 0.03, 0.05, 9),
    ("EMA 3-5 SL4TP6", 3, 5, 0.04, 0.06, 9),
    ("EMA 5-10 SL3TP5", 5, 10, 0.03, 0.05, 14),
    ("EMA 5-10 SL4TP6", 5, 10, 0.04, 0.06, 14),
    ("EMA 5-13 SL3TP5", 5, 13, 0.03, 0.05, 17),
    ("EMA 5-13 SL4TP6", 5, 13, 0.04, 0.06, 17),
    # EMA crosses with trend filter
    ("EMA 3-5 Trend200 SL3TP5", 3, 5, 0.03, 0.05, 205),
    ("EMA 3-5 Trend200 SL4TP6", 3, 5, 0.04, 0.06, 205),
    ("EMA 5-10 Trend200 SL3TP5", 5, 10, 0.03, 0.05, 215),
    ("EMA 5-10 Trend200 SL4TP6", 5, 10, 0.04, 0.06, 215),
    ("EMA 5-13 Trend200 SL3TP5", 5, 13, 0.03, 0.05, 217),
    ("EMA 5-13 Trend200 SL4TP6", 5, 13, 0.04, 0.06, 217),
    # EMA crosses with volatility filter
    ("EMA 3-5 VolATR SL4TP6", 3, 5, 0.04, 0.06, 59),
    ("EMA 5-10 VolATR SL4TP6", 5, 10, 0.04, 0.06, 65),
    ("EMA 5-13 VolATR SL4TP6", 5, 13, 0.04, 0.06, 67),
    # EMA crosses with trend + vol filter
    ("EMA 3-5 TrendVol SL4TP6", 3, 5, 0.04, 0.06, 67),
    ("EMA 5-10 TrendVol SL4TP6", 5, 10, 0.04, 0.06, 73),
    ("EMA 5-13 TrendVol SL4TP6", 5, 13, 0.04, 0.06, 75),
]

# For trend+vol filtered versions, need custom spec builder
def get_spec(config, symbol="NSE:NIFTY"):
    name, fast, slow, stop, take, wb = config
    has_trend = "Trend200" in name
    has_vol = "VolATR" in name
    has_all = "TrendVol" in name

    if has_trend:
        return ema_cross_trend_filtered_spec(name, fast, slow, 200, stop=stop, take=take), wb
    elif has_all:
        ema_k = "ema_200"
        inds = [
            {"name": "ema", "params": {"window": fast}},
            {"name": "ema", "params": {"window": slow}},
            {"name": "ema", "params": {"window": 200}},
            {"name": "atr", "params": {"window": 14}},
            {"name": "atr", "params": {"window": 55}},
        ]
        entry = logic("AND",
            make_condition(indicator_operand(f"ema_{fast}"), "crosses_above", indicator_operand(f"ema_{slow}")),
            make_condition(field_operand("close"), ">", indicator_operand(ema_k)),
            make_condition(indicator_operand("atr_14"), ">", indicator_operand("atr_55")),
        )
        exit_cond = make_condition(indicator_operand(f"ema_{fast}"), "crosses_below", indicator_operand(f"ema_{slow}"))
        risk = {}
        if stop: risk["stop_loss_pct"] = stop
        if take: risk["take_profit_pct"] = take
        return StrategySpec.from_model_json({
            "name": name, "symbol": symbol, "timeframe": "1d",
            "indicators": inds, "entry": entry, "exit": exit_cond,
            "position_sizing": {"max_allocation_pct": 0.95}, "risk": risk,
        }, model="round17-script"), wb
    elif has_vol:
        inds = [
            {"name": "ema", "params": {"window": fast}},
            {"name": "ema", "params": {"window": slow}},
            {"name": "atr", "params": {"window": 14}},
            {"name": "atr", "params": {"window": 55}},
        ]
        entry = logic("AND",
            make_condition(indicator_operand(f"ema_{fast}"), "crosses_above", indicator_operand(f"ema_{slow}")),
            make_condition(indicator_operand("atr_14"), ">", indicator_operand("atr_55")),
        )
        exit_cond = make_condition(indicator_operand(f"ema_{fast}"), "crosses_below", indicator_operand(f"ema_{slow}"))
        risk = {}
        if stop: risk["stop_loss_pct"] = stop
        if take: risk["take_profit_pct"] = take
        return StrategySpec.from_model_json({
            "name": name, "symbol": symbol, "timeframe": "1d",
            "indicators": inds, "entry": entry, "exit": exit_cond,
            "position_sizing": {"max_allocation_pct": 0.95}, "risk": risk,
        }, model="round17-script"), wb
    else:
        return ema_cross_spec(name, fast, slow, stop=stop, take=take, timeframe="1d"), wb


def run_backtests_per_stock(symbol, df):
    """Run all strategy configs on one stock."""
    results = []
    ds = HistoricalDataset(symbol=symbol, timeframe="1d", data=df, contract_id=symbol)
    for config in STRATEGY_CONFIGS:
        spec, wb = get_spec(config)
        try:
            perf, result, cfg = run_spec(ds, spec, wb)
            if perf is not None and perf.n_trades > 0:
                tw = calc_trades_per_week(result)
                results.append({
                    "stock": symbol,
                    "strategy": config[0],
                    "return": perf.total_return,
                    "sharpe": perf.sharpe,
                    "trades": perf.n_trades,
                    "trades_per_week": tw,
                    "win_rate": perf.win_rate,
                    "pf": perf.profit_factor,
                    "max_dd": perf.max_drawdown,
                })
        except Exception as e:
            pass
    return results


def aggregate_portfolio(all_results):
    """Aggregate results across stocks into portfolio-level metrics."""
    by_strategy = {}
    for r in all_results:
        s = r["strategy"]
        if s not in by_strategy:
            by_strategy[s] = {"total_trades": 0, "total_return": 0, "n_stocks": 0,
                              "profitable": 0, "sum_tw": 0}
        by_strategy[s]["total_trades"] += r["trades"]
        by_strategy[s]["sum_tw"] += r["trades_per_week"]
        by_strategy[s]["total_return"] += r["return"]
        by_strategy[s]["n_stocks"] += 1
        if r["return"] > 0:
            by_strategy[s]["profitable"] += 1

    rows = []
    for s, agg in by_strategy.items():
        avg_return = agg["total_return"] / agg["n_stocks"]  # simple average
        avg_tw = agg["sum_tw"] / agg["n_stocks"]  # average trades/week per stock
        portfolio_tw = agg["sum_tw"]  # total trades/week across portfolio
        daily_tw = avg_tw / 5  # trades per day (per stock avg)
        portfolio_daily = portfolio_tw / 5  # total trades/day across portfolio
        rows.append([s, f"{avg_return*100:.1f}%", f"{avg_tw:.1f}", f"{portfolio_tw:.1f}",
                     f"{portfolio_daily:.1f}", f"{agg['profitable']}/{agg['n_stocks']}"])
    return rows


if __name__ == "__main__":
    print("Downloading NIFTY 50 stock data...\n")
    stocks_data = {}
    for name, ticker in STOCKS.items():
        df = download_stock(ticker)
        if df is not None:
            stocks_data[name] = df
            srets = df["close"].pct_change().dropna()
            print(f"  {name}: {len(df)} rows, {df.index[0].date()} to {df.index[-1].date()}, "
                  f"std={srets.std()*100:.2f}%")
        else:
            print(f"  {name}: FAILED to download")

    if not stocks_data:
        print("No data downloaded!")
        exit(1)

    print(f"\n{'='*120}")
    print(f"Testing {len(STRATEGY_CONFIGS)} strategies on {len(stocks_data)} stocks ({len(stocks_data)*len(STRATEGY_CONFIGS)} total runs)")
    print(f"{'='*120}\n")

    all_results = []
    for name, df in stocks_data.items():
        print(f"--- Testing {name} ---")
        results = run_backtests_per_stock(name, df)
        for r in results:
            print(f"  {r['strategy']:45s} Ret={r['return']*100:6.1f}% "
                  f"TW={r['trades_per_week']:.1f} Trades={r['trades']:4d} "
                  f"Sharpe={r['sharpe']:.2f} PF={r['pf'] or 0:.2f}")
            all_results.append(r)

    print(f"\n{'='*120}")
    print("PORTFOLIO AGGREGATION")
    print(f"{'='*120}")

    portfolio_rows = aggregate_portfolio(all_results)
    headers = ["Strategy", "AvgReturn", "AvgTw/Wk", "PortfolioTw/Wk", "PortfolioTw/Day", "Profitable/N"]
    print(tabulate(portfolio_rows, headers=headers, tablefmt="grid"))

    print(f"\n{'='*120}")
    print("STRATEGIES CLOSING TO 2-3 TRADES/DAY (portfolio-level)")
    print(f"{'='*120}")

    target = [r for r in portfolio_rows if 2.0 <= float(r[4]) <= 3.0]
    target.sort(key=lambda x: float(x[1].rstrip('%')), reverse=True)
    if target:
        for r in target[:10]:
            print(f"  {r[0]:45s} AvgRet={r[1]} AvgTw={r[2]}/wk PortTw={r[3]}/wk PortTw={r[4]}/day Prof={r[5]}")
    else:
        print("  None at exactly 2-3/day. Closest:")
        portfolio_rows.sort(key=lambda x: float(x[4]), reverse=True)
        for r in portfolio_rows[:15]:
            print(f"  {r[0]:45s} AvgRet={r[1]} AvgTw={r[2]}/wk PortTw={r[3]}/wk PortTw={r[4]}/day Prof={r[5]}")

    print(f"\n{'='*120}")
    print("TOP PROFITABLE STRATEGIES (by avg return)")
    print(f"{'='*120}")
    profitable = [(r[0], float(r[1].rstrip('%')), float(r[2]), float(r[3]), float(r[4]), r[5])
                  for r in portfolio_rows if float(r[1].rstrip('%')) > 0]
    profitable.sort(key=lambda x: x[1], reverse=True)
    for name, ret, avg_tw, port_tw, port_daily, prof in profitable[:20]:
        print(f"  {name:45s} AvgRet={ret:.1f}% AvgTw={avg_tw:.1f}/wk PortTw={port_tw:.1f}/wk PortTw={port_daily:.1f}/day Prof={prof}")

    print(f"\nTotal: {len(all_results)} stock-strategy combos, {len(profitable)} profitable strategies")
