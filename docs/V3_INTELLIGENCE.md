# V3 Intelligence — Market Intelligence Foundation

Status: implemented, offline-deterministic. **Confidence is an analytical score
and is NOT a statistically calibrated probability until validated against
sufficient historical outcomes.**

All research/demo/test data in this layer is **SYNTHETIC / HISTORICAL RESEARCH
DATA — NOT LIVE MARKET DATA**. No broker API, key, websocket or live feed is
required or used. The live FYERS connection remains expired and is explicitly
NOT restored by V3.

## 1. Architecture

```
Synthetic/Historical data (provider-agnostic)
  ↓
Normalization (FeatureEngine — unchanged V2 core)
  ↓
Multi-timeframe features → per-TF regime/evidence/confidence
  ↓
Timeframe consensus (compute_timeframe_consensus)  — NOT an average
  ↓
Regime + transition detection (classify_regime + detect_regime_transition)
  ↓
Optional contexts (breadth / India VIX / FII-DII / sector / news / cross-asset)
  ↓  (missing source ⇒ UNAVAILABLE evidence item, never a fake value)
Evidence Ledger V2 (build_evidence_ledger_v2)
  ↓
Deterministic confidence (compute_confidence_v2 — LLM cannot override)
  ↓
Forecast (SignalCandidate + horizon + expected move + invalidation)
  ↓
ForecastStore (SQLite ledger, append-only)
  ↓
Historical replay (replay_history — strict no-lookahead)
  ↓
Outcome labeling (label_outcome)
  ↓
Calibration + feature performance (research-only)
```

Future live adapters plug in at the TOP only: they emit the same normalized
structures (`MarketBreadth`, `IndiaVIXContext`, `FIIDIIFlow`, `SectorContext`,
`NewsContext`, `CrossAssetContext`, OHLCV frames). No separate "live
intelligence" pipeline exists or will be forked.

## 2. Data contracts (`research/market_context.py`)

| Contract | Key fields | When unavailable |
|---|---|---|
| `MarketBreadth` | advancing/declining/unchanged, new highs/lows, source, data_quality | context `None` → UNAVAILABLE ledger item |
| `IndiaVIXContext` | india_vix, vix_change, vix_percentile, vix_regime | same |
| `FIIDIIFlow` | fii/dii `InstitutionalFlow(buy, sell)`, date, source | same |
| `SectorContext` | sector_symbol, sector_return, relative_strength, trend, momentum | same |
| `NewsEvent`/`NewsContext` | timestamp, headline, sentiment, relevance, event_type | `news_status = unavailable`; absence ≠ fake-neutral |
| `CrossAssetContext` | USDINR, US index, crude, gold, 10y yield | same |

Quality tiers: `HEALTHY / DEGRADED / THIN / STALE / UNAVAILABLE`. Every
optional source carries `source` + `data_quality`; synthetic fixtures always
carry `source="SYNTHETIC/TEST"`.

## 3. Multi-timeframe logic

`analyze_multi_timeframe(symbol, {"5m","15m","1h","1d"}, ctx)` computes
features, regime, evidence, confidence, horizon and expected move
**independently per timeframe** (insufficient bars ⇒ explicit NEUTRAL with the
reason recorded). `compute_timeframe_consensus` derives `short_term_bias`,
`short_term_alignment`, `swing_bias`, `higher_timeframe_conflict`,
`intraday_momentum`, `regime_agreement`, `volatility_agreement`,
`participating_timeframes`, `data_quality`, `notes` — weighting by timeframe
role (higher TF = trend, lower TF = momentum), never averaging confidences:
5m/15m/1h bearish + 1D bullish yields `short_term_bias=bearish`,
`higher_timeframe_conflict=True`.

## 4. Evidence Ledger V2

`EvidenceItem(category, signal, direction, strength, weight, source,
data_quality, timestamp, availability, explanation)` with
`effective_weight = weight × strength/100` for SUPPORTED items only.
Categories: trend, momentum, volume, volatility, structure, relative_strength,
breadth, india_vix, fii_dii, sector, options, news, cross_asset,
timeframe_alignment, regime_transition. Availability distinguishes
`SUPPORTED / CONTRADICTORY / UNAVAILABLE / INSUFFICIENT_DATA` — unavailable
data is recorded, never silently converted into neutral or negative evidence.

## 5. Confidence methodology

`compute_confidence_v2(ledger)` is deterministic: directional evidence is
aggregated via effective weights; CONTRADICTORY items reduce the score;
UNAVAILABLE/INSUFFICIENT items contribute nothing (missing data never mints
confidence). Output is 0–100 with low/medium/high bands and an explicit
NO-TRADE / insufficient-evidence outcome when supported evidence is too thin
or too conflicted. NEUTRAL remains a first-class bias. The LLM may narrate
the score but cannot change it.

## 6. Historical replay & no-lookahead

`replay_history(instrument, timeframe, df, start_idx=60, step=5)` walks the
historical frame; at each step it slices `df.iloc[:t]` (causal window) before
any feature computation, emits a `ReplayForecast`, and counts any slice that
extends past the forecast timestamp in `ReplayResult.lookahead_violations`
(must be 0). Future bars are used ONLY by `label_outcome` after the forecast
exists.

## 7. Outcome labeling methodology

`label_outcome(bias, entry, future_prices, lower_pct, upper_pct)`:
- LONG success = upper bound (+expected move) reached before the lower bound
  (invalidation); SHORT mirrored; simultaneous hits resolved by first touch.
- NEUTRAL success = price stays within ±0.5% over the horizon.
- Records realized_return, MFE/MAE, target_hit, invalidation_hit,
  horizon_completed, outcome_timestamp.
`ForecastStore.resolve_forecast(id, actual_return_pct)` persists the outcome
(hit = directional agreement; `within_expected_move` vs the estimated range).

## 8. Calibration & feature performance (research-only)

`compute_calibration([(confidence_0_100, hit), ...])` buckets 0-20…80-100 and
reports per-bucket win rate **without** calling it probability; below
`min_resolved` (default 100) it states "Insufficient sample size".
`analyze_feature_performance({category: [(hit, return_pct), ...]})` reports
per-category win rate / avg return, flagging `insufficient / provisional /
adequate` sample confidence. Neither layer changes production weights.

## 9. Synthetic fixture policy

`tests/fixtures/v3_fixtures.py` builds seeded, reproducible scenarios A–P
(bullish/bearish alignment, conflicting timeframes, high/low vol, sector RS,
strong/weak breadth, missing optional data, stale data, thin history,
liquid/illiquid/partial chains, evidence conflict, regime transition). Every
fixture is tagged `SYNTHETIC/TEST`; none may be used in a production/live
path, and none is labeled live anywhere.

## 10. Remaining limitations

- No live market/news/option-chain source (FYERS expired) — out of scope by design.
- Confidence is not yet calibrated (no outcome history at scale).
- Options candidates require a caller-supplied chain; without one the status is
  `unavailable_no_chain` and candidates are `[]`.

