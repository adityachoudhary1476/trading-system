"""Phase 18 — Deployment Gate tests.

Covers:
  * eligible strategy deploys
  * unknown / retired / rejected strategy rejected
  * missing / stale / insufficient evidence rejected
  * symbol / timeframe mismatch rejected
  * invalid risk config rejected
  * paper-only execution_mode enforced
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine

from trading_system.research.dataset import HistoricalDataset
from trading_system.research.evidence import (
    EvidenceStore,
    EvidenceType,
    LifecycleEvent,
    Strategy,
    StrategyEvidence,
    StrategyStatus,
    dataset_identity,
)
from trading_system.research.strategy_intelligence import (
    EvidenceFreshnessConfig,
    EvidenceRequirement,
    StrategyIntelligence,
)
from trading_system.research.strategy_lab.spec import (
    StrategySpec,
    field_operand,
    indicator_operand,
    make_condition,
)
from trading_system.research.strategy_registry import StrategyRegistry, evidence_identity

from trading_system.paper import (
    DeploymentGate,
    PaperDeploymentConfig,
    PAPER_TRADING_GATE_REASONS,
)
from trading_system.paper.gate import GateDecision


# --- fixtures --------------------------------------------------------------- #
@pytest.fixture()
def store():
    return EvidenceStore(create_engine("sqlite://"))


@pytest.fixture()
def registry(store):
    return StrategyRegistry(store)


@pytest.fixture()
def intelligence(registry):
    return StrategyIntelligence(registry)


@pytest.fixture()
def gate(intelligence):
    return DeploymentGate(
        intelligence=intelligence,
        requirement=EvidenceRequirement(),
        freshness_config=EvidenceFreshnessConfig(max_age_days=180),
    )


def _valid_spec(**overrides):
    payload = {
        "name": "Phase18 strategy",
        "description": "phase 18",
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


def _research_ev(strategy, ds, created_at, cfg_key=1):
    metrics = {
        "symbol": strategy.symbol,
        "timeframe": strategy.timeframe,
        "rows": 400,
        "requested_candidates": 1,
        "candidates": [{
            "variant_index": 0, "status": "evaluated",
            "spec_name": strategy.name, "spec_errors": [], "error": "",
            "evaluation": {"total_return": 0.10, "profit_factor": 1.5,
                            "max_drawdown": -0.05, "n_trades": 25},
            "filter_passed": True, "filter_reasons": [],
        }],
        "ranking": [], "notes": [],
    }
    ds_id = dataset_identity(ds)
    return StrategyEvidence(
        evidence_id=evidence_identity(strategy.strategy_id, EvidenceType.RESEARCH, ds_id, {"k": cfg_key}),
        strategy_id=strategy.strategy_id,
        strategy_spec_hash=strategy.spec_hash,
        evidence_type=EvidenceType.RESEARCH,
        dataset_id=ds_id,
        configuration_json={"k": cfg_key},
        metrics_json=metrics,
        created_at=created_at,
    )


def _wf_summary_ev(strategy, ds, created_at, total_trades=100, cfg_key=1):
    ds_id = dataset_identity(ds)
    cfg = {"kind": "wf_summary", "k": cfg_key}
    metrics = {
        "kind": "fixed_spec", "spec_name": strategy.name,
        "symbol": strategy.symbol, "timeframe": strategy.timeframe,
        "mode": "rolling", "folds": [],
        "summary": {
            "n_folds": 5, "n_valid": 4, "n_failed": 1,
            "coverage": 0.8, "coverage_ok": True,
            "positive_folds": 3, "positive_fold_ratio": 0.75,
            "avg_fold_return": 0.05, "median_fold_return": 0.05,
            "worst_fold_return": -0.05, "best_fold_return": 0.15,
            "return_std": 0.05, "return_dispersion": 1.0,
            "max_validation_drawdown": -0.08,
            "consistency_score": 0.7,
            "total_validation_trades": total_trades,
            "min_validation_trades": 10,
            "valid_fold_ids": [0, 1, 2, 3],
        },
        "warnings": [], "notes": [],
    }
    return StrategyEvidence(
        evidence_id=evidence_identity(strategy.strategy_id, EvidenceType.WALK_FORWARD, ds_id, cfg),
        strategy_id=strategy.strategy_id,
        strategy_spec_hash=strategy.spec_hash,
        evidence_type=EvidenceType.WALK_FORWARD,
        dataset_id=ds_id,
        configuration_json=cfg,
        metrics_json=metrics,
        created_at=created_at,
    )


def _make_eligible(intelligence, registry, *, status=StrategyStatus.WALK_FORWARD_VALIDATED,
                   total_trades=100):
    spec = _valid_spec()
    strategy = registry.register_strategy(spec)
    if status != StrategyStatus.PROPOSED:
        registry.update_strategy_status(strategy.strategy_id, status)
        strategy = registry.get_strategy(strategy.strategy_id)
    ds = _make_dataset()
    fresh = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    registry.record_evidence(_research_ev(strategy, ds, fresh))
    registry.record_evidence(_wf_summary_ev(strategy, ds, fresh, total_trades=total_trades))
    return strategy, spec, ds


# --- eligible / ineligible -------------------------------------------------- #
class TestGateEligibility:
    def test_eligible_strategy_deploys(self, gate, intelligence, registry):
        strategy, spec, ds = _make_eligible(intelligence, registry)
        cfg = PaperDeploymentConfig()
        decision = gate.evaluate(
            strategy_id=strategy.strategy_id,
            spec=spec,
            symbol=strategy.symbol,
            timeframe=strategy.timeframe,
            dataset_id=dataset_identity(ds),
            config=cfg,
        )
        assert decision.passed
        assert decision.reasons == []
        assert decision.deployment is not None
        assert decision.deployment.strategy_spec_hash == strategy.spec_hash
        assert decision.deployment.symbol == strategy.symbol
        assert decision.deployment.timeframe == strategy.timeframe
        assert decision.deployment.config.execution_mode == "paper"

    def test_unknown_strategy_rejected(self, gate):
        decision = gate.evaluate(
            strategy_id="does-not-exist",
            spec=_valid_spec(),
            symbol="NSE:SBIN",
            timeframe="1d",
            dataset_id="ds",
            config=PaperDeploymentConfig(),
        )
        assert not decision.passed
        assert "unknown_strategy" in decision.reasons
        assert decision.deployment is None

    def test_retired_strategy_rejected(self, gate, intelligence, registry):
        strategy, spec, ds = _make_eligible(intelligence, registry)
        intelligence.retire_strategy(strategy.strategy_id, "no longer viable")
        decision = gate.evaluate(
            strategy_id=strategy.strategy_id, spec=spec,
            symbol=strategy.symbol, timeframe=strategy.timeframe,
            dataset_id=dataset_identity(ds), config=PaperDeploymentConfig(),
        )
        assert not decision.passed
        assert "retired_strategy" in decision.reasons

    def test_rejected_strategy_rejected(self, gate, intelligence, registry):
        strategy = registry.register_strategy(_valid_spec())
        registry.update_strategy_status(strategy.strategy_id, StrategyStatus.REJECTED)
        spec = _valid_spec()
        decision = gate.evaluate(
            strategy_id=strategy.strategy_id, spec=spec,
            symbol=strategy.symbol, timeframe=strategy.timeframe,
            dataset_id="ds", config=PaperDeploymentConfig(),
        )
        assert not decision.passed
        assert "rejected_strategy" in decision.reasons

    def test_missing_walk_forward_rejected(self, gate, intelligence, registry):
        spec = _valid_spec()
        strategy = registry.register_strategy(spec)
        registry.update_strategy_status(strategy.strategy_id, StrategyStatus.WALK_FORWARD_VALIDATED)
        ds = _make_dataset()
        fresh = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        registry.record_evidence(_research_ev(strategy, ds, fresh))
        decision = gate.evaluate(
            strategy_id=strategy.strategy_id, spec=spec,
            symbol=strategy.symbol, timeframe=strategy.timeframe,
            dataset_id=dataset_identity(ds), config=PaperDeploymentConfig(),
        )
        assert not decision.passed
        assert "missing_walk_forward_evidence" in decision.reasons

    def test_missing_validation_metrics_rejected(self, gate, intelligence, registry):
        spec = _valid_spec()
        strategy = registry.register_strategy(spec)
        registry.update_strategy_status(strategy.strategy_id, StrategyStatus.WALK_FORWARD_VALIDATED)
        ds = _make_dataset()
        fresh = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        registry.record_evidence(_wf_summary_ev(strategy, ds, fresh))
        decision = gate.evaluate(
            strategy_id=strategy.strategy_id, spec=spec,
            symbol=strategy.symbol, timeframe=strategy.timeframe,
            dataset_id=dataset_identity(ds), config=PaperDeploymentConfig(),
        )
        assert not decision.passed
        assert "missing_validation_metrics" in decision.reasons

    def test_stale_evidence_rejected(self, gate, intelligence, registry):
        spec = _valid_spec()
        strategy = registry.register_strategy(spec)
        registry.update_strategy_status(strategy.strategy_id, StrategyStatus.WALK_FORWARD_VALIDATED)
        ds = _make_dataset()
        stale = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        registry.record_evidence(_research_ev(strategy, ds, stale))
        registry.record_evidence(_wf_summary_ev(strategy, ds, stale))
        decision = gate.evaluate(
            strategy_id=strategy.strategy_id, spec=spec,
            symbol=strategy.symbol, timeframe=strategy.timeframe,
            dataset_id=dataset_identity(ds), config=PaperDeploymentConfig(),
        )
        assert not decision.passed
        assert "stale_evidence" in decision.reasons

    def test_insufficient_validation_trades_rejected(self, gate, intelligence, registry):
        strategy, spec, ds = _make_eligible(intelligence, registry, total_trades=2)
        req = EvidenceRequirement(min_validation_trades=50)
        g2 = DeploymentGate(
            intelligence=intelligence,
            requirement=req,
            freshness_config=EvidenceFreshnessConfig(max_age_days=180),
        )
        decision = g2.evaluate(
            strategy_id=strategy.strategy_id, spec=spec,
            symbol=strategy.symbol, timeframe=strategy.timeframe,
            dataset_id=dataset_identity(ds), config=PaperDeploymentConfig(),
        )
        assert not decision.passed
        assert "insufficient_validation_trades" in decision.reasons

    def test_symbol_mismatch_rejected(self, gate, intelligence, registry):
        strategy, spec, ds = _make_eligible(intelligence, registry)
        decision = gate.evaluate(
            strategy_id=strategy.strategy_id, spec=spec,
            symbol="NSE:OTHER", timeframe=strategy.timeframe,
            dataset_id=dataset_identity(ds), config=PaperDeploymentConfig(),
        )
        assert not decision.passed
        assert "symbol_mismatch" in decision.reasons

    def test_timeframe_mismatch_rejected(self, gate, intelligence, registry):
        strategy, spec, ds = _make_eligible(intelligence, registry)
        decision = gate.evaluate(
            strategy_id=strategy.strategy_id, spec=spec,
            symbol=strategy.symbol, timeframe="1h",
            dataset_id=dataset_identity(ds), config=PaperDeploymentConfig(),
        )
        assert not decision.passed
        assert "timeframe_mismatch" in decision.reasons

    def test_paper_mode_required(self, gate, intelligence, registry):
        strategy, spec, ds = _make_eligible(intelligence, registry)
        # Bypass the pydantic gate by constructing via model_construct with a
        # non-paper execution_mode (must be rejected by the gate).
        bad_cfg = PaperDeploymentConfig.model_construct(
            execution_mode="live",
            initial_cash=100_000.0,
            slippage_bps=5.0,
            fee_bps=0.0,
            max_allocation_pct=1.0,
            max_position_size=None,
            allow_short=False,
            stop_loss_pct=None,
            take_profit_pct=None,
            max_loss_per_trade_pct=None,
            warmup_bars=0,
        )
        decision = gate.evaluate(
            strategy_id=strategy.strategy_id, spec=spec,
            symbol=strategy.symbol, timeframe=strategy.timeframe,
            dataset_id=dataset_identity(ds), config=bad_cfg,
        )
        assert not decision.passed
        assert "paper_mode_required" in decision.reasons

    def test_invalid_risk_config_rejected_at_pydantic(self):
        # Negative max_allocation_pct fails pydantic validation BEFORE the gate.
        with pytest.raises(Exception):
            PaperDeploymentConfig(max_allocation_pct=0.0)
        with pytest.raises(Exception):
            PaperDeploymentConfig(max_allocation_pct=1.5)
        with pytest.raises(Exception):
            PaperDeploymentConfig(stop_loss_pct=1.5)

    def test_all_reasons_are_documented(self):
        # Every reason emitted by the gate must appear in the documented set.
        assert "unknown_strategy" in PAPER_TRADING_GATE_REASONS
        assert "retired_strategy" in PAPER_TRADING_GATE_REASONS
        assert "rejected_strategy" in PAPER_TRADING_GATE_REASONS
        assert "missing_walk_forward_evidence" in PAPER_TRADING_GATE_REASONS
        assert "missing_validation_metrics" in PAPER_TRADING_GATE_REASONS
        assert "insufficient_validation_trades" in PAPER_TRADING_GATE_REASONS
        assert "stale_evidence" in PAPER_TRADING_GATE_REASONS
        assert "paper_mode_required" in PAPER_TRADING_GATE_REASONS


# --- PaperBroker-only safety ------------------------------------------------ #
class TestPaperOnlyGuard:
    def test_paper_broker_accepted(self):
        from trading_system.execution.paper_broker import PaperBroker
        DeploymentGate.assert_paper_broker(PaperBroker())

    def test_abstract_broker_rejected(self):
        from trading_system.execution.broker import Broker
        with pytest.raises(TypeError):
            DeploymentGate.assert_paper_broker(Broker())

    def test_fake_broker_subclass_rejected(self):
        from trading_system.execution.broker import Broker

        class FakeLiveBroker(Broker):
            def submit_order(self, *a, **kw): pass
            def cancel_order(self, *a, **kw): pass
            def get_order(self, *a, **kw): pass
            def update_market_price(self, *a, **kw): pass
            def get_position(self, *a, **kw): pass
            def positions(self): return {}
            def account(self): pass

        with pytest.raises(TypeError):
            DeploymentGate.assert_paper_broker(FakeLiveBroker())

    def test_none_rejected(self):
        with pytest.raises(TypeError):
            DeploymentGate.assert_paper_broker(None)

    def test_duck_typed_object_rejected(self):
        class Pretender:
            pass
        with pytest.raises(TypeError):
            DeploymentGate.assert_paper_broker(Pretender())


# --- strategy identity binding ---------------------------------------------- #
class TestStrategyIdentityBinding:
    def test_modified_spec_rejected_by_gate(self, gate, intelligence, registry):
        strategy, spec, ds = _make_eligible(intelligence, registry)
        tampered = spec.model_copy(update={"name": "different name"})
        decision = gate.evaluate(
            strategy_id=strategy.strategy_id, spec=tampered,
            symbol=strategy.symbol, timeframe=strategy.timeframe,
            dataset_id=dataset_identity(ds), config=PaperDeploymentConfig(),
        )
        assert not decision.passed
        assert "strategy_hash_mismatch" in decision.reasons
        # Deployment identity must NOT be produced for tampered specs.
        assert decision.deployment is None