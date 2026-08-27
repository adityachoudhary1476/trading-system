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


def _cmd_providers(args: argparse.Namespace) -> int:
    from tabulate import tabulate
    from .data.provider_exports import get_provider

    rows = []
    for name in ("binance", "stooq", "fyers"):
        try:
            p = get_provider(name)
            rows.append([name, p.name, p.has_historical, p.is_real_time])
        except Exception as e:  # pragma: no cover
            rows.append([name, "ERROR", "-", str(e)[:40]])
    print(tabulate(rows, headers=["provider", "name", "historical", "real_time"],
                  tablefmt="github"))
    return 0


def _cmd_instruments(args: argparse.Namespace) -> int:
    from tabulate import tabulate
    from .india import InstrumentRegistry, to_fyers_symbol

    reg = InstrumentRegistry()
    rows = []
    for instr in reg._by_key.values():
        fy = to_fyers_symbol(instr)
        rows.append([instr.key, instr.instrument_type.value, fy])
    print(tabulate(rows, headers=["internal", "type", "fyers_symbol"], tablefmt="github"))
    return 0


def _cmd_ingest_india(args: argparse.Namespace) -> int:
    from tabulate import tabulate
    from .data.provider_exports import get_provider
    from .india import InstrumentRegistry

    provider = get_provider(args.provider or settings.market.provider)
    reg = InstrumentRegistry()
    symbols = args.symbols.split(",") if args.symbols else []
    tf = args.timeframe or "1d"
    limit = args.limit

    print(f"Ingesting Indian market data via '{provider.name}' "
          f"(provider credentials required for live FYERS)...")
    results = []
    for sym in symbols:
        instr = reg.resolve(sym)
        fy_sym = None
        try:
            fy_sym = provider._fyers_symbol(sym) if hasattr(provider, "_fyers_symbol") else sym
        except Exception:
            fy_sym = sym
        # Historical fetch requires auth for FYERS; Binance works without.
        try:
            df = provider.get_historical(sym, tf, limit)
            received = len(df)
            rows = [
                {
                    "symbol": sym,
                    "timeframe": tf,
                    "timestamp": ts,
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "volume": float(r["volume"]),
                    "provider": provider.name,
                    "exchange": instr.internal.exchange,
                }
                for ts, r in df.iterrows()
            ]
            inserted = _store().upsert_many(rows)
            results.append({
                "symbol": sym, "fyers": fy_sym, "received": received,
                "inserted": inserted, "error": None,
            })
        except Exception as e:
            results.append({
                "symbol": sym, "fyers": fy_sym, "received": 0,
                "inserted": 0, "error": f"{type(e).__name__}: {e}",
            })
    print("\n" + tabulate(results, headers="keys", tablefmt="github") + "\n")
    failed = [r for r in results if r["error"]]
    if failed:
        for r in failed:
            print(f"FAILED {r['symbol']}: {r['error']}", file=sys.stderr)
        return 1
    print("Indian-market ingestion complete (use 'status' to verify).")
    return 0


def _cmd_live(args: argparse.Namespace) -> int:
    """Connect to FYERS live data, normalize + log events, then shut down.

    Does NOT place orders. Requires FYERS_CLIENT_ID + FYERS_ACCESS_TOKEN in env.
    If credentials are absent, exits with a clear, controlled message.
    """
    import time as _time
    from .data.provider_exports import get_provider

    provider = get_provider(args.provider or "fyers")
    if not getattr(provider, "is_authenticated", False):
        print("FYERS runtime verification blocked because credentials were not "
              "available (set FYERS_CLIENT_ID and FYERS_ACCESS_TOKEN).")
        return 1

    print(f"Connecting to FYERS live for: {args.symbols} (max {args.duration}s)...")
    received = {"n": 0}

    def on_event(ev):
        received["n"] += 1
        print(f"  [{ev.timestamp.isoformat()}] {ev.symbol} "
              f"ltp={ev.ltp} high={ev.high} low={ev.low} close={ev.close} vol={ev.volume}")

    try:
        sock = provider.connect_live(
            symbols=args.symbols.split(","), on_event=on_event,
            timeframe=args.timeframe or "1m", lite_mode=args.lite,
        )
    except Exception as e:
        print(f"LIVE CONNECT FAILED: {e}")
        return 1

    try:
        _time.sleep(args.duration)
    finally:
        sock.close()
    print(f"Live session ended. Events received: {received['n']}.")
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

    sub.add_parser("providers", help="List available market-data providers")
    sub.add_parser("instruments", help="List known Indian instruments + FYERS symbols")

    p_in = sub.add_parser("ingest-india", help="Ingest Indian-market historical data")
    p_in.add_argument("--symbols", required=True, help="Comma-separated INTERNAL symbols, e.g. NSE:RELIANCE,NSE:NIFTY50")
    p_in.add_argument("--timeframe", default="1d")
    p_in.add_argument("--limit", type=int, default=365)
    p_in.add_argument("--provider", default="fyers")

    p_lv = sub.add_parser("live", help="Connect FYERS live data (no orders placed)")
    p_lv.add_argument("--symbols", required=True, help="Comma-separated INTERNAL symbols")
    p_lv.add_argument("--timeframe", default="1m")
    p_lv.add_argument("--duration", type=int, default=15, help="seconds to run")
    p_lv.add_argument("--lite", action="store_true", help="Lite (LTP-only) mode")
    p_lv.add_argument("--provider", default="fyers")

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
    if args.command == "providers":
        return _cmd_providers(args)
    if args.command == "instruments":
        return _cmd_instruments(args)
    if args.command == "ingest-india":
        return _cmd_ingest_india(args)
    if args.command == "live":
        return _cmd_live(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
