"""Phase 16 tests: Strategy Registry + Evidence Store integration.

Covers:
  * deterministic strategy identity
  * deterministic dataset identity
  * deterministic evidence identity
  * strategy registration, retrieval, listing
  * idempotency (duplicate strategy/evidence)
  * versioning (modified spec -> new identity)
  * evidence recording and history
  * ResearchReport persistence
  * WalkForwardReport persistence
  * AI provenance preservation
  * corruption rejection (malformed JSON, invalid spec)
  * safety (no forbidden imports/calls)
"""
from __future__ import annotations

import ast
import json
import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine

from trading_system.models.base import ModelProviderError
from trading_system.research.backtester import BacktestConfig
from trading_system.research.dataset import HistoricalDataset
from trading_system.research.evidence import (
    EvidenceStore,
    EvidenceType,
    Hypothesis,
    HypothesisStatus,
    Strategy,
    StrategyEvidence,
    StrategyStatus,
    dataset_identity,
    strategy_identity,
)
from trading_system.research.strategy_registry import (
    StrategyRegistry,
    evidence_identity,
    serialize_evaluation,
    serialize_walk_forward_report,
)
from trading_system.research.strategy_registry import (
    StrategyRegistry,
    evidence_identity,
    serialize_evaluation,
    serialize_walk_forward_report,
)
from trading_system.research.strategy_lab.ai_walk_forward import (
    AIWalkForwardConfig,
    FoldProvenance,
    build_generation_context,
    walk_forward_ai_research,
)
from trading_system.research.strategy_lab.engine import ResearchConfig
from trading_system.research.strategy_lab.evaluation import StrategyEvaluation
from trading_system.research.strategy_lab.filters import QualityFilterConfig
from trading_system.research.strategy_lab.providers import (
    DeterministicStrategyProvider,
    GenerationContext,
    StrategyProposalProvider,
)
from trading_system.research.strategy_lab.spec import (
    StrategySpec,
    const_operand,
    field_operand,
    indicator_operand,
    make_condition,
)
from trading_system.research.strategy_lab.walk_forward import WalkForwardConfig
from trading_system.research.strategy_registry import (
    deserialize_strategy_spec,
    serialize_research_report,
    serialize_walk_forward_report,
    serialize_fold_provenance,
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


def _make_dataset(n=200, symbol="NSE:SBIN", seed=42):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    close = 100 + np.cumsum(rng.normal(0, 1, n))
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


# --------------------------------------------------------------------------- #
# Strategy identity
# --------------------------------------------------------------------------- #
class TestStrategyIdentity:
    def test_same_spec_same_id(self):
        spec = _valid_spec()
        assert strategy_identity(spec) == strategy_identity(spec)

    def test_reordered_json_same_id(self):
        spec = _valid_spec()
        id1 = strategy_identity(spec)
        # Reorder by reconstructing from a dict with different key order.
        data = spec.model_dump(mode="python")
        reordered = json.loads(json.dumps(data, sort_keys=True))
        spec2 = StrategySpec(**reordered)
        id2 = strategy_identity(spec2)
        assert id1 == id2

    def test_changed_parameter_different_id(self):
        spec1 = _valid_spec()
        spec2 = _valid_spec(indicators=[{"name": "sma", "params": {"window": 30}}])
        assert strategy_identity(spec1) != strategy_identity(spec2)

    def test_changed_entry_different_id(self):
        spec1 = _valid_spec()
        spec2 = _valid_spec(
            entry=make_condition(
                field_operand("close"), "<", indicator_operand("sma_20")
            )
        )
        assert strategy_identity(spec1) != strategy_identity(spec2)

    def test_identity_is_deterministic_across_processes(self):
        spec = _valid_spec()
        expected = strategy_identity(spec)
        # Re-create spec from JSON round-trip to simulate process boundary.
        payload = json.loads(spec.to_json())
        restored = StrategySpec(**payload)
        assert strategy_identity(restored) == expected


# --------------------------------------------------------------------------- #
# Dataset identity
# --------------------------------------------------------------------------- #
class TestDatasetIdentity:
    def test_same_dataset_same_id(self):
        ds = _make_dataset()
        assert dataset_identity(ds) == dataset_identity(ds)

    def test_different_data_different_id(self):
        ds1 = _make_dataset(symbol="NSE:SBIN", seed=1)
        ds2 = _make_dataset(symbol="NSE:TCS", seed=2)
        assert dataset_identity(ds1) != dataset_identity(ds2)

    def test_same_metadata_same_id(self):
        df = _make_dataset().data
        ds1 = HistoricalDataset(symbol="NSE:SBIN", timeframe="1d", data=df)
        ds2 = HistoricalDataset(symbol="NSE:SBIN", timeframe="1d", data=df.copy())
        assert dataset_identity(ds1) == dataset_identity(ds2)


# --------------------------------------------------------------------------- #
# Evidence identity
# --------------------------------------------------------------------------- #
class TestEvidenceIdentity:
    def test_deterministic(self):
        eid = evidence_identity("s1", EvidenceType.BACKTEST, "d1", {"k": 1})
        assert eid == evidence_identity("s1", EvidenceType.BACKTEST, "d1", {"k": 1})

    def test_changes_with_content(self):
        eid1 = evidence_identity("s1", EvidenceType.BACKTEST, "d1", {"k": 1})
        eid2 = evidence_identity("s1", EvidenceType.BACKTEST, "d1", {"k": 2})
        assert eid1 != eid2


# --------------------------------------------------------------------------- #
# StrategyRegistry — strategy lifecycle
# --------------------------------------------------------------------------- #
class TestStrategyRegistry:
    def test_register_and_get_strategy(self, registry):
        spec = _valid_spec()
        strategy = registry.register_strategy(spec)
        assert strategy.strategy_id == strategy_identity(spec)
        got = registry.get_strategy(strategy.strategy_id)
        assert got is not None
        assert got.name == "Test strategy"
        assert got.symbol == "NSE:SBIN"

    def test_register_same_spec_twice_is_idempotent(self, registry):
        spec = _valid_spec()
        s1 = registry.register_strategy(spec)
        s2 = registry.register_strategy(spec)
        assert s1.strategy_id == s2.strategy_id
        assert s1.spec_json == s2.spec_json

    def test_register_different_spec_creates_new_identity(self, registry):
        spec1 = _valid_spec()
        spec2 = _valid_spec(indicators=[{"name": "sma", "params": {"window": 30}}])
        s1 = registry.register_strategy(spec1)
        s2 = registry.register_strategy(spec2)
        assert s1.strategy_id != s2.strategy_id

    def test_get_strategy_by_spec(self, registry):
        spec = _valid_spec()
        registry.register_strategy(spec)
        got = registry.get_strategy_by_spec(spec)
        assert got is not None
        assert got.strategy_id == strategy_identity(spec)

    def test_list_strategies_filters(self, registry):
        spec1 = _valid_spec(symbol="NSE:SBIN")
        spec2 = _valid_spec(symbol="NSE:TCS")
        registry.register_strategy(spec1)
        registry.register_strategy(spec2)
        sbin = registry.list_strategies(symbol="NSE:SBIN")
        assert len(sbin) == 1
        assert sbin[0].symbol == "NSE:SBIN"

    def test_update_strategy_status(self, registry):
        spec = _valid_spec()
        strategy = registry.register_strategy(spec)
        registry.update_strategy_status(strategy.strategy_id, StrategyStatus.RESEARCHED)
        updated = registry.get_strategy(strategy.strategy_id)
        assert updated.status == StrategyStatus.RESEARCHED

    def test_register_same_id_different_spec_raises(self, registry):
        spec1 = _valid_spec()
        strategy = registry.register_strategy(spec1)
        # Directly create a Strategy record with the same ID but different spec.
        bad = Strategy(
            strategy_id=strategy.strategy_id,
            name="Changed",
            symbol="NSE:SBIN",
            timeframe="1d",
            spec_json=_valid_spec(name="Changed").to_json(),
            spec_hash=strategy_identity(_valid_spec(name="Changed")),
            generated_by="test",
        )
        with pytest.raises(ValueError, match="different spec"):
            registry.store.register_strategy(bad)


# --------------------------------------------------------------------------- #
# Versioning
# --------------------------------------------------------------------------- #
class TestVersioning:
    def test_old_spec_remains_immutable(self, registry):
        spec_v1 = _valid_spec(name="v1")
        s1 = registry.register_strategy(spec_v1)
        spec_v2 = _valid_spec(name="v2")
        s2 = registry.register_strategy(spec_v2)
        assert s1.strategy_id != s2.strategy_id
        assert registry.get_strategy(s1.strategy_id).name == "v1"
        assert registry.get_strategy(s2.strategy_id).name == "v2"

    def test_new_evidence_does_not_rewrite_old(self, registry):
        spec = _valid_spec()
        strategy = registry.register_strategy(spec)
        ds = _make_dataset()
        ds_id = dataset_identity(ds)
        ev1 = StrategyEvidence(
            evidence_id=evidence_identity(strategy.strategy_id, EvidenceType.BACKTEST, ds_id, {"v": 1}),
            strategy_id=strategy.strategy_id,
            strategy_spec_hash=strategy.spec_hash,
            evidence_type=EvidenceType.BACKTEST,
            dataset_id=ds_id,
            configuration_json={"version": 1},
            metrics_json={"total_return": 0.1},
        )
        ev2 = StrategyEvidence(
            evidence_id=evidence_identity(strategy.strategy_id, EvidenceType.BACKTEST, ds_id, {"v": 2}),
            strategy_id=strategy.strategy_id,
            strategy_spec_hash=strategy.spec_hash,
            evidence_type=EvidenceType.BACKTEST,
            dataset_id=ds_id,
            configuration_json={"version": 2},
            metrics_json={"total_return": 0.2},
        )
        registry.record_evidence(ev1)
        registry.record_evidence(ev2)
        history = registry.get_strategy_history(strategy.strategy_id)
        assert len(history) == 2
        assert history[0].configuration_json["version"] == 2
        assert history[1].configuration_json["version"] == 1


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #
class TestEvidence:
    def test_record_and_retrieve_evidence(self, registry):
        spec = _valid_spec()
        strategy = registry.register_strategy(spec)
        ds = _make_dataset()
        ds_id = dataset_identity(ds)
        ev = StrategyEvidence(
            evidence_id=evidence_identity(strategy.strategy_id, EvidenceType.BACKTEST, ds_id, {"k": 1}),
            strategy_id=strategy.strategy_id,
            strategy_spec_hash=strategy.spec_hash,
            evidence_type=EvidenceType.BACKTEST,
            dataset_id=ds_id,
            configuration_json={"k": 1},
            metrics_json={"total_return": 0.1},
        )
        registry.record_evidence(ev)
        got = registry.get_evidence(ev.evidence_id)
        assert got is not None
        assert got.metrics_json["total_return"] == 0.1

    def test_duplicate_evidence_is_idempotent(self, registry):
        spec = _valid_spec()
        strategy = registry.register_strategy(spec)
        ds = _make_dataset()
        ds_id = dataset_identity(ds)
        ev = StrategyEvidence(
            evidence_id=evidence_identity(strategy.strategy_id, EvidenceType.BACKTEST, ds_id, {"k": 1}),
            strategy_id=strategy.strategy_id,
            strategy_spec_hash=strategy.spec_hash,
            evidence_type=EvidenceType.BACKTEST,
            dataset_id=ds_id,
            configuration_json={"k": 1},
            metrics_json={"total_return": 0.1},
        )
        registry.record_evidence(ev)
        registry.record_evidence(ev)
        history = registry.list_evidence(strategy_id=strategy.strategy_id)
        assert len(history) == 1

    def test_list_strategy_evidence_by_type(self, registry):
        spec = _valid_spec()
        strategy = registry.register_strategy(spec)
        ds = _make_dataset()
        ds_id = dataset_identity(ds)
        for ev_type in [EvidenceType.BACKTEST, EvidenceType.RESEARCH]:
            ev = StrategyEvidence(
                evidence_id=evidence_identity(strategy.strategy_id, ev_type, ds_id, {"k": 1}),
                strategy_id=strategy.strategy_id,
                strategy_spec_hash=strategy.spec_hash,
                evidence_type=ev_type,
                dataset_id=ds_id,
                configuration_json={"k": 1},
            )
            registry.record_evidence(ev)
        bt = registry.list_evidence(strategy_id=strategy.strategy_id, evidence_type="backtest")
        assert len(bt) == 1
        assert bt[0].evidence_type == EvidenceType.BACKTEST

    def test_get_latest_evidence(self, registry):
        spec = _valid_spec()
        strategy = registry.register_strategy(spec)
        ds = _make_dataset()
        ds_id = dataset_identity(ds)
        old = StrategyEvidence(
            evidence_id=evidence_identity(strategy.strategy_id, EvidenceType.BACKTEST, ds_id, {"k": 1}),
            strategy_id=strategy.strategy_id,
            strategy_spec_hash=strategy.spec_hash,
            evidence_type=EvidenceType.BACKTEST,
            dataset_id=ds_id,
            created_at="2020-01-01T00:00:00+00:00",
        )
        new = StrategyEvidence(
            evidence_id=evidence_identity(strategy.strategy_id, EvidenceType.BACKTEST, ds_id, {"k": 2}),
            strategy_id=strategy.strategy_id,
            strategy_spec_hash=strategy.spec_hash,
            evidence_type=EvidenceType.BACKTEST,
            dataset_id=ds_id,
            created_at="2025-01-01T00:00:00+00:00",
        )
        registry.record_evidence(old)
        registry.record_evidence(new)
        latest = registry.get_latest_evidence(strategy.strategy_id)
        assert latest.evidence_id == new.evidence_id


# --------------------------------------------------------------------------- #
# ResearchReport persistence
# --------------------------------------------------------------------------- #
class TestResearchReportPersistence:
    def test_persist_and_retrieve_research_report(self, registry):
        from trading_system.research.strategy_lab.engine import StrategyResearchEngine

        ds = _make_dataset(n=400)
        provider = DeterministicStrategyProvider()
        engine = StrategyResearchEngine(
            provider,
            ResearchConfig(
                max_candidates=2,
                quality_filter=QualityFilterConfig(min_trades=1, require_reliable=False),
            ),
        )
        ctx = GenerationContext(symbol="NSE:SBIN", timeframe="1d")
        report = engine.research_candidates(ds, ctx, 2, BacktestConfig())

        evidence = registry.persist_research_report(
            report=report,
            dataset=ds,
            provider_name=provider.name,
            research_config=engine.config,
            backtest_config=BacktestConfig(),
            spec=report.passed[0].spec if report.passed else None,
        )
        assert evidence.strategy_id is not None
        assert evidence.evidence_type == EvidenceType.RESEARCH
        got = registry.get_evidence(evidence.evidence_id)
        assert got is not None
        assert "candidates" in got.metrics_json

    def test_persist_without_spec_raises(self, registry):
        from trading_system.research.strategy_lab.engine import StrategyResearchEngine

        class _NoPassProvider(StrategyProposalProvider):
            name = "no-pass"
            def generate_strategy(self, context):
                return _valid_spec(symbol="NSE:OTHER")

        ds = _make_dataset()
        engine = StrategyResearchEngine(_NoPassProvider(), ResearchConfig())
        report = engine.research_candidates(ds, GenerationContext(symbol="NSE:SBIN", timeframe="1d"), 1, BacktestConfig())
        with pytest.raises(ValueError):
            registry.persist_research_report(
                report=report,
                dataset=ds,
                provider_name="test",
                research_config=engine.config,
                backtest_config=BacktestConfig(),
            )


# --------------------------------------------------------------------------- #
# WalkForwardReport persistence
# --------------------------------------------------------------------------- #
class TestWalkForwardPersistence:
    def test_persist_walk_forward_report(self, registry):
        ds = _make_dataset(n=500)
        provider = DeterministicStrategyProvider()
        wf_config = WalkForwardConfig(
            n_folds=2, train_window=100, validation_window=50,
            step_size=50, mode="rolling", warmup_bars=20,
        )
        research_config = ResearchConfig(
            max_candidates=2,
            quality_filter=QualityFilterConfig(min_trades=1, require_reliable=False),
        )
        backtest_config = BacktestConfig()
        ai_config = AIWalkForwardConfig(candidate_count=2, min_train_bars=50)

        report = walk_forward_ai_research(
            dataset=ds,
            provider=provider,
            wf_config=wf_config,
            research_config=research_config,
            backtest_config=backtest_config,
            ai_config=ai_config,
        )

        evidences = registry.persist_walk_forward_report(
            report=report,
            dataset=ds,
            wf_config=wf_config,
            backtest_config=backtest_config,
        )
        assert len(evidences) >= 1
        # At least one per-fold evidence + one summary
        fold_evidences = [e for e in evidences if e.fold_id is not None]
        assert len(fold_evidences) == len([f for f in report.folds if f.selected_spec is not None])
        for ev in evidences:
            assert ev.evidence_type == EvidenceType.WALK_FORWARD
            got = registry.get_evidence(ev.evidence_id)
            assert got is not None


# --------------------------------------------------------------------------- #
# AI provenance
# --------------------------------------------------------------------------- #
class TestAIProvenance:
    def test_fold_provenance_serialization(self):
        prov = FoldProvenance(
            provider_name="deterministic-mock",
            train_rows=100,
            train_start="2024-01-01",
            train_end="2024-04-09",
            candidate_count=2,
            valid_candidate_count=1,
            selected_spec_name="SMA20 trend filter",
            generation_status="completed",
        )
        d = serialize_fold_provenance(prov)
        assert d["provider_name"] == "deterministic-mock"
        assert d["train_rows"] == 100
        assert d["generation_status"] == "completed"

    def test_train_boundary_preserved_in_evidence(self, registry):
        ds = _make_dataset(n=500)
        provider = DeterministicStrategyProvider()
        wf_config = WalkForwardConfig(
            n_folds=1, train_window=100, validation_window=50,
            step_size=50, mode="rolling", warmup_bars=20,
        )
        research_config = ResearchConfig(
            max_candidates=2,
            quality_filter=QualityFilterConfig(min_trades=1, require_reliable=False),
        )
        backtest_config = BacktestConfig()
        ai_config = AIWalkForwardConfig(candidate_count=2, min_train_bars=50)

        report = walk_forward_ai_research(
            dataset=ds, provider=provider, wf_config=wf_config,
            research_config=research_config, backtest_config=backtest_config,
            ai_config=ai_config,
        )
        evidences = registry.persist_walk_forward_report(
            report=report, dataset=ds, wf_config=wf_config, backtest_config=backtest_config,
        )
        for ev in evidences:
            if ev.fold_id is not None:
                assert ev.train_start is not None
                assert ev.train_end is not None
                # train_end must be before validation_start (chronology)
                assert ev.train_end < ev.validation_start


# --------------------------------------------------------------------------- #
# Corruption / safety
# --------------------------------------------------------------------------- #
class TestCorruptionSafety:
    def test_invalid_spec_rejected_by_choke_point(self, registry):
        with pytest.raises(Exception):
            registry.register_strategy(_valid_spec(name=""))  # empty name fails validation

    def test_malformed_json_rejected_on_load(self):
        with pytest.raises(Exception):
            deserialize_strategy_spec('{"name": "x", "bogus": 1}')

    def test_no_forbidden_imports_in_strategy_registry(self):
        path = Path(__file__).parent.parent / "src/trading_system/research/strategy_registry.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        forbidden = {"execution", "broker", "fyers", "upstox", "subprocess", "socket", "pickle", "marshal", "ctypes"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = (node.module or "").lower()
                for token in forbidden:
                    assert token not in mod, f"forbidden import: {mod}"

    def test_no_dynamic_execution_in_strategy_registry(self):
        path = Path(__file__).parent.parent / "src/trading_system/research/strategy_registry.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        forbidden_calls = {"eval", "exec", "compile", "__import__", "globals", "locals", "open", "system", "popen"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls, f"forbidden call: {node.func.id}()"


# --------------------------------------------------------------------------- #
# Schema versioning
# --------------------------------------------------------------------------- #
class TestSchemaVersioning:
    def test_initial_version_is_one(self, store):
        assert store._schema_version() == 1

    def test_version_can_be_bumped(self, store):
        store._set_schema_version(2)
        assert store._schema_version() == 2
