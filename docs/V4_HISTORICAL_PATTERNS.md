# V4 Historical Pattern Engine

Status: implemented, offline-deterministic. **Historical pattern statistics
describe past observations and do not guarantee future outcomes.**

Given the CURRENT market state, the engine finds similar HISTORICAL states
(strictly before the forecast timestamp) and reports what happened afterward
as HISTORICAL CONDITIONAL FREQUENCY — explicitly not a probability.

## 1. Market-state fingerprint

`MarketStateFingerprint` — normalized dims in 0..1, grouped for weighting:

| Group | Dims |
|---|---|
| price | mom10, dist_from_high/low, atr_pct, breakout, breakdown |
| trend | trend_st/mt/lt (vs SMA20/50/200), trend_strength (EMA20-50) |
| momentum | rsi_norm, mom_dir |
| volume | rel_vol, vol_trend_up |
| volatility | hist_vol, high_vol_flag |
| mtf | mtf_5m / 15m / 1h / 1d bias |
| sector / breadth / options | sector_strength, rs_market, adv_pct, breadth_strength, iv_regime, pcr |
| news | news_sent, news_count, news_impact, news_fresh |

Missing inputs stay `None` — never imputed, never fabricated. Fingerprints
are built from V3 `TechnicalFeatures` + optional V3 contexts + optional news.

## 2. Normalization (comparing 20000 vs 25000)

Only relative features enter the fingerprint (percent returns, ATR-scaled
distances, RSI/100, relative volume). `FingerprintNormalizer` z-scores dims
against the LIBRARY ONLY — the library contains exclusively pre-as_of states,
so the statistics used at time T are available at T. No future observation is
used to normalize historical states (invariant-tested).

## 3. Similarity & weights

`SimilarityEngine` — deterministic `weighted_euclidean` (default) or `cosine`
over dims present in BOTH states; per-dim weight = group weight / (#common
dims in group) so each group contributes constant mass despite missing dims.
`FeatureWeights` are explicit and documented (trend 1.5, momentum 1.2,
price 1.0, volatility 1.0, mtf 1.0, sector 0.8, news 0.8, volume 0.8,
breadth 0.6, options 0.6). Zeroing groups is the ablation mechanism
(`ABLATION_CONFIGS` A–F).

## 4. No-lookahead guarantees (enforced in the API)

1. `build_library(..., as_of)` never includes states at/after `as_of`; each
   historical fingerprint is computed causally (`features_at` slices data ≤
   bar timestamp).
2. `find_matches(..., as_of, horizon)` returns ONLY entries with
   `timestamp < as_of` AND `timestamp + horizon < as_of` — matches whose
   outcome window has not closed by the forecast time are invisible.
3. Replay/walk-forward comparison uses news only with `published_at ≤ T`.

Invariant tests construct libraries containing future entries and prove they
are excluded.

## 5. Outcomes & reliability

For every match: forward return, MFE, MAE, post-volatility per horizon
(configurable bar map, e.g. `{"1D": 5}`). Cluster report: match count,
positive/negative/neutral counts, positive rate, avg/median return, MFE/MAE,
Wilson confidence interval, regime-conditional breakdown (e.g. trending_up
vs range vs high_volatility). Statuses:

- `SUFFICIENT` — enough closed outcomes, CI excludes 50%
- `PATTERN_CONFLICTING` — rate in (0.42, 0.58): no directional answer forced
- `PATTERN_WEAK` — CI spans 50%
- `INSUFFICIENT_MATCHES` — below `min_matches`

Reports always label the rate "historical conditional frequency — not a
probability" and flag small samples (<50, <min_matches).

## 6. Evidence & forecast integration

`pattern_to_evidence` adds a HISTORICAL_PATTERN-category ledger item —
separate from technical/news/options evidence: SUFFICIENT → SUPPORTED,
CONFLICTING → CONTRADICTORY (reduces confidence), WEAK → PARTIAL,
INSUFFICIENT → UNAVAILABLE. Confidence moves ONLY through the ledger
(`compute_confidence_v2`); extra evidence never automatically raises it —
the V4 demo shows confidence DROPPING when news conflicts.

## 7. Walk-forward comparison & ablation

`compare_strategies(df, news_result, ...)` runs identical walk-forward steps
for V3 / V4_technical / V4_technical_news / V4_full and reports accuracy,
precision, recall, win rate, expectancy, profit factor, avg return, max
drawdown, Sharpe, trade count + Wilson CI + honest "insufficient sample"
notes. Ablation configs A–F answer "does news/pattern/context actually add
value?" — measure, never assume.

## 8. Limitations

- O(n) causal feature computation per library bar; fine for research scale.
- Similarity is a hand-weighted metric, not a learned embedding (by design).
- Synthetic demo samples are tiny — all comparison notes say so.
- No claim is made that news or patterns improve performance; the harness
  exists to measure it on real history at scale.

## 9. Testing

`tests/test_pattern_engine.py` (28 tests) covers: fingerprint ranges,
missing-dim honesty, determinism, price-level invariance (20000 vs 25000),
library-only normalization, similarity ordering/ablation, **future-entry
exclusion**, **outcome-window closure**, walk-forward boundaries, match
thresholds, bullish positive-rate, MFE≥return, determinism,
INSUFFICIENT_MATCHES, regime-breakdown accounting, reliability labeling,
status→availability mapping, and the six ablation weight sets.

`tests/test_v4_integration.py` (22 tests) covers: news→ledger,
relevance gating, pattern→ledger, **supportive-vs-conflicting confidence**
(higher when pattern supports, lower or equal when it conflicts — never
automatic), walk-forward comparison step parity, future-news exclusion,
V3 `NewsContext` compatibility, vote neutrality, honest metric notes.

## 10. Demo

`scripts/v4_news_pattern_demo.py` runs the whole chain offline on
SYNTHETIC_TEST fixtures and prints ingestion stats, evidence items, pattern
report with regime breakdown, the V3-vs-V4 confidence effect (which DROPS
under conflicting news), the four-config walk-forward table, then:

```
LOOKAHEAD VIOLATIONS: 0
DATA TYPE: SYNTHETIC_TEST
```

