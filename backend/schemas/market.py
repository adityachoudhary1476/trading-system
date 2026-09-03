"""API response schemas for market data endpoints."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class FactorDTO(BaseModel):
    """A single analysis factor."""

    label: str
    value: str
    tone: str  # positive, negative, neutral, warning


class AIAnalysisDTO(BaseModel):
    """AI market analysis response."""

    symbol: str
    timeframe: str
    bias: str  # bullish, bearish, neutral, choppy
    confidence: float = Field(ge=0, le=1)
    signal: str  # long, short, hold, no_signal
    summary: str
    factors: list[FactorDTO] = Field(default_factory=list)
    generated_at: int  # epoch ms
    model: str


class SignalDTO(BaseModel):
    """A trading signal.

    Contract (all fields are required and non-null on the wire):

    * ``id``        — unique identifier (uuid4 string)
    * ``symbol``    — internal symbol (e.g. ``"NSE:SBIN"``)
    * ``direction`` — one of ``long`` / ``short`` / ``hold`` / ``no_signal``
    * ``confidence`` — analytical confidence in ``[0, 1]``
    * ``price``     — finite close of the source candle (never ``0``/``None``)
    * ``bias``      — AI market view: ``bullish`` / ``bearish`` / ``neutral`` / ``choppy``
    * ``reason``    — natural-language reason from the strategy engine
    * ``timestamp`` — epoch ms of the source candle (tz-aware UTC origin)
    * ``source``    — model identifier (e.g. ``"deterministic"``)

    The ``generatedAt`` alias exists so the wire format matches the
    existing frontend ``Signal`` interface (camelCase).  The route uses
    ``response_model_by_alias=True`` to emit ``generatedAt`` in JSON.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(description="Unique signal id (uuid4)")
    symbol: str = Field(description='Internal symbol, e.g. "NSE:SBIN"')
    direction: str = Field(description="long | short | hold | no_signal")
    confidence: float = Field(ge=0, le=1, description="Analytical confidence in [0, 1]")
    price: float = Field(description="Finite close price of the source candle")
    bias: str = Field(description="bullish | bearish | neutral | choppy")
    reason: str = Field(description="Natural-language reason for the decision")
    timestamp: int = Field(
        description="Epoch ms of the source candle (tz-aware UTC)",
        serialization_alias="generatedAt",
    )
    source: str = Field(description="Originating strategy/model identifier")


class PipelineStageDTO(BaseModel):
    """A pipeline stage status."""

    id: str
    label: str
    status: str  # connected, healthy, ready, disconnected, stale, auth_error, invalid_data
    last_activity: Optional[int] = None  # epoch ms
    metric: str


class ErrorResponse(BaseModel):
    """Standard error response."""

    error: str
    message: str
