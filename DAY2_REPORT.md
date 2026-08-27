# Day 2 — AI Analyst + Deterministic Signal Architecture

## 1. Files created/modified

**Created**
- `src/trading_system/models/snapshot.py` — `MarketSnapshot` (Pydantic) + `build_snapshot_from_df`.
- `src/trading_system/models/market_view.py` — `MarketView` (strict validated output).
- `src/trading_system/models/base.py` — `ModelProvider` ABC + `ModelProviderError`.
- `src/trading_system/models/local.py` — `LocalRuleModel` (offline, deterministic, tested).
- `src/trading_system/models/openai_compatible.py` — `OpenAICompatibleProvider` (real client, untested — no key).
- `src/trading_system/models/provider_factory.py` — `get_model_provider`.
- `src/trading_system/models/analyst.py` — orchestration (snapshot → provider → view → signal).
- `src/trading_system/signals/__init__.py` — deterministic signal engine.
- `tests/test_models.py` — 26 new tests.
- `docs/AI_ANALYST.md`, `DAY2_REPORT.md`.

**Modified**
- `src/trading_system/models/__init__.py` — re-exports.
- `src/trading_system/__main__.py` — added `ai-analyze` subcommand.
- `.env.example` — AI provider config.
- `ARCHITECTURE.md` — added AI analyst + signal layers.

## 2. Features implemented

- **MarketSnapshot**: typed, validated, tz-aware, no-look-ahead input envelope for the AI (price, returns, MAs, RSI/MACD/ATR/Bollinger, volatility, drawdown, volume stats, recent closes). Built only from closed-bar history.
- **ModelProvider abstraction**: vendor-agnostic; app depends only on the interface.
- **LocalRuleModel**: a real, offline, deterministic provider (no key/GPU/network). Used as the Day 2 reference + guaranteed-testable path.
- **OpenAICompatibleProvider**: real implementation (OpenAI / Ollama-shim / any compatible endpoint). Untested here because no credential exists; fails loudly, never fabricates.
- **MarketView**: strict schema (enums, confidence 0..1, required factors, reasoning). Unvalidated model text cannot enter the system — `from_model_json` is the single choke point.
- **Deterministic signal engine**: combines the validated view + fixed indicator rules → LONG/SHORT/HOLD, with a recorded `reason`. AI confidence is only an analytical gate; it can never be the sole authority or bypass risk.
- **CLI `ai-analyze`**: snapshot → AI view → signal, labeled **ANALYSIS / PAPER ONLY**, no execution.

## 3. AI provider tested

`LocalRuleModel` (offline, deterministic) — genuinely executed and asserted.

## 4. Exact model used

`local-rule` (a fixed indicator heuristic). It is explicitly NOT a predictive/trading model; it exists to exercise the full pipeline offline and deterministically.

## 5. Local or API-based

Local (in-process, deterministic). The OpenAI-compatible path is implemented but API-based and **untested** (no key in this environment).

## 6. Test results

**60 passed, 0 failed** (34 Day 1 + 26 Day 2). Includes snapshot/view validation, malformed-AI handling, missing/invalid-enum fields, provider interface, deterministic signals, and no-look-ahead constraints.

## 7. Problems encountered

- **Pandas `freq="1d"` deprecation warning** in tests (cosmetic; harmless, left as-is to avoid churn).
- **No AI credential / no Ollama** in environment → could not live-test an LLM. Resolved honestly by implementing the interface fully, shipping a working local provider, and documenting the OpenAI path as untested rather than faking a response.
- Snapshot validation required care so the `recent_closes` tail equals `latest_price` and `timestamp == last_bar_timestamp` (no look-ahead). Enforced in the Pydantic model, not just in code comments.

## 8. Known limitations

- `LocalRuleModel` is a placeholder heuristic, not an actual model; its views are for architecture validation only.
- `OpenAICompatibleProvider` is unverified (no key). Its JSON contract and validation path are correct by construction but not runtime-proven.
- No confidence calibration: AI `confidence` is an analytical score, explicitly **not** a probability of profit (documented in code + docs).
- AI input is structured snapshot only; no news/order-flow/fundamentals yet.
- No risk engine, backtester, or paper-trader execution yet (scaffolded for Day 3).

## 9. Example MarketSnapshot

```python
MarketSnapshot(
    symbol='BTCUSDT', timeframe='1d',
    timestamp=datetime(2026,8,27,tzinfo=UTC),
    last_bar_timestamp=datetime(2026,8,27,tzinfo=UTC),  # == timestamp (no future)
    latest_price=78805.7, last_return=0.012,
    sma_20=69200.1, ema_12=70100.3, rsi_14=79.4,
    macd=420.0, macd_signal=300.0, macd_hist=120.0,
    atr_14=2100.0, bollinger_upper=81000.0, bollinger_lower=59000.0,
    volatility_annualized=0.44, max_drawdown=-0.53,
    volume_ma=12345.0, volume_z=0.2, price_vs_sma20=0.139,
    recent_closes=[..., 78805.7], data_points=365,
    lookahead_safe=True,
)
```

## 10. Example validated MarketView (from local-rule on BTCUSDT)

```json
{
  "symbol": "BTCUSDT", "timeframe": "1d",
  "market_view": "neutral", "confidence": 0.65,
  "reasoning_summary": "Local rule model: 2 bullish / 1 bearish signals; net score 1.",
  "bullish_factors": ["price 13.1% above SMA20 (uptrend)", "MACD above signal line"],
  "bearish_factors": ["RSI 79.4 overbought"],
  "risks": ["Heuristic only; not a predictive model.", "Ignores news, order flow, and regime changes."],
  "invalidating_conditions": ["A break of structure invalidates the current read."],
  "model": "local-rule"
}
```

## 11. Example signal

```json
{
  "symbol": "BTCUSDT", "timeframe": "1d",
  "timestamp": "2026-08-27 00:00:00+00:00",
  "direction": "hold", "confidence": 0.65,
  "source": "deterministic",
  "reason": "AI view neutral",
  "market_view": "neutral"
}
```

## 12. Day 3 recommendation

1. **Risk engine**: position sizing, stop/take-profit, exposure caps, gating between signal and paper trader.
2. **Backtester**: replay `generate_signal` over stored history with walk-forward, point-in-time snapshots built only from past data (look-ahead guard already in `MarketSnapshot`).
3. **Real LLM integration** (optional): install Ollama or supply an OpenAI-compatible key; point `AI_PROVIDER=openai-compatible`, verify `OpenAICompatibleProvider` end-to-end, and add a regression test with a captured fixture response.
4. **Paper trader**: consume approved signals + risk approval into a simulated account.
5. **Confidence calibration**: collect outcomes over time and recalibrate the analytical-confidence score so it is not mistaken for win probability.
