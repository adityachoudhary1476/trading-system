"""Command-line entry point.

Usage:
    python -m trading_system ingest            # fetch + validate + store
    python -m trading_system analyze SYMBOL     # quant/indicator summary on stored data
    python -m trading_system ai-analyze SYMBOL  # snapshot -> AI view -> deterministic signal
    python -m trading_system status             # config (no secrets) + stored counts

All commands are ANALYSIS / PAPER ONLY. No broker order execution exists.
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import settings, configure_logging
from .data.pipeline import IngestionPipeline
from .analysis.pipeline import analyze
from .storage.database import MarketStore
from .models.snapshot import build_snapshot_from_df
from .models import analyze_snapshot, get_model_provider


def _store() -> MarketStore:
    return MarketStore(settings.storage.db_url)


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


def _cmd_ai_analyze(args: argparse.Namespace) -> int:
    tf = args.timeframe or settings.market.timeframe
    df = _store().load(args.symbol, tf)
    if df.empty:
        print(f"No stored data for {args.symbol} ({tf}). Run: ingest --symbols {args.symbol}")
        return 1

    snapshot = build_snapshot_from_df(df, args.symbol, tf)
    provider = get_model_provider(args.provider)
    result = analyze_snapshot(snapshot, provider=provider)

    print("=" * 64)
    print("  ANALYSIS / PAPER ONLY — no broker order is placed")
    print("=" * 64)
    if result.error:
        print(f"ERROR: {result.error}")
        return 1

    v = result.view
    print(f"Symbol        : {snapshot.symbol} ({snapshot.timeframe})")
    print(f"Decision time : {snapshot.timestamp}")
    print(f"Latest price  : {snapshot.latest_price:.4f}")
    print(f"Market view   : {v.market_view.value.upper()}  (analytical confidence {v.confidence:.2f})")
    print(f"Model         : {v.model}")
    print(f"Summary       : {v.reasoning_summary}")
    if v.bullish_factors:
        print("Bullish       : " + "; ".join(v.bullish_factors))
    if v.bearish_factors:
        print("Bearish       : " + "; ".join(v.bearish_factors))
    if v.risks:
        print("Risks         : " + "; ".join(v.risks))
    sig = result.signal
    print("-" * 64)
    print(f"SIGNAL        : {sig.direction.value.upper()}")
    print(f"Reason        : {sig.reason}")
    print("=" * 64)
    if args.json:
        print(json.dumps(result.as_dict(), indent=2, default=str))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from tabulate import tabulate

    print("Configuration (no secrets):")
    print(tabulate(list(settings.summary().items()), tablefmt="github"))
    store = _store()
    print("\nStored rows per symbol/timeframe:")
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

    p_an = sub.add_parser("analyze", help="Compute quant/indicator summary on stored data")
    p_an.add_argument("symbol")
    p_an.add_argument("--timeframe", default=None)

    p_ai = sub.add_parser("ai-analyze", help="Snapshot -> AI view -> deterministic signal")
    p_ai.add_argument("symbol")
    p_ai.add_argument("--timeframe", default=None)
    p_ai.add_argument(
        "--provider",
        default=None,
        help="Model provider name: local (default) or openai-compatible",
    )
    p_ai.add_argument("--json", action="store_true", help="Also print full JSON")

    sub.add_parser("status", help="Show config (no secrets) and stored counts")

    args = parser.parse_args(argv)
    configure_logging()
    if args.command == "ingest":
        return _cmd_ingest(args)
    if args.command == "analyze":
        return _cmd_analyze(args)
    if args.command == "ai-analyze":
        return _cmd_ai_analyze(args)
    if args.command == "status":
        return _cmd_status(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
