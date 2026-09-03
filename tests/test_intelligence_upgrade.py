"""Upgraded intelligence engine tests: evidence-based confidence, instrument
specificity, multi-timeframe horizons, expected move and options candidates.

Catches the historic defects:
- identical bias/confidence for different instruments,
- confidence clustering at ~70%,
- options candidates not reacting to chain changes,
- illiquid options not rejected,
- stale/missing data not lowering confidence,
- conflicting evidence not lowering confidence.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_system.research.intelligence import (
    FeatureEngine,
    MarketRegime,
    RegimeEnum,
    SignalDirection,
    TrendEnum,
    InstrumentClass,
    EvidenceLedger,
    TimeHorizon,
    TechnicalFeatures,
    classify_regime,
    generate_signal_candidate,
    instrument_class_of,
    compute_evidence_confidence,
    compute_expected_move,
    determine_horizon,
    compute_invalidation,
    analyze_multi_timeframe,
    generate_options_candidates,
    _build_instrument_context,
    DataCompleteness,
    RelativeStrength,
    compute_data_completeness,
    compute_relative_strength,
)
from trading_system.research import MarketIntelligenceEngine


def _ohlc(n: int, start: float = 100.0, drift: float = 0.1, vol: float = 0.5, seed: int = 1) -> pd.DataFrame:
    """Deterministic OHLCV with tz-aware UTC index."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    closes = start + np.cumsum(rng.normal(drift, vol, n))
    closes = np.maximum(closes, 1.0)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(closes, opens) + np.abs(rng.normal(0, vol, n))
    lows = np.minimum(closes, opens) - np.abs(rng.normal(0, vol, n))
    lows = np.maximum(lows, 0.5)
    vols = rng.integers(1_000_000, 5_000_000, n).astype(float)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
        index=idx,
    )


def _flat_series(n: int, base: float = 100.0, amp: float = 1.5, period: int = 7) -> pd.DataFrame:
    """Mean-reverting oscillation -> range-bound / neutral regime."""
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    closes = base + amp * np.sin(np.arange(n) * 2 * np.pi / period)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(closes, opens) + 0.3
    lows = np.minimum(closes, opens) - 0.3
    vols = np.full(n, 2_000_000.0)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
        index=idx,
    )


def _chain(strikes, spot: float, opt: str = "CE", liq_vol: float = 5000, liq_oi: float = 20000,
           spread_mult: float = 1.0, delta=None) -> list[dict]:
    """LIVE-chain-shaped rows (structure only; caller passes the values)."""
    rows = []
    for i, k in enumerate(strikes):
        d = delta[i] if delta else (0.55 - i * 0.12)
        bid = max(5.0, spot * 0.01 * (1 - i * 0.2))
        rows.append({
            "strike": float(k),
            "option_type": opt,
            "expiry": "2026-09-10",
            "delta": round(d, 2),
            "theta": -0.02,
            "implied_vol": 0.22,
            "open_interest": liq_oi,
            "volume": liq_vol,
            "bid": round(bid, 1),
            "ask": round(bid * (1 + 0.01 * spread_mult), 1),
        })
    return rows


# --------------------------------------------------------------------------- #
# Test A / B / C / L — bias + confidence are input-dependent, not fixed
# --------------------------------------------------------------------------- #
class TestEvidenceBasedConfidence:

    def test_confidence_varies_with_market_conditions(self):
        """(B) Different market inputs produce different confidence values."""
        eng = MarketIntelligenceEngine(lookback=60)
        confidences = []
        for drift in [0.5, 0.3, 0.1, -0.1, -0.3, -0.5]:
            df = _ohlc(100, drift=drift, vol=0.2, seed=int(abs(drift) * 10) + 1)
            result = eng.analyze("NSE:SBIN", "1d", df)
            confidences.append(result["signal_candidate"].confidence)
        assert len(set(confidences)) > 1

    def test_confidence_not_clustered_70(self):
        """(C) Confidence must NOT always sit at/around 70% (0.65-0.75)."""
        eng = MarketIntelligenceEngine(lookback=60)
        confidences = []
        for drift in [0.5, 0.3, 0.1, -0.1, -0.3, -0.5]:
            df = _ohlc(100, drift=drift, vol=0.2, seed=int(abs(drift) * 10) + 1)
            result = eng.analyze("NSE:SBIN", "1d", df)
            confidences.append(result["signal_candidate"].confidence)
        near_70 = sum(1 for c in confidences if 0.65 <= c <= 0.75)
        assert near_70 < len(confidences)

    def test_strong_trend_higher_confidence_than_weak(self):
        """(L) Multi-factor agreement lifts confidence vs conflicting evidence."""
        # All bullish signals agree.
        agree = TechnicalFeatures(
            close=105.0, data_points=120, sma_200=95.0, rsi_14=58.0,
            price_vs_sma20=0.04, price_vs_sma50=0.03, relative_volume=1.4,
            recent_high=105.5, recent_low=95.0, trend=TrendEnum.BULLISH,
        )
        # Price bullish but momentum overbought, volume weak, vol regime high.
        conflict = TechnicalFeatures(
            close=105.0, data_points=120, sma_200=95.0, rsi_14=76.0,
            price_vs_sma20=0.04, price_vs_sma50=0.02, relative_volume=0.5,
            recent_high=105.0, recent_low=95.0, trend=TrendEnum.BULLISH,
        )
        ctx = _build_instrument_context("NSE:SBIN")
        regime_up = MarketRegime(RegimeEnum.TRENDING_UP, 0.75)
        regime_hv = MarketRegime(RegimeEnum.HIGH_VOLATILITY, 0.6)
        conf_agree, _ = compute_evidence_confidence(agree, regime_up, ctx)
        conf_conflict, _ = compute_evidence_confidence(conflict, regime_hv, ctx)
        assert conf_agree > conf_conflict

    def test_bias_varies_with_inputs(self):
        """(A) Different market inputs can produce different biases."""
        eng = MarketIntelligenceEngine(lookback=60)
        up = eng.analyze("NSE:SBIN", "1d", _ohlc(100, drift=0.6, vol=0.15, seed=3))["signal_candidate"].direction
        down = eng.analyze("NSE:SBIN", "1d", _ohlc(100, drift=-0.6, vol=0.15, seed=3))["signal_candidate"].direction
        assert up != down

    def test_evidence_ledger_populated(self):
        """Evidence ledger carries structured, symbol-specific evidence."""
        df = _ohlc(100, drift=0.3, vol=0.2)
        eng = MarketIntelligenceEngine(lookback=60)
        result = eng.analyze("NSE:SBIN", "1d", df)

        ctx = _build_instrument_context("NSE:SBIN")
        conf, ledger = compute_evidence_confidence(result["features"], result["regime"], ctx)

        assert isinstance(ledger, EvidenceLedger)
        assert 0 <= conf <= 100
        assert ledger.positive or ledger.neutral or ledger.negative


# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Test D / E / F — instrument specificity, no cross-instrument inheritance
# --------------------------------------------------------------------------- #
class TestInstrumentSpecificAnalysis:

    def test_nifty_context(self):
        """(E) NIFTY 50 gets index-specific context."""
        ctx = _build_instrument_context("NSE:NIFTY 50-INDEX")
        assert ctx.is_nifty is True
        assert ctx.is_bank_nifty is False
        assert ctx.is_index is True
        assert ctx.instrument_class == InstrumentClass.INDEX

    def test_bank_nifty_context(self):
        """(F) BANK NIFTY gets its own context — not NIFTY's."""
        ctx = _build_instrument_context("NSE:NIFTYBANK-INDEX")
        assert ctx.is_nifty is False
        assert ctx.is_bank_nifty is True
        assert ctx.is_index is True

    def test_stock_context(self):
        """Stocks are equities, never misclassified as indices."""
        ctx = _build_instrument_context("NSE:SBIN")
        assert ctx.is_nifty is False
        assert ctx.is_bank_nifty is False
        assert ctx.is_index is False
        assert ctx.instrument_class == InstrumentClass.EQUITY

    def test_different_volatility_thresholds(self):
        """(D) Instrument-specific vol bands differ NIFTY vs BANKNIFTY vs stock."""
        ctx_nifty = _build_instrument_context("NSE:NIFTY 50-INDEX")
        ctx_bank = _build_instrument_context("NSE:NIFTYBANK-INDEX")
        ctx_sbin = _build_instrument_context("NSE:SBIN")
        assert ctx_nifty.high_vol_threshold != ctx_bank.high_vol_threshold
        assert ctx_sbin.high_vol_threshold != ctx_nifty.high_vol_threshold

    def test_nifty_does_not_inherit_stock_analysis(self):
        """(E) NIFTY analysis is structurally distinct from a stock's."""
        eng = MarketIntelligenceEngine(lookback=60)
        r_nifty = eng.analyze("NSE:NIFTY 50-INDEX", "1d", _ohlc(120, 24000, 12, 90, seed=11))
        r_sbin = eng.analyze("NSE:SBIN", "1d", _ohlc(120, 24000, 12, 90, seed=11))
        ctx_n = r_nifty["instrument_context"]
        ctx_s = r_sbin["instrument_context"]
        assert ctx_n.is_index and not ctx_s.is_index
        assert (ctx_n.high_vol_threshold, ctx_n.low_vol_threshold) != \
               (ctx_s.high_vol_threshold, ctx_s.low_vol_threshold)
        assert r_nifty["signal_candidate"].invalidation_context != \
               r_sbin["signal_candidate"].invalidation_context

    def test_bank_nifty_does_not_inherit_nifty_analysis(self):
        """(F) BANK NIFTY keeps its own (wider) vol band and flags."""
        eng = MarketIntelligenceEngine(lookback=60)
        r_nifty = eng.analyze("NSE:NIFTY 50-INDEX", "1d", _ohlc(120, 24000, 12, 90, seed=11))
        r_bank = eng.analyze("NSE:NIFTYBANK-INDEX", "1d", _ohlc(120, 24000, 12, 90, seed=11))
        ctx_n = r_nifty["instrument_context"]
        ctx_b = r_bank["instrument_context"]
        assert ctx_n.is_nifty and not ctx_b.is_nifty
        assert ctx_b.is_bank_nifty and not ctx_n.is_bank_nifty
        assert ctx_b.high_vol_threshold != ctx_n.high_vol_threshold

# Test D / E / F — instrument-specific contexts, no cross-inheritance
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Expected move + horizon + invalidation (Phases 7 / 8)
# --------------------------------------------------------------------------- #
class TestExpectedMoveAndHorizon:

    def test_expected_move_present(self):
        result = MarketIntelligenceEngine(lookback=60).analyze(
            "NSE:SBIN", "1d", _ohlc(100, drift=0.2, vol=0.2))
        assert result["signal_candidate"].expected_move is not None

    def test_expected_move_symmetric(self):
        em = MarketIntelligenceEngine(lookback=60).analyze(
            "NSE:SBIN", "1d", _ohlc(100, drift=0.2, vol=0.2))["signal_candidate"].expected_move
        assert em.lower_pct < 0 < em.upper_pct

    def test_expected_move_scales_with_horizon(self):
        features = FeatureEngine(lookback=60).compute(_ohlc(100, drift=0.2, vol=0.2))
        ctx = _build_instrument_context("NSE:SBIN")
        em_intraday = compute_expected_move(features, TimeHorizon.INTRADAY, ctx)
        em_swing = compute_expected_move(features, TimeHorizon.SWING, ctx)
        assert abs(em_swing.lower_pct) > abs(em_intraday.lower_pct)

    def test_horizon_follows_regime(self):
        """Trending -> swing; range-bound -> intraday; not randomly assigned.

        Regime objects are constructed directly so the horizon MAPPING is
        tested deterministically (not dependent on synthetic-data flavor).
        """
        f_trend = FeatureEngine(lookback=60).compute(_ohlc(120, drift=0.5, vol=0.1))
        assert determine_horizon(f_trend, MarketRegime(RegimeEnum.TRENDING_UP, 0.7)) == TimeHorizon.SWING
        assert determine_horizon(f_trend, MarketRegime(RegimeEnum.TRENDING_DOWN, 0.7)) == TimeHorizon.SWING
        assert determine_horizon(f_trend, MarketRegime(RegimeEnum.RANGE_BOUND, 0.6)) == TimeHorizon.INTRADAY
        assert determine_horizon(f_trend, MarketRegime(RegimeEnum.HIGH_VOLATILITY, 0.7)) == TimeHorizon.SHORT_TERM

    def test_invalidation_direction(self):
        """SHORT invalidation sits above price; LONG below."""
        eng = MarketIntelligenceEngine(lookback=60)
        short_c = eng.analyze("NSE:SBIN", "1d", _ohlc(120, drift=-0.5, vol=0.1))["signal_candidate"]
        long_c = eng.analyze("NSE:SBIN", "1d", _ohlc(120, drift=0.5, vol=0.1))["signal_candidate"]
        if short_c.direction == SignalDirection.SHORT:
            assert "above" in short_c.invalidation_context
        if long_c.direction == SignalDirection.LONG:
            assert "below" in long_c.invalidation_context


# --------------------------------------------------------------------------- #
# Test G — multi-timeframe views can disagree
# --------------------------------------------------------------------------- #
class TestMultiTimeframe:

    def test_timeframes_can_disagree(self):
        """(G) Bearish intraday vs bullish daily is representable."""
        ctx = _build_instrument_context("NSE:NIFTY 50-INDEX")
        dfs = {"5m": _ohlc(80, 24050, -1.5, 12, seed=31),
               "1d": _ohlc(120, 23800, 12, 90, seed=11)}
        mtf = analyze_multi_timeframe("NSE:NIFTY 50-INDEX", dfs, ctx)
        assert mtf["5m"].bias != mtf["1d"].bias

    def test_each_timeframe_has_own_evidence(self):
        ctx = _build_instrument_context("NSE:NIFTY 50-INDEX")
        dfs = {"5m": _ohlc(80, 24050, -1.5, 12, seed=31),
               "15m": _ohlc(80, 24060, -1.0, 14, seed=32),
               "1d": _ohlc(120, 23800, 12, 90, seed=11)}
        mtf = analyze_multi_timeframe("NSE:NIFTY 50-INDEX", dfs, ctx)
        assert mtf["5m"].evidence is not mtf["1d"].evidence
        assert mtf["5m"].confidence != mtf["1d"].confidence

    def test_insufficient_timeframe_is_explicit(self):
        """Thin data -> explicit NEUTRAL + reason, never a fabricated view."""
        ctx = _build_instrument_context("NSE:SBIN")
        mtf = analyze_multi_timeframe("NSE:SBIN", {"5m": _ohlc(10, 100, 0.1, 1)}, ctx)
        ta = mtf["5m"]
        assert ta.bias == TrendEnum.NEUTRAL
        assert ta.confidence == 0.0
        assert ta.evidence.missing

class TestInstrumentSpecificAnalysis:

    def test_nifty_context(self):
        """NIFTY 50 gets index-specific context (not stock defaults)."""
        ctx = _build_instrument_context("NSE:NIFTY50")
        assert ctx.is_nifty is True
        assert ctx.is_bank_nifty is False
        assert ctx.is_index is True

    def test_bank_nifty_context(self):
        """BANK NIFTY gets its own context (not NIFTY's)."""
        ctx = _build_instrument_context("NSE:BANKNIFTY")
        assert ctx.is_nifty is False
        assert ctx.is_bank_nifty is True
        assert ctx.is_index is True

    def test_stock_context(self):
        """Individual stocks do NOT inherit index context."""
        ctx = _build_instrument_context("NSE:SBIN")
        assert ctx.is_nifty is False
        assert ctx.is_bank_nifty is False
        assert ctx.is_index is False

    def test_reliance_not_treated_as_index(self):
        """(E) RELIANCE must not be classified as an index."""
        ctx = _build_instrument_context("NSE:RELIANCE")
        assert ctx.is_index is False
        assert ctx.is_nifty is False

    def test_different_volatility_thresholds(self):
        """Different instruments carry different vol tolerance bands."""
        ctx_nifty = _build_instrument_context("NSE:NIFTY50")
        ctx_bank = _build_instrument_context("NSE:BANKNIFTY")
        ctx_sbin = _build_instrument_context("NSE:SBIN")
        assert ctx_nifty.high_vol_threshold != ctx_bank.high_vol_threshold
        assert ctx_sbin.high_vol_threshold != ctx_nifty.high_vol_threshold

    def test_symbol_specific_evidence(self):
        """(D) Two symbols fed different data produce different evidence text."""
        eng = MarketIntelligenceEngine(lookback=60)
        r_up = eng.analyze("NSE:SBIN", "1d", _ohlc(100, drift=0.6, vol=0.15, seed=5))
        r_down = eng.analyze("NSE:TCS", "1d", _ohlc(100, drift=-0.6, vol=0.15, seed=5))
        assert r_up["signal_candidate"].direction != r_down["signal_candidate"].direction
        assert r_up["signal_candidate"].supporting_features != r_down["signal_candidate"].supporting_features


# --------------------------------------------------------------------------- #
# Expected move + horizon
# --------------------------------------------------------------------------- #
class TestExpectedMove:

    def test_expected_move_present(self):
        """Expected move calculated when data is sufficient."""
        eng = MarketIntelligenceEngine(lookback=60)
        result = eng.analyze("NSE:SBIN", "1d", _ohlc(100, drift=0.2, vol=0.2))
        assert result["signal_candidate"].expected_move is not None

    def test_expected_move_straddles_zero(self):
        """Expected move straddles zero (estimated range, not certainty)."""
        eng = MarketIntelligenceEngine(lookback=60)
        em = eng.analyze("NSE:SBIN", "1d", _ohlc(100, drift=0.2, vol=0.2))["signal_candidate"].expected_move
        assert em.lower_pct < 0 < em.upper_pct

    def test_expected_move_scales_with_horizon(self):
        """Swing horizon implies a wider estimated range than intraday."""
        features = FeatureEngine(lookback=60).compute(_ohlc(100, drift=0.2, vol=0.2))
        ctx = _build_instrument_context("NSE:SBIN")
        em_intraday = compute_expected_move(features, TimeHorizon.INTRADAY, ctx)
        em_swing = compute_expected_move(features, TimeHorizon.SWING, ctx)
        assert abs(em_swing.lower_pct) > abs(em_intraday.lower_pct)
        assert abs(em_swing.upper_pct) > abs(em_intraday.upper_pct)

    def test_horizon_determined_from_regime(self):
        """Horizon derives deterministically from the regime, never random."""
        f = TechnicalFeatures(close=100.0, data_points=120)
        assert determine_horizon(f, MarketRegime(RegimeEnum.TRENDING_UP, 0.7)) == TimeHorizon.SWING
        assert determine_horizon(f, MarketRegime(RegimeEnum.TRENDING_DOWN, 0.7)) == TimeHorizon.SWING
        assert determine_horizon(f, MarketRegime(RegimeEnum.HIGH_VOLATILITY, 0.6)) == TimeHorizon.SHORT_TERM
        assert determine_horizon(f, MarketRegime(RegimeEnum.RANGE_BOUND, 0.6)) == TimeHorizon.INTRADAY
        assert determine_horizon(f, MarketRegime(RegimeEnum.LOW_VOLATILITY, 0.5)) == TimeHorizon.SHORT_TERM


# --------------------------------------------------------------------------- #
# Test G — multi-timeframe disagreement is representable
# --------------------------------------------------------------------------- #
class TestMultiTimeframe:

    def test_different_timeframes_can_disagree(self):
        """(G) Horizons can produce different biases (no forced agreement)."""
        ctx = _build_instrument_context("NSE:NIFTY50")
        dfs = {
            "1d": _ohlc(120, drift=-0.4, vol=0.2, seed=11),   # bearish daily
            "1h": _ohlc(120, drift=0.4, vol=0.2, seed=11),    # bullish hourly
        }
        results = analyze_multi_timeframe("NSE:NIFTY50", dfs, ctx)
        assert results["1d"].bias != results["1h"].bias

    def test_insufficient_data_is_neutral_not_fabricated(self):
        """Missing data -> explicit NEUTRAL + reason, never invented signal."""
        ctx = _build_instrument_context("NSE:NIFTY50")
        dfs = {"1d": _ohlc(120, drift=0.4, vol=0.2), "5m": _ohlc(5, drift=0.4, vol=0.2)}
        results = analyze_multi_timeframe("NSE:NIFTY50", dfs, ctx)
        weak = results["5m"]
        assert weak.bias == TrendEnum.NEUTRAL
        assert weak.confidence == 0.0

    def test_confidence_differs_across_timeframes(self):
        """Per-timeframe confidence is computed independently."""
        ctx = _build_instrument_context("NSE:NIFTY50")
        dfs = {
            "1d": _ohlc(120, drift=0.5, vol=0.1, seed=13),
            "1h": _ohlc(120, drift=0.1, vol=0.5, seed=13),
        }
        results = analyze_multi_timeframe("NSE:NIFTY50", dfs, ctx)
        assert results["1d"].confidence != results["1h"].confidence


# --------------------------------------------------------------------------- #
# Test H / I — options candidates from the live chain
# --------------------------------------------------------------------------- #
class TestOptionsCandidates:

    def test_no_direction_no_candidates(self):
        """Neutral forecast -> no options candidates (no forced trade)."""
        features = FeatureEngine(lookback=60).compute(_ohlc(100, drift=0.2, vol=0.2))
        regime = classify_regime(features)
        out = generate_options_candidates(
            features, regime, SignalDirection.NEUTRAL, 100.0, _chain([99, 100, 101], 100.0)
        )
        assert out == []

    def test_no_chain_no_candidates(self):
        """Missing chain -> empty result, explicit unavailability (no fabrication)."""
        features = FeatureEngine(lookback=60).compute(_ohlc(100, drift=0.2, vol=0.2))
        regime = classify_regime(features)
        out = generate_options_candidates(features, regime, SignalDirection.LONG, 100.0, None)
        assert out == []

    def test_bullish_prefers_calls(self):
        """Bullish forecast selects CE candidates from the chain."""
        features = FeatureEngine(lookback=60).compute(_ohlc(100, drift=0.2, vol=0.2))
        regime = classify_regime(features)
        chain = _chain([98, 100, 102], 100.0, opt="CE") + _chain([98, 100, 102], 100.0, opt="PE")
        out = generate_options_candidates(features, regime, SignalDirection.LONG, 100.0, chain)
        assert out, "expected at least one candidate"
        assert all(c.option_type == "CE" for c in out)

    def test_bearish_prefers_puts(self):
        """Bearish forecast selects PE candidates from the chain."""
        features = FeatureEngine(lookback=60).compute(_ohlc(100, drift=-0.2, vol=0.2))
        regime = classify_regime(features)
        chain = _chain([98, 100, 102], 100.0, opt="CE") + _chain([98, 100, 102], 100.0, opt="PE")
        out = generate_options_candidates(features, regime, SignalDirection.SHORT, 100.0, chain)
        assert out, "expected at least one candidate"
        assert all(c.option_type == "PE" for c in out)

    def test_candidates_change_with_chain(self):
        """(H) Candidates change when the option chain changes."""
        features = FeatureEngine(lookback=60).compute(_ohlc(100, drift=0.2, vol=0.2))
        regime = classify_regime(features)
        chain_a = _chain([98, 100, 102], 100.0, opt="CE")
        chain_b = _chain([104, 106, 108], 100.0, opt="CE")
        out_a = generate_options_candidates(features, regime, SignalDirection.LONG, 100.0, chain_a)
        out_b = generate_options_candidates(features, regime, SignalDirection.LONG, 100.0, chain_b)
        assert {c.strike for c in out_a} != {c.strike for c in out_b}

    def test_illiquid_options_rejected(self):
        """(I) Illiquid contracts are rejected, not merely downscored."""
        features = FeatureEngine(lookback=60).compute(_ohlc(100, drift=0.2, vol=0.2))
        regime = classify_regime(features)
        illiquid = _chain([99, 100, 101], 100.0, liq_vol=10, liq_oi=100)
        out = generate_options_candidates(features, regime, SignalDirection.LONG, 100.0, illiquid)
        assert out == []

    def test_wide_spread_rejected(self):
        """Wide bid/ask spreads (>20%) are rejected outright."""
        features = FeatureEngine(lookback=60).compute(_ohlc(100, drift=0.2, vol=0.2))
        regime = classify_regime(features)
        wide = _chain([99, 100, 101], 100.0, spread_mult=30.0)
        out = generate_options_candidates(features, regime, SignalDirection.LONG, 100.0, wide)
        assert out == []

    def test_candidates_ranked_by_score(self):
        """Candidates are sorted by score descending."""
        features = FeatureEngine(lookback=60).compute(_ohlc(100, drift=0.2, vol=0.2))
        regime = classify_regime(features)
        chain = _chain([97, 98, 100, 101, 102, 103], 100.0, opt="CE")
        out = generate_options_candidates(features, regime, SignalDirection.LONG, 100.0, chain)
        scores = [c.score for c in out]
        assert scores == sorted(scores, reverse=True)


# --------------------------------------------------------------------------- #
# Test J / K — data quality + conflicting evidence affect confidence
# --------------------------------------------------------------------------- #
class TestConfidenceAdjustments:

    def test_stale_or_short_data_lowers_confidence(self):
        """(J) Stale/missing data must reduce confidence, not ignore it."""
        ctx = _build_instrument_context("NSE:SBIN")
        eng = FeatureEngine(lookback=60)
        rich = eng.compute(_ohlc(120, drift=0.2, vol=0.2))
        poor = eng.compute(_ohlc(32, drift=0.2, vol=0.2))
        conf_rich, _ = compute_evidence_confidence(rich, classify_regime(rich), ctx)
        conf_poor, ledger_poor = compute_evidence_confidence(poor, classify_regime(poor), ctx)
        assert conf_poor < conf_rich
        assert any(("nsufficient" in m or "imited" in m) for m in ledger_poor.missing)

    def test_conflicting_evidence_classified_as_mixed(self):
        """(K) Near-equal positive/negative evidence is flagged 'mixed'."""
        mixed = EvidenceLedger(positive=["a", "b"], negative=["c", "d"])
        assert mixed.agreement == "mixed"
        lean = EvidenceLedger(positive=["a", "b", "c"], negative=["d"])
        assert lean.agreement != "mixed"

    def test_expected_move_uses_instrument_multiplier(self):
        """BANK NIFTY ATR multiplier widens its estimated range vs NIFTY."""
        ctx_bank = _build_instrument_context("NSE:BANKNIFTY")
        ctx_nifty = _build_instrument_context("NSE:NIFTY50")
        features = FeatureEngine(lookback=60).compute(_ohlc(120, drift=0.1, vol=0.3, seed=21))
        em_bank = compute_expected_move(features, TimeHorizon.INTRADAY, ctx_bank)
        em_nifty = compute_expected_move(features, TimeHorizon.INTRADAY, ctx_nifty)
        assert abs(em_bank.lower_pct) > abs(em_nifty.lower_pct)


# --------------------------------------------------------------------------- #
# New tests: data completeness, relative strength, freshness, news sentiment
# --------------------------------------------------------------------------- #
class TestDataCompleteness:
    """Tests for compute_data_completeness and the DataCompleteness dataclass."""

    def test_rich_data_high_completeness(self):
        ctx = _build_instrument_context("NSE:SBIN")
        eng = FeatureEngine(lookback=60)
        features = eng.compute(_ohlc(200, drift=0.2, vol=0.2))
        dc = compute_data_completeness(
            features, ctx,
            freshness_ms=60_000,
            news_available=True,
            derivatives_available=True,
            relative_strength_available=True,
        )
        assert dc.completeness > 0.8
        assert dc.missing_indicators == []
        assert dc.missing_data_sources == []

    def test_short_data_low_completeness(self):
        ctx = _build_instrument_context("NSE:SBIN")
        eng = FeatureEngine(lookback=60)
        features = eng.compute(_ohlc(40, drift=0.2, vol=0.2))
        dc = compute_data_completeness(features, ctx)
        assert dc.completeness < 0.7
        assert dc.insufficient or any("nsufficient" in m or "imited" in m for m in dc.missing_indicators)

    def test_missing_data_sources_reduces_completeness(self):
        ctx = _build_instrument_context("NSE:SBIN")
        eng = FeatureEngine(lookback=60)
        features = eng.compute(_ohlc(200, drift=0.2, vol=0.2))
        dc_full = compute_data_completeness(
            features, ctx,
            news_available=True, derivatives_available=True, relative_strength_available=True,
        )
        dc_missing = compute_data_completeness(
            features, ctx,
            news_available=False, derivatives_available=False, relative_strength_available=False,
        )
        assert dc_missing.completeness < dc_full.completeness
        assert "News / sentiment intelligence" in dc_missing.missing_data_sources
        assert "Derivatives data (OI/IV/Greeks)" in dc_missing.missing_data_sources

    def test_stale_data_reduces_completeness(self):
        ctx = _build_instrument_context("NSE:SBIN")
        eng = FeatureEngine(lookback=60)
        features = eng.compute(_ohlc(200, drift=0.2, vol=0.2))
        dc_fresh = compute_data_completeness(features, ctx, freshness_ms=1_000,
                                             news_available=True, derivatives_available=True,
                                             relative_strength_available=True)
        dc_stale = compute_data_completeness(features, ctx, freshness_ms=48 * 3_600_000,
                                             news_available=True, derivatives_available=True,
                                             relative_strength_available=True)
        assert dc_stale.completeness < dc_fresh.completeness
        assert dc_stale.staleness_note is not None


class TestRelativeStrength:
    """Tests for compute_relative_strength and the RelativeStrength dataclass."""

    def test_outperformance_positive(self):
        idx = pd.date_range("2024-01-01", periods=120, freq="D", tz="UTC")
        symbol_close = pd.Series(np.linspace(100, 115, 120), index=idx, name="close")
        bench_close = pd.Series(np.linspace(100, 105, 120), index=idx, name="close")
        symbol_df = pd.DataFrame({"close": symbol_close}, index=idx)
        bench_df = pd.DataFrame({"close": bench_close}, index=idx)
        rs = compute_relative_strength(symbol_df, bench_df, "1d")
        assert rs is not None
        assert rs.available
        assert rs.outperformance_pct > 0

    def test_underperformance_negative(self):
        idx = pd.date_range("2024-01-01", periods=120, freq="D", tz="UTC")
        symbol_close = pd.Series(np.linspace(100, 95, 120), index=idx)
        bench_close = pd.Series(np.linspace(100, 105, 120), index=idx)
        symbol_df = pd.DataFrame({"close": symbol_close}, index=idx)
        bench_df = pd.DataFrame({"close": bench_close}, index=idx)
        rs = compute_relative_strength(symbol_df, bench_df, "1d")
        assert rs is not None
        assert rs.outperformance_pct < 0

    def test_no_benchmark_returns_none(self):
        idx = pd.date_range("2024-01-01", periods=120, freq="D", tz="UTC")
        symbol_df = pd.DataFrame({"close": np.linspace(100, 115, 120)}, index=idx)
        rs = compute_relative_strength(symbol_df, None, "1d")
        assert rs is None


class TestConfidenceWithRelativeStrength:
    """Tests that passing index_features affects confidence."""

    def test_relative_strength_affects_confidence(self):
        ctx = _build_instrument_context("NSE:SBIN")
        eng = FeatureEngine(lookback=60)
        df = _ohlc(120, drift=0.2, vol=0.2)
        bench_df = _ohlc(120, drift=0.05, vol=0.2, seed=42)
        features = eng.compute(df, InstrumentClass.EQUITY)
        index_features = eng.compute(bench_df, InstrumentClass.INDEX)
        regime = classify_regime(features)

        conf_no_index, _ = compute_evidence_confidence(features, regime, ctx)
        conf_with_index, _ = compute_evidence_confidence(
            features, regime, ctx, index_features=index_features,
        )
        # Confidence should differ when relative strength info is included
        assert conf_no_index != conf_with_index


class TestConfidenceWithFreshness:
    """Tests that data_freshness_ms affects confidence."""

    def test_fresh_data_higher_confidence(self):
        ctx = _build_instrument_context("NSE:SBIN")
        eng = FeatureEngine(lookback=60)
        df = _ohlc(120, drift=0.2, vol=0.2)
        features = eng.compute(df, InstrumentClass.EQUITY)
        regime = classify_regime(features)

        conf_fresh, _ = compute_evidence_confidence(
            features, regime, ctx, data_freshness_ms=1_000,
        )
        conf_stale, _ = compute_evidence_confidence(
            features, regime, ctx, data_freshness_ms=48 * 3_600_000,
        )
        assert conf_stale < conf_fresh

    def test_freshness_does_not_inflate_confidence(self):
        """Fresh data should not cause confidence to exceed the ceiling."""
        ctx = _build_instrument_context("NSE:SBIN")
        eng = FeatureEngine(lookback=60)
        df = _ohlc(120, drift=0.2, vol=0.2)
        features = eng.compute(df, InstrumentClass.EQUITY)
        regime = classify_regime(features)

        conf, _ = compute_evidence_confidence(
            features, regime, ctx, data_freshness_ms=1_000,
        )
        assert conf <= 100.0


# --------------------------------------------------------------------------- #
# Phase 16 — forecast ledger (calibration FOUNDATION, no fake backtest)
# --------------------------------------------------------------------------- #
from trading_system.research.forecast_ledger import (
    ForecastStore,
    MIN_RESOLVED_FOR_CALIBRATION,
)


class TestForecastLedger:

    def _store(self):
        from sqlalchemy import create_engine
        return ForecastStore(create_engine("sqlite://", future=True))

    def test_record_and_list(self):
        store = self._store()
        store.record_forecast(
            "NSE:NIFTY50", "1d", "bearish", 0.72, "short_term",
            expected_move_lower_pct=-1.4, expected_move_upper_pct=-0.8,
            invalidation="Sustained move above 24000",
        )
        recs = store.list_forecasts(instrument="NSE:NIFTY50")
        assert len(recs) == 1
        assert recs[0].bias == "bearish"
        assert recs[0].resolved is False

    def test_resolve_hit_and_miss(self):
        store = self._store()
        r1 = store.record_forecast("X", "1d", "bearish", 0.7, "short_term")
        r2 = store.record_forecast("X", "1d", "bullish", 0.6, "short_term")
        out1 = store.resolve_forecast(r1.id, -1.1)
        out2 = store.resolve_forecast(r2.id, -0.4)
        assert out1.hit is True
        assert out2.hit is False
        assert out1.resolved is True and out1.actual_return_pct == -1.1

    def test_expected_move_containment(self):
        store = self._store()
        r_in = store.record_forecast("X", "1d", "bearish", 0.7, "short_term",
                                     expected_move_lower_pct=-1.4, expected_move_upper_pct=-0.8)
        r_out = store.record_forecast("Y", "1d", "bearish", 0.7, "short_term",
                                      expected_move_lower_pct=-1.4, expected_move_upper_pct=-0.8)
        assert store.resolve_forecast(r_in.id, -1.0).within_expected_move is True
        assert store.resolve_forecast(r_out.id, -3.0).within_expected_move is False

    def test_record_from_analysis_and_blocked_skip(self):
        store = self._store()
        eng = MarketIntelligenceEngine(lookback=60)
        ok = store.record_from_analysis(eng.analyze("NSE:SBIN", "1d", _ohlc(100, drift=0.3)))
        assert ok is not None
        assert ok.instrument == "NSE:SBIN"
        assert ok.market_state is not None  # market state snapshot retained
        # BLOCKED analysis = nothing forecast => nothing recorded
        assert store.record_from_analysis({"status": "BLOCKED", "reason": "NO_DATA"}) is None

    def test_calibration_never_claims_probability(self):
        store = self._store()
        s = store.summarize_calibration()
        assert s["calibration_status"] == "uncalibrated_insufficient_sample"
        assert "NOT a probability" in s["note"]
        r = store.record_forecast("Z", "1d", "bullish", 0.6, "short_term")
        store.resolve_forecast(r.id, 0.5)
        s2 = store.summarize_calibration(instrument="Z")
        assert s2["resolved_count"] == 1
        assert s2["resolved_count"] < MIN_RESOLVED_FOR_CALIBRATION
        assert s2["calibration_status"] == "uncalibrated_insufficient_sample"

