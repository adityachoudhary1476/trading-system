# AGENTS.md

Development notes for the FINOVA MARKETS trading system, focused on the
Phase 21 paper-trading stack this repo currently exercises.

## Paper-trading stack

- **Frontend**: `frontend/` (Vite + React 18 + TypeScript + Vitest + jsdom).
  The paper-trading pages live under `frontend/src/pages/paper/` and the shared
  picker/modal under `frontend/src/components/paper/`.
- **Backend API**: a stdlib HTTP server in `src/trading_system/paper_api/`.
  It is **not** a Vercel serverless function. It is started by the
  `paper-api` CLI subcommand (see `src/trading_system/__main__.py`).
  The frontend `paperApi` client targets `http://127.0.0.1:8765` by default
  (override with Vite env var `VITE_PAPER_API_URL`).

## Running the paper-trading environment (dev)

```bash
# 1. Start the paper-trading API server (localhost:8765, paper-only).
python -m trading_system paper-api --host 127.0.0.1 --port 8765

# 2. (optional) Seed one demo deployment from a validated strategy spec.
#    Idempotent: safe to re-run; reports an existing deployment if present.
python -m trading_system paper-seed --symbol NSE:SBIN --timeframe 1d

# 3. Start the frontend dev server.
cd frontend
npm run dev   # -> http://localhost:5173
```

The `paper-seed` command creates a single demo deployment (SMA(5) trend
following, `NSE:SBIN`, `1d`, 100000 cash), persists a checkpoint, and is the
canonical dev/demo seed path. After seeding, open
`http://localhost:5173/paper/overview` — the deployment dropdown populates
immediately and selecting the deployment renders the terminal with its
account/positions/performance/health/risk/circuit-breaker snapshot. No live
market-data feed is required for the terminal state to render.

## Key endpoints (Phase 21 router)

| Method | Path | Notes |
|--------|------|-------|
| GET    | `/health`            | liveness |
| GET    | `/deployments`       | list (query: `symbol`, `timeframe`, `status`, `strategy_id`, `limit`) |
| POST   | `/deployments`       | create (`spec` or `strategy_id`; runs the deployment gate) |
| GET    | `/deployments/:id`   | get one |
| GET    | `/deployments/:id/dashboard` | full terminal snapshot (works from a persisted checkpoint; no live runner required) |
| POST   | `/deployments/:id/activate \| pause \| resume \| stop` | lifecycle |
| POST   | `/deployments/:id/orders`   | external order intent (paper broker) |

The dev server uses relaxed evidence requirements (no research/walk-forward
evidence needed); the gate still enforces paper-only mode, spec identity
binding, symbol/timeframe match, and non-retired/non-rejected strategy status.

## Phase 22 — Adaptive Multi-Strategy Market Intelligence

Phase 22 extends the existing Phase 17 research scoring with regime-aware
strategy allocation. All Phase 22 components are **deterministic**,
**paper-only**, and do **not** require live market data.

### Backend modules

| Module | Purpose |
|--------|---------|
| `src/trading_system/research/phase22.py` | 5 strategy specs (Trend Following, Momentum, Breakout, Mean Reversion, VWAP), `RegimeClassifier`, `AdaptiveStrategySelector`, `RegimeAwareScorer` |
| `src/trading_system/research/intelligence_v3.py` | V3 intelligence: multi-timeframe features, evidence ledger v2, confidence v2 |
| `src/trading_system/research/market_context.py` | Market intelligence contexts (breadth, India VIX, FII/DII flow, sector, news, cross-asset) |
| `src/trading_system/research/news_intelligence.py` | V4 news pipeline: RSS providers, normalization, dedup, entity resolution, classification, sentiment (optional, `NEWS_ENABLED=false` by default) |
| `src/trading_system/research/patterns.py` | Historical pattern engine: fingerprint, similarity, outcome analysis |
| `src/trading_system/research/v4_compare.py` | Strategy comparison utilities |
| `src/trading_system/research/v5_validation.py` | V5 validation: causal snapshots, calibration, bootstrap CI, lookahead audit |
| `src/trading_system/research/causal_snapshot.py` | Causal snapshot builder (OHLCV ≤ T, no lookahead) |
| `src/trading_system/research/historical_data.py` | Local file data adapter (CSV/JSON/Parquet) |
| `src/trading_system/research/run_registry.py` | Append-only research run registry |

### Phase 22 API routes (Phase 21 router)

| Method | Path | Notes |
|--------|------|-------|
| GET    | `/strategies`  | List all Phase 22 strategy specs (no market data required) |
| GET    | `/regime`      | Classify current market regime (requires market data provider) |
| GET    | `/allocation`  | Compute adaptive strategy allocation (requires market data provider) |

`/regime` and `/allocation` return 400 when no market data provider is
configured — this is expected behaviour. The `paper-api` CLI does **not** load
live market data. A `market_data_provider` callable can be passed to
`PaperTradingControlCenter.from_engine()` for offline testing.

### Frontend (Phase 22Q)

- `frontend/src/pages/paper/PaperStrategies.tsx` — Strategy Leaderboard
  (lists all Phase 22 strategies), Current Market Regime, Recommended
  Allocation, and single-strategy detail view.
- `frontend/src/lib/paperApi.ts` — `getStrategies()`, `getRegime()`,
  `getAllocation()` client methods.
- `frontend/src/types/paper-api.ts` — `Phase22Regime`, `StrategyCategory`,
  `Phase22StrategySpec`, `RegimeResponse`, `AllocationResponse` types.
- `frontend/src/pages/__tests__/PaperStrategies.test.tsx` — 7 tests covering
  loading, error, empty, and data states.

## Lint / typecheck / test

### Frontend (`frontend/`)

```bash
npm run typecheck   # tsc -b --noEmit
npm test            # vitest run
```

Paper-trading tests: `frontend/src/pages/paper/paper-trading.test.tsx` and
`frontend/src/components/paper/__tests__/paperShared.test.tsx`.

### Backend (root)

```bash
# Phase 20 + Phase 21 + Phase 22 API/control-center coverage
python -m pytest tests/test_phase21_api.py tests/test_phase20_control_center.py tests/test_phase22_api.py -q

# Full backend test suite
python -m pytest tests/ -q

# Phase 22 V3/V4/V5 research coverage
python -m pytest tests/test_intelligence_v3.py tests/test_news_intelligence.py tests/test_pattern_engine.py tests/test_v4_integration.py tests/test_v5_*.py -q
```

## Notes / gotchas

- The paper API server is loopback-only by default; it does **not** contact any
  live broker or market-data provider. It is paper-only.
- `created_at` on a deployment summary is populated from the DB record; the
  dashboard snapshot is the source of truth for terminal rendering.
- The paper-trading dropdown (`DeploymentPicker`) renders a loading spinner,
  an explicit network-error state with Retry, and an empty state with a
  "Create paper deployment" affordance — it no longer silently renders an
  empty dropdown when the API is unreachable.
