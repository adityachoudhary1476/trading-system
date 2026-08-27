# Day 4 — Indian Data Hardening + Live Pipeline Preparation

## Completed (genuinely implemented and tested offline)

- **Historical data chunking** — `india/history_chunking.py`: provider-independent
  `plan_chunks` (respects documented FYERS caps: 100d/minute, 366d/day+), a
  `ChunkedHistoricalFetcher` that fetches each chunk via a callable, combines,
  dedupes, sorts, validates, tolerates partial failures, and retries transient
  errors. Pure date-math is fully unit-tested.
- **Instrument repository** — `india/instrument_repository.py`: `InstrumentRepository`
  with `get_instrument`, `search_instruments`, `get_equities`, `get_indices`,
  `get_derivatives`, `get_expiring_derivatives`. `from_fyers_csv` parses the
  documented FYERS master layout (equity/index/option/future) from fixtures.
- **Market calendar hardening** — `india/market_calendar.py`: explicit session
  phases (PRE_MARKET / REGULAR / POST_MARKET / CLOSED / HOLIDAY), holiday registry
  injected (NOT hard-coded en masse), Asia/Kolkata throughout.
- **Provider-independent event bus** — `india/event_bus.py`: pub/sub fan-out of
  normalized `InternalMarketEvent`; consumers never see provider JSON.
- **Closed-candle pipeline** — `india/closed_candle_pipeline.py`: aggregates ticks,
  emits `ClosedCandle` only on bar close, drops late/duplicate ticks for closed
  bars, builds `MarketSnapshot` strictly from closed history (no look-ahead).
- **Data health monitor** — `india/data_health.py`: `FeedStatus`
  (HEALTHY/STALE/DISCONNECTED/AUTH_ERROR/INVALID_DATA) + quality metrics
  (events received/rejected, duplicates, candles generated/rejected, latest
  timestamps). Signals are suppressed when not HEALTHY.
- **WebSocket reconnection (deterministic tests)** — `FyersDataSocket` made
  testable (`auto_connect=False`, injectable `_open`, extracted `_schedule_reconnect`).
  Tests cover connect, disconnect, reconnect w/ exponential backoff + cap,
  malformed/duplicate/unknown/heartbeat messages — no real socket, no spin.
- **Storage hardening** — `storage/database.py`: composite index on
  (symbol, timeframe, timestamp, provider, exchange); idempotency key now includes
  `exchange`; SQLite-safe forward migration adds `exchange` to existing DBs.
- **Paper-trading interface** — `paper_trading/interface.py`: `PaperTrader` Protocol
  + `NoOpPaperTrader` (records events, no execution). Consumer of
  `InternalMarketEvent` / `ClosedCandle` / `MarketSnapshot` / `Signal` — provider-agnostic.
- **Configuration** — `MARKET`, `MARKET_DATA_PROVIDER`, `DEFAULT_EXCHANGE`,
  `TIMEZONE`, `ANALYSIS_INTERVAL_BARS` (so the AI runs on candle close, never per tick).
- **CLI** — `instrument-search`, `market-status`/`data-health` added; existing
  `providers`, `instruments`, `ingest-india`, `live` retained.
- **Fixtures** — `tests/fixtures/india_fixtures.py`: FYERS history response,
  WebSocket event, malformed event, instrument-master CSV, equity/index/option/future.

## Tests

**106 passed, 0 failed** (80 from Days 1–3 + 26 new in `test_day4.py`). No network,
no FYERS credentials, no live market, fully deterministic.

## FYERS runtime status

- **Credentials available?** No. No `FYERS_CLIENT_ID` / `FYERS_ACCESS_TOKEN` in env.
- **REST live-tested?** No. Historical fetch code path is tested with mocked
  responses only; no real `/history` call was made.
- **WebSocket live-tested?** No. `live` CLI correctly reports
  *"FYERS runtime verification blocked because credentials were not available."*
  Socket behavior is verified via deterministic handler tests, not a live connection.
- No FYERS connectivity is claimed or simulated anywhere.

## Historical chunking

`plan_chunks` splits `[start,end]` into adjacent chunks each within the provider's
per-request cap (FYERS: 100 days for minute resolutions, 366 for day/week/month).
`ChunkedHistoricalFetcher` delegates actual fetching to a provider callable, so the
engine is provider-agnostic. Combined frames are deduped, sorted, validated. Partial
chunk failure is tolerated (other chunks still load); transient errors retried.

## Instrument master

Implemented: parser (`from_fyers_csv`) + repository queries, validated on a
documented-layout fixture. NOT executed live: the master download endpoint needs
auth. A live integration is a thin step (see `docs/INSTRUMENT_MASTER.md`); we did
not fabricate a download.

## Market calendar

Supports weekday/weekend, pre-market (09:00–09:15), regular (09:15–15:30),
post-market (15:30–16:00) IST, and holiday (registry-injected). Timezone is
Asia/Kolkata. Holidays are deliberately not hard-coded; a calendar source can be
loaded later.

## Event architecture

`FYERSMarketDataProvider` (or any provider) → normalized `InternalMarketEvent` →
`EventBus` → subscribers (candle aggregator, DB, future paper trader, Telegram, AI
snapshot generator). The AI is wired to run on candle close at `ANALYSIS_INTERVAL_BARS`,
never per tick.

## Data health

`DataHealthMonitor` tracks feed liveness, rejects, duplicates, candles, and latest
timestamps; exposes a status enum. `is_safe_for_signals()` is False unless HEALTHY,
so stale/disconnected/auth-error/invalid feeds cannot produce signals.

## Known limitations (still unverified)

1. **Live FYERS REST + WebSocket** untested (no credentials).
2. FYERS **pricing/data-feed fee** still UNVERIFIED (see docs/FYERS.md).
3. API **rate/connection limits** unverified (not published in official sources).
4. WebSocket message framing assumptions (SymbolUpdate shape, `T:"t"`) need live
   validation against a real session.
5. Instrument master **live download** not executed (auth required); fixture-parsed only.
6. `exchange` column added to pre-existing Day-1 DB via migration; legacy rows have
   NULL exchange (acceptable — Binance data unaffected).
7. Multi-chunk fetch verified via a fake fetch callable; not against the real API.

## Day 5 recommendation

1. **Obtain FYERS credentials** (or a paper/demo app). Run a real end-to-end test:
   auth → `/history` (1d + 5m) → chunked fetch of a larger window → store →
   validate → `market-status`. Then a short live WebSocket session through the
   `EventBus` → `ClosedCandlePipeline` → `DataHealthMonitor`, confirming actual
   payload shapes match the fixtures.
2. **Wire the live pipeline**: connect `FyersDataSocket.on_event` → `EventBus` →
   `ClosedCandlePipeline` → (on close) `DataHealthMonitor.record_candle` →
   `make_snapshot` at `ANALYSIS_INTERVAL_BARS` → AI view → deterministic signal.
3. **Persist closed candles** to the hardened store (idempotent, exchange-aware).
4. **Risk engine** + **backtester** (Day 2 roadmap) consuming Indian snapshots.
5. **Paper trader** scaffold implementing `PaperTrader` (records, no execution).
6. **Telegram** notifications on signal/health changes (config already present).
