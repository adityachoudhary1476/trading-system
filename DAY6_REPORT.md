# Day 6 Report — Derivatives & Commodities Foundation

**Date:** 2026-08-29
**Scope:** Prepare the system for F&O and commodity trading *data + instrument
intelligence only*. No order execution, no broker trading, no positions, no leverage.

---

## 1. Objective reminder
Build the foundation so the existing FYERS historical pipeline can fetch, validate,
and store derivative (futures/options) and commodity contract OHLCV, with
collision-free storage and clear contract identity — without guessing FYERS
symbols and without breaking the equity path.

## 2. What was built

### Phase 1 — Instrument model (extended, not replaced)
`src/trading_system/india/instruments.py`
- `OptionType` enum (CE/PE).
- `Instrument.contract_id` property: a canonical, stable identity
  `EXCHANGE:UNDERLYING|EXPIRY|STRIKE|CE|PE|FUT`. NIFTY Jun future and NIFTY Jul
  future produce different ids; NIFTY 25000 CE and PE likewise never collide.
- `Instrument.__eq__/__hash__` keyed on `contract_id` so two distinct contracts are
  never treated as the same.
- `Instrument.future(...)` / `Instrument.option(...)` constructors.
- `InstrumentRegistry.register/get` now index by **both** the user-facing key and the
  `contract_id` (so equity symbols and full derivative tokens both resolve).

### Phase 2 — FYERS derivative symbol resolution (verified, not guessed)
`src/trading_system/india/derivatives.py`
- `DerivativeRequest` — provider-independent description (underlying, kind, expiry,
  strike, option_type).
- `to_fyers_derivative_symbol()` — builds the FYERS wire symbol from the verified
  format: `<ROOT><YY><MMM><STRIKE?><CE|PE|FUT>` (no dash).
- `from_fyers_derivative_symbol()` — parses a FYERS token back to a normalized
  `Instrument`.
- Format evidence came from the **installed `fyers_apiv3` SDK source**, e.g.
  `NSE:NIFTY50-INDEX`, `MCX:SILVERMIC25DECFUT`, `MCX:SILVERMIC20NOVFUT`.

### Phase 3 — Instrument discovery (provider-independent interface)
`src/trading_system/india/instrument_repository.py` (extended)
- `list_futures(underlying)`, `list_options(underlying, expiry?, option_type?)`,
  `get_expiries(underlying)`, `find_contract(underlying, expiry, option_type?, strike?)`.
- `src/trading_system/india/instrument_discovery.py` — `FyersInstrumentDiscovery`:
  wraps the live `optionchain(symbol, timestamp, strikecount)` endpoint (read-only),
  parses contracts into normalized `Instrument`s, caches them in the repo. Degrades
  gracefully (empty result, **never fabricates**) on auth/availability failure.

### Phase 4 — Derivative historical data
Reused the existing `BackfillEngine` / `backfill-history`. `backfill.py` now threads
the resolved `contract_id` through `BackfillSymbolResult` → `_df_to_rows` →
`MarketStore.upsert_many`. No second historical architecture was created.

### Phase 5 — Commodities
The normalized `Instrument` already supports `Exchange.MCX`. FYERS commodity futures
resolve via `derivatives.py` (e.g. `MCX:SILVERMIC25DECFUT`). The storage schema and
backfill path treat them identically to index/equity futures.

### Phase 6 — Storage / schema (backward compatible)
`src/trading_system/storage/database.py`
- Added `contract_id` column (`VARCHAR(64)`, indexed).
- `upsert_many` now includes `contract_id` in both the dedup key and the stored row,
  and in the existing-rows probe — so NIFTY Jun ≠ Jul, and 25000 CE ≠ PE, even if a
  provider ever returned an ambiguous symbol.
- `init_db` adds the new column via `ALTER TABLE` only if missing (existing SBI/equity
  rows are untouched; their `contract_id` defaults to `symbol`).

### Phase 7 — Validation
`src/trading_system/data/validation.py`
- Added `validate_contract_identity(instrument)` — provider-independent structural
  check (missing underlying/expiry, bad ISO expiry, invalid strike, non-CE/PE
  option type). Expired contracts produce a **warning** (not an error); legitimate
  derivative behavior (wide strikes, weekly expiries) is never rejected.

### Phase 8 — CLI
`instruments` command extended (no existing command removed):
```
python -m trading_system instruments --underlying NIFTY --type futures
python -m trading_system instruments --underlying NIFTY --type options --expiry 2025-12-25
python -m trading_system instruments --underlying NIFTY --discover   # live option chain
```
`backfill-history` unchanged in usage; it now stores derivative `contract_id`.

## 3. Real FYERS verification — BLOCKED (expired token)
The `.env` `FYERS_ACCESS_TOKEN` **has expired** (returns `-16` "Could not
authenticate"). This was confirmed: the same command that succeeded earlier today
(`backfill-history --symbols NSE:SBIN --timeframe 1d --days 30`) now reports
`AUTH_ERROR` with a clear message — exactly the safe behaviour required. **No live
derivative/commodity data was fetched and none is claimed.** The discovery engine was
validated end-to-end with a mocked `optionchain` response (parsing + repo queries
work). Re-run Phase 10 once a valid token is present:
```
python -m trading_system instruments --underlying NIFTY --discover
python -m trading_system backfill-history --symbols NFO:NIFTY25DECFUT --timeframe 5m --days 10
python -m trading_system backfill-history --symbols MCX:SILVERMIC25DECFUT --timeframe 1d --days 30
```

## 4. Tests
- New: `tests/test_derivatives.py` — **24 tests** (futures/options identity, CE/PE &
  expiry distinction, FYERS symbol resolution, commodity representation, DB
  uniqueness, derivative backfill normalization, malformed-metadata validation,
  equity behavior unchanged).
- Full suite: **169 passed, 0 failed** (145 Day 1-5 baseline + 24 Day 6). No
  regressions.

## 5. Known limitations
- **Token expiry** blocks live verification (see §3).
- FYERS option/commodity symbols omit the day-of-month in the token; the parsed
  `Instrument.expiry` is normalized to the last day of the contract month. The
  authoritative exact expiry comes from `optionchain` discovery.
- Historical data feed / retention policy for F&O on FYERS is **unverified** (no
  official source confirmed); the command reports the actual returned range.
- `optionchain` was probed and returned HTTP 404 at `/options-chain-v3` and auth
  `-15` on `/quotes` with the current (expired) token; the SDK method signature and
  documented params (`symbol`, `timestamp`, `strikecount`, `greeks`) are wired.

## 6. Safety
DATA + INSTRUMENT INTELLIGENCE ONLY. No order placement, broker execution, position
open/close, leverage, or risk overrides. Credentials read from env, never printed.
