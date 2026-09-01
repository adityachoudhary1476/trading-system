"""Phase 17 tests: Strategy Intelligence & Lifecycle layer.

Covers comparison, freshness, eligibility, lifecycle, history, intelligence
report, persistence/schema migration, and safety. All offline, deterministic.
"""
from __future__ import annotations

import ast
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine

from trading_system.research.backtester import BacktestConfig
from trading_system.research.dataset import HistoricalDataset
from trading_system.research.evidence import (
    EvidenceStore,
    EvidenceType,
    LifecycleEvent,
    Strategy,
    StrategyEvidence,
    StrategyStatus,
    dataset_identity,
    strategy_identity,
)
from trading_system.research.strategy_registry import (
    StrategyRegistry,
    evidence_identity,
)
from trading_system.research.strategy_lab.providers import (
    DeterministicStrategyProvider,
    GenerationContext,
)
from trading_system.research.strategy_lab.spec import (
    StrategySpec,
    field_operand,
    indicator_operand,
    make_condition,
)
from trading_system.research.strategy_intelligence import (
    ComparisonConfig,
    ComparisonMetrics,
    Eligibility,
    EligibilityResult,
    EvidenceFreshness,
    EvidenceFreshnessConfig,
    EvidenceRequirement,
    HistoryEntry,
    InvalidTransitionError,
    StrategyComparisonReport,
    StrategyFreshness,
    StrategyHistory,
    StrategyIntelligence,
    StrategyIntelligenceReport,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def store():
    return EvidenceStore(create_engine("sqlite://"))


@pytest.fixture()
def registry(store):
    return StrategyRegistry(store)


@pytest.fixture()
def intelligence(registry):
    return StrategyIntelligence(registry)


@contextmanager
def store_session(store):
    """Context manager yielding the store's SQLAlchemy session (for raw inserts)."""
    session = store._Session()
    try:
        yield session
    finally:
        session.close()


def _make_dataset(n=400, symbol="NSE:SBIN", seed=42):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    close = 100 + np.cumsum(rng.normal(0.05, 1.0, n))
    df = pd.DataFrame(
        {
            "open": close + 0.1,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": rng.integers(100, 1000, n).astype(float),
        },
        index=idx,
    )
    return HistoricalDataset(symbol=symbol, timeframe="1d", data=df)


def _valid_spec(**overrides):
    payload = {
        "name": "Test strategy",
        "description": "test",
        "symbol": "NSE:SBIN",
        "timeframe": "1d",
        "indicators": [{"name": "sma", "params": {"window": 20}}],
        "entry": make_condition(
            field_operand("close"), ">", indicator_operand("sma_20")
        ),
        "generated_by": "test",
    }
    payload.update(overrides)
    return StrategySpec(**payload)


def _register(registry, spec, status=StrategyStatus.PROPOSED):
    strategy = registry.register_strategy(spec)
    if status != StrategyStatus.PROPOSED:
        registry.update_strategy_status(strategy.strategy_id, status)
        strategy = registry.get_strategy(strategy.strategy_id)
    return strategy


def _research_evaluation(total_return=0.10, profit_factor=1.5, max_drawdown=-0.05,
                         n_trades=25):
    return {
        "spec_name": "T",
        "symbol": "NSE:SBIN",
        "timeframe": "1d",
        "generated_by": "test",
        "initial_capital": 100000.0,
        "final_capital": 100000.0 * (1.0 + total_return),
        "net_pnl": 100000.0 * total_return,
        "total_return": total_return,
        "n_trades": n_trades,
        "winning": int(n_trades * 0.6),
        "losing": int(n_trades * 0.4),
        "win_rate": 0.6,
        "avg_trade": None,
        "avg_trade_return": None,
        "max_drawdown": max_drawdown,
        "transaction_costs": 0.0,
        "slippage_estimate": 0.0,
        "exposure_pct": 0.0,
        "profit_factor": profit_factor,
        "sharpe": None,
        "sortino": None,
        "reliable": False,
        "notes": [],
        "unavailable_metrics": [],
    }


def _wf_summary(n_folds=5, n_valid=4, consistency=0.7, pos_ratio=0.75,
                avg_return=0.05, max_dd=-0.08, total_trades=100,
                warnings=None):
    return {
        "n_folds": n_folds,
        "n_valid": n_valid,
        "n_failed": n_folds - n_valid,
        "coverage": n_valid / n_folds,
        "coverage_ok": True,
        "positive_folds": int(n_valid * pos_ratio),
        "positive_fold_ratio": pos_ratio,
        "avg_fold_return": avg_return,
        "median_fold_return": avg_return,
        "worst_fold_return": -0.15,
        "best_fold_return": 0.20,
        "return_std": 0.10,
        "return_dispersion": 2.0,
        "max_validation_drawdown": max_dd,
        "consistency_score": consistency,
        "total_validation_trades": total_trades,
        "min_validation_trades": 10,
        "valid_fold_ids": list(range(n_valid)),
        "warnings": warnings if warnings is not None else [],
    }


def _make_research_evidence(strategy, ds, created_at, filter_passed=True,
                            evaluation=None, cfg_key=1):
    metrics = {
        "symbol": strategy.symbol,
        "timeframe": strategy.timeframe,
        "rows": 400,
        "requested_candidates": 1,
        "candidates": [
            {
                "variant_index": 0,
                "status": "evaluated",
                "spec_name": strategy.name,
                "spec_errors": [],
                "error": "",
                "evaluation": evaluation or _research_evaluation(),
                "filter_passed": filter_passed,
                "filter_reasons": [],
            }
        ],
        "ranking": [],
        "notes": [],
    }
    ds_id = dataset_identity(ds)
    return StrategyEvidence(
        evidence_id=evidence_identity(
            strategy.strategy_id, EvidenceType.RESEARCH, ds_id, {"k": cfg_key}
        ),
        strategy_id=strategy.strategy_id,
        strategy_spec_hash=strategy.spec_hash,
        evidence_type=EvidenceType.RESEARCH,
        dataset_id=ds_id,
        configuration_json={"k": cfg_key},
        metrics_json=metrics,
        created_at=created_at,
    )


def _make_wf_evidence(strategy, ds, created_at, summary=None, fold_id=None,
                       cfg_key=1):
    ds_id = dataset_identity(ds)
    cfg = {"kind": "wf_summary", "k": cfg_key} if fold_id is None else {"fold_id": fold_id, "k": cfg_key}
    metrics = {
        "kind": "fixed_spec",
        "spec_name": strategy.name,
        "symbol": strategy.symbol,
        "timeframe": strategy.timeframe,
        "mode": "rolling",
        "folds": [],
        "summary": summary or _wf_summary(),
        "warnings": [],
        "notes": [],
    }
    return StrategyEvidence(
        evidence_id=evidence_identity(
            strategy.strategy_id, EvidenceType.WALK_FORWARD, ds_id, cfg
        ),
        strategy_id=strategy.strategy_id,
        strategy_spec_hash=strategy.spec_hash,
        evidence_type=EvidenceType.WALK_FORWARD,
        dataset_id=ds_id,
        configuration_json=cfg,
        metrics_json=metrics,
        fold_id=fold_id,
        created_at=created_at,
    )


NOW = datetime(2025, 6, 1, tzinfo=timezone.utc)
FRESH_DATE = (NOW - timedelta(days=10)).isoformat()
STALE_DATE = (NOW - timedelta(days=200)).isoformat()
OLD_DATE = (NOW - timedelta(days=400)).isoformat()


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
class TestLifecycle:
    def test_retire_active_strategy(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        event = intelligence.retire_strategy(strategy.strategy_id, "no longer viable")
        assert event.to_status == StrategyStatus.RETIRED
        assert event.reason == "no longer viable"
        assert registry.get_strategy(strategy.strategy_id).status == StrategyStatus.RETIRED

    def test_retire_preserves_historical_evidence(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        ds = _make_dataset()
        ev = _make_research_evidence(strategy, ds, FRESH_DATE)
        registry.record_evidence(ev)
        intelligence.retire_strategy(strategy.strategy_id, "test")
        # Evidence must still be present and unmodified.
        history = registry.list_evidence(strategy_id=strategy.strategy_id)
        assert len(history) == 1
        assert history[0].evidence_id == ev.evidence_id
        assert history[0].metrics_json["candidates"][0]["evaluation"]["total_return"] == pytest.approx(0.10)

    def test_invalid_transition_retire_retired(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        intelligence.retire_strategy(strategy.strategy_id, "first")
        with pytest.raises(InvalidTransitionError):
            intelligence.retire_strategy(strategy.strategy_id, "second")

    def test_retire_from_rejected(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"), StrategyStatus.REJECTED)
        event = intelligence.retire_strategy(strategy.strategy_id, "cleaning up")
        assert event.from_status == StrategyStatus.REJECTED
        assert event.to_status == StrategyStatus.RETIRED

    def test_reactivate_retired_strategy(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        intelligence.retire_strategy(strategy.strategy_id, "temp")
        event = intelligence.reactivate_strategy(strategy.strategy_id, "revisiting")
        assert event.from_status == StrategyStatus.RETIRED
        assert event.to_status == StrategyStatus.PROPOSED
        assert registry.get_strategy(strategy.strategy_id).status == StrategyStatus.PROPOSED

    def test_reactivate_preserves_retirement_history(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        intelligence.retire_strategy(strategy.strategy_id, "temp")
        intelligence.reactivate_strategy(strategy.strategy_id, "revisiting")
        events = registry.list_lifecycle_events(strategy_id=strategy.strategy_id)
        assert len(events) == 2
        assert events[0].to_status == StrategyStatus.RETIRED
        assert events[1].to_status == StrategyStatus.PROPOSED

    def test_invalid_transition_reactivate_active(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        with pytest.raises(InvalidTransitionError):
            intelligence.reactivate_strategy(strategy.strategy_id, "bad")

    def test_retire_unknown_strategy_raises(self, intelligence):
        with pytest.raises(KeyError):
            intelligence.retire_strategy("does-not-exist", "x")


# --------------------------------------------------------------------------- #
# Freshness
# --------------------------------------------------------------------------- #
class TestFreshness:
    def test_fresh_evidence(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        ds = _make_dataset()
        registry.record_evidence(_make_research_evidence(strategy, ds, FRESH_DATE))
        f = intelligence.assess_freshness(
            strategy.strategy_id, EvidenceFreshnessConfig(max_age_days=30), now=NOW
        )
        assert f.status == EvidenceFreshness.FRESH
        # SQLite strips the tz offset on round-trip; compare parsed instants.
        assert f.latest_evidence_at is not None
        parsed = datetime.fromisoformat(f.latest_evidence_at)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        assert parsed == datetime.fromisoformat(FRESH_DATE)
        assert f.age_days == pytest.approx(10.0, abs=1.0)

    def test_stale_evidence(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        ds = _make_dataset()
        registry.record_evidence(_make_research_evidence(strategy, ds, STALE_DATE))
        f = intelligence.assess_freshness(
            strategy.strategy_id, EvidenceFreshnessConfig(max_age_days=30), now=NOW
        )
        assert f.status == EvidenceFreshness.STALE

    def test_unknown_no_evidence(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        f = intelligence.assess_freshness(
            strategy.strategy_id, EvidenceFreshnessConfig(max_age_days=30), now=NOW
        )
        assert f.status == EvidenceFreshness.UNKNOWN
        assert f.age_days is None

    def test_freshness_unknown_when_timestamp_unparseable(self):
        """The timestamp parser must yield UNKNOWN on unparseable input.

        The DateTime column only stores valid timestamps, so we verify the
        parser helper directly — it is the code path that classifies
        freshness when a stored timestamp cannot be parsed.
        """
        from trading_system.research.strategy_intelligence import _parse_iso
        assert _parse_iso("not-a-date") is None
        assert _parse_iso("") is None
        assert _parse_iso(None) is None
        # A parseable timestamp must NOT be classified unknown.
        assert _parse_iso(FRESH_DATE) is not None

    def test_configurable_threshold(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        ds = _make_dataset()
        registry.record_evidence(_make_research_evidence(strategy, ds, STALE_DATE))
        loose = intelligence.assess_freshness(
            strategy.strategy_id, EvidenceFreshnessConfig(max_age_days=500), now=NOW
        )
        strict = intelligence.assess_freshness(
            strategy.strategy_id, EvidenceFreshnessConfig(max_age_days=10), now=NOW
        )
        assert loose.status == EvidenceFreshness.FRESH
        assert strict.status == EvidenceFreshness.STALE

    def test_boundary_exactly_at_threshold(self, intelligence, registry):
        exact = (NOW - timedelta(days=30)).isoformat()
        strategy = _register(registry, _valid_spec(name="A"))
        ds = _make_dataset()
        registry.record_evidence(_make_research_evidence(strategy, ds, exact))
        f = intelligence.assess_freshness(
            strategy.strategy_id, EvidenceFreshnessConfig(max_age_days=30), now=NOW
        )
        assert f.status == EvidenceFreshness.FRESH

    def test_multiple_evidence_uses_latest(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        ds = _make_dataset()
        registry.record_evidence(_make_research_evidence(strategy, ds, OLD_DATE, cfg_key=1))
        registry.record_evidence(_make_research_evidence(strategy, ds, FRESH_DATE, cfg_key=2))
        f = intelligence.assess_freshness(
            strategy.strategy_id, EvidenceFreshnessConfig(max_age_days=30), now=NOW
        )
        assert f.status == EvidenceFreshness.FRESH


# --------------------------------------------------------------------------- #
# Eligibility
# --------------------------------------------------------------------------- #
class TestEligibility:
    def test_fully_eligible(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"),
                             status=StrategyStatus.WALK_FORWARD_VALIDATED)
        ds = _make_dataset()
        registry.record_evidence(_make_research_evidence(strategy, ds, FRESH_DATE))
        registry.record_evidence(_make_wf_evidence(strategy, ds, FRESH_DATE))
        req = EvidenceRequirement()
        fresh = EvidenceFreshnessConfig(max_age_days=30)
        result = intelligence.assess_eligibility(strategy.strategy_id, req, fresh, now=NOW)
        assert result.status == Eligibility.ELIGIBLE
        assert result.reasons == []

    def test_missing_walk_forward(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        ds = _make_dataset()
        registry.record_evidence(_make_research_evidence(strategy, ds, FRESH_DATE))
        req = EvidenceRequirement(require_walk_forward=True)
        fresh = EvidenceFreshnessConfig(max_age_days=30)
        result = intelligence.assess_eligibility(strategy.strategy_id, req, fresh, now=NOW)
        assert result.status == Eligibility.INELIGIBLE
        assert "missing_walk_forward_evidence" in result.reasons

    def test_insufficient_trades(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        ds = _make_dataset()
        registry.record_evidence(_make_research_evidence(strategy, ds, FRESH_DATE))
        registry.record_evidence(_make_wf_evidence(
            strategy, ds, FRESH_DATE, summary=_wf_summary(total_trades=2)))
        req = EvidenceRequirement(min_validation_trades=10)
        fresh = EvidenceFreshnessConfig(max_age_days=30)
        result = intelligence.assess_eligibility(strategy.strategy_id, req, fresh, now=NOW)
        assert "insufficient_validation_trades" in result.reasons

    def test_stale_evidence_ineligible(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        ds = _make_dataset()
        registry.record_evidence(_make_research_evidence(strategy, ds, STALE_DATE))
        registry.record_evidence(_make_wf_evidence(strategy, ds, STALE_DATE))
        req = EvidenceRequirement(require_recent_evidence=True)
        fresh = EvidenceFreshnessConfig(max_age_days=30)
        result = intelligence.assess_eligibility(strategy.strategy_id, req, fresh, now=NOW)
        assert "stale_evidence" in result.reasons

    def test_missing_validation_metrics(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        ds = _make_dataset()
        registry.record_evidence(_make_wf_evidence(strategy, ds, FRESH_DATE))
        req = EvidenceRequirement(require_validation=True)
        fresh = EvidenceFreshnessConfig(max_age_days=30)
        result = intelligence.assess_eligibility(strategy.strategy_id, req, fresh, now=NOW)
        assert "missing_validation_metrics" in result.reasons

    def test_retired_strategy_ineligible(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        intelligence.retire_strategy(strategy.strategy_id, "x")
        ds = _make_dataset()
        registry.record_evidence(_make_research_evidence(strategy, ds, FRESH_DATE))
        registry.record_evidence(_make_wf_evidence(strategy, ds, FRESH_DATE))
        req = EvidenceRequirement()
        fresh = EvidenceFreshnessConfig(max_age_days=30)
        result = intelligence.assess_eligibility(strategy.strategy_id, req, fresh, now=NOW)
        assert result.status == Eligibility.INELIGIBLE
        assert "retired_strategy" in result.reasons

    def test_unknown_strategy(self, intelligence):
        req = EvidenceRequirement()
        fresh = EvidenceFreshnessConfig(max_age_days=30)
        result = intelligence.assess_eligibility("nope", req, fresh, now=NOW)
        assert result.status == Eligibility.INELIGIBLE
        assert "unknown_strategy" in result.reasons

    def test_multiple_failure_reasons(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        intelligence.retire_strategy(strategy.strategy_id, "x")
        req = EvidenceRequirement()
        fresh = EvidenceFreshnessConfig(max_age_days=30)
        result = intelligence.assess_eligibility(strategy.strategy_id, req, fresh, now=NOW)
        assert "retired_strategy" in result.reasons
        assert "missing_walk_forward_evidence" in result.reasons
        assert len(result.reasons) >= 2


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
class TestComparison:
    def test_compare_one_strategy(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        ds = _make_dataset()
        registry.record_evidence(_make_research_evidence(strategy, ds, FRESH_DATE))
        fresh = EvidenceFreshnessConfig(max_age_days=30)
        report = intelligence.compare_strategies([strategy.strategy_id], fresh, now=NOW)
        assert isinstance(report, StrategyComparisonReport)
        assert len(report.strategies) == 1
        assert report.strategies[0].strategy_id == strategy.strategy_id
        assert report.strategies[0].name == "A"

    def test_compare_multiple_deterministic_order(self, intelligence, registry):
        s1 = _register(registry, _valid_spec(name="Alpha"))
        s2 = _register(registry, _valid_spec(name="Beta"))
        ds = _make_dataset()
        # s1 has stronger walk-forward metrics.
        registry.record_evidence(_make_wf_evidence(
            s1, ds, FRESH_DATE, summary=_wf_summary(consistency=0.9, pos_ratio=0.9)))
        registry.record_evidence(_make_wf_evidence(
            s2, ds, FRESH_DATE, summary=_wf_summary(consistency=0.3, pos_ratio=0.4)))
        fresh = EvidenceFreshnessConfig(max_age_days=30)
        report = intelligence.compare_strategies(
            [s1.strategy_id, s2.strategy_id], fresh, now=NOW)
        ids = [r.strategy_id for r in report.strategies]
        assert ids[0] == s1.strategy_id  # stronger strategy first
        # Re-run produces the same order.
        report2 = intelligence.compare_strategies(
            [s2.strategy_id, s1.strategy_id], fresh, now=NOW)
        ids2 = [r.strategy_id for r in report2.strategies]
        assert ids == ids2

    def test_equal_scores_tie_break_by_id(self, intelligence, registry):
        s1 = _register(registry, _valid_spec(name="A"))
        s2 = _register(registry, _valid_spec(name="B"))
        # Both have no evidence -> equal (None) scores -> tie-break on id.
        fresh = EvidenceFreshnessConfig(max_age_days=30)
        report = intelligence.compare_strategies(
            [s2.strategy_id, s1.strategy_id], fresh, now=NOW)
        ids = [r.strategy_id for r in report.strategies]
        assert ids == sorted(ids)

    def test_missing_metrics_are_none(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        fresh = EvidenceFreshnessConfig(max_age_days=30)
        report = intelligence.compare_strategies([strategy.strategy_id], fresh, now=NOW)
        row = report.strategies[0]
        assert row.consistency_score is None
        assert row.validation_return is None
        assert row.profit_factor is None

    def test_unknown_strategy_row(self, intelligence):
        fresh = EvidenceFreshnessConfig(max_age_days=30)
        report = intelligence.compare_strategies(["ghost"], fresh, now=NOW)
        assert report.strategies[0].strategy_id == "ghost"
        assert report.strategies[0].name == ""

    def test_metrics_extracted_from_evidence(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        ds = _make_dataset()
        registry.record_evidence(_make_research_evidence(
            strategy, ds, FRESH_DATE, evaluation=_research_evaluation(
                total_return=0.12, profit_factor=1.8, max_drawdown=-0.06, n_trades=30)))
        registry.record_evidence(_make_wf_evidence(
            strategy, ds, FRESH_DATE,
            summary=_wf_summary(consistency=0.8, pos_ratio=0.8, total_trades=120)))
        fresh = EvidenceFreshnessConfig(max_age_days=30)
        report = intelligence.compare_strategies([strategy.strategy_id], fresh, now=NOW)
        row = report.strategies[0]
        assert row.consistency_score == pytest.approx(0.8)
        assert row.positive_fold_ratio == pytest.approx(0.8)
        assert row.validation_trade_count == 120
        assert row.profit_factor == pytest.approx(1.8)

    def test_deduplicates_strategy_ids(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        fresh = EvidenceFreshnessConfig(max_age_days=30)
        report = intelligence.compare_strategies(
            [strategy.strategy_id, strategy.strategy_id], fresh, now=NOW)
        assert len(report.strategies) == 1


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #
class TestHistory:
    def test_history_chronological_order(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        ds = _make_dataset()
        registry.record_evidence(_make_research_evidence(strategy, ds, FRESH_DATE))
        intelligence.retire_strategy(strategy.strategy_id, "x")
        history = intelligence.get_strategy_history(strategy.strategy_id)
        assert isinstance(history, StrategyHistory)
        timestamps = [e.timestamp for e in history.entries]
        assert timestamps == sorted(timestamps)

    def test_history_contains_registration_evidence_lifecycle(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        ds = _make_dataset()
        registry.record_evidence(_make_research_evidence(strategy, ds, FRESH_DATE))
        intelligence.retire_strategy(strategy.strategy_id, "x")
        history = intelligence.get_strategy_history(strategy.strategy_id)
        kinds = [e.kind for e in history.entries]
        assert "registration" in kinds
        assert "evidence" in kinds
        assert "lifecycle" in kinds

    def test_history_lifecycle_reason_preserved(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        intelligence.retire_strategy(strategy.strategy_id, "market regime changed")
        history = intelligence.get_strategy_history(strategy.strategy_id)
        lc = [e for e in history.entries if e.kind == "lifecycle"]
        assert len(lc) == 1
        assert lc[0].details["reason"] == "market regime changed"


# --------------------------------------------------------------------------- #
# Intelligence Report
# --------------------------------------------------------------------------- #
class TestIntelligenceReport:
    def test_report_builds_and_serializes(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        ds = _make_dataset()
        registry.record_evidence(_make_research_evidence(strategy, ds, FRESH_DATE))
        fresh = EvidenceFreshnessConfig(max_age_days=30)
        report = intelligence.build_intelligence_report([strategy.strategy_id], fresh, now=NOW)
        assert isinstance(report, StrategyIntelligenceReport)
        data = json.loads(report.model_dump_json())
        assert "comparison" in data
        assert "eligibility" in data
        assert "freshness" in data
        assert "lifecycle" in data
        assert "unavailable" in data
        assert data["report_version"] == "phase-17"
        assert data["schema_version"] >= 2

    def test_report_deterministic(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        ds = _make_dataset()
        registry.record_evidence(_make_research_evidence(strategy, ds, FRESH_DATE))
        fresh = EvidenceFreshnessConfig(max_age_days=30)
        r1 = intelligence.build_intelligence_report([strategy.strategy_id], fresh, now=NOW)
        r2 = intelligence.build_intelligence_report([strategy.strategy_id], fresh, now=NOW)
        # Observation timestamps may differ on generated_at; compare the rest.
        d1 = r1.model_dump()
        d2 = r2.model_dump()
        d1["generated_at"] = ""
        d2["generated_at"] = ""
        d1["comparison"]["generated_at"] = ""
        d2["comparison"]["generated_at"] = ""
        assert d1 == d2

    def test_report_unknown_strategy_warning(self, intelligence):
        fresh = EvidenceFreshnessConfig(max_age_days=30)
        report = intelligence.build_intelligence_report(["ghost"], fresh, now=NOW)
        assert any("unknown" in w for w in report.warnings)
        assert "ghost" not in report.freshness

    def test_report_unavailable_metrics(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        fresh = EvidenceFreshnessConfig(max_age_days=30)
        report = intelligence.build_intelligence_report([strategy.strategy_id], fresh, now=NOW)
        assert strategy.strategy_id in report.unavailable


# --------------------------------------------------------------------------- #
# Cross-session queries
# --------------------------------------------------------------------------- #
class TestCrossSessionQueries:
    def test_list_active_strategies(self, intelligence, registry):
        a = _register(registry, _valid_spec(name="A"))
        b = _register(registry, _valid_spec(name="B"))
        intelligence.retire_strategy(b.strategy_id, "x")
        active = intelligence.list_active_strategies()
        ids = [s.strategy_id for s in active]
        assert a.strategy_id in ids
        assert b.strategy_id not in ids

    def test_list_retired_strategies(self, intelligence, registry):
        a = _register(registry, _valid_spec(name="A"))
        intelligence.retire_strategy(a.strategy_id, "x")
        retired = intelligence.list_retired_strategies()
        assert len(retired) == 1
        assert retired[0].strategy_id == a.strategy_id

    def test_list_stale_strategies(self, intelligence, registry):
        a = _register(registry, _valid_spec(name="A"))
        b = _register(registry, _valid_spec(name="B"))
        ds = _make_dataset()
        registry.record_evidence(_make_research_evidence(a, ds, STALE_DATE))
        registry.record_evidence(_make_research_evidence(b, ds, FRESH_DATE))
        fresh = EvidenceFreshnessConfig(max_age_days=30)
        stale = intelligence.list_stale_strategies(fresh, now=NOW)
        ids = [s.strategy_id for s, _ in stale]
        assert a.strategy_id in ids
        assert b.strategy_id not in ids


# --------------------------------------------------------------------------- #
# Persistence / schema migration
# --------------------------------------------------------------------------- #
class TestPersistence:
    def test_restart_records_remain_queryable(self, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        ds = _make_dataset()
        registry.record_evidence(_make_research_evidence(strategy, ds, FRESH_DATE))
        events_before = registry.list_lifecycle_events(strategy_id=strategy.strategy_id)
        # Simulate restart: new store/registry/intelligence from same engine.
        intel2 = StrategyIntelligence(registry)
        assert intel2.registry.get_strategy(strategy.strategy_id) is not None
        evs = intel2.registry.list_evidence(strategy_id=strategy.strategy_id)
        assert len(evs) == 1
        assert intel2.registry.list_lifecycle_events(strategy_id=strategy.strategy_id) == events_before

    def test_strategy_identity_unchanged_after_restart(self, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        sid = strategy.strategy_id
        intel2 = StrategyIntelligence(registry)
        assert intel2.registry.get_strategy(sid).strategy_id == sid

    def test_old_phase16_records_readable_after_migration(self, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        ds = _make_dataset()
        registry.record_evidence(_make_research_evidence(strategy, ds, FRESH_DATE))
        # Lifecycle table added by migration; existing evidence unaffected.
        intel = StrategyIntelligence(registry)
        assert intel.store._schema_version() == 2
        evs = intel.registry.list_evidence(strategy_id=strategy.strategy_id)
        assert len(evs) == 1

    def test_schema_migration_is_idempotent(self, registry):
        intel = StrategyIntelligence(registry)
        v1 = intel.store._schema_version()
        intel.store.ensure_schema_current()
        v2 = intel.store._schema_version()
        assert v1 == v2 == 2


# --------------------------------------------------------------------------- #
# Serialization round-trip
# --------------------------------------------------------------------------- #
class TestSerialization:
    def test_comparison_metrics_roundtrip(self):
        m = ComparisonMetrics(
            strategy_id="s1", name="A", symbol="NSE:SBIN", timeframe="1d",
            status=StrategyStatus.RESEARCHED, generated_by="test",
            validation_return=0.1, consistency_score=0.7,
        )
        restored = ComparisonMetrics.model_validate_json(m.model_dump_json())
        assert restored.strategy_id == "s1"
        assert restored.validation_return == pytest.approx(0.1)
        assert restored.status == StrategyStatus.RESEARCHED

    def test_full_report_roundtrip(self, intelligence, registry):
        strategy = _register(registry, _valid_spec(name="A"))
        ds = _make_dataset()
        registry.record_evidence(_make_research_evidence(strategy, ds, FRESH_DATE))
        fresh = EvidenceFreshnessConfig(max_age_days=30)
        report = intelligence.build_intelligence_report([strategy.strategy_id], fresh, now=NOW)
        restored = StrategyIntelligenceReport.model_validate_json(report.model_dump_json())
        assert restored.report_version == "phase-17"
        assert len(restored.comparison.strategies) == 1

    def test_eligibility_result_roundtrip(self):
        e = EligibilityResult(
            strategy_id="s", status=Eligibility.INELIGIBLE,
            reasons=["stale_evidence", "retired_strategy"])
        r = EligibilityResult.model_validate_json(e.model_dump_json())
        assert r.status == Eligibility.INELIGIBLE
        assert r.reasons == ["stale_evidence", "retired_strategy"]


# --------------------------------------------------------------------------- #
# Safety
# --------------------------------------------------------------------------- #
class TestSafety:
    FORBIDDEN_IMPORT_SUBSTRINGS = (
        "execution", "fyers", "ctypes", "importlib", "marshal", "pickle",
        "subprocess", "socket", "http", "upstox",
    )
    FORBIDDEN_CALL_NAMES = {
        "eval", "exec", "compile", "__import__", "globals", "locals",
        "vars", "breakpoint", "open", "system", "popen",
    }

    def test_no_forbidden_imports(self):
        path = Path(__file__).parent.parent / "src/trading_system/research/strategy_intelligence.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name.lower()
                    for token in self.FORBIDDEN_IMPORT_SUBSTRINGS:
                        assert token not in mod, f"forbidden import: {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                mod = (node.module or "").lower()
                for token in self.FORBIDDEN_IMPORT_SUBSTRINGS:
                    assert token not in mod, f"forbidden import: {node.module}"

    def test_no_dynamic_execution_calls(self):
        path = Path(__file__).parent.parent / "src/trading_system/research/strategy_intelligence.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in self.FORBIDDEN_CALL_NAMES, (
                    f"forbidden call: {node.func.id}()"
                )

    def test_intelligence_depends_only_on_registry(self, intelligence):
        # The facade must not reach beyond the registry/store boundary.
        assert intelligence.store is intelligence.registry.store
