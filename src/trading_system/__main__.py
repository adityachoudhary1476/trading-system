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
import time

import pandas as pd

from .config import settings, configure_logging
from .data.pipeline import IngestionPipeline
from .analysis.pipeline import analyze
from .storage.database import MarketStore, OHLCVRecord
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


def _cmd_instrument_search(args: argparse.Namespace) -> int:
    from tabulate import tabulate
    from .india.instrument_repository import InstrumentRepository

    repo = InstrumentRepository()
    # Seed with the default registry so search works without a live master.
    for instr in repo.registry._by_key.values():
        repo.register(instr)
    results = repo.search_instruments(args.query)
    if not results:
        print(f"No instruments match '{args.query}' in the local registry.")
        return 0
    rows = [
        [i.key, i.instrument_type.value, i.provider_symbol or "-"]
        for i in results
    ]
    print(tabulate(rows, headers=["internal", "type", "fyers_symbol"], tablefmt="github"))
    return 0


def _cmd_data_health(args: argparse.Namespace) -> int:
    """Static data-health view: feed status (DISCONNECTED without a live session)
    plus stored-data quality from the DB."""
    from tabulate import tabulate
    from .india.data_health import FeedStatus
    from sqlalchemy import func, select

    store = _store()
    with store._Session() as s:
        rows = (
            s.execute(
                select(
                    OHLCVRecord.symbol, OHLCVRecord.timeframe,
                    OHLCVRecord.provider, OHLCVRecord.exchange,
                    func.count(), func.max(OHLCVRecord.timestamp),
                ).group_by(
                    OHLCVRecord.symbol, OHLCVRecord.timeframe,
                    OHLCVRecord.provider, OHLCVRecord.exchange,
                )
            )
            .tuples()
            .all()
        )
    print("Feed status : DISCONNECTED (no live session in CLI mode)")
    print("Data health :")
    if not rows:
        print("  (no stored data)")
    else:
        table = [
            [
                r[0], r[1], r[2], r[3] or "-", r[4],
                (pd.Timestamp(r[5]).tz_localize("UTC").date() if r[5] is not None else "-"),
            ]
            for r in rows
        ]
        print(tabulate(table, headers=["symbol", "tf", "provider", "exchange", "rows", "latest"], tablefmt="github"))
    return 0


def _cmd_live_verify(args: argparse.Namespace) -> int:
    """REAL FYERS market-data verification only. Does NOT place orders.

    Connects using .env credentials, subscribes to one liquid NSE symbol (NSE:SBIN
    by default), prints normalized events + feed health for a bounded period, then
    exits. This is data-only verification; no order API is called.
    """
    from .india.fyers import FYERSMarketDataProvider
    from .india.live_pipeline import LiveMarketPipeline

    print("=" * 70)
    print("FYERS LIVE-VERIFY  —  REAL MARKET-DATA VERIFICATION ONLY")
    print("This command does NOT place orders or call any brokerage execution API.")
    print("=" * 70)

    prov = FYERSMarketDataProvider()
    if not prov.is_authenticated:
        print("ERROR: FYERS credentials not found in environment (.env).")
        print("Set FYERS_CLIENT_ID and FYERS_ACCESS_TOKEN, then retry.")
        return 2

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    timeframe = args.timeframe
    duration = args.duration

    pipe = LiveMarketPipeline(symbols=symbols, timeframe=timeframe)
    events: list = []

    def on_event(ev):
        events.append(ev)
        print(
            f"  {ev.timestamp.isoformat()}  {ev.symbol:12s}  LTP={ev.ltp}"
            f"  provider={ev.provider_symbol}"
        )

    def on_snapshot(symbol, snap):
        print(f"  [snapshot] {symbol} @ {snap.timestamp.isoformat()} "
              f"latest={snap.latest_price}")

    pipe.on_snapshot(on_snapshot)
    pipe.start()

    try:
        socket = prov.connect_live(symbols, on_event=on_event, timeframe=timeframe)
    except Exception as e:
        print(f"FYERS connect failed: {e}")
        return 1
    pipe.attach_socket(socket)

    print(f"Subscribed to {symbols}; running for {duration}s (Ctrl-C to stop early)...")
    try:
        import time

        time.sleep(duration)
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    finally:
        pipe.stop()

    print("-" * 70)
    print(f"Events received : {len(events)}")
    print(f"Feed health     : {pipe.health_snapshot()}")
    print("Live-verify complete. No orders were placed.")
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

    p_is = sub.add_parser("instrument-search", help="Search known Indian instruments")
    p_is.add_argument("query", help="Substring, e.g. BANK, NIFTY, RELIANCE")
    sub.add_parser("market-status", help="Feed health + stored-data quality")
    sub.add_parser("data-health", help="Alias of market-status")

    p_lv2 = sub.add_parser(
        "live-verify",
        help="REAL FYERS market-data verification only (no orders placed)",
    )
    p_lv2.add_argument("--symbols", default="NSE:SBIN", help="Comma-separated INTERNAL symbols")
    p_lv2.add_argument("--timeframe", default="1m")
    p_lv2.add_argument("--duration", type=int, default=20, help="seconds to run")

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
    if args.command == "instrument-search":
        return _cmd_instrument_search(args)
    if args.command in ("market-status", "data-health"):
        return _cmd_data_health(args)
    if args.command == "live-verify":
        return _cmd_live_verify(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
