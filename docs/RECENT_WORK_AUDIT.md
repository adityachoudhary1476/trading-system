# Audit — Recent Work in the trading-system Repository

**Date of audit:** 2026-09-05  
**Repository:** adityachoudhary1476/trading-system (local clone at `C:\Users\Owner\OneDrive\Desktop\trading-system`)  
**Branch:** `master` (up to date with `origin/master`)  
**Commit count:** 109 total (history only on `master` and `feat/upstox-oauth-vercel`)  

---

## 1. Git State Summary

| Item | Value |
|---|---|
| Current branch | `master` |
| HEAD commit | `f7835ae` — `fix(quote): use only Upstox prev_close for previous close, no fallback to current price` (Fri Sep 4 23:20:08 2026 +0530) |
| Staged changes | **None** |
| Tracked modified files (uncommitted) | **7** |
| Untracked new files | **~40** |
| Divergence vs `origin/master` | **0 commits** (clean sync; local == remote) |

The most productive work in the recent sessions is **entirely uncommitted** in the
working tree. No commits have been made for the Phase 22 paper-API integration,
the V3-V5 research stack, or the paper-trading UI hardening — these exist only
as working-tree changes.

---

## 2. Recent Commit History (HEAD → older)

The last 9 commits (all **committed**) are dominated by **Vercel deployment
routing/build fixes** and one Upstox quote fix:

| SHA | Commit | Theme |
|---|---|---|
| `f7835ae` | fix(quote): use only Upstox prev_close for previous close | Live market data |
| `1a86b55` | fix(deploy): add explicit routes for API functions, static assets, SPA fallback | Vercel routing |
| `68ad1db`..`d220f02` | fix(deploy): routing refactor oscillation | Vercel routing |
| `3b00e76` | fix(deploy): restore framework:vite preset | Vercel routing |
| `aeb843b` | fix(deploy): restore rootDirectory=frontend | Vercel routing |
| `3bdf20d` | fix(vercel): remove framework:vite, add routes, remove unused status.ts | Vercel routing |
| `8440c94` | fix: update market status API endpoint path to upstox | Live market data |
| `973cb21` | fix(vercel): remove SPA fallback routes catching /assets/ paths | Vercel routing |
| `ef84551` | fix(vercel): explicit builds config to stay under Hobby 12-function limit | Vercel routing |
| `18bad40` | fix: add root vercel.json + fix Vite esbuild target | Build config |
| `0d0e290` | fix: requires-python >=3.10 -> >=3.11 | Environment |
| `9ba9371` | fix: pin @types/node to exact version | Build determinism |
| `d3f8355` | fix: remove cache:no-store from candles.ts | Vercel TS build |
| `eab540e` | Add Create Deployment feature to paper trading UI | **Phase 21 paper UI** |
| `816f8d8` | fix(frontend): render live in-progress 1m candle from 1Hz tick stream | Live market data |
| `200aed1` | fix: resolve Vercel TS errors in api/ proxy files + add api type-check to build | Build |
| `7819509` | fix: address Vercel npm warnings | Build |
| `8398152` | fix: add historical NIFTY/BANKNIFTY data access | Live market data |
| `26de190` | fix(ohlcv): correct 1m date range, bar-limiting, market status endpoint | Live market data |
| `22b60e0` | feat(paper): Day-12 paper trading engine + OHLCV stale-data fixes | **Phase 12 paper engine** |
| `68f7168` | feat: complete day 18 market data recovery | Live market data |

**Reading the signal:** the last ~20 commits are dominated by (a) a burst of
Vercel deployment/routing fixes and (b) live-market-data plumbing (Upstox,
candles, 1m rendering, NIFTY/BANKNIFTY historical access).

---

## 3. Working-Tree Changes (Uncommitted — the Recent Session Work)

### 3.1 Modified tracked files (7 files, +475 / −196 net lines)

#### `src/trading_system/research/__init__.py` (+173)
Massive **export re-homing**. The package now re-exports its entire V3-V5
intelligence stack so callers can `from trading_system.research import …`
without touching submodules. Notably it:
- **Removed** the import of `ForecastLedger`/`ForecastRecord`/`MIN_RESOLVED`
  from `.forecast_ledger` (the old V2 ledger import is gone from this spot —
  see gotcha #1 below).
- **Added** re-exports from `.market_context`, `.intelligence_v3`,
  `.news_intelligence`, `.patterns`, `.h4_compare`, `.historical_data`,
  `.causal_snapshot`, `.v5_validation`, and `.run_registry`.
- Extended `__all__` with all of the above new symbols (plus a duplicate
  `"compute_relative_strength"` entry — see gotcha #2).

#### `src/trading_system/__main__.py` (+116)
Added the **`paper-seed`** CLI subcommand (the canonical dev/demo seed path
documented in AGENTS.md):
- `_demo_seed_spec()` — deterministic SMA(5) trend-following `StrategySpec`
  for `NSE:SBIN` / `1d` / 100 000 cash. **Deliberately mirrors the frontend
  `sma5` preset** so seed and UI produce identical deployments.
- `_cmd_seed_paper_deployment()` — builds a `PaperTradingControlCenter` with
  **relaxed evidence requirements** (no walk-forward / validation / recent
  evidence needed), creates or finds an existing identical deployment,
  activates it, attaches a `PaperStrategyRunner` + `PaperBroker`, and
  persists a checkpoint via `save_session()`.
- Registered `paper-seed` in the argparse `main()` dispatcher with
  `--symbol`/`--timeframe` options.

#### `src/trading_system/paper_api/router.py` (+98)
**Phase 22 API surface** added to the paper-trading control-center router:
- `GET /regime` — classifies the current market regime from market data via
  `RegimeClassifier` (wraps Phase 17 `classify_regime` + volatility
  expansion/contraction detection). Returns regime, confidence, features,
  warnings.
- `GET /strategies` — lists all Phase 22 strategy specs
  (`build_phase22_strategy_specs`) with ids, indicators, entry op, short flag.
- `GET /allocation` — computes deterministic adaptive strategy allocation
  via `AdaptiveStrategySelector` (regime-aware weights = 0.6×compatibility +
  0.4×research-score).

All three new routes share a `load_market_data(symbol, timeframe)` call that
**delegates to the control center** (which may return `None` if no provider is
configured → 400).

#### `src/trading_system/paper/control.py` (+11)
- Added a `market_data_provider: Optional[Callable]` parameter to
  `PaperTradingControlCenter.__init__` and `from_engine()`.
- Added `load_market_data(symbol, timeframe)` method that delegates to the
  injected provider (returns `None` if absent). This is the seam the new
  Phase 22 routes use — the control center itself has **no built-in market
  data source**; it is injectable.

#### Frontend — `paperShared.tsx` (+59)
`DeploymentPicker` hardening:
- New `error` state (was silently empty on network failure).
- Explicit **network-error block** with the error message, a **Retry** button,
  and a **Create deployment** affordance.
- New `onCreateDeployment` and `refreshKey` props (the latter forces a reload
  after a sibling creates a deployment).
- Empty state now renders a "No paper deployments yet" message + create button
  instead of a silently-empty dropdown.
- Switched to `useCallback`-based `load()` to support the refresh wire-up.

#### Frontend — `PaperOverview.tsx` (+14)
- Imports + mounts `CreateDeploymentModal`.
- Adds an "overview-empty" CTA button ("Create paper deployment") that opens
  the modal.
- On modal `onCreated`, bumps `createSeq` (feeds back into `DeploymentPicker`
  via `refreshKey`) and selects the new deployment.

#### Frontend — `PaperDeployments.tsx` (−196)
- **Moved** `PRESET_STRATEGIES` + the inline `CreateDeploymentModal` component
  out to the new file `frontend/src/components/paper/CreateDeploymentModal.tsx`
  (pure extraction — no logic change, now shared by `PaperOverview` and
  `PaperDeployments`).

### 3.2 New untracked files

#### Backend — Phase 22 research modules (`src/trading_system/research/`)
| File | Purpose | Lines |
|---|---|---|
| `intelligence_v3.py` | V3 consensus, transitions, options analytics, evidence ledger V2, confidence V2, historical replay (no-lookahead), outcome labeling, calibration, feature performance | 1029 |
| `market_context.py` | Data contracts: `MarketBreadth`, `IndiaVIXContext`, `FIIDIIFlow`, `SectorContext`, `NewsEvent`/`NewsContext`, `CrossAssetContext`, quality tiers | 170 |
| `news_intelligence.py` | V4 news pipeline: RSS/Atom providers, normalizer, deduplicator, entity resolver, 36-type classifier, sentiment + impact engines, relevance scorer, polling service | ~580 |
| `patterns.py` | V4 historical pattern engine: market-state fingerprint, causal normalizer, similarity, library, no-lookahead `find_matches`, reliability-labeled reports, ablation configs | 584 |
| `historical_data.py` | V5 data adapter: `DatasetType` provenance, `HistoricalProvenance`, `validate_ohlcv`, `LocalFileAdapter` (CSV/JSON/Parquet) | 279 |
| `causal_snapshot.py` | V5 causal snapshot builder: OHLCV≤T, **closed higher-TF candles only** (in-progress candle dropped), news≤T, options≤T, context staleness policy | 173 |
| `v4_compare.py` | Walk-forward strategy comparison (V3 / tech / +news / +patterns) + metrics | n/a |
| `v5_validation.py` | V5 validation: `FullMetrics`, bootstrap CI, `improvement_test`, OOS walk-forward + `OOSLock`, confidence calibration, slippage sensitivity, `CausalityAudit`, `FinalVerdict` | n/a |
| `run_registry.py` | V5 append-only research run registry: config/dataset hash, git commit, seed, results | 122 |
| `phase22.py` | **Phase 22** adaptive multi-strategy intelligence: 7-regime classifier, 5 declarative strategy specs, regime×strategy compatibility matrix, deterministic `AdaptiveStrategySelector`, `RegimeAwareScorer` | 501 |

#### Backend — tests
| File | Coverage |
|---|---|
| `tests/test_phase22_api.py` | **Placeholder only** (1 line: `# test placeholder`). The Phase 22 `/regime`,`/strategies`,`/allocation` routes have **no tests** yet. |
| `tests/test_intelligence_v3.py` | V3 engine |
| `tests/test_news_intelligence.py` | V4 news (35 tests documented in `V4_NEWS_INTELLIGENCE.md`) |
| `tests/test_pattern_engine.py` | V4 patterns (28 tests documented) |
| `tests/test_v3_fixtures.py` | Synthetic fixture scenarios |
| `tests/test_v4_integration.py` | V4 end-to-end (22 tests documented) |
| `tests/test_v5_causality.py` | V5 no-lookahead audit |
| `tests/test_v5_historical_data.py` | V5 data adapter |
| `tests/test_v5_registry.py` | Run registry |
| `tests/test_v5_validation.py` | V5 verdict pipeline |
| `tests/fixtures/v3_fixtures.py`, `tests/fixtures/v4_fixtures.py` | Synthetic test data |

#### Frontend
| File | Purpose |
|---|---|
| `frontend/src/components/paper/CreateDeploymentModal.tsx` | Extracted shared modal: 3 presets (SMA5, SMA20/50, RSI14), form validation, create flow |
| `frontend/src/components/paper/__tests__/paperShared.test.tsx` | 5 tests: loading, error/surface, empty+create, select render, refreshKey refetch, onChange |

#### Docs
`docs/V3_INTELLIGENCE.md`, `docs/V4_HISTORICAL_PATTERNS.md`, `docs/V4_NEWS_INTELLIGENCE.md`,
`docs/V5_REAL_HISTORICAL_VALIDATION.md` — architecture + data-contract + limitations docs for each layer.

#### Scripts & helpers
`scripts/v3_research_demo.py` (224 lines, end-to-end V3 demo), `scripts/v4_news_pattern_demo.py`,
`scripts/v5_real_historical_validation.py`, `scripts/_v3_api_notes.txt`.

**Suspicious helper files** (see gotcha #3): `gen_phase22.py` (4 lines, trivial import
smoke-test), `write_phase22.py` (8-line stdin→file helper), and
`gen_phase22_v2.py` whose entire content is the single corrupted line
`print(" test\)`. These look like scratch/accident artifacts and should be
removed or cleaned up.

#### `AGENTS.md`
Listed as **untracked** in `git status` even though the Kilo environment reports
loading instructions from it. It is the Phase 21 paper-trading dev/operational
contract (CLI subcommands, endpoints, lint/test commands).

---

## 4. Cross-Repo Consistency Check

| Layer | Status |
|---|---|
| `paper-seed` CLI spec ≡ frontend `sma5` preset | ✅ Identical SMA(5) trend-following spec, `NSE:SBIN`, `1d`, `100000` cash. Confirmed `generated_by="paper-seed"` (seed) vs `"paper-ui"` (modal) — intentional label distinction. |
| Paper API `/dashboard` ↔ frontend terminal | ✅ Router dispatches `/deployments/:id/dashboard`; frontend `PaperOverview` consumes `DashboardSnapshotResponse`. |
| Phase 22 `/regime` ↔ `phase22.py` | ✅ Both use `RegimeClassifier().classify(df)`. |
| Phase 22 `/strategies` ↔ `phase22.py` | ✅ Both call `build_phase22_strategy_specs()`. |
| Phase 22 `/allocation` ↔ `phase22.py` | ✅ Both use `AdaptiveStrategySelector.allocate(df)`. |
| Phase 22 `load_market_data` seam ↔ router | ✅ Router calls `self.center.load_market_data(...)` which delegates to the injected `market_data_provider`. |

---

## 5. Test Run State

`test_output.txt` (an untracked artifact) captures a **full pytest run with
100% passing**: ~1 434 dots across 19 lines, ending at 100%. The only output
is a set of `Pandas4Warning` deprecation notices (`'d'` freq → `'D'`,
`Timestamp.utcnow` → `Timestamp.now('UTC')`) — **non-blocking warnings**, no
failures. This file appears to be a captured run and is **stale/untracked**.

Tests actually run: `test_backfill`, `test_derivatives`,
`test_phase19_operations`, `test_pipeline_e2e`, `test_research`, `test_storage`,
`test_v3_fixtures`, `test_v5_causality`, `test_validation` — i.e. the **existing**
Phase 17/19/20 test suite, **not** the new Phase 22 / V3-V5 fixture sets, which
are uncommitted and were not included in that captured run.

---

## 6. Key Findings & Risks

1. **The recent work is entirely uncommitted.** The 7 modified files + ~40 new
   files represent the bulk of recent effort (Phase 22 paper API, the V3-V5
   research stack, paper-trading UI hardening) and exist **only** in the
   working tree. Nothing has been staged or committed. If the user's goal is a
   durable checkpoint, this is the first action needed.

2. **Phase 22 API routes have no test coverage.** `tests/test_phase22_api.py`
   is a 1-line placeholder (`# test placeholder`). The three new router
   handlers (`_route_regime`, `_route_phase22_strategies`, `_route_allocation`)
   and the `load_market_data` seam are untested. Contrast with the existing
   Phase 21 API which has `test_phase21_api.py` (1064 lines, 9 test classes
   covering availability, deployments, dashboard, inspection, lifecycle,
   circuit-breaker, sessions, security, static safety, HTTP server).

3. **`gen_phase22_v2.py` is corrupted.** Its content is literally
   `print(" test\")` — a syntax error and an obvious accident. It will fail
   to parse if imported. Recommend deletion.

4. **`__init__.py` re-export hygiene issues:**
   - The `ForecastLedger`/`ForecastRecord`/`MIN_RESOLVED` import that the
     original code had from `.forecast_ledger` is **gone** from the new
     `__init__.py` (the block was commented out in the diff). If any
     non-`trading_system.research` caller imports those names from the package
     root, it will break.
   - A duplicate `"compute_relative_strength"` entry appears in `__all__`.

5. **`market_data_provider` is injectable but never wired in `__main__.py`.**
   `_cmd_seed_paper_deployment` builds the `PaperTradingControlCenter` via
   `from_engine(...)` **without** passing a `market_data_provider`. The CLI
   `paper-seed` path therefore works (it doesn't need market data), but the
   Phase 22 `/regime` and `/allocation` routes would return **400**
   ("no market data provider configured") unless the API server's
   `_cmd_serve_paper_api` injects one. Verify whether the server entrypoint
   wires a provider — otherwise the new routes are dead on arrival.

6. **No live broker connectivity in the paper stack (compliant).** `test_phase21_api.py`
   `TestStaticSafety.test_no_live_broker_imports` / `test_no_network_modules`
   forbid `requests`, `httpx`, `socket`, `http.client`, `urllib.request`,
   etc. in `paper_api/*.py`. The Phase 22 additions in `router.py` import only
   from `..research.phase22` and `..research.evidence` — **no live-network
   modules**, so the paper-only invariant is preserved.

7. **AGENTS.md is untracked.** The operational contract is in the working tree
   only. Low priority to commit, but worth noting.

---

## 7. Recommendations (no actions taken)

- **Stage + commit** the working-tree work in logical commits (research stack,
  paper API + Phase 22 routes, frontend UI hardening, AGENTS.md).
- **Add `test_phase22_api.py` coverage** for the three new routes
  (success path, `market_data_provider=None` → 400, determinism of weights).
- **Wire `market_data_provider`** in `_cmd_serve_paper_api` (or stub it from
  a local OHLCV loader so `/regime` and `/allocation` are reachable for demo).
- **Delete `gen_phase22_v2.py`** (corrupted) and `write_phase22.py` /
  `gen_phase22.py` (scratch helpers) unless they serve a documented purpose.
- **Fix the `__init__.py`** duplicate `__all__` entry and confirm whether
  the `forecast_ledger` re-exports were intentionally removed or need to be
  restored.
- **Run `pytest tests/test_phase22_api.py tests/test_phase21_api.py`** after
  writing the Phase 22 tests to confirm no regressions.
- **Remove stale `test_output.txt`** or regenerate it with the new test set
  included so the captured run reflects current coverage.
