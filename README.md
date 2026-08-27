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

- **Crypto only** (Binance). No equities/forex yet.
- Binance klines: the latest candle is the live/open bar; treat it as
  *near-real-time but provisional*. We do not claim millisecond real-time.
- Free public endpoint: generous rate limits (weight-based, ~6000/min) but
  subject to Binance terms. Not for commercial redistribution of the raw feed.
- Only daily (`1d`) ingestion was exercised end-to-end; other intervals are
  supported by the provider but untested at scale.

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
