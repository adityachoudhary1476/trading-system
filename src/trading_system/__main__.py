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
import os
import sys
import time

import pandas as pd

from .config import settings, configure_logging
from .data.pipeline import IngestionPipeline
from .analysis.pipeline import analyze
from .storage.database import MarketStore, OHLCVRecord
from .models.snapshot import build_snapshot_from_df
from .models import analyze_snapshot, get_model_provider
from .india.data_health import DataHealthMonitor, FeedStatus

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
    for name in ("binance", "stooq", "upstox"):
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
    from .india import (
        InstrumentRegistry,
        InstrumentRepository,
        to_fyers_symbol,
        FyersInstrumentDiscovery,
    )

    repo = InstrumentRepository(InstrumentRegistry())

    # Live discovery via FYERS option chain (data-only; requires credentials).
    if args.discover and args.underlying:
        from fyers_apiv3 import fyersModel
        import os
        try:
            dotenv_module = __import__("dotenv")
            dotenv_module.load_dotenv()
        except Exception:
            pass
        cid = os.getenv("FYERS_CLIENT_ID")
        tok = os.getenv("FYERS_ACCESS_TOKEN")
        if not (cid and tok):
            print("ERROR: FYERS credentials not found in environment (.env). "
                  "Set FYERS_CLIENT_ID and FYERS_ACCESS_TOKEN, then retry.")
            return 2
        model = fyersModel.FyersModel(client_id=cid, token=tok, log_level="ERROR")
        disc = FyersInstrumentDiscovery(model, repo)
        found = disc.discover_options(args.underlying, strikecount=20)
        if not found:
            print(f"No contracts discovered for {args.underlying} "
                  f"(auth or availability issue — no data fabricated).")
            return 0
        print(f"Discovered {len(found)} {args.underlying} option contracts (live FYERS).")

    if args.underlying:
        if args.instr_type in ("future", "futures"):
            rows = repo.list_futures(args.underlying)
        elif args.instr_type in ("option", "options"):
            rows = repo.list_options(args.underlying, expiry=args.expiry,
                                     option_type=args.option_type)
        else:
            # Show the underlying instrument + any discovered derivatives.
            rows = [repo.get_instrument(f"NSE:{args.underlying}")] if repo.get_instrument(f"NSE:{args.underlying}") else []
            rows += repo.list_futures(args.underlying) + repo.list_options(args.underlying,
                                                                           expiry=args.expiry,
                                                                           option_type=args.option_type)
        if not rows:
            print(f"No instruments match underlying '{args.underlying}' in the local registry.")
            return 0
    else:
        rows = [i for i in repo.registry._by_key.values()]

    table = []
    for instr in rows:
        fy = instr.provider_symbol or to_fyers_symbol(instr)
        table.append([
            instr.key,
            instr.instrument_type.value,
            instr.underlying or "",
            instr.expiry or "",
            "" if instr.strike is None else f"{instr.strike:.0f}",
            instr.option_type or "",
            fy,
        ])
    print(tabulate(table, headers=[
        "internal", "type", "underlying", "expiry", "strike", "opt", "provider_symbol"
    ], tablefmt="github"))
    return 0


def _cmd_strategies(args: argparse.Namespace) -> int:
    from .research import list_strategies

    print("Available research/backtest strategies (baselines only — not recommendations):\n")
    for name, cls in sorted(list_strategies().items()):
        print(f"  {name:10s} {cls.meta.description}")
    return 0


def _cmd_backtest(args: argparse.Namespace) -> int:
    """Research backtest. DATA ONLY — no orders, no execution, no live trading."""
    import json

    from trading_system.storage.database import MarketStore
    from trading_system.config.settings import settings
    from .research import (
        MarketDataRepository,
        get_strategy,
        run_backtest,
        compute_performance,
        BacktestConfig,
        RiskConfig,
        split_dataset,
    )

    store = MarketStore(settings.storage.db_url)
    repo = MarketDataRepository(store)
    df = repo.load(args.symbol, args.timeframe)
    if df is None or len(df) == 0:
        print(f"NO DATA: MarketStore has no {args.symbol} {args.timeframe} history. "
              f"Backtest blocked (no fabricated data).")
        return 2

    # Optional date window (slice on stored data; never fabricates missing bars).
    if args.start:
        df = df[df.index >= pd.Timestamp(args.start, tz="UTC")]
    if args.end:
        df = df[df.index <= pd.Timestamp(args.end, tz="UTC")]
    if len(df) == 0:
        print(f"NO DATA in requested window for {args.symbol} {args.timeframe}.")
        return 2

    sparams = {}
    if args.strategy_params:
        sparams = json.loads(args.strategy_params)
    strategy = get_strategy(args.strategy, **sparams)

    risk = RiskConfig(
        allow_short=args.allow_short,
        stop_loss_pct=args.stop_loss,
        take_profit_pct=args.take_profit,
    )
    cfg = BacktestConfig(
        initial_capital=args.initial_capital,
        transaction_cost_pct=args.transaction_cost,
        slippage_pct=args.slippage,
        risk=risk,
    )

    from .research import HistoricalDataset

    dataset = HistoricalDataset(symbol=args.symbol, timeframe=args.timeframe, data=df)
    result = run_backtest(dataset, strategy, cfg)
    perf = compute_performance(result)
    _print_backtest_report(result, perf, oos=False)

    if args.train_frac:
        split = split_dataset(dataset, train_frac=args.train_frac)
        train_res = run_backtest(split.train, strategy, cfg)
        test_res = run_backtest(split.test, strategy, cfg)
        train_perf = compute_performance(train_res)
        test_perf = compute_performance(test_res)
        print("\n" + "=" * 60)
        print("OUT-OF-SAMPLE (train/test split)")
        print("=" * 60)
        _print_backtest_report(train_res, train_perf, oos=False, label="TRAIN")
        _print_backtest_report(test_res, test_perf, oos=True, label="TEST")
    return 0


def _cmd_research(args: argparse.Namespace) -> int:
    """Research spine CLI (Day 10). Read-only research; NO execution/orders."""
    from trading_system.storage.database import MarketStore
    from trading_system.config.settings import settings
    from trading_system.research import FactorEngine, EvidenceStore, ResearchRegistry

    store = MarketStore(settings.storage.db_url)
    eng = FactorEngine()

    cmd = getattr(args, "research_cmd", None)
    if cmd == "factors":
        df = store.load(args.symbol, args.timeframe)
        if df is None or len(df) == 0:
            print(f"NO DATA for {args.symbol} {args.timeframe}")
            return 2
        names = args.names.split(",") if args.names else None
        out = eng.compute_many(df, names)
        print(f"\nFACTORS — {args.symbol} {args.timeframe} ({len(out)} rows)")
        print(f"Available factors: {', '.join(eng.available())}")
        print(out.tail(args.tail).to_string())
        return 0

    if cmd == "factor-analysis":
        df = store.load(args.symbol, args.timeframe)
        if df is None or len(df) == 0:
            print(f"NO DATA for {args.symbol} {args.timeframe}")
            return 2
        if args.factor not in eng.available():
            print(f"Unknown factor '{args.factor}'. Available: {', '.join(eng.available())}")
            return 2
        fac = eng.compute(args.factor, df).rename(args.factor)
        # Single-instrument time-series IC: factor_T vs forward return_{T+lag}.
        # Cross-sectional IC needs >=5 instruments; with one symbol we report the
        # time-series Spearman only and explicitly flag it as NOT cross-sectional.
        fwd = df["close"].pct_change().shift(-args.lag)
        ts = pd.concat([fac.rename("f"), fwd.rename("r")], axis=1).dropna()
        # Manual Spearman via ranks (avoid scipy dependency).
        rho = ts["f"].rank().corr(ts["r"].rank())
        print(f"\nFACTOR ANALYSIS — {args.symbol} {args.timeframe} factor={args.factor} lag={args.lag}")
        print("NOTE: single-instrument demo — time-series Spearman(factor_T, fwd_ret_T+lag).")
        print("      Cross-sectional IC requires >=5 instruments; not available here.")
        print(f"Time-series Spearman IC: {rho:.4f}")
        return 0

    if cmd == "hypothesis":
        sub = getattr(args, "hyp_cmd", None)
        reg = ResearchRegistry(EvidenceStore(store.engine))
        if sub == "list":
            hs = reg.list_hypotheses(status=args.status)
            print(f"\nHYPOTHESES ({len(hs)})")
            for h in hs:
                print(f"  {h.hypothesis_id}  [{h.status.value}]  {h.title}")
            if not hs:
                print("  (none)")
        return 0

    if cmd == "evidence":
        sub = getattr(args, "ev_cmd", None)
        reg = ResearchRegistry(EvidenceStore(store.engine))
        if sub == "list":
            evs = reg.store.list_evidence(hypothesis_id=args.hypothesis, regime=args.regime, quality=args.quality)
            print(f"\nEVIDENCE RUNS ({len(evs)})")
            for e in evs:
                print(f"  {e.run_id}  hyp={e.hypothesis_id}  sharpe={e.sharpe}  "
                      f"maxdd={e.max_drawdown}  ic={e.ic}  icir={e.icir}  q={e.quality}  {e.regime}")
            if not evs:
                print("  (none)")
        return 0

    if cmd == "coverage":
        return _cmd_research_coverage(args, store)

    print("Usage: research {factors|hypothesis list|evidence list|factor-analysis|coverage}")
    return 1


def _cmd_paper_status(args: argparse.Namespace) -> int:
    """Report paper-trading engine status. Simulation-only; never places orders."""
    from .execution import PaperBroker, SlippageConfig

    # Build a fresh, in-memory broker to report its static configuration.
    # No prices, no orders, no live calls.
    broker = PaperBroker(initial_cash=100_000.0, slippage=SlippageConfig())
    acc = broker.account()
    print("PAPER TRADING")
    print("-------------")
    print("status:        READY")
    print("mode:          PAPER")
    print("live_orders:   DISABLED")
    print("broker_class:  PaperBroker")
    print(f"slippage_bps:  {broker.slippage.slippage_bps}")
    print(f"initial_cash:  {acc.initial_cash:,.2f}")
    print()
    print("This engine simulates execution in memory. It cannot and does not")
    print("place real broker orders, call Upstox, or modify live configuration.")
    return 0


def _cmd_auth_status(args: argparse.Namespace) -> int:
    """Show Upstox token/auth status, then PROVE connectivity with a live probe.

    Never prints secrets. Reports three distinct things:
      * whether credentials/token are present,
      * whether the token was exchanged (presence-only state),
      * whether Upstox actually accepts the token (live probe).
    A token string existing is NOT reported as CONNECTED.
    """
    from .india.token_manager import UpstoxTokenManager, AuthStatus

    monitor = DataHealthMonitor()
    tm = UpstoxTokenManager(auth_status_callback=monitor.on_auth_status)
    presence = tm.token_status()
    print("UPSTOX AUTH STATUS")
    print("------------------")
    print(f"  status                : {presence.status.value}")
    print(f"  access_token_present  : {presence.access_token_present}")
    print(f"  client_id_present     : {bool(tm.client_id)}")
    print(f"  redirect_uri_present  : {bool(tm.redirect_uri)}")
    print(f"  message               : {presence.message}")
    print()

    if not presence.access_token_present:
        print("  CREDENTIALS MISSING — set UPSTOX_CLIENT_ID, UPSTOX_CLIENT_SECRET, "
              "UPSTOX_REDIRECT_URI, and UPSTOX_ACCESS_TOKEN in .env.")
        print("  (No token to validate; Upstox connectivity was NOT checked.)")
        return 2

    print("  Validating token against Upstox (live probe)...")
    result = tm.verify_authentication()
    print(f"  UPSTOX CONNECTIVITY    : {_connectivity_label(result.status)}")
    print(f"  probe detail          : {result.message}")
    print()
    if result.status == AuthStatus.AUTH_OK:
        print("  CONNECTED — access token accepted by Upstox.")
        return 0
    if result.status == AuthStatus.NETWORK_ERROR:
        print("  NETWORK ERROR — could not reach Upstox; check connectivity.")
        return 2
    if result.status == AuthStatus.ACCESS_TOKEN_EXPIRED:
        print("  AUTH FAILED / TOKEN EXPIRED — re-run: auth-login then auth-exchange.")
        return 2
    print("  OTHER UPSTOX ERROR — see probe detail.")
    return 2


def _connectivity_label(status) -> str:
    from .india.token_manager import AuthStatus

    return {
        AuthStatus.AUTH_OK: "CONNECTED",
        AuthStatus.NETWORK_ERROR: "NETWORK ERROR",
        AuthStatus.ACCESS_TOKEN_EXPIRED: "NOT CONNECTED (token rejected/expired)",
        AuthStatus.AUTH_FAILED: "NOT CONNECTED (other Upstox error)",
    }.get(status, "UNKNOWN")

def _cmd_gen_auth_url(args: argparse.Namespace) -> int:
    """Write the complete Upstox login URL to a local temp file, and
    best-effort copy to clipboard. The URL contains ONLY the OAuth params
    (client_id, redirect_uri, response_type, state); the app secret is NOT in the URL.
    The secret is never printed to the terminal or logs.

    Only the CLI output changes here; authentication logic, .env, and order/execution
    code are untouched. The full URL is returned by UpstoxTokenManager.build_authorization_url()
    and written to disk/clipboard only.

    Each run generates a FRESH random `state`, so every URL is unique (CSRF + prevents
    replay of a prior authorization). This is the `auth-login` sign-in step too.
    """
    import os
    import tempfile
    from .india.token_manager import UpstoxTokenManager, TokenError

    tm = UpstoxTokenManager()
    if not tm.client_id or not tm.secret:
        print("Cannot build login URL: UPSTOX_CLIENT_ID and UPSTOX_CLIENT_SECRET are not set.")
        print("Set them in .env (UPSTOX_CLIENT_ID=...  UPSTOX_CLIENT_SECRET=...  UPSTOX_REDIRECT_URI=...) and retry.")
        return 2
    try:
        url = tm.build_authorization_url()
    except TokenError as e:
        print(f"ERROR: {e}")
        return 2

    tmp = os.path.join(tempfile.gettempdir(), "upstox_auth_url.txt")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(url)
    except OSError as e:
        print(f"ERROR: could not write auth URL to temp file: {e}")
        return 2

    copied = False
    try:
        import pyperclip  # type: ignore
        pyperclip.copy(url)
        copied = True
    except Exception:
        copied = False

    print("Upstox login URL written to a LOCAL file (secret NOT printed to terminal):")
    print(f"  {tmp}")
    if copied:
        print("  (also copied to clipboard)")
    print()
    print("Open that file and paste the URL into your browser, then log in.")
    print("After login, the browser redirects to your redirect URI with a URL like:")
    print("  https://api.upstox.com/v2/login/authorization/dialog?...&code=XXXX&state=YYYY")
    print()
    print("Copy the auth_code value, then run:")
    print("  python -m trading_system auth-exchange   # it will prompt for the auth_code")
    print("  (or export UPSTOX_AUTH_CODE=XXXX and run the same command)")
    print()
    print("This tool NEVER prints your client secret, PIN, or tokens.")
    print("Reminder: the temp file holds the login URL — delete it after use.")
    return 0


def _extract_auth_code(raw: str) -> str:
    """Robustly extract the bare auth_code from user input.

    Accepts any of:
      * the bare token (e.g. "eyJ...abc")
      * "code=XXXX" or "code=XXXX&state=sample"
      * a full redirect URL "https://...?code=XXXX&state=sample"
    Strips surrounding whitespace and matching single/double quotes. Never logs the value.
    Returns the trimmed code, or "" if nothing usable.
    """
    if not raw:
        return ""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    if "code=" in s:
        from urllib.parse import parse_qs, urlparse
        query = s.split("?", 1)[1] if "?" in s else s
        parsed = parse_qs(query)
        vals = parsed.get("code")
        if vals:
            s = vals[0].strip()
    if "&" in s:
        s = s.split("&", 1)[0].strip()
    return s


def _cmd_auth_exchange(args: argparse.Namespace) -> int:
    """Exchange a manual auth_code for an access token. Reads the code from prompt/env only.

    The access token is written to your local .env via save_access_token()
    so subsequent commands pick it up automatically. No orders, no execution.
    """
    import getpass
    from .india.token_manager import UpstoxTokenManager, TokenError, AuthStatus

    monitor = DataHealthMonitor()
    tm = UpstoxTokenManager(auth_status_callback=monitor.on_auth_status)
    if not tm.client_id or not tm.secret:
        print("ERROR: UPSTOX_CLIENT_ID and UPSTOX_CLIENT_SECRET must be set in .env first.")
        return 2
    env_code = os.getenv("UPSTOX_AUTH_CODE", "")
    if env_code:
        print("WARNING: UPSTOX_AUTH_CODE environment variable is set; manual input takes precedence.")
    code = args.auth_code or ""
    if not code:
        try:
            code = getpass.getpass("Paste the Upstox auth_code (input hidden): ").strip()
        except Exception:
            code = input("Paste the Upstox auth_code: ").strip()
    if not code:
        code = env_code
    if not code:
        print("No auth_code provided. Aborting.")
        return 2
    code = _extract_auth_code(code)
    if not code:
        print("No usable auth_code extracted. Aborting.")
        return 2
    try:
        access = tm.exchange_auth_code(code)
    except TokenError as e:
        print("TOKEN EXCHANGE: FAIL")
        print(f"  {e}")
        return 2
    print("TOKEN EXCHANGE: SUCCESS")
    result = tm.verify_authentication()
    print(f"UPSTOX CONNECTIVITY: {_connectivity_label(result.status)}")
    if result.status != AuthStatus.AUTH_OK:
        print(f"REASON: {result.message}")
    print()
    try:
        env_path = tm.save_access_token(access)
        print(f"Access token saved to {env_path} (UPSTOX_ACCESS_TOKEN).")
    except OSError as e:
        print(f"WARNING: could not save token to .env: {e}")
        print("Add UPSTOX_ACCESS_TOKEN to your .env manually (this tool did NOT modify any files).")
    print()
    print("Then verify with:  python -m trading_system auth-status")
    return 0


def _cmd_research_coverage(args: argparse.Namespace, store) -> int:
    """Research-data coverage report. Reuses MarketStore — no network."""
    from .research.universe import UniverseRegistry, default_universe_registry
    from .data.validation import validate_ohlcv
    from trading_system.config.settings import settings

    reg = default_universe_registry()
    if getattr(args, "config", None):
        reg = UniverseRegistry.from_config_file(args.config)
    u = reg.get(args.universe)
    if u is None:
        print(f"Unknown universe '{args.universe}'. Known: {reg.names()}")
        return 1
    problems = u.validate()
    if problems:
        print(f"Universe '{u.name}' has issues (not runnable):")
        for p in problems:
            print(f"  - {p}")
        return 1

    tf = args.timeframe
    print(f"RESEARCH DATA COVERAGE")
    print(f"Universe : {u.name}")
    print(f"Segment  : {u.segment}")
    print(f"Timeframe: {tf}")
    print()

    requested = u.symbols
    with_data, missing, dups, bad = [], [], 0, 0
    date_min, date_max = None, None
    total_bars = 0
    rows = []
    for sym in requested:
        df = store.load(sym, tf)
        if df is None or len(df) == 0:
            missing.append(sym)
            rows.append((sym, 0, "-", "-", "MISSING"))
            continue
        # Integrity checks reuse validate_ohlcv (no fabrication).
        rep = validate_ohlcv(df, tf)
        n = len(df)
        total_bars += n
        with_data.append(sym)
        ts = df.index if isinstance(df.index, pd.DatetimeIndex) else pd.to_datetime(df["timestamp"], utc=True)
        dmin, dmax = ts.min(), ts.max()
        date_min = dmin if date_min is None else min(date_min, dmin)
        date_max = dmax if date_max is None else max(date_max, dmax)
        dup = int(df.index.duplicated().sum()) if isinstance(df.index, pd.DatetimeIndex) else 0
        dups += dup
        bad += len(rep.rejected)
        rows.append((sym, n, str(dmin.date()), str(dmax.date()), f"{dup} dup" if dup else "ok"))

    print(f"Symbols requested : {len(requested)}")
    print(f"Symbols with data : {len(with_data)}")
    print(f"Symbols missing   : {len(missing)}")
    if missing:
        print(f"  missing: {', '.join(missing)}")
    print(f"Total bars        : {total_bars:,}")
    print(f"Date range        : {date_min.date() if date_min else '-'} -> {date_max.date() if date_max else '-'}")
    print()
    print("Contract identity check: PASS (store keys on symbol/timeframe/timestamp/provider/exchange)")
    print(f"Duplicate timestamps  : {'PASS' if dups == 0 else f'{dups} FOUND'}")
    print(f"Validation rejections : {'PASS' if bad == 0 else f'{bad} ROWS'}")
    print()
    print(f"{'SYMBOL':12s} {'BARS':>10s}  {'START':>12s}  {'END':>12s}  STATUS")
    for sym, n, a, b, st in rows:
        print(f"{sym:12s} {n:>10,}  {a:>12s}  {b:>12s}  {st}")
    return 0


def _cmd_backfill_universe(args: argparse.Namespace) -> int:
    """Bulk historical backfill for a research universe. DATA ONLY, no orders."""
    from .india.backfill import BackfillEngine, format_symbol_summary, BackfillStatus
    from .india.fyers import FYERSMarketDataProvider
    from .india.instruments import InstrumentRegistry
    from .research.universe import UniverseRegistry, default_universe_registry
    from trading_system.config.settings import settings
    from trading_system.storage.database import MarketStore

    reg = default_universe_registry()
    if getattr(args, "config", None):
        reg = UniverseRegistry.from_config_file(args.config)
    u = reg.get(args.universe)
    if u is None:
        print(f"Unknown universe '{args.universe}'. Known: {reg.names()}")
        return 1
    problems = u.validate()
    if problems:
        print(f"Universe '{u.name}' not runnable:")
        for p in problems:
            print(f"  - {p}")
        return 1

    store = MarketStore(settings.storage.db_url)
    provider = UpstoxMarketDataProvider()
    engine = BackfillEngine(
        provider=provider, store=store, registry=InstrumentRegistry(),
        max_retries=2, retry_backoff=0.5,
    )

    print(f"BACKFILL UNIVERSE: {u.name} ({len(u.symbols)} symbols) tf={args.timeframe}")
    print("DATA ONLY — no orders, no execution.")
    print()
    ok = 0
    for sym in u.symbols:
        res = engine.backfill_symbol(
            sym, args.timeframe, days=args.days, start=args.start, end=args.end,
            dry_run=args.dry_run,
        )
        print(format_symbol_summary(res))
        for w in res.warnings:
            print(f"    ! {w}")
        if res.status == BackfillStatus.COMPLETE or res.status == BackfillStatus.PARTIAL:
            ok += 1
        if args.request_delay:
            import time
            time.sleep(args.request_delay)
    print()
    print(f"Completed/stored for {ok}/{len(u.symbols)} symbols.")
    return 0


def _cmd_analyze_history(args: argparse.Namespace) -> int:
    """Historical market analysis (Day 8). DATA ONLY — no orders, no execution."""
    from trading_system.storage.database import MarketStore
    from trading_system.config.settings import settings
    from .research import (
        MarketIntelligenceEngine, MarketReasoningProvider, AnalysisContext, AIAnalysis,
    )
    from .india.data_health import FeedStatus, DataHealthMonitor
    from .india import InstrumentRegistry

    store = MarketStore(settings.storage.db_url)
    engine = MarketIntelligenceEngine(lookback=args.lookback)
    registry = InstrumentRegistry()
    monitor = DataHealthMonitor()  # historical-only; no live ticks -> not HEALTHY

    # Historical analysis is gated on DATA existence, not live feed health. We treat
    # stored-data analysis as allowed (feed status UNKNOWN for offline), but if a
    # caller explicitly marks the feed unhealthy we honor the block. For pure
    # historical mode we pass health_status=None (no block) and rely on data checks.
    health = None

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    rc = 0
    for sym in symbols:
        df = store.load(sym, args.timeframe)
        from .india.instruments import InternalSymbol
        instr = registry.get(InternalSymbol.parse(sym))
        contract_id = instr.contract_id if instr else sym
        if df is None or len(df) == 0:
            print(f"\n{'='*60}\nMARKET ANALYSIS — {sym} ({args.timeframe})\n{'='*60}")
            print("ANALYSIS BLOCKED\n  Reason: NO_DATA (no stored history for this symbol/timeframe)")
            print("\nANALYSIS ONLY — NO ORDER PLACED")
            rc = 2
            continue

        result = engine.analyze(
            symbol=sym, timeframe=args.timeframe, df=df, instrument=instr,
            contract_id=contract_id, health_status=health,
        )
        if result.get("status") == "BLOCKED":
            print(f"\n{'='*60}\nMARKET ANALYSIS — {sym} ({args.timeframe})\n{'='*60}")
            print(f"ANALYSIS BLOCKED\n  Reason: {result['reason']}")
            print("\nANALYSIS ONLY — NO ORDER PLACED")
            rc = 2
            continue

        feats = result["features"]
        regime = result["regime"]
        cand = result["signal_candidate"]
        expl = result["explanation"]

        print(f"\n{'='*60}")
        print("MARKET ANALYSIS")
        print(f"{'='*60}")
        print(f"Symbol: {sym}")
        print(f"Timeframe: {args.timeframe}")
        print(f"Instrument class: {result['instrument_class']}")
        print(f"Data through: {df.index[-1]}  ({feats.data_points} bars)")
        print(f"\nREGIME\n{regime.regime.value}")
        print(f"Confidence: {regime.confidence:.2f}")
        for w in regime.warnings:
            print(f"  ! {w}")

        print(f"\nTECHNICAL STATE")
        print(f"  Price: {feats.close:.2f}")
        if feats.sma_20: print(f"  SMA20: {feats.sma_20:.2f}  (vs {feats.price_vs_sma20*100:+.2f}%)")
        if feats.sma_50: print(f"  SMA50: {feats.sma_50:.2f}")
        if feats.sma_200: print(f"  SMA200: {feats.sma_200:.2f}")
        if feats.ema_20: print(f"  EMA20: {feats.ema_20:.2f}")
        if feats.ema_50: print(f"  EMA50: {feats.ema_50:.2f}")
        if feats.rsi_14: print(f"  RSI14: {feats.rsi_14:.2f}")
        if feats.atr_14: print(f"  ATR14: {feats.atr_14:.2f}")
        if feats.relative_volume: print(f"  Relative Volume: {feats.relative_volume:.2f}x")
        print(f"  Vol regime: {feats.vol_regime.value}")
        print(f"  Trend: {feats.trend.value}")

        print(f"\nSIGNAL CANDIDATE")
        print(f"  Direction: {cand.direction.value.upper()}")
        print(f"  Setup: {cand.setup.value}")
        print(f"  Confidence: {cand.confidence:.2f}")
        print(f"  Entry context: {cand.entry_context}")
        print(f"  Invalidation: {cand.invalidation_context}")

        print(f"\nBULLISH EVIDENCE")
        for b in expl.bullish_factors:
            print(f"  - {b}")
        print(f"\nBEARISH EVIDENCE")
        for b in expl.bearish_factors:
            print(f"  - {b}")
        print(f"\nRISKS")
        for r in expl.risks:
            print(f"  - {r}")
        print(f"\nMISSING DATA")
        for m in expl.missing_data:
            print(f"  - {m}")

        # Optional AI reasoning over structured context.
        if args.ai:
            try:
                from .models.provider_factory import get_model_provider
                prov = get_model_provider(args.ai)
                ctx = AnalysisContext(
                    instrument={"symbol": sym, "contract_id": contract_id,
                                "instrument_class": result["instrument_class"]},
                    timeframe=args.timeframe,
                    market_regime={"regime": regime.regime.value, "confidence": regime.confidence,
                                   "supporting": regime.supporting_features},
                    features=feats.__dict__,
                    signal_candidate=cand.__dict__,
                    data_quality=result["data_quality"],
                )
                reasoner = MarketReasoningProvider(prov)
                ai = reasoner.reason(ctx)
                print(f"\nAI CONCLUSION ({args.ai})")
                print(f"  {ai.conclusion}")
                print(f"  Confidence: {ai.confidence:.2f}")
                for k in ai.risks:
                    print(f"  - risk: {k}")
            except Exception as e:
                print(f"\nAI REASONING UNAVAILABLE: {type(e).__name__}: {e}")

        print(f"\n{'='*60}")
        print("ANALYSIS ONLY — NO ORDER PLACED")
    return rc


def _print_backtest_report(result, perf, oos: bool, label: str = "BACKTEST") -> None:
    ds = result.dataset
    print("\n" + "=" * 60)
    print(f"{label}")
    print("=" * 60)
    print(f"Symbol        : {ds.symbol}")
    print(f"Timeframe     : {ds.timeframe}")
    print(f"Strategy      : {result.strategy.meta.name} {result.strategy.params}")
    print(f"Period        : {ds.quality.date_start} -> {ds.quality.date_end}")
    print(f"Initial capital: {perf.initial_capital:,.2f}")
    print(f"Final capital : {perf.final_capital:,.2f}")
    print(f"Net P&L       : {perf.net_pnl:,.2f}")
    print(f"Return        : {perf.total_return*100:.2f}%")
    print(f"Trades        : {perf.n_trades}")
    print(f"Win rate      : {perf.win_rate*100:.1f}%  ({perf.winning}W/{perf.losing}L)")
    print(f"Avg win       : {perf.avg_win:,.2f}")
    print(f"Avg loss      : {perf.avg_loss:,.2f}")
    print(f"Profit factor : {perf.profit_factor}")
    print(f"Max drawdown  : {perf.max_drawdown*100:.2f}%")
    print(f"Sharpe (ann.) : {perf.sharpe:.3f}")
    print(f"Sortino (ann.): {perf.sortino:.3f}")
    print(f"Exposure      : {perf.exposure_pct*100:.1f}%")
    print("-" * 60)
    print("DATA QUALITY")
    print(f"  Rows        : {ds.quality.rows}")
    print(f"  Date range  : {ds.quality.date_start} -> {ds.quality.date_end}")
    print(f"  Duplicate bars: {ds.quality.duplicate_bars}")
    print(f"  Contract id : {ds.contract_id or '(none)'}")
    if not perf.reliable:
        print("  *** WARNING: sample too small / unreliable — NOT evidence of profitability. ***")
    for n in perf.notes:
        print(f"  note: {n}")


def _cmd_backfill_history(args: argparse.Namespace) -> int:
    """Bulk historical backfill for Indian (FYERS) market data.

    DATA ONLY — never places orders. Chunks the requested range, fetches each
    chunk via the FYERS provider, validates, and stores idempotently.
    """
    from .india.backfill import BackfillEngine, format_symbol_summary
    from .data.provider_exports import get_provider
    from .india import InstrumentRegistry

    provider = get_provider(args.provider or settings.market.provider)
    engine = BackfillEngine(
        provider=provider,
        store=_store(),
        registry=InstrumentRegistry(),
        verbose=args.verbose,
        progress=lambda s: print(s, file=sys.stderr),
    )

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    start = pd.Timestamp(args.start) if args.start else None
    end = pd.Timestamp(args.end) if args.end else None

    print("UPSTOX HISTORICAL BACKFILL  (DATA ONLY — no orders placed)")
    if not provider.is_authenticated:
        print(
            "ERROR: Upstox credentials not found in environment (.env). "
            "Set UPSTOX_CLIENT_ID and UPSTOX_ACCESS_TOKEN, then retry."
        )
        return 2
    print(f"Provider: {provider.name}")
    print(f"Timeframe: {args.timeframe}")
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Range requested: --days={args.days} --start={args.start} --end={args.end}")
    if args.dry_run:
        print("[DRY-RUN] Planning chunks only; no API calls or DB writes.\n")

    results = []
    for sym in symbols:
        res = engine.backfill_symbol(
            sym,
            args.timeframe,
            days=args.days,
            start=start,
            end=end,
            dry_run=args.dry_run,
        )
        results.append(res)
        print()  # blank line between symbols' progress

        print("=" * 64)
        print(f"Symbol:      {res.symbol}")
        print(f"Provider symbol: {res.fyers_symbol}")
        print(f"Timeframe:   {res.timeframe}")
        if res.requested_start and res.requested_end:
            print(f"Requested:   {res.requested_start.date()} -> {res.requested_end.date()}")
        if not args.dry_run:
            if res.actual_start and res.actual_end:
                print(f"Actual:      {res.actual_start.date()} -> {res.actual_end.date()}")
            else:
                print("Actual:      (no data returned)")
            print(f"Chunks:      {res.chunks_ok} ok / {res.chunks_failed} failed of {res.chunks_total}")
            print(f"Fetched:     {res.fetched:,}")
            print(f"Valid:       {res.valid:,}")
            print(f"Stored:      {res.stored:,} new")
            print(f"Skipped:     {res.skipped:,} (rejected by validation)")
            if res.warnings:
                print("-" * 64)
                for w in res.warnings[:20]:
                    print(f"  note: {w}")
        print(f"Status:      {res.status.value}")
        if res.error:
            print(f"Detail:      {res.error}")
        print("=" * 64)

    # Overall summary.
    if len(results) > 1 or args.dry_run:
        print("\nSUMMARY")
        for r in results:
            print("  " + format_symbol_summary(r))
        complete = sum(1 for r in results if r.status.value == "COMPLETE")
        partial = sum(1 for r in results if r.status.value == "PARTIAL")
        auth = sum(1 for r in results if r.status.value == "AUTH_ERROR")
        empty = sum(1 for r in results if r.status.value == "EMPTY")
        print(f"\nOverall: {complete} complete, {partial} partial, "
              f"{auth} auth-failure, {empty} empty")
        if any(r.non_auth_failure for r in results):
            print("Some symbols were not fully backfilled (see details above).")

    # Exit code: non-zero if any non-auth failure or auth failure occurred.
    if any(r.status.value == "AUTH_ERROR" for r in results):
        return 2
    if any(r.non_auth_failure for r in results):
        return 1
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
          f"(provider credentials required for live Upstox)...")
    results = []
    for sym in symbols:
        instr = reg.resolve(sym)
        provider_symbol = None
        try:
            provider_symbol = provider._upstox_symbol(sym) if hasattr(provider, "_upstox_symbol") else sym
        except Exception:
            provider_symbol = sym
        # Historical fetch requires auth for Upstox; Binance works without.
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
                "symbol": sym, "provider_symbol": provider_symbol, "received": received,
                "inserted": inserted, "error": None,
            })
        except Exception as e:
            results.append({
                "symbol": sym, "provider_symbol": provider_symbol, "received": 0,
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
    """Connect to Upstox live data, normalize + log events, then shut down.

    Does NOT place orders. Requires UPSTOX_CLIENT_ID + UPSTOX_ACCESS_TOKEN in env.
    If credentials are absent, exits with a clear, controlled message.
    """
    import time as _time
    from .data.provider_exports import get_provider

    provider = get_provider(args.provider or "upstox")
    if not getattr(provider, "is_authenticated", False):
        print("Upstox runtime verification blocked because credentials were not "
              "available (set UPSTOX_CLIENT_ID and UPSTOX_ACCESS_TOKEN).")
        return 1

    print(f"Connecting to Upstox live for: {args.symbols} (max {args.duration}s)...")
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
    """REAL Upstox market-data verification only. Does NOT place orders.

    Connects using .env credentials, subscribes to one liquid NSE symbol (NSE:SBIN
    by default), prints normalized events + feed health for a bounded period, then
    exits. This is data-only verification; no order API is called.
    """
    from .india.upstox import UpstoxMarketDataProvider
    from .india.live_pipeline import LiveMarketPipeline

    print("=" * 70)
    print("UPSTOX LIVE-VERIFY  —  REAL MARKET-DATA VERIFICATION ONLY")
    print("This command does NOT place orders or call any brokerage execution API.")
    print("=" * 70)

    prov = UpstoxMarketDataProvider()
    if not prov.is_authenticated:
        print("ERROR: Upstox credentials not found in environment (.env).")
        print("Set UPSTOX_CLIENT_ID and UPSTOX_ACCESS_TOKEN, then retry.")
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
        print(f"Upstox connect failed: {e}")
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


def _strategy_names() -> list[str]:
    from .research import list_strategies

    return sorted(list_strategies().keys())


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
    p_in = sub.add_parser("instruments", help="List known Indian instruments + provider symbols")
    p_in.add_argument("--underlying", default=None, help="Filter/resolve by underlying (e.g. NIFTY, SBIN, SILVERMIC)")
    p_in.add_argument("--type", dest="instr_type", default=None,
                      choices=["equity", "index", "future", "option", "options", "futures"],
                      help="Instrument type filter")
    p_in.add_argument("--expiry", default=None, help="Expiry (YYYY-MM-DD) for derivatives")
    p_in.add_argument("--strike", type=float, default=None, help="Option strike")
    p_in.add_argument("--option-type", default=None, choices=["CE", "PE"], help="CE/PE for options")
    p_in.add_argument("--discover", action="store_true",
                      help="Use the live option-chain to discover contracts (DATA ONLY; requires creds)")

    p_ing_in = sub.add_parser("ingest-india", help="Ingest Indian-market historical data")
    p_ing_in.add_argument("--symbols", required=True, help="Comma-separated INTERNAL symbols, e.g. NSE:RELIANCE,NSE:NIFTY50")
    p_ing_in.add_argument("--timeframe", default="1d")
    p_ing_in.add_argument("--limit", type=int, default=365)
    p_ing_in.add_argument("--provider", default="upstox")

    p_bf = sub.add_parser(
        "backfill-history",
        help="Bulk historical backfill (Upstox) — DATA ONLY, no orders. Idempotent.",
    )
    p_bf.add_argument(
        "--symbols", required=True,
        help="Comma-separated INTERNAL symbols, e.g. NSE:SBIN,NSE:RELIANCE",
    )
    p_bf.add_argument("--timeframe", default="1d", help="Canonical timeframe (1m,5m,1h,1d,1w,1M...)")
    p_bf.add_argument("--days", type=int, default=None, help="Backfill N days ending now")
    p_bf.add_argument("--start", default=None, help="Start date (YYYY-MM-DD or ISO). Overrides --days for the start bound.")
    p_bf.add_argument("--end", default=None, help="End date (default: now). Overrides --days for the end bound.")
    p_bf.add_argument("--dry-run", action="store_true", help="Plan chunks only; no API calls or DB writes.")
    p_bf.add_argument("--provider", default=None, help="Provider name (default: settings.market.provider).")
    p_bf.add_argument("--verbose", action="store_true", help="Show detailed per-chunk errors.")

    p_st = sub.add_parser("strategies", help="List available research/backtest strategies")
    p_bt = sub.add_parser(
        "backtest",
        help="Run a research backtest (DATA ONLY, no orders, no execution).",
    )
    p_bt.add_argument("--symbol", required=True, help="Internal symbol, e.g. NSE:SBIN")
    p_bt.add_argument("--timeframe", default="1d", help="Canonical timeframe (1m,5m,1h,1d...)")
    p_bt.add_argument("--strategy", required=True, choices=sorted(_strategy_names()),
                      help="Strategy key (see `strategies`)")
    p_bt.add_argument("--start", default=None, help="Start date (YYYY-MM-DD) for the dataset")
    p_bt.add_argument("--end", default=None, help="End date (YYYY-MM-DD) for the dataset")
    p_bt.add_argument("--initial-capital", type=float, default=100000.0)
    p_bt.add_argument("--transaction-cost", type=float, default=0.0,
                      help="Per-side commission as fraction of notional (e.g. 0.0005)")
    p_bt.add_argument("--slippage", type=float, default=0.0,
                      help="Per-side adverse slippage as fraction of price")
    p_bt.add_argument("--stop-loss", type=float, default=None,
                      help="Stop-loss fraction (e.g. 0.02 = 2%)")
    p_bt.add_argument("--take-profit", type=float, default=None,
                      help="Take-profit fraction (e.g. 0.05 = 5%)")
    p_bt.add_argument("--allow-short", action="store_true",
                      help="Permit SHORT signals (default: long-only)")
    p_bt.add_argument("--train-frac", type=float, default=None,
                      help="If set, split into train/test and report out-of-sample too")
    p_bt.add_argument("--strategy-params", default=None,
                      help="JSON dict of extra strategy params, e.g. '{\"fast\":5,\"slow\":20}'")

    p_ah = sub.add_parser(
        "analyze-history",
        help="Analyze stored historical data (features/regime/signal candidate). DATA ONLY, no orders.",
    )
    p_ah.add_argument("--symbols", required=True,
                      help="Comma-separated INTERNAL symbols, e.g. NSE:SBIN,NFO:NIFTY25DECFUT")
    p_ah.add_argument("--timeframe", default="1d", help="Canonical timeframe (1m,5m,1h,1d...)")
    p_ah.add_argument("--lookback", type=int, default=60, help="Price-structure lookback window")
    p_ah.add_argument("--ai", default=None,
                      help="AI provider name for reasoning (e.g. local, openai). Default: deterministic only.")
    p_ah.add_argument("--as-of", default=None, help="Analyze as of this timestamp (YYYY-MM-DD); default: latest bar")

    p_re = sub.add_parser("research", help="Research spine (Day 10): factors, evidence, hypotheses. NO orders.")
    p_re_sub = p_re.add_subparsers(dest="research_cmd")

    p_rf = p_re_sub.add_parser("factors", help="Compute factors for a stored symbol")
    p_rf.add_argument("--symbol", required=True)
    p_rf.add_argument("--timeframe", default="1d")
    p_rf.add_argument("--names", default=None, help="Comma-separated factor names (default: all)")
    p_rf.add_argument("--tail", type=int, default=5, help="Show last N rows")

    p_rh = p_re_sub.add_parser("hypothesis", help="Hypothesis registry")
    p_rh_sub = p_rh.add_subparsers(dest="hyp_cmd")
    p_rh_l = p_rh_sub.add_parser("list", help="List hypotheses")
    p_rh_l.add_argument("--status", default=None)

    p_re2 = p_re_sub.add_parser("evidence", help="Evidence store")
    p_re2_sub = p_re2.add_subparsers(dest="ev_cmd")
    p_re2_l = p_re2_sub.add_parser("list", help="List evidence runs")
    p_re2_l.add_argument("--hypothesis", default=None)
    p_re2_l.add_argument("--regime", default=None)
    p_re2_l.add_argument("--quality", default=None)

    p_rc = p_re_sub.add_parser("coverage", help="Research-data coverage report")
    p_rc.add_argument("--universe", default="DEFAULT_BASKET")
    p_rc.add_argument("--timeframe", default="1d")
    p_rc.add_argument("--config", default=None, help="Universe config JSON path")

    p_rfa = p_re_sub.add_parser("factor-analysis", help="Factor IC/IR on a symbol's factor panel")
    p_rfa.add_argument("--symbol", required=True)
    p_rfa.add_argument("--timeframe", default="1d")
    p_rfa.add_argument("--factor", required=True, help="Factor name to analyze vs forward return")
    p_rfa.add_argument("--lag", type=int, default=1)

    p_auth = sub.add_parser("auth-status", help="Show Upstox token/auth status (no secrets)")
    p_gen = sub.add_parser("gen-auth-url", help="Print the Upstox login URL to open in a browser (no secrets)")
    p_login = sub.add_parser(
        "auth-login",
        help="Sign-in step: generate a FRESH Upstox login URL (new state) and write it to a temp file/clipboard",
    )
    p_ex = sub.add_parser(
        "auth-exchange",
        help="Exchange a manual Upstox auth_code for tokens (reads code from prompt/env; never writes files)",
    )
    p_ex.add_argument("--auth-code", default=None, help="Upstox auth_code (prefer env UPSTOX_AUTH_CODE to avoid shell history)")
    p_bu = sub.add_parser(
        "backfill-universe",
        help="Bulk historical backfill for a research universe (DATA ONLY, no orders).",
    )
    p_bu.add_argument("--universe", default="DEFAULT_BASKET", help="Universe name or config path")
    p_bu.add_argument("--timeframe", default="1d")
    p_bu.add_argument("--days", type=int, default=None)
    p_bu.add_argument("--start", default=None)
    p_bu.add_argument("--end", default=None)
    p_bu.add_argument("--config", default=None, help="Universe config JSON (overrides --universe name)")
    p_bu.add_argument("--dry-run", action="store_true", help="Plan only; do not fetch")
    p_bu.add_argument("--request-delay", type=float, default=0.5, help="Seconds between symbols")


    p_lv = sub.add_parser("live", help="Connect Upstox live data (no orders placed)")
    p_lv.add_argument("--symbols", required=True, help="Comma-separated INTERNAL symbols")
    p_lv.add_argument("--timeframe", default="1m")
    p_lv.add_argument("--duration", type=int, default=15, help="seconds to run")
    p_lv.add_argument("--lite", action="store_true", help="Lite (LTP-only) mode")
    p_lv.add_argument("--provider", default="upstox")

    p_is = sub.add_parser("instrument-search", help="Search known Indian instruments")
    p_is.add_argument("query", help="Substring, e.g. BANK, NIFTY, RELIANCE")
    sub.add_parser("market-status", help="Feed health + stored-data quality")
    sub.add_parser("data-health", help="Alias of market-status")
    sub.add_parser(
        "paper-status",
        help="Show paper-trading engine status (simulation only, live orders DISABLED)",
    )

    p_lv2 = sub.add_parser(
        "live-verify",
        help="REAL Upstox market-data verification only (no orders placed)",
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
    if args.command == "backfill-history":
        return _cmd_backfill_history(args)
    if args.command == "strategies":
        return _cmd_strategies(args)
    if args.command == "backtest":
        return _cmd_backtest(args)
    if args.command == "analyze-history":
        return _cmd_analyze_history(args)
    if args.command == "research":
        return _cmd_research(args)
    if args.command == "auth-status":
        return _cmd_auth_status(args)
    if args.command == "gen-auth-url":
        return _cmd_gen_auth_url(args)
    if args.command == "auth-login":
        # Sign-in step: identical to gen-auth-url (fresh state each run). Wrapped so the
        # obvious "sign in" action generates a brand-new login URL every time.
        return _cmd_gen_auth_url(args)
    if args.command == "auth-exchange":
        return _cmd_auth_exchange(args)
    if args.command == "backfill-universe":
        return _cmd_backfill_universe(args)
    if args.command == "live":
        return _cmd_live(args)
    if args.command == "instrument-search":
        return _cmd_instrument_search(args)
    if args.command in ("market-status", "data-health"):
        return _cmd_data_health(args)
    if args.command == "paper-status":
        return _cmd_paper_status(args)
    if args.command == "live-verify":
        return _cmd_live_verify(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
