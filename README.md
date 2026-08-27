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

## Indian-market setup

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
```

## Running tests

```bash
pytest            # 80 tests, must pass
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
pytest            # all suites; 34 tests, must pass
```

## What is NOT implemented yet

- AI analyst (model), signal generation, risk management logic, backtesting,
  paper-trading execution, Telegram delivery (wired but disabled), broker/execution.
  These are scaffolded as typed, decoupled placeholders for Day 2+.

## Roadmap

See `DAY1_REPORT.md` → *Day 2 recommendations*, and `ARCHITECTURE.md`.
