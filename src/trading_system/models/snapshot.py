"""Canonical MarketSnapshot — structured, validated input for the AI analyst.

This is the ONLY thing the AI receives. It is built from validated, stored data
and contains strictly historical information available *as of the decision
timestamp*. It must never contain future candles or the not-yet-closed bar.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class MarketSnapshot(BaseModel):
    model_config = {"extra": "forbid"}

    symbol: str
    timeframe: str
    # Decision timestamp: tz-aware UTC. Must equal the last available (closed) bar.
    timestamp: datetime
    # The timestamp of the most recent bar included. Look-ahead invariant:
    # timestamp MUST equal last_bar_timestamp.
    last_bar_timestamp: datetime

    latest_price: float = Field(gt=0)
    last_return: float = 0.0  # single-bar return of the decision bar

    sma_20: Optional[float] = None
    sma_50: Optional[float] = None
    ema_12: Optional[float] = None
    rsi_14: Optional[float] = Field(default=None, ge=0, le=100)
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    atr_14: Optional[float] = Field(default=None, ge=0)
    bollinger_upper: Optional[float] = None
    bollinger_lower: Optional[float] = None

    volatility_annualized: Optional[float] = Field(default=None, ge=0)
    max_drawdown: Optional[float] = Field(default=None, le=0)  # drawdown <= 0
    volume_ma: Optional[float] = Field(default=None, ge=0)
    volume_z: Optional[float] = None
    price_vs_sma20: Optional[float] = None  # (price - sma20) / sma20

    recent_closes: list[float] = Field(default_factory=list, max_length=120)
    data_points: int = Field(ge=1)
    data_start: Optional[datetime] = None
    data_end: Optional[datetime] = None

    # Explicit, schema-level no-look-ahead flag. Construction must set this True.
    lookahead_safe: bool = True

    @field_validator("timestamp", "last_bar_timestamp", "data_start", "data_end")
    @classmethod
    def _tz_aware(cls, v):
        if v is not None and v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (UTC)")
        return v

    @model_validator(mode="after")
    def _no_lookahead(self) -> "MarketSnapshot":
        # The decision point cannot be after the most recent data we have.
        if self.timestamp != self.last_bar_timestamp:
            raise ValueError(
                "timestamp must equal last_bar_timestamp (no future data allowed)"
            )
        if self.recent_closes:
            if abs(self.recent_closes[-1] - self.latest_price) > 1e-6:
                raise ValueError(
                    "last element of recent_closes must equal latest_price"
                )
        if not self.lookahead_safe:
            raise ValueError("snapshot marked not look-ahead safe")
        return self

    def to_context_dict(self) -> dict:
        """Plain dict for serialization to a model provider."""
        d = self.model_dump(mode="json")
        d.pop("lookahead_safe", None)
        return d


def build_snapshot_from_df(
    df, symbol: str, timeframe: str, lookback_closes: int = 60
) -> MarketSnapshot:
    """Build a MarketSnapshot from a stored OHLCV DataFrame (tz-aware index).

    Uses the LAST ROW as the decision point (a closed bar). Indicators are
    computed if missing. No future data is included.
    """
    from ..indicators import add_all_indicators
    from ..analysis.quant import (
        simple_returns,
        annualized_volatility,
        drawdown,
        volume_stats,
    )

    if df is None or len(df) == 0:
        raise ValueError("cannot build snapshot from empty frame")

    work = df.copy().sort_index()
    if "rsi_14" not in work.columns:
        work = add_all_indicators(work)

    last = work.iloc[-1]
    last_ts = work.index[-1]
    if getattr(last_ts, "tzinfo", None) is None:
        last_ts = last_ts.tz_localize("UTC")

    closes = work["close"]
    rets = simple_returns(closes)
    vol = annualized_volatility(rets, timeframe) if len(rets.dropna()) > 1 else None
    dd = drawdown(closes)
    vstats = volume_stats(work["volume"], 20)

    recent = closes.tail(lookback_closes).tolist()
    sma20 = float(last["sma_20"]) if "sma_20" in work.columns else None
    price_vs = None
    if sma20:
        price_vs = (float(last["close"]) - sma20) / sma20

    return MarketSnapshot(
        symbol=symbol,
        timeframe=timeframe,
        timestamp=last_ts,
        last_bar_timestamp=last_ts,
        latest_price=float(last["close"]),
        last_return=float(rets.iloc[-1]) if not rets.empty else 0.0,
        sma_20=sma20,
        sma_50=float(last["sma_50"]) if "sma_50" in work.columns else None,
        ema_12=float(last["ema_12"]) if "ema_12" in work.columns else None,
        rsi_14=float(last["rsi_14"]) if "rsi_14" in work.columns else None,
        macd=float(last["macd"]) if "macd" in work.columns else None,
        macd_signal=float(last["macd_signal"]) if "macd_signal" in work.columns else None,
        macd_hist=float(last["macd_hist"]) if "macd_hist" in work.columns else None,
        atr_14=float(last["atr_14"]) if "atr_14" in work.columns else None,
        bollinger_upper=float(last["bb_upper"]) if "bb_upper" in work.columns else None,
        bollinger_lower=float(last["bb_lower"]) if "bb_lower" in work.columns else None,
        volatility_annualized=vol,
        max_drawdown=float(dd.min()) if not dd.empty else None,
        volume_ma=(
            float(vstats["volume_ma"].iloc[-1])
            if "volume_ma" in vstats.columns
            else None
        ),
        volume_z=(
            float(vstats["volume_z"].iloc[-1])
            if "volume_z" in vstats.columns
            else None
        ),
        price_vs_sma20=price_vs,
        recent_closes=[float(x) for x in recent],
        data_points=int(len(work)),
        data_start=work.index[0] if getattr(work.index[0], "tzinfo", None) else work.index[0].tz_localize("UTC"),
        data_end=last_ts,
        lookahead_safe=True,
    )
