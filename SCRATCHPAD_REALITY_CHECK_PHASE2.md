# Reality-Check Phase 2 — COMPLETE (revised)

## Repo Structure: MONOREPO (not single project)
- **Main project**: `src/trading_system/` — 2491 tests, ALL PASSING (exit 0), Phase 21/22/23
- **Legacy NIFTY sandbox**: root-level scripts (`backtest_engine.py`, `deep_analysis.py`, `run_all_strategies.py`) — SEPARATE from main, still has all ZAM8 bugs
- **`trading-system-audit/`**: audit project (ZAM8 report lives here)
- Sub-repos: `bhav/`, `agency-agents/`, `algo_trading_strategies_india/`, `learn-trading-from-x/`, `vibetrading-inspection/`
- Git worktrees: `bramble-viburnum`, `charm-marlin`, `shocking-raver`
- Production: FastAPI + Supabase + PostgreSQL on Railway (`railway.json`: 2 services)

## CORRECTIONS to Phase 2 (stale findings)
| Claim (Phase 2) | Reality (Phase 2 reality-check) | Verified |
|---|---|---|
| "276 tests passing" | 2491 tests, ALL passing | execution |
| "no broker/order code exists (grep-verified)" | Paper-only execution layer exists (`execution/` module). Safety boundary INTACT: PaperBroker is simulation-only, no live broker implementations, imports nothing from fyers/upstox | execution |
| "Only NSE:SBIN 1d (2477 bars)" | 18+ instruments: SBIN(2748), NIFTY50(2638), BANKNIFTY(30m+1d), NIFTY(30m+1d), 10+ largecaps(743), BTCUSDT(365), ETHUSDT(365) | execution |
| "Cross-sectional research impossible (needs >=5 instruments)" | IMPOSSIBLE → POSSIBLE (18+ instruments) | execution |
| "Frontend NOT wired to Day 8/10 APIs" | IS wired: paperApi.ts (getStrategies, getRegime, getAllocation), PaperStrategies.tsx, PaperTrading.tsx | inspection |
| "India cost model NOT implemented" | EXISTS: research/costs.py:IndiaTransactionCostModel, injectable into PaperBroker | inspection |

## NEW Findings (not in Phase 2)

### CRITICAL: CI — MISSING
- No `.github/workflows/` at repo root
- AGENTS.md claims "CI gate: the look-ahead regression test + warmup test must run on every PR" — **FALSE**
- Only CI in `agency-agents/.github/workflows/` (for sub-repo, not main project)
- **Action**: @developer — add CI workflow enforcing test_phase21_api, test_v5_causality, test_warmup

### Evidence Forensics — COMPROMISED THEN DETECTED
- `data/market_data.db` `strategy_lifecycle_events` table has a FORENSIC QUARANTINE entry
- A strategy's evidence was manually fabricated: claimed PAPER_APPROVED status with fake metrics
- Actual tournament run produced REJECTED/score=37.0
- System correctly detected, quarantined, and retired the strategy
- Shows evidence store CAN be compromised, but detection works

### Paper Sessions — INACTIVE
- All 6 paper sessions in `data/market_data.db`: 0 processed bars, 0 generated signals, 0 submitted orders, 0 fills
- Bots created but never ran

### Database State
- `backend/data/market_data.db`: EMPTY (0 rows in market_data, paper_deployments, paper_sessions)
- `data/market_data.db`: 16,340 rows, has real data (local dev DB)
- Production DB not seeded locally

### Phase 7 Safety Layer — ROBUST
- `autonomous/safety.py`: KillSwitch (fail-closed, explicit operator resume), SafetyValidator (8 pre-trade checks), IdempotencyGuard (decisions, bars, deployments)
- Emergency order invariants: cannot increase exposure, cannot reverse through zero, cannot open from flat
- All checks are deterministic pure functions (no wall-clock reads inside check methods)

### Backend Safety
- `backend/services/broker.py`: token decryption for Upstox market DATA only, explicitly "read-only market data"
- `backend/runtime.py`: uses "DEDICATED Upstox account for the live market data feed"
- No order-placement routes in backend/routes/
- Paper API server: loopback-only by default (127.0.0.1), bounded request bodies (1 MiB)

### Production Pipeline
- `railway.json`: finova-backend (FastAPI) + finova-autonomous-scheduler (worker, bot-nifty-options)
- `backend/routes/paper_api.py`: FastAPI adapter over PaperAPIRouter (paper-only via PaperBroker)
- Complete flow: API → PaperAPIRouter → PaperTradingControlCenter → PaperStrategyRunner → PaperBroker (simulation)

## Legacy Sandbox Bugs (ZAM8) — STILL VALID
Located in root-level `backtest_engine.py`, `deep_analysis.py`, `run_all_strategies.py`:
- **BUG-Z1**: deep_analysis.py crashes (KeyError: bb_lower_20_1.8) — column mults mismatch (1.8/2.2 vs generated 1.5/2.0/2.5)
- **BUG-Z2**: Full-sample grid search (103 combos tuned on ALL data, no train/test split)
- **BUG-Z3**: IS/OOS split uses full-sample-tuned parameters (not a valid holdout)
- **BUG-Z4**: walk_forward_test uses FIXED params per fold (mislabeled as re-optimization)
- **BUG-Z5**: Sharpe error (trade Sharpe 12.81 → calendar Sharpe 1.33, 98.5% flat)
- **BUG-Z6**: bb_mr_20_2.0 placebo test fails (p=0.12, random timing matches 88%)
- **BUG-Z7**: bb_mr_20_2.0 n=17 trades < 30 floor (statistical insignificance)
- **BUG-Z8**: connors_rsi2 mutates df in-place
- These are in the SANDBOX, not the main project

## FYERS Token
- `FYERS_ACCESS_TOKEN` env var not set (expired/unavailable)
- `india/token_manager.py` is Upstox-based (TokenManager for Upstox auth)
- FYERS integration requires env vars not configured locally
- Not blocking paper trading (paper API doesn't need live market data)

## Phase 2 Corrections Summary
The Phase 2 assessment was based on a stale snapshot. The main project has evolved significantly:
- 2491 tests (not 276)
- Paper-only execution layer (not none)
- 18+ instruments (not 1)
- Frontend wired (not unwired)
- IndiaTransactionCostModel exists (not missing)
- CI is missing (not present)
- Evidence forensics already detected + quarantined
- Paper sessions inactive (created but not running)
