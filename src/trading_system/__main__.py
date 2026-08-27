"""Command-line entry point.

Usage:
    python -m trading_system ingest            # fetch + validate + store
    python -m trading_system analyze SYMBOL     # compute analytics on stored data
    python -m trading_system status             # show counts and config (no secrets)

Day 1 commands are data-only. No trading/execution subcommands exist.
"""
from __future__ import annotations

import argparse
import sys

from .config import settings, configure_logging, log
from .data.pipeline import IngestionPipeline
from .analysis.pipeline import analyze
from .storage.database import MarketStore


def _cmd_ingest(args: argparse.Namespace) -> int:
    from tabulate import tabulate

    pipe = IngestionPipeline()
    symbols = args.symbols.split(",") if args.symbols else None
    results = pipe.run(symbols)
    rows = [r.as_dict() for r in results]
    print("\n" + tabulate(rows, headers="keys", tablefmt="github") + "\n")
    failed = [r for r in results if r.error]
    if failed:
        for r in failed:
            print(f"FAILED {r.symbol}: {r.error}", file=sys.stderr)
        return 1
    print("Ingestion complete.")
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    from tabulate import tabulate

    res = analyze(args.symbol, args.timeframe or settings.market.timeframe)
    if res.rows == 0:
        print(f"No stored data for {args.symbol} ({res.timeframe}). Run ingest first.")
        return 1
    print(
        tabulate(
            [
                ["rows", res.rows],
                ["last_close", round(res.last_close, 4)],
                ["annualized_vol", round(res.annualized_vol, 4)],
                ["max_drawdown", round(res.max_drawdown, 4)],
            ],
            tablefmt="github",
        )
    )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from tabulate import tabulate

    print("Configuration (no secrets):")
    print(tabulate(list(settings.summary().items()), tablefmt="github"))
    store = MarketStore(settings.storage.db_url)
    print("\nStored rows per symbol/timeframe:")
    # Aggregate counts.
    from sqlalchemy import func, select
    from .storage.database import OHLCVRecord

    with store._Session() as s:
        rows = (
            s.execute(
                select(
                    OHLCVRecord.symbol,
                    OHLCVRecord.timeframe,
                    func.count(),
                ).group_by(OHLCVRecord.symbol, OHLCVRecord.timeframe)
            )
            .tuples()
            .all()
        )
    if not rows:
        print("  (empty)")
    else:
        print(tabulate(rows, headers=["symbol", "timeframe", "rows"], tablefmt="github"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trading_system")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ing = sub.add_parser("ingest", help="Fetch, validate, store market data")
    p_ing.add_argument("--symbols", help="Comma-separated override, e.g. BTCUSDT,ETHUSDT")

    p_an = sub.add_parser("analyze", help="Compute analytics on stored data")
    p_an.add_argument("symbol")
    p_an.add_argument("--timeframe", default=None)

    sub.add_parser("status", help="Show config (no secrets) and stored counts")

    args = parser.parse_args(argv)
    configure_logging()
    if args.command == "ingest":
        return _cmd_ingest(args)
    if args.command == "analyze":
        return _cmd_analyze(args)
    if args.command == "status":
        return _cmd_status(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
