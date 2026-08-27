# AI Analyst — Design & Operating Notes

The AI is an **analyst / decision-support** component. It answers: *"Given this
structured market information, what is your interpretation?"* It does **not** decide
position size, leverage, stops, allocation, or whether to trade. Those are
deterministic components (signal engine, risk engine, paper trader) that do not
exist as trading authorities inside the model.

## Pipeline position

```
... INDICATORS → MarketSnapshot → ModelProvider → MarketView → SignalEngine → (Risk → Backtest → Paper)
```

The AI sits between the snapshot and the signal. It receives only validated,
historical data and returns only a validated `MarketView`.

## Input schema: MarketSnapshot

Built by `build_snapshot_from_df` from stored OHLCV. Contains: symbol, timeframe,
decision `timestamp` (= last **closed** bar), latest price, last return, SMA/EMA,
RSI, MACD (+signal+hist), ATR, Bollinger bands, annualized volatility, max
drawdown, volume MA + z-score, price-vs-SMA20, and a short tail of `recent_closes`.
All timestamps are tz-aware UTC.

**No-look-ahead guarantees (enforced in Pydantic, not just comments):**
- `timestamp` MUST equal `last_bar_timestamp`.
- `recent_closes[-1]` MUST equal `latest_price`.
- The frame is built from `iloc[:-0]` of available history — only closed bars.
- `lookahead_safe` must be `True` or construction is rejected.

## Output schema: MarketView

`MarketView` (Pydantic, `extra="forbid"`):
- `market_view`: enum {bullish, bearish, neutral, choppy}
- `confidence`: float [0,1] — **analytical confidence, NOT probability of profit**
- `reasoning_summary`, `bullish_factors`, `bearish_factors`, `risks`,
  `invalidating_conditions`: lists/strings
- `model`, `generated_at`: provenance/audit

Constraints: bullish/bearish views require ≥1 matching factor; high confidence
(≥0.8) requires substantive reasoning. Malformed/partial input → `ValidationError`
→ never enters the system.

## ModelProvider interface

```python
class ModelProvider(ABC):
    name: str
    def analyze(self, snapshot: MarketSnapshot) -> MarketView: ...
    @property
    def is_available(self) -> bool: ...
```

Implementations:
- **LocalRuleModel** (`local`) — offline, deterministic, tested. A fixed heuristic
  over indicators. Used as the guaranteed-testable Day 2 reference provider.
- **OpenAICompatibleProvider** (`openai-compatible`) — real client using
  `requests` against `{AI_API_BASE}/chat/completions`, `response_format: json_object`,
  system prompt enforcing the MarketView schema. Untested in this environment
  (no key); fails loudly via `ModelProviderError` if unavailable.

Select via `get_model_provider(name)` — the app never imports a concrete class directly.

## How AI output is validated

`MarketView.from_model_json(data, model=...)` is the **single choke point**. Any
untrusted model JSON is parsed and schema-validated here. Failures raise
`ModelProviderError` and are surfaced by `analyze_snapshot` (never swallowed).

## How AI differs from deterministic trading logic

- The AI returns an **interpretation**; the **SignalEngine** (deterministic, in
  `signals/`) makes the LONG/SHORT/HOLD decision using fixed rules over the
  snapshot + view.
- AI `confidence` only gates whether the deterministic engine may act (≥ a
  configured threshold). It cannot set size, override rules, or bypass risk.
- The engine logs a `reason` for every signal; the AI's text is context, not a command.

## Known failure modes

1. **Model returns non-JSON / schema-violating text** → `ModelProviderError`;
   pipeline reports the error, emits no signal.
2. **Provider unavailable (no key / Ollama down)** → controlled error, no fake data.
3. **Overconfident but vague output** → blocked by the high-confidence reasoning rule.
4. **Look-ahead if a caller passes future bars** → rejected by snapshot validation.
5. **LocalRuleModel is a placeholder**, not a predictive model — its views are for
   architecture validation only and must not be mistaken for trading edge.
6. **Confidence ≠ win probability** — no calibration yet; treat the score as
   analytical only.
