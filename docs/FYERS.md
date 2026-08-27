# FYERS API — Current Findings (Day 3 research)

Sources: official FYERS support KB, FYERS API v3 sample code (github.com/FyersDev),
`fyers-apiv3` PyPI package docs, and the FYERS API v3 skills reference
(github.com/FyersDev/fyers-skills). Research performed 2026-08-27. Where official
sources conflict with third-party posts, official docs were trusted.

## 1. Current authentication flow (OAuth2)

- Create an APP in the FYERS developer console → obtain **App ID** (aka client_id,
  e.g. `L9NY305RTW-100`) and **Secret Key**.
- Redirect URI is registered with the app.
- Flow: `SessionModel(client_id, secret_key, redirect_uri, response_type="code")`
  → `generate_authcode()` returns a login URL → user authorizes → you receive an
  **auth code** from the redirect → exchange for an **access token** via
  `generate_access_token(...)`.
- The access token is used for REST (`Authorization` header) and, for the data
  WebSocket, as `"APPID:AccessToken"`.
- **Requires an active FYERS trading account.** API access is not available without one.

## 2. Historical candle endpoint (REST)

- Base: `https://api-t1.fyers.in/data` (official skills ref lists Data: `https://api-t1.fyers.in/data`).
- Path: `GET /history`
- Params:
  - `symbol` — e.g. `NSE:SBIN-EQ`, `NSE:NIFTY50-INDEX`, `MCX:SILVERMIC20NOVFUT`
  - `resolution` — `"1","2","3","5","10","15","20","30","45","60","120","240"`,
    `"D"`/`"1D"`, `"1W"`, `"1M"` (seconds `"5S".."45S"` also listed).
  - `date_format` — `0` = epoch seconds, `1` = `YYYY-MM-DD`
  - `range_from`, `range_to` — start/end (per `date_format`)
  - `cont_flag` — `1` for continuous futures contracts (F&O)
  - `oi_flag` — `1` to append Open Interest to each candle
- Candle array order: `[epoch, open, high, low, close, volume]` (OI appended if requested).
- Response: `{"s":"ok","candles":[[...]]}`.
- **History API is explicitly NOT real-time** (official KB: "primary function is to
  provide historical data"). Do not treat it as a live feed.

## 3. Resolutions / intervals actually available

Confirmed supported: 1,2,3,5,10,15,20,30,45,60,120,240 min; D/1D; 1W; 1M; plus
5S–45S second resolutions. (The project's internal timeframes 1m/5m/15m/1h/1d/1w/1M
map cleanly onto these — see `india/fyers.py::_RESOLUTION`.)

## 4. Historical data caps (per official skills ref)

- Minute resolutions: **100 days per request**; history available from 3 Jul 2017.
- Day / Week / Month: **366 days per request**.
- Seconds resolutions: only **last 30 trading days**.
- Larger ranges require chunking the date range across multiple requests.

## 5. Live market-data WebSocket

- Endpoint: `wss://api.fyers.in/socket/v2/data/`
- Auth frame (after connect): `{"T":"c","authorization":"APPID:AccessToken","id":<ts>}`
- Subscribe: `{"T":"t","symbols":["NSE:SBIN-EQ",...]}`
- Two modes:
  - **Lite mode**: LTP (last traded price) changes only — minimal bandwidth.
  - **SymbolUpdate mode**: full OHLCV + more (ideal for aggregation/analytics).
- Heartbeat/ping handled by the client (`ping_interval`). The official Node SDK
  exposes `autoReconnect(retryCount)` with a max of 50 retries.
- The project's `FyersDataSocket` implements auth, subscribe, message normalization,
  stale-data detection (>60s no data), and exponential-backoff reconnect (capped),
  without an aggressive CPU loop.

## 6. Supported exchanges / instruments

- NSE (equities, indices, F&O), BSE, MCX (commodities), CDS (currency) per the
  SDK samples (`NSE:`, `BSE:`, `MCX:` prefixes). Index symbols use the `-INDEX`
  suffix; equities use `-EQ`; options/futures carry expiry/strike in the symbol.

## 7. Quotes / market depth

- `GET /quotes?symbols=NSE:SBIN-EQ` (up to 50 symbols) → `{"s":"ok","d":[{"v":{"lp":...}}]}`.
- `GET /depth?symbol=NSE:SBIN-EQ&ohlcv_flag=1` for market depth.

## 8. Rate limits / connection limits

- Not published in the sources consulted. Treat as **unverified**. The data socket
  supports Lite vs full modes; batching and Lite mode are the documented way to
  reduce load. No exact requests/sec published for the History API in these sources.

## 9. Current API / data-feed pricing

- **NOT confirmed free** in the official sources reviewed. FYERS API access requires
  an active trading account; explicit free-tier or per-feed pricing was not found in
  the official docs consulted. **Documented as UNVERIFIED — do not assume free.**
  Verify current pricing at myapi.fyers.in / FYERS support before production use.

## 10. Automated-trading / order restrictions (SEBI)

- Per FYERS support KB: as of retail algo-trading regulations, **API order placement
  must use a validated static IP**, and orders are allowed only from the whitelisted
  static IP. A compliant migration was required before **Apr 1, 2026**.
- These restrictions apply to **order placement**, which is explicitly OUT OF SCOPE
  for Day 3 (data only). They are recorded here so future broker-execution work
  accounts for static-IP whitelisting.

## 11. Can market data be used without placing orders?

- Yes. Historical (`/history`) and quotes/depth/WebSocket market data do not require
  order placement. Only the order/execution endpoints need the trading + static-IP
  setup. Day 3 uses only data endpoints.

## 12. Discrepancies / caveats noted

- Some older blog posts use `api.fyers.in/api/v2/markets/history` (v2). **v3** uses
  `api-t1.fyers.in/data/history`. The adapter targets v3.
- FYERS states they "don't allow writing a wrapper on our SDK" in one community
  post; the adapter does NOT wrap the official SDK — it speaks the documented REST/
  WebSocket contract directly via `requests` + `websocket-client`, which is permitted.
- WebSocket message framing in v3 "changed completely" per the npm notes; the
  adapter's `_normalize_ws` is best-effort and must be validated against a live
  session once credentials exist.

## 13. What was actually tested on Day 3

- **Not tested live**: no FYERS credentials were available in the environment, so
  real auth, historical fetch, and WebSocket streaming were NOT executed against FYERS.
- **Tested offline** (mocked/fixtures): FYERS response normalization, symbol
  mapping, instrument model, Indian session/calendar logic, candle aggregation,
  provisional-vs-closed distinction, and the credential-missing guard.
- **Verified**: `FYERSMarketDataProvider` is a drop-in `MarketDataProvider`; Binance
  remains the working dev provider and all 80 tests pass.
