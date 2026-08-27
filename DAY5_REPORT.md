# Day 5 — Real FYERS Pipeline Integration

## CRITICAL PROTOCOL FINDING (Phase 2)

Auditing the installed `fyers_apiv3` SDK revealed the Day 3/4 assumption about the
FYERS WebSocket was **wrong**: the v3 *data* socket is **binary protobuf**, not
JSON text.

- Real endpoint: `wss://socket.fyers.in/hsm/v1-5/prod`
- Auth = binary HSM-token frame built from the access token (`access_token_to_hsmtoken`).
- Subscription = binary frame carrying the FYERS wire symbol (`NSE:SBIN-EQ`).
- Market messages = protobuf `MarketFeed` blobs the SDK decodes into plain dicts
  (precision/multiplier already applied). Lite-mode dict: `{"symbol", "ltp", "type": "sf", ...}`;
  full mode adds `open_price/high_price/low_price/vol_traded_today/prev_close_price/...`.
- Control frames observed on a real connect: `{"type":"cn","message":"Authentication done"}`
  and `{"type":"lit","message":"Lite Mode On"}` — these carry no `symbol`/price and must be skipped.
- Reconnect with bounded backoff is provided by the SDK itself.

**Decision:** `FyersDataSocket` now wraps the official `fyers_apiv3.FyersWebsocket.data_ws`
SDK as the transport (provider-specific code stays in `india/fyers.py`). Hand-rolling
the binary framing over raw `websocket-client` would never connect and would be guessing.
The old JSON `T:"c"/"T:"t"` normalization code was removed; fixtures/tests updated to the
real SDK-decoded dict shape.

## COMPLETED

- **FyersDataSocket rewritten** to use the real SDK: translates internal symbols ->
  FYERS wire symbols, normalizes SDK dicts -> `InternalMarketEvent`, forwards to
  `on_event`, exposes health callbacks (`on_connect/disconnect/auth_error/invalid`).
- **LiveMarketPipeline** (`india/live_pipeline.py`): wires
  `Provider socket -> EventBus -> ClosedCandlePipeline -> DataHealthMonitor -> snapshot`.
  AI snapshot is built only on candle close at `ANALYSIS_INTERVAL_BARS` — never per tick.
- **Historical bootstrap** (`bootstrap_historical`): loads recent candles via the
  provider's `get_historical`, persists idempotently to `MarketStore`, and seeds the
  closed-candle aggregator (`CandleAggregator.seed_bar`) so live ticks continue from
  the last close. Provider-agnostic (works for FYERS/Binance).
- **Config**: added `stale_seconds` (feed STALE threshold).
- **CLI**: added `live-verify` — bounded real market-data verification, prints
  normalized events + feed health, explicitly states NO orders are placed.
- **Tests**: +13 new in `test_day5.py` (all offline/deterministic); updated
  `test_day4.py`/`test_india.py` WS tests to the real protocol. Full suite: **119 passed**.

## REAL FYERS VERIFICATION (Phase 7)

Ran `python -m trading_system live-verify --symbols NSE:SBIN --timeframe 1m --duration 15`
with `.env` credentials loaded:

```
FYERS WS connected & authenticated
Subscribed to ['NSE:SBIN']; running for 15s ...
MSG {'type':'cn','code':200,'message':'Authentication done','s':'ok'}   <- real control frame
MSG {'type':'lit','code':200,'message':'Lite Mode On','s':'ok'}          <- real control frame
```

**Verified true:** SDK authentication, HSM-token auth frame, litemode subscription,
and the normalized-event boundary all work end-to-end against the live FYERS server.
**NOT observed:** a live market *tick* payload. The verification session ran at ~20:30
IST, after the NSE cash market close (15:30 IST) — FYERS sends no LTP ticks for a
closed scrip in litemode, so 0 ticks arrived and the socket closed cleanly on
`pipe.stop()`. This is expected market-hours behavior, not a code defect. The
tick-payload decoding path is covered offline by fixtures mirroring the SDK dict shape
plus the regression test for the exact control frames seen live.

Re-running `live-verify` during NSE trading hours (09:15–15:30 IST) is expected to
yield live `ltp` events through the normalized pipeline.

## SAFETY BOUNDARY (enforced)

- `live-verify` and the entire pipeline call **no order/execution API**. The only
  brokerage contact is data REST + data WebSocket. `grep -ri "order\|execute\|place"`
  in the new code finds only docstrings/health status names, no execution calls.
- No AI runs on the tick hot path; snapshots only on closed-candle boundaries.
- Feed must be HEALTHY to emit a snapshot; STALE/DISCONNECTED/AUTH_ERROR/INVALID_DATA
  suppress signals (tested).
- No secrets committed: `.env` and `.env.backup` are git-ignored; `scripts/fyers_auth.py`
  reads creds from env and appends the token to `.env` (never prints it).

## FILES CHANGED

- `src/trading_system/india/fyers.py` — SDK-backed `FyersDataSocket`; `_registry_lookup_internal`.
- `src/trading_system/india/live_pipeline.py` — NEW: `LiveMarketPipeline`, `bootstrap_historical`, `seed_historical_df`.
- `src/trading_system/india/candle_aggregator.py` — `seed_bar` (historical seed).
- `src/trading_system/india/__init__.py` — export `LiveMarketPipeline`, `bootstrap_historical`.
- `src/trading_system/india/data_health.py` — unchanged (used as-is).
- `src/trading_system/config/settings.py` — `stale_seconds`.
- `src/trading_system/__main__.py` — `live-verify` CLI command.
- `tests/fixtures/india_fixtures.py` — WS fixtures -> real SDK dict shape.
- `tests/test_india.py`, `tests/test_day4.py` — WS tests updated to real protocol.
- `tests/test_day5.py` — NEW: 13 integration tests (offline).
- `.gitignore` — ignore `.env.backup`, `fyersDataSocket.log`, `*.log`.
- `scripts/fyers_auth.py` — OAuth helper (pre-existing, kept; reads env, no literals).

## TESTS ADDED

13 (`test_day5.py`): event->bus, bus->candle, candle->snapshot, duplicate-tick
rejection, late-tick rejection, unhealthy-feed suppresses signals, historical
bootstrap persistence, historical bootstrap idempotency, malformed WS handling,
disconnect/reconnect (no spin), no-AI-per-tick, AI-follows-ANALYSIS_INTERVAL_BARS,
observed-control-frames skipped, historical-seed.

**Total: 119 passed (106 prior baseline + 13 new). 0 failed.**

## KNOWN LIMITATIONS

1. Live *tick* payload not observed (ran after market close). Decoding path is
   fixture-tested against the SDK dict shape + regression for the exact live control frames.
2. Full-mode (`SymbolUpdate`) OHLCV dict keys assumed from the SDK decoder
   (`__response_output`); litemode (`ltp`) is the verified path. Full-mode keys
   (`open_price` etc.) are mapped defensively (None if absent).
3. FYERS pricing/data-feed fee remains UNVERIFIED.

## DAY 6 RECOMMENDATION

Run `live-verify` during NSE hours to capture a real tick and confirm end-to-end
LTP→snapshot; then wire `LiveMarketPipeline.on_snapshot` -> existing AI analyst +
deterministic `generate_signal`, persisting closed candles to `MarketStore`, and add a
small `PaperTrader` consumer (no execution) for simulated fills.
