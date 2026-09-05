"""V5 causal snapshot: what Finova could legitimately know at time T.

The forecast engine must NEVER rely on caller-side filtering. This module
provides the first-class abstraction used by the actual forecast engine: given
instrument + timestamp T (the replay as_of), build a snapshot containing only:

  * OHLCV bars whose close <= T
  * higher-timeframe candles that are CLOSED by T (bar_start + tf <= T)
  * technical features computed causally (FeatureEngine.features_at)
  * news with published_at <= T (prefer published_at over discovered_at)
  * options rows with timestamp <= T
  * market context with timestamp <= T (explicit staleness policy)

CRITICAL LOOKAHEAD TRAP: a 1h candle ending at 11:00 must NOT be visible at
10:17. Higher-timeframe candles are only available once closed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

from .intelligence import FeatureEngine, classify_regime
from .market_context import MarketIntelligenceContext

UTC = timezone.utc


def closed_htf_candles(df: pd.DataFrame, tf: str,
                       as_of: Optional[datetime] = None) -> pd.DataFrame:
    """Resample lower-TF OHLCV to ``tf`` and keep ONLY closed candles.

    A candle opened at bar_start closes at bar_start + tf; it is available only
    when bar_start + tf <= as_of. The in-progress candle is DROPPED — this is
    the critical lookahead rule.
    """
    if df is None or len(df) == 0:
        return df.iloc[0:0].copy()
    if as_of is not None:
        as_of = as_of if getattr(as_of, "tzinfo", None) else as_of.replace(tzinfo=UTC)
        window = df[df.index <= as_of]
    else:
        window = df
    if len(window) == 0:
        return df.iloc[0:0].copy()
    offset = {"5m": "5min", "15m": "15min", "1h": "1h", "1d": "1D"}.get(tf, tf)
    tf_d = pd.Timedelta(offset)
    agg = window.resample(offset).agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna(subset=["open", "close"])
    if len(agg) == 0:
        return agg
    close_time = agg.index + tf_d
    if as_of is not None:
        keep = close_time <= as_of
        res = agg[keep.values if hasattr(keep, "values") else keep]
    else:
        res = agg.iloc[:-1] if len(agg) > 1 else agg
    return res

@dataclass
class CausalSnapshot:
    """Everything Finova may know at ``timestamp`` for ``instrument``."""

    instrument: str = ""
    timestamp: Optional[datetime] = None
    timeframe: str = ""
    ohlcv: pd.DataFrame = field(default_factory=pd.DataFrame)
    mtf: dict[str, pd.DataFrame] = field(default_factory=dict)
    features: Any = None                   # TechnicalFeatures
    regime: Any = None                     # MarketRegime
    news: list[Any] = field(default_factory=list)   # NewsEventV4 (published<=T)
    context: Optional[MarketIntelligenceContext] = None
    options: list[dict] = field(default_factory=list)
    data_availability: dict[str, str] = field(default_factory=dict)

    def availability_summary(self) -> list[str]:
        return [f"{k}={v}" for k, v in self.data_availability.items()]


class CausalSnapshotBuilder:
    """Builds causal snapshots from normalized frames + optional contexts.

    ``news`` items need ``published_at``/``discovered_at``; only published_at
    (or discovered_at when published is missing) <= as_of are visible.
    ``option_rows`` need a timestamp key <= as_of.
    ``context_history`` maps key -> [(timestamp, obj), ...] so only pre-T
    context is used; older than ``max_staleness`` is marked STALE.
    """

    HTF_CANDLES = ("5m", "15m", "1h", "1d")

    def __init__(self, feature_engine: Optional[FeatureEngine] = None,
                 max_context_staleness: Optional[Any] = None) -> None:
        self.fe = feature_engine or FeatureEngine(lookback=60)
        self.max_staleness = max_context_staleness or pd.Timedelta(days=1)

    def snapshot(self, symbol: str, timeframe: str, frame: pd.DataFrame,
                 as_of: datetime) -> CausalSnapshot:
        as_of = as_of if getattr(as_of, "tzinfo", None) else as_of.replace(tzinfo=UTC)
        causal = frame[frame.index <= as_of]
        feats = self.fe.features_at(frame, as_of)
        regime = classify_regime(feats)
        mtf = {tf: closed_htf_candles(frame, tf, as_of)
               for tf in self.HTF_CANDLES}
        avail = {"OHLCV": ("AVAILABLE" if len(causal) >= 2 else "THIN")}
        for tf in self.HTF_CANDLES:
            avail[f"MTF_{tf}"] = ("AVAILABLE" if len(mtf[tf]) >= 2 else "THIN")
        return CausalSnapshot(
            instrument=symbol, timestamp=as_of, timeframe=timeframe,
            ohlcv=causal, mtf=mtf, features=feats, regime=regime,
            data_availability=avail)

    def with_news(self, snap: CausalSnapshot,
                  news_events: list[Any]) -> CausalSnapshot:
        visible = []
        for e in news_events:
            pub = getattr(e, "published_at", None)
            disc = getattr(e, "discovered_at", None)
            base = pub if pub is not None else disc
            if base is None:
                continue  # ambiguous timestamp -> not assumed available
            base = base if getattr(base, "tzinfo", None) else base.replace(tzinfo=UTC)
            if base <= snap.timestamp:
                visible.append(e)
        snap.news = visible
        snap.data_availability["NEWS"] = "AVAILABLE" if visible else "UNAVAILABLE"
        return snap

    def with_options(self, snap: CausalSnapshot,
                     option_rows: list[dict]) -> CausalSnapshot:
        visible = []
        for row in option_rows:
            ts = row.get("snapshot_ts") or row.get("timestamp")
            if ts is None:
                continue  # no usable timestamp -> not assumed available
            ts = pd.Timestamp(ts)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            if ts <= snap.timestamp:
                visible.append(row)
        snap.options = visible
        snap.data_availability["OPTIONS"] = "AVAILABLE" if visible else "UNAVAILABLE"
        return snap

    def with_context_history(
        self, snap: CausalSnapshot,
        context_history: dict[str, list[tuple[datetime, Any]]],
    ) -> CausalSnapshot:
        ctx, avail = {}, {}
        for key, items in context_history.items():
            prior = [it for it in items if it[0] <= snap.timestamp]
            if not prior:
                avail[key] = "UNAVAILABLE"
                continue
            ts, obj = max(prior, key=lambda x: x[0])
            age = snap.timestamp - ts
            if age <= self.max_staleness:
                ctx[key] = obj
                avail[key] = "AVAILABLE"
            else:
                avail[key] = "STALE"
        for k, v in avail.items():
            snap.data_availability[k.upper()] = v
        if ctx:
            snap.context = MarketIntelligenceContext(
                breadth=ctx.get("breadth"), vix=ctx.get("vix"),
                institutional_flow=ctx.get("institutional_flow"),
                sector=ctx.get("sector"), news=None, cross_asset=ctx.get("cross_asset"))
        return snap
