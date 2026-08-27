# Data Sources

Investigated before selecting a provider. Findings below are what was actually
observed, not marketing claims.

## Selected for Day 1: Binance Public REST (`klines`)

| Attribute | Finding (verified Day 1) |
|---|---|
| Endpoint | `https://api.binance.com/api/v3/klines` (public) |
| Auth | **None** required for market data / klines / ticker |
| Rate limit | Weight-based: 6000 weight / min per IP. One `klines` call (limit ≤ 1000) costs weight **2**. Very generous for research. |
| Historical coverage | Full history since 2017 for active pairs (BTCUSDT, ETHUSDT, …). |
| Granularity | 1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w, 1M. |
| Real-time? | Candles close at interval boundaries; the newest bar is the live/open one. Effectively **near-real-time for research**, but the last bar is provisional — we do **not** claim tick-level real-time. |
| Asset classes | **Crypto only.** No equities, FX, or commodities. |
| Licensing | Binance API terms apply. Free for research/non-redistributive use. Do not resell the raw feed. |
| Reachability | ✅ Reachable from this environment; returned 365 daily rows for BTCUSDT/ETHUSDT in ~1s. |

**Used for:** historical OHLCV ingestion (Day 1). Implemented in
`src/trading_system/data/binance.py`.

---

## Evaluated but NOT selected: Stooq (CSV download)

| Attribute | Finding (verified Day 1) |
|---|---|
| Documented endpoint | `https://stooq.com/q/d/l/?s=SYMBOL&i=d` |
| Auth | None |
| Reality check | Returned **HTTP 404 / "Invalid Symbol"** for programmatic access during Day 1 verification; unreliable as an automated feed (works better for manual CSV download). |
| Coverage | Global equities, indices, FX, commodities. |
| Delay | Often delayed / EOD for many symbols — **not real-time**. |
| Licensing | Provided "as is"; redistribution limits apply. |

**Status:** Implemented as `src/trading_system/data/stooq.py` (a documented
fallback proving the provider abstraction), but **not** the Day 1 default
because it 404'd under automated access. It raises a clear error rather than
fabricating data.

---

## Not evaluated / deferred

- **Yahoo Finance (`yfinance`)** — popular but rate-limited and ToS-restricted
  for automated/redistribution use; revisit if equities are needed.
- **Paid feeds (Polygon, Alpaca, Alpha Vantage, Tiingo)** — require API keys and
  have plan-based rate limits; evaluate when a funded research tier is approved.
- **Broker data (e.g. Binance signed endpoints, Interactive Brokers)** — only for
  execution later; not needed and not used on Day 1.

## Rule of thumb applied

> Do **not** claim data is real-time unless verified. Binance klines are treated
> as near-real-time with a provisional last bar; Stooq is treated as delayed.
