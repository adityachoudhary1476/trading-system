"""Phase 5 — Strategy Selection & Signal Generation tests.

Covers strategy discovery (Phase 5 engine), deterministic selection policy,
strategy evaluation with look-ahead-safe market data, snapshot consistency,
determinism, auditability, failure handling, controller integration, and
safety boundaries (no execution paths).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from trading_system.autonomous.compatibility import (
    CompatibilityConfig,
    CompatibilityFeatures,
    CompatibilityResult,
    StrategyCompatibility,
    StrategyCompatibilityEvaluator,
)
from trading_system.autonomous.controller import AutonomousController
from trading_system.autonomous.decision import (
    DecisionExclusionReason,
    DecisionResult,
    DecisionStatus,
    SelectionConfig,
    SelectionFactor,
    SelectionPolicy,
    SelectedConfiguration,
    StrategyDecisionEngine,
    TradingDecision,
)
from trading_system.autonomous.ranker import (
    OpportunityFeatures,
    OpportunityRankingResult,
    RankedOpportunity,
)
from trading_system.autonomous.scanner import (
    MarketScanResult,
    ScanCandidate,
    ScannerConfig,
)
from trading_system.paper.control import PaperTradingControlCenter
from trading_system.strategy_factory.capability import DataRequirement
from trading_system.strategy_factory.contract import (
    MarketState,
    SignalAction,
    StrategySignal,
)
from trading_system.strategy_factory.discovery import (
    clear_discovery,
    discover,
    registered_strategy_ids,
)

UTC = timezone.utc

BUILTIN_IDS = ["ema_crossover", "rsi_mean_reversion"]

DEFAULT_MARKET_TS = datetime(2024, 2, 1, 7, 0, tzinfo=UTC)
DEFAULT_MARKET_TS_STR = DEFAULT_MARKET_TS.isoformat()


# --------------------------------------------------------------------------- #
# Data fixtures
# --------------------------------------------------------------------------- #

def _make_ohlcv(
    symbol: str = "NSE:SBIN",
    timeframe: str = "1d",
    n: int = 50,
    start: datetime = datetime(2024, 1, 1, tzinfo=UTC),
    price: float = 100.0,
    freq: str = "D",
) -> pd.DataFrame:
    """Build a valid OHLCV DataFrame with a 'timestamp' column."""
    idx = pd.date_range(start=start, periods=n, freq=freq, tz="UTC")
    closes = np.linspace(price, price + 10, n)
    df = pd.DataFrame({
        "timestamp": idx,
        "open": closes - 0.5,
        "high": closes + 0.5,
        "low": closes - 0.5,
        "close": closes,
        "volume": np.full(n, 1_000_000, dtype=float),
    })
    return df


def _make_provider(
    ohlcv_map: dict[tuple[str, str], pd.DataFrame] | None = None,
    default_df: pd.DataFrame | None = None,
) -> MagicMock:
    """Create a mock data provider keyed by (symbol, timeframe)."""
    provider = MagicMock()

    def _fetch(symbol: str, timeframe: str):
        key = (symbol, timeframe)
        if ohlcv_map is not None and key in ohlcv_map:
            return ohlcv_map[key]
        if default_df is not None:
            return default_df
        return None

    provider.side_effect = _fetch
    return provider


# --------------------------------------------------------------------------- #
# Compatibility result builders
# --------------------------------------------------------------------------- #

def _make_compatibility_entry(
    symbol: str = "NSE:SBIN",
    rank: int = 1,
    strategy_id: str = "ema_crossover",
    timeframe: str = "1d",
    score: float = 0.9,
    version: str = "1.0.0",
    family: str = "trend",
    data_points: int = 100,
    min_required_bars: int = 20,
    market_timestamp: str | None = DEFAULT_MARKET_TS_STR,
    ranking_id: str | None = "rank-test",
    scan_id: str | None = "scan-test",
) -> StrategyCompatibility:
    return StrategyCompatibility(
        strategy_id=strategy_id,
        strategy_version=version,
        strategy_family=family,
        timeframe=timeframe,
        opportunity_symbol=symbol,
        opportunity_rank=rank,
        compatibility_score=score,
        features=CompatibilityFeatures(
            trend_score=0.7,
            momentum_score=0.8,
            volatility_score=0.3,
            liquidity_score=0.9,
        ),
        score_components={
            "trend": 0.7,
            "momentum": 0.8,
            "volatility": 0.3,
            "liquidity": 0.9,
        },
        freshness_factor=1.0,
        market_timestamp=market_timestamp,
        ranking_id=ranking_id,
        scan_id=scan_id,
        data_points=data_points,
        min_required_bars=min_required_bars,
    )


def _make_compat_result(
    entries: list[StrategyCompatibility] | None = None,
    ranking_id: str = "rank-test",
    scan_id: str = "scan-test",
    config_snapshot: dict | None = None,
) -> CompatibilityResult:
    if entries is None:
        entries = [_make_compatibility_entry()]
    if config_snapshot is None:
        config_snapshot = {"version": "phase4-v1"}
    return CompatibilityResult(
        result_id="compat-result-test",
        ranking_id=ranking_id,
        scan_id=scan_id,
        evaluated_at=DEFAULT_MARKET_TS_STR,
        evaluated_count=len(entries),
        compatible=entries,
        exclusions=[],
        config_snapshot=config_snapshot,
    )


def _make_engine(
    provider: MagicMock | None = None,
    enabled: bool = True,
    discovery_module: str = "trading_system.strategy_factory.builtin",
) -> StrategyDecisionEngine:
    cfg = SelectionConfig(enabled=enabled, discovery_module=discovery_module)
    return StrategyDecisionEngine(cfg, data_provider=provider)


def _make_controller(
    provider: MagicMock | None = None,
    enabled: bool = True,
):
    from trading_system.autonomous.bot_config import (
        AutonomousBotConfig,
        BotMode,
        Source,
        TradingMode,
        UserConstraints,
    )
    config = AutonomousBotConfig(
        bot_id="bot-test-p5",
        name="Phase 5 Test Bot",
        mode=BotMode.AUTONOMOUS,
        trading_mode=TradingMode.PAPER,
        enabled=enabled,
        user_constraints=UserConstraints(
            allowed_symbols=frozenset({"NSE:SBIN", "NSE:TCS"}),
            allowed_strategy_ids=frozenset(BUILTIN_IDS),
            allowed_timeframes=frozenset({"1m", "5m", "15m", "30m", "1h", "4h", "1d"}),
        ),
        max_simultaneous_positions=5,
        source=Source.AUTONOMOUS,
    )
    cc = MagicMock(spec=PaperTradingControlCenter)
    cc.load_market_data = provider or (lambda s, tf: None)
    cc.list_deployments.return_value = []
    return AutonomousController(config=config, control_center=cc)


# --------------------------------------------------------------------------- #
# Test: Strategy discovery
# --------------------------------------------------------------------------- #


class TestStrategyDiscovery:
    """Verify the Phase 5 engine discovers strategies via the factory."""

    def test_discovers_builtin_strategies(self):
        engine = _make_engine(enabled=True)
        ids = engine.discovered_strategy_ids
        assert "ema_crossover" in ids
        assert "rsi_mean_reversion" in ids

    def test_disabled_engine_no_discovery(self):
        engine = _make_engine(enabled=False)
        ids = engine.discovered_strategy_ids
        assert ids == []

    def test_discovery_failure_graceful(self):
        engine = _make_engine(enabled=True, discovery_module="nonexistent.module.xyz")
        ids = engine.discovered_strategy_ids
        assert ids == []

    def test_build_from_discovery_resolves_strategy(self):
        engine = _make_engine(enabled=True)
        engine._ensure_discovered()
        selected = SelectedConfiguration(
            strategy_id="ema_crossover",
            strategy_version="1.0.0",
            strategy_family="trend",
            timeframe="1d",
            compatibility_score=0.9,
            compatibility_order=0,
            score_components={"trend": 0.7},
            freshness_factor=1.0,
            data_points=100,
            min_required_bars=20,
        )
        bars = _make_ohlcv(n=50)
        provider = _make_provider(default_df=bars)
        engine2 = StrategyDecisionEngine(
            SelectionConfig(),
            data_provider=provider,
        )
        signal, error, snap = engine2._evaluate_selected_strategy(
            symbol="NSE:SBIN",
            selected=selected,
            market_timestamp=DEFAULT_MARKET_TS_STR,
        )
        assert error is None
        assert signal is not None
        assert signal.action == SignalAction.BUY


# --------------------------------------------------------------------------- #
# Test: Strategy selection (deterministic policy)
# --------------------------------------------------------------------------- #


class TestStrategySelection:
    """Verify the deterministic selection policy selects the right configuration."""

    def test_selects_highest_compatibility_first(self):
        provider = _make_provider(default_df=_make_ohlcv(n=50))
        engine = _make_engine(provider=provider)

        cfg_a = _make_compatibility_entry(
            symbol="NSE:SBIN", rank=1, strategy_id="ema_crossover",
            timeframe="1d", score=0.7,
        )
        cfg_b = _make_compatibility_entry(
            symbol="NSE:SBIN", rank=1, strategy_id="rsi_mean_reversion",
            timeframe="1d", score=0.9,
        )
        result = _make_compat_result(entries=[cfg_a, cfg_b])
        decision_result = engine.generate_decisions(result)

        decision = decision_result.decision_for("NSE:SBIN")
        assert decision is not None
        assert decision.selected_configuration is not None
        assert decision.selected_configuration.strategy_id == "rsi_mean_reversion"
        assert decision.selected_configuration.compatibility_score == 0.9

    def test_tie_breaks_by_strategy_id_asc(self):
        provider = _make_provider(default_df=_make_ohlcv(n=50))
        engine = _make_engine(provider=provider)

        cfg_a = _make_compatibility_entry(
            symbol="NSE:SBIN", rank=1, strategy_id="ema_crossover",
            timeframe="1d", score=0.9,
        )
        cfg_b = _make_compatibility_entry(
            symbol="NSE:SBIN", rank=1, strategy_id="rsi_mean_reversion",
            timeframe="1d", score=0.9,
        )
        result = _make_compat_result(entries=[cfg_a, cfg_b])
        decision_result = engine.generate_decisions(result)

        decision = decision_result.decision_for("NSE:SBIN")
        assert decision.selected_configuration.strategy_id == "ema_crossover"

    def test_tie_breaks_by_timeframe_chronological(self):
        provider = _make_provider(default_df=_make_ohlcv(n=50))
        engine = _make_engine(provider=provider)

        cfg_1d = _make_compatibility_entry(
            symbol="NSE:SBIN", rank=1, strategy_id="ema_crossover",
            timeframe="1d", score=0.9,
        )
        cfg_1h = _make_compatibility_entry(
            symbol="NSE:SBIN", rank=1, strategy_id="ema_crossover",
            timeframe="1h", score=0.9,
        )
        result = _make_compat_result(entries=[cfg_1d, cfg_1h])
        decision_result = engine.generate_decisions(result)

        decision = decision_result.decision_for("NSE:SBIN")
        assert decision.selected_configuration.timeframe == "1h"

    def test_selection_factors_populated(self):
        provider = _make_provider(default_df=_make_ohlcv(n=50))
        engine = _make_engine(provider=provider)
        result = _make_compat_result()
        decision_result = engine.generate_decisions(result)

        decision = decision_result.decision_for("NSE:SBIN")
        assert len(decision.selection_factors) > 0
        names = [f.name for f in decision.selection_factors]
        assert "compatibility_score" in names
        assert "strategy_id" in names
        assert "timeframe" in names
        assert "data_adequacy" in names
        assert "family_profile" in names

    def test_multiple_opportunities_independent_selection(self):
        provider = _make_provider(default_df=_make_ohlcv(n=50))
        engine = _make_engine(provider=provider)

        cfg_sbin = _make_compatibility_entry(
            symbol="NSE:SBIN", rank=1, strategy_id="ema_crossover",
            timeframe="1d", score=0.9,
        )
        cfg_tcs = _make_compatibility_entry(
            symbol="NSE:TCS", rank=2, strategy_id="rsi_mean_reversion",
            timeframe="5m", score=0.85,
        )
        result = _make_compat_result(entries=[cfg_sbin, cfg_tcs])
        decision_result = engine.generate_decisions(result)

        d_sbin = decision_result.decision_for("NSE:SBIN")
        d_tcs = decision_result.decision_for("NSE:TCS")
        assert d_sbin is not None
        assert d_tcs is not None
        assert d_sbin.selected_configuration.strategy_id == "ema_crossover"
        assert d_tcs.selected_configuration.strategy_id == "rsi_mean_reversion"
        assert d_tcs.selected_configuration.timeframe == "5m"

    def test_no_configurations_produces_rejection(self):
        engine = _make_engine(enabled=True)
        empty_result = _make_compat_result(entries=[])
        decision_result = engine.generate_decisions(empty_result)
        assert decision_result.empty
        assert decision_result.valid_count == 0


# --------------------------------------------------------------------------- #
# Test: Strategy evaluation
# --------------------------------------------------------------------------- #


class TestStrategyEvaluation:
    """Verify the selected strategy is evaluated on look-ahead-safe data."""

    def test_evaluates_builtin_strategy(self):
        bars = _make_ohlcv(n=50)
        provider = _make_provider(default_df=bars)
        engine = _make_engine(provider=provider)
        result = _make_compat_result()
        decision_result = engine.generate_decisions(result)

        decision = decision_result.decision_for("NSE:SBIN")
        assert decision.is_valid
        assert decision.signal is not None
        assert decision.signal.strategy_id == "ema_crossover"
        assert decision.signal.action in {SignalAction.BUY, SignalAction.SELL,
                                          SignalAction.EXIT, SignalAction.HOLD}

    def test_signal_has_valid_reference_price(self):
        bars = _make_ohlcv(n=50)
        provider = _make_provider(default_df=bars)
        engine = _make_engine(provider=provider)
        result = _make_compat_result()
        decision_result = engine.generate_decisions(result)

        decision = decision_result.decision_for("NSE:SBIN")
        assert decision.signal is not None
        assert decision.signal.reference_price > 0
        assert 0.0 <= decision.signal.confidence <= 1.0
        assert decision.signal.timestamp.tzinfo is not None

    def test_signal_action_matches_market_condition(self):
        bars = _make_ohlcv(n=50, price=100.0)
        provider = _make_provider(default_df=bars)
        engine = _make_engine(provider=provider)
        result = _make_compat_result()
        decision_result = engine.generate_decisions(result)

        decision = decision_result.decision_for("NSE:SBIN")
        assert decision.signal is not None
        assert decision.signal.action in {SignalAction.BUY, SignalAction.SELL,
                                          SignalAction.EXIT, SignalAction.HOLD}

    def test_strategy_evaluated_with_selected_timeframe(self):
        bars_5m = _make_ohlcv(n=50, freq="5min", start=datetime(2024, 1, 31, 0, tzinfo=UTC))
        provider = _make_provider(ohlcv_map={("NSE:SBIN", "5m"): bars_5m})
        engine = _make_engine(provider=provider)
        entry = _make_compatibility_entry(
            symbol="NSE:SBIN", rank=1, strategy_id="ema_crossover",
            timeframe="5m", score=0.9,
        )
        result = _make_compat_result(entries=[entry])
        decision_result = engine.generate_decisions(result)

        decision = decision_result.decision_for("NSE:SBIN")
        assert decision.is_valid
        assert decision.selected_configuration.timeframe == "5m"
        assert decision.signal is not None


class TestSnapshotConsistency:
    """Verify look-ahead protection and snapshot identity consistency."""

    def test_no_future_bars_in_evaluation(self):
        bars = _make_ohlcv(n=50)
        future_bar = _make_ohlcv(n=10, start=datetime(2024, 3, 1, tzinfo=UTC))
        full_df = pd.concat([bars, future_bar], ignore_index=True)
        provider = _make_provider(ohlcv_map={("NSE:SBIN", "1d"): full_df})
        engine = _make_engine(provider=provider)
        result = _make_compat_result()
        decision_result = engine.generate_decisions(result)

        decision = decision_result.decision_for("NSE:SBIN")
        assert decision.is_valid
        assert decision.signal is not None
        assert decision.signal.timestamp <= DEFAULT_MARKET_TS

    def test_snapshot_identity_deterministic(self):
        bars = _make_ohlcv(n=50)
        provider = _make_provider(ohlcv_map={("NSE:SBIN", "1d"): bars})
        engine = _make_engine(provider=provider)
        result = _make_compat_result()
        dr1 = engine.generate_decisions(result)
        dr2 = engine.generate_decisions(result)

        d1 = dr1.decision_for("NSE:SBIN")
        d2 = dr2.decision_for("NSE:SBIN")
        assert d1.snapshot_identity == d2.snapshot_identity

    def test_empty_bars_produce_empty_snapshot(self):
        engine = _make_engine(provider=_make_provider(default_df=pd.DataFrame()))
        entry = _make_compatibility_entry(
            symbol="NSE:SBIN", rank=1, strategy_id="ema_crossover",
            timeframe="1d", score=0.9,
        )
        result = _make_compat_result(entries=[entry])
        decision_result = engine.generate_decisions(result)

        decision = decision_result.decision_for("NSE:SBIN")
        assert not decision.is_valid
        assert decision.snapshot_identity is not None
        assert len(decision.snapshot_identity) > 0

    def test_lookahead_protection_filters_future_bars(self):
        """Bars after market_timestamp are filtered out; only past bars used."""
        market_ts = datetime(2024, 1, 30, 7, 0, tzinfo=UTC)
        # Create ordered OHLCV: Jan 1 through Feb 5 (includes future bars past
        # market_timestamp that must be filtered out).
        full_df = _make_ohlcv(n=50, start=datetime(2024, 1, 1, tzinfo=UTC), freq="D")
        provider = _make_provider(ohlcv_map={("NSE:SBIN", "1d"): full_df})
        engine = _make_engine(provider=provider)
        entry = _make_compatibility_entry(
            symbol="NSE:SBIN", rank=1,
            strategy_id="ema_crossover",
            timeframe="1d",
            score=0.9,
            market_timestamp=market_ts.isoformat(),
        )
        result = _make_compat_result(entries=[entry])
        decision_result = engine.generate_decisions(result)

        decision = decision_result.decision_for("NSE:SBIN")
        assert decision.is_valid
        assert decision.signal is not None
        assert decision.signal.timestamp <= market_ts


class TestDeterminism:
    """Verify deterministic outputs from identical inputs."""

    def test_same_result_id_repeated(self):
        bars = _make_ohlcv(n=50)
        provider = _make_provider(default_df=bars)
        engine = _make_engine(provider=provider)
        result = _make_compat_result()
        dr1 = engine.generate_decisions(result)
        dr2 = engine.generate_decisions(result)
        assert dr1.result_id == dr2.result_id

    def test_same_decision_id_repeated(self):
        bars = _make_ohlcv(n=50)
        provider = _make_provider(default_df=bars)
        engine = _make_engine(provider=provider)
        result = _make_compat_result()
        dr1 = engine.generate_decisions(result)
        dr2 = engine.generate_decisions(result)
        d1 = dr1.decision_for("NSE:SBIN")
        d2 = dr2.decision_for("NSE:SBIN")
        assert d1.decision_id == d2.decision_id

    def test_deterministic_selection_with_ties(self):
        bars = _make_ohlcv(n=50)
        provider = _make_provider(default_df=bars)
        engine = _make_engine(provider=provider)

        cfg_a = _make_compatibility_entry(
            symbol="NSE:SBIN", rank=1, strategy_id="ema_crossover",
            timeframe="1d", score=0.9,
        )
        cfg_b = _make_compatibility_entry(
            symbol="NSE:SBIN", rank=1, strategy_id="rsi_mean_reversion",
            timeframe="1d", score=0.9,
        )
        result = _make_compat_result(entries=[cfg_a, cfg_b])
        dr1 = engine.generate_decisions(result)
        dr2 = engine.generate_decisions(result)
        d1 = dr1.decision_for("NSE:SBIN")
        d2 = dr2.decision_for("NSE:SBIN")
        assert d1.selected_configuration.strategy_id == d2.selected_configuration.strategy_id
        assert d1.signal.action == d2.signal.action

    def test_deterministic_signal_output(self):
        bars = _make_ohlcv(n=50)
        provider = _make_provider(default_df=bars)
        engine = _make_engine(provider=provider)
        result = _make_compat_result()
        dr1 = engine.generate_decisions(result)
        dr2 = engine.generate_decisions(result)
        d1 = dr1.decision_for("NSE:SBIN")
        d2 = dr2.decision_for("NSE:SBIN")
        assert d1.signal.action == d2.signal.action
        assert d1.signal.reference_price == d2.signal.reference_price


# --------------------------------------------------------------------------- #
# Test: Auditability
# --------------------------------------------------------------------------- #


class TestAuditability:
    """Verify decisions are auditable: decision_id, snapshot_identity, to_dict."""

    def test_decision_has_decision_id(self):
        provider = _make_provider(default_df=_make_ohlcv(n=50))
        engine = _make_engine(provider=provider)
        result = _make_compat_result()
        dr = engine.generate_decisions(result)
        decision = dr.decision_for("NSE:SBIN")
        assert len(decision.decision_id) == 64

    def test_result_has_result_id(self):
        provider = _make_provider(default_df=_make_ohlcv(n=50))
        engine = _make_engine(provider=provider)
        result = _make_compat_result()
        dr = engine.generate_decisions(result)
        assert len(dr.result_id) == 64

    def test_result_traces_to_ranking_id(self):
        provider = _make_provider(default_df=_make_ohlcv(n=50))
        engine = _make_engine(provider=provider)
        result = _make_compat_result(ranking_id="rank-abc123")
        dr = engine.generate_decisions(result)
        assert dr.ranking_id == "rank-abc123"

    def test_to_dict_serializable(self):
        provider = _make_provider(default_df=_make_ohlcv(n=50))
        engine = _make_engine(provider=provider)
        result = _make_compat_result()
        dr = engine.generate_decisions(result)
        decision = dr.decision_for("NSE:SBIN")
        d = decision.to_dict()
        assert d["decision_id"] == decision.decision_id
        assert d["opportunity_symbol"] == "NSE:SBIN"
        assert d["is_valid"] is True
        assert d["status"] == DecisionStatus.VALID.value
        assert d["signal"] is not None
        assert d["selected_configuration"] is not None

    def test_rejected_decision_to_dict(self):
        engine = _make_engine(enabled=True)
        empty_result = _make_compat_result(entries=[])
        dr = engine.generate_decisions(empty_result)
        assert dr.empty

    def test_decision_properties(self):
        provider = _make_provider(default_df=_make_ohlcv(n=50))
        engine = _make_engine(provider=provider)
        result = _make_compat_result()
        dr = engine.generate_decisions(result)
        decision = dr.decision_for("NSE:SBIN")
        assert decision.is_valid
        assert not decision.is_rejected
        assert decision.action is not None
        assert decision.action in {"buy", "sell", "hold", "exit"}

    def test_valid_count_and_rejected_count(self):
        provider = _make_provider(default_df=_make_ohlcv(n=50))
        engine = _make_engine(provider=provider)
        cfg_a = _make_compatibility_entry(
            symbol="NSE:SBIN", rank=1, strategy_id="ema_crossover",
            timeframe="1d", score=0.9,
        )
        cfg_b = _make_compatibility_entry(
            symbol="NSE:TCS", rank=2, strategy_id="rsi_mean_reversion",
            timeframe="1d", score=0.85,
        )
        result = _make_compat_result(entries=[cfg_a, cfg_b])
        dr = engine.generate_decisions(result)
        assert dr.valid_count == 2
        assert dr.rejected_count == 0
        assert len(dr.decisions) == 2

    def test_decision_decision_timestamp(self):
        provider = _make_provider(default_df=_make_ohlcv(n=50))
        engine = _make_engine(provider=provider)
        result = _make_compat_result()
        dr = engine.generate_decisions(result)
        decision = dr.decision_for("NSE:SBIN")
        parsed = datetime.fromisoformat(decision.decision_timestamp)
        assert parsed.tzinfo is not None


# --------------------------------------------------------------------------- #
# Test: Failure handling (structured rejections)
# --------------------------------------------------------------------------- #


class TestFailureHandling:
    """Verify failures produce structured rejections, never accidental execution."""

    def test_no_data_provider_rejected(self):
        engine = _make_engine(provider=None)
        result = _make_compat_result()
        dr = engine.generate_decisions(result)
        decision = dr.decision_for("NSE:SBIN")
        assert not decision.is_valid
        assert decision.evaluation_error == DecisionExclusionReason.NO_DATA_PROVIDER.value
        assert decision.signal is None

    def test_strategy_not_found_rejected(self):
        provider = _make_provider(default_df=_make_ohlcv(n=50))
        engine = _make_engine(provider=provider)
        entry = _make_compatibility_entry(
            symbol="NSE:SBIN", rank=1, strategy_id="nonexistent_strategy",
            timeframe="1d", score=0.9,
        )
        result = _make_compat_result(entries=[entry])
        dr = engine.generate_decisions(result)
        decision = dr.decision_for("NSE:SBIN")
        assert not decision.is_valid
        assert decision.evaluation_error == DecisionExclusionReason.STRATEGY_NOT_FOUND.value

    def test_version_mismatch_rejected(self):
        provider = _make_provider(default_df=_make_ohlcv(n=50))
        engine = _make_engine(provider=provider)
        entry = _make_compatibility_entry(
            symbol="NSE:SBIN", rank=1, strategy_id="ema_crossover",
            timeframe="1d", score=0.9, version="9.9.9",
        )
        result = _make_compat_result(entries=[entry])
        dr = engine.generate_decisions(result)
        decision = dr.decision_for("NSE:SBIN")
        assert not decision.is_valid
        assert DecisionExclusionReason.INVALID_STRATEGY_OUTPUT.value in decision.evaluation_error
        assert "version mismatch" in decision.evaluation_error

    def test_insufficient_data_rejected(self):
        short_df = _make_ohlcv(n=10)
        provider = _make_provider(ohlcv_map={("NSE:SBIN", "1d"): short_df})
        engine = _make_engine(provider=provider)
        entry = _make_compatibility_entry(
            symbol="NSE:SBIN", rank=1, strategy_id="ema_crossover",
            timeframe="1d", score=0.9, min_required_bars=26,
        )
        result = _make_compat_result(entries=[entry])
        dr = engine.generate_decisions(result)
        decision = dr.decision_for("NSE:SBIN")
        assert not decision.is_valid
        assert DecisionExclusionReason.INSUFFICIENT_DATA.value in decision.evaluation_error

    def test_nan_prices_rejected(self):
        bars = _make_ohlcv(n=50)
        bars.loc[10, "close"] = np.nan
        provider = _make_provider(ohlcv_map={("NSE:SBIN", "1d"): bars})
        engine = _make_engine(provider=provider)
        result = _make_compat_result()
        dr = engine.generate_decisions(result)
        decision = dr.decision_for("NSE:SBIN")
        assert not decision.is_valid
        assert DecisionExclusionReason.INVALID_MARKET_SNAPSHOT.value in decision.evaluation_error

    def test_data_provider_error_rejected(self):
        def failing_provider(symbol: str, timeframe: str):
            raise RuntimeError("network down")

        engine = StrategyDecisionEngine(
            SelectionConfig(), data_provider=failing_provider,
        )
        entry = _make_compatibility_entry(
            symbol="NSE:SBIN", rank=1, strategy_id="ema_crossover",
            timeframe="1d", score=0.9,
        )
        result = _make_compat_result(entries=[entry])
        dr = engine.generate_decisions(result)
        decision = dr.decision_for("NSE:SBIN")
        assert not decision.is_valid
        assert DecisionExclusionReason.EVALUATION_ERROR.value in decision.evaluation_error
        assert "data provider error" in decision.evaluation_error

    def test_strategy_evaluation_error_rejected(self):
        provider = _make_provider(default_df=_make_ohlcv(n=50))
        engine = _make_engine(provider=provider)
        entry = _make_compatibility_entry(
            symbol="NSE:SBIN", rank=1, strategy_id="ema_crossover",
            timeframe="1d", score=0.9,
        )
        result = _make_compat_result(entries=[entry])
        with patch.object(
            StrategyDecisionEngine, "_evaluate_selected_strategy",
            return_value=(None, "custom evaluation error", "snap123"),
        ):
            dr = engine.generate_decisions(result)
        decision = dr.decision_for("NSE:SBIN")
        assert not decision.is_valid
        assert decision.evaluation_error == "custom evaluation error"

    def test_empty_market_timestamp_rejected(self):
        bars = _make_ohlcv(n=50)
        provider = _make_provider(ohlcv_map={("NSE:SBIN", "1d"): bars})
        engine = _make_engine(provider=provider)
        entry = _make_compatibility_entry(
            symbol="NSE:SBIN", rank=1, strategy_id="ema_crossover",
            timeframe="1d", score=0.9, market_timestamp="",
        )
        result = _make_compat_result(entries=[entry])
        dr = engine.generate_decisions(result)
        decision = dr.decision_for("NSE:SBIN")
        assert not decision.is_valid
        assert DecisionExclusionReason.MISSING_SNAPSHOT.value in decision.evaluation_error

    def test_rejected_decision_has_no_signal(self):
        engine = _make_engine(provider=None)
        result = _make_compat_result()
        dr = engine.generate_decisions(result)
        decision = dr.decision_for("NSE:SBIN")
        assert not decision.is_valid
        assert decision.signal is None


# --------------------------------------------------------------------------- #
# Test: Selection policy (pure unit tests)
# --------------------------------------------------------------------------- #


class TestSelectionPolicy:
    """Verify SelectionPolicy sort_key and sort_group ordering."""

    def test_selection_key_higher_score_first(self):
        cfg_a = _make_compatibility_entry(strategy_id="s1", score=0.9)
        cfg_b = _make_compatibility_entry(strategy_id="s2", score=0.8)
        key_a = SelectionPolicy.selection_key(cfg_a)
        key_b = SelectionPolicy.selection_key(cfg_b)
        assert key_a < key_b

    def test_selection_key_tiebreak_strategy_id(self):
        cfg_a = _make_compatibility_entry(strategy_id="aaa", score=0.9)
        cfg_b = _make_compatibility_entry(strategy_id="bbb", score=0.9)
        key_a = SelectionPolicy.selection_key(cfg_a)
        key_b = SelectionPolicy.selection_key(cfg_b)
        assert key_a < key_b

    def test_timeframe_sort_key_chronological(self):
        assert _timeframe_sort_key("1m") < _timeframe_sort_key("5m")
        assert _timeframe_sort_key("5m") < _timeframe_sort_key("1h")
        assert _timeframe_sort_key("1h") < _timeframe_sort_key("1d")

    def test_timeframe_sort_key_unknown(self):
        from trading_system.autonomous.decision import _timeframe_sort_key
        # Unknown timeframes sort after known ones.
        k_known = _timeframe_sort_key("1d")
        k_unknown = _timeframe_sort_key("2h")
        assert k_known < k_unknown

    def test_sort_group_orders_by_policy(self):
        cf = CompatibilityFeatures(
            trend_score=0.7, momentum_score=0.8,
            volatility_score=0.3, liquidity_score=0.9,
        )
        cfg_worst = _make_compatibility_entry(
            strategy_id="zzz", timeframe="1d", score=0.5,
        )
        cfg_best = _make_compatibility_entry(
            strategy_id="aaa", timeframe="1m", score=0.9,
        )
        cfg_mid = _make_compatibility_entry(
            strategy_id="mmm", timeframe="5m", score=0.7,
        )
        sorted_cfgs = SelectionPolicy.sort_group([cfg_worst, cfg_best, cfg_mid])
        assert sorted_cfgs[0] is cfg_best
        assert sorted_cfgs[1] is cfg_mid
        assert sorted_cfgs[2] is cfg_worst


def _timeframe_sort_key(timeframe: str):
    from trading_system.autonomous.decision import _timeframe_sort_key
    return _timeframe_sort_key(timeframe)


# --------------------------------------------------------------------------- #
# Test: Controller integration
# --------------------------------------------------------------------------- #


class TestControllerIntegration:
    """Verify the controller orchestrates Phase 5 correctly."""

    def test_controller_generates_decisions(self):
        bars = _make_ohlcv(n=50)
        controller = _make_controller(provider=_make_provider(default_df=bars))
        result = _make_compat_result()
        dr = controller.generate_strategy_decisions(result)
        assert isinstance(dr, DecisionResult)
        assert dr.valid_count > 0

    def test_controller_does_not_create_deployments(self):
        bars = _make_ohlcv(n=50)
        controller = _make_controller(provider=_make_provider(default_df=bars))
        result = _make_compat_result()
        controller.generate_strategy_decisions(result)
        assert controller.config.deployment_count == 0

    def test_controller_does_not_create_orders(self):
        bars = _make_ohlcv(n=50)
        controller = _make_controller(provider=_make_provider(default_df=bars))
        result = _make_compat_result()
        controller.generate_strategy_decisions(result)
        cc = controller.control_center
        order_methods = [
            "create_depot_order", "submit_order", "execute_order",
            "create_order", "send_order",
        ]
        for method_name in order_methods:
            if hasattr(cc, method_name):
                mock_method = getattr(cc, method_name)
                if hasattr(mock_method, "called"):
                    assert not mock_method.called

    def test_controller_uses_load_market_data_provider(self):
        bars = _make_ohlcv(n=50)
        provider = _make_provider(ohlcv_map={("NSE:SBIN", "1d"): bars})
        controller = _make_controller(provider=provider)
        result = _make_compat_result()
        controller.generate_strategy_decisions(result)
        provider.assert_called_with("NSE:SBIN", "1d")

    def test_controller_disabled_engine(self):
        bars = _make_ohlcv(n=50)
        controller = _make_controller(provider=_make_provider(default_df=bars), enabled=False)
        result = _make_compat_result()
        dr = controller.generate_strategy_decisions(result)
        assert dr.valid_count == 0
        assert dr.rejected_count == 0

    def test_controller_passes_custom_config(self):
        bars = _make_ohlcv(n=50)
        controller = _make_controller(provider=_make_provider(default_df=bars))
        result = _make_compat_result()
        custom_cfg = SelectionConfig(enabled=False)
        dr = controller.generate_strategy_decisions(result, config=custom_cfg)
        assert dr.valid_count == 0

    def test_controller_no_compatible_configurations(self):
        controller = _make_controller(provider=MagicMock())
        empty_result = _make_compat_result(entries=[])
        dr = controller.generate_strategy_decisions(empty_result)
        assert dr.empty
        assert dr.valid_count == 0


# --------------------------------------------------------------------------- #
# Test: Safety — no execution paths
# --------------------------------------------------------------------------- #


class TestSafety:
    """Verify Phase 5 has no execution/trading paths."""

    def test_no_live_trading_constants(self):
        cfg = SelectionConfig()
        assert cfg.enabled is True
        assert not hasattr(cfg, "live_trading")
        assert not hasattr(cfg, "execute")

    def test_no_broker_fields_in_trading_decision(self):
        decision = TradingDecision(
            decision_id="abc123",
            opportunity_symbol="NSE:SBIN",
            opportunity_rank=1,
            market_timestamp=DEFAULT_MARKET_TS_STR,
            snapshot_identity="snap123",
            is_valid=True,
            status=DecisionStatus.VALID.value,
            decision_timestamp=DEFAULT_MARKET_TS_STR,
        )
        fields = set(TradingDecision.model_fields.keys())
        for forbidden in ["broker", "order", "deployment", "position"]:
            assert forbidden not in fields

    def test_no_broker_fields_in_selected_config(self):
        fields = set(SelectedConfiguration.model_fields.keys())
        for forbidden in ["broker", "order", "deployment", "position"]:
            assert forbidden not in fields

    def test_no_broker_fields_in_decision_result(self):
        fields = set(DecisionResult.model_fields.keys())
        for forbidden in ["broker", "order", "deployment", "position"]:
            assert forbidden not in fields

    def test_engine_has_no_execution_methods(self):
        engine = _make_engine(provider=MagicMock())
        for method_name in [
            "execute", "trade", "submit_order", "create_deployment",
            "allocate_capital", "send_to_broker",
        ]:
            assert not hasattr(engine, method_name)

    def test_decision_result_has_config_snapshot(self):
        provider = _make_provider(default_df=_make_ohlcv(n=50))
        engine = _make_engine(provider=provider)
        result = _make_compat_result()
        dr = engine.generate_decisions(result)
        assert "enabled" in dr.config_snapshot
        assert "discovery_module" in dr.config_snapshot

    def test_decision_has_config_snapshot(self):
        provider = _make_provider(default_df=_make_ohlcv(n=50))
        engine = _make_engine(provider=provider)
        result = _make_compat_result()
        dr = engine.generate_decisions(result)
        decision = dr.decision_for("NSE:SBIN")
        assert "enabled" in decision.config_snapshot
        assert "discovery_module" in decision.config_snapshot

    def test_freeze_model_prevents_mutation(self):
        decision = TradingDecision(
            decision_id="d1",
            opportunity_symbol="NSE:SBIN",
            opportunity_rank=1,
            market_timestamp=DEFAULT_MARKET_TS_STR,
            snapshot_identity="snap123",
            is_valid=True,
            status=DecisionStatus.VALID.value,
            decision_timestamp=DEFAULT_MARKET_TS_STR,
        )
        with pytest.raises(Exception):
            decision.decision_id = "mutated"  # frozen=True

    def test_rejection_does_not_emit_buy_sell(self):
        engine = _make_engine(provider=None)
        result = _make_compat_result()
        dr = engine.generate_decisions(result)
        decision = dr.decision_for("NSE:SBIN")
        assert decision.action is None
        assert decision.signal is None
        assert decision.is_valid is False
        assert decision.status == DecisionStatus.REJECTED.value


# --------------------------------------------------------------------------- #
# Test: SelectionFactor model
# --------------------------------------------------------------------------- #


class TestSelectionFactor:
    """Verify SelectionFactor structure."""

    def test_factor_creation(self):
        factor = SelectionFactor(
            name="test",
            observed_value=0.95,
            required_value="best",
            matched=True,
        )
        assert factor.name == "test"
        assert factor.observed_value == 0.95
        assert factor.matched is True

    def test_factor_optional_observed_value(self):
        factor = SelectionFactor(
            name="test2",
            required_value="any",
            matched=False,
        )
        assert factor.observed_value is None
        assert factor.matched is False

    def test_factor_frozen(self):
        factor = SelectionFactor(
            name="test",
            observed_value=0.95,
            required_value="best",
            matched=True,
        )
        with pytest.raises(Exception):
            factor.name = "mutated"


# --------------------------------------------------------------------------- #
# Test: DecisionResult model
# --------------------------------------------------------------------------- #


class TestDecisionResultModel:
    """Verify DecisionResult model behaviour."""

    def test_decision_for_returns_correct(self):
        provider = _make_provider(default_df=_make_ohlcv(n=50))
        engine = _make_engine(provider=provider)
        cfg_a = _make_compatibility_entry(
            symbol="NSE:SBIN", rank=1,
        )
        cfg_b = _make_compatibility_entry(
            symbol="NSE:TCS", rank=2,
        )
        result = _make_compat_result(entries=[cfg_a, cfg_b])
        dr = engine.generate_decisions(result)
        assert dr.decision_for("NSE:SBIN") is not None
        assert dr.decision_for("NSE:TCS") is not None
        assert dr.decision_for("UNKNOWN") is None

    def test_empty_property(self):
        dr = DecisionResult(
            result_id="r", ranking_id=None, scan_id=None,
            evaluated_at=DEFAULT_MARKET_TS_STR, decisions=[],
            config_snapshot={},
        )
        assert dr.empty is True
        assert dr.valid_count == 0
        assert dr.rejected_count == 0

    def test_non_empty(self):
        dr = DecisionResult(
            result_id="r", ranking_id=None, scan_id=None,
            evaluated_at=DEFAULT_MARKET_TS_STR,
            decisions=[
                TradingDecision(
                    decision_id="d1", opportunity_symbol="S",
                    opportunity_rank=1, market_timestamp="",
                    snapshot_identity="s", is_valid=True,
                    status=DecisionStatus.VALID.value,
                    decision_timestamp="",
                ),
            ],
            config_snapshot={},
        )
        assert dr.empty is False
        assert dr.valid_count == 1
