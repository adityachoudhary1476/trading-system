"""Phase 24 tournament tests."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from trading_system.research.phase23.scoring import (
    RejectionGate,
    RejectionReason,
    RobustnessScorer,
    ScorerInput,
    ScorerOutput,
)
from trading_system.research.phase23.selection import (
    DiversitySelector,
    select_diversified,
)
from trading_system.research.phase23.config import DEFAULT_CONFIG, Phase23Config, RejectionThresholds
from trading_system.research.phase23.tournament import (
    CandidateResult,
    CandidateState,
    TournamentRunner,
)
from trading_system.research.phase23.discovery import Phase23Discovery
from trading_system.research.phase23.deployment import Phase23DeploymentPolicy
from trading_system.research.phase23.monitoring import StrategyMonitor
from trading_system.research.phase23.strategies import build_default_universe


# --------------------------------------------------------------------------- #
# Scoring tests
# --------------------------------------------------------------------------- #
class TestRobustnessScorer:
    def test_score_returns_float(self):
        scorer = RobustnessScorer()
        inp = ScorerInput(total_return=0.1, sharpe=1.0, max_drawdown=0.1)
        out = scorer.score(inp)
        assert isinstance(out.score, float)
        assert 0.0 <= out.score <= 100.0

    def test_drawdown_penalty(self):
        scorer = RobustnessScorer()
        inp = ScorerInput(total_return=0.1, sharpe=1.0, max_drawdown=0.5)
        out = scorer.score(inp)
        assert out.penalties.get("drawdown", 0.0) > 0.0

    def test_deterministic(self):
        scorer = RobustnessScorer()
        inp = ScorerInput(total_return=0.1, sharpe=1.0, max_drawdown=0.1)
        out1 = scorer.score(inp)
        out2 = scorer.score(inp)
        assert out1.score == out2.score


class TestRejectionGate:
    def test_insufficient_trades(self):
        gate = RejectionGate()
        inp = ScorerInput(trade_count=10, wf_coverage=1.0, bootstrap_prob_positive=0.6)
        result = gate.evaluate(inp)
        assert RejectionReason.INSUFFICIENT_TRADES in result.rejection_reasons

    def test_excessive_drawdown(self):
        gate = RejectionGate()
        inp = ScorerInput(max_drawdown=0.5, wf_coverage=1.0, bootstrap_prob_positive=0.6)
        result = gate.evaluate(inp)
        assert RejectionReason.EXCESSIVE_DRAWDOWN in result.rejection_reasons

    def test_passes_when_clean(self):
        gate = RejectionGate()
        inp = ScorerInput(
            trade_count=50,
            total_return=0.2,
            max_drawdown=0.1,
            win_rate=0.6,
            profit_factor=2.0,
            wf_coverage=1.0,
            bootstrap_prob_positive=0.6,
        )
        result = gate.evaluate(inp)
        assert result.passed is True
        assert len(result.rejection_reasons) == 0


# --------------------------------------------------------------------------- #
# Selection tests
# --------------------------------------------------------------------------- #
class TestDiversitySelector:
    def test_select_diversified_returns_list(self):
        import pandas as pd
        candidates = {
            "a": ScorerInput(total_return=0.1, sharpe=1.0),
            "b": ScorerInput(total_return=0.05, sharpe=0.5),
        }
        returns = {
            "a": pd.Series([0.01, -0.01, 0.02]),
            "b": pd.Series([0.02, -0.02, 0.01]),
        }
        selected, clusters = select_diversified(candidates, returns, top_n=2)
        assert isinstance(selected, list)
        assert len(selected) <= 2

    def test_empty_candidates(self):
        selected, clusters = select_diversified({}, {})
        assert selected == []
        assert clusters == []


# --------------------------------------------------------------------------- #
# Tournament runner tests
# --------------------------------------------------------------------------- #
class TestTournamentRunner:
    def test_universe_size(self):
        universe = build_default_universe()
        assert len(universe) >= 90

    def test_all_candidates_explicit_state(self):
        from trading_system.storage.database import MarketStore
        store = MarketStore('sqlite:///./data/market_data.db')

        def load_data(symbol, timeframe):
            df = store.load(symbol, timeframe)
            return df if not df.empty else None

        universe = build_default_universe()
        subset = dict(list(universe.items())[:3])
        config = Phase23Config(
            tournament_id='test-explicit',
            wf_n_folds=2,
            bootstrap_n=10,
            wf_min_validation_trades=1,
            wf_min_fold_coverage=0.01,
        )
        runner = TournamentRunner(config=config, universe=subset, data_loader=load_data)
        result = runner.run()
        assert len(result.candidates) == 3
        for cid, r in result.candidates.items():
            assert r.state in (
                CandidateState.QUALIFIED,
                CandidateState.REJECTED,
                CandidateState.DATA_UNAVAILABLE,
                CandidateState.EXECUTION_ERROR,
                CandidateState.INVALID_SPEC,
            )

    def test_no_silent_skip(self):
        from trading_system.storage.database import MarketStore
        store = MarketStore('sqlite:///./data/market_data.db')

        def load_data(symbol, timeframe):
            df = store.load(symbol, timeframe)
            return df if not df.empty else None

        universe = build_default_universe()
        subset = dict(list(universe.items())[:5])
        config = Phase23Config(
            tournament_id='test-noskip',
            wf_n_folds=2,
            bootstrap_n=10,
            wf_min_validation_trades=1,
            wf_min_fold_coverage=0.01,
        )
        runner = TournamentRunner(config=config, universe=subset, data_loader=load_data)
        result = runner.run()
        assert len(result.candidates) == len(subset)

    def test_failure_isolation(self):
        from trading_system.storage.database import MarketStore
        from trading_system.research.phase23.strategies import UniverseCandidate, StrategyFamily
        store = MarketStore('sqlite:///./data/market_data.db')

        def load_data(symbol, timeframe):
            df = store.load(symbol, timeframe)
            return df if not df.empty else None

        good = build_default_universe()["trend-ema-fast-slow"]
        bad = UniverseCandidate(
            candidate_id="bad-spec",
            strategy_family=StrategyFamily.OTHER,
            strategy_name="Bad Spec",
            description="test",
            hypothesis="test",
            required_features=[],
            timeframe="1d",
            supported_instruments=["NSE:SBIN"],
            supported_sessions=["regular"],
            parameter_schema={},
            default_parameters={},
            parameter_ranges={},
            version="1.0.0",
            implementation_status="implemented",
            spec_builder=lambda s, t: {"invalid": True},
        )
        universe = {"good": good, "bad": bad}

        config = Phase23Config(
            tournament_id='test-isolation',
            wf_n_folds=2,
            bootstrap_n=10,
            wf_min_validation_trades=1,
            wf_min_fold_coverage=0.01,
        )
        runner = TournamentRunner(config=config, universe=universe, data_loader=load_data)
        result = runner.run()
        assert "bad" in result.candidates
        assert result.candidates["bad"].state == CandidateState.INVALID_SPEC
        assert "good" in result.candidates


# --------------------------------------------------------------------------- #
# Discovery tests
# --------------------------------------------------------------------------- #
class TestPhase23Discovery:
    def test_discovery_returns_list(self):
        registry = MagicMock()
        registry.list_strategies.return_value = []
        registry.list_evidence.return_value = []
        discovery = Phase23Discovery(registry)
        results = discovery.discover()
        assert isinstance(results, list)


# --------------------------------------------------------------------------- #
# Deployment tests
# --------------------------------------------------------------------------- #
class TestPhase23DeploymentPolicy:
    def test_deploy_requires_control_center(self):
        policy = Phase23DeploymentPolicy(control_center=None)
        result = policy.deploy(
            strategy_id="test",
            candidate_id="test",
            spec=MagicMock(),
            symbol="NSE:SBIN",
            timeframe="1d",
        )
        assert result.success is False


# --------------------------------------------------------------------------- #
# Monitoring tests
# --------------------------------------------------------------------------- #
class TestStrategyMonitor:
    def test_check_deployment_returns_health(self):
        cc = MagicMock()
        cc.get_deployment.return_value = MagicMock(status="ACTIVE")
        monitor = StrategyMonitor(cc)
        hc = monitor.check_deployment("dep-1")
        assert isinstance(hc.is_healthy, bool)


# --------------------------------------------------------------------------- #
# Integration tests
# --------------------------------------------------------------------------- #
class TestTournamentIntegration:
    def test_all_candidates_processed(self):
        from trading_system.storage.database import MarketStore
        store = MarketStore('sqlite:///./data/market_data.db')

        def load_data(symbol, timeframe):
            df = store.load(symbol, timeframe)
            return df if not df.empty else None

        universe = build_default_universe()
        config = Phase23Config(
            tournament_id='test-all',
            wf_n_folds=2,
            bootstrap_n=10,
            wf_min_validation_trades=1,
            wf_min_fold_coverage=0.01,
        )
        runner = TournamentRunner(config=config, universe=universe, data_loader=load_data)
        result = runner.run()
        assert len(result.candidates) == len(universe)

    def test_deterministic_scoring(self):
        from trading_system.research.phase23.scoring import RobustnessScorer, ScorerInput
        scorer = RobustnessScorer()
        inp = ScorerInput(total_return=0.1, sharpe=1.0, max_drawdown=0.1, trade_count=50)
        out1 = scorer.score(inp)
        out2 = scorer.score(inp)
        assert out1.score == out2.score

    def test_rejection_gates(self):
        from trading_system.research.phase23.scoring import RejectionGate, RejectionReason, ScorerInput
        gate = RejectionGate()
        inp = ScorerInput(trade_count=10, wf_coverage=0.5, bootstrap_prob_positive=0.3)
        result = gate.evaluate(inp)
        assert RejectionReason.INSUFFICIENT_TRADES in result.rejection_reasons
        assert RejectionReason.POOR_WALK_FORWARD in result.rejection_reasons
        assert RejectionReason.LOW_BOOTSTRAP_PROBABILITY in result.rejection_reasons

    def test_cost_sensitivity(self):
        from trading_system.research.phase23.scoring import RobustnessScorer, ScorerInput
        scorer = RobustnessScorer()
        inp_low = ScorerInput(total_return=0.1, sharpe=1.0, max_drawdown=0.1, cost_sensitivity=0.1)
        inp_high = ScorerInput(total_return=0.1, sharpe=1.0, max_drawdown=0.1, cost_sensitivity=0.8)
        out_low = scorer.score(inp_low)
        out_high = scorer.score(inp_high)
        assert out_low.score > out_high.score

    def test_walk_forward_handling(self):
        from trading_system.storage.database import MarketStore
        store = MarketStore('sqlite:///./data/market_data.db')

        def load_data(symbol, timeframe):
            df = store.load(symbol, timeframe)
            return df if not df.empty else None

        universe = build_default_universe()
        subset = dict(list(universe.items())[:2])
        config = Phase23Config(
            tournament_id='test-wf',
            wf_n_folds=2,
            bootstrap_n=10,
            wf_min_validation_trades=1,
            wf_min_fold_coverage=0.01,
        )
        runner = TournamentRunner(config=config, universe=subset, data_loader=load_data)
        result = runner.run()
        for cid, r in result.candidates.items():
            if r.walk_forward is not None:
                assert hasattr(r.walk_forward, 'summary')
                assert r.walk_forward.summary is not None

    def test_cross_sectional_handling(self):
        from trading_system.research.phase23.scoring import ScorerInput
        inp = ScorerInput(
            total_return=0.1,
            sharpe=1.0,
            max_drawdown=0.1,
            cross_section_profitable_pct=0.8,
            cross_section_concentration=0.3,
        )
        assert inp.cross_section_profitable_pct == 0.8
        assert inp.cross_section_concentration == 0.3

    def test_diversity_clustering(self):
        from trading_system.research.phase23.selection import select_diversified, DiversitySelector
        selector = DiversitySelector(correlation_threshold=0.7)
        assert selector.cluster({}, {}) == []

    def test_reproducibility(self):
        from trading_system.storage.database import MarketStore
        store = MarketStore('sqlite:///./data/market_data.db')

        def load_data(symbol, timeframe):
            df = store.load(symbol, timeframe)
            return df if not df.empty else None

        universe = build_default_universe()
        subset = dict(list(universe.items())[:3])
        config = Phase23Config(
            tournament_id='test-repro',
            wf_n_folds=2,
            bootstrap_n=10,
            wf_min_validation_trades=1,
            wf_min_fold_coverage=0.01,
            random_seed=42,
        )
        runner1 = TournamentRunner(config=config, universe=subset, data_loader=load_data)
        result1 = runner1.run()
        runner2 = TournamentRunner(config=config, universe=subset, data_loader=load_data)
        result2 = runner2.run()
        for cid in result1.candidates:
            assert result1.candidates[cid].score == result2.candidates[cid].score

    def test_report_generation(self):
        from trading_system.research.phase23.report import generate_report
        report = generate_report(
            tournament_id='test-report',
            config={},
            universe_total=10,
            universe_families=['trend_momentum'],
            backtested_count=5,
            validated_count=3,
            failed_count=5,
            qualified_count=0,
            rejected_count=5,
            top_20=[],
            top_10=[],
            final_n=[],
            clusters=[],
            paper_approved_count=0,
            discovered_count=0,
            deployments_created=0,
            failures=[],
            test_results={},
        )
        assert report.tournament_id == 'test-report'
        assert isinstance(report.to_json(), str)

    def test_no_mocked_final_claims(self):
        from trading_system.storage.database import MarketStore
        store = MarketStore('sqlite:///./data/market_data.db')

        def load_data(symbol, timeframe):
            df = store.load(symbol, timeframe)
            return df if not df.empty else None

        universe = build_default_universe()
        subset = dict(list(universe.items())[:5])
        config = Phase23Config(
            tournament_id='test-nomock',
            wf_n_folds=2,
            bootstrap_n=10,
            wf_min_validation_trades=1,
            wf_min_fold_coverage=0.01,
        )
        runner = TournamentRunner(config=config, universe=subset, data_loader=load_data)
        result = runner.run()
        for cid, r in result.candidates.items():
            if r.state == CandidateState.QUALIFIED:
                assert r.evaluation is not None
                assert r.scorer_output is not None
                break
