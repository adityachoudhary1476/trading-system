"""Final demonstration: run the upgraded intelligence across instruments.

Data is SYNTHETIC (FYERS token expired per PROJECT_AUDIT.md) — this
demonstrates that the ENGINE discriminates between different market
inputs; it is not a claim about live market prices.
"""
import sys
sys.path.insert(0, "src")

import numpy as np
import pandas as pd

from trading_system.research.intelligence import (
    MarketIntelligenceEngine,
    _build_instrument_context,
    generate_options_candidates,
    SignalDirection,
)

def ohlc(n, start, drift, vol, seed, vol_spike=False):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-08-03", periods=n, freq="h", tz="UTC")
    closes = start * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    closes = np.maximum(closes, 1.0)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    spread = np.abs(rng.normal(0, vol * start * 0.5, n))
    if vol_spike:
        spread[-5:] *= 4.0
    highs = np.maximum(closes, opens) + spread
    lows = np.minimum(closes, opens) - spread
    lows = np.maximum(lows, 0.5)
    base = rng.integers(2_000_000, 6_000_000, n).astype(float)
    if drift > 0.0005:
        base[-3:] *= 1.8  # volume confirmation on trend
    return pd.DataFrame({"open": opens, "high": highs, "low": lows,
                         "close": closes, "volume": base}, index=idx)

INSTRUMENTS = {
    #                  start   drift     vol    seed  vol_spike
    "NIFTY 50":     (24800,  0.0006, 0.0012,  101, False),
    "BANK NIFTY":   (54200, -0.0011, 0.0022,  202, True),
    "SBIN":         (  845,  0.0021, 0.0028,  303, False),
    "RELIANCE":     ( 2935, -0.0008, 0.0019,  404, False),
    "TCS":          ( 4120,  0.0002, 0.0011,  505, False),
    "INFY":         ( 1568, -0.0016, 0.0024,  606, False),
}

def synthetic_chain(spot, opt):
    rows = []
    for i, k in enumerate([spot * 0.99, spot * 0.995, spot, spot * 1.005, spot * 1.01]):
        rows.append({
            "strike": round(k / 50) * 50 if spot < 10000 else round(k / 100) * 100,
            "option_type": opt, "expiry": "2026-09-10",
            "delta": round(0.55 - i * 0.12, 2) if opt == "CE" else round(-0.55 + i * 0.12, 2),
            "gamma": 0.0006, "theta": -0.015, "vega": 0.11,
            "implied_vol": 0.21 if opt == "CE" else 0.24,
            "open_interest": 185_000 - i * 25_000,
            "volume": 42_000 - i * 6_000,
            "bid": 120 - i * 25, "ask": 121.5 - i * 25,
        })
    return rows

eng = MarketIntelligenceEngine(lookback=60)
conf_values = {}
print("=" * 78)
print("FINOVA MARKETS — UPGRADED INTELLIGENCE OUTPUTS (synthetic demo data)")
print("=" * 78)
for name, (start, drift, vol, seed, spike) in INSTRUMENTS.items():
    df = ohlc(160, start, drift, vol, seed, spike)
    kw = {}
    if name in ("NIFTY 50", "BANK NIFTY"):
        # Simulated LIVE chain for the options-enabled indices only.
        kw["option_chain"] = (synthetic_chain(start, "CE") + synthetic_chain(start, "PE"))
    r = eng.analyze(f"NSE:{name.replace(' ', '')}", "1h", df, **kw)
    c = r["signal_candidate"]
    em, hz = c.expected_move, c.horizon
    conf_values[name] = round(c.confidence * 100, 1)
    print(f"\n{name}  [{r['instrument_context'].instrument_class.value}]  "
          f"(ctx: nifty={r['instrument_context'].is_nifty}, bank={r['instrument_context'].is_bank_nifty})")
    print(f"  Bias:        {c.direction.value.upper()}")
    print(f"  Setup:       {c.setup.value}")
    print(f"  Confidence:  {c.confidence * 100:.0f}/100 (model confidence, NOT probability)")
    print(f"  Horizon:     {hz.value}")
    if em:
        print(f"  Est. range:  {em.lower_pct:+.2f}% to {em.upper_pct:+.2f}%  (basis: {em.basis})")
    print(f"  Invalidation:{' ' + c.invalidation_context}")
    print(f"  Evidence:    {'; '.join(c.supporting_features[:3]) or '(none)'}")
    print(f"  Risk flags:  {'; '.join(c.risk_flags) or '(none)'}")
    print(f"  Options:     {r['options_status']}")

    if name in ("NIFTY 50", "BANK NIFTY"):
        for j, cand in enumerate(r["options_candidates"][:2]):
            print(f"    {'Preferred' if j == 0 else 'Alternative'}: {name} {cand.strike:.0f} {cand.option_type}"
                  f"  score {cand.score:.0f}/100  delta={cand.delta}  iv={cand.implied_vol}"
                  f"  oi={cand.open_interest:.0f}")
            for line in cand.rationale[:2]:
                print(f"      - {line}")

print("\n" + "=" * 78)
print("CONFIDENCE DISTRIBUTION ACROSS INSTRUMENTS")
print("=" * 78)
vals = list(conf_values.values())
for name, v in conf_values.items():
    print(f"  {name:<12} {v:>5}")
print(f"\n  distinct values: {len(set(vals))}/{len(vals)}")
print(f"  min={min(vals)}  max={max(vals)}  mean={sum(vals)/len(vals):.1f}")
near70 = sum(1 for v in vals if 65 <= v <= 75)
print(f"  values in the 65-75 cluster: {near70}/{len(vals)}  -> "
      f"{'STILL CLUSTERED AT 70 — FAIL' if near70 == len(vals) else 'no longer clustered at 70 — PASS'}")
