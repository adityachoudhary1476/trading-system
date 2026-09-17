# Bias-Validation Checklist & Standards — Trading Strategy Backtests
**Owner:** @reality-checker · **Status:** ACTIVE GATE (any strategy/evidence that fails a MUST item is rejected)
**Scope:** applies to BOTH the Finova core (`trading-system/`) and the standalone NIFTY sandbox.
**How to use:** every backtest result presented to the team MUST be accompanied by a "checklist pass"
stamp showing each item below; a single MUST-fail rejects the result from promotion/paper-trade.

Legend: 🔴 MUST | 🟡 SHOULD | 🟢 NICE. Reproducible = must pass deterministically with a fixed seed and
re-runnable via one command. "Report" = artifact written to disk before claim is trusted.

---

## A. LOOK-AHEAD BIAS  🔴
- A1. Signal at bar T uses only data with timestamp ≤ T. Verify no `shift(-k)`, `.iloc[i+1]`, or
      `rolling(...).apply` that reaches into the future. (Core backtester passes — see
      `test_v5_causality.py`; warmup test `test_warmup.py` enforces no warmup leakage.)
- A2. Execution is at the NEXT bar's open given the prior bar's signal (no same-bar close-to-close
      peeking). The standalone `backtest_engine.py` does this (`entry_long.iloc[i-1]` → `open[i]`). ✅
- A3. Factor-vs-forward-return is causal: factor_T predicts return at T+lag, NEVER T. Verified in
      `factor_analysis.py` `forward_return` (`prices.shift(-lag)/prices - 1`). ✅
- A4. HTF/resampled candles exclude the still-forming (in-progress) bar. `closed_htf_candles`
      enforces this; TESTED. ✅
- A5. Stop-loss / take-profit evaluated on the prior bar's close, filled at next open (conservative;
      no intrabar peeking). ✅
- A6. **REJECT-FLAG:** `_trend_strength` in `factors.py` uses `idx.iloc[len(idx)-len(x):]` inside a
      rolling apply — this returns the LAST 60 positional indices instead of the current window. It
      is numerically harmless ONLY because the reference is monotonic (Spearman is rank-invariant).
      Flag as fragile: if the reference ever becomes non-monotonic the factor silently breaks.

## B. DATA LEAKAGE  🔴
- B1. **The two-codebase trap:** `backtest_engine.py` (root) and `research/backtester.py`
      (`trading-system/`) are DIFFERENT engines. Do NOT treat root-prototype results as Finova-core
      results. Report which engine produced each number. (The hive previously conflated them.)
- B2. Data file identity is verified by inspection, not name. `nifty_5y_full.pkl` is real (1236 daily
      NIFTY 50 bars, 2021-09-13→2026-09-10, no OHLC gaps). **NIFTY ≠ BANKNIFTY** — confirm the
      correct universe per result.
- B3. Indicator columns in the feature DataFrame MATCH what the strategy code requests.
      **REJECT:** `deep_analysis.py` crashes with `KeyError: 'bb_lower_20_1.8'` because
      `compute_indicators.py` only generates mult∈{1.5,2.0,2.5}. Any result relying on a missing
      column is void.
- B4. No column reads future data. (Indicators verified causal.) ✅
- B5. Survivorship: instruments that disappear from the universe are handled (or the limitation is
      stated). For a single index this is low-risk; for cross-sectional work it is high-risk.

## C. OVERFITTING  🔴
- C1. **Parameter tuning must NEVER touch the evaluation window.** Train/optimize only inside the
      pre-eval-start slice; validate on the held-out future. The claim "walk-forward" in
      `backtest_engine.walk_forward_test` is MISLABELED — it re-tests FIXED params per window, it does
      not re-optimize. Demand a true re-optimization WFA if "walk-forward" is cited.
- C2. **No single headline number.** Require IS, OOS (true holdout), and ≥3 out-of-time folds. If
      IS and OOS disagree in SIGN, the strategy is overfit (see: donchian +23%/−28%, ma_xo +29%/−24%).
- C3. **Small-sample floor:** fewer than ~30 trades in-sample → no significance claim allowed
      (`bb_mr_20_2.0` has 17 trades → claim is void).
- C4. **Multiple-comparisons awareness:** testing 7+ strategies and reporting only winners is
      cherry-picking unless a correction (Bonferroni/Sherring's) or an out-of-time gate is shown.

## D. STATISTICAL SIGNIFICANCE  🔴
- D1. **Trade-Sharpe is misleading.** Never report √252-annualized per-trade Sharpe as "the Sharpe."
      Report the **calendar Sharpe over the full equity curve** (flat days counted) and time-in-market.
      (`bb_mr_20_2.0`: trade Sharpe 12.81 → calendar Sharpe 1.33; 98.5% flat.)
- D2. **Bootstrap 95% CI on the calendar Sharpe** must exclude 0.
- D3. **Placebo (timing) test:** randomly relocate the SAME signal count to different bar positions;
      the real strategy must beat ≥95% of random timings (p<0.05). For `bb_mr_20_2.0`:
      **p=0.12 → FAILS** (random timing matches it 88% of the time).
- D4. **Sign-flip permutation** on trade returns (p<0.05) as a secondary check — weaker than D3;
      pass D3 is required, D2+D4 are supportive.
- D5. **OOS consistency:** sign of IS return must match sign of OOS return.

## E. REGIME DEPENDENCE  🔴
- E1. Report performance by regime (trend/vol/breadth) AND confirm it is not concentrated in one
      period. IS/OOS sign flips (C2) are the tripwire.
- E2. At least one adverse regime (high-vol, low-trend, or a 2020-style shock) must be in-sample.

## F. UNSUITABLE ASSUMPTIONS  🟡
- F1. Realistic costs: India cost model (STT + exchange + SEBI + stamp + brokerage + DP + GST +
      lot sizing + expiry rollover). The generic `breakeven_fee_bps` is a placeholder — must be
      replaced with `TransactionCostModel` segments. Core backtester has the hooks; sandbox does NOT.
- F2. Slippage scales with volatility/impact, not a flat 10 bps — verify under stress.
- F3. Liquidity: daily NIFTY is deep; intraday/high-freq strategies must model impact. The root
      scripts don't → their results are suspect for any non-daily or high-frequency variant.
- F4. No overnight/gap risk beyond next-bar-open (gap from close→next-open is real).

## G. ENGINEERING INTEGRITY  🔴
- G1. **The script must run to completion.** `deep_analysis.py` CRASHES
      (`KeyError: 'bb_lower_20_1.8'`) → no parameter-stability output exists. A crashing script
      produces no valid result. Fix before trusting any of its printed numbers.
- G2. Determinism: same input → same output (seeded). Test exists in core (`test_warmup.py`). ✅
- G3. CI gate: the look-ahead regression test + warmup test must run on every PR.
      (`test_v5_causality.py` + `test_warmup.py` + `test_factor_analysis.py` — currently all GREEN.)

---

## Gate decision rule
Reject from promotion/paper-trade if ANY 🔴 item fails. A strategy "passes" only when:
A1–A5 ✅ · B3 ✅ · C1,C2,C3 ✅ · D1–D5 ✅ · E1,E2 ✅ · G1 ✅.

`bb_mr_20_2.0` FAILS: C3 (n=17<30), D3 (placebo p=0.12), G1 only for the core file (the crashing one
is the sandbox `deep_analysis.py`). Do not promote.
