"""V3 intelligence research demo.

SYNTHETIC / HISTORICAL RESEARCH DATA — NOT LIVE MARKET DATA.

Runs entirely offline: no broker API, no key, no live feed. Demonstrates the
V3 pipeline end-to-end on deterministic synthetic fixtures: contexts,
multi-timeframe consensus, regime transition, options analytics (liquid vs
illiquid vs partial chains), historical replay with no-lookahead checks,
forecast persistence, outcome labeling, calibration and feature performance.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from sqlalchemy import create_engine  # noqa: E402

from fixtures.v3_fixtures import (  # noqa: E402
    SYNTHETIC_TAG, bullish_aligned, bearish_aligned, conflicting_timeframes,
    regime_transition, high_volatility, low_volatility, strong_breadth,
    weak_breadth, calm_vix, high_vix, bullish_fii_dii, sector_outperforming,
    liquid_option_chain, illiquid_option_chain, partial_option_chain,
    news_context_bullish, cross_asset_risk_on, empty_market_context,
)
from trading_system.research.intelligence import (  # noqa: E402
    MarketIntelligenceEngine, FeatureEngine, classify_regime, SignalDirection,
    _build_instrument_context, analyze_multi_timeframe,
    generate_options_candidates,
)
from trading_system.research.intelligence_v3 import (  # noqa: E402
    compute_timeframe_consensus, detect_regime_transition,
    compute_options_analytics, build_evidence_ledger_v2, compute_confidence_v2,
    replay_history, label_outcome, compute_calibration,
    analyze_feature_performance,
)
from trading_system.research.forecast_ledger import ForecastStore  # noqa: E402

BAR = "=" * 76
print(BAR)
print("V3 INTELLIGENCE DEMO — SYNTHETIC / HISTORICAL RESEARCH DATA")
print("NOT LIVE MARKET DATA. No broker/API/network access is used.")
print(BAR)

eng = MarketIntelligenceEngine(lookback=60)
fe = FeatureEngine(lookback=60)

# [1] Instruments x scenarios -------------------------------------------------
print("\n[1] Instruments (synthetic scenarios A/B/D/E)")
scenarios = {
    "NSE:TCS-EQ (A bullish)": bullish_aligned(seed=11),
    "NSE:INFY-EQ (B bearish)": bearish_aligned(seed=12),
    "NSE:SBIN-EQ (D high-vol)": high_volatility(seed=14),
    "NSE:RELIANCE-EQ (E low-vol)": low_volatility(seed=15),
}
for name, df in scenarios.items():
    r = eng.analyze(name.split(" ")[0], "1d", df)
    c = r["signal_candidate"]
    print(f"  {name:<30} bias={c.direction.value:<8} "
          f"conf={c.confidence * 100:5.1f}/100 "
          f"horizon={c.horizon.value if c.horizon else '-'}")

# [2] Multi-timeframe consensus ------------------------------------------------
print("\n[2] Multi-timeframe consensus (scenario C: intraday bear / daily bull)")
mtf = analyze_multi_timeframe("NSE:NIFTY 50-INDEX",
                              conflicting_timeframes(),
                              _build_instrument_context("NSE:NIFTY 50-INDEX"))
for tf, ta in mtf.items():
    print(f"  {tf:>4}: {ta.bias.value:<8} {ta.confidence:5.1f}/100")
cons = compute_timeframe_consensus(mtf)
print(f"  consensus: short_term={cons.short_term_bias}/{cons.short_term_alignment}"
      f" swing={cons.swing_bias} htf_conflict={cons.higher_timeframe_conflict}"
      f" regime_agree={cons.regime_agreement}")

# [3] Contexts ------------------------------------------------------------------
print("\n[3] Optional contexts (all SYNTHETIC)")
b = strong_breadth()
print(f"  breadth: {b.advancing_count} adv / {b.declining_count} dec"
      f" [{b.data_quality.value}] (weak fixture: "
      f"{weak_breadth().advancing_count} adv)")
v = calm_vix()
print(f"  vix: {v.india_vix} p{v.vix_percentile:.0f} [calm] | "
      f"{high_vix().india_vix} [high]")
f = bullish_fii_dii()
print(f"  fii/dii: net {f.fii.buy - f.fii.sell:+.0f} / "
      f"{f.dii.buy - f.dii.sell:+.0f} cr")
s = sector_outperforming()
print(f"  sector: {s.sector_symbol} rs={s.relative_strength:+.1f}pp vs market")
n = news_context_bullish()
n_mean = sum(e.sentiment for e in n.events) / len(n.events)
print(f"  news: {len(n.events)} events, mean sentiment {n_mean:+.2f}"
      f" [{n.news_status}]")
ca = cross_asset_risk_on()
print(f"  cross-asset: usdinr {ca.usdinr}, us_idx {ca.us_index_change:+.2f}%,"
      f" crude {ca.crude_oil_change:+.2f}%")
empty = empty_market_context()
print(f"  empty context -> breadth={empty.breadth} vix={empty.vix}"
      f" (all optional sources UNAVAILABLE)")

# [4] Evidence ledger V2 / confidence V2 --------------------------------------
print("\n[4] Evidence ledger V2 / confidence V2 (synthetic uptrend)")
df_a = bullish_aligned(seed=11)
feats = fe.compute(df_a)
regime = classify_regime(feats)
led_plain = build_evidence_ledger_v2(feats, regime, SignalDirection.LONG)
led_full = build_evidence_ledger_v2(feats, regime, SignalDirection.LONG,
                                    breadth=strong_breadth(),
                                    sector=sector_outperforming())
cp, cp_lv = compute_confidence_v2(led_plain)
cf, cf_lv = compute_confidence_v2(led_full)
print(f"  no optional context : conf={cp:5.1f}/100 [{cp_lv}] items={len(led_plain.items)}")
print(f"  breadth+sector ctx  : conf={cf:5.1f}/100 [{cf_lv}] items={len(led_full.items)}")
for it in led_full.items:
    cat = str(it.category).split(".")[-1]
    strength = f"{it.strength:.0f}" if it.strength is not None else "-"
    print(f"    {cat:<20} {it.availability.name:<16} dir={str(it.direction):<8} "
          f"w={it.weight} strength={strength}")

# [5] Options analytics ---------------------------------------------------------
print("\n[5] Options analytics (SYNTHETIC chains; strikes FROM the chain)")
spot = 23950.0
for label, chain in [("liquid", liquid_option_chain()),
                     ("illiquid", illiquid_option_chain()),
                     ("partial", partial_option_chain())]:
    a = compute_options_analytics(chain[len(chain) // 2], spot,
                                  expected_move_pct=1.2)
    print(f"  {label:<9} strike={a.strike:.0f} liq={a.liquidity_score} "
          f"iv_suit={a.iv_suitability} data_sufficient={a.data_sufficient} "
          f"missing={a.missing_fields}")
cands = generate_options_candidates(feats, regime, SignalDirection.SHORT,
                                    spot, liquid_option_chain())
for c in cands[:2]:
    print(f"  candidate: {c.strike:.0f} {c.option_type} score={c.score:.0f}/100")
if not cands:
    print("  -> no attractive options setup (valid result)")

# [6] Regime transition ----------------------------------------------------------
print("\n[6] Regime transition (scenario P: trend -> range)")
tr_df = regime_transition()
reg_t = classify_regime(fe.compute(tr_df.iloc[:90]))
reg_f = classify_regime(fe.compute(tr_df))
tr = detect_regime_transition(reg_f, reg_t, fe.compute(tr_df))
print(f"  previous={reg_t.regime.value} current={reg_f.regime.value} "
      f"-> type={tr.transition_type} risk={tr.transition_risk}")

# [7] Historical replay ----------------------------------------------------------
print("\n[7] Historical replay (strict no-lookahead)")
store = ForecastStore(create_engine("sqlite://", future=True))
outcomes = []
for sym, dfx in [("NSE:SBIN-EQ", bullish_aligned(seed=11)),
                 ("NSE:RELIANCE-EQ", bearish_aligned(seed=12))]:
    res = replay_history(sym, "1d", dfx, start_idx=60, step=10)
    print(f"  {sym}: {len(res.forecasts)} forecasts, "
          f"lookahead_violations={res.lookahead_violations}")
    closes = dfx["close"]
    for fc in res.forecasts:
        i = closes.index.get_indexer([fc.timestamp])[0]
        if i < 0 or i + 10 >= len(closes):
            continue
        entry = float(closes.iloc[i])
        fut = [float(x) for x in closes.iloc[i + 1:i + 11]]
        lbl = label_outcome(fc.bias, entry, fut,
                            fc.expected_move_lower_pct,
                            fc.expected_move_upper_pct)
        rec = store.record_forecast(
            sym, "1d", fc.bias, fc.confidence, fc.horizon,
            expected_move_lower_pct=fc.expected_move_lower_pct,
            expected_move_upper_pct=fc.expected_move_upper_pct,
            invalidation=fc.invalidation,
            market_state={"source": SYNTHETIC_TAG}, created_at=fc.timestamp)
        store.resolve_forecast(rec.id, lbl.realized_return_pct)
        conf100 = fc.confidence * 100 if fc.confidence <= 1 else fc.confidence
        outcomes.append((conf100, lbl.outcome == "success"))

# [8] Calibration -------------------------------------------------------------------
print("\n[8] Forecast ledger + calibration (research-only)")
print(f"  persisted+resolved forecasts: {len(store.list_forecasts(resolved=True))}")
rep = compute_calibration(outcomes, min_resolved=100)
print(f"  resolved={rep.total_resolved} "
      f"sample_sufficient={rep.sample_sufficient}")
print(f"  note: {rep.note}")
for b in rep.buckets:
    if b.forecasts:
        wr = f"{b.win_rate:.0%}" if b.win_rate is not None else "-"
        print(f"  bucket {b.bucket_range:>7}: n={b.forecasts:<3} win={wr}")

# [9] Feature performance -------------------------------------------------------------
print("\n[9] Feature performance (tiny sample flagged insufficient)")
cats: dict[str, list[tuple[bool, float]]] = {}
for sym, dfx in [("NSE:SBIN-EQ", bullish_aligned(seed=11)),
                 ("NSE:RELIANCE-EQ", bearish_aligned(seed=12))]:
    closes = dfx["close"]
    for start in range(60, len(closes) - 12, 12):
        r = eng.analyze(sym, "1d", dfx.iloc[:start])
        if r["status"] != "OK":
            continue
        c = r["signal_candidate"]
        fut = [float(x) for x in closes.iloc[start + 1:start + 11]]
        if len(fut) < 5:
            continue
        bias = ("bullish" if c.direction.value == "long"
                else "bearish" if c.direction.value == "short" else "neutral")
        em = c.expected_move
        lbl = label_outcome(bias, float(closes.iloc[start]), fut,
                            em.lower_pct if em else -1.0,
                            em.upper_pct if em else 1.0)
        hit, ret = lbl.outcome == "success", lbl.realized_return_pct
        led = build_evidence_ledger_v2(r["features"], r["regime"], c.direction)
        for it in led.items:
            if it.availability.name == "SUPPORTED":
                cats.setdefault(str(it.category).split(".")[-1],
                                []).append((hit, ret))
for p in analyze_feature_performance(cats, min_samples=30):
    if p.forecast_count:
        wr = f"{p.win_rate:.0%}" if p.win_rate is not None else "-"
        print(f"  {p.category:<22} n={p.forecast_count:<4} win={wr} "
              f"sample={p.sample_confidence}")

print("\n" + BAR)
print("Done. All data above is SYNTHETIC / HISTORICAL RESEARCH DATA — NOT LIVE.")

