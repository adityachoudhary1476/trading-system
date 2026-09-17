#!/usr/bin/env python3
"""Round 3: Optimization phase.

Focus: risk-parameter sweeps on top performers from Round 1+2, plus advanced
strategy variants (Donchian with improved exits, EMA+filter combos, more
SMA/EMA crosses, risk-free vs risk versions).

All strategies use the existing deterministic backtester + IndiaTransactionCostModel.
"""
from __future__ import annotations

import warnings
import numpy as np
import pandas as pd
from trading_system.research.backtester import BacktestConfig, run_backtest
from trading_system.research.costs import IndiaTransactionCostModel, Segment as CostSegment
from trading_system.research.dataset import HistoricalDataset
from trading_system.research.performance import compute_performance
from trading_system.research.strategies import (
    Strategy, Signal, StrategyMeta,
    EMATrendStrategy, MomentumStrategy,
)
from trading_system.research.strategy_lab.spec import (
    StrategySpec, const_operand, field_operand,
    indicator_operand, logic, make_condition,
)
from trading_system.research.strategy_lab.interpreter import build_strategy
from trading_system.research.strategy_lab.engine import merged_backtest_config
from trading_system.research.risk import RiskConfig
from trading_system.indicators import sma, ema, donchian_upper, donchian_lower
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
    print(f"  Loaded {len(df)} rows, {df.index[0].date()} to {df.index[-1].date()}")
    return HistoricalDataset(symbol="NSE:NIFTY", timeframe="1d", data=df, contract_id="NSE:NIFTY")


# --------------------------------------------------------------------------- #
# Custom strategies
# --------------------------------------------------------------------------- #
class TurtleDonchianStrategy(Strategy):
    """Classic Turtle Donchian: entry on N-bar breakout, exit on M-bar reversal.

    LONG when close > N-bar high (shift 1 to avoid lookahead).
    Exit when close < M-bar low (shift 1), where M < N for a tighter exit.
    """

    meta = StrategyMeta("turtle_donchian", "Turtle-style Donchian breakout.")

    def __init__(self, entry_lookback: int = 20, exit_lookback: int = 10):
        self.entry_lookback = entry_lookback
        self.exit_lookback = exit_lookback

    @property
    def params(self):
        return {"entry_lookback": self.entry_lookback, "exit_lookback": self.exit_lookback}

    def generate(self, df):
        close = df["close"]
        n, m = self.entry_lookback, self.exit_lookback
        entry_high = close.shift(1).rolling(window=n, min_periods=n).max()
        exit_low = close.shift(1).rolling(window=m, min_periods=m).min()
        target = pd.Series(0, index=df.index, dtype=int)
        target[close > entry_high] = Signal.LONG
        target[close < exit_low] = Signal.FLAT
        return target


class DonchianWithMAPositionExitStrategy(Strategy):
    """Donchian breakout with SMA-based position exit.

    LONG when close > Donchian upper(N).
    Exit when close < SMA(M) (moving average as trailing exit).
    """

    meta = StrategyMeta("donchian_ma_exit", "Donchian entry with SMA trailing exit.")

    def __init__(self, entry_lookback: int = 20, sma_exit: int = 10):
        self.entry_lookback = entry_lookback
        self.sma_exit = sma_exit

    @property
    def params(self):
        return {"entry_lookback": self.entry_lookback, "sma_exit": self.sma_exit}

    def generate(self, df):
        close = df["close"]
        n = self.entry_lookback
        chan_high = close.shift(1).rolling(window=n, min_periods=n).max()
        sma_exit = sma(close, self.sma_exit)
        target = pd.Series(0, index=df.index, dtype=int)
        target[close > chan_high] = Signal.LONG
        target[close < sma_exit] = Signal.FLAT
        return target


class EMAFilterStrategy(Strategy):
    """EMA cross with trend filter: only trade in the direction of the longer trend.

    LONG when EMA(fast) > EMA(slow) AND price > EMA(filter).
    Exit when EMA fast < EMA slow.
    """

    meta = StrategyMeta("ema_filter", "EMA cross with EMA filter confirmation.")

    def __init__(self, fast: int = 13, slow: int = 34, filter_ema: int = 200):
        self.fast = fast
        self.slow = slow
        self.filter_ema = filter_ema

    @property
    def params(self):
        return {"fast": self.fast, "slow": self.slow, "filter_ema": self.filter_ema}

    def generate(self, df):
        close = df["close"]
        fe = close.ewm(span=self.fast, adjust=False, min_periods=1).mean()
        se = close.ewm(span=self.slow, adjust=False, min_periods=1).mean()
        filter_val = close.ewm(span=self.filter_ema, adjust=False, min_periods=1).mean()
        target = pd.Series(0, index=df.index, dtype=int)
        long_cond = (fe > se) & (close > filter_val)
        target[long_cond] = Signal.LONG
        target[~(long_cond & (fe > se))] = Signal.FLAT
        # Only exit to flat when condition breaks, not flip to short
        # Reset: use a stateful approach
        target = pd.Series(0, index=df.index, dtype=int)
        in_long = False
        for i in range(len(df)):
            if not in_long:
                if long_cond.iloc[i]:
                    target.iloc[i] = Signal.LONG
                    in_long = True
            else:
                if not long_cond.iloc[i] or (fe.iloc[i] < se.iloc[i]):
                    target.iloc[i] = Signal.FLAT
                    in_long = False
                else:
                    target.iloc[i] = Signal.LONG
        return target


class MACDHistogramStrategy(Strategy):
    """MACD histogram momentum strategy.

    LONG when MACD histogram crosses above 0; exit when it crosses below 0.
    The histogram crossing zero is equivalent to MACD crossing the signal line,
    but can also be used for magnitude-based signals.
    """

    meta = StrategyMeta("macd_hist", "MACD histogram zero-crossing strategy.")

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self.fast = fast
        self.slow = slow
        self.signal = signal

    @property
    def params(self):
        return {"fast": self.fast, "slow": self.slow, "signal": self.signal}

    def generate(self, df):
        close = df["close"]
        macd_line = ema(close, self.fast) - ema(close, self.slow)
        signal_line = macd_line.ewm(span=self.signal, adjust=False, min_periods=self.signal).mean()
        hist = macd_line - signal_line
        target = pd.Series(0, index=df.index, dtype=int)
        target[hist > 0] = Signal.LONG
        target[hist <= 0] = Signal.FLAT
        return target


class EMADualMomentumStrategy(Strategy):
    """Dual momentum: EMA cross + absolute momentum filter.

    LONG when EMA(fast) > EMA(slow) AND close > EMA(close, lookback) (absolute momentum).
    """

    meta = StrategyMeta("ema_dual_mom", "EMA cross + absolute momentum filter.")

    def __init__(self, fast: int = 21, slow: int = 55, abs_mom_lookback: int = 200):
        self.fast = fast
        self.slow = slow
        self.abs_mom_lookback = abs_mom_lookback

    @property
    def params(self):
        return {"fast": self.fast, "slow": self.slow, "abs_mom_lookback": self.abs_mom_lookback}

    def generate(self, df):
        close = df["close"]
        fe = close.ewm(span=self.fast, adjust=False, min_periods=1).mean()
        se = close.ewm(span=self.slow, adjust=False, min_periods=1).mean()
        long_ma = close.ewm(span=self.abs_mom_lookback, adjust=False, min_periods=1).mean()
        target = pd.Series(0, index=df.index, dtype=int)
        target[(fe > se) & (close > long_ma)] = Signal.LONG
        target[(fe <= se) | (close < long_ma)] = Signal.FLAT
        return target


# --------------------------------------------------------------------------- #
# StrategySpec builders
# --------------------------------------------------------------------------- #
def ema_cross_spec(fast, slow, stop=None, take=None, allow_short=False):
    fk, sk = f"ema_{fast}", f"ema_{slow}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    payload = {
        "name": f"EMA {fast}-{slow} Spec",
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
    return StrategySpec.from_model_json(payload, model="round3-script")


def ema_filter_spec(fast, slow, filter_ema, stop=None, take=None):
    fk, sk, fak = f"ema_{fast}", f"ema_{slow}", f"ema_{filter_ema}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"EMA {fast}-{slow} filter by EMA {filter_ema}",
        "description": f"LONG when EMA({fast}) > EMA({slow}) AND close > EMA({filter_ema}).",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [
            {"name": "ema", "params": {"window": fast}},
            {"name": "ema", "params": {"window": slow}},
            {"name": "ema", "params": {"window": filter_ema}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand(fk), "crosses_above", indicator_operand(sk)),
            make_condition(field_operand("close"), ">", indicator_operand(fak)),
        ),
        "exit": make_condition(indicator_operand(fk), "crosses_below", indicator_operand(sk)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round3-script")


def donchian_spec(entry_lookback, exit_lookback, stop=None, take=None):
    du_k = f"donchian_upper_{entry_lookback}"
    dl_k = f"donchian_lower_{exit_lookback}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"Donchian {entry_lookback} entry {exit_lookback} exit",
        "description": f"LONG when close > Donchian upper({entry_lookback}); exit when close < Donchian lower({exit_lookback}).",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [
            {"name": "donchian_upper", "params": {"window": entry_lookback}},
            {"name": "donchian_lower", "params": {"window": exit_lookback}},
        ],
        "entry": make_condition(field_operand("close"), ">", indicator_operand(du_k)),
        "exit": make_condition(field_operand("close"), "<", indicator_operand(dl_k)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round3-script")


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
    }, model="round3-script")


def momentum_spec(window, entry_thr, exit_thr, stop=None, take=None):
    mk = f"momentum_{window}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"Momentum {window} entry {entry_thr*100:.0f}pct",
        "description": f"LONG when momentum({window}) > {entry_thr}; exit when < {exit_thr}.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [{"name": "momentum", "params": {"window": window}}],
        "entry": make_condition(indicator_operand(mk), ">", const_operand(entry_thr)),
        "exit": make_condition(indicator_operand(mk), "<", const_operand(exit_thr)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round3-script")


def run_strategy(dataset, name, strat_or_spec, is_spec, warmup_bars=50, risk_dict=None):
    cost_model = IndiaTransactionCostModel()
    seg = CostSegment.EQUITY_FUTURE

    if is_spec:
        base = BacktestConfig(
            initial_capital=INITIAL_CAPITAL,
            slippage_pct=0.001,
            cost_model=cost_model,
            cost_segment=seg.value,
            warmup_bars=warmup_bars,
        )
        cfg = merged_backtest_config(strat_or_spec, base)
        strategy = build_strategy(strat_or_spec)
    else:
        risk_cfg = RiskConfig(
            max_allocation_pct=risk_dict.get("max_allocation_pct", 0.95) if risk_dict else 0.95,
            max_position_size=risk_dict.get("max_position_size") if risk_dict else None,
            allow_short=risk_dict.get("allow_short", False) if risk_dict else False,
            stop_loss_pct=risk_dict.get("stop_loss_pct") if risk_dict else None,
            take_profit_pct=risk_dict.get("take_profit_pct") if risk_dict else None,
        ) if risk_dict else None
        cfg = BacktestConfig(
            initial_capital=INITIAL_CAPITAL,
            slippage_pct=0.001,
            cost_model=cost_model,
            cost_segment=seg.value,
            warmup_bars=warmup_bars,
            risk=risk_cfg,
        )
        strategy = strat_or_spec

    try:
        result = run_backtest(dataset, strategy, cfg)
        perf = compute_performance(result)
        return (name, perf, result, cfg)
    except Exception as e:
        print(f"ERROR {name}: {e}")
        return (name, None, None, cfg)


def run_all_backtests(dataset):
    """Round 3: optimization and parameter sweeps."""
    configs = []

    # === EMA cross risk parameter sweep (top performer: EMA 21-55) ===
    for stop, take in [(0.05, 0.10), (0.05, 0.15), (0.05, 0.20),
                       (0.07, 0.15), (0.07, 0.20), (0.10, 0.15), (0.10, 0.25),
                       (0.12, 0.30), (0.15, 0.30), (0.08, 0.20)]:
        configs.append((
            f"EMA 21-55 SL{stop*100:.0f} TP{take*100:.0f}%",
            ema_cross_spec(21, 55, stop=stop, take=take),
            True, 70,
            None,
        ))

    # === EMA 26-52 risk parameter sweep ===
    for stop, take in [(0.08, 0.15), (0.10, 0.20), (0.12, 0.25)]:
        configs.append((
            f"EMA 26-52 SL{stop*100:.0f} TP{take*100:.0f}%",
            ema_cross_spec(26, 52, stop=stop, take=take),
            True, 70,
            None,
        ))

    # === EMA 10-30 risk parameter sweep ===
    for stop, take in [(0.05, 0.10), (0.08, 0.15), (0.10, 0.20)]:
        configs.append((
            f"EMA 10-30 SL{stop*100:.0f} TP{take*100:.0f}%",
            ema_cross_spec(10, 30, stop=stop, take=take),
            True, 45,
            None,
        ))

    # === EMA 15-45 risk parameter sweep ===
    for stop, take in [(0.08, 0.20), (0.10, 0.20), (0.10, 0.30)]:
        configs.append((
            f"EMA 15-45 SL{stop*100:.0f} TP{take*100:.0f}%",
            ema_cross_spec(15, 45, stop=stop, take=take),
            True, 60,
            None,
        ))

    # === EMA cross: additional parameter combinations ===
    for fast, slow in [(10, 50), (15, 50), (20, 55), (20, 65), (30, 80),
                       (30, 100), (40, 100), (50, 120), (5, 34), (10, 45),
                       (15, 65), (25, 75), (35, 100), (45, 120)]:
        configs.append((
            f"EMA {fast}-{slow} cross built-in (SL10 TP20)",
            EMATrendStrategy(fast=fast, slow=slow),
            False, max(fast, slow) + 10,
            {"stop_loss_pct": 0.10, "take_profit_pct": 0.20},
        ))

    # === EMA long-short variations ===
    for fast, slow in [(12, 26), (8, 21), (21, 55), (10, 30)]:
        configs.append((
            f"EMA {fast}-{slow} long-short Spec (SL10 TP20)",
            ema_cross_spec(fast, slow, stop=0.10, take=0.20, allow_short=True),
            True, max(fast, slow) + 10,
            None,
        ))

    # === EMA with filter (EMA cross + long-term EMA filter) ===
    for fast, slow, filt in [(13, 34, 100), (13, 34, 200), (21, 55, 200),
                             (8, 21, 100), (8, 21, 200), (10, 30, 200)]:
        configs.append((
            f"EMA {fast}-{slow} + filter EMA {filt} (SL8 TP15)",
            ema_filter_spec(fast, slow, filt, stop=0.08, take=0.15),
            True, max(slow, filt) + 10,
            None,
        ))

    # === EMA dual momentum ===
    for fast, slow, lb in [(21, 55, 200), (13, 34, 200), (21, 55, 100),
                           (34, 89, 200), (10, 30, 200)]:
        configs.append((
            f"EMA {fast}-{slow} dual mom lb{lb} (SL8 TP15)",
            EMADualMomentumStrategy(fast=fast, slow=slow, abs_mom_lookback=lb),
            False, max(slow, lb) + 10,
            {"stop_loss_pct": 0.08, "take_profit_pct": 0.15},
        ))

    # === SMA cross additional combos ===
    for fast, slow in [(3, 10), (5, 34), (5, 65), (10, 65), (20, 65),
                       (15, 100), (30, 100), (30, 200), (40, 150), (60, 200)]:
        configs.append((
            f"SMA {fast}-{slow} cross (SL10 TP20)",
            sma_cross_spec(fast, slow, stop=0.10, take=0.20),
            True, max(slow, fast) + 10,
            None,
        ))

    # === SMA cross with tighter risk ===
    for fast, slow in [(20, 50), (20, 100), (50, 100), (20, 200)]:
        configs.append((
            f"SMA {fast}-{slow} cross tight SL5 TP10",
            sma_cross_spec(fast, slow, stop=0.05, take=0.10),
            True, max(slow, fast) + 10,
            None,
        ))

    # === Donchian with Turtle-style exit ===
    for entry_lb, exit_lb in [(20, 10), (20, 5), (20, 15), (55, 20), (55, 10),
                              (10, 5), (30, 10), (50, 20), (20, 20), (30, 15)]:
        configs.append((
            f"Turtle Donchian {entry_lb}/{exit_lb} (SL8 TP15)",
            TurtleDonchianStrategy(entry_lookback=entry_lb, exit_lookback=exit_lb),
            False, entry_lb + 5,
            {"stop_loss_pct": 0.08, "take_profit_pct": 0.15},
        ))

    # === Donchian spec with stop-loss ===
    for entry_lb, exit_lb in [(20, 10), (55, 20), (20, 5)]:
        configs.append((
            f"Donchian {entry_lb}/{exit_lb} Spec (SL8 TP15)",
            donchian_spec(entry_lb, exit_lb, stop=0.08, take=0.15),
            True, entry_lb + 10,
            None,
        ))

    # === Donchian with SMA trailing exit ===
    for entry_lb, sma_exit in [(20, 10), (20, 20), (55, 20), (55, 50),
                               (20, 50), (30, 20)]:
        configs.append((
            f"Donchian {entry_lb} SMA{sma_exit} exit (SL8 TP15)",
            DonchianWithMAPositionExitStrategy(entry_lookback=entry_lb, sma_exit=sma_exit),
            False, max(entry_lb, sma_exit) + 5,
            {"stop_loss_pct": 0.08, "take_profit_pct": 0.15},
        ))

    # === Momentum parameter sweep ===
    for window, thr, exit_thr in [(10, 0.0, -0.0), (12, 0.0, -0.0), (20, 0.0, -0.0),
                                   (30, 0.0, -0.0), (30, 0.005, -0.0),
                                   (10, 0.02, 0.0), (20, 0.02, 0.0), (20, 0.03, 0.0),
                                   (60, 0.0, -0.0), (60, 0.01, -0.0)]:
        wb = window + 5
        configs.append((
            f"Momentum {window} thr{thr*100:.0f}pct exit{exit_thr*100:.0f}pct (SL8 TP12)",
            momentum_spec(window, thr, exit_thr, stop=0.08, take=0.12),
            True, wb,
            None,
        ))

    # === Momentum built-in variations ===
    for window, entry_thr in [(10, 0.0), (12, 0.0), (20, 0.0), (30, 0.0),
                               (60, 0.0), (60, 0.01)]:
        configs.append((
            f"Momentum built-in {window} thr{entry_thr*100:.0f}pct (SL8 TP12)",
            MomentumStrategy(window=window, entry_thr=entry_thr, exit_thr=-0.01),
            False, window + 5,
            {"stop_loss_pct": 0.08, "take_profit_pct": 0.12},
        ))

    # === MACD histogram strategy ===
    configs.append((
        "MACD histogram zero-cross (SL5 TP10)",
        MACDHistogramStrategy(),
        False, 40,
        {"stop_loss_pct": 0.05, "take_profit_pct": 0.10},
    ))

    # === EMA cross with tighter risk ===
    for fast, slow in [(8, 21), (13, 34), (21, 55), (10, 30)]:
        configs.append((
            f"EMA {fast}-{slow} cross tight SL5 TP10",
            EMATrendStrategy(fast=fast, slow=slow),
            False, slow + 10,
            {"stop_loss_pct": 0.05, "take_profit_pct": 0.10},
        ))

    # === EMA cross with looser risk ===
    for fast, slow in [(8, 21), (13, 34), (21, 55), (10, 30)]:
        configs.append((
            f"EMA {fast}-{slow} cross loose SL15 TP30",
            EMATrendStrategy(fast=fast, slow=slow),
            False, slow + 10,
            {"stop_loss_pct": 0.15, "take_profit_pct": 0.30},
        ))

    # === No-risk baselines (for comparison) ===
    for fast, slow in [(21, 55), (26, 52), (10, 30), (15, 45)]:
        configs.append((
            f"EMA {fast}-{slow} cross no-risk (baseline)",
            EMATrendStrategy(fast=fast, slow=slow),
            False, slow + 10,
            None,
        ))

    results = []
    for name, strat, is_spec, wb, risk_dict in configs:
        print(f"--- {name} (warmup={wb}) ---", end=" ")
        r = run_strategy(dataset, name, strat, is_spec, wb, risk_dict)
        if r[1] is not None:
            p = r[1]
            print(f"Return={p.total_return*100:.1f}% Sharpe={p.sharpe:.2f} "
                  f"MaxDD={p.max_drawdown*100:.1f}% Trades={p.n_trades} "
                  f"Win%={p.win_rate*100:.0f}% PF={p.profit_factor or 0:.2f}")
        else:
            print(f"ERROR")
        results.append(r)

    return results


def print_summary(results):
    rows = []
    for name, perf, result, cfg in results:
        if perf is None:
            rows.append([name, "ERR", "ERR", "ERR", "ERR", "ERR", "ERR", "ERR"])
        else:
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
    print("NIFTY 50 Round 3 - Optimization & Parameter Sweeps")
    print(f"Capital: Rs.{INITIAL_CAPITAL:,.0f}  Data: {CSV_PATH}  Strategies: {len(results)}")
    print("=" * 140)
    print(tabulate(rows, headers=headers, tablefmt="grid"))
    print("=" * 140)

    valid = [(n, p) for n, p, _, _ in results if p is not None]
    if valid:
        print("\nRanking by Sharpe Ratio (best risk-adjusted):")
        for i, (n, p) in enumerate(sorted(valid, key=lambda x: x[1].sharpe, reverse=True), 1):
            print(f"  {i}. {n}: Sharpe={p.sharpe:.2f} Return={p.total_return*100:.1f}% "
                  f"MaxDD={p.max_drawdown*100:.1f}% Trades={p.n_trades} PF={p.profit_factor or 0:.2f}")

        print("\nRanking by Total Return:")
        for i, (n, p) in enumerate(sorted(valid, key=lambda x: x[1].total_return, reverse=True), 1):
            print(f"  {i}. {n}: Return={p.total_return*100:.1f}% "
                  f"MaxDD={p.max_drawdown*100:.1f}% Trades={p.n_trades} PF={p.profit_factor or 0:.2f}")

        print(f"\nProfitable: {sum(1 for _,p in valid if p.total_return > 0)}/{len(valid)}  "
              f"Unprofitable: {sum(1 for _,p in valid if p.total_return <= 0)}/{len(valid)}")


if __name__ == "__main__":
    print("Loading Nifty 50 dataset...")
    ds = load_nifty_dataset()
    print(f"\nDataset: {ds.symbol} {ds.timeframe}, {len(ds.data)} rows")
    print(f"Date range: {ds.data.index[0].date()} to {ds.data.index[-1].date()}")
    print("\nRunning Round 3 backtests...\n")
    results = run_all_backtests(ds)
    print_summary(results)
