# TRADING-SYSTEM — PROJECT AUDIT (hand-off for external analysis)

> Self-contained briefing. Read this top-to-bottom and you will understand the whole
> project: what it is, what is built (verified), what is deliberately NOT built, and
> exactly what remains. Written 2026-08-29 after Day 10.

---

## 0. ONE-LINE SUMMARY

A **provider-independent Indian-market quantitative trading research platform** being built
incrementally toward an eventually-autonomous loop (research → validate → paper → [future]
execution). Currently it is a **research/analysis engine with a real evidence store** — there
is **NO execution, NO broker order code, NO live trading**, by design and by hard safety rule.

---

## 1. LONG-TERM GOAL (the thing we are ultimately building)

An autonomous Indian-market quant system that can:

```
discover → hypothesize → research → backtest → validate → paper trade
→ monitor → promote/reject → (future) execute approved strategies via FYERS
→ detect decay → retire → research again
```

The AI agent ("Hermes") will eventually *operate the research loop* and *recommend* promotions,
but **must never directly control execution**. Deterministic research/risk/execution gates stay
authoritative. Execution is a future, isolated phase (Day 16–18 in the roadmap).

---

## 2. HARD SAFETY BOUNDARY (NON-NEGOTIABLE — DO NOT VIOLATE)

These rules have been asserted every day (Day 8/9/10) and are the project's spine:

- NO order placement, NO position modification, NO broker order endpoints.
- NO live execution, NO auto-trading, NO exposure of broker order APIs to any frontend/agent.
- NO real money required; output is for AI-analyst / paper-trading / backtesting only.
- AI "cannot trade": it may inspect, hypothesize, request backtests, compare, propose retirement
  — but NOT call FYERS order endpoints, bypass risk, change limits, approve its own live strategy,
  see credentials, or build authenticated broker requests.
- NO fabricated data, NO fabricated Greeks/OI/news, NO fabricated indicators.
- NO look-ahead bias anywhere (enforced by construction + tests).
- Deterministic calculations must be testable without an LLM.
- AI reasons over structured evidence, not invented market facts.
- Every signal candidate must carry uncertainty/risk information.
- `.env` is gitignored; FYERS credentials (`FYERS_CLIENT_ID`, `FYERS_ACCESS_TOKEN`) are NEVER
  printed, stored in source, or exposed.

If you are asked to "just add execution" or "wire it to FYERS live" — STOP. That is a future
phase with its own isolated architecture (see §9). Do not silently turn analysis into execution.

---

## 3. WHAT IS VERIFIED BUILT (Day 1 → Day 10)

Test status: **276 tests passing, 0 failing** (baseline 169 Day1-6 + 32 Day7 + 32 Day8 + 43 Day10).
No execution code exists anywhere (grep-verified).

### Data / ingestion
- FYERS authentication + historical market data; real FYERS WebSocket integration; live market
  pipeline; candle aggregation; idempotent bulk backfill engine (`india/backfill.py`).
- `DataHealthMonitor` (flags stale/disconnected/auth-error/invalid feeds → gates analysis).
- `MarketStore` — SQLAlchemy SQLite store, idempotent upsert, contract_id guards.

### Derivatives / commodities (Day 6) — schema + discovery only
- F&O / options / futures instrument model; FYERS derivative symbol resolver; live option-chain
  discovery; MCX commodity support. `contract_id` prevents expiry/strike collisions.
- **BUT:** no derivative/commodity history is currently stored (FYERS token expired).

### Intelligence / analysis (Day 8) — DATA/ANALYSIS ONLY
`research/intelligence.py`:
- `FeatureEngine` (causal SMA/EMA/RSI/ATR/vol/volume/price-structure), `MarketRegime` +
  `classify_regime`, `SignalCandidate` (analytical LONG/SHORT/NEUTRAL hypothesis, confidence,
  risks, `not_an_order: True`), `AnalysisExplanation`, `AIAnalysis` (strict pydantic, rejects
  malformed), `MarketReasoningProvider` (wraps `ModelProvider`), `instrument_class_of`.
- CLI `analyze-history` (real demo works on SBIN; derivative symbols correctly BLOCKED/NO_DATA).

### Research spine (Day 7 + Day 10) — THE CORE OF WHAT WAS JUST BUILT
- Day 7: `backtester.py` (deterministic next-bar-open fill, no look-ahead), `strategies.py`
  (EMATrend/Momentum/Breakout baselines), `risk.py` (`RiskConfig`, leverage 1.0, no short),
  `dataset.py`, `performance.py`, `walkforward.py` (train/test split).
- Day 10 (this session): `factors.py`, `factor_analysis.py`, `evidence.py`, warmup-aware
  backtester, `research` CLI. See §4.

### AI abstractions (reused, NOT duplicated)
- `MarketSnapshot`, `MarketView`, `ModelProvider` (`LocalRuleModel` / `OpenAICompatibleProvider`),
  `analyst.analyze_snapshot`, `signals.generate_signal`. AI vendor choice stays isolated.

### Frontend
- React + TypeScript + Vite app (`frontend/`, dev server historically on port 4180). Reads market
  data / intelligence; NO order surface.

---

## 4. DAY 10 DELIVERABLES IN DETAIL (most recent work)

New modules (all RESEARCH ONLY, no execution):

**`research/factors.py`** — `Factor` + `FactorEngine`. 17 documented causal factors across
trend / momentum / volatility / volume / price-structure. Each uses only data ≤ T; insufficient
history → `NaN`. Small, interpretable set by design (not a sprawling library).

**`research/factor_analysis.py`**
- `forward_return(prices, lag)` — price[T+lag]/price[T]-1 (never factor_T vs return_T).
- `compute_ic_series(factor, fwd_ret, lag)` — per-date cross-sectional Spearman IC; requires ≥5
  instruments (MIN_CROSS_SECTION) else `NaN` (no fabrication).
- `ic_statistics` — mean/median/std IC, ICIR (=mean/std, `NaN` on zero variance, never `inf`),
  positive-IC fraction, n_obs.
- `grouped_backtest` — rank → N groups (default 10), equal-weight forward returns, Q1..Q10 curves,
  long-short, monotonicity flag. Handles duplicate values + insufficient instruments.
- `breakeven_fee_bps(alpha_daily, n_trades, position_size)` — generic cost breakeven, units
  explicit (BPS). Invariant: half exposure → ~2× per-unit fee (test-verified).

**`research/backtester.py` (enhanced)** — `BacktestConfig.warmup_bars` + `evaluation_start_date`.
Full sim primes indicators on warmup bars; **reported performance covers only the evaluation
window** (trades exiting before eval start excluded; equity measured from eval start). Also fixed
a pre-existing ledger bug (final mark-out now reconciles to `final_capital`).

**`research/evidence.py`** — the evidence/registry layer:
- `ExperimentManifest` — deterministic SHA-256 `identity_hash` (same config → same hash; any
  meaningful change → different hash; run metadata excluded from identity).
- `Hypothesis` (pydantic, extra="forbid") + 9-status lifecycle enum. **Status = research state,
  NOT execution permission.**
- `EvidenceRun` (pydantic) — metrics actually present; nothing fabricated.
- `EvidenceStore` — SQLAlchemy tables (`hypotheses`, `evidence_runs`) on the **SAME engine as
  MarketStore** (no second DB framework). Parameterized SQL, no network, no creds, no AI.
- `ResearchRegistry` — clean facade (create/get/list hypothesis, record/retrieve evidence,
  get_latest, compare, is_evidence_stale). Future Hermes tools call THIS, not SQLite.
- `classify_quality` → INSUFFICIENT / MARGINAL / ADEQUATE (provisional, documented thresholds:
  trades≥30 adequate, ≥10 marginal, cost assumption required). `is_evidence_stale` (180d) flags
  STALE but never deletes/auto-retires.

**CLI** — `research factors | factor-analysis | hypothesis list | evidence list` (read-only).

**Tests added** — 43 new (factor determinism, mandatory look-ahead, IC alignment/min-cross-section/
zero-variance, grouped backtest, breakeven invariants, research-integrity regression, warmup
no-contribution, evidence round-trip/manifest/filtering/stale/malformed-rejected). Full suite 276
green.

---

## 5. CURRENT REAL DATA STATE (honest)

- `NSE:SBIN` 1d = **2477 bars** (2016-08-30 → 2026-08-27). Used for live demos.
- `NSE:SBIN` 5m = **600 bars**.
- **NO F&O / commodity / option-chain history stored** (FYERS access token expired → cannot
  backfill). Derivative analysis therefore BLOCKED (returns NO_DATA, not fabricated).
- Consequence: cross-sectional factor research (needs ≥5 instruments) is **impossible today** —
  this is stated explicitly in code/CLI output; no synthetic universe is invented.

---

## 6. ARCHITECTURE MAP (as-built)

```
MarketStore (SQLite, SQLAlchemy)
   │  load()  ──► HistoricalDataset (provider-independent)
   ├──► indicators / analysis.quant  ──► FeatureEngine (Day8) + FactorEngine (Day10)
   ├──► research/intelligence.py  ──► MarketRegime / SignalCandidate / AIAnalysis
   ├──► research/backtester.py  ──► BacktestResult (warmup-aware)
   ├──► research/factor_analysis.py  ──► IC / IR / grouped backtest / breakeven
   └──► research/evidence.py  ──► ExperimentManifest / Hypothesis / EvidenceRun
                                   └──► EvidenceStore (same engine) ──► ResearchRegistry
Models layer: MarketSnapshot → ModelProvider → MarketView / AIAnalysis (AI isolated)
India layer: FYERS adapter, instruments, symbol resolver, option-chain discovery, DataHealthMonitor
Frontend: React/TS (reads data + intelligence; NO order surface)
```

No module imports broker/order/execution code. Provider logic is isolated under `india/`;
AI-vendor logic under `models/`; research logic under `research/`; storage under `storage/`.

---

## 7. VIBE-TRADING REFERENCE (Day 9 audit — what we learned, what we rejected)

Studied `HKUDS/Vibe-Trading` as an architectural reference (US/A-share/HK centric, 12 brokers,
**zero India**). NOT a dependency; NOT cloned; NOT installed.

Adopt (concepts): evidence store, hypothesis registry, durable order-intent + client_order_id
recovery, reconciliation, warmup/eval split, MCP tool boundary (research), manifest hashing,
shadow/paper pattern, TAP-style credential isolation, permission tiers.

Adapt (build on our arch): India factor library, IC/IR/decile backtest, **Indian cost model**
(STT/exchange/SEBI/stamp/brokerage/DP/GST, lot sizes, expiry/rollover), strategy sandbox,
portfolio/risk, `FyersBroker` behind `ExecutionInterface`, execution gateway.

Reject: Alpaca/US connectors, ccxt/vnpy/MT5/Binance/OKX/Futu/Longbridge, A-share Tushare, gold
FX-pip conventions, swarm-for-complexity, any token/coin surface (VT's own SECURITY.md warns of
impersonation), unsandboxed LLM code-gen.

Real bugs THEY fixed (lessons for us): gold spread 1460× too cheap (cost convention), HKD valued as
USD (valuation bug), Shadow PnL=0 (key mismatch), under-reported tokens. → We must unit-test the
India cost schedule and contract-test PaperBroker vs live schema.

---

## 8. TARGET AUTONOMOUS ARCHITECTURE (from Day 9 blueprint — refine as you learn)

```
MARKET DATA → DATA NORMALIZATION → MARKETSTORE
   ┌─────────────────────────────┴─────────────────────────────┐
   ▼                                                            ▼
MARKET INTELLIGENCE                                      RESEARCH ENGINE
   │                                                   (Factors/Backtest/Regimes)
   │                                                            │
   └───────────────────────┬────────────────────────────────────┘
                           ▼
                     EVIDENCE STORE  (Hypothesis → EvidenceRun, quality, freshness, regime)
                           │
                           ▼
                     HERMES AGENT  (tools only; NO credentials; inspect/research/hypothesize/
                                    request backtests/compare/propose retirement — NOT execute)
                           │
                           ▼
                     STRATEGY SANDBOX  (static validation → sandboxed run → backtest → OOS →
                                    walk-forward → cost/slippage stress → regime analysis → Evidence)
                           │
                           ▼
                     PAPER TRADING  (PaperBroker == same Risk/Strategy interfaces as live)
                           │
                           ▼
                     RISK ENGINE  (policy gate — deterministic, non-bypassable)
                           │
                           ▼
                     EXECUTION GATEWAY  (future; durable order intent → broker → reconcile)
                           │
                           ▼
                         FYERS  (future; only after human-approved promotion + canary)
```

AI authority model (precise): MAY inspect/record/research/compare/propose. MAY NOT call order
endpoints, modify positions, bypass risk, change limits, self-approve live, see creds, build
broker requests, disable gates. Strategy-generation sandbox: hypothesis → static AST allow-list →
restricted interpreter (no net / no fs outside sandbox / no secrets / no broker / no subprocess /
no prod-DB mutation / no orders). VT's AST check is necessary-but-not-sufficient → add process
boundary; reject-on-fail.

---

## 9. WHAT IS LEFT (roadmap — proposed, derived from repo readiness)

- **Day 11 — Strategy discovery (research-only):** `StrategySpec` + `DiscoveryEngine` turning a
  `Hypothesis` into candidate strategies via documented bounded hyperparameter sets (NO
  optimization/best-of). Wire `run_backtest`+`compute_performance`+`ExperimentManifest` into
  `analyze_strategy(...)` recording an `EvidenceRun` (regime, OOS flag, cost). No LLM code-gen yet.
- **Day 12 — Strategy sandbox:** static validation + restricted execution; backtest+OOS+walk-forward
  + cost/slippage stress + regime analysis → evidence. (First place LLM strategy generation could
  be allowed, sandboxed.)
- **Day 13 — Agent tool layer:** RESEARCH / TRADING / EXECUTION permission tiers; MCP optional.
- **Day 14 — Paper trading:** `PaperBroker` implementing `ExecutionInterface`; same Risk/Strategy
  as live. No FYERS order write.
- **Day 15 — Portfolio / multi-strategy:** allocation, limits, exposure, concentration, correlation.
- **Day 16 — Execution boundary:** `ExecutionGateway`, durable order intent, policy gate. Still
  PaperBroker-backed.
- **Day 17 — FYERS integration:** read-only + paper via FYERS paper API; durable order state;
  reconciliation; kill switch. (Requires refreshed FYERS token.)
- **Day 18 — Live canary:** small capital, human-approved promotion, monitoring, decay detection,
  retire.

Prerequisites blocking several phases: **refresh expired FYERS token** (to backfill F&O/commodity
history and exercise derivative paths); **India cost model** (not yet implemented — `breakeven_fee_bps`
is generic); **more instruments stored** (cross-sectional research currently impossible with one symbol).

---

## 10. KNOWN LIMITATIONS / HONEST GAPS

- Only one instrument has history → no cross-sectional factor research, no multi-name backtest.
- Derivative/commodity analysis blocked (no stored data; token expired).
- OI/IV/greeks/basis remain `None` (schema fields exist; FYERS doesn't supply them in current flow).
- India-specific cost schedule not implemented; current breakeven is generic.
- Factor set is small/interpretable by design; no optimization or parameter search has been run
  (Day 10 forbids it). SBI results are NOT profitability claims.
- `local` AI is a deterministic heuristic; `openai` untested (no creds).
- Frontend exists but is not wired to the Day 8/10 research/evidence APIs yet (contract doc exists:
  `FRONTEND_BACKEND_CONTRACT.md`).

---

## 11. DEPENDENCY POSTURE

Present: stdlib, numpy, pandas, sqlalchemy, pydantic (project deps). 
Deliberately NOT added: vibe-trading, mcp, ccxt, vnpy, alpaca, binance SDKs, OpenAI SDK, Ollama,
scipy (avoided — Spearman computed via pandas `.rank()`, no scipy import). 
If a sandbox lib is ever needed (Day 12), evaluate it explicitly; default is pure-Python on existing arch.

---

## 12. TESTS / VERIFICATION HABITS (how to prove claims)

- Always run `pytest` before claiming done. Current: **276 passed**.
- Look-ahead is a hard regression test (mutate future bars → past features/IC unchanged).
- Evidence store round-trips; manifest hash determinism + config-change tested.
- Warmup must not improve metrics (tested).
- No execution symbols exist (`grep -rniE "class .*broker|def place_order|..."` → NONE).
- Real data used honestly; missing data → BLOCKED/NO_DATA, never fabricated.

---

## 13. KEY DECISIONS / CONSTRAINTS TO PRESERVE

- Reuse existing `indicators`, `analysis.quant`, `MarketStore`, `ModelProvider` — do NOT duplicate.
- Evidence store reuses MarketStore's SQLAlchemy engine — do NOT create a second DB framework.
- `SignalCandidate` (Day 8) is an analytical hypothesis, distinct from `signals.Signal` (execution
  decision). No order placement anywhere.
- No-look-ahead enforced by slicing `index <= ts` and by warmup/eval window separation.
- All thresholds in evidence quality/freshness are flagged provisional/configurable.
- Nothing is committed automatically; `.env` gitignored.

---

## 14. WHAT TO TELL THE NEXT AGENT (Claude) IF IT ASKS "WHAT SHOULD I BUILD NEXT?"

Start with **Day 11 (strategy discovery, research-only)** — it is the natural next layer and keeps
the safety boundary. Do NOT jump to execution. Before any F&O/derivative work, the FYERS token must
be refreshed and history backfilled. Before any live code, the India cost model and strategy sandbox
(Day 12) must exist. The full blueprint is in `DAY9_AUDIT.md`; the research-spine detail is in
`DAY10_REPORT.md`; execution boundary design is specified (not built) in `DAY9_AUDIT.md` §10–§11.

---

## 15. FILE INVENTORY (current, verified)

New/changed this session (Day 10):
- `src/trading_system/research/factors.py` (NEW)
- `src/trading_system/research/factor_analysis.py` (NEW)
- `src/trading_system/research/evidence.py` (NEW)
- `src/trading_system/research/backtester.py` (MODIFIED: warmup/eval window)
- `src/trading_system/research/__init__.py` (MODIFIED: exports)
- `src/trading_system/__main__.py` (MODIFIED: `research` CLI)
- `tests/test_factors.py`, `test_factor_analysis.py`, `test_evidence.py`, `test_warmup.py` (NEW)
- `DAY10_REPORT.md` (NEW)
- `README.md`, `ARCHITECTURE.md` (MODIFIED: research-spine docs)

Reference docs present: `DAY7_REPORT.md`, `DAY8_REPORT.md`, `DAY9_AUDIT.md`,
`FRONTEND_BACKEND_CONTRACT.md`, `DAY10_REPORT.md`.

Git: uncommitted (by instruction). No production execution surface added.
