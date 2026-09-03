"""Intelligence upgrade demonstration.

Runs the upgraded engine against NIFTY 50, BANK NIFTY, SBIN, RELIANCE, TCS, INFY
using DETERMINISTIC SYNTHETIC OHLCV (live FYERS token is expired -- no live data
is available and none is fabricated; this demonstrates the ENGINE's
discrimination, not a market call). Options candidates use a SYNTHETIC chain
shaped like live NSE chains -- also clearly labeled.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd

sys.path.insert(0, "src")

from trading_system.research.intelligence import (
    MarketIntelligenceEngine, _build_instrument_context,
    analyze_multi_timeframe, generate_options_candidates, SignalDirection,
)


def _ohlc(n, start, drift, vol, seed, base_vol_share=1.0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC")
    closes = start + np.cumsum(rng.normal(drift, vol, n))
    closes = np.maximum(closes, 1.0)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    # Index moves are less volatile than single stocks; scale bars accordingly.
    vol_px = max(0.05, start * 0.004 * base_vol_share)
    vols = rng.integers(1_000_000, 5_000_000, n).astype(float)
    return pd.DataFrame({"open": opens, "high": np.maximum(closes, opens) + vol_px,
                         "low": np.minimum(closes, opens) - vol_px,
                         "close": closes, "volume": vols}, index=idx)


def _range_series(n, base, amp, period, seed=5):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC")
    closes = base + amp * np.sin(np.arange(n) * 2 * np.pi / period) + rng.normal(0, amp * 0.15, n)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    vols = rng.integers(1_000_000, 5_000_000, n).astype(float)
    return pd.DataFrame({"open": opens, "high": np.maximum(closes, opens) + amp * 0.2,
                         "low": np.minimum(closes, opens) - amp * 0.2,
                         "close": closes, "volume": vols}, index=idx)


def _synthetic_chain(spot, opt, strikes, seed=7):
    """SYNTHETIC chain shaped like a live NSE option chain (labeled as such)."""
    rng = np.random.default_rng(seed)
    rows = []
    for k in strikes:
        dist = abs(k - spot) / spot
        d = max(0.05, 0.55 - dist * 12) * (1 if opt == "CE" else -1)
        liq = 1.0 if dist <= 0.03 else max(0.05, 1.0 - dist * 10)
        bid = max(2.0, spot * 0.012 * (1 - dist * 8)) * liq
        rows.append({
            "strike": float(k), "option_type": opt, "expiry": "2026-09-10",
            "delta": round(d, 2), "theta": round(-0.015 - dist * 0.2, 3),
            "implied_vol": round(0.16 + dist * 1.2, 3),
            "open_interest": int(rng.integers(50_000, 500_000) * liq),
            "volume": int(rng.integers(500, 200_000) * liq),
            "bid": round(bid, 1), "ask": round(bid * (1 + 0.012 + 0.02 * (1 - liq)), 1),
        })
    return rows


eng = MarketIntelligenceEngine(lookback=60)

print("=" * 78)
print("INTELLIGENCE UPGRADE -- INSTRUMENT SWEEP (synthetic inputs, deterministic)")
print("=" * 78)
specs = [
    ("NSE:NIFTY 50-INDEX",   _ohlc(120, 24000,  12,  90, seed=11, base_vol_share=0.5)),
    ("NSE:NIFTY 50-INDEX-2", _range_series(120, 24100, 60, 9)),
    ("NSE:NIFTYBANK-INDEX",  _ohlc(120, 52000, -60, 260, seed=13, base_vol_share=0.8)),
    ("NSE:SBIN-EQ",          _ohlc(120,  620, 2.2,   6, seed=17)),
    ("NSE:RELIANCE-EQ",      _ohlc(120, 2950, -8,   22, seed=19)),
    ("NSE:TCS-EQ",           _range_series(120, 4100, 35, 8)),
    ("NSE:INFY-EQ",          _ohlc(120, 1580, 4.0,  14, seed=23)),
]
results = {}
for sym, df in specs:
    r = eng.analyze(sym, "1d", df)
    c = r["signal_candidate"]
    results[sym] = r
    em = c.expected_move
    ctx = r["instrument_context"]
    ledger = r["explanation"]
    print(f"\n{sym}")
    print(f"  instrument_class={ctx.instrument_class.value}  is_nifty={ctx.is_nifty}  "
          f"is_bank_nifty={ctx.is_bank_nifty}  vol_band=({ctx.low_vol_threshold}-{ctx.high_vol_threshold})")
    print(f"  bias={c.direction.value.upper():<8} confidence={c.confidence * 100:.0f}/100  "
          f"horizon={c.horizon.value if c.horizon else '-'}")
    if em:
        print(f"  expected range: {em.lower_pct:+.2f}% to {em.upper_pct:+.2f}%  (basis={em.basis})")
    print(f"  invalidation: {c.invalidation_context}")
    print(f"  regime={r['regime'].regime.value}  evidence+: {len(ledger.bullish_factors)}  "
          f"evidence-: {len(ledger.bearish_factors)}  missing: {len(ledger.missing_data)}")
    print(f"  top evidence: {(ledger.bullish_factors or ledger.bearish_factors or ['(neutral)'])[0]}")


print("\n" + "=" * 78)
print("MULTI-TIMEFRAME -- NSE:NIFTY 50-INDEX (bearish 5m/15m, range 1h, bullish 1d)")
print("=" * 78)
dfs = {
    "5m": _ohlc(80, 24050, -1.5, 12, seed=31),
    "15m": _ohlc(80, 24060, -1.0, 14, seed=32),
    "1h": _range_series(80, 24100, 40, 10),
    "1d": _ohlc(120, 23800, 12, 90, seed=11, base_vol_share=0.5),
}
mtf = analyze_multi_timeframe("NSE:NIFTY 50-INDEX", dfs, _build_instrument_context("NSE:NIFTY 50-INDEX"))
for tf, ta in mtf.items():
    print(f"  {tf:>4}: {ta.bias.value.upper():<8} {ta.confidence:.0f}/100   "
          f"(+{len(ta.evidence.positive)}/-{len(ta.evidence.negative)} evidence)")

print("\n" + "=" * 78)
print("OPTIONS INTELLIGENCE -- SYNTHETIC CHAINS (labeled; strikes FROM the chain)")
print("=" * 78)
for sym, spot, opt, bias in [("NIFTY", 23950.0, "PE", SignalDirection.SHORT),
                             ("BANKNIFTY", 51900.0, "CE", SignalDirection.LONG)]:
    strikes = sorted({round((spot + off) / 100) * 100 for off in
                      (-900, -600, -300, -100, 100, 300, 600, 900)})
    chain = _synthetic_chain(spot, opt, strikes)
    cands = generate_options_candidates(None, None, bias, spot, chain)
    print(f"\n{sym}  spot={spot:.0f}  bias={bias.value.upper()}  (SYNTHETIC chain, {len(chain)} rows)")
    for c in cands[:3]:
        spread = f"spread={c.bid_ask_spread:.1f}%" if c.bid_ask_spread is not None else "spread=n/a"
        print(f"  {sym} {c.strike:.0f} {c.option_type}  score={c.score:.0f}/100  "
              f"delta={c.delta}  iv={c.implied_vol}  {spread}")
        for rsn in c.rationale[:2]:
            print(f"      + {rsn}")
        for rsk in c.risks[:2]:
            print(f"      ! {rsk}")
    if not cands:
        print("  -> no attractive options setup (valid result)")

print("\n" + "=" * 78)
print("STALE / INSUFFICIENT DATA -- confidence downgrade (no fabrication)")
print("=" * 78)
r_ok = eng.analyze("NSE:SBIN-EQ", "1d", _ohlc(120, 620, 2.2, 6, seed=17))
r_thin = eng.analyze("NSE:SBIN-EQ", "1d", _ohlc(35, 620, 2.2, 6, seed=17))
print(f"  full history : conf={r_ok['signal_candidate'].confidence * 100:.0f}/100  "
      f"rows={r_ok['data_quality']['rows']}")
print(f"  thin history : conf={r_thin['signal_candidate'].confidence * 100:.0f}/100  "
      f"rows={r_thin['data_quality']['rows']}  insufficient={r_thin['data_quality']['insufficient']}")
r_blk = eng.analyze("NSE:SBIN-EQ", "1d", None, health_status="STALE")
print(f"  stale feed   : status={r_blk['status']} reason={r_blk.get('reason')}")

pairs = {
    "NSE:NIFTY 50-INDEX vs NSE:SBIN-EQ": ("NSE:NIFTY 50-INDEX", "NSE:SBIN-EQ"),
    "NSE:NIFTY 50-INDEX vs NSE:NIFTYBANK-INDEX": ("NSE:NIFTY 50-INDEX", "NSE:NIFTYBANK-INDEX"),
}
print("\n" + "-" * 78)
print("DISCRIMINATION CHECKS")
print("-" * 78)
for label, (a, b) in pairs.items():
    ca, cb = results[a]["signal_candidate"], results[b]["signal_candidate"]
    print(f"{label}:")
    print(f"  bias_a={ca.direction.value} conf_a={ca.confidence * 100:.0f} | "
          f"bias_b={cb.direction.value} conf_b={cb.confidence * 100:.0f}  -> "
          f"{'DISTINCT' if (ca.direction, ca.confidence) != (cb.direction, cb.confidence) else 'IDENTICAL'}")

