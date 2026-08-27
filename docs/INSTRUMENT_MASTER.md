# Instrument Master

## What it is

FYERS (and most Indian brokers) expose an **instrument master** — a file/feed
mapping every tradable contract to its exchange, token, instrument type, expiry,
strike, option type, and lot size. The system consumes this to resolve
`InternalSymbol` ↔ provider symbol and to discover the full universe dynamically
instead of maintaining a hand-coded list.

## Current official mechanism (Day 4 research)

- FYERS publishes the symbol master as a downloadable file (CSV/JSON) from the
  developer console / API. Format (documented columns, per Day 3/4 research):
  `Symbol, Exch, Token, Instrument, Expiry, StrikePrice, OptionType, LotSize`.
- The endpoint for *live* master download typically requires authentication
  (an app session / access token). We did **not** hit it on Day 4 because no
  credentials were available.
- The master is updated periodically by the exchange (new expiries, listings);
  a production system should refresh it on a schedule rather than embed a snapshot.

## Implementation status (Day 4)

**IMPLEMENTED + TESTED (offline):**
- `InstrumentRepository` — normalized store with `get_instrument`,
  `search_instruments`, `get_equities(exchange)`, `get_indices(exchange)`,
  `get_derivatives(exchange)`, `get_expiring_derivatives(as_of, within_days)`.
- `InstrumentRepository.from_fyers_csv(csv_text)` — parses the documented FYERS
  master layout into normalized `Instrument` objects. Provider-specific keys are
  normalized here only; the rest of the system sees `InternalSymbol`.
- `import_master_rows(rows)` — accepts already-parsed dicts so a future live
  download can feed parsed rows without coupling to the transport.

**REQUIRES LIVE ACCESS (not executed on Day 4):**
- Actually downloading the master from FYERS (needs `FYERS_ACCESS_TOKEN`).
- Scheduling periodic refresh.
- Validating the live payload shape against these fixtures.

## Why we used a fixture, not a live download

No credentials existed in the Day 4 environment. Downloading would require auth
and would constitute "live testing" we are explicitly not claiming. Instead the
parser was built and validated against a fixture with the documented column
layout (`tests/fixtures/india_fixtures.py::instrument_master_csv`). When
credentials arrive, the integration is a small step:

```python
import requests
csv_text = requests.get(MASTER_URL, headers={"Authorization": f"Bearer {token}"}).text
repo = InstrumentRepository.from_fyers_csv(csv_text)
```

The parser handles equities (`-EQ`), indices (`-INDEX`), and derivatives
(`CE`/`PE`/`FUT` via `OptionType` or suffix), and derives the internal symbol
from the master's own fields so option/future tokens resolve correctly.

## Normalized representation

See `india/instruments.py`: `InternalSymbol` (`EXCHANGE:SYMBOL`), `Instrument`
(exchange, symbol, type, underlying, expiry, strike, option_type, provider_symbol).
This is provider-agnostic — the same repository can later back Angel One / Upstox
masters with a per-provider parser feeding `import_master_rows`.
