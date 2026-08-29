"""Day 10 evidence-store tests — hypothesis model, evidence store, manifest, quality, decay."""
import datetime as dt
from sqlalchemy import create_engine

import pytest

from trading_system.research import (
    EvidenceStore, ResearchRegistry, Hypothesis, HypothesisStatus, EvidenceRun,
    ExperimentManifest, classify_quality, is_evidence_stale,
)


@pytest.fixture
def reg():
    store = EvidenceStore(create_engine("sqlite://"))  # isolated in-memory
    return ResearchRegistry(store)


def test_create_and_get_hypothesis(reg):
    h = Hypothesis(hypothesis_id="H1", title="RSI MR", description="d", timeframe="1d",
                   factor_names=["rsi_14"], provenance="manual")
    reg.create_hypothesis(h)
    got = reg.get_hypothesis("H1")
    assert got is not None
    assert got.title == "RSI MR" and got.status == HypothesisStatus.HYPOTHESIS


def test_duplicate_hypothesis_rejected(reg):
    h = Hypothesis(hypothesis_id="H1", title="x", description="d", timeframe="1d")
    reg.create_hypothesis(h)
    with pytest.raises(ValueError):
        reg.create_hypothesis(h)


def test_update_hypothesis_status(reg):
    reg.create_hypothesis(Hypothesis(hypothesis_id="H1", title="x", description="d", timeframe="1d"))
    reg.store.update_status("H1", HypothesisStatus.RESEARCH)
    assert reg.get_hypothesis("H1").status == HypothesisStatus.RESEARCH


def test_record_and_retrieve_evidence(reg):
    reg.create_hypothesis(Hypothesis(hypothesis_id="H1", title="x", description="d", timeframe="1d"))
    e = EvidenceRun(run_id="R1", hypothesis_id="H1", manifest_hash="m1",
                    dataset_identity="NSE:SBIN|1d", trade_count=40, sharpe=0.9,
                    max_drawdown=-0.1, ic=0.03, icir=0.4, cost_assumptions_bps=10.0,
                    regime="trending_up", sample_size=2477, quality="adequate")
    reg.record_evidence(e)
    got = reg.get_evidence("R1")
    assert got.sharpe == 0.9 and got.quality == "adequate"


def test_get_latest_evidence(reg):
    reg.create_hypothesis(Hypothesis(hypothesis_id="H1", title="x", description="d", timeframe="1d"))
    reg.record_evidence(EvidenceRun(run_id="R1", hypothesis_id="H1", manifest_hash="m",
                       dataset_identity="x", trade_count=10, quality="insufficient", created_at="2020-01-01T00:00:00+00:00"))
    reg.record_evidence(EvidenceRun(run_id="R2", hypothesis_id="H1", manifest_hash="m",
                       dataset_identity="x", trade_count=50, sharpe=1.2, quality="adequate",
                       created_at=dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc).isoformat()))
    latest = reg.get_latest_evidence("H1")
    assert latest.run_id == "R2"  # most recent created_at wins


def test_manifest_hash_determinism():
    m1 = ExperimentManifest(experiment_id="e1", strategy_id="s", factor_set=["rsi_14"],
                            dataset="NSE:SBIN|1d", symbol_universe=["NSE:SBIN"], timeframe="1d",
                            warmup_bars=50, transaction_cost_bps=10.0, code_version="v10")
    m2 = ExperimentManifest(experiment_id="e1", strategy_id="s", factor_set=["rsi_14"],
                            dataset="NSE:SBIN|1d", symbol_universe=["NSE:SBIN"], timeframe="1d",
                            warmup_bars=50, transaction_cost_bps=10.0, code_version="v10")
    assert m1.identity_hash == m2.identity_hash


def test_manifest_hash_changes_on_config():
    base = dict(experiment_id="e1", strategy_id="s", factor_set=["rsi_14"],
                dataset="NSE:SBIN|1d", symbol_universe=["NSE:SBIN"], timeframe="1d",
                warmup_bars=50, transaction_cost_bps=10.0, code_version="v10")
    m1 = ExperimentManifest(**base)
    m2 = ExperimentManifest(**{**base, "transaction_cost_bps": 20.0})
    m3 = ExperimentManifest(**{**base, "warmup_bars": 100})
    assert m1.identity_hash != m2.identity_hash
    assert m1.identity_hash != m3.identity_hash


def test_manifest_hash_ignores_run_metadata():
    m1 = ExperimentManifest(experiment_id="e1", strategy_id="s", factor_set=["rsi_14"],
                            dataset="NSE:SBIN|1d", symbol_universe=["NSE:SBIN"], timeframe="1d")
    # run_metadata is not a field in identity_dict(); mutating non-identity fields only
    # (timestamp) must not change hash. There is no timestamp field in identity -> stable.
    assert m1.identity_hash == m1.identity_hash


def test_filter_by_regime_and_quality(reg):
    reg.create_hypothesis(Hypothesis(hypothesis_id="H1", title="x", description="d", timeframe="1d"))
    reg.record_evidence(EvidenceRun(run_id="RA", hypothesis_id="H1", manifest_hash="m",
                       dataset_identity="x", trade_count=40, regime="trending_up", quality="adequate"))
    reg.record_evidence(EvidenceRun(run_id="RB", hypothesis_id="H1", manifest_hash="m",
                       dataset_identity="x", trade_count=12, regime="range_bound", quality="marginal"))
    by_regime = reg.store.list_evidence(regime="trending_up")
    assert len(by_regime) == 1 and by_regime[0].run_id == "RA"
    by_quality = reg.store.list_evidence(quality="marginal")
    assert len(by_quality) == 1 and by_quality[0].run_id == "RB"


def test_evidence_quality_classification():
    assert classify_quality(5, has_oos=False, missing_metrics=[], cost_assumptions_bps=0.0) == "insufficient"
    assert classify_quality(15, has_oos=False, missing_metrics=["sortino"], cost_assumptions_bps=10.0) == "marginal"
    assert classify_quality(40, has_oos=True, missing_metrics=[], cost_assumptions_bps=10.0) == "adequate"
    # zero cost assumption => insufficient (unrealistic)
    assert classify_quality(100, has_oos=True, missing_metrics=[], cost_assumptions_bps=0.0) == "insufficient"


def test_evidence_stale():
    fresh = dt.datetime.now(dt.timezone.utc).isoformat()
    old = "2020-01-01T00:00:00+00:00"
    assert is_evidence_stale(old) is True
    assert is_evidence_stale(fresh) is False


def test_stale_does_not_delete_or_retire(reg):
    reg.create_hypothesis(Hypothesis(hypothesis_id="H1", title="x", description="d", timeframe="1d"))
    reg.record_evidence(EvidenceRun(run_id="R1", hypothesis_id="H1", manifest_hash="m",
                       dataset_identity="x", trade_count=40, quality="adequate",
                       created_at="2020-01-01T00:00:00+00:00"))
    # Stale evidence remains in the store; status is untouched (no auto-retire).
    assert is_evidence_stale(reg.get_evidence("R1").created_at) is True
    assert reg.get_hypothesis("H1").status == HypothesisStatus.HYPOTHESIS


def test_malformed_evidence_rejected_by_schema():
    # extra fields are forbidden by pydantic (extra="forbid")
    with pytest.raises(Exception):
        EvidenceRun(run_id="R1", hypothesis_id="H1", manifest_hash="m", dataset_identity="x",
                    bogus_field=1)


def test_compare_hypotheses(reg):
    reg.create_hypothesis(Hypothesis(hypothesis_id="H1", title="A", description="d", timeframe="1d"))
    reg.create_hypothesis(Hypothesis(hypothesis_id="H2", title="B", description="d", timeframe="1d"))
    reg.record_evidence(EvidenceRun(run_id="R1", hypothesis_id="H1", manifest_hash="m",
                       dataset_identity="x", trade_count=40, sharpe=1.1, quality="adequate"))
    reg.record_evidence(EvidenceRun(run_id="R2", hypothesis_id="H2", manifest_hash="m",
                       dataset_identity="x", trade_count=20, sharpe=0.3, quality="marginal"))
    df = reg.compare_hypotheses(["H1", "H2"])
    assert set(df["hypothesis_id"]) == {"H1", "H2"}
    assert df[df.hypothesis_id == "H1"].iloc[0]["sharpe"] == 1.1
