# Architecture

The system is a linear, replaceable pipeline. Each stage depends only on the
stage before it through a **stable interface**, not on a concrete implementation.
The AI/model and broker are deliberately isolated so neither can couple to the
data source.

```
                 ┌─────────────────────────────────────────────┐
   MARKET DATA   │  MarketDataProvider (abstract)              │
   (provider)    │   ├─ BinanceProvider  (Day 1, active)       │
                 │   └─ StooqProvider    (fallback, documented)│
                 └───────────────────────┬─────────────────────┘
                                         │  OHLCV DataFrame (tz-aware UTC)
                                         ▼
                 ┌─────────────────────────────────────────────┐
   DATA          │  validate_ohlcv()                            │
   VALIDATION    │   • missing/impossible values               │
                 │   • invalid OHLC relationships              │
                 │   • duplicate / out-of-order timestamps     │
                 │   • abnormal gaps (warning or reject)        │
                 │   → FAILS LOUDLY on bad data                 │
                 └───────────────────────┬─────────────────────┘
                                         ▼
                 ┌─────────────────────────────────────────────┐
   STORAGE       │  MarketStore (SQLAlchemy)                   │
                 │   • SQLite (Day 1)                           │
                 │   • UNIQUE(symbol,tf,timestamp,provider)     │
                 │   • idempotent upsert (no duplicates)        │
                 └───────────────────────┬─────────────────────┘
                                         ▼
                 ┌─────────────────────────────────────────────┐
   QUANT /       │  quant.py  +  indicators.py                 │
   INDICATORS    │   returns, vol, drawdown, volume stats      │
                 │   SMA/EMA/RSI/MACD/Bollinger/ATR             │
                 │   (deterministic, tested)                    │
                 └───────────────────────┬─────────────────────┘
                                         ▼
                 ┌─────────────────────────────────────────────┐
   SNAPSHOT      │  models.snapshot.MarketSnapshot (Pydantic)  │
   (structured)  │   validated, tz-aware, NO-LOOK-AHEAD        │
                 │   only closed-bar history reaches the AI    │
                 └───────────────────────┬─────────────────────┘
                                         ▼
                 ┌─────────────────────────────────────────────┐
   AI ANALYST    │  ModelProvider.analyze(snapshot) -> View    │
   (Day 2)       │   • LocalRuleModel (offline, tested)        │
                 │   • OpenAICompatibleProvider (impl, untested)│
                 │   returns validated MarketView (interpretation│
                 │   ONLY — never an order, size, or risk override)│
                 └───────────────────────┬─────────────────────┘
                                         ▼
                 ┌─────────────────────────────────────────────┐
   SIGNAL ENGINE │  signals.generate_signal(snapshot, view)     │
   (Day 2)       │   deterministic LONG/SHORT/HOLD + reason    │
                 │   AI confidence = analytical gate only      │
                 └───────────────────────┬─────────────────────┘
                                         ▼
   RISK MGMT ─► BACKTESTER ─► PAPER TRADING ─► TELEGRAM / DASHBOARD
   (Day 3)       (Day 3)       (Day 3)          (wired/disabled)
```

## Key design decisions

1. **Provider abstraction.** All exchange-specific logic lives in
   `data/binance.py` / `data/stooq.py` behind `MarketDataProvider`. Swapping
   providers is a one-line factory change (`data/provider_exports.py`).
2. **Validation before storage.** No row reaches the DB (or any future signal
   logic) without passing the validator. Invalid rows are rejected and counted;
   error-severity problems abort the batch.
3. **Idempotent storage.** A UNIQUE constraint + read-before-write upsert means
   ingestion is safe to re-run and never double-counts.
4. **Secrets via env only.** Config holds connection *metadata* and the *names*
   of env vars for secrets — never the secrets themselves.
5. **AI as analyst, not actor.** The future model returns a structured
   `MarketView`; it has no path to place orders. Execution is gated behind
   signal + risk stages that do not exist yet.
6. **Deterministic, tested core.** Indicators/quant are pure functions; the
   validator and storage have explicit unit + integration tests.

## Module map (`src/trading_system/`)

| Package | Responsibility | Day 1 state |
|---|---|---|
| `config/` | Settings, env-driven config, logging | done |
| `data/` | Provider interface, Binance/Stooq, validation, ingestion pipeline | done |
| `storage/` | SQLAlchemy SQLite store, idempotent upsert | done |
| `analysis/` | Quant metrics + analysis pipeline | done |
| `indicators/` | Technical indicators | done |
| `models/` | `MarketSnapshot`, `MarketView`, `ModelProvider` (LocalRule + OpenAI-compatible), analyst orchestration | Day 2 |
| `signals/` | Deterministic signal engine (LONG/SHORT/HOLD) | Day 2 |
| `risk/` | Risk manager contract | placeholder |
| `backtesting/` | Backtest result contract | placeholder |
| `paper_trading/` | Paper account contract | placeholder |
| `notifications/` | Telegram (env-secret, disabled by default) | wired/off |
