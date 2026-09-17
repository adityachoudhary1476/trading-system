# Finova Markets — Prioritized Roadmap

*Generated: 2026-09-16 | Verified against commit 6acc2a2e*

## Mission
Make Finova Markets a robust autonomous trading research and execution platform.
Start by examining the current state and producing a prioritized roadmap of what
should happen next. Do not assume existing architecture, strategies, intelligence,
or validation methodology is correct.

## Repository State (Verified)
- **Phase:** 22/24 (NOT Day 10)
- **Tests:** 2491 tests in `src/trading_system/`, all passing
- **Safety:** Paper-only boundary intact (PaperBroker only, no live broker)
- **Instruments:** 18+ available (NSE equities, indices, crypto sandbox)
- **Frontend:** Wired to paper API at `http://127.0.0.1:8765`
- **CI:** **MISSING** — no `.github/workflows/` despite AGENTS.md claims

## Priority 1: Statistical Significance Gap (CRITICAL)

### Problem
The evidence gate (`classify_quality` in `evidence.py`) and the tournament gate
(`RejectionGate` in `scoring.py`) lack statistical-significance checks. The
`min_bootstrap_prob_positive=0.45` threshold compares against the raw win rate
(`sum(1 for r in trade_returns if r > 0) / len(trade_returns)` in tournament.py
line 416), NOT a real bootstrap probability.

Specific gaps verified:
1. `bootstrap_ci()` IS called (tournament.py:410) producing `(lo, hi, mean)`
   but the CI is only stored in `result.bootstrap` dict — never passed to
   `ScorerInput.bootstrap_return_ci` (which has a default of `(0.0, 0.0)`)
2. No `timing_placebo_pvalue()` function exists
3. No `block_bootstrap_pvalue()` function exists
4. No p-value fields in `ScorerInput` or `EvidenceRun`
5. `RejectionGate.evaluate()` only checks `bootstrap_prob_positive` (win rate)
6. `classify_quality` checks only trade_count + OOS + costs — no significance

### Requirements (per docs/rc_significance_gate.md + docs/rc_bias_checklist.md)
1. Newey-West t-stat on daily P&L: t ≥ 1.96 (two-tailed)
2. Block-bootstrap p-value (21-day blocks): p < 0.05
3. Timing-placebo p-value (signals shifted 10–60 days): p < 0.05
4. OOS Sharpe (annualized, post-cost): Sharpe ≥ 0.5
5. Holding-period sanity: median holding ∈ [1, 10] trading days
6. OOS/IS integrity gap: OOS Sharpe ≥ 0.8 × IS Sharpe

### Tasks
- Add `timing_placebo_pvalue()` + `block_bootstrap_pvalue()` to
  `src/trading_system/research/v5_validation.py` (placebo = permutation WITHOUT
  replacement; bootstrap_ci stays as-is for CI estimation)
- Wire `bootstrap_ci` output → `ScorerInput.bootstrap_return_ci`
- Add `placebo_pvalue` + `bootstrap_pvalue` fields to `ScorerInput`
- Add p-value checks to `RejectionGate.evaluate()`
- Schema migration for `evidence_runs` table (additive ALTER TABLE):
  `p_value`, `placebo_q`, `ci_lower`, `ci_upper`
- Extend `classify_quality` with significance sub-object per RC doc
- Fix hardcoded values in tournament.py: `cagr=0.0`, `volatility=0.0`,
  `parameter_stability=1.0`, `wf_oos_sharpe=None`, `wf_degradation=None`

## Priority 2: CI Enforcement (CRITICAL)

### Problem
No CI pipeline exists. AGENTS.md claims tests are enforced on every PR, but
there is no `.github/workflows/` directory.

### Tasks
- Create `.github/workflows/ci.yml` running:
  - Full backend test suite (`pytest tests/ -q`)
  - Frontend typecheck + tests (`npm run typecheck && npm test`)
  - Lint checks
- Gate on all tests passing

## Priority 3: FYERS Removal (Cleanup)

### Problem
`src/trading_system/india/__init__.py` docstring CLAIMS FYERS is removed, but:
- `src/trading_system/india/fyers.py` still exists (520 lines)
- `__main__.py:689` still imports `FYERSMarketDataProvider`
- `symbol_map.py` exports `to_fyers_symbol`, `from_fyers_symbol`
- `derivatives.py` exports `to_fyers_derivative_symbol`
- `.gitignore` still has `fyersDataSocket.log`
- `scripts/_superseded/fyers_auth.py` exists
- 18 test files reference FYERS

NOTE: `.env`/`.env.example` already Upstox-only. `pyproject.toml` has no
`fyers-apiv3` dependency. No FYERS credentials in env files.

### Tasks
- Delete `src/trading_system/india/fyers.py`
- Remove `__main__.py:689` import, switch to `UpstoxMarketDataProvider`
- Remove FYERS symbol functions from `symbol_map.py` and `derivatives.py`
- Clean `.gitignore` entry
- Delete `scripts/_superseded/fyers_auth.py`
- Update all 18 test files to use Upstox instead of FYERS
- Fix `__init__.py` docstring to accurately reflect state
