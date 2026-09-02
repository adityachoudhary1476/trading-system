"""API response schemas for market data endpoints."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


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
    """A trading signal."""

    id: str
    symbol: str
    direction: str  # long, short, hold, no_signal
    confidence: float = Field(ge=0, le=1)
    generated_at: int  # epoch ms
    price: float
    bias: str  # bullish, bearish, neutral, choppy
    reason: str
    source: str


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
