# Day 10 Report — Research Infrastructure & Evidence Engine

**Date:** 2026-08-29
**Scope:** RESEARCH ONLY. Built the research spine the future autonomous agent (Hermes)
will depend on: India factor engine, factor analysis (IC/IR, grouped backtest, breakeven),
warmup-aware backtesting, experiment manifest hashing, hypothesis + evidence store, quality
and freshness classification. **No execution, no broker, no orders, no LLM, no Vibe-Trading.**

## Files changed
**New:** `src/trading_system/research/factors.py`, `src/trading_system/research/factor_analysis.py`,
`src/trading_system/research/evidence.py`, `tests/test_factors.py`, `tests/test_factor_analysis.py`,
`tests/test_evidence.py`, `tests/test_warmup.py`, `DAY10_REPORT.md`.
**Modified:** `src/trading_system/research/backtester.py` (warmup/eval window),
`src/trading_system/research/__init__.py` (exports), `src/trading_system/__main__.py` (`research` CLI),
`README.md`, `ARCHITECTURE.md`.
**Reused (no duplication):** `indicators` (sma/ema/rsi/atr/rolling_std), `analysis.quant`
(annualized_volatility, TRADING_PERIODS), `storage.database.Base`/`Engine` (evidence tables on the same
SQLite DB — no second framework), `dataset.HistoricalDataset`, `RiskConfig`, `run_backtest`.

## Architecture
```
MarketStore ──► FactorEngine ──► factors (causal)
                  │
                  └─► factor_analysis (IC/IR, grouped backtest, breakeven)
MarketStore ──► backtester (warmup/eval window) ──► BacktestResult
                  │
                  └─► ExperimentManifest (deterministic hash)
EvidenceStore (SQLite, same engine) ──► Hypothesis / EvidenceRun / ResearchRegistry
```

## Factor engine (`factors.py`)
`Factor` (name/metadata/compute) + `FactorEngine`. 17 factors across 5 categories, every one
documented (name, definition, required data, lookback, output, causal behavior, limitations):
* Trend: `sma_distance_20`, `ema_distance_20`, `ma_spread`, `trend_strength`
* Momentum: `rsi_14`, `roc_20`, `momentum_60_20`, `multi_mom`
* Volatility: `atr_14`, `realized_vol_20`, `vol_pct_rank`, `vol_expansion`
* Volume: `relative_volume_20`, `volume_momentum`
* Price structure: `dist_from_high_20`, `dist_from_low_20`, `range_position`

Causal: each factor uses only data ≤ T. Insufficient history → NaN (never fabricated). Small,
economically interpretable set — not a sprawling library.

## Factor analysis (`factor_analysis.py`)
* `forward_return(prices, lag)` — price[T+lag]/price[T]-1 (never factor_T vs return_T).
* `compute_ic_series(factor, fwd_ret, lag)` — per-date cross-sectional Spearman IC; requires
  ≥5 instruments (MIN_CROSS_SECTION) else NaN. Report-ready, no fabrication.
* `ic_statistics` — mean/median/std IC, ICIR (=mean/std, NaN on zero variance, never inf),
  positive-IC fraction, n_obs.
* `grouped_backtest` — rank→N groups (default 10) equal-weight fwd returns; Q1..Q10 curves,
  long-short, monotonicity flag; handles duplicate values + insufficient instruments safely.
* `breakeven_fee_bps(alpha_daily, n_trades, position_size)` — generic cost breakeven. Units explicit
  (BPS). Invariant VERIFIED: half exposure → ~2× per-unit fee. No hidden conversions.

## Warmup / backtest changes
`BacktestConfig.warmup_bars` + `evaluation_start_date`. The full simulation still primes indicators
on warmup bars (correct), but REPORTED performance covers only the evaluation window: trades exiting
before eval start are excluded and equity is measured from eval start. Regression test confirms
warmup does NOT inflate/improve metrics. Also fixed a pre-existing ledger bug (final mark-out now
reconciles to `final_capital`) — caught by Day 7 test `test_trade_ledger_reproducible`.

## Manifest hashing (`evidence.py`)
`ExperimentManifest.identity_hash` = SHA-256 of sorted, deterministic config (factors, dataset,
universe, timeframe, warmup, costs, code_version). Same config → same hash; any meaningful change
(warmup, cost, factor set) → different hash. `run_metadata` excluded from identity.

## Hypothesis model
`Hypothesis` (pydantic, extra="forbid") + `HypothesisRecord` (ORM). Status enum: HYPOTHESIS →
RESEARCH → BACKTEST → HOLDOUT → PAPER → CANARY → LIVE → REVIEW → RETIRED/REJECTED. Status is
**research state, not execution permission** — no auto-promotion.

## Evidence Store
`EvidenceStore` + `EvidenceRun` (pydantic) + `EvidenceRecord` (ORM) on the SAME SQLAlchemy engine as
MarketStore (no second DB framework). Supports create/get/update hypothesis, record/retrieve evidence,
list by hypothesis/regime/quality, `get_latest_evidence`, `compare_hypotheses`. Parameterized SQL,
no network, no creds, no AI.

## Evidence quality / freshness
`classify_quality` → INSUFFICIENT / MARGINAL / ADEQUATE using configurable, documented (provisional)
thresholds (MIN_TRADES_ADEQUATE=30, MIN_TRADES_MARGINAL=10, STALE_DAYS=180). `is_evidence_stale`
marks old evidence STALE (requires revalidation) but never deletes or auto-retires.

## Regime attribution
Evidence carries a `regime` field (trending_up/down, range_bound, high/low_volatility, unknown) and
the store filters by it — regime-specific performance is queryable (no assumption that one regime
generalizes).

## CLI
```
python -m trading_system research factors --symbol NSE:SBIN --timeframe 1d [--names ...]
python -m trading_system research factor-analysis --symbol NSE:SBIN --factor rsi_14 --lag 1
python -m trading_system research hypothesis list [--status ...]
python -m trading_system research evidence list [--hypothesis ...] [--regime ...] [--quality ...]
```
Read-only research; no order commands.

## Tests
**43 new tests**, full suite **276 passed, 0 failed** (233 Day1-9 baseline + 43 Day10).
* Factor: deterministic, expected values, insufficient data, NaN, unsorted, dup timestamp, tz-aware,
  metadata; **mandatory look-ahead** (future-edit leaves factor_T unchanged).
* Factor analysis: IC alignment/lag, min cross-section, zero-variance ICIR, known Spearman, grouped
  backtest, duplicate values, insufficient instruments, breakeven invariants, **research-integrity**
  (future return change after T leaves earlier IC unchanged).
* Warmup/backtest: warmup no-contribution, eval-start date, determinism, empty dataset, ordering,
  full-capital equality, no warmup inflation.
* Evidence: create/get/update hypothesis, record/retrieve, manifest determinism + config-change,
  filtering by regime/quality, quality classification, stale (no delete/retire), malformed rejected,
  compare.

## Real SBI demonstration (honest)
`NSE:SBIN 1d` = **2477 real bars**. Factor engine produced 17 factors (last: dist_from_high_20=-0.073,
rsi_14=47.3). Manifest hashing works. Evidence demo recorded (quality=insufficient, correctly flagged
— single symbol, no OOS). `factor-analysis` reported time-series Spearman IC = -0.001 (essentially no
predictability) and **explicitly stated cross-sectional IC (≥5 instruments) is NOT possible from one
symbol** — no synthetic multi-name universe was fabricated.

## Quant limitations
* India cost model (STT/exchange/SEBI/stamp/brokerage/GST, lot sizes, rollover) NOT yet implemented —
  `breakeven_fee_bps` is generic; Day 11/15 will add the India-specific schedule.
* Only one instrument stored (SBIN) → cross-sectional factor research blocked until more history is
  backfilled (needs refreshed FYERS token).
* Factor set is small/interpretable by design; no optimization or parameter search was run (Day 10
  explicitly forbids strategy optimization).
* SBI result is NOT a profitability claim.

## Security/safety verification
* No broker/order/execution code added or modified.
* No `place_order`/`submit_order`/`broker` symbols exist in the repo (grep-verified).
* No LLM/OpenAI/MCP/Vibe-Trading dependency introduced.
* No `.env` modified; creds remain isolated.
* Evidence store reuses the existing read-only MarketStore engine; no new secrets.

## Git status
Uncommitted (per instruction). Only docs + `DAY10_REPORT.md` + research modules + tests added/modified.
No production execution surface.

## Exact recommended Day 11
**Strategy discovery (still research-only).** Objectives:
1. `research/strategies.py` already has baselines — add a `StrategySpec` (deterministic, serializable)
   and a `DiscoveryEngine` that turns a `Hypothesis` into candidate `Strategy` instances WITHOUT
   optimization (grid of documented, bounded hyperparameter sets only; no best-of selection yet).
2. Wire `run_backtest` (now warmup-aware) + `compute_performance` + `ExperimentManifest` into a
   `analyze_strategy(hypothesis, strategy, dataset)` that records an `EvidenceRun` (regime, OOS flag,
   cost assumption) — no human/live promotion.
3. Add `Split`/walk-forward already exists (`walkforward.py`) — extend evidence to mark OOS vs in-sample.
4. Tests: discovery determinism, strategy↔spec round-trip, OOS separation, no-optimization guard.
5. Safety boundary: no execution, no broker, no LLM code-gen yet (LLM strategy generation deferred to
   Day 12 sandbox per Day 9 blueprint). Reuse `ResearchRegistry` for all writes.
