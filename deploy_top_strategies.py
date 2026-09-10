"""Deploy top tournament strategies to paper trading."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from trading_system.research.phase23.config import Phase23Config
from trading_system.research.phase23.tournament import TournamentRunner
from trading_system.research.phase23.deployment import Phase23DeploymentPolicy
from trading_system.research.phase23.discovery import Phase23Discovery
from trading_system.research.phase23.registry import Phase23Registry
from trading_system.research.strategy_registry import StrategyRegistry
from trading_system.storage.database import MarketStore
from trading_system.research.phase23.strategies import build_default_universe


def main():
    parser = argparse.ArgumentParser(description="Run tournament and deploy top strategies")
    parser.add_argument("--top-n", type=int, default=5, help="Number of top strategies to deploy")
    parser.add_argument("--dry-run", action="store_true", help="Only discover, do not deploy")
    parser.add_argument("--tournament-id", default="phase24-auto", help="Tournament ID")
    parser.add_argument("--db-url", default=None, help="Production database URL (default: local SQLite)")
    parser.add_argument("--market-data-db-url", default=None, help="Market data database URL (default: local SQLite)")
    args = parser.parse_args()

    # Load historical data
    market_data_db_url = args.market_data_db_url or 'sqlite:///./data/market_data.db'
    store = MarketStore(market_data_db_url)

    def load_data(symbol, timeframe):
        df = store.load(symbol, timeframe)
        return df if not df.empty else None

    # Build universe and run tournament
    universe = build_default_universe()
    config = Phase23Config(
        tournament_id=args.tournament_id,
        wf_n_folds=3,
        bootstrap_n=100,
        wf_min_validation_trades=1,
        wf_min_fold_coverage=0.01,
    )

    # Use production registry if db-url is provided
    registry = None
    if args.db_url:
        from trading_system.research.strategy_registry import StrategyRegistry
        from trading_system.research.phase23.registry import Phase23Registry
        strategy_registry = StrategyRegistry(args.db_url)
        registry = Phase23Registry(strategy_registry)

    runner = TournamentRunner(config=config, universe=universe, data_loader=load_data, registry=registry)
    result = runner.run()

    print(f"Tournament {args.tournament_id} complete")
    print(f"  Processed: {len(result.candidates)}")
    print(f"  Qualified: {len(result.qualified)}")
    print(f"  Rejected: {len(result.rejected)}")
    print(f"  Failures: {len(result.failures)}")

    # Set up registry and discovery
    db_url = args.db_url or 'sqlite:///./data/strategy_registry.db'
    production_registry = Phase23Registry(StrategyRegistry(db_url))
    discovery = Phase23Discovery(production_registry)

    # Discover qualified strategies
    discovered = discovery.discover(max_candidates=args.top_n)
    print(f"\nDiscovered {len(discovered)} paper-approved strategies")

    if args.dry_run:
        print("\nDry run - skipping deployment")
        for d in discovered[:args.top_n]:
            print(f"  {d.strategy_id} (candidate={d.candidate_id}, score={d.score})")
        return

    # Deploy top strategies
    try:
        from trading_system.paper.control import PaperTradingControlCenter
        from sqlalchemy import create_engine
        from trading_system.research.evidence import EvidenceStore
        from trading_system.autonomous.persistence import AutonomousBotStateStore
        
        db_url = args.db_url or 'sqlite:///./data/market_data.db'
        engine = create_engine(db_url)
        EvidenceStore(engine).ensure_schema_current()
        AutonomousBotStateStore(engine).ensure_schema()
        
        cc = PaperTradingControlCenter.from_engine(engine)
        policy = Phase23DeploymentPolicy(control_center=cc, dataset_id=args.tournament_id)
    except Exception as exc:
        print(f"Warning: could not initialize paper control center: {exc}")
        print("Deployments skipped. Run from an environment with paper trading available.")
        return

    deployed = []
    for d in discovered[:args.top_n]:
        try:
            # Rebuild spec from candidate
            candidate = universe.get(d.candidate_id)
            if candidate is None:
                print(f"  {d.strategy_id}: candidate {d.candidate_id} not found, skipping")
                continue

            spec = candidate.build_spec(symbol=d.symbol, timeframe=d.timeframe)
            dep_result = policy.deploy(
                strategy_id=d.strategy_id,
                candidate_id=d.candidate_id,
                spec=spec,
                symbol=d.symbol,
                timeframe=d.timeframe,
                activate=True,
            )
            if dep_result.success:
                deployed.append(dep_result)
                status = "DUPLICATE" if dep_result.is_duplicate else "CREATED"
                print(f"  {d.strategy_id}: {status} deployment {dep_result.deployment.deployment_id}")
            else:
                print(f"  {d.strategy_id}: FAILED - {dep_result.error}")
        except Exception as exc:
            print(f"  {d.strategy_id}: ERROR - {exc}")

    print(f"\nDeployed {len(deployed)} strategies")

    # Save report
    report = {
        "tournament_id": args.tournament_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "qualified": len(result.qualified),
        "rejected": len(result.rejected),
        "failures": len(result.failures),
        "discovered": len(discovered),
        "deployed": len(deployed),
        "top_n": args.top_n,
    }
    with open(f"deployment_report_{args.tournament_id}.json", "w") as f:
        json.dump(report, f, indent=2)
    print(f"Report saved to deployment_report_{args.tournament_id}.json")


if __name__ == "__main__":
    main()
