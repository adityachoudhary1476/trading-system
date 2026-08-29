# Day 7 Report — F&O Research + Backtesting Engine

**Date:** 2026-08-29
**Scope:** Provider-independent historical research & backtesting engine. DATA ONLY —
no orders, no broker execution, no live trading, no autonomous trading, no LLM in the
decision loop. The purpose is *reproducible, honest* answers to "what would have
happened", not trading recommendations.

---

## 1. What was built

A new `src/trading_system/research/` package, fully provider-independent. The
backtester depends only on a `HistoricalDataset` (OHLCV DataFrame + contract identity +
data quality); it never touches FYERS or MarketStore directly except via the read-only
`MarketDataRepository` loader.

### Features (`features.py`)
Provider-independent, deterministic, CAUSAL indicators computed on normalized OHLCV:
simple return, log return, SMA, EMA, rolling volatility, ATR (Wilder), momentum,
high/low range, volume change, rolling volume, trend classification (EMA cross sign),
volatility regime (vs expanding median). Every indicator uses only data available
at/before T; no-look-ahead is unit-tested explicitly.

`AVAILABLE_FEATURES` lists what the engine CAN compute from stored OHLCV.
`MISSING_REQUIRED_FEATURES` lists derivative analytics the DB cannot supply (open
interest, OI change, basis, IV, greeks, delta, theta) so the gap is explicit — these
are NEVER fabricated.

### Strategies (`strategies.py`)
Clean `Strategy` interface: `generate(df) -> Series{-1,0,1}` (FLAT/LONG/SHORT). Three
research baselines (not recommendations): `EMATrendStrategy`, `MomentumStrategy`,
`BreakoutStrategy`. Each derives its OWN causal indicators from the raw df (self-contained).

### Risk (`risk.py`)
`RiskConfig`: max position size, max allocation %, allow_short, leverage, stop_loss_pct,
take_profit_pct, max_loss_per_trade_pct, max_positions. Applied consistently by the
engine; no parameter is silently ignored.

### Backtester (`backtester.py`)
Deterministic. EXECUTION MODEL: signal at bar T → ENTER at bar **T+1 open** (never the
signal bar's own close); exits (signal flip / stop / TP) also at next-bar open. Costs
applied per side: transaction_cost_pct (fraction of notional) + slippage_pct (adverse
fill). One dataset = one contract; the engine NEVER rolls across contracts — running on
two contracts requires two backtests (prevents silent expiry crossing).

### Performance (`performance.py`)
Deterministic metrics: total return, ending capital, net P&L, #trades, win/loss, win
rate, avg win/loss, largest win/loss, profit factor, max drawdown, avg trade return,
exposure, Sharpe-like & Sortino-like (annualized via `analysis.quant.TRADING_PERIODS`).
Small samples are flagged `reliable=False` with explicit warnings — never presented as
proof of profitability.

### Walk-forward (`walkforward.py`)
Chronological TRAIN/TEST split (default 70/30, configurable). Test strictly after train;
no shuffling, no leakage. Day 7 intentionally does NOT optimize parameters on the split.

### AI boundary (`ai_interface.py`)
Inactive `AnalysisSnapshot` struct for where an AI analyst could later read feature
snapshot / signal / performance / risk / regime. No LLM is invoked; the backtester never
imports it.

### CLI
```
python -m trading_system strategies
python -m trading_system backtest --symbol NSE:SBIN --timeframe 5m --strategy ema \
    --initial-capital 100000 --transaction-cost 0.0005 --slippage 0.0002
python -m trading_system backtest --symbol NSE:SBIN --timeframe 5m --strategy momentum \
    --train-frac 0.7        # adds an OUT-OF-SAMPLE section
```
Existing commands unchanged.

## 2. Real-data verification (only what actually exists)
The persistent store (`data/market_data.db`) contains **exactly NSE:SBIN 5m, 600 rows,
2026-08-18 → 2026-08-27** (0 duplicates). No 1d, no derivative history is stored. So:
- **Equity backtest: RUN, end-to-end.** EMA on SBIN 5m over the 600-row window returned
  -1.01% net (9 trades, profit factor 0.54) with the small-sample warning firing. This is
  a smoke/integration result, NOT evidence of profitability (see §5).
- **Derivative backtest: BLOCKED** — `backtest --symbol NFO:NIFTY25DECFUT` prints
  "NO DATA ... blocked (no fabricated data)". No derivative rows exist in MarketStore, so
  no derivative backtest is claimed. Requires backfilling F&O first (Day 6 path), which is
  itself blocked by the expired FYERS token.

## 3. Test count
`tests/test_research.py` — **32 tests**. Full suite: **201 passed, 0 failed**
(169 Day 1–6 baseline + 32 Day 7). No regressions. `py_compile` clean.

## 4. Safety
Research + backtesting ONLY. No order placement, broker/FYERS/Groww API, live execution,
autonomous trading, real-money positions, or account mutation. Credentials read from env,
never printed. No LLM in the decision loop.

## 5. Known limitations / honesty notes
- Only 600 rows of real data exist; trade counts (<10) make all metrics statistically
  meaningless. The engine says so explicitly.
- Stop/take-profit fills are approximated at NEXT-bar open (no intrabar high/low peeking)
  — a documented, conservative choice.
- No leverage applied unless `leverage>1` is explicitly set (default 1.0, long-only).
- Derivative OI/IV/greeks/basis are NOT computed (DB lacks them); listed as missing.
- Backtest result is NOT evidence of future profitability.

## 6. Files changed
NEW: `src/trading_system/research/{__init__,features,strategies,risk,dataset,backtester,
performance,walkforward,ai_interface}.py`, `tests/test_research.py`, `DAY7_REPORT.md`.
MODIFIED: `src/trading_system/__main__.py` (strategies + backtest CLI), `README.md`,
`ARCHITECTURE.md` (module map + research section). NOT committed.
