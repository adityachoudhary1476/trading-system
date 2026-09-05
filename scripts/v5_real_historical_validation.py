"""V5 real-historical validation demo.

SYNTHETIC / TEST DATA — NOT REAL MARKET DATA.

No real historical dataset is present in the repository (no broker/API, no
paid data). This script exercises the ENTIRE V5 pipeline on clearly-labeled
SYNTHETIC_TEST fixtures — provenance, validation, causal snapshots, V2/V3/V4
replay, metrics, calibration, costs, slippage, no-lookahead audit, run
registry, and the deterministic verdict — then states the honest outcome:

    REAL_HISTORICAL_DATA_NOT_PRESENT
    REAL_HISTORICAL_VALIDATION_PENDING
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import pandas as pd  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from fixtures.v4_fixtures import historical_bullish_pattern  # noqa: E402
from trading_system.research.historical_data import (  # noqa: E402
    DatasetType, HistoricalProvenance, LocalFileAdapter, utc_now_str,
    validate_ohlcv,
)
from trading_system.research.run_registry import ResearchRunRegistry  # noqa: E402
from trading_system.research.v5_validation import (  # noqa: E402
    CostAssumptions, VerdictInput, classify_verdict, compute_metrics,
    confidence_calibration, edge_survives_slippage, improvement_test,
    replay_v5_dataset, slippage_sensitivity,
)

BAR = "=" * 78
print(BAR)
print("V5 REAL-HISTORICAL VALIDATION DEMO — SYNTHETIC / TEST DATA")
print("NOT REAL MARKET DATA. No network/broker/API used.")
print(BAR)

# [1] Dataset + provenance ----------------------------------------------------
df = historical_bullish_pattern(seed=13)
prov = HistoricalProvenance(
    dataset_id="SYNTH-DEMO-1", source=DatasetType.SYNTHETIC_TEST.value,
    imported_at=utc_now_str(),
    instruments=["NSE:SYNTH-EQ"], timeframes=["1d"], timezone="UTC",
    dataset_type=DatasetType.SYNTHETIC_TEST)
print("\n[1] DATASET PROVENANCE")
print(f"  dataset_type  : {prov.dataset_type.value}")
print(f"  known_real    : {prov.known_real()}  (FALSE — synthetic)")
print(f"  dataset_hash  : {prov.sha256()}")
rep = validate_ohlcv(df)
print(f"  validation    : valid={rep.valid} rows={rep.rows} "
      f"dups={rep.duplicates} gaps={rep.gaps}")

# [2] CSV round-trip (proves the local adapter) --------------------------------
print("\n[2] LOCAL FILE ADAPTER (CSV round-trip)")
tmp = ROOT / "scripts" / "_v5_demo.csv"
out = df.reset_index()
out.columns = ["datetime", "open", "high", "low", "close", "volume"]
out.to_csv(tmp, index=False)
ds = LocalFileAdapter().load_csv(str(tmp), "NSE:SYNTH-EQ", "1d",
                                 provenance=prov)
tmp.unlink(missing_ok=True)
frame = ds.frames["NSE:SYNTH-EQ"]["1d"]
print(f"  imported rows={len(frame)} tz={frame.index.tz} "
      f"validation_valid={ds.validation['NSE:SYNTH-EQ'].valid}")

# [3] Causal replay V2/V3/V4 ----------------------------------------------------
print("\n[3] CAUSAL REPLAY (V2/V3/V4 on causal snapshots)")
rows, audit = replay_v5_dataset({"NSE:SYNTH-EQ": frame}, step=5, start=60,
                                horizon_bars=5)
print(f"  forecasts      : {len(rows)}  (per-config, same steps)")
print(f"  LOOKAHEAD      : {audit.lookahead_violations} violations (must be 0)")
by = {}
for r in rows:
    by.setdefault(r.config, []).append((r.bias, r.realized_return))
for cfg in ("V2", "V3", "V4_technical", "V4_news", "V4_full"):
    m = compute_metrics(cfg, by[cfg])
    acc = m.directional_accuracy
    exp = m.expectancy
    print(f"  {cfg:<12} n={m.trades:>3} acc={acc and round(acc,3) or '-'} "
          f"exp={exp and round(exp,3) or '-'} note={m.note}")

# [4] Calibration + costs + slippage -------------------------------------------
print("\n[4] CALIBRATION / COSTS / SLIPPAGE")
conf_rows = [(50.0 + (i % 4) * 10, r.realized_return)
             for i, r in enumerate(rows)]
cc = confidence_calibration(conf_rows)
for b in cc.buckets:
    wr = f"{b.win_rate:.0%}" if b.win_rate is not None else "-"
    print(f"  confidence {b.bucket:>6}: n={b.count:<4} win={wr}")
print(f"  probability_status: {cc.probability_status}")
cost = CostAssumptions()
print(f"  round-trip cost  : {cost.round_trip_bps():.1f} bps "
      f"({cost.round_trip_pct():.2f}%) — ASSUMPTION, not historical fact")
sl = slippage_sensitivity(by["V3"])
for lbl, m in sl.items():
    print(f"  slippage {lbl:<4}: cost={m.cost_pct:.2f}% "
          f"net_exp={m.cost_adjusted_return and round(m.cost_adjusted_return, 3)}")
print(f"  edge_survives_slippage: {edge_survives_slippage(sl)}")

# [5] Improvement test + verdict -----------------------------------------------
print("\n[5] IMPROVEMENT TEST + VERDICT")
imp_v3 = improvement_test(by["V2"], by["V3"], min_sample=30,
                          base_direction="V2", compare_direction="V3")
imp_v4 = improvement_test(by["V3"], by["V4_full"], min_sample=30,
                          base_direction="V3", compare_direction="V4_full")
print(f"  V3 vs V2 : delta={imp_v3.absolute_delta} "
      f"CI=[{imp_v3.ci_low:.3f},{imp_v3.ci_high:.3f}] verdict={imp_v3.verdict}")
print(f"  V4 vs V3 : delta={imp_v4.absolute_delta} "
      f"CI=[{imp_v4.ci_low:.3f},{imp_v4.ci_high:.3f}] verdict={imp_v4.verdict}")
vd = classify_verdict(VerdictInput(
    improvement_V3=imp_v3, improvement_V4=imp_v4,
    net_expectancy_base=0.5, net_expectancy_compare=0.5,
    edge_survives_costs=False, edge_survives_slippage=False,
    min_sample_met=len(by["V3"]) >= 30, oos_supported=False))
print(f"  FINAL VERDICT  : {vd}")

# [6] Run registry ---------------------------------------------------------------
print("\n[6] RESEARCH RUN REGISTRY (append-only)")
reg = ResearchRunRegistry(create_engine("sqlite://", future=True))
run = reg.create_run(dataset_id=prov.dataset_id, dataset_hash=prov.sha256(),
                     config={"step": 5, "start": 60, "horizon_bars": 5},
                     seed=7)
reg.complete_run(run.run_id, {"verdict": vd, "forecasts": len(rows)},
                 warnings=["SYNTHETIC_TEST data; no real dataset present"])
runs = reg.list_runs()
print(f"  runs stored    : {len(runs)}  run_id={runs[0].run_id[:8]}... "
      f"status={runs[0].status}")

print("\n" + BAR)
print("REAL_HISTORICAL_DATA_NOT_PRESENT")
print("REAL_HISTORICAL_VALIDATION_PENDING")
print("DATA TYPE: SYNTHETIC_TEST — infrastructure verified, predictive")
print("validation requires a real historical dataset.")
print(BAR)