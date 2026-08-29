# Day 10.5 Report — Data Foundation + India Cost Model

**Date:** 2026-08-29
**Scope:** DATA / RESEARCH INFRASTRUCTURE PHASE. No execution, no orders, no broker
order APIs, no `.env` changes, no auto-TOTP/browser login.

---

## Definition of Done — status

| Requirement | Status |
|---|---|
| Existing architecture preserved | YES |
| FYERS token lifecycle explicit | YES (`india/token_manager.py`) |
| Refresh-token path works or fails clearly | YES (fails clearly: refresh token absent/expired) |
| Auth errors observable | YES (`auth-status` CLI + DataHealthMonitor integration) |
| No credentials leak into logs | YES (test-enforced) |
| Research universe abstraction exists | YES (`research/universe.py`) |
| Bulk backfill idempotent/resumable | YES (reuses existing `BackfillEngine`) |
| Rate limiting exists | YES (`--request-delay`, bounded retries/backoff) |
| Data coverage reporting exists | YES (`research coverage`) |
| India cost model exists | YES (`research/costs.py`) |
| Effective-date rates exist | YES (`EffectiveRate` table) |
| GST treatment correct | YES (test-enforced: STT/stamp excluded) |
| Backtester accepts cost model | YES (`BacktestConfig.cost_model`) |
| Cost tests pass | YES |
| Existing tests remain green | YES (307 passed, 0 failed) |
| No broker/order/execution code added | YES (grep-verified) |
| `.env` untouched | YES (only `.env.example` edited) |
| No automatic browser/TOTP login added | YES |
| Documentation complete | YES (this file + README + ARCHITECTURE) |

---

## Files changed

### New
- `src/trading_system/india/token_manager.py` — `TokenManager` (FYERS token lifecycle)
- `src/trading_system/research/costs.py` — India transaction-cost model
- `src/trading_system/research/universe.py` — `ResearchUniverse` / `UniverseRegistry`
- `research/universes.example.json` — explicit universe config (no fabricated constituents)
- `tests/test_token_manager.py` — 10 tests
- `tests/test_universe.py` — 4 tests
- `tests/test_costs.py` — 12 tests
- `tests/test_backtest_cost.py` — 5 tests
- `DAY10_5_REPORT.md`

### Modified
- `src/trading_system/india/data_health.py` — added `on_auth_status()` (non-breaking)
- `src/trading_system/research/backtester.py` — `cost_model` + `cost_segment` in
  `BacktestConfig`; `_round_trip_cost()` + `_infer_cost_segment()` helpers
- `src/trading_system/research/__init__.py` — exported costs + universe symbols
- `src/trading_system/__main__.py` — `auth-status`, `backfill-universe`, `research coverage` commands
- `.env.example` — documented `FYERS_REFRESH_TOKEN` + `FYERS_SECRET` (NOT in `.env`)
- `README.md`, `ARCHITECTURE.md` — Day 10.5 notes

---

## FYERS authentication

`TokenManager` (Phase 2–3):
- `token_status()` → best-known state without network (`AUTH_OK` / `ACCESS_TOKEN_EXPIRED`).
- `get_valid_access_token()` → returns current token or raises (no silent fabrication).
- `refresh_access_token()` → POSTs to `https://api-t1.fyers.in/api/v3/token` with
  `grant_type=refresh_token`, `refresh_token`, `client_id`, SHA-256 `checksum(client_id:secret)`.
  On success stores + returns new access token; on auth error → `TokenError` with
  `REFRESH_TOKEN_EXPIRED` semantics ("human re-authorization required").
- Distinguishable states: `ACCESS_TOKEN_EXPIRED`, `REFRESH_TOKEN_EXPIRED`, `AUTH_FAILED`,
  `NETWORK_ERROR`.
- **Never prints/logs/embeds** access token, refresh token, secret, or PIN (enforced by
  `test_secrets_never_in_logs` and `test_secrets_never_in_exception`).
- No browser/TOTP automation added (per Phase 18).
- Integrated with `DataHealthMonitor.on_auth_status()` (existing behavior intact).

**Real status:** `auth-status` reports `access_token_expired` (token present but no refresh
token configured). Live refresh NOT attempted (no refresh token; would require re-auth).
**AUTH BLOCKED** honestly reported.

---

## Research universe

`ResearchUniverse(name, symbols, segment, description)` + `UniverseRegistry`. Constituents
come from explicit config (`universes.example.json`) — **never fabricated**. The default
registry seeds only the 6 real instruments already in `InstrumentRegistry`, plus clearly
marked scaffolds (`NIFTY50`, `NIFTY100`, `LIQUID_FNO`, `MCX_RESEARCH`) with
`requires_constituents=True` (blocked until the official list is pasted). `validate()`
gates runnable universes.

---

## Backfill

Reused existing `BackfillEngine` (idempotent via `UNIQUE(symbol,timeframe,timestamp,
provider,exchange)`; resumable; rate-limit aware; bounded retries; typed FYERS errors).
New `backfill-universe` CLI iterates the universe; reports per-symbol status; honors
`--dry-run`, `--request-delay`; unsupported/missing symbols reported (not silently skipped).
Example: `python -m trading_system backfill-universe --universe DEFAULT_BASKET --timeframe 1d --days 90`.

---

## India cost model

`research/costs.py`:
- `TransactionCostModel` (interface) → `IndiaTransactionCostModel`.
- `CostBreakdown(brokerage, stt_ctt, exchange_charges, sebi_fee, stamp_duty, gst, ipft, total)`
  with explicit INR units.
- `EffectiveRate(name, segment, rate, basis, side, effective_from, source)` — historical
  backtests pick the rate whose `effective_from` is latest ≤ trade date.
- Segments: `EQUITY_DELIVERY, EQUITY_INTRADAY, EQUITY_FUTURE, EQUITY_OPTION,
  COMMODITY_FUTURE, COMMODITY_OPTION`.
- **GST** computed ONLY on taxable components (brokerage + exchange + SEBI + IPFT);
  STT/CTT and stamp duty explicitly excluded (test-enforced).
- **Effective-date F&O STT**: pre-2026-04-01 (futures 0.0002 / options 0.00125) vs
  post-2026-04-01 (futures 0.0005 / options 0.0015) encoded and verified by
  `test_effective_date_stt_change`.
- Missing required rate → `CostNotConfigured` (never silently zero).
- Rates sourced from published FYERS/NSE schedule; **RE-VERIFY before live use**. Pre-2026
  rates encoded from the NSE circular (documented), not invented.

Backtest integration: `BacktestConfig.cost_model` + optional `cost_segment`. When set, each
trade's round-trip cost uses the real `CostBreakdown`; otherwise the generic
`transaction_cost_pct` applies (backward compatible). Warmup, evaluation window, look-ahead
protection, trade ledger, risk controls all preserved (tests confirm).

---

## Tests

**31 new tests; full suite 307 passed / 0 failed** (276 baseline + 31).
- `test_token_manager.py`: status states, refresh success, expired refresh token, network
  failure, missing secret, **secrets never in logs/exceptions**.
- `test_universe.py`: default registry real instruments, no fabrication, validation gates,
  dedup, config roundtrip.
- `test_costs.py`: brokerage cap/floor, futures STT sell-side turnover, options premium STT
  sell-side, **GST excludes STT/stamp**, SEBI turnover, effective-date 2026-03-31 vs
  2026-04-01 differ, missing-rate-raises, determinism, total==sum.
- `test_backtest_cost.py`: generic cost unchanged (back-compat), injected model changes
  cost, ledger reconciles, warmup preserved, determinism.

---

## Real-data verification

- `auth-status`: → `access_token_expired`, `AUTH BLOCKED` (honest; no refresh token).
- `research coverage --universe DEFAULT_BASKET --timeframe 1d`: real SBIN = 2,477 bars
  (2016-08-30 → 2026-08-27); other 5 default instruments reported MISSING (not fabricated).
  Contract-identity / duplicate / validation checks PASS.
- `backfill-universe --dry-run`: plans 6 symbols, zero fetch (no network).
- Cost sanity (real numbers, labeled, not a claim of profitability):
  NIFTY futures 1-lot RT cost ≈ ₹998.85 (brokerage ₹40, STT ₹937.50, GST ₹8.79, SEBI ₹3.75);
  option premium STT ₹13.50 = 0.0015 × 120 × 75 ✓.

---

## Limitations (honest)

1. **FYERS token expired / no refresh token** → live backfill NOT performed. `auth-status`
   reports AUTH BLOCKED. No live pull claimed.
2. **Only SBIN stored** → cross-sectional research still blocked. `research coverage` shows
   the gap honestly. NIFTY50/100/LIQUID_FNO scaffolds require the official constituent list
   (not fabricated here).
3. **Cost rates are encoded from published FYERS/NSE schedules** and must be re-verified
   against the current brokerage sheet before any capital decisions. State-dependent stamp
   duty uses an NSE default and is overridable.
4. No automatic daily auth / scheduler added (per Phase 18) — token lifecycle is correct and
   observable first, automation deferred.
5. No execution/order code added or modified (grep-verified). `.env` untouched.

---

## Exact recommended next Day 11

**Strategy discovery (research-only).** Add `StrategySpec` (serializable) + a
`DiscoveryEngine` that turns a `Hypothesis` into candidate `Strategy` instances from
documented, bounded hyperparameter sets (NO optimization / no best-of selection yet — selection
comes after evidence). Wire `run_backtest` (warmup-aware) + `compute_performance` +
`ExperimentManifest` into an `analyze_strategy(...)` that records an `EvidenceRun` (regime,
OOS flag, cost model used). Extend the evidence store to mark OOS vs in-sample. Tests:
discovery determinism, spec↔strategy round-trip, OOS separation, no-optimization guard.
Reuse `ResearchRegistry` for all writes. Keep LLM strategy generation deferred to the later
sandbox phase (per Day 9 blueprint). No execution, no broker, no LLM code-gen yet.
