# Greeks Decision Layer — Design

**Status:** Phases 0–5 implemented, PAPER-only. **On by default in production**
(the scheduler worker always seeds the `AUTONOMOUS_GREEKS_*` variables the
operator has not set; the FastAPI API does the same when
`ENVIRONMENT=production`, which the deployment images set); still fully
env-overridable and reversible.
Phases 0–1 add the shadow/trust-predicate engine (`autonomous/greeks_policy.py`),
fallback accounting (`autonomous/greeks_metrics.py`), the `AUTONOMOUS_GREEKS_*`
flags (`autonomous/bot_config.py`), and the builder seam
(`options_contract.py` / `controller.resolve_options_plan`). Phases 2–3 add
target-delta strike selection (`autonomous/greeks_selection.py`) and risk-budget
sizing (`autonomous/greeks_sizing.py`), wired into the same seam behind
`AUTONOMOUS_GREEKS_TARGET_DELTA` / `AUTONOMOUS_GREEKS_RISK_SIZING` and gated by
the master `AUTONOMOUS_GREEKS_ENABLED` switch. Phase 4 adds book-level greek
caps (`autonomous/portfolio_greeks.py`, enforced entry-only in
`controller.execute_option_order` behind `AUTONOMOUS_GREEKS_PORTFOLIO_LIMITS`
plus the four `AUTONOMOUS_GREEKS_MAX_NET_*` caps). Phase 5 adds the `iv_crush`
exit (`autonomous/greeks_exits.py`, wired into `portfolio._exit_reason` behind
`AUTONOMOUS_GREEKS_EXITS_ENABLED`). With no `AUTONOMOUS_GREEKS_*`
variable set the layer computes nothing and the legacy path is byte-identical.
The **production processes** opt in on the operator's behalf at startup
(`apply_greeks_production_defaults` in `autonomous/bot_config.py`, called by
the scheduler worker's `run()` and by the FastAPI API lifespan when
`ENVIRONMENT=production`): target delta 0.50, net book delta cap 5.0, risk
sizing and IV-crush exits on. Those are defaults only — `os.environ.setdefault`,
so any variable the operator sets, including a falsy value, wins and the layer
can be rolled back without a redeploy.
**Companions:** `GREEKS_PLAN.md` (shipped read-only analytics), `ARCHITECTURE.md`, `AGENTS.md`.

---

## 1. Goal

Use greeks to *decide* option trades — strike selection, sizing, portfolio limits, exits —
instead of the current heuristic of "a strike roughly 2% away, one contract".

Today the bot picks option contracts by price proximity and buys a fixed single contract.
It never asks what risk that purchase actually carries. This document specifies a layer that
does, in stages, each one independently switchable.

## 2. Non-goals

- **Not a live trading path.** Everything stays paper-only. Observational until a flag is set.
- **Not a pricing engine.** `autonomous/greeks.py` is the single source of math truth. This
  layer consumes greeks; it never re-derives them differently.
- **Not a signal generator.** The strategy signal still decides *whether* to trade. This layer
  decides *how* — which strike, how many contracts, whether the book can afford it.
- **No new market data.** Uses the chain snapshot the resolver already fetches
  (`options_contract.py:704`). No extra upstream calls.
- **Not a UI change.** `/paper/options` already renders greeks; this is about decisions.

## 3. Current state

| Stage | Behaviour today | Location |
|---|---|---|
| Strike selection | `moneyness_offset = 0.02` — a 2% *price* offset, delta-agnostic | `options_contract.py:338`, `:757` |
| Sizing | `qty = cfg.max_contracts_per_leg`, default `1.0` — one contract | `options_contract.py:340`, `:766` |
| Expiry filter | `min_days_to_expiry = 7` | `options_contract.py:339` |
| Portfolio risk | No greek aggregation exists. Limits are position counts and notional fractions | `bot_config.py:196-222`, `safety.py:531-556` |
| Exits | One rule: delta decay via `delta_retention() < floor` | `portfolio.py:1204-1269` |

Two structural consequences worth stating plainly:

- `max_simultaneous_positions` defaults to `1` (`bot_config.py:196-200`). With one open
  position, a book-level delta cap **cannot bind**. Phase 4 is therefore gated on a separate
  decision about position limits.
- Greeks are only computed today *after* a position exists, during mark-to-market
  (`controller.py:1466` `_capture_position_greeks`). Nothing computes them before an entry.

## 4. Locked decisions

These are settled; later phases should not reopen them.

| # | Decision | Consequence |
|---|---|---|
| D1 | **Fail open on unknown greeks.** Missing IV, unusable quote, or uncomputable greek → skip the greek logic and fall through to today's behaviour. Never refuse a trade for absent data. | Every gate is conditioned on a trust predicate. Requires D6. |
| D2 | **Env-gated, default off**, with one master kill switch, copying `AUTONOMOUS_DELTA_DECAY_ENABLED` (`portfolio.py:1204-1214`). | Safe to land in production inert. Roll back by unsetting an env var. |
| D3 | **Prefer solved IV.** Build on `normalise_iv()` and the mid-premium fallback (`iv_source: "solved"`) rather than Upstox's IV field, whose names and scale are still unverified. | Working greeks without waiting on an unknown payload. |
| D4 | **Paper-only.** No broker calls, no config schema changes, no DDL. | Preserves the existing invariant. |
| D5 | **Causal and deterministic.** Greeks come from the chain snapshot as-of the decision. Same decision + same chain → identical greeks. | Reproducible backtests and replays. |
| D6 | **Fallback accounting is mandatory.** Because D1 degrades silently, fallback rate is a first-class, persisted, alertable metric. | The counterweight to fail-open. Without it, D1 hides outages. |
| D7 | **Null never coerces to zero in a gate.** An uncomputed gamma must not read as "no gamma". | A `None` reaching a comparison is a bug, caught by tests. |
| D8 | **Safety stays I/O-free.** Greek exposure is aggregated upstream and passed into `safety.py` as numbers. | Safety never fetches a chain; validation stays fast and pure. |

### D1 in detail

Fail-open is the right default here because the greek inputs are not yet trusted: live
Upstox IV field names and scale are unverified. Refusing trades on unverified data could
halt the bot entirely for reasons that have nothing to do with the strategy.

The cost of fail-open is that bot behaviour becomes data-dependent — the same strategy can
behave differently on two days because one day's quotes were unusable. That is acceptable
only if it is *visible*. Hence D6, and hence phase 1 below is a trust predicate rather than
a refusal.

## 5. Architecture

### The seam

`OptionsStructureBuilder.build()` (`options_contract.py`) is the only place a
`TradingDecision` already holds the chain and chooses a strike. It fetches the chain via
`chain_provider.get_chain(underlying, expiry=None)`, applies the existing spot-sanity check,
then dispatches to a builder (`_build_long_call` etc.). `OptionContractResolver` is the
helper the builders call to pick a strike from the chain.

The policy layer wraps this. It does not reach into the scanner or ranker, which work on
OHLCV and have no chain.

**Implemented integration (Phases 2–3).** The builder carries a transient `_LayerContext`
for the duration of a single `build()` call (cleared in a `finally`). Phase 2 routes the
near-leg selection through `_near_instrument()`, which calls `select_by_target_delta()`
when a target delta is configured and otherwise delegates to the legacy
`OptionContractResolver` offset. Phase 3 routes single-leg quantity through
`_leg_quantity()`, which calls `size_for_risk_budget()` and falls back to
`max_contracts_per_leg` on any failure. Phase 2/3 inputs are resolved in the controller:
`target_delta` (with the `target_delta_by_strategy` override) plus `risk_budget` /
`notional_cap` derived from `capital_allocation` and `max_position_allocation_pct`
(`risk_budget_pct` defaults to `max_position_allocation_pct`).

### New modules

`src/trading_system/autonomous/greeks_policy.py` — pure, no I/O, no clock reads beyond an
injected `now`:

```python
# greeks_policy.py (Phase 0/1) — implemented
def evaluate_candidate(
    *, instrument, quote, chain, spot=None, now=None, rate=..., min_greeks=("delta",),
) -> GreeksVerdict:
    """Per-instrument greek evaluation. Never fetches anything."""

# greeks_selection.py (Phase 2) — implemented
def select_by_target_delta(
    *, chain, option_type, target_delta, delta_tolerance, now,
    rate=DEFAULT_RISK_FREE_RATE,
) -> Optional[StrikeSelection]:
    """Strike whose delta lands in the configured band, or None (fail open)."""

# greeks_sizing.py (Phase 3) — implemented
def size_for_risk_budget(
    *, verdict, risk_budget, notional_cap, max_contracts,
    contract_multiplier=1,
) -> Optional[SizingResult]:
    """Contracts to trade, plus the reason any cap bound it."""
```

`src/trading_system/autonomous/portfolio_greeks.py` (Phase 4, proposed) — aggregation across open positions:

```python
def aggregate(positions, now) -> PortfolioGreekExposure:
    """net/gamma/vega/theta totals, plus which positions lack greeks."""
```

### Config

- Phase parameters live on `GreeksPolicyConfig` (`bot_config.py`): `enabled` (master
  switch), `shadow`, `target_delta`, `delta_tolerance`, `target_delta_by_strategy`,
  `risk_sizing`, `risk_budget_pct`, `portfolio_limits`.
- `OptionsTradeConfig` is unchanged: it still carries the legacy fallback knobs
  (`moneyness_offset`, `max_contracts_per_leg`, `contract_multiplier`), which remain the
  values used whenever the greeks layer is off or a phase fails open.
- Every field defaults to today's behaviour. A default that changes trading is a bug.

### The trust predicate

Availability is **per field**, not one boolean. A quote with no IV still has a usable delta
only if IV can be solved from the mid; a zero-width spread invalidates the mid itself.
A single blanket `greeks_ok: bool` would let a missing gamma disable working delta logic.

Each gate therefore asks the narrow question it needs — "do I have a usable delta?" — and
records why the answer was no. `GreeksVerdict` carries per-field availability plus a
`fallback_reason`.

## 6. Phases

Each phase ships independently, default-off, and is separately verifiable. Do not merge
phases into one commit; the point of the gating is that each can be reasoned about alone.

### Phase 0 — Shadow mode (no behaviour change)

Compute greeks for every contract the resolver would pick. Log the verdict and what the
layer *would* have done. Touch nothing.

- Flags: `AUTONOMOUS_GREEKS_SHADOW=1`
- Delivers: the fallback-rate baseline that every later phase needs, and an honest answer to
  "is our quote data good enough to trade on".
- Exit criteria: fallback rate measured over a meaningful sample, and the dominant
  `fallback_reason` identified. **If the fallback rate is high, stop here** — phases 2-4
  would mostly not fire.

### Phase 1 — Trust predicate

Wire the per-field availability check into the resolver. With the flag on, trades with
usable greeks carry a `GreeksVerdict` on the plan; trades without fall through to today's
behaviour and log a reason.

- Flags: `AUTONOMOUS_GREEKS_ENABLED=1`
- Note: this phase *gates applicability*, it does not refuse trades. That is D1.
- Tests: each fallback reason reachable; a trade with a good delta but no gamma still gets
  delta logic.
- Exit criteria: fallback reasons enumerated from real data, none dominant.

### Phase 2 — Target-delta strike selection

Replace the 2% price offset with a delta band. Select the strike whose computed delta lands
closest to `target_delta` within `delta_tolerance`.

- Flags (implemented): `AUTONOMOUS_GREEKS_TARGET_DELTA=<delta>` (a magnitude, e.g. `0.30`),
  `AUTONOMOUS_GREEKS_ENABLED=1`; per-family overrides via
  `GreeksPolicyConfig.target_delta_by_strategy`.
- Motivation: "2% away" means something different at 15% IV than at 40%, and different things
  again per expiry. A delta target makes legs comparable.
- Applied to the near leg of single-leg and vertical-spread strategies via
  `_near_instrument()`. Straddle/strangle keep their legacy ATM/OTM selection (a directional
  delta target is not meaningful for a two-sided structure).
- Tests: target hit exactly at ATM; falls back to legacy selection when no strike qualifies
  (never returns nothing where the old path would have returned something); band edges;
  multi-expiry chains. Implemented in `tests/test_greeks_selection.py` and
  `tests/test_greeks_integration_phase2_3.py`.
- Exit criteria: shadow-mode comparison shows the strike choice changing for a defensible
  reason, not randomly.

### Phase 3 — Risk-based sizing

Size legs to a risk budget rather than the fixed `1.0` contract.

- Flags (implemented): `AUTONOMOUS_GREEKS_RISK_SIZING=1`,
  `AUTONOMOUS_GREEKS_RISK_BUDGET_PCT=<fraction>`, gated by `AUTONOMOUS_GREEKS_ENABLED`.
- Order of operations: compute budget → derive contracts → clamp by `max_position_allocation_pct`
  → clamp by `max_contracts_per_leg` → record which clamp bound (`bound_by` precedence:
  max contracts → notional → risk budget).
- **Trap:** greeks are per *option unit*; position risk is per *contract*, scaled by
  `contract_multiplier` (1 for India). `size_for_risk_budget` uses
  `risk_per_contract = abs(price) * contract_multiplier`.
- **Scope (implemented):** sizing applies to single-leg long options (`LONG_CALL`,
  `LONG_PUT`) via `_leg_quantity()`. Multi-leg structures keep the legacy fixed quantity:
  a single leg's premium is not the structure's risk (a debit spread's risk is the net
  debit), so per-leg sizing of a spread is deliberately deferred.
- Fail-open: a zero/non-finite budget, an unusable verdict, or `now=None` restores
  `max_contracts_per_leg`.
- Tests: budget respected; every clamp reachable and reported; monotonicity (bigger budget
  never yields fewer contracts); integer rounding never exceeds the notional cap. Implemented
  in `tests/test_greeks_sizing.py` and `tests/test_greeks_integration_phase2_3.py`.
- Exit criteria: paper soak, because this changes P&L and drawdown, not just selection.

### Phase 4 — Portfolio greek limits (implemented)

`autonomous/portfolio_greeks.py` aggregates a book into net greek exposure and checks a
prospective entry against per-metric caps. Enforcement is entry-only in
`controller.execute_option_order` (a cap on new risk must never trap an exit).

- Flags: `AUTONOMOUS_GREEKS_PORTFOLIO_LIMITS=1` plus caps `AUTONOMOUS_GREEKS_MAX_NET_DELTA`
  / `_GAMMA` / `_THETA` / `_VEGA` (unset = uncapped, `0.0` = a real cap of no net exposure).
- Production runs `max_simultaneous_positions=5` (`__main__.py`), so the book-level caps can
  genuinely bind. The config default is 5 to match; a single-position book keeps them inert.
- Per D8, `aggregate_positions` runs upstream (controller) and the pure checker receives
  numbers; `safety.py` is untouched and stays I/O-free.
- Unknown is never zero (D7): a position whose delta is unreadable keeps `net_delta` `None`,
  so `check_portfolio_limits` skips the metric and fails open. The live book persists
  gamma/theta/vega per position alongside delta (`last_<greek>` with `entry_<greek>` as the
  fallback anchor, captured from the same quote), so those caps bind too; a value missing for
  even one position keeps that net `None`. An optional `greeks_by_key` source overrides the
  stored values when the caller holds fresher greeks.
- Tests: `tests/test_greeks_portfolio_limits.py`, `tests/test_greeks_integration_phase4_5.py`.

### Phase 5 — Exit rules (implemented)

`autonomous/greeks_exits.py` is an ordered, independently gated rule registry evaluated from
the persisted position facts. The one honest rule today is **IV crush** (`iv_crush`): a
long-premium thesis is spent once implied vol falls below a configured fraction of its entry
level. Wired into `AutonomousPortfolio._exit_reason` after the P&L thresholds and the
delta-decay rule, so it can only close a position sooner, never later.

- Flags: `AUTONOMOUS_GREEKS_EXITS_ENABLED=1` (and the master `AUTONOMOUS_GREEKS_ENABLED`),
  `AUTONOMOUS_IV_CRUSH_FLOOR` (default 0.5, clamped to (0,1)),
  `AUTONOMOUS_IV_CRUSH_MIN_HOLDING_SECONDS` (default 0.0).
- A future theta/gamma/vega rule registers itself in `_RULES`; the caller does not change.
- Tests: `tests/test_greeks_exits.py`, `tests/test_greeks_integration_phase4_5.py`.

## 7. Observability

Because D1 degrades to today's behaviour rather than failing, these are not optional:

| Metric | Meaning |
|---|---|
| `greeks_evaluated` / `greeks_skipped` | Applicability rate |
| `greeks_fallback_reason` | Dominant reason, broken down — the early-warning signal |
| `strike_selected_by` | legacy price-offset vs target-delta, so phase 2's effect is measurable |
| `sizing_bound_by` | which clamp bound sizing, and how often |
| `greek_limit_blocked` | phase 4 refusals, with the limit that fired |
| `shadow_verdict_diff` | where shadow mode disagreed with actual behaviour |

**Requirement:** a persistently high fallback rate must be alertable. Fail-open without
accounting is indistinguishable from the layer silently doing nothing.

## 8. Testing strategy

- **No live data.** Synthetic chains via `InMemoryOptionsChainProvider` (`options_contract.py:362`),
  shaped like `tests/test_phase24_options_analytics_api.py`.
- **Property tests**, not just examples: delta in (−1, 1) for long options; put delta negative;
  `aggregate()` net delta equals the sum of per-position contributions; sizing monotonic in
  budget; expiry in the past never selects.
- **Causality:** pin a chain snapshot and assert the greeks are byte-identical across runs and
  unaffected by later data.
- **Unit traps:** one explicit test per unit boundary (per-unit vs per-contract; IV as decimal
  vs percent; theta per day vs per year).
- **Regression floor:** the existing 2914-test suite must stay green. Every gate needs a test
  proving it *did not* fire when the flag is off.

## 9. Risks

| Risk | Mitigation |
|---|---|
| Silent degradation hides a broken quote feed | D6 fallback accounting, alertable |
| Unverified Upstox IV names/scale | D3 solved-IV preference; report `iv_source` per row |
| Per-unit vs per-contract unit error | Explicit conversion tests, phase 3 trap noted above |
| Sizing change moves drawdown materially | Phase 3 gated, paper soak, exit criteria |
| Portfolio caps cannot bind at 1 position | Resolved: `max_simultaneous_positions` defaults to 5 |
| Greeks treated as truth when stale | Use as-of-decision snapshot only (D5) |

## 10. Rollout

1. Land phase 0, run it, read the fallback rate. **Gate: if the rate is high, stop and fix data.**
2. Enable phase 1. Behaviour identical to today, but every decision now carries a reason.
3. Enable phase 2 per strategy family. Compare against shadow verdicts.
4. Enable phase 3 after a soak with drawdown compared to the pre-change baseline.
5. Phase 4 now that `max_simultaneous_positions` defaults to 5.

Each step is a single env var. No code change, no redeploy of logic, no migration.

## 11. Open questions

1. ~~**Position limits.** Should `max_simultaneous_positions` rise above 1?~~ Resolved:
   `max_simultaneous_positions` defaults to 5, so Phase 4 caps can bind.
2. **Target delta per strategy.** One global target, or per strategy family? A trend strategy
   and a mean-reversion strategy plausibly want different directional exposure.
3. **Does theta belong in sizing?** Long premium bleeds by construction. Whether carry should
   shrink size, shorten the horizon, or only inform exits is a real design choice, not an
   implementation detail.