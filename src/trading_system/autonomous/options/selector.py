"""Phase 8 — Deterministic options contract selector.

Converts a directional decision (bullish/bearish) into a concrete, repository-
resolved ``Instrument`` via ``OptionsContractSelection``.

Selection policy (deterministic, documented, no LLM, no fabricated data):

  1. Direction → option right
     bullish → CALL (CE)
     bearish → PUT  (PE)

  2. Expiry → nearest non-expired expiry relative to ``as_of``.
     (Falls back to the nearest expiry in the repository if ``as_of`` yields
     no valid candidates — the contract must exist, even if today's date is
     past the original as_of.)

  3. Strike → nearest ATM (at-the-money) strike relative to ``spot_price``.
     ``spot_price`` is the underlying's latest close (injected, never faked).

If the repository has no option contracts matching the criteria, the selector
returns ``None`` (fail-safe) rather than fabricating a contract.
"""
from __future__ import annotations

from datetime import date
from typing import Callable, Optional

from trading_system.india.instruments import (
    Instrument,
    InstrumentType,
)
from trading_system.india.instrument_repository import InstrumentRepository

from .model import (
    ExpirySelectionPolicy,
    OptionDirection,
    OptionsContractSelection,
    StrikeSelectionPolicy,
)


class OptionsContractSelector:
    """Deterministic single-leg options contract selector.

    Parameters
    ----------
    repository:
        The ``InstrumentRepository`` that knows about available option contracts.
        Must be populated (via FYERS/Upstox master import or manual registration)
        with real option instruments. If no options are known, ``select``
        returns ``None``.
    spot_price_provider:
        Optional callable ``(underlying: str) -> float`` that returns the latest
        spot price of the underlying. Used to compute the nearest ATM strike.
        If not provided, the caller must pass ``spot_price`` to ``select``.
    """

    def __init__(
        self,
        repository: InstrumentRepository,
        spot_price_provider: Optional[Callable[[str], float]] = None,
    ) -> None:
        self._repository = repository
        self._spot_provider = spot_price_provider

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def select(
        self,
        *,
        underlying: str,
        direction: OptionDirection,
        as_of: Optional[str] = None,
        spot_price: Optional[float] = None,
        strike_policy: StrikeSelectionPolicy = StrikeSelectionPolicy.ATM,
        expiry_policy: ExpirySelectionPolicy = ExpirySelectionPolicy.NEAREST,
    ) -> Optional[OptionsContractSelection]:
        """Select a specific option contract.

        Returns an ``OptionsContractSelection`` wrapping the resolved
        ``Instrument``, or ``None`` if no matching contract exists in the
        repository (fail-safe — never fabricates a contract).
        """
        ot = direction.option_type

        # --- Resolve spot price ---
        spot = spot_price
        if spot is None and self._spot_provider is not None:
            spot = self._spot_provider(underlying)
        if spot is None or spot <= 0:
            return None

        # --- Resolve expiry ---
        expiry = self._select_expiry(underlying, as_of, expiry_policy)
        if expiry is None:
            return None

        # --- Resolve strike ---
        strike = self._select_strike(underlying, expiry, ot, spot, strike_policy)
        if strike is None:
            return None

        # --- Resolve the exact Instrument from the repository ---
        instrument = self._repository.find_contract(
            underlying=underlying,
            expiry=expiry,
            option_type=ot,
            strike=strike,
        )
        if instrument is None:
            return None

        return OptionsContractSelection.from_instrument(instrument)

    def can_select(
        self,
        *,
        underlying: str,
        direction: OptionDirection,
        as_of: Optional[str] = None,
        spot_price: Optional[float] = None,
    ) -> bool:
        """Quick check: would ``select`` return a non-None result?

        Useful for pre-flight validation without constructing the full
        selection object.
        """
        return self.select(
            underlying=underlying,
            direction=direction,
            as_of=as_of,
            spot_price=spot_price,
        ) is not None

    # ------------------------------------------------------------------ #
    # Expiry selection
    # ------------------------------------------------------------------ #

    def _select_expiry(
        self,
        underlying: str,
        as_of: Optional[str],
        policy: ExpirySelectionPolicy,
    ) -> Optional[str]:
        """Select the nearest non-expired expiry for the underlying.

        Returns the ISO date string (YYYY-MM-DD) of the selected expiry,
        or ``None`` if no expiries are known.
        """
        exps = self._repository.get_expiries(underlying)
        if not exps:
            return None

        today = date.fromisoformat(as_of) if as_of else date.today()

        # Filter to expiries strictly after ``today`` (not expired).
        future = [e for e in exps if date.fromisoformat(e) > today]
        if not future:
            # No future expiries — fall back to the nearest expiry overall.
            future = list(exps)

        if policy == ExpirySelectionPolicy.NEAREST:
            return future[0]
        elif policy == ExpirySelectionPolicy.NEXT:
            if len(future) >= 2:
                return future[1]
            return future[0] if future else None
        return future[0] if future else None

    # ------------------------------------------------------------------ #
    # Strike selection
    # ------------------------------------------------------------------ #

    def _select_strike(
        self,
        underlying: str,
        expiry: str,
        option_type: str,
        spot: float,
        policy: StrikeSelectionPolicy,
    ) -> Optional[float]:
        """Select a strike from the available option chain.

        For ATM (default): nearest strike to ``spot``.
        For ITM: for CE, nearest strike below spot; for PE, nearest strike above spot.
        For OTM: for CE, nearest strike above spot; for PE, nearest strike below spot.
        """
        options = self._repository.list_options(
            underlying=underlying,
            expiry=expiry,
            option_type=option_type,
        )
        if not options:
            return None

        strikes = [float(o.strike) for o in options if o.strike is not None]
        if not strikes:
            return None

        strikes.sort()

        if policy == StrikeSelectionPolicy.ATM:
            return _nearest(strikes, spot)

        if policy == StrikeSelectionPolicy.ITM:
            if option_type == "CE":
                below = [s for s in strikes if s <= spot]
                return below[-1] if below else _nearest(strikes, spot)
            else:  # PE
                above = [s for s in strikes if s >= spot]
                return above[0] if above else _nearest(strikes, spot)

        if policy == StrikeSelectionPolicy.OTM:
            if option_type == "CE":
                above = [s for s in strikes if s >= spot]
                return above[0] if above else _nearest(strikes, spot)
            else:  # PE
                below = [s for s in strikes if s <= spot]
                return below[-1] if below else _nearest(strikes, spot)

        return _nearest(strikes, spot)


def _nearest(strikes: list[float], spot: float) -> float:
    """Return the strike closest to ``spot``."""
    return min(strikes, key=lambda s: abs(s - spot))
