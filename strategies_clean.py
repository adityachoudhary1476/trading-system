"""Phase 23 — Strategy Universe (initial set of 5 candidates).

Reuses the existing StrategySpec DSL. Each candidate is a machine-readable
spec with explicit provenance, parameters, and required features.

This is a focused initial set to validate the tournament pipeline. The
full ~100-candidate universe will be added incrementally.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..strategy_lab.spec import (
    StrategySpec,
    const_operand,
    field_operand,
    indicator_operand,
    logic,
    make_condition,
)


class StrategyFamily(str, Enum):
    TREND_MOMENTUM = "trend_momentum"
    MEAN_REVERSION = "mean_reversion"
    BREAKOUT_VOLATILITY = "breakout_volatility"
    VOLUME_MARKET_STRUCTURE = "volume_market_structure"
    MULTI_TIMEFRAME = "multi_timeframe"
    REGIME_AWARE = "regime_aware"
    CROSS_SECTIONAL = "cross_sectional"
    STATISTICAL = "statistical"
    OTHER = "other"


@dataclass(frozen=True)
class UniverseCandidate:
    candidate_id: str
    strategy_family: StrategyFamily
    strategy_name: str
    description: str
    hypothesis: str
    required_features: list[str]
    timeframe: str
    supported_instruments: list[str]
    supported_sessions: list[str]
    parameter_schema: dict[str, Any]
    default_parameters: dict[str, Any]
    parameter_ranges: dict[str, tuple[Any, Any]]
    version: str = "1.0.0"
    implementation_status: str = "implemented"
    spec_builder: Callable[..., dict] | None = None
    options_strategy: bool = False

    def build_spec(self, symbol: str = "NSE:SBIN", timeframe: str = "1d") -> StrategySpec:
        if self.spec_builder is None:
            raise ValueError(f"candidate {self.candidate_id!r} has no spec_builder")
        payload = self.spec_builder(symbol, timeframe)
        payload.setdefault("generated_by", f"phase23:{self.candidate_id}")
        if "description" not in payload or not payload["description"]:
            payload["description"] = self.description
        if "name" not in payload or not payload["name"]:
            payload["name"] = self.strategy_name
        return StrategySpec(**payload)


def _risk(stop=None, take=None, allow_short=False):
    out = {}
    if stop is not None:
        out["stop_loss_pct"] = float(stop)
    if take is not None:
        out["take_profit_pct"] = float(take)
    if allow_short:
        out["allow_short"] = True
    return out


def _size(max_alloc=0.95):
    return {"max_allocation_pct": float(max_alloc)}


# --------------------------------------------------------------------------- #
# Strategy 1 — Trend / Momentum: EMA fast/slow cross
# --------------------------------------------------------------------------- #
def _trend_ema_fast_slow(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Trend_EMA_FastSlow",
        "description": "LONG when EMA(12) > EMA(26); exit when EMA(12) < EMA(26).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 12}},
            {"name": "ema", "params": {"window": 26}},
        ],
        "entry": make_condition(indicator_operand("ema_12"), ">", indicator_operand("ema_26")),
        "entry_short": make_condition(indicator_operand("ema_12"), "<", indicator_operand("ema_26")),
        "exit": make_condition(indicator_operand("ema_12"), "<", indicator_operand("ema_26")),
        "allow_long": True,
        "position_sizing": _size(0.30),
        "risk": _risk(stop=0.015, allow_short=True),
    }


# --------------------------------------------------------------------------- #
# Strategy 2 — Mean Reversion: RSI oversold bounce
# --------------------------------------------------------------------------- #
def _meanrev_rsi_oversold(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_MeanRev_RSI_Oversold",
        "description": "LONG when RSI(14) < 30; exit when RSI(14) > 55.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [{"name": "rsi", "params": {"window": 14}}],
        "entry": make_condition(indicator_operand("rsi_14"), "<", const_operand(30.0)),
        "entry_short": make_condition(indicator_operand("rsi_14"), ">", const_operand(70.0)),
        "exit": make_condition(indicator_operand("rsi_14"), ">", const_operand(55.0)),
        "allow_long": True,
        "position_sizing": _size(0.30),
        "risk": _risk(stop=0.02, allow_short=True),
    }


# --------------------------------------------------------------------------- #
# Strategy 3 — Breakout: N-bar high breakout
# --------------------------------------------------------------------------- #
def _breakout_nbar(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Breakout_NBar",
        "description": "LONG when close > highest high of prior 20 bars; exit on 10-bar low.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "donchian_upper", "params": {"window": 20}},
            {"name": "donchian_lower", "params": {"window": 10}},
        ],
        "entry": make_condition(field_operand("close"), ">", indicator_operand("donchian_upper_20")),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("donchian_lower_10")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.04),
    }


# --------------------------------------------------------------------------- #
# Strategy 4 — Volume: volume-confirmed trend
# --------------------------------------------------------------------------- #
def _volume_confirmation(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Volume_Confirmation",
        "description": "LONG when close > SMA(20) AND volume > volume SMA(20); exit on close < SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 20}},
            {"name": "volume_sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 5 — Multi-Timeframe: higher-timeframe trend + lower-timeframe entry
# --------------------------------------------------------------------------- #
def _mtf_htf_trend_ltf_entry(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_MTF_HTF_Trend_LTF_Entry",
        "description": "LONG when close > SMA(50) AND EMA(10) > SMA(20); exit on close < SMA(50).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 50}},
            {"name": "ema", "params": {"window": 10}},
            {"name": "sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("sma_50")),
            make_condition(indicator_operand("ema_10"), ">", indicator_operand("sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_50")),
        "allow_long": True,
        "position_sizing": _size(0.30),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Universe assembly
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Strategy 6 — Trend / Momentum: Triple EMA stack
# --------------------------------------------------------------------------- #
def _trend_ema_triple(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Trend_EMA_Triple",
        "description": "LONG when EMA(5) > EMA(20) > EMA(50); exit on close < EMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 5}},
            {"name": "ema", "params": {"window": 20}},
            {"name": "ema", "params": {"window": 50}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("ema_5"), ">", indicator_operand("ema_20")),
            make_condition(indicator_operand("ema_20"), ">", indicator_operand("ema_50")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("ema_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.02),
    }


# --------------------------------------------------------------------------- #
# Strategy 7 — Trend / Momentum: Price momentum
# --------------------------------------------------------------------------- #
def _trend_price_momentum(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Trend_PriceMomentum",
        "description": "LONG when 20-bar momentum > 5%; exit when momentum < 0.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [{"name": "momentum", "params": {"window": 20}}],
        "entry": make_condition(indicator_operand("momentum_20"), ">", const_operand(0.05)),
        "entry_short": None,
        "exit": make_condition(indicator_operand("momentum_20"), "<", const_operand(0.0)),
        "allow_long": True,
        "position_sizing": _size(0.30),
        "risk": _risk(stop=0.05, take=0.15),
    }


# --------------------------------------------------------------------------- #
# Strategy 8 — Breakout / Volatility: ATR volatility breakout
# --------------------------------------------------------------------------- #
def _breakout_atr(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Breakout_ATR",
        "description": "LONG when close > SMA(20) + 1.5*ATR(14); exit on close < SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 20}},
            {"name": "atr", "params": {"window": 14}},
        ],
        "entry": make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 9 — Breakout / Volatility: Range breakout
# --------------------------------------------------------------------------- #
def _breakout_range(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Breakout_Range",
        "description": "LONG when close > high of prior 20 bars; exit on close < low of prior 10 bars.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "donchian_upper", "params": {"window": 20}},
            {"name": "donchian_lower", "params": {"window": 10}},
        ],
        "entry": make_condition(field_operand("close"), ">", indicator_operand("donchian_upper_20")),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("donchian_lower_10")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.04),
    }


# --------------------------------------------------------------------------- #
# Strategy 10 — Volume / Market Structure: Volume spike
# --------------------------------------------------------------------------- #
def _volume_spike(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Volume_Spike",
        "description": "LONG when volume > 2*volume SMA(20) AND close > SMA(10); exit on volume < SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 10}},
            {"name": "volume_sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("volume"), ">", const_operand(2.0)),
            make_condition(field_operand("close"), ">", indicator_operand("sma_10")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("volume"), "<", indicator_operand("volume_sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.20),
        "risk": _risk(stop=0.03, take=0.06),
    }


# --------------------------------------------------------------------------- #
# Strategy 11 — Volume / Market Structure: Price-volume momentum
# --------------------------------------------------------------------------- #
def _price_volume_momentum(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_PriceVolume_Momentum",
        "description": "LONG when momentum(10) > 0 AND volume > volume SMA(20); exit on momentum < 0.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "momentum", "params": {"window": 10}},
            {"name": "volume_sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("momentum_10"), ">", const_operand(0.0)),
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("momentum_10"), "<", const_operand(0.0)),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 12 — Multi-Timeframe: Dual MA + momentum
# --------------------------------------------------------------------------- #
def _mtf_dual_ma_momentum(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_MTF_DualMA_Momentum",
        "description": "LONG when EMA(5) > SMA(20) AND momentum(10) > 0; exit on cross below.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 5}},
            {"name": "sma", "params": {"window": 20}},
            {"name": "momentum", "params": {"window": 10}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("ema_5"), ">", indicator_operand("sma_20")),
            make_condition(indicator_operand("momentum_10"), ">", const_operand(0.0)),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("ema_5"), "<", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.02),
    }


# --------------------------------------------------------------------------- #
# Strategy 13 — Regime-Aware: Trend regime
# --------------------------------------------------------------------------- #
def _regime_trend(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Regime_Trend",
        "description": "LONG when close > SMA(20) AND ATR(14) > 1.0; exit on close < SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 20}},
            {"name": "atr", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
            make_condition(indicator_operand("atr_14"), ">", const_operand(1.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 14 — Regime-Aware: Volatility regime
# --------------------------------------------------------------------------- #
def _regime_volatility(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Regime_Volatility",
        "description": "LONG when ATR(14) > 2.0 AND close > SMA(20); exit on ATR < 1.0.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 20}},
            {"name": "atr", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
            make_condition(indicator_operand("atr_14"), ">", const_operand(2.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.04),
    }


# --------------------------------------------------------------------------- #
# Strategy 15 — Regime-Aware: Mean-reversion in low-volatility regimes
# --------------------------------------------------------------------------- #
def _regime_mean_rev_low_vol(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Regime_MeanRev_LowVol",
        "description": "LONG when RSI(14) < 30 AND ATR(14) < 1.5; exit on RSI > 50.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "rsi", "params": {"window": 14}},
            {"name": "atr", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("rsi_14"), "<", const_operand(30.0)),
            make_condition(indicator_operand("atr_14"), "<", const_operand(1.5)),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("rsi_14"), ">", const_operand(50.0)),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.015),
    }


# --------------------------------------------------------------------------- #
# Strategy 16 — Cross-Sectional: MACD + RSI multi-indicator confirmation
# --------------------------------------------------------------------------- #
def _cross_macd_rsi(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Cross_MACD_RSI",
        "description": "LONG when MACD > signal AND RSI > 50; exit on MACD < signal.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"name": "macd_signal", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"name": "rsi", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("macd_12_26_9"), ">", indicator_operand("macd_signal_12_26_9")),
            make_condition(indicator_operand("rsi_14"), ">", const_operand(50.0)),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("macd_12_26_9"), "<", indicator_operand("macd_signal_12_26_9")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 17 — Statistical: Bollinger Band mean reversion
# --------------------------------------------------------------------------- #
def _stat_bollinger_reversion(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Stat_Bollinger_Reversion",
        "description": "LONG when close < BB lower; exit when close > BB middle.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "bb_lower", "params": {"window": 20, "num_std": 2.0}},
            {"name": "bb_middle", "params": {"window": 20, "num_std": 2.0}},
        ],
        "entry": make_condition(field_operand("close"), "<", indicator_operand("bb_lower_20_2")),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), ">", indicator_operand("bb_middle_20_2")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 18 — Statistical: MACD histogram momentum
# --------------------------------------------------------------------------- #
def _stat_macd_velocity(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Stat_MACD_Velocity",
        "description": "LONG when MACD histogram > 0; exit when histogram < 0.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "macd_histogram", "params": {"fast": 12, "slow": 26, "signal": 9}},
        ],
        "entry": make_condition(indicator_operand("macd_histogram_12_26_9"), ">", const_operand(0.0)),
        "entry_short": None,
        "exit": make_condition(indicator_operand("macd_histogram_12_26_9"), "<", const_operand(0.0)),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 19 — Other: Gap-down fill
# --------------------------------------------------------------------------- #
def _other_gap_down_fill(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Other_GapDown_Fill",
        "description": "LONG when prior close > open gap down AND volume > vol SMA(20); exit when close > prior close.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "volume_sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("prev_close"), ">", field_operand("open")),
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), ">", field_operand("prev_close")),
        "allow_long": True,
        "position_sizing": _size(0.20),
        "risk": _risk(stop=0.02, take=0.04),
    }


# --------------------------------------------------------------------------- #
# Strategy 20 — Other: Gap-up continuation
# --------------------------------------------------------------------------- #
def _other_gap_up_continuation(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Other_GapUp_Continuation",
        "description": "LONG when prior close < open gap up AND close > open; exit when close < open.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [],
        "entry": logic("AND",
            make_condition(field_operand("prev_close"), "<", field_operand("open")),
            make_condition(field_operand("close"), ">", field_operand("open")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", field_operand("open")),
        "allow_long": True,
        "position_sizing": _size(0.20),
        "risk": _risk(stop=0.02, take=0.04),
    }


# --------------------------------------------------------------------------- #
# Strategy 21 — Trend / Momentum: MACD trend
# --------------------------------------------------------------------------- #
def _trend_macd(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Trend_MACD",
        "description": "LONG when MACD > signal AND close > SMA(20); exit on MACD < signal.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"name": "macd_signal", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"name": "sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("macd_12_26_9"), ">", indicator_operand("macd_signal_12_26_9")),
            make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("macd_12_26_9"), "<", indicator_operand("macd_signal_12_26_9")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 22 — Mean Reversion: Bollinger + RSI
# --------------------------------------------------------------------------- #
def _meanrev_bollinger_rsi(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_MeanRev_Bollinger_RSI",
        "description": "LONG when RSI(14) < 30 AND close < BB lower; exit on RSI > 50.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "rsi", "params": {"window": 14}},
            {"name": "bb_lower", "params": {"window": 20, "num_std": 2.0}},
            {"name": "bb_middle", "params": {"window": 20, "num_std": 2.0}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("rsi_14"), "<", const_operand(30.0)),
            make_condition(field_operand("close"), "<", indicator_operand("bb_lower_20_2")),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("rsi_14"), ">", const_operand(50.0)),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.02),
    }


# --------------------------------------------------------------------------- #
# Strategy 23 — Breakout / Volatility: Bollinger upper breakout
# --------------------------------------------------------------------------- #
def _breakout_bollinger_upper(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Breakout_Bollinger_Upper",
        "description": "LONG when close > BB upper; exit on close < BB middle.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "bb_upper", "params": {"window": 20, "num_std": 2.0}},
            {"name": "bb_middle", "params": {"window": 20, "num_std": 2.0}},
        ],
        "entry": make_condition(field_operand("close"), ">", indicator_operand("bb_upper_20_2")),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("bb_middle_20_2")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 24 — Volume / Market Structure: Multi-factor trend
# --------------------------------------------------------------------------- #
def _volume_price_trend(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Volume_Price_Trend",
        "description": "LONG when volume > vol SMA(20) AND close > SMA(20) AND momentum(10) > 0; exit on close < SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "volume_sma", "params": {"window": 20}},
            {"name": "sma", "params": {"window": 20}},
            {"name": "momentum", "params": {"window": 10}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
            make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
            make_condition(indicator_operand("momentum_10"), ">", const_operand(0.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 25 — Regime-Aware: Trend strength
# --------------------------------------------------------------------------- #
def _regime_trend_strength(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Regime_Trend_Strength",
        "description": "LONG when close > SMA(50) AND SMA(20) > SMA(50) AND ATR(14) > 1.0; exit on close < SMA(50).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 50}},
            {"name": "sma", "params": {"window": 20}},
            {"name": "atr", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("sma_50")),
            make_condition(indicator_operand("sma_20"), ">", indicator_operand("sma_50")),
            make_condition(indicator_operand("atr_14"), ">", const_operand(1.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_50")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 16 — Cross-Sectional: MACD crossover + RSI filter
# --------------------------------------------------------------------------- #
def _cross_macd_crossover_rsi(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Cross_MACD_Crossover_RSI",
        "description": "LONG when MACD crosses above signal AND RSI > 50; exit on cross below.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"name": "macd_signal", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"name": "rsi", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("macd_12_26_9"), "crosses_above", indicator_operand("macd_signal_12_26_9")),
            make_condition(indicator_operand("rsi_14"), ">", const_operand(50.0)),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("macd_12_26_9"), "crosses_below", indicator_operand("macd_signal_12_26_9")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 17 — Statistical: Bollinger middle reversion with volume confirmation
# --------------------------------------------------------------------------- #
def _stat_bollinger_volume(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Stat_Bollinger_Volume",
        "description": "LONG when close > BB middle AND volume > volume SMA(20); exit on close < BB middle.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "bb_middle", "params": {"window": 20, "num_std": 2.0}},
            {"name": "volume_sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("bb_middle_20_2")),
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("bb_middle_20_2")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 18 — Other: Gap-and-recovery pattern
# --------------------------------------------------------------------------- #
def _other_gap_recovery(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Other_Gap_Recovery",
        "description": "LONG when prior close > open gap down AND close > prior open; exit when close < open.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [],
        "entry": logic("AND",
            make_condition(field_operand("prev_close"), ">", field_operand("open")),
            make_condition(field_operand("close"), ">", field_operand("open")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", field_operand("open")),
        "allow_long": True,
        "position_sizing": _size(0.20),
        "risk": _risk(stop=0.02, take=0.04),
    }


# --------------------------------------------------------------------------- #
# Strategy 19 — Other: Strong bullish close
# --------------------------------------------------------------------------- #
def _other_strong_close(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Other_Strong_Close",
        "description": "LONG when close > 98% of high AND volume > volume SMA(20); exit when close < open.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "volume_sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", const_operand(0.98)),
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", field_operand("open")),
        "allow_long": True,
        "position_sizing": _size(0.20),
        "risk": _risk(stop=0.02, take=0.04),
    }


# --------------------------------------------------------------------------- #
# Strategy 20 — Trend / Momentum: EMA cloud (8/21/50)
# --------------------------------------------------------------------------- #
def _trend_ema_cloud(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Trend_EMA_Cloud",
        "description": "LONG when EMA(8) > EMA(21) AND EMA(21) > EMA(50); exit on close < EMA(21).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 8}},
            {"name": "ema", "params": {"window": 21}},
            {"name": "ema", "params": {"window": 50}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("ema_8"), ">", indicator_operand("ema_21")),
            make_condition(indicator_operand("ema_21"), ">", indicator_operand("ema_50")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("ema_21")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 21 — Mean Reversion: RSI + volume spike
# --------------------------------------------------------------------------- #
def _meanrev_rsi_volume(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_MeanRev_RSI_Volume",
        "description": "LONG when RSI(14) < 25 AND volume > 2*volume SMA(20); exit when RSI > 40.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "rsi", "params": {"window": 14}},
            {"name": "volume_sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("rsi_14"), "<", const_operand(25.0)),
            make_condition(field_operand("volume"), ">", const_operand(2.0)),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("rsi_14"), ">", const_operand(40.0)),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.02),
    }


# --------------------------------------------------------------------------- #
# Strategy 22 — Breakout / Volatility: Donchian breakout with volume confirmation
# --------------------------------------------------------------------------- #
def _breakout_donchian_volume(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Breakout_Donchian_Volume",
        "description": "LONG when close > Donchian upper(20) AND volume > volume SMA(20); exit on close < SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "donchian_upper", "params": {"window": 20}},
            {"name": "volume_sma", "params": {"window": 20}},
            {"name": "sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("donchian_upper_20")),
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 23 — Volume / Market Structure: Volume surge with SMA filter
# --------------------------------------------------------------------------- #
def _volume_surge_sma(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Volume_Surge_SMA",
        "description": "LONG when volume > 1.5*volume SMA(10) AND close > SMA(20); exit on close < SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "volume_sma", "params": {"window": 10}},
            {"name": "sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("volume"), ">", const_operand(1.5)),
            make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 24 — Regime-Aware: Strong trend in high volatility
# --------------------------------------------------------------------------- #
def _regime_strong_trend_high_vol(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Regime_Strong_Trend_HighVol",
        "description": "LONG when close > SMA(50) AND ATR(14) > 2.0; exit on close < SMA(50).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 50}},
            {"name": "atr", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("sma_50")),
            make_condition(indicator_operand("atr_14"), ">", const_operand(2.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_50")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.035),
    }


# --------------------------------------------------------------------------- #
# Strategy 25 — Multi-Timeframe: Triple EMA alignment with RSI filter
# --------------------------------------------------------------------------- #
def _mtf_triple_ema_rsi(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_MTF_Triple_EMA_RSI",
        "description": "LONG when EMA(5) > EMA(20) AND EMA(20) > EMA(50) AND RSI > 50; exit on close < EMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 5}},
            {"name": "ema", "params": {"window": 20}},
            {"name": "ema", "params": {"window": 50}},
            {"name": "rsi", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("ema_5"), ">", indicator_operand("ema_20")),
            make_condition(indicator_operand("ema_20"), ">", indicator_operand("ema_50")),
            make_condition(indicator_operand("rsi_14"), ">", const_operand(50.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("ema_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 36 — Cross-Sectional: BB upper breakout + MACD histogram
# --------------------------------------------------------------------------- #
def _cross_bb_macd(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Cross_BB_MACD",
        "description": "LONG when close > BB upper AND MACD histogram > 0; exit on close < BB middle.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "bb_upper", "params": {"window": 20, "num_std": 2.0}},
            {"name": "bb_middle", "params": {"window": 20, "num_std": 2.0}},
            {"name": "macd_histogram", "params": {"fast": 12, "slow": 26, "signal": 9}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("bb_upper_20_2")),
            make_condition(indicator_operand("macd_histogram_12_26_9"), ">", const_operand(0.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("bb_middle_20_2")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 37 — Cross-Sectional: Triple filter (SMA + RSI + momentum)
# --------------------------------------------------------------------------- #
def _cross_triple_filter(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Cross_Triple_Filter",
        "description": "LONG when close > SMA(20) AND RSI > 50 AND momentum(10) > 0; exit on close < SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 20}},
            {"name": "rsi", "params": {"window": 14}},
            {"name": "momentum", "params": {"window": 10}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
            make_condition(indicator_operand("rsi_14"), ">", const_operand(50.0)),
            make_condition(indicator_operand("momentum_10"), ">", const_operand(0.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 38 — Statistical: BB mean reversion with ATR filter
# --------------------------------------------------------------------------- #
def _stat_bb_atr_filter(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Stat_BB_ATR_Filter",
        "description": "LONG when close < BB lower AND ATR(14) < 1.5; exit on close > BB middle.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "bb_lower", "params": {"window": 20, "num_std": 2.0}},
            {"name": "bb_middle", "params": {"window": 20, "num_std": 2.0}},
            {"name": "atr", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), "<", indicator_operand("bb_lower_20_2")),
            make_condition(indicator_operand("atr_14"), "<", const_operand(1.5)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), ">", indicator_operand("bb_middle_20_2")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.02),
    }


# --------------------------------------------------------------------------- #
# Strategy 39 — Statistical: RSI + SMA mean reversion
# --------------------------------------------------------------------------- #
def _stat_rsi_sma_reversion(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Stat_RSI_SMA_Reversion",
        "description": "LONG when RSI(14) < 28 AND close < SMA(20); exit when RSI(14) > 45.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "rsi", "params": {"window": 14}},
            {"name": "sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("rsi_14"), "<", const_operand(28.0)),
            make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("rsi_14"), ">", const_operand(45.0)),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.02),
    }


# --------------------------------------------------------------------------- #
# Strategy 40 — Statistical: Momentum surge
# --------------------------------------------------------------------------- #
def _stat_momentum_surge(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Stat_Momentum_Surge",
        "description": "LONG when momentum(20) > 5% AND close > SMA(10); exit when momentum < 0.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "momentum", "params": {"window": 20}},
            {"name": "sma", "params": {"window": 10}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("momentum_20"), ">", const_operand(0.05)),
            make_condition(field_operand("close"), ">", indicator_operand("sma_10")),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("momentum_20"), "<", const_operand(0.0)),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.04, take=0.12),
    }


# --------------------------------------------------------------------------- #
# Strategy 41 — Other: Prior high breakout with volume
# --------------------------------------------------------------------------- #
def _other_prev_high_breakout(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Other_PrevHigh_Breakout",
        "description": "LONG when close > prev_high AND volume > volume SMA(20); exit on close < prev_close.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "volume_sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", field_operand("prev_high")),
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", field_operand("prev_close")),
        "allow_long": True,
        "position_sizing": _size(0.20),
        "risk": _risk(stop=0.025, take=0.05),
    }


# --------------------------------------------------------------------------- #
# Strategy 42 — Other: Inside bar breakout
# --------------------------------------------------------------------------- #
def _other_inside_bar_breakout(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Other_InsideBar_Breakout",
        "description": "LONG when close > prev_high (breakout); exit when close < prev_low.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [],
        "entry": make_condition(field_operand("close"), ">", field_operand("prev_high")),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", field_operand("prev_low")),
        "allow_long": True,
        "position_sizing": _size(0.20),
        "risk": _risk(stop=0.02, take=0.04),
    }


# --------------------------------------------------------------------------- #
# Strategy 43 — Other: Gap-up with volume
# --------------------------------------------------------------------------- #
def _other_gap_up_volume(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Other_GapUp_Volume",
        "description": "LONG when prior close < open gap up AND volume > volume SMA(20); exit when close < open.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "volume_sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("prev_close"), "<", field_operand("open")),
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", field_operand("open")),
        "allow_long": True,
        "position_sizing": _size(0.20),
        "risk": _risk(stop=0.02, take=0.04),
    }


# --------------------------------------------------------------------------- #
# Strategy 44 — Trend / Momentum: EMA cloud (5/13/34)
# --------------------------------------------------------------------------- #
def _trend_ema_cloud_short(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Trend_EMA_Cloud_Short",
        "description": "LONG when EMA(5) > EMA(13) AND EMA(13) > EMA(34); exit on close < EMA(13).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 5}},
            {"name": "ema", "params": {"window": 13}},
            {"name": "ema", "params": {"window": 34}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("ema_5"), ">", indicator_operand("ema_13")),
            make_condition(indicator_operand("ema_13"), ">", indicator_operand("ema_34")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("ema_13")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 45 — Trend / Momentum: Price above EMA cloud
# --------------------------------------------------------------------------- #
def _trend_price_above_ema(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Trend_Price_Above_EMA",
        "description": "LONG when close > EMA(8) AND EMA(8) > EMA(21); exit on close < EMA(8).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 8}},
            {"name": "ema", "params": {"window": 21}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("ema_8")),
            make_condition(indicator_operand("ema_8"), ">", indicator_operand("ema_21")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("ema_8")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 46 — Trend / Momentum: Momentum + EMA filter
# --------------------------------------------------------------------------- #
def _trend_momentum_ema(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Trend_Momentum_EMA",
        "description": "LONG when momentum(10) > 2% AND close > EMA(20); exit on momentum < 0.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "momentum", "params": {"window": 10}},
            {"name": "ema", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("momentum_10"), ">", const_operand(0.02)),
            make_condition(field_operand("close"), ">", indicator_operand("ema_20")),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("momentum_10"), "<", const_operand(0.0)),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03, take=0.09),
    }


# --------------------------------------------------------------------------- #
# Strategy 47 — Mean Reversion: RSI deep oversold
# --------------------------------------------------------------------------- #
def _meanrev_rsi_deep(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_MeanRev_RSI_Deep",
        "description": "LONG when RSI(14) < 25; exit when RSI(14) > 55.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [{"name": "rsi", "params": {"window": 14}}],
        "entry": make_condition(indicator_operand("rsi_14"), "<", const_operand(25.0)),
        "entry_short": None,
        "exit": make_condition(indicator_operand("rsi_14"), ">", const_operand(55.0)),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.02),
    }


# --------------------------------------------------------------------------- #
# Strategy 48 — Mean Reversion: BB lower + RSI
# --------------------------------------------------------------------------- #
def _meanrev_bb_rsi(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_MeanRev_BB_RSI",
        "description": "LONG when close < BB lower AND RSI(14) < 30; exit on close > BB middle.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "bb_lower", "params": {"window": 20, "num_std": 2.0}},
            {"name": "bb_middle", "params": {"window": 20, "num_std": 2.0}},
            {"name": "rsi", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), "<", indicator_operand("bb_lower_20_2")),
            make_condition(indicator_operand("rsi_14"), "<", const_operand(30.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), ">", indicator_operand("bb_middle_20_2")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.02),
    }


# --------------------------------------------------------------------------- #
# Strategy 49 — Breakout / Volatility: Donchian + ATR
# --------------------------------------------------------------------------- #
def _breakout_donchian_atr(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Breakout_Donchian_ATR",
        "description": "LONG when close > Donchian upper(20) AND ATR(14) > 1.5; exit on close < SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "donchian_upper", "params": {"window": 20}},
            {"name": "atr", "params": {"window": 14}},
            {"name": "sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("donchian_upper_20")),
            make_condition(indicator_operand("atr_14"), ">", const_operand(1.5)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 50 — Volume / Market Structure: Volume spike + momentum
# --------------------------------------------------------------------------- #
def _volume_spike_momentum(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Volume_Spike_Momentum",
        "description": "LONG when volume > 2*volume SMA(20) AND momentum(10) > 0; exit on volume < volume SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "volume_sma", "params": {"window": 20}},
            {"name": "momentum", "params": {"window": 10}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("volume"), ">", const_operand(2.0)),
            make_condition(indicator_operand("momentum_10"), ">", const_operand(0.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("volume"), "<", indicator_operand("volume_sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 51 — Cross-Sectional: BB upper + RSI filter
# --------------------------------------------------------------------------- #
def _cross_bb_rsi(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Cross_BB_RSI",
        "description": "LONG when close > BB upper AND RSI > 50; exit on close < BB middle.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "bb_upper", "params": {"window": 20, "num_std": 2.0}},
            {"name": "bb_middle", "params": {"window": 20, "num_std": 2.0}},
            {"name": "rsi", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("bb_upper_20_2")),
            make_condition(indicator_operand("rsi_14"), ">", const_operand(50.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("bb_middle_20_2")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 52 — Cross-Sectional: EMA + BB middle + volume
# --------------------------------------------------------------------------- #
def _cross_ema_bb_volume(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Cross_EMA_BB_Volume",
        "description": "LONG when close > EMA(20) AND close > BB middle AND volume > volume SMA(20); exit on close < EMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 20}},
            {"name": "bb_middle", "params": {"window": 20, "num_std": 2.0}},
            {"name": "volume_sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("ema_20")),
            make_condition(field_operand("close"), ">", indicator_operand("bb_middle_20_2")),
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("ema_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 53 — Cross-Sectional: MACD + ATR
# --------------------------------------------------------------------------- #
def _cross_macd_atr(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Cross_MACD_ATR",
        "description": "LONG when MACD > signal AND ATR(14) > 1.5; exit on MACD < signal.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"name": "macd_signal", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"name": "atr", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("macd_12_26_9"), ">", indicator_operand("macd_signal_12_26_9")),
            make_condition(indicator_operand("atr_14"), ">", const_operand(1.5)),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("macd_12_26_9"), "<", indicator_operand("macd_signal_12_26_9")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 54 — Statistical: RSI + BB upper mean reversion
# --------------------------------------------------------------------------- #
def _stat_rsi_bb_upper(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Stat_RSI_BB_Upper",
        "description": "LONG when RSI(14) < 35 AND close < BB upper; exit when RSI(14) > 55.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "rsi", "params": {"window": 14}},
            {"name": "bb_upper", "params": {"window": 20, "num_std": 2.0}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("rsi_14"), "<", const_operand(35.0)),
            make_condition(field_operand("close"), "<", indicator_operand("bb_upper_20_2")),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("rsi_14"), ">", const_operand(55.0)),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 55 — Statistical: Momentum + ATR filter
# --------------------------------------------------------------------------- #
def _stat_momentum_atr(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Stat_Momentum_ATR",
        "description": "LONG when momentum(10) > 2% AND ATR(14) > 1.0; exit when momentum < 0.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "momentum", "params": {"window": 10}},
            {"name": "atr", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("momentum_10"), ">", const_operand(0.02)),
            make_condition(indicator_operand("atr_14"), ">", const_operand(1.0)),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("momentum_10"), "<", const_operand(0.0)),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03, take=0.09),
    }


# --------------------------------------------------------------------------- #
# Strategy 56 — Statistical: SMA + BB middle
# --------------------------------------------------------------------------- #
def _stat_sma_bb_middle(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Stat_SMA_BB_Middle",
        "description": "LONG when close < SMA(20) AND close < BB middle; exit when close > BB middle.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 20}},
            {"name": "bb_middle", "params": {"window": 20, "num_std": 2.0}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
            make_condition(field_operand("close"), "<", indicator_operand("bb_middle_20_2")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), ">", indicator_operand("bb_middle_20_2")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.02),
    }


# --------------------------------------------------------------------------- #
# Strategy 57 — Other: Prior low bounce
# --------------------------------------------------------------------------- #
def _other_prev_low_bounce(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Other_PrevLow_Bounce",
        "description": "LONG when close > prev_low AND close > open; exit when close < open.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", field_operand("prev_low")),
            make_condition(field_operand("close"), ">", field_operand("open")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", field_operand("open")),
        "allow_long": True,
        "position_sizing": _size(0.20),
        "risk": _risk(stop=0.015, take=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 58 — Other: Open above prev close
# --------------------------------------------------------------------------- #
def _other_open_above_close(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Other_OpenAbove_Close",
        "description": "LONG when open > prev_close AND close > open; exit when close < open.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [],
        "entry": logic("AND",
            make_condition(field_operand("open"), ">", field_operand("prev_close")),
            make_condition(field_operand("close"), ">", field_operand("open")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", field_operand("open")),
        "allow_long": True,
        "position_sizing": _size(0.20),
        "risk": _risk(stop=0.015, take=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 59 — Other: Volume decline + price rise
# --------------------------------------------------------------------------- #
def _other_volume_decline_price_rise(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Other_VolumeDecline_PriceRise",
        "description": "LONG when volume < volume SMA(20) AND close > SMA(10); exit on close < SMA(10).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "volume_sma", "params": {"window": 20}},
            {"name": "sma", "params": {"window": 10}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("volume"), "<", indicator_operand("volume_sma_20")),
            make_condition(field_operand("close"), ">", indicator_operand("sma_10")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_10")),
        "allow_long": True,
        "position_sizing": _size(0.20),
        "risk": _risk(stop=0.02, take=0.04),
    }


# --------------------------------------------------------------------------- #
# Strategy 60 — Trend / Momentum: EMA + MACD
# --------------------------------------------------------------------------- #
def _trend_ema_macd(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Trend_EMA_MACD",
        "description": "LONG when close > EMA(20) AND MACD > signal; exit on close < EMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 20}},
            {"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"name": "macd_signal", "params": {"fast": 12, "slow": 26, "signal": 9}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("ema_20")),
            make_condition(indicator_operand("macd_12_26_9"), ">", indicator_operand("macd_signal_12_26_9")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("ema_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 61 — Trend / Momentum: SMA + EMA + momentum
# --------------------------------------------------------------------------- #
def _trend_sma_ema_momentum(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Trend_SMA_EMA_Momentum",
        "description": "LONG when EMA(10) > SMA(50) AND momentum(10) > 0; exit on EMA(10) < SMA(50).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 10}},
            {"name": "sma", "params": {"window": 50}},
            {"name": "momentum", "params": {"window": 10}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("ema_10"), ">", indicator_operand("sma_50")),
            make_condition(indicator_operand("momentum_10"), ">", const_operand(0.0)),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("ema_10"), "<", indicator_operand("sma_50")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 62 — Trend / Momentum: Price above EMA + BB
# --------------------------------------------------------------------------- #
def _trend_price_ema_bb(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Trend_Price_EMA_BB",
        "description": "LONG when close > EMA(20) AND close > BB middle; exit on close < EMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 20}},
            {"name": "bb_middle", "params": {"window": 20, "num_std": 2.0}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("ema_20")),
            make_condition(field_operand("close"), ">", indicator_operand("bb_middle_20_2")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("ema_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 63 — Mean Reversion: RSI + BB upper
# --------------------------------------------------------------------------- #
def _meanrev_rsi_bb_upper(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_MeanRev_RSI_BB_Upper",
        "description": "LONG when RSI(14) < 35 AND close < BB upper; exit when RSI(14) > 55.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "rsi", "params": {"window": 14}},
            {"name": "bb_upper", "params": {"window": 20, "num_std": 2.0}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("rsi_14"), "<", const_operand(35.0)),
            make_condition(field_operand("close"), "<", indicator_operand("bb_upper_20_2")),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("rsi_14"), ">", const_operand(55.0)),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 64 — Mean Reversion: BB middle + volume decline
# --------------------------------------------------------------------------- #
def _meanrev_bb_volume_decline(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_MeanRev_BB_VolumeDecline",
        "description": "LONG when close < BB middle AND volume < volume SMA(20); exit on close > BB middle.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "bb_middle", "params": {"window": 20, "num_std": 2.0}},
            {"name": "volume_sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), "<", indicator_operand("bb_middle_20_2")),
            make_condition(field_operand("volume"), "<", indicator_operand("volume_sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), ">", indicator_operand("bb_middle_20_2")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.02),
    }


# --------------------------------------------------------------------------- #
# Strategy 65 — Breakout / Volatility: Donchian + BB upper
# --------------------------------------------------------------------------- #
def _breakout_donchian_bb(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Breakout_Donchian_BB",
        "description": "LONG when close > Donchian upper(20) AND close > BB upper; exit on close < SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "donchian_upper", "params": {"window": 20}},
            {"name": "bb_upper", "params": {"window": 20, "num_std": 2.0}},
            {"name": "sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("donchian_upper_20")),
            make_condition(field_operand("close"), ">", indicator_operand("bb_upper_20_2")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 66 — Breakout / Volatility: ATR + SMA
# --------------------------------------------------------------------------- #
def _breakout_atr_sma(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Breakout_ATR_SMA",
        "description": "LONG when close > SMA(20) AND ATR(14) > 1.5; exit on close < SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 20}},
            {"name": "atr", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
            make_condition(indicator_operand("atr_14"), ">", const_operand(1.5)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 67 — Volume / Market Structure: Volume + RSI
# --------------------------------------------------------------------------- #
def _volume_rsi(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Volume_RSI",
        "description": "LONG when volume > volume SMA(20) AND RSI(14) > 50; exit on volume < volume SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "volume_sma", "params": {"window": 20}},
            {"name": "rsi", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
            make_condition(indicator_operand("rsi_14"), ">", const_operand(50.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("volume"), "<", indicator_operand("volume_sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 68 — Volume / Market Structure: Volume surge + RSI oversold
# --------------------------------------------------------------------------- #
def _volume_surge_rsi_oversold(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Volume_Surge_RSI_Oversold",
        "description": "LONG when volume > 2*volume SMA(20) AND RSI(14) < 30; exit on RSI(14) > 50.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "volume_sma", "params": {"window": 20}},
            {"name": "rsi", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("volume"), ">", const_operand(2.0)),
            make_condition(indicator_operand("rsi_14"), "<", const_operand(30.0)),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("rsi_14"), ">", const_operand(50.0)),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.02),
    }


# --------------------------------------------------------------------------- #
# Strategy 69 — Regime-Aware: Low ATR + SMA
# --------------------------------------------------------------------------- #
def _regime_low_atr_sma(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Regime_LowATR_SMA",
        "description": "LONG when close > SMA(20) AND ATR(14) < 1.0; exit on close < SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 20}},
            {"name": "atr", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
            make_condition(indicator_operand("atr_14"), "<", const_operand(1.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.02),
    }


# --------------------------------------------------------------------------- #
# Strategy 70 — Regime-Aware: High ATR + BB
# --------------------------------------------------------------------------- #
def _regime_high_atr_bb(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Regime_HighATR_BB",
        "description": "LONG when close > BB upper AND ATR(14) > 2.0; exit on close < BB middle.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "bb_upper", "params": {"window": 20, "num_std": 2.0}},
            {"name": "bb_middle", "params": {"window": 20, "num_std": 2.0}},
            {"name": "atr", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("bb_upper_20_2")),
            make_condition(indicator_operand("atr_14"), ">", const_operand(2.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("bb_middle_20_2")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.035),
    }


# --------------------------------------------------------------------------- #
# Strategy 71 — Trend / Momentum: Slow EMA uptrend with momentum filter
# --------------------------------------------------------------------------- #
def _trend_ema_slow_uptrend(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Trend_EMA_SlowUptrend",
        "description": "LONG when close > EMA(50) AND EMA(20) > EMA(50) AND momentum(10) > 0; exit on close < EMA(50).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 50}},
            {"name": "ema", "params": {"window": 20}},
            {"name": "momentum", "params": {"window": 10}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("ema_50")),
            make_condition(indicator_operand("ema_20"), ">", indicator_operand("ema_50")),
            make_condition(indicator_operand("momentum_10"), ">", const_operand(0.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("ema_50")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 72 — Trend / Momentum: Price above slow EMA + RSI filter
# --------------------------------------------------------------------------- #
def _trend_price_above_slow_ema(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Trend_Price_Above_SlowEMA",
        "description": "LONG when close > EMA(50) AND RSI(14) > 50; exit on close < EMA(50).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 50}},
            {"name": "rsi", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("ema_50")),
            make_condition(indicator_operand("rsi_14"), ">", const_operand(50.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("ema_50")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 73 — Trend / Momentum: EMA fast/slow cross + volume confirmation
# --------------------------------------------------------------------------- #
def _trend_ema_fast_slow_volume(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Trend_EMA_FastSlow_Volume",
        "description": "LONG when EMA(12) > EMA(26) AND volume > volume SMA(20); exit on EMA(12) < EMA(26).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 12}},
            {"name": "ema", "params": {"window": 26}},
            {"name": "volume_sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("ema_12"), ">", indicator_operand("ema_26")),
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("ema_12"), "<", indicator_operand("ema_26")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 74 — Trend / Momentum: Momentum + volume spike
# --------------------------------------------------------------------------- #
def _trend_momentum_volume_spike(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Trend_Momentum_VolumeSpike",
        "description": "LONG when momentum(20) > 3% AND volume > 2*volume SMA(20); exit on momentum < 0.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "momentum", "params": {"window": 20}},
            {"name": "volume_sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("momentum_20"), ">", const_operand(0.03)),
            make_condition(field_operand("volume"), ">", const_operand(2.0)),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("momentum_20"), "<", const_operand(0.0)),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.04, take=0.12),
    }


# --------------------------------------------------------------------------- #
# Strategy 75 — Trend / Momentum: SMA trend + RSI filter
# --------------------------------------------------------------------------- #
def _trend_sma_rsi_filter(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Trend_SMA_RSI_Filter",
        "description": "LONG when close > SMA(50) AND RSI(14) > 55; exit on close < SMA(50).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 50}},
            {"name": "rsi", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("sma_50")),
            make_condition(indicator_operand("rsi_14"), ">", const_operand(55.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_50")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 76 — Trend / Momentum: EMA + MACD + volume
# --------------------------------------------------------------------------- #
def _trend_ema_macd_volume(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Trend_EMA_MACD_Volume",
        "description": "LONG when close > EMA(20) AND MACD > signal AND volume > volume SMA(20); exit on close < EMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 20}},
            {"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"name": "macd_signal", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"name": "volume_sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("ema_20")),
            make_condition(indicator_operand("macd_12_26_9"), ">", indicator_operand("macd_signal_12_26_9")),
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("ema_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 77 — Mean Reversion: RSI + BB lower + momentum
# --------------------------------------------------------------------------- #
def _meanrev_rsi_bb_bounce(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_MeanRev_RSI_BB_Bounce",
        "description": "LONG when RSI(14) < 28 AND close < BB lower AND momentum(5) > 0; exit on RSI(14) > 50.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "rsi", "params": {"window": 14}},
            {"name": "bb_lower", "params": {"window": 20, "num_std": 2.0}},
            {"name": "momentum", "params": {"window": 5}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("rsi_14"), "<", const_operand(28.0)),
            make_condition(field_operand("close"), "<", indicator_operand("bb_lower_20_2")),
            make_condition(indicator_operand("momentum_5"), ">", const_operand(0.0)),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("rsi_14"), ">", const_operand(50.0)),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.02),
    }


# --------------------------------------------------------------------------- #
# Strategy 78 — Mean Reversion: SMA support + RSI filter
# --------------------------------------------------------------------------- #
def _meanrev_sma_support(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_MeanRev_SMA_Support",
        "description": "LONG when close > SMA(20) AND RSI(14) < 35; exit when RSI(14) > 55.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 20}},
            {"name": "rsi", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
            make_condition(indicator_operand("rsi_14"), "<", const_operand(35.0)),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("rsi_14"), ">", const_operand(55.0)),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.02),
    }


# --------------------------------------------------------------------------- #
# Strategy 79 — Mean Reversion: EMA bounce + RSI oversold
# --------------------------------------------------------------------------- #
def _meanrev_ema_bounce(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_MeanRev_EMA_Bounce",
        "description": "LONG when close > EMA(20) AND RSI(14) < 25; exit when RSI(14) > 55.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 20}},
            {"name": "rsi", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("ema_20")),
            make_condition(indicator_operand("rsi_14"), "<", const_operand(25.0)),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("rsi_14"), ">", const_operand(55.0)),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.02),
    }


# --------------------------------------------------------------------------- #
# Strategy 80 — Mean Reversion: BB middle + volume decline
# --------------------------------------------------------------------------- #
def _meanrev_bb_middle_decline(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_MeanRev_BB_Middle_Decline",
        "description": "LONG when close < BB middle AND volume < volume SMA(20) AND RSI(14) < 40; exit on close > BB middle.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "bb_middle", "params": {"window": 20, "num_std": 2.0}},
            {"name": "volume_sma", "params": {"window": 20}},
            {"name": "rsi", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), "<", indicator_operand("bb_middle_20_2")),
            make_condition(field_operand("volume"), "<", indicator_operand("volume_sma_20")),
            make_condition(indicator_operand("rsi_14"), "<", const_operand(40.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), ">", indicator_operand("bb_middle_20_2")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.02),
    }


# --------------------------------------------------------------------------- #
# Strategy 81 — Mean Reversion: Double bounce
# --------------------------------------------------------------------------- #
def _meanrev_double_bounce(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_MeanRev_Double_Bounce",
        "description": "LONG when close > prev_low AND close > open AND RSI(14) < 30; exit when close < open.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "rsi", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", field_operand("prev_low")),
            make_condition(field_operand("close"), ">", field_operand("open")),
            make_condition(indicator_operand("rsi_14"), "<", const_operand(30.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", field_operand("open")),
        "allow_long": True,
        "position_sizing": _size(0.20),
        "risk": _risk(stop=0.015, take=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 82 — Breakout / Volatility: Donchian + volume + SMA
# --------------------------------------------------------------------------- #
def _breakout_donchian_volume_sma(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Breakout_Donchian_Volume_SMA",
        "description": "LONG when close > Donchian upper(20) AND volume > volume SMA(20) AND close > SMA(20); exit on close < SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "donchian_upper", "params": {"window": 20}},
            {"name": "volume_sma", "params": {"window": 20}},
            {"name": "sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("donchian_upper_20")),
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
            make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 83 — Breakout / Volatility: BB upper + volume
# --------------------------------------------------------------------------- #
def _breakout_bb_upper_volume(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Breakout_BB_Upper_Volume",
        "description": "LONG when close > BB upper AND volume > 2*volume SMA(20); exit on close < BB middle.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "bb_upper", "params": {"window": 20, "num_std": 2.0}},
            {"name": "bb_middle", "params": {"window": 20, "num_std": 2.0}},
            {"name": "volume_sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("bb_upper_20_2")),
            make_condition(field_operand("volume"), ">", const_operand(2.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("bb_middle_20_2")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 84 — Breakout / Volatility: Range breakout + volume
# --------------------------------------------------------------------------- #
def _breakout_range_volume(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Breakout_Range_Volume",
        "description": "LONG when close > highest high of prior 20 bars AND volume > volume SMA(20); exit on close < lowest low of prior 10 bars.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "donchian_upper", "params": {"window": 20}},
            {"name": "donchian_lower", "params": {"window": 10}},
            {"name": "volume_sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("donchian_upper_20")),
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("donchian_lower_10")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.04),
    }


# --------------------------------------------------------------------------- #
# Strategy 85 — Breakout / Volatility: SMA breakout + momentum
# --------------------------------------------------------------------------- #
def _breakout_sma_momentum(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Breakout_SMA_Momentum",
        "description": "LONG when close > SMA(20) AND momentum(10) > 1% AND ATR(14) > 1.0; exit on close < SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 20}},
            {"name": "momentum", "params": {"window": 10}},
            {"name": "atr", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
            make_condition(indicator_operand("momentum_10"), ">", const_operand(0.01)),
            make_condition(indicator_operand("atr_14"), ">", const_operand(1.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 86 — Breakout / Volatility: Triple confirmation
# --------------------------------------------------------------------------- #
def _breakout_triple_confirmation(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Breakout_Triple_Confirmation",
        "description": "LONG when close > Donchian upper(20) AND close > BB upper AND volume > volume SMA(20); exit on close < SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "donchian_upper", "params": {"window": 20}},
            {"name": "bb_upper", "params": {"window": 20, "num_std": 2.0}},
            {"name": "volume_sma", "params": {"window": 20}},
            {"name": "sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("donchian_upper_20")),
            make_condition(field_operand("close"), ">", indicator_operand("bb_upper_20_2")),
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 87 — Volume / Market Structure: Volume price trend
# --------------------------------------------------------------------------- #
def _volume_volume_price_trend(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Volume_VolumePriceTrend",
        "description": "LONG when volume > volume SMA(20) AND close > SMA(20) AND EMA(10) > EMA(20); exit on close < SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "volume_sma", "params": {"window": 20}},
            {"name": "sma", "params": {"window": 20}},
            {"name": "ema", "params": {"window": 10}},
            {"name": "ema", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
            make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
            make_condition(indicator_operand("ema_10"), ">", indicator_operand("ema_20")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 88 — Volume / Market Structure: Accumulation
# --------------------------------------------------------------------------- #
def _volume_accumulation(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Volume_Accumulation",
        "description": "LONG when volume > volume SMA(20) AND close > open AND close > SMA(10); exit on close < SMA(10).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "volume_sma", "params": {"window": 20}},
            {"name": "sma", "params": {"window": 10}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
            make_condition(field_operand("close"), ">", field_operand("open")),
            make_condition(field_operand("close"), ">", indicator_operand("sma_10")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_10")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.02),
    }


# --------------------------------------------------------------------------- #
# Strategy 89 — Volume / Market Structure: Volume climax
# --------------------------------------------------------------------------- #
def _volume_climax(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Volume_Climax",
        "description": "LONG when volume > 2.5*volume SMA(20) AND close > 99% of high AND RSI(14) > 60; exit on volume < volume SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "volume_sma", "params": {"window": 20}},
            {"name": "rsi", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("volume"), ">", const_operand(2.5)),
            make_condition(field_operand("close"), ">", const_operand(0.99)),
            make_condition(indicator_operand("rsi_14"), ">", const_operand(60.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("volume"), "<", indicator_operand("volume_sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.20),
        "risk": _risk(stop=0.025, take=0.05),
    }


# --------------------------------------------------------------------------- #
# Strategy 90 — Volume / Market Structure: Volume trend confirmation
# --------------------------------------------------------------------------- #
def _volume_trend_confirmation(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Volume_Trend_Confirmation",
        "description": "LONG when volume > volume SMA(20) AND close > EMA(20) AND momentum(10) > 0; exit on close < EMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "volume_sma", "params": {"window": 20}},
            {"name": "ema", "params": {"window": 20}},
            {"name": "momentum", "params": {"window": 10}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
            make_condition(field_operand("close"), ">", indicator_operand("ema_20")),
            make_condition(indicator_operand("momentum_10"), ">", const_operand(0.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("ema_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 91 — Cross-Sectional: BB upper + ATR + RSI
# --------------------------------------------------------------------------- #
def _cross_bb_atr_rsi(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Cross_BB_ATR_RSI",
        "description": "LONG when close > BB upper AND ATR(14) > 1.5 AND RSI(14) > 50; exit on close < BB middle.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "bb_upper", "params": {"window": 20, "num_std": 2.0}},
            {"name": "bb_middle", "params": {"window": 20, "num_std": 2.0}},
            {"name": "atr", "params": {"window": 14}},
            {"name": "rsi", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("bb_upper_20_2")),
            make_condition(indicator_operand("atr_14"), ">", const_operand(1.5)),
            make_condition(indicator_operand("rsi_14"), ">", const_operand(50.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("bb_middle_20_2")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 92 — Cross-Sectional: MACD + BB middle + volume
# --------------------------------------------------------------------------- #
def _cross_macd_bb_volume(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Cross_MACD_BB_Volume",
        "description": "LONG when MACD > signal AND close > BB middle AND volume > volume SMA(20); exit on MACD < signal.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"name": "macd_signal", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"name": "bb_middle", "params": {"window": 20, "num_std": 2.0}},
            {"name": "volume_sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("macd_12_26_9"), ">", indicator_operand("macd_signal_12_26_9")),
            make_condition(field_operand("close"), ">", indicator_operand("bb_middle_20_2")),
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("macd_12_26_9"), "<", indicator_operand("macd_signal_12_26_9")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 93 — Cross-Sectional: EMA + ATR + momentum
# --------------------------------------------------------------------------- #
def _cross_ema_atr_momentum(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Cross_EMA_ATR_Momentum",
        "description": "LONG when close > EMA(20) AND ATR(14) > 1.0 AND momentum(10) > 0; exit on close < EMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 20}},
            {"name": "atr", "params": {"window": 14}},
            {"name": "momentum", "params": {"window": 10}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("ema_20")),
            make_condition(indicator_operand("atr_14"), ">", const_operand(1.0)),
            make_condition(indicator_operand("momentum_10"), ">", const_operand(0.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("ema_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 94 — Statistical: RSI + BB middle + ATR
# --------------------------------------------------------------------------- #
def _stat_rsi_bb_atr(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Stat_RSI_BB_ATR",
        "description": "LONG when RSI(14) < 30 AND close < BB middle AND ATR(14) < 1.5; exit on RSI(14) > 50.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "rsi", "params": {"window": 14}},
            {"name": "bb_middle", "params": {"window": 20, "num_std": 2.0}},
            {"name": "atr", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("rsi_14"), "<", const_operand(30.0)),
            make_condition(field_operand("close"), "<", indicator_operand("bb_middle_20_2")),
            make_condition(indicator_operand("atr_14"), "<", const_operand(1.5)),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("rsi_14"), ">", const_operand(50.0)),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.02),
    }


# --------------------------------------------------------------------------- #
# Strategy 95 — Statistical: SMA + momentum + BB
# --------------------------------------------------------------------------- #
def _stat_sma_momentum_bb(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Stat_SMA_Momentum_BB",
        "description": "LONG when close < SMA(20) AND momentum(10) < 0 AND close > BB lower; exit on close > SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 20}},
            {"name": "momentum", "params": {"window": 10}},
            {"name": "bb_lower", "params": {"window": 20, "num_std": 2.0}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
            make_condition(indicator_operand("momentum_10"), "<", const_operand(0.0)),
            make_condition(field_operand("close"), ">", indicator_operand("bb_lower_20_2")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.02),
    }


# --------------------------------------------------------------------------- #
# Strategy 96 — Other: Close near high + open near low
# --------------------------------------------------------------------------- #
def _other_close_high_open_low(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Other_CloseHigh_OpenLow",
        "description": "LONG when close > 95% of high AND open < 5% of low; exit when close < open.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", const_operand(0.95)),
            make_condition(field_operand("open"), "<", const_operand(0.05)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", field_operand("open")),
        "allow_long": True,
        "position_sizing": _size(0.20),
        "risk": _risk(stop=0.02, take=0.04),
    }


# --------------------------------------------------------------------------- #
# Strategy 97 — Other: Three white soldiers
# --------------------------------------------------------------------------- #
def _other_three_white_soldiers(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Other_ThreeWhiteSoldiers",
        "description": "LONG when close > open AND close > prev_close AND prev_close > prev_open; exit when close < open.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", field_operand("open")),
            make_condition(field_operand("close"), ">", field_operand("prev_close")),
            make_condition(field_operand("prev_close"), ">", field_operand("open")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", field_operand("open")),
        "allow_long": True,
        "position_sizing": _size(0.20),
        "risk": _risk(stop=0.015, take=0.03),
    }


# --------------------------------------------------------------------------- #
# Strategy 98 — Other: Volume + price above both MAs
# --------------------------------------------------------------------------- #
def _other_volume_above_mas(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Other_Volume_Above_MAs",
        "description": "LONG when volume > volume SMA(20) AND close > SMA(20) AND close > EMA(20); exit on close < SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "volume_sma", "params": {"window": 20}},
            {"name": "sma", "params": {"window": 20}},
            {"name": "ema", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
            make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
            make_condition(field_operand("close"), ">", indicator_operand("ema_20")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 99 — Trend / Momentum: EMA + RSI + ATR
# --------------------------------------------------------------------------- #
def _trend_ema_rsi_atr(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Trend_EMA_RSI_ATR",
        "description": "LONG when close > EMA(20) AND RSI(14) > 50 AND ATR(14) > 1.0; exit on close < EMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 20}},
            {"name": "rsi", "params": {"window": 14}},
            {"name": "atr", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("ema_20")),
            make_condition(indicator_operand("rsi_14"), ">", const_operand(50.0)),
            make_condition(indicator_operand("atr_14"), ">", const_operand(1.0)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("ema_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy 100 — Trend / Momentum: Triple EMA + volume
# --------------------------------------------------------------------------- #
def _trend_triple_ema_volume(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_Trend_Triple_EMA_Volume",
        "description": "LONG when EMA(5) > EMA(20) AND EMA(20) > EMA(50) AND volume > volume SMA(20); exit on close < EMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 5}},
            {"name": "ema", "params": {"window": 20}},
            {"name": "ema", "params": {"window": 50}},
            {"name": "volume_sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("ema_5"), ">", indicator_operand("ema_20")),
            make_condition(indicator_operand("ema_20"), ">", indicator_operand("ema_50")),
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("ema_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# New strategies (Phase 24 expansion)
# --------------------------------------------------------------------------- #

def _new_macd_positive_trend(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_New_MACD_Positive_Trend",
        "description": "LONG when close > SMA(20) AND MACD > 0 AND MACD > signal; exit on close < SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 20}},
            {"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"name": "macd_signal", "params": {"fast": 12, "slow": 26, "signal": 9}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
            make_condition(indicator_operand("macd_12_26_9"), ">", const_operand(0.0)),
            make_condition(indicator_operand("macd_12_26_9"), ">", indicator_operand("macd_signal_12_26_9")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


def _new_rsi_bb_upper_bounce(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_New_RSI_BB_Upper_Bounce",
        "description": "LONG when RSI(14) < 35 AND close < BB upper AND close > SMA(20); exit when RSI(14) > 55.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "rsi", "params": {"window": 14}},
            {"name": "bb_upper", "params": {"window": 20, "num_std": 2.0}},
            {"name": "sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("rsi_14"), "<", const_operand(35.0)),
            make_condition(field_operand("close"), "<", indicator_operand("bb_upper_20_2")),
            make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("rsi_14"), ">", const_operand(55.0)),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


def _new_volume_spike_atr_trend(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_New_Volume_Spike_ATR_Trend",
        "description": "LONG when volume > 2*volume SMA(20) AND close > EMA(20) AND ATR(14) > 1.5; exit on close < EMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "volume_sma", "params": {"window": 20}},
            {"name": "ema", "params": {"window": 20}},
            {"name": "atr", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("volume"), ">", const_operand(2.0)),
            make_condition(field_operand("close"), ">", indicator_operand("ema_20")),
            make_condition(indicator_operand("atr_14"), ">", const_operand(1.5)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("ema_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


def _new_donchian_bb_breakout(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_New_Donchian_BB_Breakout",
        "description": "LONG when close > Donchian upper(20) AND close > BB upper AND volume > volume SMA(20); exit on close < SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "donchian_upper", "params": {"window": 20}},
            {"name": "bb_upper", "params": {"window": 20, "num_std": 2.0}},
            {"name": "volume_sma", "params": {"window": 20}},
            {"name": "sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("donchian_upper_20")),
            make_condition(field_operand("close"), ">", indicator_operand("bb_upper_20_2")),
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


def _new_ema_rsi_momentum(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_New_EMA_RSI_Momentum",
        "description": "LONG when close > EMA(20) AND RSI(14) > 50 AND momentum(10) > 0.01; exit on close < EMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 20}},
            {"name": "rsi", "params": {"window": 14}},
            {"name": "momentum", "params": {"window": 10}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("ema_20")),
            make_condition(indicator_operand("rsi_14"), ">", const_operand(50.0)),
            make_condition(indicator_operand("momentum_10"), ">", const_operand(0.01)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("ema_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


def _new_sma_rsi_volume_trend(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_New_SMA_RSI_Volume_Trend",
        "description": "LONG when close > SMA(50) AND RSI(14) > 55 AND volume > volume SMA(20); exit on close < SMA(50).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 50}},
            {"name": "rsi", "params": {"window": 14}},
            {"name": "volume_sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("sma_50")),
            make_condition(indicator_operand("rsi_14"), ">", const_operand(55.0)),
            make_condition(field_operand("volume"), ">", indicator_operand("volume_sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_50")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03),
    }


def _new_bb_middle_meanrev(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_New_BB_Middle_MeanRev",
        "description": "LONG when close < BB middle AND RSI(14) < 35 AND volume < volume SMA(20); exit on close > BB middle.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "bb_middle", "params": {"window": 20, "num_std": 2.0}},
            {"name": "rsi", "params": {"window": 14}},
            {"name": "volume_sma", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), "<", indicator_operand("bb_middle_20_2")),
            make_condition(indicator_operand("rsi_14"), "<", const_operand(35.0)),
            make_condition(field_operand("volume"), "<", indicator_operand("volume_sma_20")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), ">", indicator_operand("bb_middle_20_2")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.02),
    }


def _new_macd_histogram_trend(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_New_MACD_Histogram_Trend",
        "description": "LONG when MACD histogram > 0 AND MACD > signal AND close > EMA(20); exit on MACD histogram < 0.",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "macd_histogram", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"name": "macd_signal", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"name": "ema", "params": {"window": 20}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("macd_histogram_12_26_9"), ">", const_operand(0.0)),
            make_condition(indicator_operand("macd_12_26_9"), ">", indicator_operand("macd_signal_12_26_9")),
            make_condition(field_operand("close"), ">", indicator_operand("ema_20")),
        ),
        "entry_short": None,
        "exit": make_condition(indicator_operand("macd_histogram_12_26_9"), "<", const_operand(0.0)),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


def _new_atr_trend_candle(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_New_ATR_Trend_Candle",
        "description": "LONG when close > SMA(20) AND ATR(14) > 1.0 AND close > open; exit on close < SMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "sma", "params": {"window": 20}},
            {"name": "atr", "params": {"window": 14}},
        ],
        "entry": logic("AND",
            make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
            make_condition(indicator_operand("atr_14"), ">", const_operand(1.0)),
            make_condition(field_operand("close"), ">", field_operand("open")),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


def _new_triple_ema_momentum(symbol: str, timeframe: str) -> dict:
    return {
        "name": "Phase23_New_Triple_EMA_Momentum",
        "description": "LONG when EMA(5) > EMA(20) AND EMA(20) > EMA(50) AND momentum(10) > 0.01; exit on close < EMA(20).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 5}},
            {"name": "ema", "params": {"window": 20}},
            {"name": "ema", "params": {"window": 50}},
            {"name": "momentum", "params": {"window": 10}},
        ],
        "entry": logic("AND",
            make_condition(indicator_operand("ema_5"), ">", indicator_operand("ema_20")),
            make_condition(indicator_operand("ema_20"), ">", indicator_operand("ema_50")),
            make_condition(indicator_operand("momentum_10"), ">", const_operand(0.01)),
        ),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("ema_20")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.025),
    }


# --------------------------------------------------------------------------- #
# Strategy — NIFTY Options SMA5 Trend (options-capable, index-based)
# --------------------------------------------------------------------------- #
def _nifty_options_sma5_trend(symbol: str, timeframe: str) -> dict:
    return {
        "name": "NIFTY Options SMA5 Trend",
        "description": "LONG when close > SMA(5); exit when close < SMA(5). Applied to NIFTY index via option contracts (CE/PE).",
        "symbol": symbol, "timeframe": timeframe,
        "indicators": [{"name": "sma", "params": {"window": 5}}],
        "entry": make_condition(field_operand("close"), ">", indicator_operand("sma_5")),
        "entry_short": None,
        "exit": make_condition(field_operand("close"), "<", indicator_operand("sma_5")),
        "allow_long": True,
        "position_sizing": _size(0.25),
        "risk": _risk(stop=0.03, take=0.06),
    }


# --------------------------------------------------------------------------- #
# Universe assembly
# --------------------------------------------------------------------------- #
def build_default_universe() -> dict[str, UniverseCandidate]:
    return {
        "nifty-sma5": UniverseCandidate(
            candidate_id="nifty-sma5",
            strategy_family=StrategyFamily.TREND_MOMENTUM,
            strategy_name="NIFTY Options SMA5 Trend",
            description="LONG when close > SMA(5); exit when close < SMA(5). Applied to NIFTY index via option contracts (CE/PE).",
            hypothesis="Short-term SMA(5) trend persistence on the NIFTY index, traded via option contracts",
            required_features=["sma"],
            timeframe="1d",
            supported_instruments=["NSE:NIFTY"],
            supported_sessions=["regular"],
            parameter_schema={"window": {"type": "int", "min": 3, "max": 20}},
            default_parameters={"window": 5},
            parameter_ranges={"window": (3, 20)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_nifty_options_sma5_trend,
            options_strategy=True,
        ),
        "trend-ema-fast-slow": UniverseCandidate(
            candidate_id="trend-ema-fast-slow",
            strategy_family=StrategyFamily.TREND_MOMENTUM,
            strategy_name="EMA Fast/Slow Cross",
            description="LONG when EMA(12) > EMA(26); exit on cross below.",
            hypothesis="Trend persistence via dual EMA crossover",
            required_features=["ema"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"fast": {"type": "int", "min": 5, "max": 50}, "slow": {"type": "int", "min": 20, "max": 200}},
            default_parameters={"fast": 12, "slow": 26},
            parameter_ranges={"fast": (5, 20), "slow": (20, 100)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_trend_ema_fast_slow,
        ),
        "meanrev-rsi-oversold": UniverseCandidate(
            candidate_id="meanrev-rsi-oversold",
            strategy_family=StrategyFamily.MEAN_REVERSION,
            strategy_name="RSI Oversold Bounce",
            description="LONG when RSI(14) < 30; exit when RSI(14) > 55.",
            hypothesis="Oversold RSI reverts toward the mean on short horizons",
            required_features=["rsi"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"window": {"type": "int", "min": 2, "max": 30}, "entry_threshold": {"type": "float", "min": 10, "max": 40}, "exit_threshold": {"type": "float", "min": 50, "max": 80}},
            default_parameters={"window": 14, "entry_threshold": 30.0, "exit_threshold": 55.0},
            parameter_ranges={"window": (2, 30), "entry_threshold": (10, 40), "exit_threshold": (50, 80)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_meanrev_rsi_oversold,
        ),
        "breakout-nbar": UniverseCandidate(
            candidate_id="breakout-nbar",
            strategy_family=StrategyFamily.BREAKOUT_VOLATILITY,
            strategy_name="N-Bar High Breakout",
            description="LONG when close > highest high of prior 20 bars; exit on 10-bar low.",
            hypothesis="Breakout of prior N-bar range predicts continued upward movement",
            required_features=["donchian_upper", "donchian_lower"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"lookback": {"type": "int", "min": 10, "max": 60}},
            default_parameters={"lookback": 20},
            parameter_ranges={"lookback": (10, 60)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_breakout_nbar,
        ),
        "volume-confirmation": UniverseCandidate(
            candidate_id="volume-confirmation",
            strategy_family=StrategyFamily.VOLUME_MARKET_STRUCTURE,
            strategy_name="Volume-Confirmed Trend",
            description="LONG when close > SMA(20) AND volume > volume SMA(20); exit on close < SMA(20).",
            hypothesis="Price moves accompanied by above-average volume are more persistent",
            required_features=["sma", "volume_sma"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"sma_window": {"type": "int", "min": 10, "max": 50}, "volume_window": {"type": "int", "min": 10, "max": 50}},
            default_parameters={"sma_window": 20, "volume_window": 20},
            parameter_ranges={"sma_window": (10, 50), "volume_window": (10, 50)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_volume_confirmation,
        ),
        "mtf-htf-trend-ltf-entry": UniverseCandidate(
            candidate_id="mtf-htf-trend-ltf-entry",
            strategy_family=StrategyFamily.MULTI_TIMEFRAME,
            strategy_name="Multi-Timeframe Trend Entry",
            description="LONG when close > SMA(50) AND EMA(10) > SMA(20); exit on close < SMA(50).",
            hypothesis="Higher-timeframe trend filter combined with lower-timeframe entry signal improves risk-adjusted returns",
            required_features=["sma", "ema"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"sma_slow": {"type": "int", "min": 50, "max": 200}, "ema_fast": {"type": "int", "min": 5, "max": 20}, "sma_mid": {"type": "int", "min": 10, "max": 50}},
            default_parameters={"sma_slow": 50, "ema_fast": 10, "sma_mid": 20},
            parameter_ranges={"sma_slow": (50, 200), "ema_fast": (5, 20), "sma_mid": (10, 50)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_mtf_htf_trend_ltf_entry,
        ),
        "trend-ema-triple": UniverseCandidate(
            candidate_id="trend-ema-triple",
            strategy_family=StrategyFamily.TREND_MOMENTUM,
            strategy_name="EMA Triple Stack",
            description="LONG when EMA(5) > EMA(20) > EMA(50); exit on close < EMA(20).",
            hypothesis="Multi-timeframe trend alignment reduces whipsaws",
            required_features=["ema"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"fast": {"type": "int", "min": 5, "max": 20}, "mid": {"type": "int", "min": 20, "max": 50}, "slow": {"type": "int", "min": 50, "max": 200}},
            default_parameters={"fast": 5, "mid": 20, "slow": 50},
            parameter_ranges={"fast": (5, 20), "mid": (20, 50), "slow": (50, 200)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_trend_ema_triple,
        ),
        "trend-price-momentum": UniverseCandidate(
            candidate_id="trend-price-momentum",
            strategy_family=StrategyFamily.TREND_MOMENTUM,
            strategy_name="Price Momentum",
            description="LONG when 20-bar momentum > 5%; exit when momentum < 0.",
            hypothesis="Momentum continuation over medium horizons",
            required_features=["momentum"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"window": {"type": "int", "min": 10, "max": 60}, "threshold": {"type": "float", "min": 0.01, "max": 0.10}},
            default_parameters={"window": 20, "threshold": 0.05},
            parameter_ranges={"window": (10, 60), "threshold": (0.01, 0.10)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_trend_price_momentum,
        ),
        "breakout-atr": UniverseCandidate(
            candidate_id="breakout-atr",
            strategy_family=StrategyFamily.BREAKOUT_VOLATILITY,
            strategy_name="ATR Volatility Breakout",
            description="LONG when close > SMA(20) + 1.5*ATR(14); exit on close < SMA(20).",
            hypothesis="Volatility-scaled breakouts capture trend persistence",
            required_features=["sma", "atr"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"sma_window": {"type": "int", "min": 10, "max": 50}, "atr_window": {"type": "int", "min": 10, "max": 30}},
            default_parameters={"sma_window": 20, "atr_window": 14},
            parameter_ranges={"sma_window": (10, 50), "atr_window": (10, 30)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_breakout_atr,
        ),
        "breakout-range": UniverseCandidate(
            candidate_id="breakout-range",
            strategy_family=StrategyFamily.BREAKOUT_VOLATILITY,
            strategy_name="Range Breakout",
            description="LONG when close > high of prior 20 bars; exit on close < low of prior 10 bars.",
            hypothesis="Breakout from prior range predicts continued movement",
            required_features=["donchian_upper", "donchian_lower"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"upper_window": {"type": "int", "min": 10, "max": 60}, "lower_window": {"type": "int", "min": 5, "max": 30}},
            default_parameters={"upper_window": 20, "lower_window": 10},
            parameter_ranges={"upper_window": (10, 60), "lower_window": (5, 30)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_breakout_range,
        ),
        "volume-spike": UniverseCandidate(
            candidate_id="volume-spike",
            strategy_family=StrategyFamily.VOLUME_MARKET_STRUCTURE,
            strategy_name="Volume Spike",
            description="LONG when volume > 2*volume SMA(20) AND close > SMA(10); exit on volume < SMA(20).",
            hypothesis="Volume spikes indicate institutional interest and future price movement",
            required_features=["sma", "volume_sma"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"sma_window": {"type": "int", "min": 5, "max": 30}, "volume_window": {"type": "int", "min": 10, "max": 50}, "volume_multiplier": {"type": "float", "min": 1.5, "max": 3.0}},
            default_parameters={"sma_window": 10, "volume_window": 20, "volume_multiplier": 2.0},
            parameter_ranges={"sma_window": (5, 30), "volume_window": (10, 50), "volume_multiplier": (1.5, 3.0)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_volume_spike,
        ),
        "price-volume-momentum": UniverseCandidate(
            candidate_id="price-volume-momentum",
            strategy_family=StrategyFamily.VOLUME_MARKET_STRUCTURE,
            strategy_name="Price-Volume Momentum",
            description="LONG when momentum(10) > 0 AND volume > volume SMA(20); exit on momentum < 0.",
            hypothesis="Momentum confirmed by volume is more persistent",
            required_features=["momentum", "volume_sma"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"momentum_window": {"type": "int", "min": 5, "max": 30}, "volume_window": {"type": "int", "min": 10, "max": 50}},
            default_parameters={"momentum_window": 10, "volume_window": 20},
            parameter_ranges={"momentum_window": (5, 30), "volume_window": (10, 50)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_price_volume_momentum,
        ),
        "mtf-dual-ma-momentum": UniverseCandidate(
            candidate_id="mtf-dual-ma-momentum",
            strategy_family=StrategyFamily.MULTI_TIMEFRAME,
            strategy_name="Dual MA Momentum",
            description="LONG when EMA(5) > SMA(20) AND momentum(10) > 0; exit on cross below.",
            hypothesis="Trend direction confirmed by momentum",
            required_features=["ema", "sma", "momentum"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"ema_fast": {"type": "int", "min": 5, "max": 20}, "sma_slow": {"type": "int", "min": 20, "max": 50}, "momentum_window": {"type": "int", "min": 5, "max": 30}},
            default_parameters={"ema_fast": 5, "sma_slow": 20, "momentum_window": 10},
            parameter_ranges={"ema_fast": (5, 20), "sma_slow": (20, 50), "momentum_window": (5, 30)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_mtf_dual_ma_momentum,
        ),
        "regime-trend": UniverseCandidate(
            candidate_id="regime-trend",
            strategy_family=StrategyFamily.REGIME_AWARE,
            strategy_name="Trend Regime",
            description="LONG when close > SMA(20) AND ATR(14) > 1.0; exit on close < SMA(20).",
            hypothesis="Trend strategies work better in high-volatility regimes",
            required_features=["sma", "atr"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"sma_window": {"type": "int", "min": 10, "max": 50}, "atr_window": {"type": "int", "min": 10, "max": 30}, "atr_threshold": {"type": "float", "min": 0.5, "max": 3.0}},
            default_parameters={"sma_window": 20, "atr_window": 14, "atr_threshold": 1.0},
            parameter_ranges={"sma_window": (10, 50), "atr_window": (10, 30), "atr_threshold": (0.5, 3.0)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_regime_trend,
        ),
        "regime-volatility": UniverseCandidate(
            candidate_id="regime-volatility",
            strategy_family=StrategyFamily.REGIME_AWARE,
            strategy_name="Volatility Regime",
            description="LONG when ATR(14) > 2.0 AND close > SMA(20); exit on ATR < 1.0.",
            hypothesis="Breakout strategies work better in expanding volatility regimes",
            required_features=["sma", "atr"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"sma_window": {"type": "int", "min": 10, "max": 50}, "atr_window": {"type": "int", "min": 10, "max": 30}, "atr_entry": {"type": "float", "min": 1.0, "max": 3.0}, "atr_exit": {"type": "float", "min": 0.5, "max": 2.0}},
            default_parameters={"sma_window": 20, "atr_window": 14, "atr_entry": 2.0, "atr_exit": 1.0},
            parameter_ranges={"sma_window": (10, 50), "atr_window": (10, 30), "atr_entry": (1.0, 3.0), "atr_exit": (0.5, 2.0)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_regime_volatility,
        ),
        "regime-meanrev-lowvol": UniverseCandidate(
            candidate_id="regime-meanrev-lowvol",
            strategy_family=StrategyFamily.REGIME_AWARE,
            strategy_name="Mean Reversion Low Vol",
            description="LONG when RSI(14) < 30 AND ATR(14) < 1.5; exit on RSI > 50.",
            hypothesis="Mean reversion works better in low-volatility regimes",
            required_features=["rsi", "atr"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"rsi_window": {"type": "int", "min": 2, "max": 30}, "atr_window": {"type": "int", "min": 10, "max": 30}, "atr_threshold": {"type": "float", "min": 0.5, "max": 3.0}},
            default_parameters={"rsi_window": 14, "atr_window": 14, "atr_threshold": 1.5},
            parameter_ranges={"rsi_window": (2, 30), "atr_window": (10, 30), "atr_threshold": (0.5, 3.0)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_regime_mean_rev_low_vol,
        ),
        "cross-macd-rsi": UniverseCandidate(
            candidate_id="cross-macd-rsi",
            strategy_family=StrategyFamily.CROSS_SECTIONAL,
            strategy_name="MACD + RSI Confirmation",
            description="LONG when MACD > signal AND RSI > 50; exit on MACD < signal.",
            hypothesis="Multi-indicator confirmation reduces false signals",
            required_features=["macd", "macd_signal", "rsi"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"rsi_threshold": {"type": "float", "min": 40, "max": 70}},
            default_parameters={"rsi_threshold": 50.0},
            parameter_ranges={"rsi_threshold": (40, 70)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_cross_macd_rsi,
        ),
        "stat-bollinger-reversion": UniverseCandidate(
            candidate_id="stat-bollinger-reversion",
            strategy_family=StrategyFamily.STATISTICAL,
            strategy_name="Bollinger Band Reversion",
            description="LONG when close < BB lower; exit when close > BB middle.",
            hypothesis="Statistical mean reversion to Bollinger Band middle",
            required_features=["bb_lower", "bb_middle"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"window": {"type": "int", "min": 10, "max": 50}, "num_std": {"type": "float", "min": 1.0, "max": 3.0}},
            default_parameters={"window": 20, "num_std": 2.0},
            parameter_ranges={"window": (10, 50), "num_std": (1.0, 3.0)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_stat_bollinger_reversion,
        ),
        "stat-macd-velocity": UniverseCandidate(
            candidate_id="stat-macd-velocity",
            strategy_family=StrategyFamily.STATISTICAL,
            strategy_name="MACD Histogram Velocity",
            description="LONG when MACD histogram > 0; exit when histogram < 0.",
            hypothesis="MACD histogram sign indicates momentum direction",
            required_features=["macd_histogram"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"fast": {"type": "int", "min": 8, "max": 20}, "slow": {"type": "int", "min": 20, "max": 50}, "signal": {"type": "int", "min": 5, "max": 20}},
            default_parameters={"fast": 12, "slow": 26, "signal": 9},
            parameter_ranges={"fast": (8, 20), "slow": (20, 50), "signal": (5, 20)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_stat_macd_velocity,
        ),
        "other-gap-down-fill": UniverseCandidate(
            candidate_id="other-gap-down-fill",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="Gap-Down Fill",
            description="LONG when prior close > open (gap down) AND volume > vol SMA(20); exit when close > prior close.",
            hypothesis="Gap-down with volume often fills back to prior close",
            required_features=["volume_sma"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"volume_window": {"type": "int", "min": 10, "max": 50}},
            default_parameters={"volume_window": 20},
            parameter_ranges={"volume_window": (10, 50)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_other_gap_down_fill,
        ),
        "other-gap-up-continuation": UniverseCandidate(
            candidate_id="other-gap-up-continuation",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="Gap-Up Continuation",
            description="LONG when prior close < open (gap up) AND close > open; exit when close < open.",
            hypothesis="Gap-up with continuation indicates strong buying interest",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_other_gap_up_continuation,
        ),
        "trend-macd": UniverseCandidate(
            candidate_id="trend-macd",
            strategy_family=StrategyFamily.TREND_MOMENTUM,
            strategy_name="MACD Trend",
            description="LONG when MACD > signal AND close > SMA(20); exit on MACD < signal.",
            hypothesis="MACD trend confirmation with price filter",
            required_features=["macd", "macd_signal", "sma"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"sma_window": {"type": "int", "min": 10, "max": 50}},
            default_parameters={"sma_window": 20},
            parameter_ranges={"sma_window": (10, 50)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_trend_macd,
        ),
        "meanrev-bollinger-rsi": UniverseCandidate(
            candidate_id="meanrev-bollinger-rsi",
            strategy_family=StrategyFamily.MEAN_REVERSION,
            strategy_name="Bollinger + RSI Reversion",
            description="LONG when RSI(14) < 30 AND close < BB lower; exit on RSI > 50.",
            hypothesis="Dual oversold conditions increase mean-reversion probability",
            required_features=["rsi", "bb_lower", "bb_middle"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"rsi_window": {"type": "int", "min": 2, "max": 30}, "bb_window": {"type": "int", "min": 10, "max": 50}, "bb_std": {"type": "float", "min": 1.0, "max": 3.0}},
            default_parameters={"rsi_window": 14, "bb_window": 20, "bb_std": 2.0},
            parameter_ranges={"rsi_window": (2, 30), "bb_window": (10, 50), "bb_std": (1.0, 3.0)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_meanrev_bollinger_rsi,
        ),
        "breakout-bollinger-upper": UniverseCandidate(
            candidate_id="breakout-bollinger-upper",
            strategy_family=StrategyFamily.BREAKOUT_VOLATILITY,
            strategy_name="Bollinger Upper Breakout",
            description="LONG when close > BB upper; exit on close < BB middle.",
            hypothesis="Breakout above upper Bollinger Band signals strong momentum",
            required_features=["bb_upper", "bb_middle"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"window": {"type": "int", "min": 10, "max": 50}, "num_std": {"type": "float", "min": 1.0, "max": 3.0}},
            default_parameters={"window": 20, "num_std": 2.0},
            parameter_ranges={"window": (10, 50), "num_std": (1.0, 3.0)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_breakout_bollinger_upper,
        ),
        "volume-price-trend": UniverseCandidate(
            candidate_id="volume-price-trend",
            strategy_family=StrategyFamily.VOLUME_MARKET_STRUCTURE,
            strategy_name="Volume-Price Trend",
            description="LONG when volume > vol SMA(20) AND close > SMA(20) AND momentum(10) > 0; exit on close < SMA(20).",
            hypothesis="Volume-confirmed multi-factor trend is more persistent",
            required_features=["volume_sma", "sma", "momentum"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"sma_window": {"type": "int", "min": 10, "max": 50}, "volume_window": {"type": "int", "min": 10, "max": 50}, "momentum_window": {"type": "int", "min": 5, "max": 30}},
            default_parameters={"sma_window": 20, "volume_window": 20, "momentum_window": 10},
            parameter_ranges={"sma_window": (10, 50), "volume_window": (10, 50), "momentum_window": (5, 30)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_volume_price_trend,
        ),
        "regime-trend-strength": UniverseCandidate(
            candidate_id="regime-trend-strength",
            strategy_family=StrategyFamily.REGIME_AWARE,
            strategy_name="Trend Strength Regime",
            description="LONG when close > SMA(50) AND SMA(20) > SMA(50) AND ATR(14) > 1.0; exit on close < SMA(50).",
            hypothesis="Strong uptrends with elevated volatility produce better risk-adjusted returns",
            required_features=["sma", "atr"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"sma_fast": {"type": "int", "min": 10, "max": 50}, "sma_slow": {"type": "int", "min": 50, "max": 200}, "atr_window": {"type": "int", "min": 10, "max": 30}, "atr_threshold": {"type": "float", "min": 0.5, "max": 3.0}},
            default_parameters={"sma_fast": 20, "sma_slow": 50, "atr_window": 14, "atr_threshold": 1.0},
            parameter_ranges={"sma_fast": (10, 50), "sma_slow": (50, 200), "atr_window": (10, 30), "atr_threshold": (0.5, 3.0)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_regime_trend_strength,
        ),
        "cross-macd-crossover-rsi": UniverseCandidate(
            candidate_id="cross-macd-crossover-rsi",
            strategy_family=StrategyFamily.CROSS_SECTIONAL,
            strategy_name="MACD Crossover + RSI",
            description="LONG when MACD crosses above signal AND RSI > 50; exit on cross below.",
            hypothesis="Momentum crossover confirmed by RSI filter reduces false signals",
            required_features=["macd", "macd_signal", "rsi"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"rsi_threshold": {"type": "float", "min": 40, "max": 70}},
            default_parameters={"rsi_threshold": 50.0},
            parameter_ranges={"rsi_threshold": (40, 70)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_cross_macd_crossover_rsi,
        ),
        "stat-bollinger-volume": UniverseCandidate(
            candidate_id="stat-bollinger-volume",
            strategy_family=StrategyFamily.STATISTICAL,
            strategy_name="Bollinger Volume Reversion",
            description="LONG when close > BB middle AND volume > volume SMA(20); exit on close < BB middle.",
            hypothesis="Mean reversion to Bollinger middle confirmed by volume",
            required_features=["bb_middle", "volume_sma"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"window": {"type": "int", "min": 10, "max": 50}, "num_std": {"type": "float", "min": 1.0, "max": 3.0}},
            default_parameters={"window": 20, "num_std": 2.0},
            parameter_ranges={"window": (10, 50), "num_std": (1.0, 3.0)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_stat_bollinger_volume,
        ),
        "other-gap-recovery": UniverseCandidate(
            candidate_id="other-gap-recovery",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="Gap Recovery",
            description="LONG when prior close > open (gap down) AND close > prior open; exit when close < open.",
            hypothesis="Gap-down patterns that recover show buying pressure",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_other_gap_recovery,
        ),
        "other-strong-close": UniverseCandidate(
            candidate_id="other-strong-close",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="Strong Close",
            description="LONG when close > 98% of high AND volume > volume SMA(20); exit when close < open.",
            hypothesis="Strong closes near the high with volume indicate bullish conviction",
            required_features=["volume_sma"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"volume_window": {"type": "int", "min": 10, "max": 50}},
            default_parameters={"volume_window": 20},
            parameter_ranges={"volume_window": (10, 50)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_other_strong_close,
        ),
        "trend-ema-cloud": UniverseCandidate(
            candidate_id="trend-ema-cloud",
            strategy_family=StrategyFamily.TREND_MOMENTUM,
            strategy_name="EMA Cloud",
            description="LONG when EMA(8) > EMA(21) AND EMA(21) > EMA(50); exit on close < EMA(21).",
            hypothesis="Multi-period EMA alignment identifies strong trends",
            required_features=["ema"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"fast": {"type": "int", "min": 5, "max": 20}, "mid": {"type": "int", "min": 15, "max": 30}, "slow": {"type": "int", "min": 40, "max": 100}},
            default_parameters={"fast": 8, "mid": 21, "slow": 50},
            parameter_ranges={"fast": (5, 20), "mid": (15, 30), "slow": (40, 100)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_trend_ema_cloud,
        ),
        "meanrev-rsi-volume": UniverseCandidate(
            candidate_id="meanrev-rsi-volume",
            strategy_family=StrategyFamily.MEAN_REVERSION,
            strategy_name="RSI Volume Reversion",
            description="LONG when RSI(14) < 25 AND volume > 2*volume SMA(20); exit when RSI > 40.",
            hypothesis="Oversold RSI with volume spike indicates reversal",
            required_features=["rsi", "volume_sma"],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={"rsi_window": {"type": "int", "min": 2, "max": 30}, "volume_window": {"type": "int", "min": 10, "max": 50}},
            default_parameters={"rsi_window": 14, "volume_window": 20},
            parameter_ranges={"rsi_window": (2, 30), "volume_window": (10, 50)},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_meanrev_rsi_volume,
        ),

        "breakout-donchian-volume": UniverseCandidate(
            candidate_id="breakout-donchian-volume",
            strategy_family=StrategyFamily.BREAKOUT_VOLATILITY,
            strategy_name="Breakout Donchian Volume",
            description="Auto-generated candidate from _breakout_donchian_volume.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_breakout_donchian_volume,
        ),

        "volume-surge-sma": UniverseCandidate(
            candidate_id="volume-surge-sma",
            strategy_family=StrategyFamily.VOLUME_MARKET_STRUCTURE,
            strategy_name="Volume Surge Sma",
            description="Auto-generated candidate from _volume_surge_sma.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_volume_surge_sma,
        ),

        "regime-strong-trend-high-vol": UniverseCandidate(
            candidate_id="regime-strong-trend-high-vol",
            strategy_family=StrategyFamily.REGIME_AWARE,
            strategy_name="Regime Strong Trend High Vol",
            description="Auto-generated candidate from _regime_strong_trend_high_vol.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_regime_strong_trend_high_vol,
        ),

        "mtf-triple-ema-rsi": UniverseCandidate(
            candidate_id="mtf-triple-ema-rsi",
            strategy_family=StrategyFamily.MULTI_TIMEFRAME,
            strategy_name="Mtf Triple Ema Rsi",
            description="Auto-generated candidate from _mtf_triple_ema_rsi.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_mtf_triple_ema_rsi,
        ),

        "cross-bb-macd": UniverseCandidate(
            candidate_id="cross-bb-macd",
            strategy_family=StrategyFamily.CROSS_SECTIONAL,
            strategy_name="Cross Bb Macd",
            description="Auto-generated candidate from _cross_bb_macd.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_cross_bb_macd,
        ),

        "cross-triple-filter": UniverseCandidate(
            candidate_id="cross-triple-filter",
            strategy_family=StrategyFamily.CROSS_SECTIONAL,
            strategy_name="Cross Triple Filter",
            description="Auto-generated candidate from _cross_triple_filter.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_cross_triple_filter,
        ),

        "stat-bb-atr-filter": UniverseCandidate(
            candidate_id="stat-bb-atr-filter",
            strategy_family=StrategyFamily.STATISTICAL,
            strategy_name="Stat Bb Atr Filter",
            description="Auto-generated candidate from _stat_bb_atr_filter.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_stat_bb_atr_filter,
        ),

        "stat-rsi-sma-reversion": UniverseCandidate(
            candidate_id="stat-rsi-sma-reversion",
            strategy_family=StrategyFamily.STATISTICAL,
            strategy_name="Stat Rsi Sma Reversion",
            description="Auto-generated candidate from _stat_rsi_sma_reversion.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_stat_rsi_sma_reversion,
        ),

        "stat-momentum-surge": UniverseCandidate(
            candidate_id="stat-momentum-surge",
            strategy_family=StrategyFamily.STATISTICAL,
            strategy_name="Stat Momentum Surge",
            description="Auto-generated candidate from _stat_momentum_surge.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_stat_momentum_surge,
        ),

        "other-prev-high-breakout": UniverseCandidate(
            candidate_id="other-prev-high-breakout",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="Other Prev High Breakout",
            description="Auto-generated candidate from _other_prev_high_breakout.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_other_prev_high_breakout,
        ),

        "other-inside-bar-breakout": UniverseCandidate(
            candidate_id="other-inside-bar-breakout",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="Other Inside Bar Breakout",
            description="Auto-generated candidate from _other_inside_bar_breakout.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_other_inside_bar_breakout,
        ),

        "other-gap-up-volume": UniverseCandidate(
            candidate_id="other-gap-up-volume",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="Other Gap Up Volume",
            description="Auto-generated candidate from _other_gap_up_volume.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_other_gap_up_volume,
        ),

        "trend-ema-cloud-short": UniverseCandidate(
            candidate_id="trend-ema-cloud-short",
            strategy_family=StrategyFamily.TREND_MOMENTUM,
            strategy_name="Trend Ema Cloud Short",
            description="Auto-generated candidate from _trend_ema_cloud_short.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_trend_ema_cloud_short,
        ),

        "trend-price-above-ema": UniverseCandidate(
            candidate_id="trend-price-above-ema",
            strategy_family=StrategyFamily.TREND_MOMENTUM,
            strategy_name="Trend Price Above Ema",
            description="Auto-generated candidate from _trend_price_above_ema.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_trend_price_above_ema,
        ),

        "trend-momentum-ema": UniverseCandidate(
            candidate_id="trend-momentum-ema",
            strategy_family=StrategyFamily.TREND_MOMENTUM,
            strategy_name="Trend Momentum Ema",
            description="Auto-generated candidate from _trend_momentum_ema.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_trend_momentum_ema,
        ),

        "meanrev-rsi-deep": UniverseCandidate(
            candidate_id="meanrev-rsi-deep",
            strategy_family=StrategyFamily.MEAN_REVERSION,
            strategy_name="Meanrev Rsi Deep",
            description="Auto-generated candidate from _meanrev_rsi_deep.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_meanrev_rsi_deep,
        ),

        "meanrev-bb-rsi": UniverseCandidate(
            candidate_id="meanrev-bb-rsi",
            strategy_family=StrategyFamily.MEAN_REVERSION,
            strategy_name="Meanrev Bb Rsi",
            description="Auto-generated candidate from _meanrev_bb_rsi.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_meanrev_bb_rsi,
        ),

        "breakout-donchian-atr": UniverseCandidate(
            candidate_id="breakout-donchian-atr",
            strategy_family=StrategyFamily.BREAKOUT_VOLATILITY,
            strategy_name="Breakout Donchian Atr",
            description="Auto-generated candidate from _breakout_donchian_atr.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_breakout_donchian_atr,
        ),

        "volume-spike-momentum": UniverseCandidate(
            candidate_id="volume-spike-momentum",
            strategy_family=StrategyFamily.VOLUME_MARKET_STRUCTURE,
            strategy_name="Volume Spike Momentum",
            description="Auto-generated candidate from _volume_spike_momentum.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_volume_spike_momentum,
        ),

        "cross-bb-rsi": UniverseCandidate(
            candidate_id="cross-bb-rsi",
            strategy_family=StrategyFamily.CROSS_SECTIONAL,
            strategy_name="Cross Bb Rsi",
            description="Auto-generated candidate from _cross_bb_rsi.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_cross_bb_rsi,
        ),

        "cross-ema-bb-volume": UniverseCandidate(
            candidate_id="cross-ema-bb-volume",
            strategy_family=StrategyFamily.CROSS_SECTIONAL,
            strategy_name="Cross Ema Bb Volume",
            description="Auto-generated candidate from _cross_ema_bb_volume.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_cross_ema_bb_volume,
        ),

        "cross-macd-atr": UniverseCandidate(
            candidate_id="cross-macd-atr",
            strategy_family=StrategyFamily.CROSS_SECTIONAL,
            strategy_name="Cross Macd Atr",
            description="Auto-generated candidate from _cross_macd_atr.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_cross_macd_atr,
        ),

        "stat-rsi-bb-upper": UniverseCandidate(
            candidate_id="stat-rsi-bb-upper",
            strategy_family=StrategyFamily.STATISTICAL,
            strategy_name="Stat Rsi Bb Upper",
            description="Auto-generated candidate from _stat_rsi_bb_upper.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_stat_rsi_bb_upper,
        ),

        "stat-momentum-atr": UniverseCandidate(
            candidate_id="stat-momentum-atr",
            strategy_family=StrategyFamily.STATISTICAL,
            strategy_name="Stat Momentum Atr",
            description="Auto-generated candidate from _stat_momentum_atr.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_stat_momentum_atr,
        ),

        "stat-sma-bb-middle": UniverseCandidate(
            candidate_id="stat-sma-bb-middle",
            strategy_family=StrategyFamily.STATISTICAL,
            strategy_name="Stat Sma Bb Middle",
            description="Auto-generated candidate from _stat_sma_bb_middle.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_stat_sma_bb_middle,
        ),

        "other-prev-low-bounce": UniverseCandidate(
            candidate_id="other-prev-low-bounce",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="Other Prev Low Bounce",
            description="Auto-generated candidate from _other_prev_low_bounce.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_other_prev_low_bounce,
        ),

        "other-open-above-close": UniverseCandidate(
            candidate_id="other-open-above-close",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="Other Open Above Close",
            description="Auto-generated candidate from _other_open_above_close.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_other_open_above_close,
        ),

        "other-volume-decline-price-rise": UniverseCandidate(
            candidate_id="other-volume-decline-price-rise",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="Other Volume Decline Price Rise",
            description="Auto-generated candidate from _other_volume_decline_price_rise.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_other_volume_decline_price_rise,
        ),

        "trend-ema-macd": UniverseCandidate(
            candidate_id="trend-ema-macd",
            strategy_family=StrategyFamily.TREND_MOMENTUM,
            strategy_name="Trend Ema Macd",
            description="Auto-generated candidate from _trend_ema_macd.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_trend_ema_macd,
        ),

        "trend-sma-ema-momentum": UniverseCandidate(
            candidate_id="trend-sma-ema-momentum",
            strategy_family=StrategyFamily.TREND_MOMENTUM,
            strategy_name="Trend Sma Ema Momentum",
            description="Auto-generated candidate from _trend_sma_ema_momentum.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_trend_sma_ema_momentum,
        ),

        "trend-price-ema-bb": UniverseCandidate(
            candidate_id="trend-price-ema-bb",
            strategy_family=StrategyFamily.TREND_MOMENTUM,
            strategy_name="Trend Price Ema Bb",
            description="Auto-generated candidate from _trend_price_ema_bb.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_trend_price_ema_bb,
        ),

        "meanrev-rsi-bb-upper": UniverseCandidate(
            candidate_id="meanrev-rsi-bb-upper",
            strategy_family=StrategyFamily.MEAN_REVERSION,
            strategy_name="Meanrev Rsi Bb Upper",
            description="Auto-generated candidate from _meanrev_rsi_bb_upper.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_meanrev_rsi_bb_upper,
        ),

        "meanrev-bb-volume-decline": UniverseCandidate(
            candidate_id="meanrev-bb-volume-decline",
            strategy_family=StrategyFamily.MEAN_REVERSION,
            strategy_name="Meanrev Bb Volume Decline",
            description="Auto-generated candidate from _meanrev_bb_volume_decline.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_meanrev_bb_volume_decline,
        ),

        "breakout-donchian-bb": UniverseCandidate(
            candidate_id="breakout-donchian-bb",
            strategy_family=StrategyFamily.BREAKOUT_VOLATILITY,
            strategy_name="Breakout Donchian Bb",
            description="Auto-generated candidate from _breakout_donchian_bb.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_breakout_donchian_bb,
        ),

        "breakout-atr-sma": UniverseCandidate(
            candidate_id="breakout-atr-sma",
            strategy_family=StrategyFamily.BREAKOUT_VOLATILITY,
            strategy_name="Breakout Atr Sma",
            description="Auto-generated candidate from _breakout_atr_sma.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_breakout_atr_sma,
        ),

        "volume-rsi": UniverseCandidate(
            candidate_id="volume-rsi",
            strategy_family=StrategyFamily.VOLUME_MARKET_STRUCTURE,
            strategy_name="Volume Rsi",
            description="Auto-generated candidate from _volume_rsi.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_volume_rsi,
        ),

        "volume-surge-rsi-oversold": UniverseCandidate(
            candidate_id="volume-surge-rsi-oversold",
            strategy_family=StrategyFamily.VOLUME_MARKET_STRUCTURE,
            strategy_name="Volume Surge Rsi Oversold",
            description="Auto-generated candidate from _volume_surge_rsi_oversold.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_volume_surge_rsi_oversold,
        ),

        "regime-low-atr-sma": UniverseCandidate(
            candidate_id="regime-low-atr-sma",
            strategy_family=StrategyFamily.REGIME_AWARE,
            strategy_name="Regime Low Atr Sma",
            description="Auto-generated candidate from _regime_low_atr_sma.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_regime_low_atr_sma,
        ),

        "regime-high-atr-bb": UniverseCandidate(
            candidate_id="regime-high-atr-bb",
            strategy_family=StrategyFamily.REGIME_AWARE,
            strategy_name="Regime High Atr Bb",
            description="Auto-generated candidate from _regime_high_atr_bb.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_regime_high_atr_bb,
        ),

        "trend-ema-slow-uptrend": UniverseCandidate(
            candidate_id="trend-ema-slow-uptrend",
            strategy_family=StrategyFamily.TREND_MOMENTUM,
            strategy_name="Trend Ema Slow Uptrend",
            description="Auto-generated candidate from _trend_ema_slow_uptrend.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_trend_ema_slow_uptrend,
        ),

        "trend-price-above-slow-ema": UniverseCandidate(
            candidate_id="trend-price-above-slow-ema",
            strategy_family=StrategyFamily.TREND_MOMENTUM,
            strategy_name="Trend Price Above Slow Ema",
            description="Auto-generated candidate from _trend_price_above_slow_ema.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_trend_price_above_slow_ema,
        ),

        "trend-ema-fast-slow-volume": UniverseCandidate(
            candidate_id="trend-ema-fast-slow-volume",
            strategy_family=StrategyFamily.TREND_MOMENTUM,
            strategy_name="Trend Ema Fast Slow Volume",
            description="Auto-generated candidate from _trend_ema_fast_slow_volume.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_trend_ema_fast_slow_volume,
        ),

        "trend-momentum-volume-spike": UniverseCandidate(
            candidate_id="trend-momentum-volume-spike",
            strategy_family=StrategyFamily.TREND_MOMENTUM,
            strategy_name="Trend Momentum Volume Spike",
            description="Auto-generated candidate from _trend_momentum_volume_spike.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_trend_momentum_volume_spike,
        ),

        "trend-sma-rsi-filter": UniverseCandidate(
            candidate_id="trend-sma-rsi-filter",
            strategy_family=StrategyFamily.TREND_MOMENTUM,
            strategy_name="Trend Sma Rsi Filter",
            description="Auto-generated candidate from _trend_sma_rsi_filter.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_trend_sma_rsi_filter,
        ),

        "trend-ema-macd-volume": UniverseCandidate(
            candidate_id="trend-ema-macd-volume",
            strategy_family=StrategyFamily.TREND_MOMENTUM,
            strategy_name="Trend Ema Macd Volume",
            description="Auto-generated candidate from _trend_ema_macd_volume.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_trend_ema_macd_volume,
        ),

        "meanrev-rsi-bb-bounce": UniverseCandidate(
            candidate_id="meanrev-rsi-bb-bounce",
            strategy_family=StrategyFamily.MEAN_REVERSION,
            strategy_name="Meanrev Rsi Bb Bounce",
            description="Auto-generated candidate from _meanrev_rsi_bb_bounce.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_meanrev_rsi_bb_bounce,
        ),

        "meanrev-sma-support": UniverseCandidate(
            candidate_id="meanrev-sma-support",
            strategy_family=StrategyFamily.MEAN_REVERSION,
            strategy_name="Meanrev Sma Support",
            description="Auto-generated candidate from _meanrev_sma_support.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_meanrev_sma_support,
        ),

        "meanrev-ema-bounce": UniverseCandidate(
            candidate_id="meanrev-ema-bounce",
            strategy_family=StrategyFamily.MEAN_REVERSION,
            strategy_name="Meanrev Ema Bounce",
            description="Auto-generated candidate from _meanrev_ema_bounce.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_meanrev_ema_bounce,
        ),

        "meanrev-bb-middle-decline": UniverseCandidate(
            candidate_id="meanrev-bb-middle-decline",
            strategy_family=StrategyFamily.MEAN_REVERSION,
            strategy_name="Meanrev Bb Middle Decline",
            description="Auto-generated candidate from _meanrev_bb_middle_decline.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_meanrev_bb_middle_decline,
        ),

        "meanrev-double-bounce": UniverseCandidate(
            candidate_id="meanrev-double-bounce",
            strategy_family=StrategyFamily.MEAN_REVERSION,
            strategy_name="Meanrev Double Bounce",
            description="Auto-generated candidate from _meanrev_double_bounce.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_meanrev_double_bounce,
        ),

        "breakout-donchian-volume-sma": UniverseCandidate(
            candidate_id="breakout-donchian-volume-sma",
            strategy_family=StrategyFamily.BREAKOUT_VOLATILITY,
            strategy_name="Breakout Donchian Volume Sma",
            description="Auto-generated candidate from _breakout_donchian_volume_sma.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_breakout_donchian_volume_sma,
        ),

        "breakout-bb-upper-volume": UniverseCandidate(
            candidate_id="breakout-bb-upper-volume",
            strategy_family=StrategyFamily.BREAKOUT_VOLATILITY,
            strategy_name="Breakout Bb Upper Volume",
            description="Auto-generated candidate from _breakout_bb_upper_volume.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_breakout_bb_upper_volume,
        ),

        "breakout-range-volume": UniverseCandidate(
            candidate_id="breakout-range-volume",
            strategy_family=StrategyFamily.BREAKOUT_VOLATILITY,
            strategy_name="Breakout Range Volume",
            description="Auto-generated candidate from _breakout_range_volume.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_breakout_range_volume,
        ),

        "breakout-sma-momentum": UniverseCandidate(
            candidate_id="breakout-sma-momentum",
            strategy_family=StrategyFamily.BREAKOUT_VOLATILITY,
            strategy_name="Breakout Sma Momentum",
            description="Auto-generated candidate from _breakout_sma_momentum.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_breakout_sma_momentum,
        ),

        "breakout-triple-confirmation": UniverseCandidate(
            candidate_id="breakout-triple-confirmation",
            strategy_family=StrategyFamily.BREAKOUT_VOLATILITY,
            strategy_name="Breakout Triple Confirmation",
            description="Auto-generated candidate from _breakout_triple_confirmation.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_breakout_triple_confirmation,
        ),

        "volume-volume-price-trend": UniverseCandidate(
            candidate_id="volume-volume-price-trend",
            strategy_family=StrategyFamily.VOLUME_MARKET_STRUCTURE,
            strategy_name="Volume Volume Price Trend",
            description="Auto-generated candidate from _volume_volume_price_trend.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_volume_volume_price_trend,
        ),

        "volume-accumulation": UniverseCandidate(
            candidate_id="volume-accumulation",
            strategy_family=StrategyFamily.VOLUME_MARKET_STRUCTURE,
            strategy_name="Volume Accumulation",
            description="Auto-generated candidate from _volume_accumulation.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_volume_accumulation,
        ),

        "volume-climax": UniverseCandidate(
            candidate_id="volume-climax",
            strategy_family=StrategyFamily.VOLUME_MARKET_STRUCTURE,
            strategy_name="Volume Climax",
            description="Auto-generated candidate from _volume_climax.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_volume_climax,
        ),

        "volume-trend-confirmation": UniverseCandidate(
            candidate_id="volume-trend-confirmation",
            strategy_family=StrategyFamily.VOLUME_MARKET_STRUCTURE,
            strategy_name="Volume Trend Confirmation",
            description="Auto-generated candidate from _volume_trend_confirmation.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_volume_trend_confirmation,
        ),

        "cross-bb-atr-rsi": UniverseCandidate(
            candidate_id="cross-bb-atr-rsi",
            strategy_family=StrategyFamily.CROSS_SECTIONAL,
            strategy_name="Cross Bb Atr Rsi",
            description="Auto-generated candidate from _cross_bb_atr_rsi.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_cross_bb_atr_rsi,
        ),

        "cross-macd-bb-volume": UniverseCandidate(
            candidate_id="cross-macd-bb-volume",
            strategy_family=StrategyFamily.CROSS_SECTIONAL,
            strategy_name="Cross Macd Bb Volume",
            description="Auto-generated candidate from _cross_macd_bb_volume.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_cross_macd_bb_volume,
        ),

        "cross-ema-atr-momentum": UniverseCandidate(
            candidate_id="cross-ema-atr-momentum",
            strategy_family=StrategyFamily.CROSS_SECTIONAL,
            strategy_name="Cross Ema Atr Momentum",
            description="Auto-generated candidate from _cross_ema_atr_momentum.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_cross_ema_atr_momentum,
        ),

        "stat-rsi-bb-atr": UniverseCandidate(
            candidate_id="stat-rsi-bb-atr",
            strategy_family=StrategyFamily.STATISTICAL,
            strategy_name="Stat Rsi Bb Atr",
            description="Auto-generated candidate from _stat_rsi_bb_atr.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_stat_rsi_bb_atr,
        ),

        "stat-sma-momentum-bb": UniverseCandidate(
            candidate_id="stat-sma-momentum-bb",
            strategy_family=StrategyFamily.STATISTICAL,
            strategy_name="Stat Sma Momentum Bb",
            description="Auto-generated candidate from _stat_sma_momentum_bb.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_stat_sma_momentum_bb,
        ),

        "other-close-high-open-low": UniverseCandidate(
            candidate_id="other-close-high-open-low",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="Other Close High Open Low",
            description="Auto-generated candidate from _other_close_high_open_low.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_other_close_high_open_low,
        ),

        "other-three-white-soldiers": UniverseCandidate(
            candidate_id="other-three-white-soldiers",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="Other Three White Soldiers",
            description="Auto-generated candidate from _other_three_white_soldiers.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_other_three_white_soldiers,
        ),

        "other-volume-above-mas": UniverseCandidate(
            candidate_id="other-volume-above-mas",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="Other Volume Above Mas",
            description="Auto-generated candidate from _other_volume_above_mas.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_other_volume_above_mas,
        ),

        "trend-ema-rsi-atr": UniverseCandidate(
            candidate_id="trend-ema-rsi-atr",
            strategy_family=StrategyFamily.TREND_MOMENTUM,
            strategy_name="Trend Ema Rsi Atr",
            description="Auto-generated candidate from _trend_ema_rsi_atr.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_trend_ema_rsi_atr,
        ),

        "trend-triple-ema-volume": UniverseCandidate(
            candidate_id="trend-triple-ema-volume",
            strategy_family=StrategyFamily.TREND_MOMENTUM,
            strategy_name="Trend Triple Ema Volume",
            description="Auto-generated candidate from _trend_triple_ema_volume.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_trend_triple_ema_volume,
        ),

        "new-macd-positive-trend": UniverseCandidate(
            candidate_id="new-macd-positive-trend",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="New Macd Positive Trend",
            description="Auto-generated candidate from _new_macd_positive_trend.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_new_macd_positive_trend,
        ),

        "new-rsi-bb-upper-bounce": UniverseCandidate(
            candidate_id="new-rsi-bb-upper-bounce",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="New Rsi Bb Upper Bounce",
            description="Auto-generated candidate from _new_rsi_bb_upper_bounce.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_new_rsi_bb_upper_bounce,
        ),

        "new-volume-spike-atr-trend": UniverseCandidate(
            candidate_id="new-volume-spike-atr-trend",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="New Volume Spike Atr Trend",
            description="Auto-generated candidate from _new_volume_spike_atr_trend.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_new_volume_spike_atr_trend,
        ),

        "new-donchian-bb-breakout": UniverseCandidate(
            candidate_id="new-donchian-bb-breakout",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="New Donchian Bb Breakout",
            description="Auto-generated candidate from _new_donchian_bb_breakout.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_new_donchian_bb_breakout,
        ),

        "new-ema-rsi-momentum": UniverseCandidate(
            candidate_id="new-ema-rsi-momentum",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="New Ema Rsi Momentum",
            description="Auto-generated candidate from _new_ema_rsi_momentum.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_new_ema_rsi_momentum,
        ),

        "new-sma-rsi-volume-trend": UniverseCandidate(
            candidate_id="new-sma-rsi-volume-trend",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="New Sma Rsi Volume Trend",
            description="Auto-generated candidate from _new_sma_rsi_volume_trend.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_new_sma_rsi_volume_trend,
        ),

        "new-bb-middle-meanrev": UniverseCandidate(
            candidate_id="new-bb-middle-meanrev",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="New Bb Middle Meanrev",
            description="Auto-generated candidate from _new_bb_middle_meanrev.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_new_bb_middle_meanrev,
        ),

        "new-macd-histogram-trend": UniverseCandidate(
            candidate_id="new-macd-histogram-trend",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="New Macd Histogram Trend",
            description="Auto-generated candidate from _new_macd_histogram_trend.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_new_macd_histogram_trend,
        ),

        "new-atr-trend-candle": UniverseCandidate(
            candidate_id="new-atr-trend-candle",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="New Atr Trend Candle",
            description="Auto-generated candidate from _new_atr_trend_candle.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_new_atr_trend_candle,
        ),

        "new-triple-ema-momentum": UniverseCandidate(
            candidate_id="new-triple-ema-momentum",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="New Triple Ema Momentum",
            description="Auto-generated candidate from _new_triple_ema_momentum.",
            hypothesis="Auto-generated hypothesis",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=_new_triple_ema_momentum,
        ),
    }


def get_strategy_candidate(candidate_id: str) -> Optional[UniverseCandidate]:
    universe = build_default_universe()
    return universe.get(candidate_id)


def list_strategy_candidates() -> list[UniverseCandidate]:
    universe = build_default_universe()
    return list(universe.values())
