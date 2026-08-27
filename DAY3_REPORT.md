# Day 3 — Indian Market Data Layer

## 1. Files created/modified

**Created**
- `src/trading_system/india/__init__.py`
- `src/trading_system/india/instruments.py` — `Instrument`, `InternalSymbol`, `InstrumentRegistry`, `Exchange`, `InstrumentType`.
- `src/trading_system/india/symbol_map.py` — `to_fyers_symbol` / `from_fyers_symbol` (provider-specific mapping isolated here).
- `src/trading_system/india/market_calendar.py` — `Asia/Kolkata` session logic (open/closed, trading days, boundaries).
- `src/trading_system/india/candle_aggregator.py` — deterministic ticks→OHLCV aggregation, provisional vs closed.
- `src/trading_system/india/events.py` — `InternalMarketEvent` (decoupling boundary for future PaperTrader).
- `src/trading_system/india/fyers.py` — `FYERSMarketDataProvider` (REST history/quotes) + `FyersDataSocket` (WebSocket, reconnect/stale handling).
- `tests/test_india.py` — 20 new tests.
- `docs/FYERS.md`, `docs/INDIAN_MARKET.md`, `DAY3_REPORT.md`.

**Modified**
- `src/trading_system/data/provider_exports.py` — added `fyers` to the provider factory.
- `src/trading_system/storage/database.py` — added nullable `exchange` column (backward compatible; Binance rows keep provider-based keys).
- `src/trading_system/__main__.py` — added CLI: `providers`, `instruments`, `ingest-india`, `live`.
- `.env.example`, `README.md`, `ARCHITECTURE.md` — Indian-market configuration + docs.

## 2. Current test count

**80 passed, 0 failed** (60 from Days 1–2 + 20 new in `test_india.py`). No live network/credentials required for any test.

## 3. FYERS research findings

Documented in `docs/FYERS.md` from official v3 sources (support KB, FyersDev sample code, `fyers-apiv3` PyPI, skills reference):
- OAuth2 auth (App ID + Secret + redirect → auth code → access token); token used as `APPID:AccessToken` for the socket.
- History `GET https://api-t1.fyers.in/data/history` with `symbol/resolution/date_format/range_from/range_to/cont_flag`; candles `[epoch,o,h,l,c,v]`; response `{"s":"ok","candles":[...]}`.
- Resolutions include 1/5/15/60/D/1W/1M (plus many more). Caps: minute 100d/req, day+ 366d/req.
- WebSocket `wss://api.fyers.in/socket/v2/data/` with Lite (LTP) and SymbolUpdate (OHLCV) modes; auth + subscribe frames; autoReconnect supported.
- Requires an active FYERS account. SEBI algo rules require a validated static IP for *order placement* (data-only Day 3 is unaffected).

## 4. Current FYERS pricing

**UNVERIFIED.** No published free tier or per-feed fee was found in the official sources reviewed. Explicitly documented as not-free-until-confirmed. Verify at myapi.fyers.in before production.

## 5. API/data limitations

- History API is **not real-time** (per official KB) — historical candles only.
- Rate/connection limits **not published** in consulted sources → unverified.
- Minute history capped at 100 days/request → larger windows need chunking (not yet implemented).
- WebSocket message framing changed in v3; the adapter's normalization is best-effort and must be validated against a live session.

## 6. Authentication requirements

OAuth2: App ID + Secret Key from myapi.fyers.in, plus a registered redirect URI; user authorization yields an auth code exchanged for an access token. `FYERS_CLIENT_ID` and `FYERS_ACCESS_TOKEN` are read from the environment only — never hard-coded.

## 7. Historical-data capabilities

Implemented `get_historical(symbol, timeframe, limit, start, end)` → normalized OHLCV DataFrame (tz-aware UTC), routed through the existing validator. Interval mapping covers 1m/5m/15m/1h/1d/1w/1M (Day 3); the FYERS side supports more granular resolutions if needed later. Multi-chunk fetching for >100d windows is a Day 4 item.

## 8. WebSocket capabilities

`FyersDataSocket` implements: connect → auth → subscribe (Lite/SymbolUpdate), message normalization to `InternalMarketEvent`, heartbeat ping (20s), stale-data detection (>60s no messages), and capped exponential-backoff reconnect (no aggressive CPU loop), graceful `close()`. The AI is never in the tick hot path. **Not runtime-tested** (no credentials).

## 9. Indian instruments tested

Tested via fixtures/mapping (no live API): RELIANCE, TCS, INFY, HDFCBANK, ICICIBANK, SBIN (equities) and NIFTY50, NIFTYBANK, FINNIFTY (indices). Symbol normalization verified for equity (`NSE:SBIN-EQ`), index (`NSE:NIFTY50-INDEX`), and option (`NSE:SBIN25DEC400CE`) forms.

## 10. Real FYERS credentials available?

**No.** No `FYERS_CLIENT_ID` / `FYERS_ACCESS_TOKEN` in the environment.

## 11. Historical API actually tested?

**No live test.** The historical *code path* was tested offline with a mocked REST response (normalization + validation), but no real FYERS request was executed.

## 12. WebSocket actually tested?

**No.** Live socket requires credentials; the `live` CLI correctly reports
`FYERS runtime verification blocked because credentials were not available.` WebSocket logic was unit-tested with mocked messages only.

## 13. Exact sample data received

None from FYERS (no live call). The offline fixture used mirrors the documented shape:
`{"s":"ok","candles":[[<epoch>,100.0,102.0,99.0,101.0,1000.0], ...]}` → normalized to the internal OHLCV frame and passed through `validate_ohlcv` successfully.

## 14. Problems encountered

- **`websocket-client` not installed** → added to the venv (and documented as optional dep; live path imports it lazily and fails clearly if absent).
- **Windows lacks IANA tz data** → added `tzdata` so `zoneinfo("Asia/Kolkata")` works.
- **Test assertion error (candle volume)** → the aggregator was correct; the test wrongly assumed 3 ticks in the first 5m bar when 4 fit. Fixed the test, not the code.
- **WS message `T:"t"` collision** → initial code skipped `t` as a control frame, but FYERS uses `t` for both subscribe-ack and SymbolUpdate quotes. Removed `t` from the skip list so quote messages are normalized. (This is a best-effort assumption pending live validation — see §5.)

## 15. Known limitations

- FYERS pricing unverified; live API untested (no creds).
- Multi-chunk historical fetching (>100d) not implemented.
- `from_fyers_symbol` handles equity/index forms; complex F&O reversibility is partial.
- WebSocket framing assumptions need live confirmation.
- No real-time "current bar" streaming reconciliation against the stored closed-bar set yet.
- Binance remains the working dev/test provider; both coexist via the factory.

## 16. Day 4 recommendation

1. **Obtain FYERS credentials** (or a FYERS paper/demo app) and run a real end-to-end test: auth → `/history` for NSE:SBIN-EQ (1d and 5m) → store → validate → snapshot → AI view → signal; then a short live WebSocket session logging normalized events. Update `FYERS.md` with *actual* observed payloads.
2. **Multi-chunk historical fetch** to exceed the 100d/366d caps.
3. **Instrument master** ingestion (FYERS symbol CSV) → populate `InstrumentRegistry` for full NSE/BSE coverage instead of the curated default list.
4. **Risk engine** + **backtester** (Day 2 roadmap) consuming the now-available Indian snapshots.
5. **Paper trader** scaffold consuming `InternalMarketEvent` (decoupled from FYERS).
6. **Confidence calibration** for the AI analyst (Day 2 item still open).
