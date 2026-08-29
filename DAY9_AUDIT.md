# Day 9 — Vibe-Trading Capability Audit & Autonomous-Trading Blueprint

**Date:** 2026-08-29
**Nature:** ARCHITECTURE AUDIT + TECHNICAL BLUEPRINT. **No code implemented. No execution
added. No broker endpoints. No dependency installs.** Vibe-Trading is a *reference*, not a
dependency.

## 0. Verified current state (do not trust prior reports)
- `pytest -q` → **233 passed, 0 failed** (169 Day1-6 + 32 Day7 research + 32 Day8 intelligence).
- `git status`: production Python changes are Day 1-8 work; **no execution/broker/order code
  exists** (grep for `class.*Broker|def place_order|def submit_order|order_gateway|
  execution_gateway` → NONE). The only `__main__.py` "order" match is the literal string
  "ANALYSIS ONLY — NO ORDER PLACED".
- Existing reusable pieces: `research/` (features, strategies, backtester, performance,
  walkforward, risk, **intelligence**), `indicators/`, `models/` (`MarketSnapshot`,
  `MarketView`, `ModelProvider`, `analyze_snapshot`, `signals.generate_signal`),
  `india/` (FYERS adapter, instruments, derivative resolver, discovery, `DataHealthMonitor`,
  closed-candle pipeline), `storage/` (`MarketStore`, idempotent upsert).
- `RiskConfig` exists: leverage 1.0, `allow_short=False`, no leverage by default — conservative.
- FYERS token **expired**; live verification blocked. Stored data: `NSE:SBIN` 1d (2477 bars) +
  5m (600 bars). No F&O/commodity history.

---

## 1. Current architecture (as-built)
```
FYERS ──> normalized events ──> MarketStore(SQLite, idempotent)
                                  │
              ┌───────────────────┼────────────────────┐
              ▼                                        ▼
   RESEARCH (backtest/strategies)            MARKET INTELLIGENCE (Day8)
   - features, walk-forward, performance      - FeatureEngine, MarketRegime
   - risk (RiskConfig)                        - SignalCandidate (hypothesis, NOT order)
                                             - AnalysisExplanation, AIAnalysis
                                             - MarketReasoningProvider (wraps ModelProvider)
DataHealthMonitor gates live feed. AI vendor decoupled via ModelProvider
(local rule-model + OpenAI-compatible). NO execution layer. NO order surface.
```

## 2. Vibe-Trading capability map (per requested axis)

### Research
| Capability | VT impl | Value to us | India adaptation | Deps | Security risk | Compat | Rec |
|---|---|---|---|---|---|---|---|
| Factor analysis (Alpha158=154, gtja191=191) | `agent/src/trading/alphas/*` | High (research spine) | **Adapt**: build India factor library (momentum/mean-reversion/vol/carry/roll-yield) on our OHLCV+derivative schema | qlib (Apache-2) if reused | low if isolated | our `research/` | **ADAPT** |
| IC/IR | implied in alpha compare | High | reuse our `analysis.quant` | none | low | yes | **ADAPT** |
| Decile/grp backtest | alpha compare | Med | group by quantile on our factors | none | low | yes | **ADAPT** |
| Strategy discovery | skill-generated | High | Hermes hypothesis → candidate | LLM | prompt-injection | via tools | **ADAPT (+sandbox)** |
| Evidence store | manifest + run cards | High | new `evidence` tables in our DB | none | low | yes | **ADOPT** |
| Hypothesis lifecycle | Hypothesis Registry | High | `hypotheses` table + states | none | low | yes | **ADOPT** |
| Experiment tracking | manifest hashes prompt+skill+tool+ver | High | reuse idea: hash inputs for reproducibility | none | low | yes | **ADOPT** |
| Cost-sensitive analysis | per-broker cost models | High | **India cost model** (STT/exchange/SEBI/stamp/brokerage/GST) — NOT Alpaca 0-comm | none | wrong-cost bug risk | our own | **ADAPT** |
| Warmup/eval separation | `warmup_bars`/`evaluation_start_date` | High (anti-lookahead) | add `warmup` to our backtester | none | low | yes | **ADOPT** |

### AI
| Capability | VT impl | Value | India adapt | Deps | Risk | Compat | Rec |
|---|---|---|---|---|---|---|---|
| Strategy generation | skills write Python | High | generate candidate code in sandbox | LLM | code-exec risk | tools | **ADAPT (+sandbox)** |
| Journal→hypothesis | trade journal → hyp | Med | our journal table | none | low | yes | **ADAPT** |
| SignalEngine | `signal_engine` | Med | our `signals.generate_signal` already exists | none | low | yes | **REUSE** |
| AI research workflows | swarm | Med | single Hermes + deterministic tools | none | low | yes | **ADAPT** |
| Grounding | fetched market data | High | pass structured `AnalysisContext` (we do) | none | low | yes | **REUSE** |
| Agent tools | MCP | High | our tool layer w/ permission tiers | MCP lib opt | tool-injection | yes | **ADOPT (RESEARCH only)** |
| Multi-agent/swarm | many workers | Low for us | reject swarm-for-complexity | heavy | high | no | **REJECT (now)** |

### Trading
| Capability | VT impl | Value | India adapt | Deps | Risk | Compat | Rec |
|---|---|---|---|---|---|---|---|
| Shadow account | durable paper | High | `PaperBroker` implementing our `ExecutionInterface` | none | low | yes | **ADOPT** |
| Paper trading | shadow acct | High | same Strategy/Risk as live, swap broker | none | low | yes | **ADOPT** |
| Portfolio construction | `/portfolio` | Med | our `portfolio/` module later | none | low | yes | **ADAPT** |
| Risk controls | policy gates | High | extend `RiskConfig` + `RiskEngine` | none | low | yes | **ADAPT** |
| Broker abstraction | 12 connectors | High pattern, **0 India** | `ExecutionInterface` + `FyersBroker` (future) | none | creds | yes | **ADAPT** |
| Durable order state | durable-before-broker | **Critical** | `order_intents` table + `client_order_id` recovery | none | low | yes | **ADOPT** |
| Execution gateway | gate | High | `ExecutionGateway` + policy gate | none | low | yes | **ADOPT** |
| Reconciliation | signed position delta | High | our reconciliation vs FYERS book | none | low | yes | **ADOPT** |
| Live canary | canary runs | Med | small-capital, human-approved | none | money | later | **ADAPT** |

### Infrastructure
| Capability | VT impl | Value | India adapt | Deps | Risk | Compat | Rec |
|---|---|---|---|---|---|---|---|
| MCP | client+server | High (tool boundary) | our tool schema, 3 tiers | `mcp` opt | injection | yes | **ADOPT (research)** |
| Skills/tools | skill files | Med | our skills system | none | low | yes | **REUSE** |
| Data loaders + fallback | 23 srcs | High pattern | our FYERS + fallback concept | none | low | yes | **ADAPT** |
| Persistence | Docker volumes | Med | our SQLite/MarketStore | none | low | yes | **REUSE** |
| Testing arch | coverage CI | High | keep our pytest green gate | none | low | yes | **REUSE** |

## 3. Adopt / Adapt / Reject matrix
**ADOPT (add):** Evidence store (DB tables), Hypothesis Registry (DB + states), durable
order-intent + `client_order_id` recovery, reconciliation, warmup/eval split, MCP tool
boundary (RESEARCH tier), manifest/hash reproducibility, Shadow/Paper account pattern,
permission-tiered tool model, TAP-style credential isolation concept.
**ADAPT (rebuild on our arch):** factor library (India-specific), IC/IR/decile backtest,
cost-sensitive analysis with **Indian** charges, strategy-generation sandbox, portfolio/
risk engine, broker abstraction with `FyersBroker`, execution gateway + policy gate,
experiment tracking, multi-agent trimmed to role split.
**REJECT:** Alpaca/US connectors, vnpy export, ccxt, MT5/Exness, Binance/OKX, Futu/Longbridge,
A-share Tushare, gold/metal FX-pip conventions, swarm-for-complexity, any token/coin (see
VT SECURITY.md impersonation warning), LLM strategy generation **without** a sandbox.

## 4. Target autonomous architecture (refined)
```
MARKET DATA → DATA NORMALIZATION → MarketStore
   ┌───────────────────────┬───────────────────────┐
   ▼                       ▼                        ▼
MARKET INTELLIGENCE   RESEARCH ENGINE          EVIDENCE STORE (DB)
   │                  (factors, backtest,        hypotheses, runs,
   │                   regimes, OOS, WF)              metrics, manifest)
   └───────────┬──────────────┘
               ▼
         HERMES AGENT (tool caller; NO broker creds)
               │ research hypotheses (RESEARCH tools only)
               ▼
         STRATEGY SANDBOX (restricted exec)
               │ static validate → backtest → OOS → WF → cost/slippage stress → regime
               ▼
         EVIDENCE STORE (promotion criteria checked)
               ▼
         PAPER TRADING (PaperBroker == live interface, no money)
               ▼
         RISK ENGINE (deterministic limits; Hermes cannot change)
               ▼
         EXECUTION GATEWAY (policy gate; durable order intent)
               ▼
         FYERS BROKER (future; read-only/paper first)
```
Research/AI/Risk/Execution are **separate processes with explicit interfaces**. Hermes sits
between Research and Strategy; it never touches the Gateway directly.

## 5. AI permission model
**MAY:** inspect intelligence; inspect research; inspect factor results; formulate
hypotheses; request backtests/factor analysis/walk-forward/cost-stress; compare strategies;
analyze paper results; detect decay; propose retirement/new research; generate candidate
strategy **code into the sandbox only**.
**MAY NOT:** call FYERS order endpoints; modify positions; bypass risk; change risk limits;
self-approve live promotion; see credentials; build authenticated broker requests; disable
safety gates. Every tool call is logged; EXECUTION tools require human approval until Day 18.

## 6. Strategy-generation sandbox design
Flow: hypothesis → candidate → static validation (AST allow-list: no `import os/sys/
subprocess/socket`, no `__import__`, no `open` outside sandbox dir, no attribute access to
`fy`/`broker`/`order`, no `eval/exec/compile` of external strings) → run in **restricted
interpreter** (Subprocess + seccomp/container, OR `RestrictedPython`) with: no network, no
fs outside sandbox, no secrets (env empty), no broker APIs, no subprocess spawn, no prod-DB
mutation (read-only snapshot), no order calls. Output: a `Strategy` instance scored by the
research engine. VT's AST check is **necessary but not sufficient** → we add the process
boundary. Reject if any check fails (never warn-only).

## 7. MCP / tool architecture
Three tiers, explicit permissions:
- **RESEARCH** (Hermes autonomous): `get_market_data`, `get_features`, `get_market_regime`,
  `run_backtest`, `run_factor_analysis`, `get_strategy_evidence`, `compare_strategies`,
  `run_walk_forward`, `run_cost_stress`. No money side-effects.
- **TRADING** (Hermes autonomous, no broker): `start_paper_strategy`, `get_paper_results`,
  `retire_strategy`, `propose_allocation`.
- **EXECUTION** (human-approved only, Day 18+): `submit_order`, `cancel_order`,
  `reconcile`, `kill_switch`. Never exposed to Hermes without an approved promotion.
Tool results are **data, not instructions** (injection-safe); `env`/creds never serialized.

## 8. Multi-agent recommendation
**Smallest useful = one Hermes agent calling deterministic tools.** A full swarm is rejected
for now. If value emerges, add at most 3 roles sharing the SAME tool layer: Researcher
(discovery/backtest), Risk Analyst (limit/decay checks), Strategy Critic (challenges
evidence). Portfolio Manager role = Hermes proposing allocations within hard limits. No
agent may call EXECUTION tools.

## 9. Research lifecycle (deterministic promotion)
`HYPOTHESIS → RESEARCH → BACKTEST → HOLDOUT(OOS) → PAPER → CANARY → LIVE → REVIEW → RETIRED`.
Promotion criteria (all must hold; strategy cannot self-promote):
- min sample: ≥ N trades (e.g. ≥30) and ≥ X months of data;
- OOS Sharpe ≥ threshold AND OOS ≤ in-sample within tolerance (no >2× blow-up);
- max drawdown within limit; cost/slippage stress still profitable;
- parameter sensitivity: P&L stable across ±20% param perturbation;
- regime robustness: positive in ≥2 regimes;
- paper performance ≥ OOS within band; evidence not stale (< T days);
- human approval for CANARY→LIVE. Decay: rolling OOS Sharpe < floor for K days → auto-propose
  RETIRE (Hermes recommends, human retires).

## 10. India/FYERS execution architecture
`ExecutionInterface` (provider-independent): `submit(intent)→ack`, `cancel`, `status`,
`positions`, `reconcile`. `FyersBroker` implements it (future). India specifics we must
model (VT does NOT): NSE/NFO/MCX; futures+options CE/PE; lot sizes; expiry + **rollover**
(never merge contracts — our `contract_id` already separates); **costs**: STT (0.0125% sell
equity / 0.05% sell F&O), exchange txn charges, SEBI, stamp duty, brokerage, DP, **GST 18%
on charges**; margins (span/exposure); market hours + holidays; order types (LIMIT/MKT/SL);
partial fills; rejected orders; broker disconnects. Paper vs live differ ONLY at the broker
adapter. No Alpaca assumptions.

## 11. Dependency plan
- **New deps we may add later:** `mcp` (optional, research tools), a sandbox lib
  (`RestrictedPython` or container runtime) — only if LLM strategy-gen ships.
- **Rejected deps:** `vibe-trading-ai`, `qlib` (we write our own India factors), `ccxt`,
  `vnpy`, `ib_insync`, `alpaca-py`, any coin SDK.
- For every new dep: reason + alternative + security (supply-chain/audit) + maintenance cost.
  Default: **pure-Python on our arch** unless a dep is load-bearing and auditable.

## 12. Security / threat model
- Credential exposure: broker creds in sealed store (TAP-style); Hermes/LLM never receive
  them; only `FyersBroker` reads them at execution time. `.env` gitignored (verified).
- Prompt injection: MCP/web tool results = data, never instructions; sink-aware redaction;
  `env` never released.
- LLM code risk: sandbox (§6) + reject-on-fail.
- Order safety: durable intent before broker write; recover by `client_order_id` only; HALT
  on contradiction; duplicate-request idempotency; kill switch.
- Broker disconnect/timeout: reconciliation loop; unknown state → HALT, never resubmit.

## 13. Quant-risk concerns (learned from VT's own fixed bugs)
- Wrong cost conventions (their gold 1460× spread, HKD-as-USD) → we MUST encode Indian charges
  exactly and unit-test each.
- Paper PnL=0 bug (read key mismatch) → our PaperBroker emits the same schema live will
  consume; contract-test them.
- Look-ahead: warmup separation (adopt); never train indicators on eval window.
- Survivorship/selection bias: use the FULL stored universe, fail-closed on missing symbols
  (adopt their fallback-close principle).
- Overfitting: OOS + walk-forward + parameter sensitivity + regime robustness + decay.
- Unrealistic costs: cost/slippage stress mandatory before promotion.

## 14. Staged implementation roadmap (derived from repo readiness)
- **Day 10 — Research infra:** factor library + factor engine (IC/IR/decile); `Hypothesis`
  + `Evidence` DB tables; `warmup` param in backtester; experiment manifest/hash.
  Safety: read-only, no execution. Tests: factor math, warmup no-leak, evidence persistence.
- **Day 11 — Strategy discovery:** Hermes hypothesis → candidate; static AST validator;
  deterministic baselines + LLM-candidate-in-sandbox scaffold (no exec yet).
  Safety: generated code never runs outside sandbox. Tests: validator rejects bad AST.
- **Day 12 — Strategy sandbox:** restricted executor; wire backtest+OOS+WF+cost-stress+regime
  → evidence store; promotion-criteria checker (advisory). Safety: sandbox boundaries tested.
- **Day 13 — Agent tool layer:** RESEARCH/TRADING/EXECUTION tiers; permission model; MCP
  (research only). Safety: EXECUTION tier disabled; injection-safe results.
- **Day 14 — Paper trading:** `PaperBroker` implementing `ExecutionInterface`; same
  Strategy/Risk as live; paper ledger + reconciliation. Safety: no broker, no money.
- **Day 15 — Portfolio/risk:** multi-strategy allocation, limits, exposure, correlation;
  Hermes proposes, hard limits enforced. Safety: limits deterministic, unchangeable by AI.
- **Day 16 — Execution boundary:** `ExecutionGateway` + policy gate + durable `order_intents`
  (client_order_id recovery, HALT-on-contradiction). Safety: still PaperBroker-backed.
- **Day 17 — FYERS integration:** read-only + paper via FYERS paper API; durable order state;
  reconciliation; kill switch. Safety: live order write gated behind human approval.
- **Day 18 — Live canary:** small capital, human-approved promotion, monitoring, decay
  detection, auto-retire proposal. Safety: canary limits; kill switch; full audit ledger.

## 15. Exact Day 10 recommendation
**Objective:** stand up the research spine that everything else hangs off — factor library,
hypothesis/evidence persistence, and warmup-safe backtesting — with ZERO execution code.
**Files/modules:** new `research/factors.py` (India factor set: momentum/vol/carry/
mean-reversion/roll-yield), extend `research/backtester.py` with `warmup` (indicators
primed on warmup window, metrics only on eval window — no look-ahead), new
`research/evidence.py` (`Hypothesis`, `EvidenceRun`, `EvidenceStore` over MarketStore/SQLite),
`storage` migration adding `hypotheses` + `evidence_runs` tables (idempotent).
**Interfaces:** `FactorEngine.compute(df) -> DataFrame`, `EvidenceStore.save(run)`,
`BacktestConfig(warmup=...)`.
**Tests:** factor determinism, warmup does not leak future into eval metrics (no-lookahead
test), evidence round-trip, manifest hash reproducibility. Keep 233→green+new.
**Safety boundary:** read-only research; no broker; no order; no creds; reuse existing
indicators/quant; no new third-party deps.
**Prereqs:** Day 8 complete (done). FYERS token refresh NOT required (research is on stored
data). Optional: refresh token later to backfill F&O so factor research covers derivatives.

---
*Audit complete. No production code modified. No execution, broker, or order code added.
Vibe-Trading treated strictly as a reference; its India-incompatible and unsafe parts
(Alpaca assumptions, swarm complexity, token/coin surface, unsandboxed code-gen) are
explicitly rejected.*
