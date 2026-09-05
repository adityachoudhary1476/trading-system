"""Deterministic synthetic fixtures for the V3 intelligence suite.

SYNTHETIC / TEST DATA — NOT LIVE MARKET DATA.

Every builder in this module produces seeded, reproducible synthetic data for
tests and the research demo. Nothing here represents real market conditions
and nothing here may be used in a production/live data path. Scenarios map to
the V3 fixture matrix A–P (see tests/test_v3_fixtures.py).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

SYNTHETIC_TAG = "SYNTHETIC/TEST"

_INDEX_CACHE: dict[tuple[int, str, str], pd.DatetimeIndex] = {}


def _index(n: int, freq: str = "1D", start: str = "2025-01-01") -> pd.DatetimeIndex:
    key = (n, freq, start)
    if key not in _INDEX_CACHE:
        _INDEX_CACHE[key] = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return _INDEX_CACHE[key]


def _frame(closes: np.ndarray, opens: np.ndarray, bar: float,
           volume: np.ndarray, freq: str = "1D") -> pd.DataFrame:
    return pd.DataFrame({
        "open": opens,
        "high": np.maximum(closes, opens) + bar,
        "low": np.minimum(closes, opens) - bar,
        "close": closes,
        "volume": volume,
    }, index=_index(len(closes), freq=freq))


def ohlcv(n: int = 120, start_px: float = 100.0, daily_drift: float = 0.2,
          daily_vol: float = 1.0, seed: int = 1, freq: str = "1D") -> pd.DataFrame:
    """Seeded random-walk OHLCV. Deterministic for identical arguments."""
    rng = np.random.default_rng(seed)
    closes = np.maximum(start_px + np.cumsum(rng.normal(daily_drift, daily_vol, n)), 1.0)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    bar = max(daily_vol, 0.05) * 0.8
    volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
    return _frame(closes, opens, bar, volume, freq=freq)


def regime_transition(n: int = 140, start_px: float = 100.0, seed: int = 16) -> pd.DataFrame:
    """(P) Uptrend that decays into a range — trending -> range transition."""
    rng = np.random.default_rng(seed)
    trend_len = n * 2 // 3
    trend = start_px + np.cumsum(rng.normal(0.35, 0.5, trend_len))
    flat = trend[-1] + 1.2 * np.sin(np.arange(n - trend_len) * 2 * np.pi / 9)
    closes = np.concatenate([trend, flat])
    opens = np.concatenate([[closes[0]], closes[:-1]])
    volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
    return _frame(closes, opens, 0.4, volume)


# --- Scenario OHLCV fixtures (A–E, L, K, O) -------------------------------- #
def bullish_aligned(seed: int = 11) -> pd.DataFrame:
    """(A) Strong multi-factor bullish uptrend."""
    return ohlcv(120, 100.0, 0.35, 0.8, seed=seed)


def bearish_aligned(seed: int = 12) -> pd.DataFrame:
    """(B) Strong multi-factor bearish downtrend."""
    return ohlcv(120, 100.0, -0.35, 0.8, seed=seed)


def conflicting_timeframes() -> dict[str, pd.DataFrame]:
    """(C) Intraday bearish, daily bullish — higher-timeframe conflict."""
    return {
        "5m": ohlcv(80, 100.0, -0.15, 0.4, seed=31, freq="5min"),
        "15m": ohlcv(80, 100.0, -0.12, 0.4, seed=32, freq="15min"),
        "1h": ohlcv(80, 100.0, -0.10, 0.4, seed=33, freq="1h"),
        "1d": ohlcv(120, 100.0, 0.30, 0.6, seed=34, freq="1D"),
    }


def high_volatility(seed: int = 14) -> pd.DataFrame:
    """(D) Elevated-volatility market."""
    return ohlcv(120, 100.0, 0.05, 4.0, seed=seed)


def low_volatility(seed: int = 15) -> pd.DataFrame:
    """(E) Compressed-volatility market."""
    return ohlcv(120, 100.0, 0.05, 0.15, seed=seed)


def insufficient_history(seed: int = 21) -> pd.DataFrame:
    """(L) Below the FeatureEngine.MIN_BARS threshold."""
    return ohlcv(20, 100.0, 0.2, 1.0, seed=seed)


def evidence_conflict(seed: int = 22) -> pd.DataFrame:
    """(O) Strong uptrend stretched into overbought RSI — trend vs momentum."""
    return ohlcv(120, 100.0, 0.55, 0.5, seed=seed)


def stale_ohlcv(seed: int = 23, stale_days: int = 90) -> pd.DataFrame:
    """(K) Well-formed history whose last bar is far in the past."""
    df = ohlcv(120, 100.0, 0.2, 0.8, seed=seed)
    old_end = pd.Timestamp.utcnow().tz_convert("UTC").normalize() - pd.Timedelta(days=stale_days)
    df.index = pd.date_range(old_end - pd.Timedelta(days=119), periods=120, freq="1D", tz="UTC")
    return df


# --- Context fixtures (F–J, M, N + combined) ------------------------------- #
# All values below are INVENTED TEST VALUES tagged source=SYNTHETIC/TEST.
# They exercise the optional-context plumbing; never presented as live.
from trading_system.research.market_context import (  # noqa: E402
    CrossAssetContext, DataQualityTier, FIIDIIFlow, IndiaVIXContext,
    InstitutionalFlow, MarketBreadth, MarketIntelligenceContext,
    NewsContext, NewsEvent, NewsEventType, SectorContext,
)

SRC = "SYNTHETIC/TEST"


def strong_breadth() -> MarketBreadth:
    """(H) Broad advance: ~72% advancing."""
    return MarketBreadth(advancing_count=1840, declining_count=690, unchanged_count=70,
                         new_highs=210, new_lows=25, source=SRC,
                         data_quality=DataQualityTier.HEALTHY)


def weak_breadth() -> MarketBreadth:
    """(I) Broad decline: ~24% advancing."""
    return MarketBreadth(advancing_count=610, declining_count=1920, unchanged_count=70,
                         new_highs=30, new_lows=240, source=SRC,
                         data_quality=DataQualityTier.HEALTHY)


def calm_vix() -> IndiaVIXContext:
    """Low, falling volatility context."""
    return IndiaVIXContext(india_vix=12.4, vix_change=-0.3, vix_percentile=28.0,
                           source=SRC, data_quality=DataQualityTier.HEALTHY)


def high_vix() -> IndiaVIXContext:
    """Elevated, rising volatility context."""
    return IndiaVIXContext(india_vix=21.8, vix_change=2.1, vix_percentile=86.0,
                           source=SRC, data_quality=DataQualityTier.HEALTHY)


def bullish_fii_dii() -> FIIDIIFlow:
    """(Supportive flows) FII net +3000cr, DII net +1500cr."""
    return FIIDIIFlow(fii=InstitutionalFlow(buy=12000.0, sell=9000.0),
                      dii=InstitutionalFlow(buy=6500.0, sell=5000.0),
                      source=SRC, data_quality=DataQualityTier.HEALTHY)


def bearish_fii_dii() -> FIIDIIFlow:
    """(Adverse flows) FII net -4200cr, DII net +800cr."""
    return FIIDIIFlow(fii=InstitutionalFlow(buy=7800.0, sell=12000.0),
                      dii=InstitutionalFlow(buy=5200.0, sell=4400.0),
                      source=SRC, data_quality=DataQualityTier.HEALTHY)


def sector_outperforming() -> SectorContext:
    """(F) Sector beating the market by +1.1pp."""
    return SectorContext(sector_symbol="NIFTY BANK", sector_name="Banking",
                         sector_return=1.5, relative_strength=1.1,
                         source=SRC, data_quality=DataQualityTier.HEALTHY)


def sector_underperforming() -> SectorContext:
    """(G) Sector lagging the market by -1.4pp."""
    return SectorContext(sector_symbol="NIFTY BANK", sector_name="Banking",
                         sector_return=-0.9, relative_strength=-1.4,
                         source=SRC, data_quality=DataQualityTier.HEALTHY)


def _put(strike: float, spot: float, *, volume: float, oi: float, spread: float,
         greeks: bool = True, iv: float = 0.16) -> dict:
    """One synthetic PE chain row (structure only; values are test values)."""
    row = {
        "strike": float(strike), "option_type": "PE", "expiry": "2026-09-10",
        "open_interest": oi, "volume": volume,
        "bid": round(max(2.0, spot * 0.012 * (1 - abs(strike - spot) / spot * 8)), 1),
        "ask": round(max(2.0, spot * 0.012 * (1 - abs(strike - spot) / spot * 8)) * (1 + spread), 1),
        "last_price": round(spot * 0.013, 1), "oi_change": 1200.0,
        "implied_vol": iv, "implied_volatility": iv,
    }
    if greeks:
        row.update({"delta": round(-0.5 + (strike - spot) / spot * 8, 2),
                    "gamma": 0.0004, "theta": -0.014, "vega": 18.5})
    return row


def liquid_option_chain(spot: float = 23950.0) -> list[dict]:
    """(M) Liquid NIFTY-like PE chain: tight spreads, deep OI/volume, full Greeks."""
    return [_put(k, spot, volume=180_000, oi=420_000, spread=0.008)
            for k in (23400, 23700, 23800, 23900, 24000, 24100, 24300, 24600)]


def illiquid_option_chain(spot: float = 23950.0) -> list[dict]:
    """(N) Illiquid chain: tiny volume/OI, wide spreads."""
    return [_put(k, spot, volume=40, oi=120, spread=0.35)
            for k in (23400, 23700, 23900, 24100, 24400)]


def partial_option_chain(spot: float = 23950.0) -> list[dict]:
    """(Missing-field case) Liquid rows but NO Greeks / IV supplied."""
    return [_put(k, spot, volume=150_000, oi=380_000, spread=0.010, greeks=False, iv=0.0)
            for k in (23700, 23900, 24100)]


def news_context_bullish() -> NewsContext:
    """(News supportive) Two positive synthetic events."""
    t = pd.Timestamp("2026-09-04T10:00:00Z")
    return NewsContext(events=[
        NewsEvent(timestamp=t, source=SRC, headline="SYNTHETIC: earnings beat",
                  symbol="NSE:SBIN-EQ", sector="Banking", sentiment=0.7,
                  sentiment_confidence=0.8, relevance=0.9,
                  event_type=NewsEventType.EARNINGS,
                  data_quality=DataQualityTier.HEALTHY),
        NewsEvent(timestamp=t, source=SRC, headline="SYNTHETIC: supportive policy",
                  symbol=None, sector=None, sentiment=0.5,
                  sentiment_confidence=0.7, relevance=0.6,
                  event_type=NewsEventType.MACRO,
                  data_quality=DataQualityTier.HEALTHY),
    ])


def news_context_bearish() -> NewsContext:
    """(News adverse) Negative synthetic regulatory event."""
    return NewsContext(events=[
        NewsEvent(timestamp=pd.Timestamp("2026-09-04T10:00:00Z"), source=SRC,
                  headline="SYNTHETIC: regulatory action", symbol="NSE:SBIN-EQ",
                  sector="Banking", sentiment=-0.7, sentiment_confidence=0.85,
                  relevance=0.95, event_type=NewsEventType.REGULATORY,
                  data_quality=DataQualityTier.HEALTHY),
    ])


def cross_asset_risk_on() -> CrossAssetContext:
    """(Cross-asset) Risk-on synthetic snapshot."""
    return CrossAssetContext(usdinr=83.1, usdinr_change=-0.15, us_index_change=0.8,
                             crude_oil_change=-1.2, gold_change=0.3,
                             bond_yield_10y=6.92, source=SRC,
                             data_quality=DataQualityTier.HEALTHY)


def full_market_context() -> MarketIntelligenceContext:
    """Combined supportive context (all synthetic)."""
    return MarketIntelligenceContext(
        breadth=strong_breadth(), vix=calm_vix(),
        institutional_flow=bullish_fii_dii(),
        sector=sector_outperforming(), news=news_context_bullish(),
        cross_asset=cross_asset_risk_on(),
    )


def empty_market_context() -> MarketIntelligenceContext:
    """(J) Every optional source unavailable — the honest default."""
    return MarketIntelligenceContext()
