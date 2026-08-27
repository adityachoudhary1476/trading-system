# FINOVA MARKETS — Frontend / Backend Contract (Day 5 → Day 6 handoff)

This document describes exactly how the frontend (built on Day 5, mock-data only)
will connect to the real Python backend during NSE market hours. **No backend
code was modified tonight. No FYERS credentials are present in the frontend.**

## 1. Where mock data lives

- `frontend/src/data/mock.ts` — all deterministic mock generators
  (quotes, OHLCV, AI analysis, signals, feed health, pipeline stages).
- `frontend/src/data/MarketDataSource.ts` — the single swap point. Today it
  exports `MockMarketDataSource`. Replace the `dataSource` instance with a real
  `ApiMarketDataSource` to go live — **no component changes required**.

## 2. The interface the real backend must implement

`MarketDataSource` (in `src/data/MarketDataSource.ts`):

```ts
interface MarketDataSource {
  readonly mode: "mock" | "live";
  getQuote(symbol: string): Promise<MarketQuote>;
  getOHLCV(symbol: string, timeframe: string, bars?: number): Promise<OHLCVBar[]>;
  getAIAnalysis(symbol: string): Promise<AIAnalysis>;
  getSignals(limit?: number): Promise<Signal[]>;
  getFeedHealth(): Promise<FeedHealth>;
  getPipeline(): Promise<PipelineStage[]>;
}
```

A real `ApiMarketDataSource` will call the Python backend over REST (snapshot /
signals / feed-health) and subscribe to a WebSocket for live quotes + candles.
The frontend NEVER connects to FYERS directly — it talks only to the Python
engine, which already isolates FYERS specifics under `src/trading_system/india/`.

## 3. Field contracts the frontend expects

These mirror the backend models so the real payloads drop in cleanly.

### MarketQuote  (backend: FYERS quote + MarketSnapshot spot)
```
symbol, providerSymbol, name, exchange, instrumentType,
price, previousClose, change, changePct, dayOpen, dayHigh, dayLow,
volume, vwap, dayRange, volatility, sessionState, lastUpdate(epoch ms)
```

### OHLCVBar  (backend: ClosedCandlePipeline → MarketStore)
```
time(epoch ms), open, high, low, close, volume
```

### MarketSnapshot → AIAnalysis  (backend: models/snapshot.py + models/market_view.py)
```
symbol, timeframe, bias(=market_view), confidence(0..1), signal(direction),
summary(=reasoning_summary), factors[ label, value, tone ], generatedAt, model
```
The real backend returns a validated `MarketView` (bias/confidence/factors) plus
a `Signal` (direction/confidence/reason). `AIAnalysis` is assembled from those.

### Signal  (backend: signals/__init__.py — Signal dataclass)
```
id, symbol, direction(long|short|hold|no_signal), confidence(0..1),
generatedAt(epoch ms), price, bias, reason, source
```

### FeedHealth  (backend: india/data_health.py — FeedStatus + FeedMetrics)
```
feed, status(healthy|stale|disconnected|auth_error|invalid_data),
lastTick, eventsReceived, eventsRejected, candlesGenerated,
lastClosedCandle, connected
```

### PipelineStage  (backend: pipeline stages)
```
id, label, status, lastActivity(epoch ms), metric
```

## 4. Where the mock source is replaced

Edit ONLY `frontend/src/data/MarketDataSource.ts`:

```ts
// export const dataSource = new MockMarketDataSource();
export const dataSource = new ApiMarketDataSource(import.meta.env.VITE_API_BASE);
```

`env.mode` flips to `"live"`, the header badge switches from `DEMO DATA` to
`LIVE`, and `execution` stays `DISABLED` until a separate, explicit decision.
All components already depend on `dataSource` via the `MarketDataSource`
interface, so nothing else changes.

## 5. Safety reminders (unchanged tonight)

- No Buy/Sell execution in the UI; Signal panel says "Analytical signal only".
- No secrets/credentials in frontend code or `import.meta.env`.
- Mock mode is always visibly identified (`DEMO DATA` badge + `OFFLINE · MOCK`).
- "Connected to FYERS" is NEVER claimed in mock mode.
