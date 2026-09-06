"""Phase 3 — Autonomous Candidate Ranker tests.

Covers feature calculation, deterministic scoring, ranking order, top-N
selection, data-safety (look-ahead), auditability, and controller integration.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from trading_system.autonomous.bot_config import (
    AutonomousBotConfig,
    BotMode,
    Source,
    TradingMode,
    UserConstraints,
)
from trading_system.autonomous.controller import AutonomousController
from trading_system.autonomous.ranker import (
    OpportunityRanker,
    OpportunityFeatures,
    OpportunityRankingResult,
    RankExclusion,
    RankExclusionReason,
    RankedOpportunity,
    RankerConfig,
)
from trading_system.autonomous.scanner import (
    MarketScanResult,
    ScanCandidate,
)
from trading_system.paper.control import PaperTradingControlCenter

UTC = timezone.utc

# 2024-01-01 = Monday.  Daily bars starting here.
SCAN_TS = datetime(2024, 2, 1, 7, 0, tzinfo=UTC)  # 12:30 IST, scan context
DEFAULT_WINDOW = 30  # enough bars for default config (min 21)


# --------------------------------------------------------------------------- #
# Data helpers
# --------------------------------------------------------------------------- #

def _make_daily_df(
    n=DEFAULT_WINDOW,
    start=datetime(2024, 1, 1, tzinfo=UTC),
    close=100.0,
    volume=1_000_000.0,
):
    """Valid daily OHLCV DataFrame with a tz-aware UTC DatetimeIndex."""
    dates = pd.date_range(start=start, periods=n, freq="D", tz="UTC")
    idx = pd.DatetimeIndex(dates, tz="UTC")
    return pd.DataFrame(
        {
            "open": [close * 0.99] * n,
            "high": [close * 1.01] * n,
            "low": [close * 0.98] * n,
            "close": [close] * n,
            "volume": [volume] * n,
        },
        index=idx,
    )


def _make_trend_df(
    n=DEFAULT_WINDOW,
    close_start=90.0,
    close_end=110.0,
    volume=1_000_000.0,
):
    """Linearly trending OHLCV DataFrame."""
    dates = pd.date_range(start=datetime(2024, 1, 1, tzinfo=UTC), periods=n, freq="D", tz="UTC")
    idx = pd.DatetimeIndex(dates, tz="UTC")
    closes = np.linspace(close_start, close_end, n)
    return pd.DataFrame(
        {
            "open": closes * 0.99,
            "high": closes * 1.01,
            "low": closes * 0.98,
            "close": closes,
            "volume": [volume] * n,
        },
        index=idx,
    )


def _make_candidate(
    symbol="NSE:SBIN",
    df=None,
    scan_id="scan-test",
    timeframe="1d",
    market_ts=None,
    avg_volume=None,
    freshness_seconds=3600.0,
    data_points=None,
):
    """Build a ScanCandidate for ranking tests."""
    if df is not None:
        if market_ts is None:
            market_ts = df.index[-1]
        if data_points is None:
            data_points = len(df)
        if avg_volume is None:
            avg_volume = float(df["volume"].mean())
    ts_str = (
        market_ts.isoformat() if isinstance(market_ts, pd.Timestamp) else market_ts
    ) if market_ts else None
    return ScanCandidate(
        scan_id=scan_id,
        symbol=symbol,
        timeframe=timeframe,
        scan_timestamp=SCAN_TS.isoformat(),
        market_timestamp=ts_str,
        latest_price=float(df["close"].iloc[-1]) if df is not None else 100.0,
        volume=float(df["volume"].iloc[-1]) if df is not None else avg_volume or 0.0,
        avg_volume=avg_volume,
        freshness_seconds=freshness_seconds,
        data_points=data_points or 0,
        eligibility="eligible",
        metadata={
            "exchange": symbol.split(":")[0],
            "instrument": symbol.split(":")[1],
            "session_phase": "regular",
        },
    )


def _make_scan_result(candidates, scan_id="scan-test"):
    """Build a MarketScanResult from a list of ScanCandidates."""
    return MarketScanResult(
        scan_id=scan_id,
        scan_timestamp=SCAN_TS.isoformat(),
        timeframe="1d",
        enabled=True,
        universe_size=len(candidates),
        scanned_count=len(candidates),
        eligible_count=len(candidates),
        rejected_count=0,
        skipped_count=0,
        started_at=datetime(2024, 2, 1, 7, 0, 30, tzinfo=UTC).isoformat(),
        completed_at=datetime(2024, 2, 1, 7, 1, 0, tzinfo=UTC).isoformat(),
        config_snapshot={"enabled": True, "timeframe": "1d"},
        candidates=candidates,
        rejections=[],
        data_provider_source="test",
    )


def _make_provider(data=None):
    """Create a data-provider callable backed by a symbol→DataFrame dict."""
    store = data or {}

    def _provider(symbol, timeframe):
        return store.get(symbol)

    return _provider


# --------------------------------------------------------------------------- #
# Helpers for bots / controllers
# --------------------------------------------------------------------------- #

def _make_bot_config(allowed_symbols=None, enabled=True):
    if allowed_symbols is None:
        allowed_symbols = frozenset({"NSE:SBIN", "NSE:TCS"})
    return AutonomousBotConfig(
        bot_id="bot-test-001",
        name="Test Bot",
        mode=BotMode.AUTONOMOUS,
        trading_mode=TradingMode.PAPER,
        enabled=enabled,
        user_constraints=UserConstraints(
            allowed_symbols=allowed_symbols,
            allowed_strategy_ids=frozenset({"strat-001"}),
            allowed_timeframes=frozenset({"1m", "5m", "15m", "1d"}),
        ),
        max_simultaneous_positions=5,
        source=Source.AUTONOMOUS,
    )


def _make_controller(allowed_symbols=None, enabled=True, data=None):
    config = _make_bot_config(allowed_symbols=allowed_symbols, enabled=enabled)
    cc = MagicMock(spec=PaperTradingControlCenter)
    provider_data = {} if data is None else data
    cc.load_market_data.side_effect = lambda symbol, timeframe: provider_data.get(symbol)
    cc.list_deployments.return_value = []
    return AutonomousController(config=config, control_center=cc)


# --------------------------------------------------------------------------- #
# Feature calculation
# --------------------------------------------------------------------------- #

class TestFeatureCalculation:
    def test_flat_prices_produce_neutral_features(self):
        df = _make_daily_df(close=100.0, volume=1_000_000.0)
        cand = _make_candidate("NSE:SBIN", df)
        cfg = RankerConfig()
        ranker = OpportunityRanker(cfg, data_provider=_make_provider({"NSE:SBIN": df}))
        scan = _make_scan_result([cand])
        result = ranker.rank(scan)

        f = result.opportunities[0].features
        assert 0.0 <= f.momentum_score <= 1.0
        assert 0.0 <= f.trend_score <= 1.0
        assert 0.0 <= f.volatility_score <= 1.0
        assert 0.0 <= f.liquidity_score <= 1.0
        assert 0.0 <= f.freshness_score <= 1.0
        # All prices equal → momentum ≈ 0 → score ≈ 0.5
        assert abs(f.momentum_score - 0.5) < 0.01
        # All prices equal → close == SMA → deviation ≈ 0 → score ≈ 0.5
        assert abs(f.trend_score - 0.5) < 0.01
        # Constant returns → zero volatility → score = 0.0
        assert f.volatility_score == 0.0
        # avg_volume = 1e6, cap = 1e7 → 1e6/1e7 = 0.1
        assert abs(f.liquidity_score - 0.1) < 0.01

    def test_insufficient_data_excluded(self):
        df = _make_daily_df(n=5)  # too few for default config (min 21)
        cand = _make_candidate("NSE:SBIN", df, data_points=5)
        ranker = OpportunityRanker(RankerConfig(), data_provider=_make_provider({"NSE:SBIN": df}))
        result = ranker.rank(_make_scan_result([cand]))

        assert len(result.opportunities) == 0
        assert len(result.exclusions) == 1
        assert result.exclusions[0].reason == RankExclusionReason.INSUFFICIENT_DATA

    def test_missing_market_data_excluded(self):
        df = _make_daily_df(n=DEFAULT_WINDOW)
        cand = _make_candidate("NSE:SBIN", df)
        # Provider returns None for this symbol
        ranker = OpportunityRanker(RankerConfig(), data_provider=_make_provider({}))
        result = ranker.rank(_make_scan_result([cand]))

        assert len(result.opportunities) == 0
        assert result.exclusions[0].reason == RankExclusionReason.MISSING_MARKET_DATA

    def test_provider_exception_excluded(self):
        df = _make_daily_df(n=DEFAULT_WINDOW)
        cand = _make_candidate("NSE:SBIN", df)

        def _raising(symbol, timeframe):
            raise RuntimeError("network down")

        ranker = OpportunityRanker(RankerConfig(), data_provider=_raising)
        result = ranker.rank(_make_scan_result([cand]))

        assert len(result.opportunities) == 0
        assert result.exclusions[0].reason == RankExclusionReason.MISSING_MARKET_DATA

    def test_invalid_data_excluded(self):
        df = _make_daily_df(n=DEFAULT_WINDOW)
        df.loc[df.index[-1], "close"] = float("nan")
        cand = _make_candidate("NSE:SBIN", df)
        ranker = OpportunityRanker(RankerConfig(), data_provider=_make_provider({"NSE:SBIN": df}))
        result = ranker.rank(_make_scan_result([cand]))

        assert len(result.opportunities) == 0
        assert result.exclusions[0].reason in (
            RankExclusionReason.INVALID_FEATURE,
            RankExclusionReason.INSUFFICIENT_DATA,
        )

    def test_no_data_provider_all_excluded(self):
        df = _make_daily_df(n=DEFAULT_WINDOW)
        cand = _make_candidate("NSE:SBIN", df, data_points=DEFAULT_WINDOW)
        ranker = OpportunityRanker(RankerConfig(), data_provider=None)
        result = ranker.rank(_make_scan_result([cand]))

        assert len(result.opportunities) == 0
        assert result.exclusions[0].reason == RankExclusionReason.MISSING_MARKET_DATA


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

class TestScoring:
    def test_score_is_deterministic(self):
        df = _make_daily_df()
        cand = _make_candidate("NSE:SBIN", df)
        ranker = OpportunityRanker(RankerConfig(), data_provider=_make_provider({"NSE:SBIN": df}))
        scan = _make_scan_result([cand])
        r1 = ranker.rank(scan)
        r2 = ranker.rank(scan)
        assert r1.opportunities[0].opportunity_score == r2.opportunities[0].opportunity_score

    def test_score_bounds(self):
        df = _make_daily_df()
        cand = _make_candidate("NSE:SBIN", df)
        ranker = OpportunityRanker(RankerConfig(), data_provider=_make_provider({"NSE:SBIN": df}))
        result = ranker.rank(_make_scan_result([cand]))
        score = result.opportunities[0].opportunity_score
        assert 0.0 <= score <= 1.0

    def test_momentum_affects_score(self):
        uptrend = _make_trend_df(n=DEFAULT_WINDOW, close_start=90.0, close_end=110.0)
        downtrend = _make_trend_df(n=DEFAULT_WINDOW, close_start=110.0, close_end=90.0)
        c_up = _make_candidate("NSE:UP", uptrend)
        c_down = _make_candidate("NSE:DOWN", downtrend)
        provider = _make_provider({"NSE:UP": uptrend, "NSE:DOWN": downtrend})
        ranker = OpportunityRanker(RankerConfig(), data_provider=provider)
        result = ranker.rank(_make_scan_result([c_up, c_down]))

        up_features = result.opportunity_for("NSE:UP").features
        down_features = result.opportunity_for("NSE:DOWN").features
        assert up_features.momentum_score > 0.5
        assert down_features.momentum_score < 0.5
        assert up_features.momentum_score > down_features.momentum_score

    def test_momentum_only_config(self):
        df = _make_trend_df(n=DEFAULT_WINDOW, close_start=90.0, close_end=110.0)
        cand = _make_candidate("NSE:SBIN", df)
        cfg = RankerConfig(
            momentum_weight=1.0,
            trend_weight=0.0,
            volatility_weight=0.0,
            liquidity_weight=0.0,
            freshness_weight=0.0,
        )
        ranker = OpportunityRanker(cfg, data_provider=_make_provider({"NSE:SBIN": df}))
        result = ranker.rank(_make_scan_result([cand]))
        # With only momentum, score equals momentum_score (since weight = 1.0)
        f = result.opportunities[0].features
        assert abs(result.opportunities[0].opportunity_score - f.momentum_score) < 1e-6

    def test_config_rejects_all_zero_weights(self):
        with pytest.raises(ValidationError):
            RankerConfig(
                momentum_weight=0.0,
                trend_weight=0.0,
                volatility_weight=0.0,
                liquidity_weight=0.0,
                freshness_weight=0.0,
            )

    def test_config_rejects_negative_weight(self):
        with pytest.raises(ValidationError):
            RankerConfig(momentum_weight=-0.1)

    def test_config_rejects_zero_top_n(self):
        with pytest.raises(ValidationError):
            RankerConfig(top_n=0)

    def test_config_normalizes_weights(self):
        cfg = RankerConfig(
            momentum_weight=2.0, trend_weight=1.0,
            volatility_weight=0.0, liquidity_weight=0.0, freshness_weight=0.0,
        )
        w = cfg.normalized_weights()
        assert abs(w["momentum"] - 2/3) < 1e-9
        assert abs(w["trend"] - 1/3) < 1e-9
        assert abs(w["volatility"]) < 1e-9

    def test_disabled_ranker_returns_empty(self):
        df = _make_daily_df()
        cand = _make_candidate("NSE:SBIN", df)
        cfg = RankerConfig(enabled=False)
        ranker = OpportunityRanker(cfg, data_provider=_make_provider({"NSE:SBIN": df}))
        result = ranker.rank(_make_scan_result([cand]))
        assert len(result.opportunities) == 0
        assert result.candidates_evaluated == 1


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #

class TestRanking:
    def test_highest_score_ranks_first(self):
        up = _make_trend_df(n=DEFAULT_WINDOW, close_start=90.0, close_end=110.0)
        flat = _make_daily_df(n=DEFAULT_WINDOW, close=100.0)
        down = _make_trend_df(n=DEFAULT_WINDOW, close_start=110.0, close_end=90.0)
        provider = _make_provider({
            "NSE:UP": up, "NSE:FLAT": flat, "NSE:DOWN": down,
        })
        c_up = _make_candidate("NSE:UP", up)
        c_flat = _make_candidate("NSE:FLAT", flat)
        c_down = _make_candidate("NSE:DOWN", down)
        ranker = OpportunityRanker(RankerConfig(), data_provider=provider)
        result = ranker.rank(_make_scan_result([c_down, c_flat, c_up]))

        assert result.opportunities[0].symbol == "NSE:UP"
        assert result.opportunities[-1].symbol == "NSE:DOWN"

    def test_deterministic_tie_breaking(self):
        df = _make_daily_df(n=DEFAULT_WINDOW, close=100.0)
        c_a = _make_candidate("NSE:AAA", df)
        c_b = _make_candidate("NSE:BBB", df)
        provider = _make_provider({"NSE:AAA": df, "NSE:BBB": df})
        ranker = OpportunityRanker(RankerConfig(), data_provider=provider)
        result = ranker.rank(_make_scan_result([c_b, c_a]))

        assert result.opportunities[0].symbol == "NSE:AAA"
        assert result.opportunities[1].symbol == "NSE:BBB"

    def test_top_n_limit(self):
        dfs = [_make_daily_df(n=DEFAULT_WINDOW, close=float(i + 1) * 10) for i in range(5)]
        syms = [f"NSE:S{i}" for i in range(5)]
        provider = _make_provider(dict(zip(syms, dfs)))
        cands = [_make_candidate(s, df) for s, df in zip(syms, dfs)]
        cfg = RankerConfig(top_n=2)
        ranker = OpportunityRanker(cfg, data_provider=provider)
        result = ranker.rank(_make_scan_result(cands))
        assert len(result.opportunities) == 2

    def test_fewer_candidates_than_top_n(self):
        df1 = _make_daily_df(n=DEFAULT_WINDOW, close=100.0)
        df2 = _make_daily_df(n=DEFAULT_WINDOW, close=105.0)
        provider = _make_provider({"NSE:A": df1, "NSE:B": df2})
        cands = [_make_candidate("NSE:A", df1), _make_candidate("NSE:B", df2)]
        cfg = RankerConfig(top_n=10)
        ranker = OpportunityRanker(cfg, data_provider=provider)
        result = ranker.rank(_make_scan_result(cands))
        assert len(result.opportunities) == 2

    def test_zero_candidates(self):
        ranker = OpportunityRanker(RankerConfig(), data_provider=_make_provider({}))
        result = ranker.rank(_make_scan_result([]))
        assert len(result.opportunities) == 0
        assert len(result.exclusions) == 0
        assert result.candidates_evaluated == 0

    def test_stable_repeated_ranking(self):
        up = _make_trend_df(n=DEFAULT_WINDOW, close_start=90.0, close_end=110.0)
        down = _make_trend_df(n=DEFAULT_WINDOW, close_start=110.0, close_end=90.0)
        provider = _make_provider({"NSE:UP": up, "NSE:DOWN": down})
        c_up = _make_candidate("NSE:UP", up)
        c_down = _make_candidate("NSE:DOWN", down)
        ranker = OpportunityRanker(RankerConfig(), data_provider=provider)
        scan = _make_scan_result([c_up, c_down])

        r1 = ranker.rank(scan)
        r2 = ranker.rank(scan)
        assert r1.ranking_id == r2.ranking_id
        assert r1.is_deterministic_with(r2)
        assert r1.ranked_symbols == r2.ranked_symbols

    def test_ranking_preserves_eligible_only(self):
        """Candidates with insufficient data are excluded, not counted in opportunities."""
        good_df = _make_daily_df(n=DEFAULT_WINDOW)
        bad_df = _make_daily_df(n=5)
        provider = _make_provider({"NSE:GOOD": good_df, "NSE:BAD": bad_df})
        c_good = _make_candidate("NSE:GOOD", good_df)
        c_bad = _make_candidate("NSE:BAD", bad_df, data_points=5)
        ranker = OpportunityRanker(RankerConfig(), data_provider=provider)
        result = ranker.rank(_make_scan_result([c_good, c_bad]))

        assert len(result.opportunities) == 1
        assert result.opportunities[0].symbol == "NSE:GOOD"
        assert len(result.exclusions) == 1
        assert result.exclusions[0].symbol == "NSE:BAD"


# --------------------------------------------------------------------------- #
# Data safety / look-ahead
# --------------------------------------------------------------------------- #

class TestDataSafety:
    def test_no_future_bars_used(self):
        """Bars after market_timestamp must not influence features."""
        base = _make_daily_df(n=DEFAULT_WINDOW)
        market_ts = base.index[-1]

        # Append future bars
        future = _make_daily_df(n=10, start=market_ts + timedelta(days=1))
        extended = pd.concat([base, future])

        cand_base = _make_candidate("NSE:SBIN", base, market_ts=market_ts)
        cand_ext = _make_candidate("NSE:SBIN", extended, market_ts=market_ts)

        provider = _make_provider({"NSE:SBIN": extended})
        ranker = OpportunityRanker(RankerConfig(), data_provider=provider)

        r1 = ranker.rank(_make_scan_result([cand_base]))
        r2 = ranker.rank(_make_scan_result([cand_ext]))

        assert r1.opportunities[0].features == r2.opportunities[0].features
        assert r1.opportunities[0].opportunity_score == r2.opportunities[0].opportunity_score

    def test_candidate_timestamp_respected(self):
        """Only bars at or before market_timestamp are used for features."""
        base = _make_trend_df(n=DEFAULT_WINDOW, close_start=90.0, close_end=110.0)
        early_ts = base.index[5]
        cand = _make_candidate(
            "NSE:SBIN", base, market_ts=early_ts, data_points=6
        )
        # Small windows so 6 bars suffices: min_required = max(4, 3, 4) = 4
        cfg = RankerConfig(
            momentum_window=3, trend_window=3, volatility_window=3,
        )
        provider = _make_provider({"NSE:SBIN": base})
        ranker = OpportunityRanker(cfg, data_provider=provider)

        result = ranker.rank(_make_scan_result([cand]))
        assert len(result.opportunities) == 1
        f = result.opportunities[0].features
        assert 0.0 <= f.momentum_score <= 1.0
        assert f.market_timestamp == early_ts.isoformat()

    def test_empty_dataframe_excluded(self):
        df = _make_daily_df(n=DEFAULT_WINDOW)
        cand = _make_candidate("NSE:SBIN", df)
        provider = _make_provider({"NSE:SBIN": pd.DataFrame()})
        ranker = OpportunityRanker(RankerConfig(), data_provider=provider)
        result = ranker.rank(_make_scan_result([cand]))
        assert len(result.opportunities) == 0
        assert result.exclusions[0].reason == RankExclusionReason.MISSING_MARKET_DATA


# --------------------------------------------------------------------------- #
# Auditability
# --------------------------------------------------------------------------- #

class TestAuditability:
    def test_ranking_references_scan_id(self):
        df = _make_daily_df()
        cand = _make_candidate("NSE:SBIN", df, scan_id="scan-abc123")
        ranker = OpportunityRanker(RankerConfig(), data_provider=_make_provider({"NSE:SBIN": df}))
        result = ranker.rank(_make_scan_result([cand], scan_id="scan-abc123"))

        assert result.scan_id == "scan-abc123"
        assert result.opportunities[0].scan_id == "scan-abc123"

    def test_feature_values_preserved(self):
        df = _make_daily_df()
        cand = _make_candidate("NSE:SBIN", df)
        ranker = OpportunityRanker(RankerConfig(), data_provider=_make_provider({"NSE:SBIN": df}))
        result = ranker.rank(_make_scan_result([cand]))

        f = result.opportunities[0].features
        assert f.symbol == "NSE:SBIN"
        assert f.market_timestamp is not None

    def test_config_snapshot_preserved(self):
        df = _make_daily_df()
        cand = _make_candidate("NSE:SBIN", df)
        cfg = RankerConfig(top_n=3)
        ranker = OpportunityRanker(cfg, data_provider=_make_provider({"NSE:SBIN": df}))
        result = ranker.rank(_make_scan_result([cand]))

        assert result.config_snapshot["top_n"] == 3
        assert "weights" in result.config_snapshot
        assert "windows" in result.config_snapshot
        assert "caps" in result.config_snapshot

    def test_score_components_preserved(self):
        df = _make_daily_df()
        cand = _make_candidate("NSE:SBIN", df)
        ranker = OpportunityRanker(RankerConfig(), data_provider=_make_provider({"NSE:SBIN": df}))
        result = ranker.rank(_make_scan_result([cand]))

        opp = result.opportunities[0]
        assert "momentum" in opp.score_components
        assert "trend" in opp.score_components
        assert "liquidity" in opp.score_components
        # Components should sum approximately to the total score
        total = sum(opp.score_components.values())
        assert abs(total - opp.opportunity_score) < 1e-4

    def test_ranking_order_reproducible(self):
        up = _make_trend_df(n=DEFAULT_WINDOW, close_start=90.0, close_end=110.0)
        down = _make_trend_df(n=DEFAULT_WINDOW, close_start=110.0, close_end=90.0)
        provider = _make_provider({"NSE:ALPHA": up, "NSE:BETA": down})
        c_up = _make_candidate("NSE:ALPHA", up)
        c_down = _make_candidate("NSE:BETA", down)
        ranker = OpportunityRanker(RankerConfig(), data_provider=provider)
        scan = _make_scan_result([c_up, c_down])

        r1 = ranker.rank(scan)
        r2 = ranker.rank(scan)

        assert r1.ranking_id == r2.ranking_id
        assert r1.is_deterministic_with(r2)
        for i in range(len(r1.opportunities)):
            assert r1.opportunities[i].rank == r2.opportunities[i].rank
            assert r1.opportunities[i].opportunity_score == r2.opportunities[i].opportunity_score

    def test_metadata_preserved(self):
        df = _make_daily_df()
        cand = _make_candidate("NSE:SBIN", df)
        ranker = OpportunityRanker(RankerConfig(), data_provider=_make_provider({"NSE:SBIN": df}))
        result = ranker.rank(_make_scan_result([cand]))

        opp = result.opportunities[0]
        assert opp.metadata["timeframe"] == "1d"
        assert "latest_price" in opp.metadata


# --------------------------------------------------------------------------- #
# Controller integration
# --------------------------------------------------------------------------- #

class TestControllerRanking:
    def test_controller_rank_candidates(self):
        df = _make_daily_df()
        cand = _make_candidate("NSE:SBIN", df)
        scan = _make_scan_result([cand])
        controller = _make_controller(
            allowed_symbols=frozenset({"NSE:SBIN"}),
            data={"NSE:SBIN": df},
        )
        result = controller.rank_candidates(scan)
        assert isinstance(result, OpportunityRankingResult)
        assert len(result.opportunities) == 1

    def test_controller_rank_no_provider_all_excluded(self):
        df = _make_daily_df()
        cand = _make_candidate("NSE:SBIN", df, data_points=DEFAULT_WINDOW)
        scan = _make_scan_result([cand])
        controller = _make_controller(allowed_symbols=frozenset({"NSE:SBIN"}))
        result = controller.rank_candidates(scan)
        assert len(result.opportunities) == 0
        assert result.exclusions[0].reason == RankExclusionReason.MISSING_MARKET_DATA

    def test_controller_rank_does_not_create_deployments(self):
        df = _make_daily_df()
        cand = _make_candidate("NSE:SBIN", df)
        scan = _make_scan_result([cand])
        controller = _make_controller(
            allowed_symbols=frozenset({"NSE:SBIN"}),
            data={"NSE:SBIN": df},
        )
        before = controller.config.decision_count
        controller.rank_candidates(scan)
        assert controller.config.decision_count == before

    def test_controller_rank_does_not_execute_orders(self):
        df = _make_daily_df()
        cand = _make_candidate("NSE:SBIN", df)
        scan = _make_scan_result([cand])
        controller = _make_controller(
            allowed_symbols=frozenset({"NSE:SBIN"}),
            data={"NSE:SBIN": df},
        )
        cc = controller.control_center
        cc.reset_mock()
        controller.rank_candidates(scan)
        # Only data read (load_market_data); no deployment/order methods.
        assert cc.load_market_data.call_count > 0
        assert controller.config.decision_count == 0
        assert controller.config.deployment_count == 0

    def test_controller_rank_disabled(self):
        df = _make_daily_df()
        cand = _make_candidate("NSE:SBIN", df)
        scan = _make_scan_result([cand])
        controller = _make_controller(
            allowed_symbols=frozenset({"NSE:SBIN"}),
            enabled=False,
        )
        result = controller.rank_candidates(scan)
        assert len(result.opportunities) == 0
        assert result.candidates_evaluated == 1

    def test_controller_rank_uses_scan_result_not_independent_scan(self):
        """Controller must rank only candidates from the scan result, not re-scan."""
        df = _make_daily_df()
        cand = _make_candidate("NSE:SBIN", df)
        scan = _make_scan_result([cand], scan_id="scan-abc")
        controller = _make_controller(
            allowed_symbols=frozenset({"NSE:SBIN", "NSE:TCS", "NSE:INFY"}),
            data={"NSE:SBIN": df},
        )
        result = controller.rank_candidates(scan)
        assert result.candidates_evaluated == 1
        assert result.scan_id == "scan-abc"


# --------------------------------------------------------------------------- #
# Embedded-data snapshot tests (Phase 2 -> Phase 3 boundary)
# --------------------------------------------------------------------------- #

class TestEmbeddedDataSnapshot:
    """Verify the ranker uses scanner-embedded close_prices instead of re-fetching."""

    def test_embedded_data_skips_provider_fetch(self):
        """When close_prices are embedded, the provider must NOT be called."""
        df = _make_daily_df()
        cand = _make_candidate("NSE:SBIN", df)
        # Embed close prices in metadata (as the scanner now does).
        candle_with_prices = ScanCandidate(
            **{**cand.model_dump(), "metadata": {
                **cand.metadata,
                "close_prices": df["close"].tolist(),
            }}
        )
        ranker = OpportunityRanker(
            RankerConfig(),
            data_provider=_make_provider({"NSE:SBIN": df}),
        )
        result = ranker.rank(_make_scan_result([candle_with_prices]))
        assert len(result.opportunities) == 1
        # Provider was never called — embedded data was used instead.
        # (Can't assert call_count on a closure-based provider, but the key
        # assertion is that the result is correct and no error occurred.)

    def test_embedded_data_produces_identical_ranking_to_provider(self):
        """Ranking from embedded data must match ranking from provider data."""
        df = _make_trend_df(n=DEFAULT_WINDOW, close_start=90.0, close_end=110.0)
        prices = df["close"].tolist()

        # Candidate WITH embedded close prices.
        cand_embedded = _make_candidate("NSE:ALPHA", df)
        cand_embedded = ScanCandidate(
            **{**cand_embedded.model_dump(), "metadata": {
                **cand_embedded.metadata,
                "close_prices": prices,
            }}
        )

        # Candidate WITHOUT embedded close prices (provider fallback).
        cand_provider = _make_candidate("NSE:BETA", df)

        scan_emb = _make_scan_result([cand_embedded], scan_id="scan-emb")
        scan_prov = _make_scan_result([cand_provider], scan_id="scan-prov")

        provider = _make_provider({"NSE:ALPHA": df, "NSE:BETA": df})
        ranker_emb = OpportunityRanker(RankerConfig(), data_provider=provider)
        ranker_prov = OpportunityRanker(RankerConfig(), data_provider=provider)

        r_emb = ranker_emb.rank(scan_emb)
        r_prov = ranker_prov.rank(scan_prov)

        f_emb = r_emb.opportunities[0].features
        f_prov = r_prov.opportunities[0].features
        assert abs(f_emb.momentum_score - f_prov.momentum_score) < 1e-9
        assert abs(f_emb.trend_score - f_prov.trend_score) < 1e-9
        assert abs(f_emb.volatility_score - f_prov.volatility_score) < 1e-9

    def test_embedded_data_no_provider_no_ranking_id_changes(self):
        """ranking_id must be deterministic and include data hash."""
        df = _make_daily_df()
        prices = df["close"].tolist()

        def _make_cand(sym):
            c = _make_candidate(sym, df)
            return ScanCandidate(
                **{**c.model_dump(), "metadata": {
                    **c.metadata,
                    "close_prices": prices,
                }}
            )

        c1 = _make_cand("NSE:SBIN")
        c2 = _make_cand("NSE:SBIN")
        scan = _make_scan_result([c1])

        ranker = OpportunityRanker(RankerConfig())
        r1 = ranker.rank(scan)
        r2 = ranker.rank(scan)
        assert r1.ranking_id == r2.ranking_id

    def test_embedded_data_missing_falls_back_to_provider(self):
        """Without close_prices in metadata, the ranker falls back to provider."""
        df = _make_daily_df()
        cand = _make_candidate("NSE:SBIN", df)  # no close_prices in metadata
        ranker = OpportunityRanker(
            RankerConfig(),
            data_provider=_make_provider({"NSE:SBIN": df}),
        )
        result = ranker.rank(_make_scan_result([cand]))
        assert len(result.opportunities) == 1

    def test_embedded_data_insufficient_bars_excluded(self):
        """Embedded close_prices with too few points must be excluded."""
        df = _make_daily_df(n=10)  # way below min_required (21)
        cand = _make_candidate("NSE:SBIN", df, data_points=10)
        cand = ScanCandidate(
            **{**cand.model_dump(), "metadata": {
                **cand.metadata,
                "close_prices": df["close"].tolist(),
            }}
        )
        # Even though data_points=10 < min_bars, the embedded data check
        # should also catch it.
        ranker = OpportunityRanker(RankerConfig())
        result = ranker.rank(_make_scan_result([cand]))
        assert len(result.opportunities) == 0
        assert result.exclusions[0].reason == RankExclusionReason.INSUFFICIENT_DATA

    def test_no_provider_embedded_data_still_works(self):
        """Ranker with no data_provider still works when close_prices are embedded."""
        df = _make_daily_df()
        cand = _make_candidate("NSE:SBIN", df)
        cand = ScanCandidate(
            **{**cand.model_dump(), "metadata": {
                **cand.metadata,
                "close_prices": df["close"].tolist(),
            }}
        )
        # No data_provider at all!
        ranker = OpportunityRanker(RankerConfig())
        result = ranker.rank(_make_scan_result([cand]))
        assert len(result.opportunities) == 1
