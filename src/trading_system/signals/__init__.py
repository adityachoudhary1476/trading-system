"""Deterministic signal engine.

Combines a MarketView (AI interpretation) with fixed, auditable rules over the
MarketSnapshot to emit LONG / SHORT / HOLD. This is deliberately NOT an attempt
to be profitable — it establishes the architecture and is fully testable.

AI confidence is treated as an ANALYTICAL score, never as a probability of
profit. It only modulates how strongly the deterministic rules may act within a
fixed envelope; it can never bypass the risk layer or sizing (those don't exist
yet) and can never be the sole authority for a trade.

Decision rules (documented and fixed):
  * Require a minimum amount of data (data_points >= min_data_points).
  * Require a non-NEUTRAL market_view to trade at all (NEUTRAL => HOLD).
  * Require AI confidence >= min_confidence to act (else HOLD). This is an
    analytical-confidence gate, not a win-probability claim.
  * If market_view == BULLISH:
      - LONG if price > SMA20 AND MACD > MACD_signal (trend + momentum align)
  * If market_view == BEARISH:
      - SHORT if price < SMA20 AND MACD < MACD_signal
  * Otherwise HOLD.
A 'reason' string records exactly which conditions fired.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ..models.snapshot import MarketSnapshot
from ..models.market_view import MarketView, MarketViewEnum


class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    HOLD = "hold"


@dataclass
class Signal:
    symbol: str
    timeframe: str
    timestamp: object
    direction: SignalDirection
    confidence: float  # analytical confidence carried from the view (0..1)
    source: str = "deterministic"
    reason: str = ""
    market_view: str = ""

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp": str(self.timestamp),
            "direction": self.direction.value,
            "confidence": round(self.confidence, 4),
            "source": self.source,
            "reason": self.reason,
            "market_view": self.market_view,
        }


@dataclass
class SignalConfig:
    min_data_points: int = 30
    min_confidence: float = 0.5  # analytical-confidence gate, NOT win prob
    require_sma: bool = True
    require_macd: bool = True


def generate_signal(
    snapshot: MarketSnapshot,
    view: MarketView,
    config: Optional[SignalConfig] = None,
) -> Signal:
    cfg = config or SignalConfig()
    direction = SignalDirection.HOLD
    reasons: list[str] = []

    if snapshot.data_points < cfg.min_data_points:
        reasons.append(f"insufficient data ({snapshot.data_points}<{cfg.min_data_points})")
        return _mk(snapshot, view, SignalDirection.HOLD, "; ".join(reasons))

    if view.market_view == MarketViewEnum.NEUTRAL:
        reasons.append("AI view neutral")
        return _mk(snapshot, view, SignalDirection.HOLD, "; ".join(reasons))

    if view.confidence < cfg.min_confidence:
        reasons.append(f"AI confidence {view.confidence:.2f} < {cfg.min_confidence}")
        return _mk(snapshot, view, SignalDirection.HOLD, "; ".join(reasons))

    bullish = view.market_view == MarketViewEnum.BULLISH
    bearish = view.market_view == MarketViewEnum.BEARISH

    sma_ok = (not cfg.require_sma) or snapshot.sma_20 is not None
    price_above = sma_ok and snapshot.price_vs_sma20 is not None and snapshot.price_vs_sma20 > 0
    price_below = sma_ok and snapshot.price_vs_sma20 is not None and snapshot.price_vs_sma20 < 0

    macd_ok = (not cfg.require_macd) or (
        snapshot.macd is not None and snapshot.macd_signal is not None
    )
    macd_up = macd_ok and snapshot.macd is not None and snapshot.macd_signal is not None and snapshot.macd > snapshot.macd_signal
    macd_down = macd_ok and snapshot.macd is not None and snapshot.macd_signal is not None and snapshot.macd < snapshot.macd_signal

    if bullish and sma_ok and macd_ok and price_above and macd_up:
        direction = SignalDirection.LONG
        reasons.append("bullish view + price>SMA20 + MACD>signal")
    elif bearish and sma_ok and macd_ok and price_below and macd_down:
        direction = SignalDirection.SHORT
        reasons.append("bearish view + price<SMA20 + MACD<signal")
    else:
        reasons.append("view/indicator conflict -> hold")
        direction = SignalDirection.HOLD

    return _mk(snapshot, view, direction, "; ".join(reasons))


def _mk(snapshot, view, direction, reason) -> Signal:
    return Signal(
        symbol=snapshot.symbol,
        timeframe=snapshot.timeframe,
        timestamp=snapshot.timestamp,
        direction=direction,
        confidence=view.confidence if view else 0.0,
        reason=reason,
        market_view=(view.market_view.value if view else ""),
    )
