# Day 12 — Paper Trading Engine

A production-quality, **simulation-only** paper trading engine. It executes orders
against an in-memory virtual portfolio and **never places real broker orders, calls
FYERS/Upstox, or touches live configuration**.

## Architecture

```
Market Data ──► Strategy ──► Signal ──► Risk Manager ──► Order ──► Broker ──► Fill ──► Portfolio
                                    (Broker ONLY executes; it never decides)
```

The `Broker` abstraction is provider-independent. Today there is one implementation,
`PaperBroker`. Future brokers (`FyersBroker`, `UpstoxBroker`) will implement the same
`Broker` interface so strategies stay agnostic to the venue.

```
Broker (ABC)
├── PaperBroker        ✅ implemented (this milestone)
├── FyersBroker        ⏳ future — must implement Broker, place REAL orders
└── UpstoxBroker       ⏳ future

MarketDataProvider (pre-existing)
├── HistoricalDataProvider
├── PaperDataProvider
├── FyersMarketDataProvider
└── UpstoxMarketDataProvider
```

## Files

| File | Role |
|---|---|
| `src/trading_system/execution/__init__.py` | Package exports |
| `execution/orders.py` | `Order`, `Fill`, `Side`, `OrderType`, `OrderStatus`, `OrderStateMachine` |
| `execution/broker.py` | `Broker` ABC, `CostModel` protocol, `AccountSnapshot`, `BrokerError` |
| `execution/paper_broker.py` | `PaperBroker`, `SlippageConfig`, `SimpleCostModel` |
| `paper_trading/__init__.py` | `Position`, `PaperAccount` dataclasses (reused by the broker) |
| `tests/test_paper_broker.py` | 34 deterministic tests |

## Order lifecycle

Explicit states with a strict state machine (`OrderStateMachine`):

```
PENDING ──► OPEN ──► FILLED           (terminal)
              ├──► PARTIALLY_FILLED ──► FILLED   (terminal)
              ├──► CANCELLED          (terminal)
              └──► REJECTED           (terminal)
```

Invalid transitions (e.g. `CANCELLED → FILLED`, `FILLED → OPEN`, filling a `REJECTED`
order) raise `InvalidOrderTransition`. Orders are never silently mutated.

## Fill assumptions

- **MARKET** orders fill immediately at the supplied/last market price.
- **LIMIT** orders rest OPEN and are evaluated on every `update_market_price`:
  - BUY LIMIT fills when `market_price <= limit_price`
  - SELL LIMIT fills when `market_price >= limit_price`
- No fills occur without the condition being satisfied. A cancelled order cannot fill.

## Slippage model

Configurable via `SlippageConfig(slippage_bps=...)`. Applied **adversely and
deterministically** (no randomness):

- BUY executes at `market * (1 + slippage_bps/10000)`
- SELL executes at `market * (1 - slippage_bps/10000)`

Default is **5 bps** (conservative but reasonable). Not hard-coded throughout the
codebase — it is a single constructor parameter on `PaperBroker`.

## Cost model

The broker accepts any object satisfying the `CostModel` protocol
(`estimate_fill_fee(symbol, side, price, quantity) -> float`). Two implementations:

1. `SimpleCostModel(fee_bps=, min_fee=)` — provider-independent default (NOT
   FYERS-specific). `fee = max(min_fee, fee_bps * notional)`.
2. `research.costs.IndiaTransactionCostModel` — the repo's real, tested India
   schedule (brokerage/STT/GST/stamp). It satisfies the same protocol and can be
   injected for realistic statutory charges. **The paper broker never hard-codes
   FYERS-specific charges itself.**

Fees reduce cash but are kept separate from `realized_pnl` (gross). Net P&L is
`equity - initial_cash`.

## Position accounting

Per-instrument `Position` with signed `qty` (positive long, negative short) and a
running average entry price. Correctly handles:

- **Open**: qty set, avg entry = fill price.
- **Average up/down**: `avg_entry = (old_avg*|old| + price*|new|) / |new_total|`.
  e.g. BUY 10 @100 + BUY 10 @110 → qty 20, avg 105.
- **Partial close**: realized PnL booked on the closed portion; avg entry unchanged.
- **Full close**: position flattened; avg entry reset to 0.
- **Reverse**: closing the old side plus opening the opposite; leftover carries the
  new fill price as its entry.

Realized PnL on a reduce/close:
- Long being sold: `(sell_price - avg_entry) * closed_qty`
- Short being bought: `(avg_entry - buy_price) * closed_qty`

Unrealized PnL = `(current_price - avg_entry) * qty`, marked on every price update.

## Account / portfolio

`PaperAccount` holds `initial_cash`, `cash`, `realized_pnl`, `unrealized_pnl`,
`margin_used` (simple, default 0 — **not** real FYERS margin). `account()` returns an
`AccountSnapshot` with `equity = cash + unrealized_pnl` and `available_cash = cash`.

**Paper accounting is explicitly separated from real broker margin requirements.**

## Using the paper broker

```python
from trading_system.execution import PaperBroker, SlippageConfig

broker = PaperBroker(initial_cash=100_000, slippage=SlippageConfig(0.0))
broker.update_market_price("NSE:RELIANCE-EQ", 2500)

order = broker.submit_order(
    symbol="NSE:RELIANCE-EQ", side="BUY", quantity=10, order_type="MARKET"
)
broker.update_market_price("NSE:RELIANCE-EQ", 2520)

order                      # FILLED, avg_fill_price 2500
broker.get_position("NSE:RELIANCE-EQ")   # qty 10, avg_entry 2500
broker.account().cash      # 75_000
broker.account().equity    # 75_000 + 20*10 = 75_200
broker.account().realized_pnl  # 0.0
broker.account().unrealized_pnl  # 200.0
```

For realistic India charges, inject the existing model:

```python
from trading_system.research.costs import IndiaTransactionCostModel
broker = PaperBroker(cost_model=IndiaTransactionCostModel())
```

## Feeding prices

`broker.update_market_price(symbol, price)` is the single entry point. The historical
backtest engine and a future live market-data engine both feed prices here, which:
1. marks open positions (unrealized PnL),
2. evaluates eligible resting LIMIT orders,
3. generates fills where conditions are met.

## CLI

```
python -m trading_system paper-status
```

Prints READY / mode PAPER / live_orders DISABLED and a clear statement that the engine
cannot place real orders. No command in this engine can submit a live order.

## Safety

- `PaperBroker` imports **nothing** from `india.fyers` or `india.token_manager`.
- No `fyers_apiv3` import, no `validate-authcode`, no `orders/sync` endpoint.
- `.env` is never read or modified by the broker.
- Strategy logic is excluded: the broker executes orders, it does not generate them.
- Verified by `tests/test_paper_broker.py::TestIsolation`.

## Limitations (be aware)

- Execution is an **approximation**: immediate MARKET fills, adverse slippage only,
  next-tick LIMIT fills. It does **not** reproduce real exchange matching, partial
  fills from liquidity, intrabar highs/lows, or FYERS margin rules.
- Margin model is a simple placeholder (`margin_used = 0`); leverage is not modeled.
- Persistence is in-memory only (by design for v1); a clean future extension can add
  it without changing the interfaces.
- This is a simulation. Do not treat its P&L as a forecast of real trading outcomes.
