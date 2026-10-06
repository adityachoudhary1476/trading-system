# Plan: Greeks / OI / IV Analytics for NIFTY Options Paper Trading

> **Scope note.** This document covers the *observational* greeks surface that has shipped.
> Using greeks to **decide** trades — strike selection, sizing, portfolio limits, exits — is
> specified separately in [`docs/GREEKS_DECISION_LAYER.md`](docs/GREEKS_DECISION_LAYER.md),
> which is implemented (Phases 0–5, PAPER-only, env-gated and reversible).

## Status (updated)

Shipped, paper-only, all deterministic:

| Item | Where | Note |
|------|-------|------|
| Black-Scholes greeks | `src/trading_system/autonomous/greeks.py` | `calculate_greeks`, `implied_vol_from_price` (bisection), `greeks_from_market_inputs` |
| Volatility units | `src/trading_system/autonomous/greeks.py` | `normalise_iv`: percent (Upstox) -> decimal, cutoff at 3.0; zero/negative/unparseable -> `None`. Single shared authority — both `option_quotes.py` and the chain provider call it |
| IV from the existing quote | same | `bid_iv` / `ask_iv` / `implied_vol` read from the payload already fetched; **no extra API call** |
| Entry + current delta anchors | `src/trading_system/autonomous/controller.py` | captured during mark-to-market; anchor only within 1800 s of the fill |
| Persistence | `src/trading_system/paper_trading/__init__.py`, `src/trading_system/paper/book.py` | greeks fields ride along in `positions_json`; no DDL, backward compatible |
| Delta-decay exit | `src/trading_system/autonomous/portfolio.py` | **disabled by default**, env-gated, ordered after SL/TP |
| Per-strike chain greeks | `src/trading_system/autonomous/chain_analytics.py` | ATM-window greeks for every strike/side; see the section below |
| Analytics API + page | `GET /deployments/{id}/options/analytics/{symbol}`, `/paper/options` | read-only; nullable greeks, coverage reporting |

Chain persistence already existed and was made **fail-loud** rather than silently
dropping snapshots: `UpstoxOptionChainProvider._persist()` now raises, `get_chain()`
records the failure, and `persistence_status()` surfaces it to
`GET /api/paper/options/capability` as `DEGRADED` while trading continues.

### Corrections to the original plan below

Several assumptions in the plan were stale. Do not re-derive them:

- **"Enable option chain provider ... check if it exists"** — `UpstoxOptionChainProvider`
  already exists and is wired.
- **"`option_position_iv` table, insert after each quote fetch"** — not needed for greeks.
  Quotes already carry IV, and the paper book already stores per-position state, so a
  new table would duplicate data. OI history is a separate question (see below).
- **"GET /v3/market/option-chain"** — the endpoint in use is
  `GET /v2/option/chain?instrument_key=...&expiry_date=...`.
- **`PaperDeploymentConfig` for the risk knob** — must not gain fields.
  `deployment_identity()` hashes the whole config, so a new field changes every live
  deployment's id and orphans its session, book and open positions. Hence env vars.

### Delta-decay exit: why it is off by default

`AUTONOMOUS_DELTA_DECAY_ENABLED=1`, `AUTONOMOUS_DELTA_DECAY_FLOOR=0.5` (fraction of entry
delta retained). The rule is unvalidated on real fills, so it ships dark. When enabled it
can only close positions *sooner* than the configured percentage stop, never later:
it is evaluated after SL/TP, and unknown greeks decline to fire.

### Per-strike chain greeks + analytics API/frontend (shipped)

`src/trading_system/autonomous/chain_analytics.py` derives Black-Scholes greeks for a
bounded band of strikes around the money from the existing chain snapshot. It is
observational: no orders, no provider registration, no config changes.

| Layer | Location |
|-------|----------|
| Engine | `src/trading_system/autonomous/chain_analytics.py` |
| Endpoint | `GET /deployments/{id}/options/analytics/{symbol}?expiry=YYYY-MM-DD&strikes=N` |
| Expiry listing | `GET /deployments/{id}/options/expiries?symbol=NIFTY` |
| Page | `/paper/options` → `frontend/src/pages/paper/PaperOptionsAnalytics.tsx` |

Design decisions worth keeping:

- **The window is always explicit.** A NIFTY weekly chain runs to several hundred
  strikes whose greeks are numerically valid but economically meaningless. Default
  is ±10 strikes around the money, hard-capped at ±100; `window_truncated` plus a
  note say when the cap bit. `strikes=0` means ATM only.
- **`None` never renders as `0.00`.** An uncomputed delta and a real zero delta mean
  opposite things, and a greeks table cannot show the difference once both are numbers.
  Every greeks field is nullable end-to-end, and the frontend renders a dash.
- **Totals travel with their row count.** `rows_with_greeks` / `coverage` sit beside
  every aggregate, because a net delta from 6 of 40 rows is a different claim from one
  over all 40.
- **IV provenance is reported per row.** `iv_source` distinguishes the market's own
  bid/ask IV (`quote`) from volatility inverted out of a mid premium (`solved`); the
  latter is only as good as that mid.
- **No extra market-data calls.** The endpoint reuses the one chain snapshot the raw
  chain endpoint already fetches. Worst case (every row requiring an IV solve) measures
  ~60ms for 402 rows, pinned by a test so the cap cannot silently regress.

Related fix found while building this: `_route_options_chain` read the `expiry` query
parameter with `ctx.query.get(...)`, which returns a **list**. It passed
`['2026-11-19']` to `get_chain()`, so the expiry comparison could never match a real
row and the endpoint would 404 on valid data. Now uses the existing `_single()` helper,
with a regression test covering both chain endpoints.

### Expiry discovery (shipped)

The analytics route takes an explicit `expiry`, which left the page asking a human to
type a date. That is a bad prompt: NSE weekly and monthly expiries do not follow a
formula, and `InMemoryOptionsChainProvider._available_expiries()` is US third-Friday
arithmetic that would hand a user dates this exchange never lists.

| Layer | Location |
|-------|----------|
| Source | `CurrentOptionDiscoverer.list_expiries()` in `src/trading_system/india/upstox_discovery.py` (authenticated `/option/contract`) |
| Provider | `UpstoxOptionChainProvider.list_expiries()` |
| Endpoint | `GET /deployments/{id}/options/expiries?symbol=NIFTY` |

Design decisions worth keeping:

- **Never a fabricated date.** Expiries come from the exchange listing or not at all.
  The synthetic test provider keeps its US calendar, and nothing on the NSE path may
  borrow it.
- **`unavailable` is not `none`.** An empty list reports `source: "unavailable"` plus a
  note, because "the exchange lists no expiries" and "the provider could not answer"
  are different claims and conflating them would hide an auth failure behind an empty
  dropdown. Expired and unparseable values are dropped but counted in `notes`, so
  nothing disappears silently.
- **Listing is not fetching.** The endpoint reads the contract list and no chain, and
  the analytics route reuses the single chain snapshot it already took. A listed value
  is guaranteed to satisfy the analytics route — a test feeds one straight through.
- **The cut is inclusive of today.** An expiry happening today still has a live chain;
  only yesterday's is gone. Both sides of that boundary are pinned by tests using a UTC
  clock, because a local-date call made one of them flip as the date rolled over.
- **Underlying is a choice, not a constant.** The page defaults to NIFTY but offers
  BANKNIFTY and FINNIFTY, and changing it refetches that underlying's expiries. The
  shortlist is a UI affordance, not a claim that any symbol is currently listed.

### Not started (deferred, in priority order)

1. **OI change flags** — `change_oi` is already persisted per strike, but there is no
   history table to diff against and no unusual-activity threshold.
2. **IV rank / percentile** — needs an IV history table; genuinely new storage.
3. **OI heatmap and IV sparkline** — the analytics endpoint and page carry the per-strike
   numbers these widgets need; the aggregation into a heatmap/sparkline is not built.
4. **IV-percentile entry strategy** — depends on (2); do not build before it.
5. **Portfolio delta/vega limits and max pain** — need the exit telemetry above before a
   limit can be calibrated.
6. **Backtest** — replay validation for any of the above before it is enabled.

---

## Original plan (stale — retained for context)

## Current State
- Backend capable, discoverer attached, quote authenticated
- Chain disabled (Phase 8A multi-leg only)
- options_enabled=True (DB + code + model default)
- Scheduler running, kill switch cleared

## Approach: Build incrementally
Start with what the existing pipeline can support, then add missing pieces.

## Phase 1 — Data (Week 1)

### 1. Enable option chain provider
- File: src/trading_system/research/phase23/deployment.py
- Add chain provider to Phase23DeploymentConfig
- Attach CurrentOptionChainProvider (check if it exists in codebase)

### 2. Persist IV per strike
- Model: src/trading_system/paper/schema.py (PaperPosition)
- Table: option_position_iv (instrument_key, timestamp, iv, delta, gamma, theta, vega)
- Insert via paper_api.py after each quote fetch

### 3. Verify Upstox option chain endpoint
- Endpoint: GET /v3/market/option-chain?symbol=NSE:NIFTY
- Fields: strike_price, bid_iv, ask_iv, volume, oi, change_oi
- Store raw JSON + parsed fields

## Phase 2 — Calculation (Week 2)

### 1. Implement Black-Scholes on chain data
- File: src/trading_system/research/options/analytics.py
- Functions: calculate_greeks(S, K, T, r, sigma, option_type)
- Use bid/ask mid-price as sigma input
- Cache results: 5s TTL per strike

### 2. Compute OI change
- Field: option_chain_snapshot (store hourly snapshots)
- Calc: current_oi - previous_oi (per strike)
- Flag: >20% change in OI = unusual activity

### 3. IV rank / percentile (20-period)
- Store IV history: option_iv_history table (1h candles)
- IV Rank = (current_iv - min_iv_20) / (max_iv_20 - min_iv_20)
- IV Percentile = % of 20-period IVs below current

## Phase 3 — Integration (Week 3)

### 1. Expose via API
- GET /api/paper/options/analytics/{symbol}?expiry=...
- Returns: greeks per strike, OI change, IV rank/percentile, max pain

### 2. Paper decision engine
- File: src/trading_system/research/phase23/strategy.py
- Use IV percentile < 20 as "IV is low" entry signal
- Use OI spike as confirmation
- Max 2 contracts per trade (existing constraint)

### 3. Frontend widgets
- TradingView-style strike table with color-coded Greeks
- OI heatmap per strike
- IV rank sparkline

## Phase 4 — Safety (Week 4)

### 1. Greeks-based risk checks
- Portfolio delta: |delta| < 50 (position limit)
- Portfolio vega: |vega| < 1000 (IV shock limit)
- Max pain: warn if expiry < 3D and ITM

### 2. Backtest validation
- Replay last 30 days of IV/OI
- Compare IV-percentile entry vs. buy-hold
- Target: >55% win rate on NIFTY expiry

## Data Flow
```
Upstox API → OptionChainProvider → AnalyticsEngine → PaperTradingEngine → DB
    ↓           ↓                    ↓              ↓
  IV per strike  Greeks (B76)     IV Rank/OI%    Trade decision
  OI per strike  IV Percentile    Max Pain       Risk checks
```

## Estimated Effort
- Phase 1: 5 hrs (data plumbing)
- Phase 2: 6 hrs (calculations + storage)
- Phase 3: 4 hrs (API + strategy)
- Phase 4: 3 hrs (risk + backtest)
- Total: ~18 hrs (2-3 weeks part-time)
