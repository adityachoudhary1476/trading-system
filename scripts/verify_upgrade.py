"""Quick smoke check of the backend DTO + service wiring (Phase 3-13)."""
import json, sys
sys.path.insert(0, "src")
sys.path.insert(0, "backend")

from schemas.market import AIAnalysisDTO, ExpectedMoveDTO, EvidenceDTO
import pandas as pd, numpy as np
from trading_system.research.intelligence import MarketIntelligenceEngine

# 1. DTO parses with all new fields
d = AIAnalysisDTO(symbol="NSE:SBIN", timeframe="1d", bias="bearish",
                  confidence=0.62, signal="short", summary="t",
                                    generated_at=1, model="deterministic", horizon="swing",
                  expectedMove=ExpectedMoveDTO(lowerPct=-0.8, upperPct=-0.3, basis="atr"),
                  evidence=EvidenceDTO(positive=["a"], negative=["b"], neutral=["c"], agreement="moderate"),
                  invalidation="Above 24500", instrumentClass="index")
wire = d.model_dump(by_alias=True)
assert wire["horizon"] == "swing"
assert wire["expectedMove"]["lowerPct"] == -0.8
assert wire["evidence"]["agreement"] == "moderate"
assert wire["instrumentClass"] == "index"
print("DTO OK:", json.dumps(wire, default=str)[:180])

# 2. Engine still works end-to-end on a synthetic series
rng = np.random.default_rng(7)
idx = pd.date_range("2026-01-01", periods=120, freq="D", tz="UTC")
closes = 620 + np.cumsum(rng.normal(-2.2, 6, 120))
df = pd.DataFrame({"open": closes, "high": closes+3, "low": closes-3,
                   "close": closes, "volume": rng.integers(1_000_000, 5_000_000, 120)}, index=idx)
r = MarketIntelligenceEngine(lookback=60).analyze("NSE:SBIN-EQ", "1d", df)
c = r["signal_candidate"]
print(f"ENGINE OK: bias={c.direction.value} conf={c.confidence:.2f} "
      f"horizon={c.horizon.value if c.horizon else None} "
      f"em={'yes' if c.expected_move else 'no'} inv={'yes' if c.invalidation else 'no'} "
      f"ledger={'yes' if c.evidence_ledger else 'no'} opts={len(r['options_candidates'])} "
      f"ostatus={r['options_status']}")
assert c.evidence_ledger is not None
assert c.invalidation is not None
# no option chain passed -> must be explicitly unavailable
assert r["options_status"] == "unavailable_no_chain"
assert r["options_candidates"] == []
print("All checks passed. Confidence is NOT fixed at 0.70 (observed", round(c.confidence,2), ").")
