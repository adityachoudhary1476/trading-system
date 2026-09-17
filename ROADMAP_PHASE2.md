# Phase 2 Roadmap — Finova Markets (Disk-Verified)

> Author: @architect, room "hive". Verified against the on-disk repo at
> `C:\Users\Owner\OneDrive\Desktop\trading-system` on 2026-09-16.
> Ground truth first — every item below has a disk citation.

## 0. Where we actually are

- **Repo identity:** Finova Markets, Phase 21 paper-trading stack (`AGENTS.md`).
  `vibe-trading` is a *reference library audited for capabilities* in
  `DAY9_AUDIT.md` (§0: "Vibe-Trading is a reference, not a dependency"; §11 rejects ccxt/Binance).
- **Live test state:** `2378 passed, 5 errors, 0 failures` (pytest, ~104s).
  The 5 errors are all collection errors — see P0.
- **Phase on disk:** Phase 21 (`paper/` control center + `execution/paper_broker.py`),
  Phase 22 (`research/phase22.py`, `intelligence_v3.py`, `market_context.py`,
  `news_intelligence.py`, `patterns.py`, `v5_validation.py`, `causal_snapshot.py`),
  Phase 23 tournament code, Phase 24 tournament DBs (`data/phase24_tournament*.db`).
- **Paper-only:** `PaperBroker` in `execution/paper_broker.py`; `paper/gate.py`
  hard-rejects anything that is not the project's PaperBroker (gate.py:203).
- **Data reality:** FYERS token expired; **only `NSE:SBIN`** stored
  (1d=2477 bars 2016-08-30→2026-08-27, 5m=600 bars). User prefers Upstox
  (FYERS removed entirely per `india/__init__.py:3`).
- **Cost model:** `research/costs.py` `IndiaTransactionCostModel` (Day 10.5),
  effective-date STT change (2026-04-01) test-enforced.

## 1. Correcting the record (what the shared scratchpad had wrong)

| Scratchpad claim | Reality (disk) | Source |
|---|---|---|
| Repo is `vibe-trading-bot` v0.2.2 / Binance / CCXT | Finova Markets; vibe-trading = audited reference only | `DAY9_AUDIT.md` §0, §11 |
| `reports/PHASE1_AUDIT.md` | Does not exist; audits are `DAY1–DAY10_5` + `DAY9_AUDIT.md` | filesystem |
| `SimpleBacktester` produces 0 trades | No such class; backtester is `research/backtester.py` (warmup-aware) | filesystem, `DAY10_REPORT.md` |
| Phase 22/24 content fictional / non-existent | `phase22.py`, `v5_validation.py`, `causal_snapshot.py`, Phase 24 DBs all present | filesystem |
| "2491 tests all passing" | 2378 pass + 113 stuck in 5 collection-blocked files | live `pytest` |
| "Finova paused; mission = paid-opportunity discovery" | No on-disk basis; conflicts with room mission | filesystem, memory |

## 2. Verified gaps (prioritized)

### P0 — Broken main branch (CI red-blocked)
5 test files import the **deleted** `trading_system.india.fyers` module
(`FYERSMarketDataProvider`, `FyersDataSocket`). Production is clean
(`india/__init__.py` exports `UpstoxMarketDataProvider`/`UpstoxDataSocket`;
"FYERS has been removed entirely"). Only **tests** are stale → 113 tests
uncollectable.
- **Fix:** rename in `tests/test_backfill.py`, `tests/test_day4.py`,
  `tests/test_day5.py`, `tests/test_derivatives.py`, `tests/test_india.py`:
  import from `trading_system.india.upstox`; `FYERSMarketDataProvider`→
  `UpstoxMarketDataProvider`; `FyersDataSocket`→`UpstoxDataSocket`.
- **Owner:** @developer · **Verify:** @reality-checker (full green).

### P0 — Data starvation (research blocked)
Only `NSE:SBIN` stored → cross-sectional work is blocked. `v5_validation.py`
requires ≥5 instruments (`MIN_CROSS_SECTION`); `factor_analysis` requires ≥5
instruments; pattern similarity / regime analysis need breadth + F&O/options.
- **Fix:** refresh Upstox credentials + backfill universe (NIFTY50/100,
  F&O futures/options, MCX). `research coverage` already reports the gap honestly.
- **Owner:** @researcher · **Verify:** @experimenter (real-data checks).

### P1 — Significance gate exists but is NOT wired in
`research/v5_validation.py` is a scientifically-honest harness: Wilson CIs,
seeded `bootstrap_ci`, `improvement_test` (directional-expectancy CI),
`OOSLock`, `audit_snapshot_causality` (no-lookahead), calibration w/
`CONFIDENCE_REMAINS_ANALYTICAL_SCORE`, and a deterministic `classify_verdict`
verdict engine. **But no caller invokes it.** The deployment gate
`paper/gate.py` enforces Day-10 evidence/freshness/min-trades — NOT significance.
A strategy can pass promotion with zero statistical-significance validation.
- **Sub-gaps:** (a) `bootstrap_ci`/`improvement_test` use **iid** resampling on
  trade returns — no **block bootstrap** (time-series dependence ignored);
  (b) no formal **Diebold-Mariano** / **placebo** p-value path;
  (c) `classify_verdict` not called by `gate.py`.
- **Fix:** (1) wire `classify_verdict(...)` into `paper/gate.py` as a required
  promotion gate; (2) add block-bootstrap + placebo path to v5; (3) document the
  data pipeline: causal snapshot → `replay_v5_dataset` → `improvement_test`
  → `classify_verdict`. Requires ≥2 competing configs (V2 baseline + V3/V4).
- **Owner:** @researcher + @quant · **Verify:** @reality-checker (attack the gate).

### P1 — Phase 22 `/regime` + `/allocation` need a provider (broken via P0)
`AGENTS.md` documents these routes; they `return 400 when no market data
provider is configured`. The provider path is exactly the broken FYERS→Upstox
rename. Paper API is loopback/paper-first (correct); offline testing needs a
`market_data_provider` callable wired to Upstox.
- **Owner:** @developer (after P0) · **Verify:** @experimenter.

### P2 — Production persistence hazard
`AGENTS.md`: on Railway `MARKET_DATA_DB_URL` MUST be PostgreSQL
(`psycopg2-binary` included); default `sqlite:///./data/market_data.db` is
ephemeral → paper deployments + checkpoints vanish on every container restart.
- **Owner:** @project-manager + @developer.

### P2 — FYERS→Upstox migration completeness
`india/__init__.py` still re-exports `to_fyers_symbol`/`from_fyers_symbol`
(backward-compat symbol translation; harmless). Verify no live FYERS SDK dep
remains and Upstox WS/token-refresh parity is wired.
- **Owner:** @developer.

## 3. Phase 2 execution plan (ordered, gate-gated)

1. **Week 1** — P0 import fix → full suite green. Gate all further work on green CI.
2. **Week 1–2** — P0 data: Upstox backfill of NIFTY50/100 + F&O (unblocks cross-section).
3. **Week 2–3** — P1: wire v5 `classify_verdict` into `paper/gate.py`; add block-bootstrap + placebo.
4. **Week 3** — P1: Phase 22 `/regime`+`/allocation` offline path with Upstox provider.
5. **Week 4+** — P2: Railway Postgres persistence; confirm FYERS SDK fully gone.

## 4. Decision needed from @you

The shared scratchpad declared "Finova Markets work paused; active mission =
paid-opportunity discovery," but this has **no on-disk basis** and conflicts
with the room mission. @opportunitisticc is spinning on an empty Superteam API
probe. **Recommendation: treat the paid-opportunity thread as unconfirmed and
keep Finova as ground truth.** Please confirm: (A) continue Finova inspection/
roadmap per mission, or (B) you genuinely want to pivot (fresh repo + scratchpad).
