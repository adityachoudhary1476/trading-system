# Phase 1 Reality-Check Status — Finova Markets trading system
**Owner:** @reality-checker · **For:** @you · **Timebox:** ASAP (accelerated from the cancelled 48h plan)

This is the honest, evidence-grounded status. Nothing here is aspirational.

---

## 1. Two codebases — kept strictly separate (a previous confusion that inflated claims)

| Location | Nature | Reality-check verdict |
|---|---|---|
| `C:/Users/Owner/OneDrive/Desktop/trading-system/` | **Finova Markets** — the real system (frontend + FastAPI backend + `research/` spine + paper-trading + FYERS) | Core backtester & factors **PASS** causality audit (see §2) |
| `C:/Users/Owner/*.py` (root) | Standalone NIFTY backtest sandbox: `backtest_engine.py`, `strategies.py`, `deep_analysis.py`, `nifty_5y_full.pkl` | **FALSIFIED** — see §3 |

## 2.5. Evidence-store quality gate — reality-check  (#uqjg follow-on)
Read `research/evidence.py` + `research/risk.py`:
- `classify_quality` gate is conservative on trade-count (`insufficient` <10 OR no cost assumption;
  `marginal` <30 OR no OOS; `adequate` ≥30 + OOS + costs). It correctly demotes the 17-trade
  `bb_mr_20_2.0` to "marginal" ✅. `risk.py` defaults are conservative (long-only, 1.0 leverage).
- **`forward_return` = `prices.shift(-lag)/prices - 1`** → factor_T predicts return_T+lag, no leakage ✅.
- `ExperimentManifest.identity_hash` excludes run metadata ✅; schema migrations are additive-only ✅.
- **GAP (the real finding):** the quality gate has **no statistical-significance requirement** —
  it uses trade-count (≥30) as a proxy for "stable read" and never checks p-value / bootstrap CI /
  placebo. By this gate a 125-trade strategy that loses 21% would be "adequate"; a 30-trade winner
  could be luck. The `EvidenceRun` schema has no `p_value`/`placebo_q`/`ci_lower` field, so there is
  no place to enforce it. My checklist (#0yzh) requires placebo p<0.05 + CI-excludes-0; the
  evidence store does not. **Recommendation:** add a `significance` sub-field to the gate
  (>=30 trades AND placebo p<0.05 AND bootstrap CI excludes 0) before a strategy can reach
  `WALK_FORWARD_VALIDATED`.

## 2. Finova-core research layer — causality audit (#uqjg) — PASSED
Read and ground-truthed `research/backtester.py` + `research/factors.py`, then ran the project's
own enforcement tests:
- **44/44 tests pass** (`test_v5_causality.py`, `test_warmup.py`, `test_factor_analysis.py`, `test_factors.py`).
- Engine: next-bar-open fills from prior-bar signals ✅; warmup/eval split excludes pre-warmup
  trades from reported metrics ✅; India-aware `TransactionCostModel` hooks exist ✅.
- Factors: every factor uses only data ≤ T; `forward_return` is `prices.shift(-lag)/prices-1`
  (factor_T → return_T+lag, never T) ✅; IC requires `MIN_CROSS_SECTION>=5` instruments (no
  fabrication when fewer) ✅.

**One latent fragility (flagged, not look-ahead):** `factors.py._trend_strength` indexes
`idx.iloc[len(idx)-len(x):]` inside a rolling apply — returns last-60 positions, not the current
window. Harmless only because Spearman is rank-invariant to a monotonic reference; breaks silently
if the reference ever becomes non-monotonic. Code-smell, not bias.

## 3. Standalone NIFTY sandbox — falsified (separate deliverable)
Full detail: `RC_REPORT_NIFTY_BACKTEST.md`. Headline:
- **7 of 8 strategies lose money.**
- **`bb_mr_20_2.0` (only non-loser) FAILS the placebo gate:** timing-placebo p=0.12 (random entry
  timing matches it 88% of the time). Reported Sharpe 12.81 is a per-trade √252 artifact; true
  calendar Sharpe = 1.33 (inflated by 98.5% flat days). 17 trades over 5 years → too few to
  separate skill from luck.
- IS/OOS sign flips for donchian (+23%→−28%), ma_xo (+29%→−24%), price_ma_200 (+15%→−10%) →
  overfit / regime dependence.
- `deep_analysis.py` **CRASHES** (`KeyError: 'bb_lower_20_1.8'`); its parameter-stability section
  never produced output. Walk-forward functions are fixed-param re-tests, not true WFA.

## 4. Test-suite reality-check (against the stale "276 passing" claim)
- Collected now: **2491 tests across 98 files** (PROJECT_AUDIT.md §3's "276" is from Aug 29 and is
  stale — the suite grew with Phase 22/23 + v5_validation).
- Full run: **no hang** (steady progress 63%→66% under a 120s cap; ETA ~3 min).
- The 4 bias-critical suites all GREEN (§2).

## 5. Bias checklist / validation standards (#0yzh) — DONE
Written to `docs/rc_bias_checklist.md` (A=look-ahead, B=leakage, C=overfitting, D=significance,
E=regime, F=assumptions, G=engineering). Includes the gate decision rule: any 🔴 MUST item = reject.

## 6. What is BLOCKED / needs a decision
- **No execution code exists (by design — correct).** Paper-trading-only confirmed; this is the right
  architecture for the current phase. Do NOT add broker order code (would violate the safety boundary).
- **FYERS token expired** → no new data backfill; only `NSE:SBIN` 1d/5m stored → cross-sectional
  factor research is BLOCKED (needs ≥5 instruments; `MIN_CROSS_SECTION` enforces it).
- **India cost model not implemented** — `breakeven_fee_bps` is generic; core backtester has the
  `TransactionCostModel` hooks but they aren't populated for Indian equities/F&O.
- **Frontend not wired to research APIs** (contract exists: `FRONTEND_BACKEND_CONTRACT.md`).

## 7. Next — prioritized
1. Refresh FYERS token + backfill ≥5 liquid instruments (unblocks cross-sectional factor research —
   the actual signal-validation path, not the NIFTY-sandbox toy).
2. Replace `breakeven_fee_bps` with a populated India cost schedule.
3. Fix or delete root-sandbox `deep_analysis.py` (crashing grid). If kept, constrain its param grid
   to columns that actually exist; never tune on full-sample then report OOS.
4. Promote the bias checklist (§5) into CI as a gate: any PR that changes a backtested strategy
   must attach a placebo p-value + bootstrap CI + IS/OOS sign agreement.
