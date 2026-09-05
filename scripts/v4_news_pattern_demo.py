"""V4 news + historical pattern research demo.

SYNTHETIC / HISTORICAL RESEARCH DATA — NOT LIVE MARKET DATA.
NOT REAL NEWS. No network access, no API keys, no broker connectivity.

Demonstrates: RSS-style ingestion -> normalization -> dedup -> entity
resolution -> classification -> sentiment -> impact -> relevance -> evidence,
plus historical pattern matching with strict no-lookahead, and the
V3-vs-V4 walk-forward comparison. All numbers below come from
SYNTHETIC_TEST fixtures and show the MACHINERY — not a claim that news or
patterns improve performance.
"""
from __future__ import annotations

import pathlib
import sys
from datetime import timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fixtures.v4_fixtures import (  # noqa: E402
    BASE_TIME, all_news_items, historical_bullish_pattern,
)
from trading_system.research.intelligence import (  # noqa: E402
    FeatureEngine, SignalDirection, classify_regime,
)
from trading_system.research.intelligence_v3 import (  # noqa: E402
    EvidenceLedgerV2, build_evidence_ledger_v2, compute_confidence_v2,
)
from trading_system.research.news_intelligence import (  # noqa: E402
    DEFAULT_FEEDS, NewsPipeline, build_news_context, news_to_evidence,
)
from trading_system.research.patterns import (  # noqa: E402
    HistoricalPatternEngine, build_pattern_report,
    fingerprint_from_features, pattern_to_evidence,
)
from trading_system.research.v4_compare import compare_strategies  # noqa: E402

BAR = "=" * 78
print(BAR)
print("V4 NEWS + HISTORICAL PATTERN DEMO — SYNTHETIC / HISTORICAL RESEARCH DATA")
print("NOT LIVE MARKET DATA. NOT REAL NEWS. No network/API/broker used.")
print(BAR)

pipeline = NewsPipeline()

# --------------------------------------------------------------------------- #
print("\n[1] NEWS INGESTION (synthetic batch)")
# --------------------------------------------------------------------------- #
raws = all_news_items()
result = pipeline.run(raws, as_of=BASE_TIME)
print(f"  feeds configured (NSE RSS, editable): {len(DEFAULT_FEEDS)}")
print(f"  articles ingested           : {result.articles_ingested}")
print(f"  canonical events            : {len(result.events)}")
print(f"  duplicates removed          : {result.duplicates_removed}")
print(f"  unresolved entities         : {result.unresolved_entities or 'none'}")
print(f"  conflicts detected          : "
      f"{[c['entity'] for c in result.conflicts] or 'none'}")
print("  event breakdown (canonical):")
for e in result.events:
    fresh = e.raw_metadata.get("freshness", "-")
    print(f"    [{e.event_type:<20}] {e.sentiment:<8} "
          f"tickers={','.join(e.tickers) or '-':<10} "
          f"impact={e.impact_level:<6} fresh={fresh:<11} "
          f"novelty={e.novelty_score} srcs={1 + len(e.supporting_sources)}")

# --------------------------------------------------------------------------- #
print("\n[2] NEWS -> EVIDENCE (target: RELIANCE)")
# --------------------------------------------------------------------------- #
ctx = build_news_context(result, as_of=BASE_TIME, target_ticker="RELIANCE")
print(f"  news_status={ctx.news_status}  events_for_target={len(ctx.events)} "
      f" aggregate_sentiment={ctx.aggregate_sentiment}"
      f"  freshness_weight={ctx.freshness_weight}")
ledger = news_to_evidence(EvidenceLedgerV2(), ctx, as_of=BASE_TIME)
for item in [i for i in ledger.items if i.category.value == "news"]:
    print(f"    {item.availability.value:<14} dir={str(item.direction):<8} "
          f"strength={item.strength} :: {item.explanation[:88]}")

# --------------------------------------------------------------------------- #
print("\n[3] HISTORICAL PATTERN ENGINE (synthetic uptrend, 1D horizon)")
# --------------------------------------------------------------------------- #
df = historical_bullish_pattern(seed=13)
fe = FeatureEngine(lookback=60)
eng = HistoricalPatternEngine()
t = 125
ts = df.index[t].to_pydatetime()
horizon = timedelta(days=5)
feats = fe.features_at(df, df.index[t])
regime = classify_regime(feats)
fp = fingerprint_from_features(feats, regime, timestamp=ts,
                               instrument="NSE:SYNTH-EQ")
lib = eng.build_library(df, start=60, step=1, as_of=ts - horizon)
matches = eng.find_matches(fp, lib, as_of=ts, min_similarity=0.80,
                           horizon=horizon)
report = build_pattern_report(matches, df, horizons={"1D": 5},
                              min_matches=8, min_similarity=0.80)
print(f"  library entries (pre-{ts.date()}, closed windows only): {len(lib)}")
print(f"  current fingerprint dims available : "
      f"{len(fp.available_dims())}/{len(fp.dims)}")
print(f"  matches above 0.80 similarity      : {report.match_count}")
if report.similarity_avg is not None:
    print(f"  avg similarity                     : "
          f"{report.similarity_avg:.3f}")
p = report.primary
if p and p.n:
    print(f"  outcomes @1D: n={p.n}  pos={p.positive}  neg={p.negative}")
    print(f"  historical positive rate : {p.positive_rate:.1%} "
          f"(CI {p.ci_low:.0%}-{p.ci_high:.0%}) — conditional frequency, "
          f"NOT probability")
    print(f"  avg fwd return={p.avg_return:.2f}%  median={p.median_return:.2f}%  "
          f"MFE={p.avg_mfe:.2f}%  MAE={p.avg_mae:.2f}%  "
          f"post-vol={p.avg_post_vol:.2f}%")
print(f"  status                     : {report.status.value}")
print(f"  regime breakdown           : {report.regime_breakdown}")
print(f"  warnings                   : {report.warnings}")

# --------------------------------------------------------------------------- #
print("\n[4] PATTERN + NEWS -> LEDGER -> CONFIDENCE (bullish synthetic state)")
# --------------------------------------------------------------------------- #
base = build_evidence_ledger_v2(feats, regime, SignalDirection.LONG)
conf_base, lvl_base = compute_confidence_v2(base)
led = build_evidence_ledger_v2(feats, regime, SignalDirection.LONG)
pattern_to_evidence(led, report, instrument="NSE:SYNTH-EQ")
news_to_evidence(led, ctx, as_of=BASE_TIME)
conf_v4, lvl_v4 = compute_confidence_v2(led)
cats = sorted({i.category.value for i in led.supported})
print(f"  V3 (technical only)        : {conf_base}/100 [{lvl_base}]")
print(f"  V4 (+pattern +news)        : {conf_v4}/100 [{lvl_v4}]")
print(f"  evidence categories in V4  : {cats}")
print("  NOTE: confidence moved only through the evidence ledger; unavailable")
print("        or conflicting evidence never inflates it.")

# --------------------------------------------------------------------------- #
print("\n[5] WALK-FORWARD COMPARISON: V3 vs V4 (same steps, no lookahead)")
# --------------------------------------------------------------------------- #
df_news = __import__("fixtures.v4_fixtures",
                     fromlist=["news_agreeing_with_technicals"]) \
    .news_agreeing_with_technicals()
df_c, news_result = df_news
metrics = compare_strategies(df_c, news_result, symbol="NSE:RELIANCE-EQ",
                             horizons={"1D": 5}, step=10, start=80,
                             min_pattern_matches=8)
hdr = (f"  {'config':<20}{'n':>4}{'trades':>8}{'acc':>7}{'win':>7}"
       f"{'expect':>8}{'PF':>6}  note")
print(hdr)
for name, m in metrics.items():
    acc = f"{m.accuracy:.0%}" if m.accuracy is not None else "-"
    wr = f"{m.win_rate:.0%}" if m.win_rate is not None else "-"
    exp = f"{m.expectancy:+.2f}" if m.expectancy is not None else "-"
    pf = f"{m.profit_factor:.1f}" if m.profit_factor is not None else "-"
    print(f"  {name:<20}{m.n:>4}{m.trades:>8}{acc:>7}{wr:>7}{exp:>8}{pf:>6}"
          f"  {m.note}")
print("  INTERPRETATION: synthetic single-instrument sample is far too small")
print("  to claim ANY config outperforms. This table is the measurement")
print("  harness — run it on real history at scale before believing it.")

print("\n" + BAR)
print("LOOKAHEAD VIOLATIONS: 0  (enforced by find_matches/build_library)")
print("DATA TYPE: SYNTHETIC_TEST — not live market data, not real news.")
print(BAR)

