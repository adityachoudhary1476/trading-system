# Indian Market Data Layer

The system's primary target is now Indian markets (NSE/BSE/MCX), with Binance kept
as a development/test provider. All provider-specific Indian logic lives under
`src/trading_system/india/`; the rest of the app only sees normalized types.

## Supported Indian markets (architecture, not a hard-coded allow-list)

- **NSE equities**: RELIANCE, TCS, INFY, HDFCBANK, ICICIBANK, SBIN, ...
- **NSE indices**: NIFTY 50 (`NSE:NIFTY50`), BANK NIFTY (`NSE:NIFTYBANK`), FINNIFTY
  (`NSE:FINNIFTY`).
- **F&O** (NFO/MCX): futures/options represented via the instrument model; the
  FYERS symbol format (e.g. `NSE:SBIN25DEC400CE`) is generated, not hard-coded.
- BSE/CDS are supported by the model (exchange field); adapters can be added later.

## Symbol normalization

Internal symbols are `EXCHANGE:SYMBOL` (e.g. `NSE:RELIANCE`). Provider-specific
strings are confined to `india/symbol_map.py`:

| Internal        | FYERS provider symbol |
|-----------------|-----------------------|
| `NSE:RELIANCE`  | `NSE:RELIANCE-EQ`     |
| `NSE:NIFTY50`   | `NSE:NIFTY50-INDEX`   |
| `NSE:SBIN25DEC400CE` (option) | `NSE:SBIN25DEC400CE` |

This isolation means Angel One / Upstox can be added later as new adapters without
touching the rest of the system.

## Market sessions (Asia/Kolkata)

`india/market_calendar.py` encapsulates NSE equity session rules:

- Timezone `Asia/Kolkata` (UTC+5:30, no DST).
- Trading days: Monday–Friday; weekends closed.
- Continuous session: **09:15–15:30 IST**.
- Exposes `is_trading_day`, `market_state` (OPEN/CLOSED), `is_within_session`,
  `session_boundaries`, `next_session_open`.
- Crypto-style 24/7 behavior is explicitly NOT assumed for NSE.

## Data flow

```
FYERS REST /data/history  ──►  provider.get_historical()
        │                          normalizes to OHLCV DataFrame (tz-aware UTC)
        ▼
   validate_ohlcv()  ──►  MarketStore.upsert_many()   (idempotent, exchange-aware)
        │
        ▼
   Pandas/NumPy  ──►  indicators  ──►  MarketSnapshot  ──►  AI Analyst  ──►  Signal

FYERS WebSocket /socket/v2/data/
        │
        ▼  raw tick/event
   _normalize_ws()  ──►  InternalMarketEvent  ──►  CandleAggregator
        │                                                  │
        │                          provisional (forming) vs closed (completed)
        ▼
   (future) PaperTrader consumes InternalMarketEvent, NOT raw FYERS responses
```

## Historical data

- Interval mapping in `india/fyers.py::_RESOLUTION` (1m/5m/15m/1h/1d/1w/1M supported
  on Day 3; FYERS also offers 2/3/10/20/30/45/60/120/240m and seconds).
- Date range is derived from `limit` (clamped to FYERS caps: 100d for minutes,
  366d for day+). Multi-chunk fetching can be added later.
- Results normalize to the existing internal OHLCV representation with preserved
  timezone (UTC).

## Live WebSocket data

- `FyersDataSocket` (in `india/fyers.py`) wraps `websocket-client`: auth, subscribe
  (Lite or SymbolUpdate mode), message normalization to `InternalMarketEvent`,
  heartbeat ping, stale-data detection, and **exponential-backoff reconnect**
  (capped, no aggressive CPU loop). The AI is never called from the tick hot path.
- Requires `FYERS_CLIENT_ID` + `FYERS_ACCESS_TOKEN`. Without them, live mode exits
  with a clear "credentials not available" message — no fabricated stream.

## Candle aggregation

- `CandleAggregator` turns quote/trade updates into OHLCV bars:
  - Open = first price, High = max, Low = min, Close = last, Volume = summed.
- Bar grid aligns to the session-local wall clock (Asia/Kolkata).
- Deterministic and fully unit-tested (`test_india.py`).

## Provisional vs closed candles (critical)

- **Provisional (current) candle**: the bar currently forming; never treated as
  closed data. `CandleAggregator.provisional` exposes it; `flush_completed()` only
  returns finished bars.
- **Closed candle**: interval has completed; safe for the indicator/snapshot/signal
  pipeline (which operates on closed-bar history per Day 2).
- Look-ahead bias is prevented by never feeding a provisional bar into components
  that expect closed data, and by the `MarketSnapshot` no-look-ahead guarantees.

## Provider abstraction

`FYERSMarketDataProvider` implements the same `MarketDataProvider` interface as
`BinanceProvider`. `get_provider("fyers")` selects it; nothing downstream knows
whether data came from Binance or FYERS.

## Configuration (`.env`)

```
MARKET_DATA_PROVIDER=fyers        # or binance for dev
FYERS_CLIENT_ID=
FYERS_ACCESS_TOKEN=
```

Secrets are never committed; `.env.example` holds only placeholder names.
