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


class QuoteDTO(BaseModel):
    """A live quote (one tick snapshot).

    Wire format contract — every field is explicit.  Numeric values
    are finite real numbers; the wire may carry ``null`` for fields
    the upstream did not provide (e.g. prev close, VWAP).  ``price``
    is required and must be a finite real number greater than 0.
    """

    model_config = ConfigDict(populate_by_name=True)

    symbol: str = Field(description='Internal symbol, e.g. "NSE:SBIN"')
    price: float = Field(gt=0, description="Last trade price (finite, > 0)")
    previousClose: Optional[float] = Field(
        default=None, description="Previous session close (finite or null)"
    )
    change: Optional[float] = Field(
        default=None, description="Absolute change from previous close"
    )
    changePct: Optional[float] = Field(
        default=None, description="Percent change from previous close"
    )
    dayOpen: Optional[float] = Field(default=None)
    dayHigh: Optional[float] = Field(default=None)
    dayLow: Optional[float] = Field(default=None)
    volume: Optional[int] = Field(default=None)
    vwap: Optional[float] = Field(default=None)
    timestamp: int = Field(
        description="Epoch ms of the source tick (UTC)",
    )
    sessionState: str = Field(
        default="REGULAR",
        description="PRE_MARKET | REGULAR | POST_MARKET | CLOSED",
    )
    instrumentToken: Optional[int] = Field(default=None)

    @classmethod
    def from_upstox_quote(
        cls,
        symbol: str,
        quote: dict,
        session_state: str,
        fallback_timestamp_ms: int,
    ) -> "QuoteDTO":
        """Build a QuoteDTO from a normalized Upstox quote dict.

        ``quote`` is the dict returned by ``market_data.fetch_quote``.
        Numeric values that are not finite real numbers are dropped to
        ``None`` rather than zeroed.  ``price`` is required and must
        be finite; the caller must validate before calling.
        """
        import math

        def _f(v) -> Optional[float]:
            if not isinstance(v, (int, float)):
                return None
            fv = float(v)
            if not math.isfinite(fv):
                return None
            return fv

        def _i(v) -> Optional[int]:
            fv = _f(v)
            if fv is None:
                return None
            return int(fv)

        price = _f(quote.get("last_price"))
        if price is None or price <= 0:
            raise ValueError("QuoteDTO requires a finite price > 0")

        prev_close = _f(quote.get("prev_close"))
        change = (
            price - prev_close
            if (price is not None and prev_close is not None)
            else None
        )
        change_pct = (
            (change / prev_close) * 100
            if (change is not None and prev_close not in (None, 0))
            else None
        )

        # Upstox timestamp is epoch seconds; fall back to wall clock if absent.
        ts_s = quote.get("timestamp")
        if isinstance(ts_s, (int, float)) and float(ts_s) == float(ts_s):
            ts_ms = int(float(ts_s) * 1000)
        else:
            ts_ms = fallback_timestamp_ms

        return cls(
            symbol=symbol,
            price=price,
            previousClose=prev_close,
            change=change,
            changePct=change_pct,
            dayOpen=_f(quote.get("open_price")),
            dayHigh=_f(quote.get("high_price")),
            dayLow=_f(quote.get("low_price")),
            volume=_i(quote.get("volume")),
            vwap=_f(quote.get("average_price")),
            timestamp=ts_ms,
            sessionState=session_state,
            instrumentToken=_i(quote.get("instrument_token")),
        )


class MarketStatusDTO(BaseModel):
    """Authoritative Indian market session status.

    Returned by ``/api/market/status``.  The phase is computed from
    the NSE equity session rules in
    :mod:`src.trading_system.india.market_calendar`.
    """

    model_config = ConfigDict(populate_by_name=True)

    market: str = Field(default="NSE", description="Exchange code")
    phase: str = Field(
        description="pre_market | regular | post_market | closed | holiday"
    )
    serverTime: int = Field(description="Epoch ms of the server clock (UTC)")
    nextOpen: Optional[int] = Field(
        default=None, description="Epoch ms of the next session open (UTC)"
    )
    nextClose: Optional[int] = Field(
        default=None, description="Epoch ms of the next session close (UTC)"
    )


class ErrorResponse(BaseModel):
    """Standard error response."""

    error: str
    message: str
