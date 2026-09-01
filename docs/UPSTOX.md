# Upstox API — Current Findings

Sources: official Upstox API v2 documentation, developer console, and public samples.
Research performed 2026-09-01.

## 1. Current authentication flow (OAuth2)

- Create an APP in the Upstox developer console → obtain **Client ID** and **Client Secret**.
- Register a **Redirect URI** with the app. The URI is treated as configuration
  (env var `UPSTOX_REDIRECT_URI`); do not hardcode it in source.
- Flow (manual, no local callback server):
  1. Build the authorize URL (see below).
  2. User opens the URL in a browser and completes Upstox login/consent.
  3. Upstox redirects the browser to the registered redirect URI with
     `?code=<auth_code>&state=<state>`.
  4. User copies the `auth_code` from the redirect URL and runs
     `python -m trading_system auth-exchange` (or sets `UPSTOX_AUTH_CODE`).
  5. The CLI exchanges the code at the token endpoint → receives an access token.
  6. The access token is saved to `.env` (`UPSTOX_ACCESS_TOKEN`) automatically.

### Authorization URL

```
https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id=<UPSTOX_CLIENT_ID>&redirect_uri=<UPSTOX_REDIRECT_URI>&state=<random>
```

- `response_type=code` is always `code`.
- `state` is a fresh random value per request (CSRF protection).
- The client secret is **never** included in the authorization URL.

### Token exchange (server-to-server)

```
POST https://api.upstox.com/v2/login/authorization/token
Content-Type: application/x-www-form-urlencoded

code=<auth_code>
client_id=<UPSTOX_CLIENT_ID>
client_secret=<UPSTOX_CLIENT_SECRET>
redirect_uri=<UPSTOX_REDIRECT_URI>
grant_type=authorization_code
```

Response (on success):
```json
{
  "access_token": "<token>",
  "token_type": "Bearer",
  ...
}
```

- The `redirect_uri` in the exchange must **exactly match** the URI registered with Upstox.
- The `code` is single-use.

### Token expiration

- Upstox access tokens expire at **3:30 AM IST the following day**.
- When a token is expired, re-authorization is required (`auth-login` + `auth-exchange`).
- Upstox does **not** provide a refresh-token flow for this application; the token
  manager does **not** invent one.

### Profile / connectivity probe

```
GET https://api.upstox.com/v2/user/profile
Authorization: Bearer <access_token>
```

- HTTP 200 → token is live (AUTH_OK).
- HTTP 401/403 → token rejected/expired (ACCESS_TOKEN_EXPIRED).
- Network failure → NETWORK_ERROR.

### Environment variables

| Variable | Description |
|---|---|
| `UPSTOX_CLIENT_ID` | API key from console.upstox.com |
| `UPSTOX_CLIENT_SECRET` | API secret (never sent in URLs) |
| `UPSTOX_REDIRECT_URI` | Exact URI registered in the Upstox console |
| `UPSTOX_ACCESS_TOKEN` | Access token (obtained via auth-exchange) |

## 2. Historical candle endpoint (REST)

- Base: `https://api.upstox.com/v2`
- Path: `GET /historical-candle/{instrument_key}/{interval}/{to_date}/{from_date}`
- Candle array order: `[epoch, open, high, low, close, volume, oi]`
- Response: `{"status":"success","data":{"candles":[[...]]}}`
- Intervals: `1minute`, `5minute`, `15minute`, `30minute`, `1hour`, `day`, `week`, `month`

## 3. Instrument keys

Upstox uses instrument keys in the form `EXCHANGE_SEGMENT|SYMBOL`:
  * Equity  : `NSE_EQ|SBIN`
  * Index   : `NSE_INDEX|NIFTY50`
  * Futures : `NSE_FUT|SBIN25DECFUT`
  * Options : `NSE_OPT|SBIN25DEC400CE`

## 4. Quotes

- `GET /market-quote/quotes?symbol=NSE_EQ|SBIN` → `{"status":"success","data":{"NSE_EQ|SBIN":{"last_price":...}}}`

## 5. Live market-data WebSocket

- Endpoint: `wss://ws-api.upstox.com/v2/feed`
- Auth frame: `{"type":"auth","access_token":"<client_id>:<access_token>"}`
- Subscribe: `{"type":"subscribe","symbols":["NSE_EQ|SBIN",...]}`

## 6. Rate limits

Not confirmed in current sources. Treat as unverified until documented.

## 7. Current API pricing

Verify at console.upstox.com before production use.
