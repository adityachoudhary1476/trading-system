"""AI strategy proposal providers (Phase 13, Step 6).

A StrategyProposalProvider turns a read-only GenerationContext (a summary of
what the data looks like) into a VALIDATED StrategySpec. Providers are the ONLY
new-AI entry point and are strictly separated from execution:

  * No provider imports or touches Broker / PaperBroker / FYERS / orders.
  * No provider sees a position, an account, or an order API — only the
    GenerationContext summary.
  * DeterministicStrategyProvider is fully offline (no API key, no network) and
    is the guaranteed-testable path, mirroring LocalRuleModel for MarketView.
  * OpenAICompatibleStrategyProvider SUBCLASSES the existing
    OpenAICompatibleProvider (same client, same auth plumbing, same strict
    JSON contract) instead of introducing a second LLM abstraction. Malformed
    model output becomes ModelProviderError — never a spec.

The proposal is a RESEARCH HYPOTHESIS. Nothing here places orders or claims
future profitability.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import Optional

from ..dataset import HistoricalDataset
from ...models.base import ModelProviderError
from ...models.openai_compatible import OpenAICompatibleProvider
from .spec import (
    StrategySpec,
    const_operand,
    field_operand,
    indicator_operand,
    logic,
    make_condition,
    not_,
)


@dataclass(frozen=True)
class GenerationContext:
    """Read-only dataset summary handed to a strategy-proposal provider.

    Contains NO prices history beyond aggregates, NO account state, and NO
    execution capability — the AI cannot see or reach the broker through it.
    ``variant_index`` lets a provider propose DIFFERENT deterministic
    candidates across engine iterations (0, 1, 2, ...).
    """

    symbol: str
    timeframe: str
    contract_id: str = ""
    rows: int = 0
    date_start: Optional[str] = None
    date_end: Optional[str] = None
    feature_summary: dict = field(default_factory=dict)
    variant_index: int = 0

    @classmethod
    def from_dataset(
        cls, dataset: HistoricalDataset, variant_index: int = 0
    ) -> "GenerationContext":
        data = dataset.data
        start = end = None
        summary: dict = {}
        if data is not None and len(data):
            start = str(data.index.min())
            end = str(data.index.max())
            close = data["close"]
            summary = {
                "close_min": round(float(close.min()), 6),
                "close_max": round(float(close.max()), 6),
                "rows": int(len(data)),
            }
        return cls(
            symbol=dataset.symbol,
            timeframe=dataset.timeframe,
            contract_id=dataset.contract_id,
            rows=0 if data is None else int(len(data)),
            date_start=start,
            date_end=end,
            feature_summary=summary,
            variant_index=variant_index,
        )

    def with_variant(self, variant_index: int) -> "GenerationContext":
        return replace(self, variant_index=variant_index)

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "contract_id": self.contract_id,
            "rows": self.rows,
            "date_start": self.date_start,
            "date_end": self.date_end,
            "feature_summary": dict(self.feature_summary),
            "variant_index": self.variant_index,
        }


class StrategyProposalProvider(ABC):
    """Interface for AI strategy proposal — produce a VALIDATED StrategySpec."""

    name: str = "abstract"

    @abstractmethod
    def generate_strategy(self, context: GenerationContext) -> StrategySpec:
        """Propose one strategy spec for the context. Must be deterministic
        for a given context and fail safely (raise) on unusable output."""
        raise NotImplementedError

    @property
    def is_available(self) -> bool:
        return True


# --------------------------------------------------------------------------- #
# Deterministic offline provider (no API key, no network, no randomness)
# --------------------------------------------------------------------------- #
def _spec_ema_cross(ctx: GenerationContext) -> dict:
    return {
        "name": "EMA cross 12-26",
        "description": "LONG when fast EMA crosses above slow EMA; exit on cross below.",
        "symbol": ctx.symbol,
        "timeframe": ctx.timeframe,
        "indicators": [
            {"name": "ema", "params": {"window": 12}},
            {"name": "ema", "params": {"window": 26}},
        ],
        "entry": make_condition(
            indicator_operand("ema_12"), "crosses_above", indicator_operand("ema_26")
        ),
        "exit": make_condition(
            indicator_operand("ema_12"), "crosses_below", indicator_operand("ema_26")
        ),
        "position_sizing": {"max_allocation_pct": 0.95},
        "risk": {"stop_loss_pct": 0.05, "take_profit_pct": 0.15},
    }


def _spec_ema_cross_short(ctx: GenerationContext) -> dict:
    spec = _spec_ema_cross(ctx)
    spec["name"] = "EMA cross 12-26 long-short"
    spec["description"] = (
        "LONG on fast-over-slow EMA cross; SHORT on fast-under-slow cross."
    )
    spec["entry_short"] = make_condition(
        indicator_operand("ema_12"), "crosses_below", indicator_operand("ema_26")
    )
    spec["risk"] = {"stop_loss_pct": 0.05, "take_profit_pct": 0.15, "allow_short": True}
    return spec


def _spec_sma_trend(ctx: GenerationContext) -> dict:
    return {
        "name": "SMA20 trend filter",
        "description": "LONG while close holds above its SMA20; exit below.",
        "symbol": ctx.symbol,
        "timeframe": ctx.timeframe,
        "indicators": [{"name": "sma", "params": {"window": 20}}],
        "entry": make_condition(
            field_operand("close"), ">", indicator_operand("sma_20")
        ),
        "exit": make_condition(
            field_operand("close"), "<", indicator_operand("sma_20")
        ),
        "risk": {"stop_loss_pct": 0.07},
    }


def _spec_rsi_reversion(ctx: GenerationContext) -> dict:
    return {
        "name": "RSI mean reversion",
        "description": "LONG when RSI14 crosses up through 30 (oversold); exit above 55.",
        "symbol": ctx.symbol,
        "timeframe": ctx.timeframe,
        "indicators": [{"name": "rsi", "params": {"window": 14}}],
        "entry": make_condition(
            indicator_operand("rsi_14"), "crosses_above", const_operand(30.0)
        ),
        "exit": make_condition(indicator_operand("rsi_14"), ">", const_operand(55.0)),
        "risk": {"stop_loss_pct": 0.04, "take_profit_pct": 0.08},
    }


def _spec_momentum(ctx: GenerationContext) -> dict:
    return {
        "name": "Momentum breakout",
        "description": "LONG when 10-bar momentum exceeds +2%; exit when it turns negative.",
        "symbol": ctx.symbol,
        "timeframe": ctx.timeframe,
        "indicators": [{"name": "momentum", "params": {"window": 10}}],
        "entry": make_condition(
            indicator_operand("momentum_10"), ">", const_operand(0.02)
        ),
        "exit": make_condition(
            indicator_operand("momentum_10"), "<", const_operand(0.0)
        ),
        "risk": {"stop_loss_pct": 0.06},
    }


def _spec_bollinger_squeeze(ctx: GenerationContext) -> dict:
    entry = logic(
        "AND",
        make_condition(field_operand("close"), "<", indicator_operand("bb_lower_20_2")),
        make_condition(indicator_operand("rsi_14"), "<", const_operand(40.0)),
    )
    return {
        "name": "Bollinger RSI pullback",
        "description": "LONG when close is under the lower band AND RSI confirms weakness.",
        "symbol": ctx.symbol,
        "timeframe": ctx.timeframe,
        "indicators": [
            {"name": "bb_lower", "params": {"window": 20, "num_std": 2.0}},
            {"name": "rsi", "params": {"window": 14}},
        ],
        "entry": entry,
        "exit": make_condition(
            field_operand("close"), ">", indicator_operand("bb_lower_20_2")
        ),
        "position_sizing": {"max_allocation_pct": 0.5},
        "risk": {"stop_loss_pct": 0.05, "take_profit_pct": 0.10},
    }


def _spec_macd_trend(ctx: GenerationContext) -> dict:
    return {
        "name": "MACD trend confirmation",
        "description": "LONG when MACD line is above its signal line and momentum is positive.",
        "symbol": ctx.symbol,
        "timeframe": ctx.timeframe,
        "indicators": [
            {"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"name": "macd_signal", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"name": "momentum", "params": {"window": 10}},
        ],
        "entry": logic(
            "AND",
            make_condition(
                indicator_operand("macd_12_26_9"), ">",
                indicator_operand("macd_signal_12_26_9"),
            ),
            make_condition(
                indicator_operand("momentum_10"), ">", const_operand(0.0)
            ),
        ),
        "exit": make_condition(
            indicator_operand("macd_12_26_9"), "<", indicator_operand("macd_signal_12_26_9")
        ),
        "risk": {"stop_loss_pct": 0.05},
    }


def _spec_not_condition(ctx: GenerationContext) -> dict:
    """Demonstrates the NOT primitive: long while close is NOT below SMA50."""
    return {
        "name": "Hold above SMA50",
        "description": "LONG while close is NOT under its SMA50; exit when it is.",
        "symbol": ctx.symbol,
        "timeframe": ctx.timeframe,
        "indicators": [{"name": "sma", "params": {"window": 50}}],
        "entry": not_(
            make_condition(field_operand("close"), "<", indicator_operand("sma_50"))
        ),
        "exit": make_condition(
            field_operand("close"), "<", indicator_operand("sma_50")
        ),
        "risk": {},
    }


_DETERMINISTIC_CATALOG = [
    _spec_ema_cross,
    _spec_sma_trend,
    _spec_rsi_reversion,
    _spec_momentum,
    _spec_macd_trend,
    _spec_bollinger_squeeze,
    _spec_ema_cross_short,
    _spec_not_condition,
]


class DeterministicStrategyProvider(StrategyProposalProvider):
    """Offline, deterministic, no-key provider of KNOWN StrategySpecs.

    Mirrors LocalRuleModel's role for the MarketView layer: a real
    implementation of the provider interface used for tests, demos, and the
    guaranteed-offline path. ``variant_index`` selects the catalog entry
    (modulo catalog size), so the same context always yields the same spec.
    """

    name = "deterministic-mock"

    @property
    def catalog_size(self) -> int:
        return len(_DETERMINISTIC_CATALOG)

    def generate_strategy(self, context: GenerationContext) -> StrategySpec:
        builder = _DETERMINISTIC_CATALOG[
            context.variant_index % len(_DETERMINISTIC_CATALOG)
        ]
        payload = builder(context)
        # Catalog payloads are plain dicts; route them through the SAME
        # validation choke point an LLM response would use (never blind).
        return StrategySpec.from_model_json(payload, model=self.name)


# --------------------------------------------------------------------------- #
# OpenAI-compatible provider (extends the EXISTING LLM client)
# --------------------------------------------------------------------------- #
_STRATEGY_SYSTEM_PROMPT = (
    "You are a strategy-RESEARCH assistant for a deterministic backtesting "
    "engine. You propose trading-strategy hypotheses as STRICT JSON matching "
    "the StrategySpec schema below. You are NOT a trader: you have no broker, "
    "no orders, no account access, and your output is only ever backtested on "
    "historical data.\n"
    "\n"
    "Schema:\n"
    "{\n"
    '  "name": str (short, letters/digits/space/underscore/dash),\n'
    '  "description": str,\n'
    '  "symbol": str, "timeframe": str,\n'
    '  "indicators": [{"name": "sma"|"ema"|"rsi"|"atr"|"macd"|"macd_signal"|'
    '"macd_histogram"|"bb_upper"|"bb_middle"|"bb_lower"|"momentum", '
    '"params": {...} }],\n'
    '  "entry": <condition>, "entry_short": <condition>|null, "exit": <condition>|null,\n'
    '  "allow_long": bool,\n'
    '  "position_sizing": {"max_allocation_pct": float in (0,1]},\n'
    '  "risk": {"stop_loss_pct": float in (0,1)|null, "take_profit_pct": float in (0,1)|null,'
    ' "allow_short": bool}\n'
    "}\n"
    "\n"
    "A condition is one of:\n"
    '  {"type": "comparison", "left": <operand>, "op": ">"|"<"|">="|"<="|"=="|'
    '"crosses_above"|"crosses_below", "right": <operand>}\n'
    '  {"type": "logic", "op": "AND"|"OR", "conditions": [<condition>, ...]}\n'
    '  {"type": "not", "condition": <condition>}\n'
    "An operand is one of:\n"
    '  {"kind": "field", "field": "open"|"high"|"low"|"close"|"volume"}\n'
    '  {"kind": "indicator", "indicator": "<canonical key, e.g. sma_20>"}\n'
    '  {"kind": "constant", "constant": <number>}\n'
    "\n"
    "HARD RULES: only the listed indicators; only the listed operators; NO "
    "Python, NO code, NO unknown fields; every referenced indicator MUST be "
    "declared in 'indicators'; if risk.allow_short is true you MUST provide "
    "'entry_short'. Return ONLY the JSON object."
)


class OpenAICompatibleStrategyProvider(OpenAICompatibleProvider):
    """StrategySpec generation over the EXISTING OpenAI-compatible client.

    Extends (does not replace) ``OpenAICompatibleProvider``: same model/auth/
    availability plumbing, same strict-JSON contract. Malformed output is
    converted to ModelProviderError — a bad response can NEVER become a spec.
    """

    name = "openai-compatible-strategy"

    def generate_strategy(self, context: GenerationContext) -> StrategySpec:
        if not self.is_available:
            raise ModelProviderError(
                "OpenAICompatibleStrategyProvider unavailable: set AI_API_KEY_ENV "
                "and the corresponding environment variable."
            )
        user_payload = json.dumps(context.as_dict(), indent=2, default=str)
        content = self._post_chat(_STRATEGY_SYSTEM_PROMPT, user_payload)
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise ModelProviderError(f"model returned non-JSON: {e}")
        # Single validation choke point — arbitrary text cannot become a spec.
        try:
            return StrategySpec.from_model_json(data, model=self.model)
        except Exception as e:
            raise ModelProviderError(f"model output failed StrategySpec validation: {e}")



