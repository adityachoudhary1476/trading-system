# Research Stack Investigation — Findings for @quant

## Scope
Investigated `v5_validation.py`'s `bootstrap_ci()`, the Phase 23 tournament significance pipeline,
and the evidence store schema migration mechanism. Cross-referenced with
`docs/rc_significance_gate.md` (@reality-checker) and `docs/rc_bias_checklist.md` (@reality-checker).

---

## Q1: Can `bootstrap_ci()` be extended to a proper placebo/timing-randomization test?

### STATUS: YES — but as a NEW function, not by modifying `bootstrap_ci()`

`bootstrap_ci()` (v5_validation.py:138-151) is a **correct percentile bootstrap CI** for the mean:
```python
def bootstrap_ci(values, seed=7, n_boot=1000, alpha=0.05):
    rng = np.random.default_rng(seed)
    arr = np.asarray(values, dtype=float)
    means = np.empty(n_boot)
    for i in range(n_boot):
        means[i] = rng.choice(arr, size=len(arr), replace=True).mean()
    q = alpha / 2 * 100
    return (float(np.percentile(means, q)), float(np.percentile(means, 100 - q)), float(means.mean()))
```
- ✅ Seeded RNG (deterministic, reproducible)
- ✅ Resamples WITH replacement (correct for CI estimation)
- ✅ Returns `(lo, hi, mean)` via percentile method

**Why it can't directly do placebo testing**: A placebo/timing-randomization test requires
**permutation testing** — shuffling WITHOUT replacement (or shifting time indices). Bootstrap
(with replacement) and permutation (shuffling) are fundamentally different:
- Bootstrap: estimates the sampling distribution of a statistic under the EMPIRICAL distribution
- Permutation: tests the null hypothesis of NO association by reshuffling labels

### Extension path (proven by `improvement_test()` in the same file, lines 178-234)

`improvement_test()` already demonstrates the infrastructure pattern:
```python
rng = np.random.default_rng(seed + 1)
for _ in range(500):
    sa = rng.choice(a, size=len(a), replace=True).mean()
    sb = rng.choice(b, size=len(b), replace=True).mean()
    diffs.append(sb - sa)
res.ci_low = float(np.percentile(diffs, 2.5))
res.ci_high = float(np.percentile(diffs, 97.5))
```
Same seeded-RNG + numpy + percentile-extraction pattern.

### Recommended new functions for `v5_validation.py`

1. **`timing_placebo_pvalue(trade_returns, entry_indices, seed, n_perms=9999)`**
   - Shifts signal entry times by random offsets in [10, 60] trading days (per rc_significance_gate.md §Defaults)
   - Recomputes Sharpe for each shift
   - Returns p-value: P(placebo Sharpe ≥ live Sharpe)
   - Uses `rng.integers(10, 61, size=n)` for shift generation

2. **`block_bootstrap_pvalue(returns, seed, n_boot=9999, block_size=21)`**
   - Stationary block bootstrap with 21-day blocks (per rc_significance_gate.md §Defaults)
   - Resamples blocks (not individual returns) to preserve temporal dependence
   - Returns two-sided p-value
   - Uses `rng.choice()` on block-start indices with replacement

Both functions reuse the same seeded-RNG + numpy pattern proven by `bootstrap_ci()` and
`improvement_test()`. No new dependencies required.

---

## Q2: Can significance fields be added to `evidence_runs` schema as an additive migration?

### STATUS: YES — infrastructure is already built for this exact pattern

`EvidenceStore.ensure_schema_current()` (evidence.py:688-778) provides a **proven, idempotent,
additive-only migration system**:

- `CURRENT_SCHEMA_VERSION = 4` → can bump to 5
- Version-gated: `if current < 5 and inspector.has_table("evidence_runs"):`
- Idempotent column checks: `if col not in existing: ALTER TABLE ... ADD COLUMN ...`
- Dialect-aware: TIMESTAMPTZ for PostgreSQL, DATETIME for SQLite
- Already proven for v3 (paper_deployments options columns) and v4 (scheduler heartbeat columns)
- Unconditional idempotent column fixes (post-v4 hotfix for strategy_parameters_json)

### Required changes:

1. **Schema** (evidence.py): Add `CURRENT_SCHEMA_VERSION = 5`, add v5 migration block:
   ```python
   if current < 5 and inspector.has_table("evidence_runs"):
       existing = {c["name"] for c in inspector.get_columns("evidence_runs")}
       sig_cols = {
           "p_value": "ALTER TABLE evidence_runs ADD COLUMN p_value FLOAT",
           "placebo_q": "ALTER TABLE evidence_runs ADD COLUMN placebo_q FLOAT",
           "ci_lower": "ALTER TABLE evidence_runs ADD COLUMN ci_lower FLOAT",
           "ci_upper": "ALTER TABLE evidence_runs ADD COLUMN ci_upper FLOAT",
       }
       for col, ddl in sig_cols.items():
           if col not in existing:
               conn.execute(_text(ddl))
   ```

2. **EvidenceRecord** (evidence.py:259-281): Add nullable columns to the SQLAlchemy model
3. **EvidenceRun** (evidence.py:210-256): Add optional pydantic fields
4. **as_record()** (evidence.py:234): Pass through new fields
5. **classify_quality()** (evidence.py:431-448): Add significance sub-object per
   `docs/rc_significance_gate.md` §Implementation wiring

---

## 🚨 CRITICAL CORRECTION: `bootstrap_prob_positive` is NOT a bootstrap probability

The ROADMAP (line 32) describes the gap as:
> "min_bootstrap_prob_positive = 0.45 (45% chance ≠ 95% confidence)"

**Reality is worse than described.** The field named `bootstrap_prob_positive` is NOT a
probability at all — it is the **raw win rate**:

```python
# tournament.py:414-418
prob_positive = sum(1 for r in trade_returns if r > 0) / len(trade_returns)
result.bootstrap = {
    "prob_positive": prob_positive,   # ← just (winning trades / total trades)
    "ci_low": lo, "ci_high": hi, "mean": mean,  # ← real CI from bootstrap_ci()
}
```

**Line 460**: `bootstrap_prob_positive=result.bootstrap.get("prob_positive", 0.0)`

The `bootstrap_ci()` IS called (tournament.py:410, n_boot=1000, alpha=0.05) and produces
a **proper 95% CI** `(lo, hi)`. But:
1. **The CI is NEVER passed to ScorerInput** — the ScorerInput constructor (tournament.py:437-464)
   does NOT include `bootstrap_return_ci` or `bootstrap_sharpe_ci`
2. **Only the win rate** (`prob_positive`) reaches the `RejectionGate.evaluate()` (scoring.py:298)
3. **The 0.45 threshold** is a 45% win-rate bar — not a 95% confidence threshold

### Additional hardcoded fields that disable checks:

| Field | Value | Effect |
|-------|-------|--------|
| `cagr` | `0.0` | Not computed |
| `volatility` | `0.0` | Not computed |
| `wf_oos_sharpe` | `None` | Not computed |
| `wf_degradation` | `None` | Not computed |
| `parameter_stability` | `1.0` | Always passes (max stability) |
| `bootstrap_return_ci` | not passed | CI from `bootstrap_ci()` is ignored |
| `bootstrap_sharpe_ci` | not passed | CI from `bootstrap_ci()` is ignored |

### Gate redundancy

| Check | Field | Threshold | Actual meaning |
|-------|-------|-----------|----------------|
| #4 (scoring.py:287) | `win_rate` | 0.30 | Win rate ≥ 30% |
| #8 (scoring.py:298) | `bootstrap_prob_positive` | 0.45 | Win rate ≥ 45% (SAME VALUE!) |

Two checks for the same quantity (win rate) with different thresholds. The 0.45 bar is the
effective one (stricter), but neither is a statistical significance test.

---

## Files referenced

| File | Lines | Key content |
|------|-------|-------------|
| `research/v5_validation.py` | 138-151 | `bootstrap_ci()` definition |
| `research/v5_validation.py` | 156-234 | `ImprovementResult`, `improvement_test()` (pattern reference) |
| `research/phase23/tournament.py` | 33 | `from ..v5_validation import bootstrap_ci` |
| `research/phase23/tournament.py` | 404-424 | Bootstrap section: calls `bootstrap_ci()` but computes `prob_positive` as win rate |
| `research/phase23/tournament.py` | 437-464 | ScorerInput construction (missing bootstrap CI fields) |
| `research/phase23/config.py` | 59 | `min_bootstrap_prob_positive: float = 0.45` |
| `research/phase23/config.py` | 32 | `min_win_rate: float = 0.30` |
| `research/phase23/config.py` | 74 | `bootstrap_stability: float = 0.10` (10% weight) |
| `research/phase23/scoring.py` | 62-104 | `ScorerInput` dataclass |
| `research/phase23/scoring.py` | 271-314 | `RejectionGate.evaluate()` |
| `research/evidence.py` | 210-256 | `EvidenceRun` pydantic model (20 fields, no significance) |
| `research/evidence.py` | 259-281 | `EvidenceRecord` SQLAlchemy model |
| `research/evidence.py` | 431-448 | `classify_quality()` (trade-count + OOS + costs only) |
| `research/evidence.py` | 688-778 | `ensure_schema_current()` (additive migration infrastructure) |
| `docs/rc_significance_gate.md` | 1-42 | @reality-checker's 6-check gate spec with wiring |
| `docs/rc_bias_checklist.md` | 1-97 | @reality-checker's bias validation checklist (D1-D5 significance) |
| `frontend/src/lib/paperApi.ts` | 286-303 | Frontend calls `/strategies`, `/regime`, `/allocation` |

---

## Summary of recommendations

| Priority | Action | Owner |
|----------|--------|-------|
| **P0** | Add `timing_placebo_pvalue()` + `block_bootstrap_pvalue()` to `v5_validation.py` | @developer |
| **P0** | Add v5 additive migration for `p_value`, `placebo_q`, `ci_lower`, `ci_upper` on `evidence_runs` | @developer |
| **P0** | Fix tournament bootstrap: pass CI to ScorerInput, rename `prob_positive`→`win_rate`, add `placebo_p` to gate | @developer |
| **P0** | Wire `classify_quality()` with significance sub-object per `rc_significance_gate.md` | @developer |
| **P0** | Replace `min_bootstrap_prob_positive=0.45` gate with proper `placebo_p < 0.05` check | @quant (decide threshold) |
| **P1** | Fix `_trend_strength` idx slice in factors.py:199 | @developer |
| **P1** | Delete root-level prototype scripts (not git-tracked) | @developer |
| **P2** | Refresh FYERS/Upstox token; backfill ≥15 NSE stocks @1d+5m | @developer |
| **P2** | Fix `/regime` + `/allocation` dev mode (400 error) | @developer |
| **P3** | Promote `docs/rc_bias_checklist.md` into CI gate | @experimenter |
