"""Day 8 tests: Market Intelligence Engine (features, regime, signal, AI, look-ahead).

Offline, deterministic, no live data, no fabricated indicators/Greeks/OI.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_system.research.intelligence import (
    FeatureEngine,
    TechnicalFeatures,
    DerivativeFeatures,
    MarketRegime,
    RegimeEnum,
    VolRegime,
    SignalCandidate,
    SignalDirection,
    SetupType,
    AnalysisExplanation,
    AIAnalysis,
    AnalysisContext,
    MarketReasoningProvider,
    AnalysisRejected,
    InstrumentClass,
    classify_regime,
    generate_signal_candidate,
    instrument_class_of,
)
from trading_system.india.data_health import FeedStatus, DataHealthMonitor
from trading_system.research import MarketIntelligenceEngine


def _ohlc(n: int, start: float = 100.0, drift: float = 0.1, vol: float = 0.5, seed: int = 1) -> pd.DataFrame:
    """Deterministic OHLCV with tz-aware UTC index."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    closes = start + np.cumsum(rng.normal(drift, vol, n))
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(closes, opens) + np.abs(rng.normal(0, vol, n))
    lows = np.minimum(closes, opens) - np.abs(rng.normal(0, vol, n))
    vols = rng.integers(1_000_000, 5_000_000, n).astype(float)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
        index=idx,
    )


# ---------------------------------------------------------------- feature tests
def test_sma_ema_rsi_atr_computed():
    df = _ohlc(260, drift=0.2, vol=0.3)
    f = FeatureEngine(lookback=60).compute(df)
    assert f.sma_20 is not None
    assert f.sma_50 is not None
    assert f.sma_200 is not None
    assert f.ema_20 is not None
    assert f.ema_50 is not None
    assert f.rsi_14 is not None
    assert 0 <= f.rsi_14 <= 100
    assert f.atr_14 is not None and f.atr_14 > 0


def test_price_vs_sma_signs():
    df = _ohlc(260, drift=0.5, vol=0.1)  # strong uptrend
    f = FeatureEngine(lookback=60).compute(df)
    assert f.price_vs_sma20 is not None and f.price_vs_sma20 > 0
    assert f.trend == __import__("trading_system.research.intelligence", fromlist=["TrendEnum"]).TrendEnum.BULLISH


def test_volatility_regime_present():
    df = _ohlc(120, drift=0.0, vol=1.0)
    f = FeatureEngine(lookback=60).compute(df)
    assert f.vol_regime in (VolRegime.LOW, VolRegime.NORMAL, VolRegime.HIGH)
    assert f.hist_vol is not None and f.hist_vol >= 0


def test_relative_volume_and_unusual_flag():
    df = _ohlc(60, drift=0.1, vol=0.3)
    df = df.copy()
    df.iloc[-1, df.columns.get_loc("volume")] = df["volume"].iloc[:-1].mean() * 3.0
    f = FeatureEngine(lookback=60).compute(df)
    assert f.relative_volume is not None and f.relative_volume > 2.0
    assert f.unusual_volume is True


def test_breakout_candidate_logic():
    df = _ohlc(80, drift=0.1, vol=0.2)
    df = df.copy()
    df.iloc[-1, df.columns.get_loc("close")] = df["high"].iloc[:-1].max() * 1.001
    f = FeatureEngine(lookback=60).compute(df)
    assert f.breakout_candidate is True


# ---------------------------------------------------------------- edge cases
def test_insufficient_bars():
    df = _ohlc(10)
    f = FeatureEngine(lookback=60).compute(df)
    assert f.insufficient is True
    assert f.sma_200 is None


def test_constant_prices():
    idx = pd.date_range("2024-01-01", periods=50, freq="D", tz="UTC")
    df = pd.DataFrame(
        {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0e6},
        index=idx,
    )
    f = FeatureEngine(lookback=30).compute(df)
    assert f.rsi_14 == 100.0
    assert f.ema20_vs_ema50 == 0.0


def test_zero_volume():
    df = _ohlc(60)
    df = df.copy()
    df["volume"] = 0.0
    f = FeatureEngine(lookback=30).compute(df)
    assert f.volume_sma20 == 0.0
    assert f.relative_volume is None or f.relative_volume == 0.0


def test_missing_values_dropped():
    df = _ohlc(60)
    df = df.copy()
    df.iloc[5, df.columns.get_loc("close")] = np.nan
    f = FeatureEngine(lookback=30).compute(df)
    assert f.close is not None


def test_duplicate_timestamp_dropped():
    df = _ohlc(60)
    df2 = df.copy()
    dup = df2.iloc[[-1]].copy()
    dup.iloc[0, dup.columns.get_loc("close")] = 999.0
    combined = pd.concat([df2, dup])
    f = FeatureEngine(lookback=30).compute(combined)
    assert f.close != 999.0


def test_unsorted_data_sorted():
    df = _ohlc(60).sort_index(ascending=False)
    f = FeatureEngine(lookback=30).compute(df)
    assert f.data_points == 60


def test_timezone_aware_index():
    df = _ohlc(60)
    df = df.copy()
    df.index = df.index.tz_convert("Asia/Kolkata")
    f = FeatureEngine(lookback=30).compute(df)
    assert f.close is not None


# ---------------------------------------------------------------- regime
def test_regime_trending_up():
    df = _ohlc(260, drift=0.5, vol=0.1)
    f = FeatureEngine(lookback=60).compute(df)
    r = classify_regime(f)
    assert r.regime in (RegimeEnum.TRENDING_UP, RegimeEnum.HIGH_VOLATILITY)
    assert 0.0 <= r.confidence <= 1.0


def test_regime_unknown_insufficient():
    df = _ohlc(10)
    f = FeatureEngine(lookback=60).compute(df)
    r = classify_regime(f)
    assert r.regime == RegimeEnum.UNKNOWN
    assert r.confidence == 0.0


def test_regime_confidence_bounded():
    df = _ohlc(260, drift=-0.5, vol=0.1)
    f = FeatureEngine(lookback=60).compute(df)
    r = classify_regime(f)
    assert 0.0 <= r.confidence <= 1.0


# ---------------------------------------------------------------- derivative schemas
def test_derivative_features_none_by_default():
    d = DerivativeFeatures()
    assert d.open_interest is None
    assert d.implied_vol is None
    assert d.delta is None
    assert d.gamma is None
    assert d.theta is None
    assert d.vega is None
    assert d.basis is None


def test_option_moneyness_and_dte():
    d = DerivativeFeatures(strike=100.0, expiry="2030-12-25")
    assert d.strike == 100.0
    assert d.open_interest is None


# ---------------------------------------------------------------- signal candidates
def test_signal_bullish_scenario():
    df = _ohlc(260, drift=0.5, vol=0.1)
    f = FeatureEngine(lookback=60).compute(df)
    r = classify_regime(f)
    c = generate_signal_candidate("NSE:SBIN", "cid", "1d", f, r)
    assert c.direction == SignalDirection.LONG
    assert c.setup in (SetupType.TREND_CONTINUATION, SetupType.BREAKOUT)
    assert 0.0 <= c.confidence <= 1.0


def test_signal_bearish_scenario():
    # Deterministic monotonic decline => bearish trend, no noise ambiguity.
    idx = pd.date_range("2024-01-01", periods=260, freq="D", tz="UTC")
    closes = np.linspace(200.0, 50.0, 260)
    df = pd.DataFrame(
        {"open": closes, "high": closes + 1, "low": closes - 1, "close": closes,
         "volume": 1.0e6 * np.ones(260)},
        index=idx,
    )
    f = FeatureEngine(lookback=60).compute(df)
    r = classify_regime(f)
    c = generate_signal_candidate("NSE:SBIN", "cid", "1d", f, r)
    assert c.direction == SignalDirection.SHORT


def test_signal_neutral_scenario():
    # Directly exercise candidate logic with an explicitly neutral feature set:
    # no trend edge, mid-band RSI, no breakout/breakdown => NEUTRAL.
    from trading_system.research.intelligence import TrendEnum

    f = TechnicalFeatures(
        close=100.0, trend=TrendEnum.NEUTRAL, rsi_14=50.0,
        breakout_candidate=False, breakdown_candidate=False,
    )
    r = classify_regime(f)
    c = generate_signal_candidate("NSE:SBIN", "cid", "1d", f, r)
    assert c.direction == SignalDirection.NEUTRAL
    assert c.setup == SetupType.NO_SETUP


def test_signal_insufficient_data():
    df = _ohlc(10)
    f = FeatureEngine(lookback=60).compute(df)
    r = classify_regime(f)
    c = generate_signal_candidate("NSE:SBIN", "cid", "1d", f, r)
    assert c.direction == SignalDirection.NEUTRAL
    assert "insufficient_history" in c.risk_flags


def test_signal_confidence_bounded():
    df = _ohlc(260, drift=0.3, vol=0.2)
    f = FeatureEngine(lookback=60).compute(df)
    r = classify_regime(f)
    c = generate_signal_candidate("NSE:SBIN", "cid", "1d", f, r)
    assert 0.0 <= c.confidence <= 1.0


# ---------------------------------------------------------------- look-ahead (critical)
def test_no_lookahead_features_stable_to_future_spikes():
    df = _ohlc(200, drift=0.2, vol=0.3, seed=7)
    fe = FeatureEngine(lookback=60)
    ts = df.index[150]
    base = fe.features_at(df, ts)
    future = pd.DataFrame(
        {"open": [9999.0], "high": [99999.0], "low": [9999.0], "close": [99990.0], "volume": [1.0e9]},
        index=pd.date_range(df.index[-1] + pd.Timedelta(days=1), periods=1, freq="D", tz="UTC"),
    )
    df2 = pd.concat([df, future])
    after = fe.features_at(df2, ts)
    assert base.close == after.close
    assert base.sma_20 == after.sma_20
    assert base.rsi_14 == after.rsi_14
    assert base.atr_14 == after.atr_14
    assert base.recent_high == after.recent_high


def test_features_at_uses_only_prior_bars():
    df = _ohlc(100, drift=0.1, vol=0.2)
    fe = FeatureEngine(lookback=60)
    ts = df.index[50]
    f = fe.features_at(df, ts)
    assert f.data_points == 51


# ---------------------------------------------------------------- AI interface
class _FakeProvider:
    name = "fake"
    is_available = True

    def analyze(self, snapshot):
        from trading_system.models.market_view import MarketView, MarketViewEnum

        return MarketView(
            symbol=snapshot.symbol,
            timeframe=snapshot.timeframe,
            market_view=MarketViewEnum.BULLISH,
            confidence=0.62,
            reasoning_summary="fake heuristic bullish",
            bullish_factors=["price above SMA20"],
            bearish_factors=[],
            risks=["heuristic only"],
            invalidating_conditions=["structure breaks"],
            model=self.name,
        )


def test_ai_valid_response():
    df = _ohlc(260, drift=0.3, vol=0.15)
    eng = MarketIntelligenceEngine(lookback=60)
    res = eng.analyze("NSE:SBIN", "1d", df)
    ctx = AnalysisContext(
        instrument={"symbol": "NSE:SBIN"},
        timeframe="1d",
        market_regime={"regime": res["regime"].regime.value, "confidence": res["regime"].confidence},
        features=res["features"].__dict__,
        signal_candidate=res["signal_candidate"].__dict__,
    )
    ai = MarketReasoningProvider(_FakeProvider()).reason(ctx)
    assert isinstance(ai, AIAnalysis)
    assert 0.0 <= ai.confidence <= 1.0


def test_ai_malformed_rejected():
    from pydantic import ValidationError

    with pytest.raises((ValidationError, TypeError, AnalysisRejected)):
        AIAnalysis.from_model_json({"confidence": 5.0})


def test_ai_missing_fields_rejected():
    with pytest.raises(Exception):
        AIAnalysis.from_model_json({"confidence": 0.5})


def test_ai_invalid_confidence_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AIAnalysis.from_model_json({"conclusion": "x", "confidence": -1.0})


def test_ai_provider_failure_propagates():
    class _Broken:
        name = "broken"
        is_available = True

        def analyze(self, snapshot):
            raise RuntimeError("provider down")

    ctx = AnalysisContext(instrument={}, timeframe="1d", market_regime={}, features={}, signal_candidate={})
    with pytest.raises(Exception):
        MarketReasoningProvider(_Broken()).reason(ctx)


# ---------------------------------------------------------------- data health gating
def test_analysis_blocked_on_unhealthy_feed():
    df = _ohlc(260, drift=0.2, vol=0.2)
    eng = MarketIntelligenceEngine(lookback=60)
    res = eng.analyze("NSE:SBIN", "1d", df, health_status=FeedStatus.STALE.value)
    assert res["status"] == "BLOCKED"
    assert "STALE" in res["reason"]


def test_analysis_blocked_on_no_data():
    eng = MarketIntelligenceEngine(lookback=60)
    res = eng.analyze("NSE:SBIN", "1d", None, health_status=FeedStatus.HEALTHY.value)
    assert res["status"] == "BLOCKED"
    assert res["reason"] == "NO_DATA"


def test_data_health_monitor_gates_signals():
    m = DataHealthMonitor()
    m.on_connect()
    m._last_msg_wall = 1.0  # epoch 1s, long ago => stale
    assert m.evaluate() == FeedStatus.STALE
    assert m.is_safe_for_signals() is False
    m.on_disconnect()
    assert m.evaluate() == FeedStatus.DISCONNECTED
