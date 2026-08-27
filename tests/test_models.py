"""Tests for MarketSnapshot, MarketView, providers, and the signal engine."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from trading_system.models.snapshot import MarketSnapshot, build_snapshot_from_df
from trading_system.models.market_view import MarketView, MarketViewEnum
from trading_system.models.local import LocalRuleModel
from trading_system.models.openai_compatible import OpenAICompatibleProvider
from trading_system.models.base import ModelProvider, ModelProviderError
from trading_system.models.provider_factory import get_model_provider
from trading_system.signals import generate_signal, SignalDirection, SignalConfig


UTC = timezone.utc
NOW = datetime(2024, 6, 1, 0, 0, tzinfo=UTC)


def _valid_snapshot(**overrides) -> MarketSnapshot:
    base = dict(
        symbol="BTCUSDT",
        timeframe="1d",
        timestamp=NOW,
        last_bar_timestamp=NOW,
        latest_price=100.0,
        last_return=0.01,
        sma_20=99.0,
        rsi_14=60.0,
        macd=0.5,
        macd_signal=0.3,
        atr_14=2.0,
        bollinger_upper=105.0,
        bollinger_lower=95.0,
        volatility_annualized=0.5,
        max_drawdown=-0.2,
        volume_ma=1000.0,
        volume_z=0.1,
        price_vs_sma20=0.01,
        recent_closes=[98.0, 99.0, 100.0],
        data_points=200,
        data_start=datetime(2023, 1, 1, tzinfo=UTC),
        data_end=NOW,
        lookahead_safe=True,
    )
    base.update(overrides)
    return MarketSnapshot(**base)


# ---------------- MarketSnapshot ----------------
def test_snapshot_valid():
    s = _valid_snapshot()
    assert s.symbol == "BTCUSDT"
    assert s.lookahead_safe is True


def test_snapshot_rejects_naive_timestamp():
    with pytest.raises(Exception):
        _valid_snapshot(timestamp=datetime(2024, 6, 1))  # naive


def test_snapshot_rejects_lookahead_timestamp():
    # timestamp after last_bar_timestamp => look-ahead
    with pytest.raises(Exception):
        _valid_snapshot(
            timestamp=datetime(2024, 6, 2, tzinfo=UTC),
            last_bar_timestamp=NOW,
        )


def test_snapshot_rejects_recent_closes_mismatch():
    with pytest.raises(Exception):
        _valid_snapshot(recent_closes=[98.0, 99.0, 999.0])  # last != latest_price


def test_snapshot_rejects_non_positive_price():
    with pytest.raises(Exception):
        _valid_snapshot(latest_price=0.0)


def test_snapshot_rejects_rsi_out_of_range():
    with pytest.raises(Exception):
        _valid_snapshot(rsi_14=150.0)


def test_build_snapshot_from_df_no_lookahead():
    idx = pd.date_range("2024-01-01", periods=120, freq="1D", tz=UTC)
    rng = __import__("numpy").random.default_rng(7)
    close = 100 + rng.normal(0, 1, 120).cumsum()
    df = pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": rng.integers(100, 1000, 120).astype(float),
        },
        index=idx,
    )
    snap = build_snapshot_from_df(df, "BTCUSDT", "1d")
    assert snap.timestamp == snap.last_bar_timestamp
    assert snap.recent_closes[-1] == pytest.approx(snap.latest_price)
    assert snap.data_points == 120
    # Decision timestamp equals last available bar (no future).
    assert snap.timestamp == idx[-1]


# ---------------- MarketView ----------------
def _valid_view(**o) -> dict:
    d = dict(
        symbol="BTCUSDT",
        timeframe="1d",
        market_view="bullish",
        confidence=0.6,
        reasoning_summary="Multiple indicators aligned to the upside.",
        bullish_factors=["RSI rising", "MACD above signal"],
        bearish_factors=[],
        risks=["crypto volatility"],
        invalidating_conditions=["loss of support"],
        model="local-rule",
    )
    d.update(o)
    return d


def test_marketview_valid():
    v = MarketView(**_valid_view())
    assert v.market_view == MarketViewEnum.BULLISH
    assert 0 <= v.confidence <= 1


def test_marketview_rejects_bad_enum():
    with pytest.raises(Exception):
        MarketView(**_valid_view(market_view="superbullish"))


def test_marketview_rejects_confidence_over_1():
    with pytest.raises(Exception):
        MarketView(**_valid_view(confidence=1.5))


def test_marketview_requires_factors_for_bullish():
    with pytest.raises(Exception):
        MarketView(**_valid_view(bullish_factors=[]))


def test_marketview_rejects_high_conf_low_reasoning():
    with pytest.raises(Exception):
        MarketView(**_valid_view(confidence=0.95, reasoning_summary="x"))


def test_marketview_from_json_accepts_valid():
    v = MarketView.from_model_json(_valid_view(model="test"))
    assert v.model == "test"


def test_marketview_from_json_rejects_malformed():
    with pytest.raises(Exception):
        # missing required fields
        MarketView.from_model_json({"symbol": "BTCUSDT"})


def test_marketview_from_json_rejects_non_dict():
    with pytest.raises(TypeError):
        MarketView.from_model_json("bullish because reasons")


# ---------------- ModelProvider ----------------
def test_local_rule_provider_runs_offline():
    prov = LocalRuleModel()
    assert prov.is_available
    snap = _valid_snapshot(price_vs_sma20=0.05, rsi_14=65.0, macd=0.6, macd_signal=0.2)
    view = prov.analyze(snap)
    assert isinstance(view, MarketView)
    assert view.market_view in list(MarketViewEnum)


def test_local_rule_provider_is_deterministic():
    snap = _valid_snapshot(price_vs_sma20=-0.05, rsi_14=25.0, macd=-0.6, macd_signal=-0.2)
    prov = LocalRuleModel()
    v1 = prov.analyze(snap)
    v2 = prov.analyze(snap)
    assert v1.model_dump() == v2.model_dump()


def test_openai_provider_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("MY_KEY", raising=False)
    prov = OpenAICompatibleProvider(api_key_env="MY_KEY", api_base="http://x/v1")
    assert prov.is_available is False
    with pytest.raises(ModelProviderError):
        prov.analyze(_valid_snapshot())


def test_factory_returns_local_by_default():
    assert isinstance(get_model_provider("local"), LocalRuleModel)


def test_factory_unknown_raises():
    with pytest.raises(ValueError):
        get_model_provider("nonsense")


# ---------------- Signal engine ----------------
def test_signal_long_on_aligned_bullish():
    snap = _valid_snapshot(price_vs_sma20=0.05, macd=0.6, macd_signal=0.2, rsi_14=62.0)
    view = LocalRuleModel().analyze(snap)
    sig = generate_signal(snap, view)
    assert sig.direction == SignalDirection.LONG


def test_signal_hold_on_neutral_view():
    snap = _valid_snapshot()
    view = MarketView(**_valid_view(market_view="neutral", bullish_factors=[], bearish_factors=[]))
    sig = generate_signal(snap, view)
    assert sig.direction == SignalDirection.HOLD


def test_signal_hold_on_low_confidence():
    snap = _valid_snapshot(price_vs_sma20=0.05, macd=0.6, macd_signal=0.2, rsi_14=62.0)
    view = LocalRuleModel().analyze(snap).model_dump()
    view["confidence"] = 0.1  # below min_confidence
    from trading_system.models.market_view import MarketView as MV

    v = MV(**view)
    sig = generate_signal(snap, v, SignalConfig(min_confidence=0.5))
    assert sig.direction == SignalDirection.HOLD


def test_signal_short_on_aligned_bearish():
    snap = _valid_snapshot(price_vs_sma20=-0.05, macd=-0.6, macd_signal=-0.2, rsi_14=38.0)
    view = LocalRuleModel().analyze(snap)
    sig = generate_signal(snap, view)
    assert sig.direction == SignalDirection.SHORT


def test_signal_hold_on_insufficient_data():
    snap = _valid_snapshot(data_points=5)
    view = LocalRuleModel().analyze(_valid_snapshot())
    sig = generate_signal(snap, view)
    assert sig.direction == SignalDirection.HOLD
    assert "insufficient data" in sig.reason


def test_signal_reason_is_recorded():
    snap = _valid_snapshot(price_vs_sma20=0.05, macd=0.6, macd_signal=0.2, rsi_14=62.0)
    view = LocalRuleModel().analyze(snap)
    sig = generate_signal(snap, view)
    assert sig.reason  # non-empty explanation
