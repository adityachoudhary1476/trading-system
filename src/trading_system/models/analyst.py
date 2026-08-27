"""Analyst orchestration: snapshot -> ModelProvider -> validated MarketView.

This module is the thin bridge. It does NOT call the model directly; it uses a
ModelProvider so the app never depends on a specific AI vendor. The returned
MarketView is always schema-validated (see models.market_view.MarketView).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..config import log
from .snapshot import MarketSnapshot
from .market_view import MarketView
from .base import ModelProvider, ModelProviderError
from .provider_factory import get_model_provider
from ..signals import generate_signal, Signal, SignalConfig


@dataclass
class AnalysisResult:
    snapshot: MarketSnapshot
    view: Optional[MarketView] = None
    signal: Optional[Signal] = None
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "symbol": self.snapshot.symbol,
            "timeframe": self.snapshot.timeframe,
            "timestamp": str(self.snapshot.timestamp),
            "view": self.view.model_dump(mode="json") if self.view else None,
            "signal": self.signal.as_dict() if self.signal else None,
            "error": self.error,
        }


def analyze_snapshot(
    snapshot: MarketSnapshot,
    provider: Optional[ModelProvider] = None,
    provider_name: Optional[str] = None,
    signal_config: Optional[SignalConfig] = None,
) -> AnalysisResult:
    """Run the AI analyst on a snapshot and produce a validated view + signal."""
    result = AnalysisResult(snapshot=snapshot)
    try:
        prov = provider or get_model_provider(provider_name)
        log.info("ANALYST_START provider=%s symbol=%s", prov.name, snapshot.symbol)
        view = prov.analyze(snapshot)
        result.view = view
        # Deterministic signal from snapshot + validated view.
        result.signal = generate_signal(snapshot, view, signal_config)
        log.info(
            "ANALYST_DONE view=%s conf=%.2f signal=%s",
            view.market_view.value, view.confidence, result.signal.direction.value,
        )
    except ModelProviderError as e:
        result.error = str(e)
        log.error("ANALYST_FAILED %s", result.error)
    except Exception as e:  # never swallow — surface explicitly
        result.error = f"{type(e).__name__}: {e}"
        log.error("ANALYST_ERROR %s", result.error)
    return result
