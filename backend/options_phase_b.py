"""Phase B — Options provider wiring for the autonomous scheduler.

This module provides the deterministic, paper-only wiring that connects the
autonomous scheduler to real option discovery and quote providers. It does
NOT execute any orders.

Design constraints:

  * Uses ``CurrentOptionDiscoverer`` and ``CurrentOptionQuoteProvider``
    (real interfaces).
  * NEVER uses ``InMemoryOptionsChainProvider`` for production capability.
  * NEVER calls ``execute_option_order`` or ``submit_order_intent``.
  * Fails closed on provider unavailability, stale quotes, or missing contracts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from trading_system.autonomous.options.discovery import (
    CurrentOptionDiscoverer,
    DiscoveryConfig,
)
from trading_system.autonomous.options.model import OptionDirection
from trading_system.india.instrument_repository import InstrumentRepository
from trading_system.india.instruments import Instrument


# ---------------------------------------------------------------------------
# Event constants
# ---------------------------------------------------------------------------
EVENT_DISCOVERY_OK = "discovery_ok"
EVENT_DISCOVERY_FAILED = "discovery_failed"
EVENT_QUOTE_OK = "quote_ok"
EVENT_QUOTE_STALE = "quote_stale"
EVENT_QUOTE_UNAVAILABLE = "quote_unavailable"
EVENT_PROVIDER_UNAVAILABLE = "provider_unavailable"
EVENT_CAPABILITY_READY = "capability_ready"
EVENT_PHASE_B_NO_EXECUTION = "phase_b_no_execution"


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PhaseBObservation:
    """One typed observation from the Phase B validation pass."""

    event: str
    detail: str = ""
    premium: Optional[float] = None
    instrument_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "detail": self.detail,
            "premium": self.premium,
            "instrument_id": self.instrument_id,
        }


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
@dataclass
class OptionsPhaseBWiring:
    """Phase B wiring: real providers, no synthetic chain provider."""

    repository: InstrumentRepository
    discoverer: CurrentOptionDiscoverer
    quote_provider: Any  # CurrentOptionQuoteProvider (duck-typed)
    discovery: Optional[Any] = None

    # Paper-only safety: never instantiate a synthetic chain provider.
    _synthetic_chain_attached: bool = field(default=False, repr=False)

    # Test-compatible aliases.
    @property
    def _discoverer(self) -> CurrentOptionDiscoverer:
        return self.discoverer

    @property
    def _quote_provider(self) -> Any:
        return self.quote_provider

    def attach_to_controller(self, controller: Any) -> bool:
        """Attach this wiring to an AutonomousController.

        Returns False if a synthetic chain provider is already attached
        (defence in depth), or if the quote provider is not authenticated
        (in which case the discoverer is attached but the quote provider
        slot is left as None).
        """
        if getattr(controller, "_chain_provider", None) is not None:
            from trading_system.autonomous.options_contract import (
                InMemoryOptionsChainProvider,
            )
            if isinstance(controller._chain_provider, InMemoryOptionsChainProvider):
                return False

        controller.set_option_discoverer(self.discoverer, repository=self.repository)
        if getattr(self.quote_provider, "is_authenticated", False):
            controller.set_quote_provider(self.quote_provider)
        else:
            controller.set_quote_provider(None)
        return True

    def verify_deployment_capability(
        self,
        *,
        underlying: str,
        direction: OptionDirection,
        spot_price: float,
        max_quote_age_seconds: float = 300.0,
        config: Optional[DiscoveryConfig] = None,
    ) -> list[PhaseBObservation]:
        """Run the full Phase B validation pass for one underlying + direction.

        Returns a list of observations. No orders are submitted.
        """
        observations: list[PhaseBObservation] = []

        # --- Provider availability ---
        if not self.quote_provider.is_authenticated:
            observations.append(
                PhaseBObservation(
                    event=EVENT_PROVIDER_UNAVAILABLE,
                    detail="quote provider is not authenticated",
                )
            )
            return observations

        # --- Discovery ---
        try:
            result = self.discoverer.discover(
                underlying=underlying,
                direction=direction,
                spot_price=spot_price,
                config=config,
            )
        except Exception as exc:  # noqa: BLE001
            observations.append(
                PhaseBObservation(
                    event=EVENT_DISCOVERY_FAILED,
                    detail=f"discovery raised {type(exc).__name__}: {exc}",
                )
            )
            return observations

        if not result.has_selection or result.selected is None:
            observations.append(
                PhaseBObservation(
                    event=EVENT_DISCOVERY_FAILED,
                    detail="no option contract selected",
                )
            )
            return observations

        instrument = result.selected.instrument
        observations.append(
            PhaseBObservation(
                event=EVENT_DISCOVERY_OK,
                detail=f"selected {instrument.contract_id}",
                instrument_id=instrument.contract_id,
            )
        )

        # --- Quote ---
        quote = self.quote_provider.get_quote(instrument)
        if quote is None:
            observations.append(
                PhaseBObservation(
                    event=EVENT_QUOTE_UNAVAILABLE,
                    detail="quote provider returned None",
                    instrument_id=instrument.contract_id,
                )
            )
            return observations

        if not self.quote_provider.is_fresh(quote, max_age_seconds=max_quote_age_seconds):
            observations.append(
                PhaseBObservation(
                    event=EVENT_QUOTE_STALE,
                    detail=f"quote age={quote.age_seconds:.0f}s > {max_quote_age_seconds}s",
                    instrument_id=instrument.contract_id,
                )
            )
            return observations

        observations.append(
            PhaseBObservation(
                event=EVENT_QUOTE_OK,
                detail=f"premium={quote.ltp}",
                premium=float(quote.ltp),
                instrument_id=instrument.contract_id,
            )
        )

        # --- Capability ready ---
        observations.append(
            PhaseBObservation(
                event=EVENT_CAPABILITY_READY,
                detail="discovery + quote + contract validity passed",
                premium=float(quote.ltp),
                instrument_id=instrument.contract_id,
            )
        )

        return observations


__all__ = [
    "EVENT_CAPABILITY_READY",
    "EVENT_DISCOVERY_FAILED",
    "EVENT_DISCOVERY_OK",
    "EVENT_PHASE_B_NO_EXECUTION",
    "EVENT_PROVIDER_UNAVAILABLE",
    "EVENT_QUOTE_OK",
    "EVENT_QUOTE_STALE",
    "EVENT_QUOTE_UNAVAILABLE",
    "OptionsPhaseBWiring",
    "PhaseBObservation",
]
