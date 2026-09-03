"""Forecast ledger (Phase 16 foundation) — persistence for FUTURE calibration.

Every intelligence output can be recorded here with the market state that
produced it, and later resolved with the actual subsequent outcome. This is the
prerequisite for confidence calibration, bias/horizon/expected-move accuracy
and options-setup evaluation.

IMPORTANT: recording forecasts does NOT make confidence a probability. Nothing
in this module claims statistical calibration — ``summarize_calibration``
reports raw observed frequencies and explicitly labels them uncalibrated until
a sufficient resolved sample exists. Research/analytics only; no execution.
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    Column, String, Integer, Float, Text, DateTime, Boolean, Index, select, create_engine,
)
from sqlalchemy.orm import sessionmaker

from ..storage.database import Base


# Minimum resolved forecasts before any calibration read-out is even labeled
# "provisional" (provisional policy — flagged as such, like evidence.py thresholds).
MIN_RESOLVED_FOR_CALIBRATION = 100


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_json(value: Any) -> Optional[str]:
    """Serialize dataclasses/dicts/objects to JSON; None stays None."""
    if value is None:
        return None
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    elif not isinstance(value, (dict, list, str, int, float, bool)):
        # e.g. enums, custom objects: best-effort str() — never fabricate fields.
        try:
            value = {"repr": str(value)}
        except Exception:
            value = {"repr": "<unserializable>"}
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return json.dumps({"repr": str(value)})


def _decode_json(value: Any) -> Any:
    """Parse JSON text columns back into Python objects; pass through others."""
    if isinstance(value, str) and value:
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


class ForecastRecord(Base):
    __tablename__ = "forecast_ledger"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True), nullable=False, index=True)

    # What was forecast
    instrument = Column(String(64), nullable=False, index=True)
    timeframe = Column(String(16), nullable=False)
    forecast = Column(Text, nullable=False, default="")          # free-text thesis/summary
    bias = Column(String(16), nullable=False)                    # bullish/bearish/neutral
    confidence = Column(Float, nullable=False)                   # model confidence 0..1 (NOT probability)
    horizon = Column(String(24), nullable=False)                 # intraday/short_term/swing
    expected_move_lower_pct = Column(Float, nullable=True)
    expected_move_upper_pct = Column(Float, nullable=True)
    invalidation = Column(Text, nullable=True)
    selected_option = Column(Text, nullable=True)                # JSON of top options candidate
    market_state = Column(Text, nullable=True)                   # JSON snapshot of inputs/evidence

    # Resolution (filled later — never at forecast time)
    resolved = Column(Boolean, nullable=False, default=False, index=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    actual_return_pct = Column(Float, nullable=True)
    hit = Column(Boolean, nullable=True)                         # bias direction matched outcome
    within_expected_move = Column(Boolean, nullable=True)        # outcome inside estimated range


Index("ix_forecast_instrument_time", ForecastRecord.instrument, ForecastRecord.created_at)

class ForecastStore:
    """SQLite forecast store on the SAME engine as MarketStore/EvidenceStore."""

    def __init__(self, engine) -> None:
        if engine is None:
            engine = create_engine("sqlite:///forecast_ledger.db", future=True)
        self.engine = engine
        Base.metadata.create_all(engine)  # idempotent; adds forecast_ledger
        self._Session = sessionmaker(bind=engine, future=True)

    # --- recording ---------------------------------------------------------
    def record_forecast(
        self,
        instrument: str,
        timeframe: str,
        bias: str,
        confidence: float,
        horizon: str,
        forecast: str = "",
        expected_move_lower_pct: Optional[float] = None,
        expected_move_upper_pct: Optional[float] = None,
        invalidation: Optional[str] = None,
        selected_option: Any = None,
        market_state: Any = None,
        created_at: Optional[datetime] = None,
    ) -> ForecastRecord:
        """Persist one forecast. ``confidence`` is model confidence 0..1."""
        rec = ForecastRecord(
            created_at=created_at or datetime.now(timezone.utc),
            instrument=instrument,
            timeframe=timeframe,
            forecast=forecast or "",
            bias=str(bias).lower(),
            confidence=float(min(1.0, max(0.0, confidence))),
            horizon=str(horizon),
            expected_move_lower_pct=expected_move_lower_pct,
            expected_move_upper_pct=expected_move_upper_pct,
            invalidation=invalidation,
            selected_option=_as_json(selected_option),
            market_state=_as_json(market_state),
            resolved=False,
        )
        with self._Session() as s:
            s.add(rec)
            s.commit()
            s.refresh(rec)
        # Detached now: expose JSON columns as Python objects (dict), not text.
        rec.selected_option = _decode_json(rec.selected_option)
        rec.market_state = _decode_json(rec.market_state)
        return rec

    def record_from_analysis(self, analysis: dict) -> Optional[ForecastRecord]:
        """Adapter: persist the outcome of MarketIntelligenceEngine.analyze().

        BLOCKED analyses are NOT recorded (nothing was forecast). Returns None.
        """
        if not analysis or analysis.get("status") != "OK":
            return None
        cand = analysis.get("signal_candidate")
        if cand is None:
            return None
        em = getattr(cand, "expected_move", None)
        horizon = getattr(cand, "horizon", None)
        top_opt = None
        opts = analysis.get("options_candidates") or []
        if opts:
            o = opts[0]
            top_opt = {
                "strike": getattr(o, "strike", None),
                "option_type": getattr(o, "option_type", None),
                "expiry": getattr(o, "expiry", None),
                "score": getattr(o, "score", None),
            }
        _bias_map = {"long": "bullish", "short": "bearish", "neutral": "neutral"}
        _dir = getattr(cand, "direction", None)
        return self.record_forecast(
            instrument=analysis.get("symbol", ""),
            timeframe=analysis.get("timeframe", ""),
            bias=_bias_map.get(_dir.value, "neutral") if _dir is not None else "neutral",
            confidence=float(getattr(cand, "confidence", 0.0)),
            horizon=horizon.value if horizon is not None else "unknown",
            forecast=getattr(analysis.get("explanation"), "summary", "") if analysis.get("explanation") else "",
            expected_move_lower_pct=getattr(em, "lower_pct", None) if em is not None else None,
            expected_move_upper_pct=getattr(em, "upper_pct", None) if em is not None else None,
            invalidation=getattr(cand, "invalidation_context", None),
            selected_option=top_opt,
            market_state={
                "instrument_class": analysis.get("instrument_class"),
                "options_status": analysis.get("options_status"),
                "regime": str(getattr(analysis.get("regime"), "regime", "")),
                "data_quality": analysis.get("data_quality"),
            },
            created_at=getattr(cand, "timestamp", None),
        )



    # --- resolution ----------------------------------------------------------
    def resolve_forecast(
        self,
        forecast_id: int,
        actual_return_pct: float,
        resolved_at: Optional[datetime] = None,
    ) -> ForecastRecord:
        """Attach the actual subsequent outcome and score the forecast.

        hit = directional agreement (bullish & up, bearish & down, neutral only
        if |return| is negligible). within_expected_move checks the realized
        return against the estimated range when one was recorded.
        """
        with self._Session() as s:
            key = forecast_id.id if isinstance(forecast_id, ForecastRecord) else forecast_id
            rec = s.get(ForecastRecord, key)
            if rec is None:
                raise KeyError(key)
            ret = float(actual_return_pct)
            eps = 1e-9
            if rec.bias == "bullish":
                rec.hit = ret > eps
            elif rec.bias == "bearish":
                rec.hit = ret < -eps
            else:  # neutral "forecast": only a non-move counts as a hit
                rec.hit = abs(ret) <= 0.25
            if rec.expected_move_lower_pct is not None and rec.expected_move_upper_pct is not None:
                lo = min(rec.expected_move_lower_pct, rec.expected_move_upper_pct)
                hi = max(rec.expected_move_lower_pct, rec.expected_move_upper_pct)
                rec.within_expected_move = lo <= ret <= hi
            rec.resolved = True
            rec.resolved_at = resolved_at or datetime.now(timezone.utc)
            rec.actual_return_pct = ret
            s.commit()
            s.refresh(rec)
            resolved_rec = rec
        # Detached now: expose JSON columns as Python objects (dict), not text.
        resolved_rec.selected_option = _decode_json(resolved_rec.selected_option)
        resolved_rec.market_state = _decode_json(resolved_rec.market_state)
        return resolved_rec

    # --- queries ---------------------------------------------------------
    def list_forecasts(
        self,
        instrument: Optional[str] = None,
        resolved: Optional[bool] = None,
        limit: int = 500,
    ) -> list[ForecastRecord]:
        with self._Session() as s:
            q = select(ForecastRecord)
            if instrument:
                q = q.where(ForecastRecord.instrument == instrument)
            if resolved is not None:
                q = q.where(ForecastRecord.resolved == resolved)
            recs = s.execute(q.order_by(ForecastRecord.created_at.desc()).limit(limit)).scalars().all()
        records = list(recs)
        for rec in records:  # detached: JSON columns as Python objects, not text
            rec.selected_option = _decode_json(rec.selected_option)
            rec.market_state = _decode_json(rec.market_state)
        return records

    def summarize_calibration(self, instrument: Optional[str] = None) -> dict:
        """Raw observed frequencies over resolved forecasts.

        NOT a calibrated probability model. Output is explicitly labeled
        uncalibrated until >= MIN_RESOLVED_FOR_CALIBRATION resolved rows exist;
        do not present these numbers as win probabilities.
        """
        recs = self.list_forecasts(instrument=instrument, resolved=True, limit=100_000)
        n = len(recs)
        summary: dict = {
            "instrument": instrument or "ALL",
            "resolved_count": n,
            "calibration_status": (
                "uncalibrated_insufficient_sample"
                if n < MIN_RESOLVED_FOR_CALIBRATION
                else "provisional_not_statistically_validated"
            ),
            "note": (
                "Model confidence is NOT a probability. These are raw observed "
                "frequencies for future evaluation only."
            ),
        }
        if n == 0:
            return summary
        hits = [r for r in recs if r.hit]
        summary["directional_hit_rate"] = round(len(hits) / n, 4)
        summary["mean_confidence"] = round(sum(r.confidence for r in recs) / n, 4)
        scored = [r for r in recs if r.within_expected_move is not None]
        if scored:
            summary["expected_move_containment"] = round(
                sum(1 for r in scored if r.within_expected_move) / len(scored), 4
            )
        else:
            summary["expected_move_containment"] = None
        # Bias distribution (raw counts)
        summary["bias_counts"] = {
            b: sum(1 for r in recs if r.bias == b) for b in ("bullish", "bearish", "neutral")
        }
        return summary
