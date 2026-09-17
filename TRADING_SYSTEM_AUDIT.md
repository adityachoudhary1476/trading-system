## Phase 1: Codebase Audit — COMPLETE

### Two codebases identified
1. **ROOT-LEVEL PROTOTYPE** (`C:/Users/Owner/`): `backtest_engine.py`, `strategies.py`, `compute_indicators.py`, `deep_analysis.py`, `run_all_strategies.py`, `nifty_backtest.py` + pickle data files
2. **MAIN PROJECT** (`C:/Users/Owner/OneDrive/Desktop/trading-system/`): Full Finova Markets platform with Phase 21-23, research spine, paper trading, AI intelligence

### @reality-checker's deep audit of prototype (COMPLETE)
- Data `nifty_5y_full.pkl`: 1236 daily NIFTY 50 bars, no gaps, causal indicators verified ✅
- Look-ahead/bias: GREEN in causal path ✅
- **7/8 strategies lose money** — sign-flip IS→OOS reversals (overfitting)
- Headline Sharpe 12.81 is per-trade √252 artifact — true calendar Sharpe = 1.33
- `deep_analysis.py` crashes (KeyError: bb_lower_20_1.8) — columns never generated
- 103 grid combos tuned on FULL sample in `run_all_strategies.py` — no OOS split
- Full report: `RC_REPORT_NIFTY_BACKTEST.md`, scripts `_rc_verify.py`, `_rc_falsify3.py`

### @developer task #zam8: deep_analysis.py crash + grid search audit (COMPLETE)
- **CRASH CONFIRMED**: `deep_analysis.py` parameter stability test (L172-179) crashes with `KeyError: 'bb_lower_20_1.8'`
- Root cause: `compute_indicators.py` generates BB mult=[1.5, 2.0, 2.5] only; `deep_analysis.py` tests [1.8, 2.0, 2.2]
- **GRID SEARCH ON FULL SAMPLE**: `run_all_strategies.py` tests 103 param combos on full dataset, NO train/test split
- **IS/OOS uses full-sample-tuned params**: overfitting vector
- **Walk-forward is fixed-param only**: No per-fold optimization
- **Main project contrast**: `parameter_sensitivity.py` is sensitivity analysis (NOT grid search selection) — correct pattern
- Report: `ZAM8_REPORT.md`

### Main project status
- **276 tests passing** (after creating missing `tests/__init__.py` + `tests/fixtures/__init__.py`)
- Hard safety boundary: NO execution/broker/order code anywhere (grep-verified)
- Research spine: backtester (warmup-aware), factors (causal), evidence store, performance analytics
- Only data: `NSE:SBIN` 1d (2477 bars) + 5m (600 bars); FYERS token expired
- Frontend exists but not wired to research APIs

### @reality-checker's Finova-core audit (COMPLETE)  
- `backtester.py`: GENUINELY CAUSAL ✅
- `factors.py`: ALL factors causal ✅
- Warmup/eval split: correct ✅
- `forward_return`: factor_T → return_T+lag ✅
- Tests enforce no-lookahead ✅
- Latent fragility: `_trend_strength` idx slice wrong but harmless

### Key Decision Needed @you
The root-level prototype scripts are **NOT under version control** and have **no tests**. They enable overfitting via full-sample grid search. The main Finova Markets project is well-architected with proper guardrails.

**Recommendation:** Focus all Phase 2 efforts on the main project (`trading-system/`). The prototype scripts are a dead-end for building a robust autonomous system.

---
