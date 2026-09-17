#!/usr/bin/env python3
"""
Phase 23 Tournament Runner Script.

Runs the genuine Phase 23 tournament against real historical market data
to produce PAPER_APPROVED strategies for autonomous paper trading.

Usage:
    python run_tournament.py --db-url postgresql://... [--universe SUBSET]
"""

import argparse
import os
import sys
from datetime import UTC, datetime

# Add project root to path (portable — works locally and in Docker)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from trading_system.storage.database import MarketStore
from trading_system.research.phase23.tournament import run_tournament, TournamentRunner
from trading_system.research.phase23.config import Phase23Config, DEFAULT_CONFIG
from trading_system.research.phase23.strategies import build_default_universe
from trading_system.research.evidence import EvidenceStore
from trading_system.research.strategy_registry import StrategyRegistry
from trading_system.research.phase23.registry import Phase23Registry
from trading_system.paper.control import PaperTradingControlCenter
from trading_system.research.strategy_intelligence import EvidenceRequirement, EvidenceFreshnessConfig


def main():
    parser = argparse.ArgumentParser(description="Run Phase 23 tournament")
    parser.add_argument("--db-url", default=None, help="Database URL (PostgreSQL or SQLite). Defaults to MARKET_DATA_DB_URL env var.")
    parser.add_argument("--tournament-id", default=f"phase23-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}", help="Tournament ID")
    parser.add_argument("--universe", choices=["all", "nifty-only", "equity-only"], default="nifty-only", help="Candidate subset")
    parser.add_argument("--wf-folds", type=int, default=5, help="Walk-forward folds")
    parser.add_argument("--bootstrap-n", type=int, default=1000, help="Bootstrap iterations")
    parser.add_argument("--top-n-deploy", type=int, default=5, help="Number of strategies to deploy")
    parser.add_argument("--dry-run", action="store_true", help="Run without persisting to registry")
    args = parser.parse_args()

    print(f"=" * 60)
    print(f"PHASE 23 TOURNAMENT RUNNER")
    print(f"=" * 60)
    print(f"Tournament ID: {args.tournament_id}")
    db_url = args.db_url or os.environ.get("MARKET_DATA_DB_URL")
    if db_url is None:
        print("ERROR: --db-url is required (or set MARKET_DATA_DB_URL env var)")
        sys.exit(1)
    print(f"Database: {db_url[:50]}...")
    print(f"Universe: {args.universe}")

    # Initialize data store
    store = MarketStore(db_url)

    def data_loader(symbol: str, timeframe: str):
        df = store.load(symbol, timeframe)
        if df is None or df.empty:
            return None
        return df

    # Build universe
    full_universe = build_default_universe()

    if args.universe == "nifty-only":
        # NIFTY options strategies (18 candidates)
        universe = {k: v for k, v in full_universe.items() if v.supported_instruments == ["NSE:NIFTY"]}
        print(f"Selected {len(universe)} NIFTY options candidates")
    elif args.universe == "equity-only":
        # Equity strategies (110 candidates)
        universe = {k: v for k, v in full_universe.items() if v.supported_instruments == ["NSE:SBIN"]}
        print(f"Selected {len(universe)} equity candidates")
    else:
        universe = full_universe
        print(f"Selected all {len(universe)} candidates")

    # Configure tournament
    config = Phase23Config(
        tournament_id=args.tournament_id,
        wf_n_folds=args.wf_folds,
        bootstrap_n=args.bootstrap_n,
        wf_min_validation_trades=1,
        wf_min_fold_coverage=0.2,
        top_n_deploy=args.top_n_deploy,
    )

    # Initialize registry and control center for persistence
    evidence_store = None
    strategy_registry = None
    phase23_registry = None
    control_center = None

    if not args.dry_run:
        print("Initializing persistence layer...")
        evidence_store = EvidenceStore(store.engine)
        strategy_registry = StrategyRegistry(evidence_store)
        phase23_registry = Phase23Registry(strategy_registry)

        requirement = EvidenceRequirement(require_walk_forward=False, require_validation=False, require_recent_evidence=False, min_validation_trades=0)
        freshness = EvidenceFreshnessConfig(max_age_days=180)

        control_center = PaperTradingControlCenter.from_engine(
            store.engine,
            requirement=requirement,
            freshness_config=freshness,
            market_data_provider=data_loader,
        )
        print("Persistence layer ready")

    print(f"\nStarting tournament with {len(universe)} candidates...")
    print(f"Walk-forward: {config.wf_n_folds} folds, {config.wf_train_window}d train, {config.wf_validation_window}d validation")
    print(f"Bootstrap: {config.bootstrap_n} iterations")
    print()

    # Run tournament
    result = run_tournament(
        tournament_id=args.tournament_id,
        config=config,
        universe=universe,
        data_loader=data_loader,
        registry=strategy_registry,  # TournamentRunner wraps in Phase23Registry
        control_center=control_center,
    )

    # Print results
    print(f"\n{'=' * 60}")
    print(f"TOURNAMENT COMPLETE: {result.tournament_id}")
    print(f"{'=' * 60}")
    print(f"Total candidates: {len(result.candidates)}")
    print(f"Qualified: {len(result.qualified)}")
    print(f"Rejected: {len(result.rejected)}")
    print(f"Top 20: {len(result.top_20)}")
    print(f"Final N (diversified): {len(result.final_n)}")
    print(f"Clusters: {len(result.clusters)}")
    print(f"Failures: {len(result.failures)}")

    print(f"\nQUALIFIED CANDIDATES:")
    for cid in result.qualified:
        r = result.candidates[cid]
        print(f"  {cid}: score={r.score:.1f}, strategy_id={r.strategy_id}")

    print(f"\nREJECTED CANDIDATES:")
    for cid in result.rejected:
        r = result.candidates[cid]
        print(f"  {cid}: state={r.state.value}, reasons={r.rejection_reasons}")

    print(f"\nFINAL N (PAPER_APPROVED):")
    for item in result.final_n:
        print(f"  {item['candidate_id']}: score={item['score']:.1f}, strategy_id={item['strategy_id']}, cluster={item['cluster_id']}")

    if args.dry_run:
        print("\nDRY RUN - no data persisted to registry")

    return 0 if result.qualified else 1


if __name__ == "__main__":
    sys.exit(main())