# V5 — Real Historical Data & Predictive Validation

## Executive Summary

V5 is the FIRST serious real-historical validation phase. It is NOT another
indicator-building phase. Its purpose is to determine whether Finova's
intelligence demonstrates measurable predictive value on genuinely historical
market data — and to say so honestly when it does not.

**Current status: infrastructure complete, predictive validation PENDING.**

`REAL_HISTORICAL_DATA_NOT_PRESENT` — no real historical dataset ships with this
repository (no paid API, no broker connectivity, no third-party archive). All
V5 components are implemented, tested, and exercised by a clearly labeled
`SYNTHETIC_TEST` smoke run. The honest headline is therefore:

> `REAL_HISTORICAL_VALIDATION_PENDING`

No claim of predictive or trading edge is made anywhere. The demo verdict is
`INSUFFICIENT_DATA` because 17 directional trades on synthetic data is far
below the documented minimum of 30.

## Data Capabilities (audited)

- **OHLCV:** `HistoricalDataset` / `DataQuality` (Day-7) + new
  `LocalFileAdapter` (CSV / JSON / Parquet) in `historical_data.py`.
- **Provenance:** `DatasetType` (`REAL_HISTORICAL` / `SYNTHETIC_RESEARCH` /
  `SYNTHETIC_TEST` / `UNKNOWN`) + `HistoricalProvenance` with `sha256()`.
  A report may only call data "real historical" when provenance says
  `REAL_HISTORICAL` with a source and dataset_id.
- **Calendar:** existing `india/market_calendar.py` `TradingCalendar` (IST
  sessions, weekends, configurable holidays) is reused unchanged.
- **News/options/context:** V4 structures reused; causal filtering added.
- **Replay/outcomes/calibration:** V3 `ForecastStore`, V4 `compare_strategies`
  and `patterns` reused; V5 adds the causal snapshot layer and replay driver.

## Architecture

```
Local files (CSV/JSON/Parquet, provider-agnostic)
  →  LocalFileAdapter (normalize timestamps, column mapping)
  →  validate_ohlcv        (duplicates, OHLC, volume, prices, gaps — never repair)
  →  HistoricalProvenance  (dataset type, source, hash — never fake it)
  →  CausalSnapshotBuilder (OHLCV ≤ T; CLOSED HTF candles; news ≤ T;
                            options ≤ T; context within staleness policy)
  →  replay_v5_dataset     (V2/V3/V4_technical/V4_news/V4_full per timestamp)
  →  FullMetrics           (direction/returns/trading/risk) + bootstrap CI
  →  improvement_test      (delta + CI + IMPROVEMENT/REGRESSION/…)
  →  confidence_calibration(buckets; CONFIDENCE_MONOTONICITY_FAILED)
  →  CostAssumptions + slippage_sensitivity
  →  no-lookahead audit    (CausalityAudit; input ≤ forecast < outcome)
  →  classify_verdict      (deterministic documented rules)
  →  ResearchRunRegistry   (append-only: config/dataset hash, git commit, seed)
  →  Report (docs/V5 …)
```

Business rules honored: no lookahead (enforced inside the API via `CausalSnapshot`
and audit), no fabricated data (only `SYNTHETIC_TEST` fixtures, labeled),
no tuning on the final OOS period (OOSLock), no claim that confidence is
probability, no claim that news/patterns help until measured.