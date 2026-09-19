# Plan: Greeks / OI / IV Analytics for NIFTY Options Paper Trading

## Current State
- Backend capable, discoverer attached, quote authenticated
- Chain disabled (Phase 8A multi-leg only)
- options_enabled=True (DB + code + model default)
- Scheduler running, kill switch cleared

## Approach: Build incrementally
Start with what the existing pipeline can support, then add missing pieces.

## Phase 1 — Data (Week 1)

### 1. Enable option chain provider
- File: src/trading_system/research/phase23/deployment.py
- Add chain provider to Phase23DeploymentConfig
- Attach CurrentOptionChainProvider (check if it exists in codebase)

### 2. Persist IV per strike
- Model: src/trading_system/paper/schema.py (PaperPosition)
- Table: option_position_iv (instrument_key, timestamp, iv, delta, gamma, theta, vega)
- Insert via paper_api.py after each quote fetch

### 3. Verify Upstox option chain endpoint
- Endpoint: GET /v3/market/option-chain?symbol=NSE:NIFTY
- Fields: strike_price, bid_iv, ask_iv, volume, oi, change_oi
- Store raw JSON + parsed fields

## Phase 2 — Calculation (Week 2)

### 1. Implement Black-Scholes on chain data
- File: src/trading_system/research/options/analytics.py
- Functions: calculate_greeks(S, K, T, r, sigma, option_type)
- Use bid/ask mid-price as sigma input
- Cache results: 5s TTL per strike

### 2. Compute OI change
- Field: option_chain_snapshot (store hourly snapshots)
- Calc: current_oi - previous_oi (per strike)
- Flag: >20% change in OI = unusual activity

### 3. IV rank / percentile (20-period)
- Store IV history: option_iv_history table (1h candles)
- IV Rank = (current_iv - min_iv_20) / (max_iv_20 - min_iv_20)
- IV Percentile = % of 20-period IVs below current

## Phase 3 — Integration (Week 3)

### 1. Expose via API
- GET /api/paper/options/analytics/{symbol}?expiry=...
- Returns: greeks per strike, OI change, IV rank/percentile, max pain

### 2. Paper decision engine
- File: src/trading_system/research/phase23/strategy.py
- Use IV percentile < 20 as "IV is low" entry signal
- Use OI spike as confirmation
- Max 2 contracts per trade (existing constraint)

### 3. Frontend widgets
- TradingView-style strike table with color-coded Greeks
- OI heatmap per strike
- IV rank sparkline

## Phase 4 — Safety (Week 4)

### 1. Greeks-based risk checks
- Portfolio delta: |delta| < 50 (position limit)
- Portfolio vega: |vega| < 1000 (IV shock limit)
- Max pain: warn if expiry < 3D and ITM

### 2. Backtest validation
- Replay last 30 days of IV/OI
- Compare IV-percentile entry vs. buy-hold
- Target: >55% win rate on NIFTY expiry

## Data Flow
```
Upstox API → OptionChainProvider → AnalyticsEngine → PaperTradingEngine → DB
    ↓           ↓                    ↓              ↓
  IV per strike  Greeks (B76)     IV Rank/OI%    Trade decision
  OI per strike  IV Percentile    Max Pain       Risk checks
```

## Estimated Effort
- Phase 1: 5 hrs (data plumbing)
- Phase 2: 6 hrs (calculations + storage)
- Phase 3: 4 hrs (API + strategy)
- Phase 4: 3 hrs (risk + backtest)
- Total: ~18 hrs (2-3 weeks part-time)
