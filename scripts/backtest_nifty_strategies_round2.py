#!/usr/bin/env python3
"""Round 2: Comprehensive Nifty 50 single-leg strategy backtest.

Expands on the original 11 strategies with:
  - Parameter sweeps of winning strategies (EMA, Supertrend, SMA, RSI)
  - Risk parameters (stop_loss/take_profit) applied to ALL strategies
  - New strategy types: Donchian, volume, multi-factor, Bollinger variants
  - EMA cross with short entries
  - ATR-based exit strategies

All strategies tested on the same dataset (4536 rows, 2007-2026) with the
existing deterministic backtester and IndiaTransactionCostModel.
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
    EMATrendStrategy, MomentumStrategy, BreakoutStrategy,
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
    print(f"  Loaded {len(df)} rows, {df.index[0].date()} to {df.index[-1].date()}")
    return HistoricalDataset(symbol="NSE:NIFTY", timeframe="1d", data=df, contract_id="NSE:NIFTY")


# --------------------------------------------------------------------------- #
# Custom strategies
# --------------------------------------------------------------------------- #
class SupertrendStrategy(Strategy):
    """Supertrend(ATR multiplier) trend-following: LONG above, FLAT below.

    Uses a close-and-reverse behavior: when the trend flips, position reverses.
    """

    meta = StrategyMeta(
        "supertrend", "Supertrend trend-following: LONG above, FLAT below."
    )

    def __init__(self, atr_period=10, multiplier=3.0, allow_short=False):
        self.atr_period = atr_period
        self.multiplier = multiplier
        self.allow_short = allow_short

    @property
    def params(self):
        return {"atr_period": self.atr_period, "multiplier": self.multiplier,
                "allow_short": self.allow_short}

    def generate(self, df):
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low).rename("hl"),
            (high - prev_close).abs().rename("hc"),
            (low - prev_close).abs().rename("lc"),
        ], axis=1).max(axis=1)
        atr_val = tr.ewm(alpha=1 / self.atr_period, adjust=False,
                         min_periods=self.atr_period).mean()
        hl2 = (high + low) / 2.0
        basic_upper = hl2 + self.multiplier * atr_val
        basic_lower = hl2 - self.multiplier * atr_val

        n = len(df)
        final_upper = pd.Series(np.nan, index=df.index, dtype=float)
        final_lower = pd.Series(np.nan, index=df.index, dtype=float)
        direction = pd.Series(0, index=df.index, dtype=int)

        for i in range(n):
            if i == 0 or np.isnan(atr_val.iloc[i]):
                continue
            if i == 1 or (np.isnan(final_upper.iloc[i - 1]) and np.isnan(final_lower.iloc[i - 1])):
                final_upper.iloc[i] = basic_upper.iloc[i]
                final_lower.iloc[i] = basic_lower.iloc[i]
            else:
                fu_prev = final_upper.iloc[i - 1]
                fl_prev = final_lower.iloc[i - 1]
                pc = close.iloc[i - 1]
                if (basic_upper.iloc[i] < fu_prev) or (pc > fu_prev):
                    final_upper.iloc[i] = basic_upper.iloc[i]
                else:
                    final_upper.iloc[i] = fu_prev
                if (basic_lower.iloc[i] > fl_prev) or (pc < fl_prev):
                    final_lower.iloc[i] = basic_lower.iloc[i]
                else:
                    final_lower.iloc[i] = fl_prev

            prev_dir = direction.iloc[i - 1] if i > 0 else 0
            if prev_dir == -1:
                if close.iloc[i] > final_upper.iloc[i]:
                    direction.iloc[i] = 1
                else:
                    direction.iloc[i] = -1
            elif prev_dir == 1:
                if close.iloc[i] < final_lower.iloc[i]:
                    direction.iloc[i] = -1
                else:
                    direction.iloc[i] = 1
            else:
                if close.iloc[i] > final_upper.iloc[i]:
                    direction.iloc[i] = 1
                else:
                    direction.iloc[i] = -1

        supertrend = pd.Series(np.nan, index=df.index, dtype=float)
        for i in range(n):
            if direction.iloc[i] == 1:
                supertrend.iloc[i] = final_lower.iloc[i]
            elif direction.iloc[i] == -1:
                supertrend.iloc[i] = final_upper.iloc[i]

        target = pd.Series(0, index=df.index, dtype=int)
        target[close > supertrend] = Signal.LONG
        return target


class DonchianBreakoutStrategy(Strategy):
    """Donchian channel breakout.

    LONG when close breaks above the N-bar high; FLAT when close falls below the
    N-bar low (channel exit).
    """

    meta = StrategyMeta(
        "donchian", "Donchian channel breakout: LONG above N-bar high, FLAT below N-bar low."
    )

    def __init__(self, lookback: int = 20):
        self.lookback = lookback

    @property
    def params(self):
        return {"lookback": self.lookback}

    def generate(self, df):
        n = self.lookback
        close = df["close"]
        # Prior N-bar high (excludes current bar to avoid lookahead at entry)
        chan_high = close.shift(1).rolling(window=n, min_periods=n).max()
        chan_low = close.shift(1).rolling(window=n, min_periods=n).min()
        target = pd.Series(0, index=df.index, dtype=int)
        target[close > chan_high] = Signal.LONG
        target[close < chan_low] = Signal.FLAT
        return target


class DonchianMeanReversionStrategy(Strategy):
    """Donchian channel mean reversion (anti-breakout).

    SHORT when close breaks below the N-bar low; LONG when close rises above
    the N-bar high (channel mean reversion).
    """

    meta = StrategyMeta(
        "donchian_mr", "Donchian mean reversion: SHORT on lower break, LONG on upper break."
    )

    def __init__(self, lookback: int = 20):
        self.lookback = lookback

    @property
    def params(self):
        return {"lookback": self.lookback}

    def generate(self, df):
        n = self.lookback
        close = df["close"]
        chan_high = close.shift(1).rolling(window=n, min_periods=n).max()
        chan_low = close.shift(1).rolling(window=n, min_periods=n).min()
        target = pd.Series(0, index=df.index, dtype=int)
        target[close > chan_high] = Signal.SHORT
        target[close < chan_low] = Signal.LONG
        return target


class EMAReversalStrategy(Strategy):
    """EMA cross with mean-reversion flavor.

    LONG when fast EMA > slow EMA (trend), but exit quickly if price closes
    below the fast EMA (mean-reversion twist on exit).
    """

    meta = StrategyMeta(
        "ema_reversal", "EMA cross with EMA-exit twist."
    )

    def __init__(self, fast: int = 12, slow: int = 26):
        self.fast = fast
        self.slow = slow

    @property
    def params(self):
        return {"fast": self.fast, "slow": self.slow}

    def generate(self, df):
        close = df["close"]
        fe = close.ewm(span=self.fast, adjust=False, min_periods=1).mean()
        se = close.ewm(span=self.slow, adjust=False, min_periods=1).mean()
        target = pd.Series(0, index=df.index, dtype=int)
        # Long when fast EMA > slow EMA
        long_mask = fe > se
        # Exit when close < fast EMA (mean-reversion twist)
        exit_mask = close < fe
        in_pos = False
        values = np.zeros(len(df), dtype=int)
        for i in range(len(df)):
            if not in_pos:
                if long_mask.iloc[i]:
                    values[i] = Signal.LONG
                    in_pos = True
                # else stays flat
            else:
                if exit_mask.iloc[i]:
                    values[i] = Signal.FLAT
                    in_pos = False
                else:
                    values[i] = Signal.LONG
        return pd.Series(values, index=df.index, dtype=int)


class MomentumMeanReversionStrategy(Strategy):
    """Momentum reversal: short when momentum is extremely high, long when extremely low.

    This is a volatility/momentum mean-reversion strategy that goes long when
    momentum is very negative (oversold) and short when very positive (overbought).
    """

    meta = StrategyMeta(
        "momentum_mr", "Momentum mean reversion: LONG on extreme negative momentum, SHORT on extreme positive."
    )

    def __init__(self, window: int = 20, entry_thr: float = 0.03, exit_thr: float = 0.0):
        self.window = window
        self.entry_thr = entry_thr
        self.exit_thr = exit_thr

    @property
    def params(self):
        return {"window": self.window, "entry_thr": self.entry_thr, "exit_thr": self.exit_thr}

    def generate(self, df):
        mom = df["close"] / df["close"].shift(self.window) - 1.0
        target = pd.Series(0, index=df.index, dtype=int)
        target[mom < -self.entry_thr] = Signal.LONG
        target[mom > self.entry_thr] = Signal.SHORT
        target[(mom > self.exit_thr) & (mom <= self.entry_thr)] = Signal.FLAT
        target[(mom < self.exit_thr) & (mom >= -self.entry_thr)] = Signal.FLAT
        return target


class VolumeEMAFilterStrategy(Strategy):
    """Price trend + volume confirmation.

    LONG when fast EMA > slow EMA AND volume > volume_sma(N).
    Exit when EMA cross flips.
    """

    meta = StrategyMeta(
        "vol_ema", "EMA cross with volume confirmation."
    )

    def __init__(self, fast: int = 12, slow: int = 26, vol_window: int = 20):
        self.fast = fast
        self.slow = slow
        self.vol_window = vol_window

    @property
    def params(self):
        return {"fast": self.fast, "slow": self.slow, "vol_window": self.vol_window}

    def generate(self, df):
        close = df["close"]
        volume = df["volume"]
        fe = close.ewm(span=self.fast, adjust=False, min_periods=1).mean()
        se = close.ewm(span=self.slow, adjust=False, min_periods=1).mean()
        vol_sma = volume.rolling(window=self.vol_window, min_periods=self.vol_window).mean()
        target = pd.Series(0, index=df.index, dtype=int)
        target[(fe > se) & (volume > vol_sma)] = Signal.LONG
        return target


# --------------------------------------------------------------------------- #
# StrategySpec builders
# --------------------------------------------------------------------------- #
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
    }, model="round2-script")


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
    return StrategySpec.from_model_json(payload, model="round2-script")


def rsi_mr_spec(window, oversold, overbought, stop=None, take=None):
    rk = f"rsi_{window}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"RSI {window} MR {oversold}-{overbought}",
        "description": f"LONG when RSI {window} crosses above {oversold}; exit above {overbought}.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [{"name": "rsi", "params": {"window": window}}],
        "entry": make_condition(indicator_operand(rk), "crosses_above",
                                const_operand(float(oversold))),
        "exit": make_condition(indicator_operand(rk), ">",
                               const_operand(float(overbought))),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round2-script")


def bb_mr_spec(window, num_std, rsi_window, rsi_os, stop=None, take=None):
    ns = int(num_std) if float(num_std).is_integer() else num_std
    bb_lk = f"bb_lower_{window}_{ns}"
    bb_mk = f"bb_middle_{window}_{ns}"
    rk = f"rsi_{rsi_window}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"BB {window}-{ns} and RSI {rsi_window} MR",
        "description": f"LONG when close < lower BB AND RSI<{rsi_os}; exit when close > middle BB.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [
            {"name": "bb_lower", "params": {"window": window, "num_std": num_std}},
            {"name": "bb_middle", "params": {"window": window, "num_std": num_std}},
            {"name": "rsi", "params": {"window": rsi_window}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), "<", indicator_operand(bb_lk)),
            make_condition(indicator_operand(rk), "<", const_operand(float(rsi_os))),
        ),
        "exit": make_condition(field_operand("close"), ">", indicator_operand(bb_mk)),
        "position_sizing": {"max_allocation_pct": 0.5},
        "risk": risk,
    }, model="round2-script")


def bb_upper_breakout_spec(window, num_std, stop=None, take=None):
    ns = int(num_std) if float(num_std).is_integer() else num_std
    bb_uk = f"bb_upper_{window}_{ns}"
    bb_mk = f"bb_middle_{window}_{ns}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"BB {window}-{ns} upper breakout",
        "description": f"LONG when close > upper BB({window},{ns}); exit when close < middle BB.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [
            {"name": "bb_upper", "params": {"window": window, "num_std": num_std}},
            {"name": "bb_middle", "params": {"window": window, "num_std": num_std}},
        ],
        "entry": make_condition(field_operand("close"), ">", indicator_operand(bb_uk)),
        "exit": make_condition(field_operand("close"), "<", indicator_operand(bb_mk)),
        "position_sizing": {"max_allocation_pct": 0.5},
        "risk": risk,
    }, model="round2-script")


def sma_trend_spec(window, stop=None, take=None):
    sk = f"sma_{window}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"SMA {window} trend filter",
        "description": f"LONG while close > SMA {window}; exit when close < SMA {window}.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [{"name": "sma", "params": {"window": window}}],
        "entry": make_condition(field_operand("close"), ">", indicator_operand(sk)),
        "exit": make_condition(field_operand("close"), "<", indicator_operand(sk)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round2-script")


def multi_factor_spec(sma_w, ema_fast, ema_slow, rsi_w, stop=None, take=None):
    sma_k = f"sma_{sma_w}"
    ef_k = f"ema_{ema_fast}"
    es_k = f"ema_{ema_slow}"
    rk = f"rsi_{rsi_w}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"Multi-factor SMA {sma_w} EMA {ema_fast}-{ema_slow} RSI {rsi_w}",
        "description": f"LONG when SMA{sma_w} trend AND EMA cross AND RSI>{rsi_w}>50.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [
            {"name": "sma", "params": {"window": sma_w}},
            {"name": "ema", "params": {"window": ema_fast}},
            {"name": "ema", "params": {"window": ema_slow}},
            {"name": "rsi", "params": {"window": rsi_w}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand(sma_k)),
            make_condition(indicator_operand(ef_k), "crosses_above", indicator_operand(es_k)),
            make_condition(indicator_operand(rk), ">", const_operand(50.0)),
        ),
        "exit": make_condition(field_operand("close"), "<", indicator_operand(sma_k)),
        "position_sizing": {"max_allocation_pct": 0.7},
        "risk": risk,
    }, model="round2-script")


def macd_rsi_spec(fast, slow, signal_sp, rsi_w, rsi_threshold, stop=None, take=None):
    mk = f"macd_{fast}_{slow}_{signal_sp}"
    sk = f"macd_signal_{fast}_{slow}_{signal_sp}"
    rk = f"rsi_{rsi_w}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"MACD {fast}-{slow}-{signal_sp} and RSI {rsi_w} above {rsi_threshold}",
        "description": f"LONG when MACD > signal AND RSI > {rsi_threshold}; exit on MACD < signal.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [
            {"name": "macd", "params": {"fast": fast, "slow": slow, "signal": signal_sp}},
            {"name": "macd_signal", "params": {"fast": fast, "slow": slow, "signal": signal_sp}},
            {"name": "rsi", "params": {"window": rsi_w}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand(mk), ">", indicator_operand(sk)),
            make_condition(indicator_operand(rk), ">", const_operand(float(rsi_threshold))),
        ),
        "exit": make_condition(indicator_operand(mk), "crosses_below", indicator_operand(sk)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round2-script")


def momentum_spec(window, entry_thr, exit_thr, stop=None, take=None):
    mk = f"momentum_{window}"
    risk = {}
    if stop:
        risk["stop_loss_pct"] = stop
    if take:
        risk["take_profit_pct"] = take
    return StrategySpec.from_model_json({
        "name": f"Momentum {window} entry {entry_thr*100:.0f}%",
        "description": f"LONG when 12-bar momentum > {entry_thr}; exit when momentum < {exit_thr}.",
        "symbol": "NSE:NIFTY", "timeframe": "1d",
        "indicators": [{"name": "momentum", "params": {"window": window}}],
        "entry": make_condition(indicator_operand(mk), ">", const_operand(entry_thr)),
        "exit": make_condition(indicator_operand(mk), "<", const_operand(exit_thr)),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": risk,
    }, model="round2-script")


def warmup(w):
    return max(w, 5)


def make_config(risk_dict=None, warmup_bars=50):
    risk_params = {}
    if risk_dict:
        risk_params = risk_dict
    return BacktestConfig(
        initial_capital=INITIAL_CAPITAL,
        slippage_pct=0.001,
        cost_model=IndiaTransactionCostModel(),
        cost_segment=CostSegment.EQUITY_FUTURE.value,
        warmup_bars=warmup_bars,
        risk=RiskConfig(
            max_allocation_pct=risk_params.get("max_allocation_pct", 0.95),
            max_position_size=risk_params.get("max_position_size"),
            allow_short=risk_params.get("allow_short", False),
            stop_loss_pct=risk_params.get("stop_loss_pct"),
            take_profit_pct=risk_params.get("take_profit_pct"),
        ),
    )


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
        cfg = make_config(risk_dict, warmup_bars)
        strategy = strat_or_spec

    try:
        result = run_backtest(dataset, strategy, cfg)
        perf = compute_performance(result)
        return (name, perf, result, cfg)
    except Exception as e:
        print(f"ERROR {name}: {e}")
        return (name, None, None, cfg)


def run_all_backtests(dataset):
    """Test 30+ strategies with parameter sweeps and risk parameters."""
    configs = []

    # === EMA Cross Parameter Sweep (with risk) ===
    for fast, slow in [(5, 13), (5, 20), (8, 21), (10, 30), (13, 34),
                       (21, 55), (26, 52), (21, 144), (5, 10), (15, 45)]:
        configs.append((
            f"EMA {fast}-{slow} cross (built-in, SL10 TP20)",
            EMATrendStrategy(fast=fast, slow=slow),
            False, 200,
            {"stop_loss_pct": 0.10, "take_profit_pct": 0.20},
        ))

    # === Supertrend Parameter Sweep (with risk) ===
    for atr_p, mult in [(7, 2.0), (7, 2.5), (10, 2.0), (10, 2.5),
                        (10, 3.0), (14, 2.0), (14, 3.0), (14, 3.5), (21, 3.0)]:
        configs.append((
            f"Supertrend({atr_p},{mult}) (SL10 TP20)",
            SupertrendStrategy(atr_period=atr_p, multiplier=mult),
            False, atr_p + 10,
            {"stop_loss_pct": 0.10, "take_profit_pct": 0.20},
        ))

    # === SMA Cross Parameter Sweep (StrategySpec, with risk) ===
    for fast, slow in [(5, 10), (5, 20), (10, 20), (10, 30), (20, 50),
                       (20, 100), (50, 100), (50, 150)]:
        configs.append((
            f"SMA {fast}-{slow} cross (SL10 TP20)",
            sma_cross_spec(fast, slow, stop=0.10, take=0.20),
            True, max(slow, fast) + 10,
            None,
        ))

    # === SMA Trend Filter Parameter Sweep ===
    for w in [10, 30, 50, 100]:
        configs.append((
            f"SMA {w} trend filter (SL7)",
            sma_trend_spec(w, stop=0.07),
            True, w + 10,
            None,
        ))

    # === RSI Mean Reversion Parameter Sweep ===
    for window, os, ob in [(2, 10, 65), (2, 5, 70), (2, 15, 80),
                           (5, 20, 60), (5, 25, 65), (7, 25, 65),
                           (14, 30, 55), (14, 20, 50)]:
        configs.append((
            f"RSI {window} MR {os}-{ob} (SL4 TP8)",
            rsi_mr_spec(window, os, ob, stop=0.04, take=0.08),
            True, window * 2 + 10,
            None,
        ))

    # === Bollinger Band Strategies ===
    configs.append((
        "BB 20-2 upper breakout (SL6 TP12)",
        bb_upper_breakout_spec(20, 2.0, stop=0.06, take=0.12),
        True, 40,
        None,
    ))
    configs.append((
        "BB 20-2 lower MR RSI14 (SL5 TP10)",
        bb_mr_spec(20, 2.0, 14, 40, stop=0.05, take=0.10),
        True, 40,
        None,
    ))
    configs.append((
        "BB 10-2 lower MR RSI14 (SL5 TP10)",
        bb_mr_spec(10, 2.0, 14, 35, stop=0.05, take=0.10),
        True, 30,
        None,
    ))
    configs.append((
        "BB 20-3 upper breakout (SL6 TP10)",
        bb_upper_breakout_spec(20, 3.0, stop=0.06, take=0.10),
        True, 40,
        None,
    ))
    configs.append((
        "BB 50-2 lower MR RSI14 (SL5 TP10)",
        bb_mr_spec(50, 2.0, 14, 30, stop=0.05, take=0.10),
        True, 100,
        None,
    ))

    # === Donchian Channel Strategies ===
    for lb in [10, 20, 55]:
        configs.append((
            f"Donchian {lb} breakout (SL8 TP15)",
            DonchianBreakoutStrategy(lookback=lb),
            False, lb + 5,
            {"stop_loss_pct": 0.08, "take_profit_pct": 0.15},
        ))

    # === Multi-Factor Strategies ===
    configs.append((
        "SMA20+EMA13-34+RSI14 multi-factor (SL5 TP12)",
        multi_factor_spec(20, 13, 34, 14, stop=0.05, take=0.12),
        True, 50,
        None,
    ))
    configs.append((
        "SMA30+EMA8-21+RSI14 multi-factor (SL5 TP10)",
        multi_factor_spec(30, 8, 21, 14, stop=0.05, take=0.10),
        True, 30,
        None,
    ))
    configs.append((
        "MACD+RSI14 filter (SL5 TP10)",
        macd_rsi_spec(12, 26, 9, 14, 50, stop=0.05, take=0.10),
        True, 40,
        None,
    ))
    configs.append((
        "MACD+RSI14>60 filter (SL5 TP12)",
        macd_rsi_spec(12, 26, 9, 14, 60, stop=0.05, take=0.12),
        True, 40,
        None,
    ))

    # === Momentum Parameter Sweep ===
    for window, thr in [(5, 0.01), (10, 0.02), (12, 0.0), (20, 0.02), (20, 0.03)]:
        configs.append((
            f"Momentum {window} thr{thr} (SL6 TP10)",
            MomentumStrategy(window=window, entry_thr=thr, exit_thr=0.0),
            False, window + 5,
            {"stop_loss_pct": 0.06, "take_profit_pct": 0.10},
        ))

    # === EMA Reversal (mean-reversion twist) ===
    for fast, slow in [(12, 26), (8, 21), (13, 34)]:
        configs.append((
            f"EMA {fast}-{slow} reversal (SL5 TP10)",
            EMAReversalStrategy(fast=fast, slow=slow),
            False, slow + 5,
            {"stop_loss_pct": 0.05, "take_profit_pct": 0.10},
        ))

    # === Volume-Confirmed Strategies ===
    configs.append((
        "EMA12-26 + volume filter (SL10 TP20)",
        VolumeEMAFilterStrategy(fast=12, slow=26, vol_window=20),
        False, 35,
        {"stop_loss_pct": 0.10, "take_profit_pct": 0.20},
    ))
    configs.append((
        "EMA8-21 + volume filter (SL8 TP15)",
        VolumeEMAFilterStrategy(fast=8, slow=21, vol_window=20),
        False, 30,
        {"stop_loss_pct": 0.08, "take_profit_pct": 0.15},
    ))

    # === 200-day regime filter ===
    configs.append((
        "SMA 200 trend filter (SL5 TP10)",
        sma_trend_spec(200, stop=0.05, take=0.10),
        True, 220,
        None,
    ))

    # === EMA short-enabled ===
    configs.append((
        "EMA 12-26 long-short (SL10 TP20)",
        ema_cross_spec(12, 26, allow_short=True, stop=0.10, take=0.20),
        True, 40,
        None,
    ))

    # === Donchian mean reversion (anti-breakout) ===
    configs.append((
        "Donchian 20 MR (anti-breakout) (SL6 TP12)",
        DonchianMeanReversionStrategy(lookback=20),
        False, 25,
        {"stop_loss_pct": 0.06, "take_profit_pct": 0.12},
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
    print("\n" + "=" * 130)
    print("NIFTY 50 Round 2 - Extended Strategy Backtest Results")
    print(f"Capital: Rs.{INITIAL_CAPITAL:,.0f}  Data: {CSV_PATH}  Strategies: {len(results)}")
    print("=" * 130)
    print(tabulate(rows, headers=headers, tablefmt="grid"))
    print("=" * 130)

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

        print(f"\nProfitable: {sum(1 for _,p in valid if p.total_return > 0)}/"
              f"{len(valid)}  Unprofitable: {sum(1 for _,p in valid if p.total_return <= 0)}/{len(valid)}")


if __name__ == "__main__":
    print("Loading Nifty 50 dataset...")
    ds = load_nifty_dataset()
    print(f"\nDataset: {ds.symbol} {ds.timeframe}, {len(ds.data)} rows")
    print(f"Date range: {ds.data.index[0].date()} to {ds.data.index[-1].date()}")
    print("\nRunning Round 2 backtests...\n")
    results = run_all_backtests(ds)
    print_summary(results)
