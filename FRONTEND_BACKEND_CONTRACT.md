# Frontend ↔ Backend Contract (Day 8)

Analysis / intelligence only. **No order, no execution, no broker API.**

## Guiding principle
The backend exposes **structured analysis**. The frontend displays it. A
`SignalCandidate` is an *analytical hypothesis*, never an order. The contract
makes this explicit: there is no order endpoint, and none will be added here.

```
GET /market/:symbol        -> live price + chart candles + data-health
GET /features/:symbol       -> computed technical features for a symbol/tf
GET /analysis/:symbol       -> regime + signal candidate + explanation
GET /signals                -> list of current signal candidates (read-only)
```

No `POST /order`, `POST /trade`, `POST /execute`, or any mutating trade route.

## Signal Candidate ≠ Order
```
SignalCandidate {            Order (NOT IMPLEMENTED) {
  symbol                       symbol
  contract_id                  qty
  direction: LONG|SHORT|...    side: BUY|SELL
  setup: TREND_CONTINUATION..  order_type
  confidence: 0..1             price
  entry_context                ...
  invalidation_context         => executed by a system that DOES NOT EXIST HERE
  risk_flags
}
```
The frontend MUST render `SignalCandidate` with a clear "ANALYSIS ONLY" label and
must NOT present it as actionable without a separate, explicitly-built execution
service (out of scope for Day 8).

## GET /market/:symbol
```json
{
  "symbol": "NSE:SBIN",
  "timeframe": "5m",
  "latest_price": 1042.90,
  "candles": [ { "t": "2026-08-27T00:00:00Z", "o":..,"h":..,"l":..,"c":..,"v":.. } ],
  "data_health": { "status": "healthy", "stale_seconds": 60 }
}
```

## GET /features/:symbol?timeframe=1d
```json
{
  "symbol": "NSE:SBIN",
  "timeframe": "1d",
  "close": 1042.90,
  "sma_20": 1058.20, "sma_50": 1042.55, "sma_200": 1037.38,
  "ema_20": 1050.93, "ema_50": 1042.90,
  "rsi_14": 47.33, "atr_14": 17.60,
  "relative_volume": 0.47, "vol_regime": "low", "trend": "bullish",
  "price_vs_sma20": -0.0145,
  "recent_high": 1099.0, "recent_low": 980.0,
  "breakout_candidate": false, "breakdown_candidate": false,
  "instrument_class": "equity",
  "derivative": { "open_interest": null, "implied_vol": null, "delta": null,
                  "gamma": null, "theta": null, "vega": null, "basis": null }
}
```
> Derivative fields are `null` until FYERS supplies OI/IV/greeks. Frontend must
> render "n/a", never a fabricated number.

## GET /analysis/:symbol?timeframe=1d
```json
{
  "symbol": "NSE:SBIN",
  "timeframe": "1d",
  "status": "OK",
  "regime": { "regime": "trending_up", "confidence": 0.70,
              "supporting_features": ["trend=BULLISH"], "warnings": [] },
  "signal_candidate": {
    "direction": "long", "setup": "trend_continuation", "confidence": 0.70,
    "entry_context": "last close 1042.90, SMA20 1058.20",
    "invalidation_context": "trend structure breaks (price < SMA20 and EMA20 < EMA50)",
    "supporting_features": ["trend bullish (EMA20>EMA50, price>SMA20)"],
    "risk_flags": []
  },
  "explanation": {
    "summary": "trending_up | candidate long/trend_continuation conf=0.70",
    "bullish_factors": ["EMA20 above EMA50 (short-term uptrend)"],
    "bearish_factors": ["price 1.45% below SMA20"],
    "neutral_factors": [], "risks": [], "missing_data": ["SMA200 (need >=200 bars)"]
  },
  "data_quality": { "rows": 2477, "insufficient": false, "missing": [] },
  "ai": { "conclusion": "...", "confidence": 0.62, "risks": [...] }   // optional
}
```
If `status` is `BLOCKED`, the body contains `{ "status": "BLOCKED", "reason": "DATA_HEALTH = STALE" }`
and the frontend must show "ANALYSIS BLOCKED" instead of any signal.

## GET /signals
```json
[
  { "symbol": "NSE:SBIN", "timeframe": "1d", "direction": "long",
    "setup": "trend_continuation", "confidence": 0.70, "generated_at": "..." }
]
```
Read-only list of latest candidates. No state mutation.

## Data health gating
When `DataHealthMonitor.status` is `stale|disconnected|auth_error|invalid_data`,
`/analysis/:symbol` returns `BLOCKED` with the reason. The frontend shows the
blocked banner and hides signal UI.

## Confidence semantics
`confidence` is an **analytical score** in [0,1], NOT a probability of profit.
The contract forbids presenting it as "P=0.70 it will go up".

## Authoring notes
- All timestamps: ISO-8601, timezone-aware UTC.
- No secrets, no credentials, no broker tokens in any response.
- Derivatives: CE/PE and futures are separate `contract_id`s; never merge.
- This contract is consumed by the React+TS+Vite "Finova Markets" frontend.
