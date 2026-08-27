"""Strict structured output returned by the AI analyst (MarketView).

The AI is an analyst/decision-support component. It returns an *interpretation*,
never an order. All fields are constrained so malformed output fails validation
instead of flowing into trading logic.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import (
    BaseModel,
    Field,
    ConfigDict,
    field_validator,
    model_validator,
)


class MarketViewEnum(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    CHOPPY = "choppy"


class MarketView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe: str
    # The AI's directional interpretation — NOT a trading command.
    market_view: MarketViewEnum
    # Analytical confidence 0..1. See note on confidence below.
    confidence: float = Field(ge=0.0, le=1.0)

    reasoning_summary: str = Field(min_length=1, max_length=2000)
    bullish_factors: list[str] = Field(default_factory=list)
    bearish_factors: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    invalidating_conditions: list[str] = Field(default_factory=list)

    # Which model produced this view (provenance / audit).
    model: str = "unknown"
    # Timestamp the analysis was produced (tz-aware UTC).
    generated_at: Optional[str] = None

    @field_validator("confidence")
    @classmethod
    def _conf(cls, v):
        # Confidence is an *analytical* score, not a probability of profit.
        return round(float(v), 4)

    @model_validator(mode="after")
    def _consistent(self) -> "MarketView":
        # Bullish/Bearish views should carry at least one supporting factor.
        if self.market_view == MarketViewEnum.BULLISH and not self.bullish_factors:
            raise ValueError("bullish view requires at least one bullish_factor")
        if self.market_view == MarketViewEnum.BEARISH and not self.bearish_factors:
            raise ValueError("bearish view requires at least one bearish_factor")
        # High confidence with no reasoning is not allowed.
        if self.confidence >= 0.8 and len(self.reasoning_summary) < 20:
            raise ValueError("high confidence requires substantive reasoning")
        return self

    @classmethod
    def from_model_json(cls, data: dict, model: str = "unknown") -> "MarketView":
        """Parse untrusted model JSON into a validated MarketView.

        Raises pydantic.ValidationError on malformed/partial output. This is the
        single choke point that keeps arbitrary LLM text out of the system.
        """
        if not isinstance(data, dict):
            raise TypeError("model output must be a JSON object")
        data = dict(data)
        data.setdefault("model", model)
        return cls(**data)
