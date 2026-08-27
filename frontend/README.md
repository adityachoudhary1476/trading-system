# FINOVA MARKETS — Frontend

AI-powered Indian market intelligence terminal (React + TypeScript + Vite).
Built on Day 5 with **deterministic mock data only** — no live FYERS connection.

## Run

```bash
cd frontend
npm install
npm run dev      # http://localhost:5173
npm run build    # production build -> dist/
npm run preview  # serve the production build
```

## Architecture

- `src/data/MarketDataSource.ts` — the single data interface. Components depend
  only on it. Tonight it's `MockMarketDataSource`; swap to a real
  `ApiMarketDataSource` (REST + WebSocket to the Python engine) tomorrow with no
  UI changes.
- `src/data/mock.ts` — all mock generators (deterministic, no network).
- `src/types/index.ts` — shared contracts (MarketQuote, OHLCVBar, AIAnalysis,
  Signal, FeedHealth, PipelineStage) mirroring the backend models.
- `src/components/{layout,market,charts,ai,signals,system}` + `src/pages/`.
- `src/store/AppContext.tsx` — selected instrument + app environment.

## Safety / scope (enforced)

- `DEMO DATA` badge + `OFFLINE · MOCK` are always visible in mock mode.
- No Buy/Sell execution anywhere; signals are analytical only.
- No FYERS credentials / secrets in frontend code.
- Frontend never connects to FYERS directly — only to the Python backend.

See `FRONTEND_BACKEND_CONTRACT.md` for the exact field mapping and the swap point.
