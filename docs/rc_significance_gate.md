# Statistical Significance Validation Gate
**Purpose:** A strategy must pass ALL checks below to leave `RESEARCH_PROPOSED` and enter `WALK_FORWARD_VALIDATED`.
**Scope:** applies to every strategy @researcher proposes in Phase 2. Blocks overfit/noise strategies before any "results" are trusted.

## The gate (all must hold)

| # | Check | Threshold | Why here |
|---|-------|-----------|----------|
| 1 | Newey-West t-stat on daily P&L | t ≥ 1.96 (two-tailed) | Accounts for daily-return autocorrelation; ~95% confidence vs zero. The 17-trade `bb_mr_20_2.0` (Sharpe 12.81) has |t| < 1 → rejected. |
| 2 | Block-bootstrap p-value (21-day blocks) | p < 0.05 (two-sided) | HAC + non-normality robustness. Rejects if result is non-robust to resampling. |
| 3 | Timing-placebo p-value (signals time-shifted by 10–60 days) | p < 0.05 | Strategy must beat shuffled timing. In sandbox, the lone "winner" was p = 0.12 → rejected (luck). |
| 4 | OOS Sharpe (annualized, post-cost) | Sharpe ≥ 0.5 | Break-even floor for Indian equities after ~15 bps round-trip + slippage. Below this the signal is eaten by frictions. |
| 5 | Holding-period sanity | median holding ∈ [1, 10] trading days | Filters noise-trading (sub-day) and position-concentration (multi-week) artifacts. |
| 6 | OOS/IS integrity gap | OOS Sharpe ≥ 0.8 × IS Sharpe (no >20% inflation) | Catches overfitting where IS looks great and OOS collapses. |

## Implementation wiring (extends `evidence.py`)
`classify_quality` currently uses ONLY trade-count + OOS + costs. Add a `significance` sub-object to `EvidenceRun` and require it for `adequate`:
```
significance = {
    "nw_tstat": float,            # ≥ 1.96
    "bootstrap_p": float,          # < 0.05
    "placebo_p": float,            # < 0.05
    "oos_sharpe": float,           # ≥ 0.5
    "median_hold_days": float,    # ∈ [1,10]
    "is_oos_sharpe_ratio": float, # ≥ 0.8
    "passes": bool,               # ALL above true
}
```
A strategy with `trade_count ≥ 30` but `significance.passes == False` → stays `marginal` and CANNOT be promoted. This closes the gap found in the evidence-store audit (§2.5 of RC_PHASE1_STATUS.md): today the gate certifies a 125-trade loser as "adequate" because it lacks any significance check.

## Defaults (Indian daily equities context)
- Newey-West lag = `min(6, T/4)` (≈ 3 trading weeks of dependence).
- Bootstrap = stationary bootstrap, 9999 reps, 21-day blocks.
- Placebo = shift entry signals by a uniform random offset in [10, 60] trading days, recompute; p = P(placebo Sharpe ≥ live Sharpe).
- Sharpe floor 0.5; IS/OOS ratio floor 0.8 (penalizes >20% IS inflation).
- Costs: 15 bps round-trip (brokerage+slippage+taxes) — do NOT assume zero; otherwise `classify_quality` returns `insufficient`.

## Decisions these numbers force
- A Sharpe-12.81 / 17-trade strategy → **REJECT** (checks 1, 2, 6).
- A 125-trade strategy losing 21% → **REJECT** (check 4).
- A 40-trade winner with OOS Sharpe 0.8 and |t|=3.2 → **ACCEPT** (all pass).
- No significance computed → **REJECT by default** (cannot trust trade-count alone).
