# AI-Assisted Market Analysis & Paper-Trading System

A modular, research-first system for ingesting market data, validating it,
storing it locally, and computing quantitative analysis and technical
indicators — built as the foundation for a future AI analyst and paper-trading
engine.

> **Day 1 scope.** This is the *foundation only*. It does **NOT** trade, does
> **NOT** connect to a broker, and does **NOT** execute orders. The AI is a
> planned *analyst/decision-support* component, not an autonomous trader.

---

## What it does today

- Pulls historical OHLCV data from a data provider (Binance, public REST, no key).
- Validates every row — missing/invalid/impossible values, bad OHLC, duplicate
  or out-of-order timestamps, abnormal gaps — and **fails loudly** rather than
  silently feeding bad data downstream.
- Stores validated data idempotently in SQLite (re-runs never duplicate rows).
- Computes foundational quant metrics (returns, volatility, drawdown, volume
  stats) and technical indicators (SMA, EMA, RSI, MACD, Bollinger, ATR).
- Structured logging and a CLI (`ingest`, `analyze`, `status`).

## Current data limitations

- **Primary target is now Indian markets (FYERS).** Binance remains the dev/test provider.
- FYERS historical (`/history`) is **not real-time** — it is for historical candles only.
- FYERS **pricing/data-feed fee is UNVERIFIED** as of Day 3; do not assume free. Requires an active FYERS account.
- Live WebSocket requires `FYERS_CLIENT_ID` + `FYERS_ACCESS_TOKEN` (OAuth2). Not tested live on Day 3 (no credentials in env).
- Only daily (`1d`) ingestion was exercised via fixtures; other intervals are supported by mapping but untested against the live API.
- SEBI algo rules: order placement (future work) requires a validated static IP; data-only use is unaffected.

## Historical backfill (bulk, idempotent, data-only)

Bulk-load historical OHLCV from FYERS into the local SQLite store. The range is
**automatically split** into chunks that respect FYERS per-request limits (≈100
days for minute resolutions, ≈366 for day/week/month), each chunk is fetched,
validated (corrupt rows are rejected, not stored), and persisted **idempotently**
(re-running inserts zero duplicate rows). This command is **data-only** — it never
places orders or calls any brokerage execution API.

```bash
# Last 10 years of daily candles for one symbol (auto-chunked, idempotent):
PYTHONPATH=src python -m trading_system backfill-history \
    --symbols NSE:SBIN --timeframe 1d --days 3650

# Multiple symbols, intraday:
PYTHONPATH=src python -m trading_system backfill-history \
    --symbols NSE:SBIN,NSE:RELIANCE --timeframe 5m --days 100

# Explicit date range (precedence: --start/--end override --days for their bound):
PYTHONPATH=src python -m trading_system backfill-history \
    --symbols NSE:SBIN --timeframe 1d --start 2020-01-01 --end 2024-01-01

# Dry-run: plan the chunks only — no API calls, no DB writes:
PYTHONPATH=src python -m trading_system backfill-history \
    --symbols NSE:SBIN --timeframe 1d --days 3650 --dry-run
```

Notes:
- **FYERS credentials must exist in `.env`** (`FYERS_CLIENT_ID`,
  `FYERS_ACCESS_TOKEN`). Without them the command exits with a clear, controlled
  message (no fabricated data, no crash).
- **Credentials are never printed** — not in output, not in errors, not in logs.
- Intraday history is subject to FYERS provider-imposed range limits per request
  (see `history_chunking.py`); the command honors them automatically.
- The final available candle depends on what FYERS actually returns for the
  requested range — the command reports the *actual* stored range, and never
  claims data exists when FYERS returned none.
- Rerunning is safe and idempotent: a second run over the same period reports
  `new rows stored: 0` (existing rows are never duplicated, never overwritten).
- Auth failures are distinguished from empty data / API errors / network errors,
  so an authentication problem is never silently reported as "no market data".
- Supported timeframes: `1m 5m 15m 30m 1h 4h 1d 1w 1M` (canonical names).

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env: set MARKET_DATA_PROVIDER=fyers and (for live data) FYERS_CLIENT_ID / FYERS_ACCESS_TOKEN
PYTHONPATH=src python -m trading_system providers        # confirm fyers is registered
PYTHONPATH=src python -m trading_system instruments      # see NSE:SYMBOL -> FYERS symbol map
```

Ingestion (requires FYERS credentials for live Indian data; Binance works without):
```bash
PYTHONPATH=src python -m trading_system ingest-india --symbols NSE:RELIANCE,NSE:NIFTY50 --timeframe 1d
```

Live data (no orders placed):
```bash
PYTHONPATH=src python -m trading_system live --symbols NSE:SBIN --duration 15
# Without credentials it prints: "FYERS runtime verification blocked because credentials were not available."
PYTHONPATH=src python -m trading_system live-verify --symbols NSE:SBIN --duration 20
# REAL market-data verification only (data WS, no orders). For live ticks, run during NSE hours.
```

## Derivatives & commodities (Day 6)

The system now models F&O and commodity contracts in a provider-independent way and
can bulk-load their historical OHLCV through the same `backfill-history` engine.

**Normalized model.** Every instrument carries `exchange`, `symbol`, `instrument_type`,
`underlying`, `expiry`, `strike`, `option_type`. A canonical `contract_id`
(`EXCHANGE:UNDERLYING|EXPIRY|STRIKE|CE|PE|FUT`) guarantees two contracts with
different expiries/strikes/option-types **never collide** (NIFTY Jun future vs Jul
future; NIFTY 25000 CE vs PE).

**FYERS symbol handling** (format verified from the installed `fyers_apiv3` SDK — not
guessed):
- Index:        `NSE:NIFTY50-INDEX`
- Equity:       `NSE:SBIN-EQ`
- Future:       `NSE:NIFTY25DECFUT` / `MCX:SILVERMIC25DECFUT` (no dash; `<ROOT><YY><MMM>FUT`)
- Option:       `NFO:NIFTY25DEC24800CE` (`<ROOT><YY><MMM><STRIKE><CE|PE>`)

**Discovery** (provider-independent interface): `InstrumentRepository.list_futures()`,
`list_options()`, `get_expiries()`, `find_contract()`. The FYERS live source is the
`optionchain` endpoint, wrapped by `FyersInstrumentDiscovery` (read-only, no orders).

**Historical data:** reuse `backfill-history` — the engine threads `contract_id`
through to storage so derivative rows are idempotent and collision-free.
```bash
# Index future (use a contract that is actually trading; do not hardcode today's expiry)
PYTHONPATH=src python -m trading_system backfill-history --symbols NFO:NIFTY25DECFUT --timeframe 5m --days 10
# Commodity future
PYTHONPATH=src python -m trading_system backfill-history --symbols MCX:SILVERMIC25DECFUT --timeframe 1d --days 30
```

**Contract discovery CLI:**
```bash
PYTHONPATH=src python -m trading_system instruments --underlying NIFTY --type futures
PYTHONPATH=src python -m trading_system instruments --underlying NIFTY --type options --expiry 2025-12-25
PYTHONPATH=src python -m trading_system instruments --underlying NIFTY --discover   # live option chain (needs creds)
```

**Safety:** Day 6 is DATA + INSTRUMENT INTELLIGENCE ONLY. No order placement, no
broker execution, no position opening/closing, no leverage. Credentials are never
printed. Validation (`validate_ohlcv` + `validate_contract_identity`) rejects
impossible OHLC, duplicate/out-of-order timestamps, missing fields, and malformed
contract metadata — but never rejects legitimate derivative behavior.

## Research & backtesting (Day 7)

Provider-independent historical research engine. DATA ONLY — no orders, no execution,
no LLM in the decision loop. Reusable primitives: `research/features` (causal SMA/EMA/
vol/ATR/momentum), `research/strategies` (EMA / momentum / breakout baselines),
`research/backtester` (next-bar execution, costs, risk), `research/performance`
(deterministic metrics + reliability flag), `research/walkforward` (train/test split, no
leakage). Derivative analytics the DB cannot supply (OI, IV, greeks, basis) are listed as
`MISSING_REQUIRED_FEATURES` and never fabricated.

```bash
PYTHONPATH=src python -m trading_system strategies                       # list strategies
PYTHONPATH=src python -m trading_system backtest --symbol NSE:SBIN --timeframe 5m --strategy ema \
    --initial-capital 100000 --transaction-cost 0.0005 --slippage 0.0002
PYTHONPATH=src python -m trading_system backtest --symbol NSE:SBIN --timeframe 5m --strategy momentum --train-frac 0.7
```

**Honesty:** backtest output is NOT evidence of future profitability. Small samples are
flagged unreliable. If a symbol has no stored history the command prints
"NO DATA ... blocked" and returns — no fabricated results.

## AI Market Intelligence (Day 8)

Provider-independent analysis layer: features → regime → signal candidate → explanation →
AI reasoning. **DATA/ANALYSIS ONLY — no orders, no execution, no broker API.**
Reuses `indicators` + the existing `ModelProvider`/`MarketView` AI contract. Produces a
`SignalCandidate` (analytical hypothesis), never an order. Confidence is an *analytical*
score in [0,1], not a win probability. Malformed AI output is rejected (pydantic).
Data-health gating blocks analysis when the feed is stale/disconnected/auth-error/invalid.
OI/IV/greeks/basis are schema fields kept `None` until FYERS supplies them (never fabricated).
See `DAY8_REPORT.md` and `FRONTEND_BACKEND_CONTRACT.md`.

```bash
PYTHONPATH=src python -m trading_system analyze-history --symbols NSE:SBIN --timeframe 1d
PYTHONPATH=src python -m trading_system analyze-history --symbols NSE:SBIN,NFO:NIFTY25DECFUT --timeframe 5m
PYTHONPATH=src python -m trading_system analyze-history --symbols NSE:SBIN --timeframe 1d --ai local
# A symbol with no stored history prints "ANALYSIS BLOCKED / NO_DATA" — no fabrication.
```

**Honesty:** the intelligence engine describes what the data shows and where it is
ambiguous. It is not a predictor of profitability and never places trades.

## Research infrastructure & evidence engine (Day 10)

Provider-independent research spine for the future autonomous loop (Hermes operates this via
deterministic tools; no execution, no broker, no LLM code-gen). Builds on Day 7 backtesting.

* `research/factors.py` — causal `FactorEngine` (17 documented factors: trend, momentum, volatility,
  volume, price-structure). Each factor uses only data ≤ T; insufficient history → `NaN`.
* `research/factor_analysis.py` — `compute_ic_series` (cross-sectional Spearman IC, factor_T →
  forward return, ≥5 instruments required), `ic_statistics` (mean/median/std IC, ICIR), `grouped_backtest`
  (decile portfolios, long-short), `breakeven_fee_bps` (generic cost breakeven, units explicit).
* `research/backtester.py` (Day 10) — `warmup_bars` / `evaluation_start_date`: indicators prime on
  warmup bars but **reported performance covers only the evaluation window** (no look-ahead inflation).
* `research/evidence.py` — `ExperimentManifest` (deterministic SHA-256 identity hash), `Hypothesis`
  (research-lifecycle status — research state, **not** execution permission), `EvidenceRun`,
  `EvidenceStore` (SQLite on the SAME engine as MarketStore — no second DB), `ResearchRegistry`
  (clean API for future Hermes tools), `classify_quality` (INSUFFICIENT/MARGINAL/ADEQUATE),
  `is_evidence_stale`. Evidence is filtered by regime.

```bash
PYTHONPATH=src python -m trading_system research factors --symbol NSE:SBIN --timeframe 1d
PYTHONPATH=src python -m trading_system research factor-analysis --symbol NSE:SBIN --factor rsi_14 --lag 1
PYTHONPATH=src python -m trading_system research hypothesis list
PYTHONPATH=src python -m trading_system research evidence list
```

**Honesty:** evidence quality is classified by documented, *provisional* thresholds (sample size, OOS
availability, cost assumption). A single stored symbol (SBIN) makes cross-sectional factor research
(≥5 instruments) impossible — this is reported explicitly; no synthetic universe is fabricated.
See `DAY10_REPORT.md`.

## Data foundation & India cost model (Day 10.5)

Data/research-infrastructure phase (no execution, no orders). Builds the foundation for
cross-sectional research and realistic India backtests.

* `india/token_manager.py` — `TokenManager`: explicit FYERS token lifecycle (access/refresh token,
  SHA-256 checksum refresh grant). Distinguishable states (`AUTH_OK`, `ACCESS_TOKEN_EXPIRED`,
  `REFRESH_TOKEN_EXPIRED`, `AUTH_FAILED`, `NETWORK_ERROR`). **Never logs/embeds secrets** (test-enforced).
  Integrated with `DataHealthMonitor.on_auth_status()`. No browser/TOTP automation (per scope).
* `research/universe.py` — `ResearchUniverse` / `UniverseRegistry`: configurable research baskets
  (NIFTY50, NIFTY100, LIQUID_FNO, MCX_RESEARCH) loaded from explicit config. Constituents are
  **never fabricated**; scaffolds require the official list before running. `research coverage` reports
  exactly what is stored.
* `research/costs.py` — `IndiaTransactionCostModel`: explicit `EffectiveRate` table (effective-dated,
  e.g. the 2026-04-01 F&O STT change), `CostBreakdown` in INR, all segments
  (equity/intraday/futures/options/commodity), **GST only on taxable components** (STT/stamp excluded,
  test-enforced), missing-rate → explicit error (never silent zero). Backtester accepts it via
  `BacktestConfig.cost_model` (backward compatible with generic `transaction_cost_pct`).
* `backfill-universe` CLI — bulk, idempotent, resumable backfill over a universe; `--dry-run`,
  `--request-delay`; unsupported symbols reported, not skipped.
* `auth-status` CLI — observable FYERS auth state (no secrets printed).
* `research coverage` CLI — per-symbol stored-data report, contract-identity / duplicate /
  validation checks.

```bash
PYTHONPATH=src python -m trading_system auth-status
PYTHONPATH=src python -m trading_system research coverage --universe DEFAULT_BASKET --timeframe 1d
PYTHONPATH=src python -m trading_system backfill-universe --universe DEFAULT_BASKET --timeframe 1d --dry-run
```

**Honesty:** the FYERS access token is currently expired and no refresh token is configured, so live
backfill was **not** performed (AUTH BLOCKED, reported honestly). Only `NSE:SBIN` 1d is stored; the
other default instruments are reported MISSING by `research coverage` (not invented). Cost rates are
encoded from the published FYERS/NSE schedule and must be re-verified before any capital use. See
`DAY10_5_REPORT.md`.

## CLI (Day 4 + Day 6)

```bash
PYTHONPATH=src python -m trading_system providers          # list data providers
PYTHONPATH=src python -m trading_system instruments         # NSE:SYMBOL -> FYERS map (+ derivative discovery flags)
PYTHONPATH=src python -m trading_system instrument-search BANK   # search registry
PYTHONPATH=src python -m trading_system ingest-india --symbols NSE:RELIANCE,NSE:NIFTY50 --timeframe 1d
PYTHONPATH=src python -m trading_system backfill-history --symbols NSE:SBIN --timeframe 1d --days 3650   # bulk, idempotent, data-only
PYTHONPATH=src python -m trading_system market-status       # feed health + stored quality
PYTHONPATH=src python -m trading_system live --symbols NSE:SBIN --duration 15
# Without FYERS creds the live command prints:
#   "FYERS runtime verification blocked because credentials were not available."
```

**Live FYERS connectivity is NOT claimed.** All live behavior is gated on
`FYERS_CLIENT_ID` + `FYERS_ACCESS_TOKEN`; without them the `live` command exits
with a clear message (no fabricated stream). Historical chunking, instrument
parsing, market calendar, event bus, closed-candle pipeline, and data-health
monitor are all implemented and unit-tested offline. The `backfill-history` command
is **data-only** (historical ingestion only; it never places orders).

## Current data limitations

- **Primary target is now Indian markets (FYERS).** Binance remains the dev/test provider.
- FYERS historical (`/history`) is **not real-time** — historical candles only.
- FYERS **pricing/data-feed fee is UNVERIFIED**; requires an active FYERS account.
- Live WebSocket requires credentials (OAuth2). Not tested live on Day 4.
- Only daily (`1d`) ingestion exercised via fixtures; other intervals supported by
  mapping but untested against the live API.
- SEBI algo rules: order placement (future work) requires a validated static IP;
  data-only use is unaffected.

## Running tests

```bash
pytest            # 307 tests (Day 1-10.5), must pass
```

## Installation

Requires Python 3.10+. (Developed on 3.11.)

```bash
git clone <repo> && cd trading-system
python -m venv .venv && source .venv/Scripts/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env      # edit only if you want non-defaults; no secrets needed for Day 1
```

## Environment configuration

All configuration is environment-driven (see `.env.example`). Secrets (API
keys, Telegram tokens) are **never** hard-coded — only the *name* of the env
var that holds them is stored. Day 1 needs no secrets at all.

| Variable | Default | Purpose |
|---|---|---|
| `DATA_PROVIDER` | `binance` | Which provider to use |
| `SYMBOLS` | `BTCUSDT,ETHUSDT` | Comma-separated symbols |
| `TIMEFRAME` | `1d` | Bar interval |
| `LOOKBACK_BARS` | `365` | Bars pulled per ingest |
| `DB_PATH` | `data/market_data.db` | SQLite location |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

## Running ingestion

```bash
PYTHONPATH=src python -m trading_system ingest --symbols BTCUSDT,ETHUSDT
# Re-running the same command inserts 0 new rows (idempotent).
```

Output reports: requested, received, valid, inserted, rejected, duration, errors.

## Analyzing stored data

```bash
PYTHONPATH=src python -m trading_system analyze BTCUSDT
PYTHONPATH=src python -m trading_system status
```

## Running tests

```bash
pytest            # all suites; 169 tests (Day 1-6), must pass
```

## What is NOT implemented yet

- AI analyst (model), signal generation, risk management logic, backtesting,
  paper-trading execution, Telegram delivery (wired but disabled), broker/execution.
  These are scaffolded as typed, decoupled placeholders for Day 2+.

## Roadmap

See `DAY1_REPORT.md` → *Day 2 recommendations*, and `ARCHITECTURE.md`.
