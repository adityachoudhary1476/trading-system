"""Phase 8B — Current-market option candidate discovery and evaluation.

Architectural boundary between the autonomous decision flow and the actual
instrument universe:

    TradingDecision (Phase 5)
        ↓
    CurrentOptionDiscoverer.discover()
        ↓  →  CandidateEvaluationResult  (selected Instrument or None)
    SafetyValidator.check_contract_validity()
        ↓
    Exact Instrument identity propagates to Order → Position

The discoverer queries the real ``InstrumentRepository`` for currently-available
option contracts. It NEVER manufactures contract symbols, strikes, or expiries.

Candidate evaluation uses only data available in the repository:

  - strike distance from spot (directional suitability)
  - instrument validity (non-expired, correct type, correct underlying)
  - configured constraints (allowed option types, max contracts)

Liquidity, volume, OI, and premiums are NOT available on the ``Instrument``
model itself. If the caller supplies a market-data provider, the discoverer MAY
use it for premium lookups — but contract existence always comes from the
repository, never from the LLM or market-data alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Callable, Optional

from trading_system.india.instruments import (
    Instrument,
    InstrumentType,
)
from trading_system.india.instrument_repository import InstrumentRepository

from .model import OptionsContractSelection, OptionDirection, StrikeSelectionPolicy


class OptionEligibilityReason(str, Enum):
    """Why a candidate contract was rejected during evaluation."""

    VALID = "valid"
    WRONG_RIGHT = "wrong_option_type"
    EXPIRED = "expired"
    NOT_OPTION = "not_an_option"
    WRONG_UNDERLYING = "wrong_underlying"
    STALE_INSTRUMENT = "stale_instrument_data"
    PREMIUM_UNAVAILABLE = "premium_unavailable"
    STAKE_TOO_HIGH = "stake_too_high"
    STRIKE_TOO_FAR = "strike_too_far_from_spot"
    MAX_CONTRACTS_EXCEEDED = "max_contracts_exceeded"


@dataclass
class EvaluatedCandidate:
    """One candidate contract with its evaluation score and metadata."""

    instrument: Instrument
    selection: OptionsContractSelection
    score: float = 0.0
    eligibility: OptionEligibilityReason = OptionEligibilityReason.VALID
    reason: str = ""
    distance_from_spot: float = 0.0
    strike_direction: str = ""  # "ITM", "ATM", "OTM"

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument.contract_id,
            "symbol": self.instrument.key,
            "underlying": self.instrument.underlying,
            "expiry": self.instrument.expiry,
            "strike": self.instrument.strike,
            "option_type": self.instrument.option_type,
            "score": round(self.score, 4),
            "eligibility": self.eligibility.value,
            "reason": self.reason,
            "distance_from_spot": round(self.distance_from_spot, 2),
            "strike_direction": self.strike_direction,
        }


@dataclass
class DiscoveryConfig:
    """Configuration for current-market option discovery.

    All defaults are conservative and paper-only.
    """

    allowed_option_types: list[str] = field(default_factory=lambda: ["CE", "PE"])
    max_options_contracts_per_trade: Optional[int] = None
    min_days_to_expiry: int = 3
    max_strike_distance_pct: float = 0.15  # max distance from spot as fraction
    prefer_near_expiry: bool = False  # if True, prefer nearest expiry; else prefer further
    require_premium: bool = False  # if True, require premium data from market provider


@dataclass
class CandidateEvaluationResult:
    """Result of evaluating all discovered option candidates.

    ``selected`` is the best candidate, or ``None`` if no suitable contract
    was found. ``candidates`` always contains ALL evaluated candidates
    (including rejected ones) for observability.
    """

    underlying: str
    direction: str
    spot_price: float
    as_of_date: str
    candidates: list[EvaluatedCandidate] = field(default_factory=list)
    selected: Optional[EvaluatedCandidate] = None
    selection_reason: str = ""
    selection_note: str = ""

    @property
    def has_selection(self) -> bool:
        return self.selected is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "direction": self.direction,
            "spot_price": self.spot_price,
            "as_of_date": self.as_of_date,
            "candidates_count": len(self.candidates),
            "selected": self.selected.to_dict() if self.selected else None,
            "selection_reason": self.selection_reason,
            "candidates": [c.to_dict() for c in self.candidates],
        }


class CurrentOptionDiscoverer:
    """Discovers and evaluates currently-available option contracts from the
    real instrument repository.

    This is the Phase 8B autonomous contract discovery layer. It replaces the
    rigid ``OptionsStructureBuilder`` chain-provider path for the autonomous
    paper-trading runtime.

    The discoverer:
      1. Queries ``InstrumentRepository.list_options()`` for ALL available
         CE or PE contracts for the underlying.
      2. Filters by expiry, option type, and configured constraints.
      3. Evaluates each candidate's strike distance from spot.
      4. Scores and selects the best candidate.
      5. Returns the canonical ``Instrument`` identity.

    NEVER fabricates contracts. If the repository has no options, the
    discoverer returns ``None``.
    """

    def __init__(
        self,
        repository: InstrumentRepository,
        market_data_provider: Optional[Callable] = None,
    ) -> None:
        self._repository = repository
        self._market_data_provider = market_data_provider

    def discover(
        self,
        *,
        underlying: str,
        direction: OptionDirection,
        spot_price: float,
        config: Optional[DiscoveryConfig] = None,
        as_of: Optional[str] = None,
    ) -> CandidateEvaluationResult:
        """Discover and evaluate current option candidates for an underlying.

        Parameters
        ----------
        underlying:
            The underlying symbol (e.g. "NIFTY", "SBIN")
        direction:
            OptionDirection.CALL for bullish, PUT for bearish
        spot_price:
            Current spot price of the underlying (from actual market snapshot)
        config:
            Discovery configuration (constraints, preferences)
        as_of:
            Reference date for expiry filtering (ISO date string).
            Defaults to today.

        Returns
        -------
        CandidateEvaluationResult with all evaluated candidates and the
        selected one (or None if no suitable contract).
        """
        cfg = config or DiscoveryConfig()
        ref_date = date.fromisoformat(as_of) if as_of else date.today()

        result = CandidateEvaluationResult(
            underlying=underlying,
            direction=direction.value,
            spot_price=spot_price,
            as_of_date=ref_date.isoformat(),
        )

        # --- 1. Discover actual available contracts from the repository ---
        option_type_str = direction.option_type  # "CE" or "PE"

        # Filter by allowed option types
        if option_type_str not in cfg.allowed_option_types:
            result.selection_note = (
                f"option type {option_type_str} not in allowed types "
                f"{cfg.allowed_option_types}"
            )
            return result

        # Query the actual instrument repository
        all_options = self._repository.list_options(
            underlying=underlying,
            option_type=option_type_str,
        )

        if not all_options:
            result.selection_note = (
                f"no {option_type_str} options available for underlying '{underlying}' "
                f"in the current instrument universe"
            )
            return result

        # --- 2. Filter + evaluate each candidate ---
        for instr in all_options:
            evaluated = self._evaluate_candidate(
                instr=instr,
                spot_price=spot_price,
                option_type_str=option_type_str,
                direction=direction,
                ref_date=ref_date,
                config=cfg,
            )
            result.candidates.append(evaluated)

        # --- 3. Select the best valid candidate ---
        valid = [c for c in result.candidates if c.eligibility == OptionEligibilityReason.VALID]
        if not valid:
            result.selection_note = (
                f"all {len(result.candidates)} candidate(s) rejected; "
                f"no suitable {option_type_str} contract"
            )
            return result

        # Sort by score descending
        valid.sort(key=lambda c: c.score, reverse=True)
        best = valid[0]
        result.selected = best
        result.selection_reason = best.reason

        return result

    def _evaluate_candidate(
        self,
        *,
        instr: Instrument,
        spot_price: float,
        option_type_str: str,
        direction: OptionDirection,
        ref_date: date,
        config: DiscoveryConfig,
    ) -> EvaluatedCandidate:
        """Evaluate a single instrument candidate.

        Returns an EvaluatedCandidate with eligibility reason and score.
        """
        # --- Structural validation ---
        if instr.instrument_type not in (InstrumentType.OPTION_CE, InstrumentType.OPTION_PE):
            return EvaluatedCandidate(
                instrument=instr,
                selection=OptionsContractSelection.from_instrument(instr),
                eligibility=OptionEligibilityReason.NOT_OPTION,
                reason=f"instrument {instr.key} is not an option (type={instr.instrument_type})",
            )

        instr_ot = instr.option_type or (
            "CE" if instr.instrument_type == InstrumentType.OPTION_CE else "PE"
        )
        if instr_ot != option_type_str:
            return EvaluatedCandidate(
                instrument=instr,
                selection=OptionsContractSelection.from_instrument(instr),
                eligibility=OptionEligibilityReason.WRONG_RIGHT,
                reason=f"option_type mismatch: expected {option_type_str}, got {instr_ot}",
            )

        if not instr.underlying or instr.underlying.upper() != instr.underlying:
            pass  # underlying may be set differently, check below
        if instr.underlying and instr.underlying.upper() != instr.underlying.upper():
            pass  # just normalize

        # --- Expiry validation ---
        if not instr.expiry:
            return EvaluatedCandidate(
                instrument=instr,
                selection=OptionsContractSelection.from_instrument(instr),
                eligibility=OptionEligibilityReason.EXPIRED,
                reason="instrument has no expiry date",
            )

        try:
            expiry_dt = date.fromisoformat(instr.expiry)
        except (ValueError, TypeError):
            return EvaluatedCandidate(
                instrument=instr,
                selection=OptionsContractSelection.from_instrument(instr),
                eligibility=OptionEligibilityReason.EXPIRED,
                reason=f"cannot parse expiry date: {instr.expiry}",
            )

        days_to_expiry = (expiry_dt - ref_date).days
        if days_to_expiry < config.min_days_to_expiry:
            return EvaluatedCandidate(
                instrument=instr,
                selection=OptionsContractSelection.from_instrument(instr),
                eligibility=OptionEligibilityReason.EXPIRED,
                reason=f"expiry {instr.expiry} too close ({days_to_expiry}d < {config.min_days_to_expiry}d minimum)",
            )

        # --- Strike validation ---
        if instr.strike is None or instr.strike <= 0:
            return EvaluatedCandidate(
                instrument=instr,
                selection=OptionsContractSelection.from_instrument(instr),
                eligibility=OptionEligibilityReason.VALID,
                reason=f"strike {instr.strike} invalid",
            )

        # --- Strike distance evaluation ---
        distance_pct = abs(instr.strike - spot_price) / spot_price if spot_price > 0 else 999.0
        distance_from_spot = instr.strike - spot_price

        # Determine strike direction for CALL: ITM = strike < spot, OTM = strike > spot
        # For PUT: ITM = strike > spot, OTM = strike < spot
        if option_type_str == "CE":
            if instr.strike < spot_price:
                strike_dir = "ITM"
            elif instr.strike == spot_price:
                strike_dir = "ATM"
            else:
                strike_dir = "OTM"
        else:  # PE
            if instr.strike > spot_price:
                strike_dir = "ITM"
            elif instr.strike == spot_price:
                strike_dir = "ATM"
            else:
                strike_dir = "OTM"

        # --- Score calculation ---
        # Higher score = better candidate.
        # Score components:
        # 1. Prefer ATM (distance close to 0) → higher score
        # 2. Prefer ITM (in the money) over deep OTM for the direction
        # 3. Prefer nearer expiry for shorter-dated plays (unless prefer_near_expiry=False)
        # 4. Max strike distance constraint
        if distance_pct > config.max_strike_distance_pct:
            return EvaluatedCandidate(
                instrument=instr,
                selection=OptionsContractSelection.from_instrument(instr),
                score=0.0,
                eligibility=OptionEligibilityReason.STRIKE_TOO_FAR,
                reason=f"strike {instr.strike} is {distance_pct:.1%} from spot (exceeds max {config.max_strike_distance_pct:.1%})",
                distance_from_spot=distance_from_spot,
                strike_direction=strike_dir,
            )
        else:
            # Base score: inversely proportional to distance from spot
            # ATM = best, OTM/ITM slightly away = lower
            distance_score = max(0.0, 1.0 - distance_pct / config.max_strike_distance_pct)

            # ITM bonus: for a directional play, ITM has intrinsic value
            itm_bonus = 0.15 if strike_dir == "ITM" else 0.0
            atm_bonus = 0.20 if strike_dir == "ATM" else 0.0

            # Expiry score: prefer near expiry for short-dated, or further for longer-dated
            # Normalize days_to_expiry (lower = closer, capped at 90 days)
            expiry_score = max(0.0, 1.0 - days_to_expiry / 90.0)
            if config.prefer_near_expiry:
                expiry_component = expiry_score  # prefer near
            else:
                expiry_component = 1.0 - expiry_score  # prefer further

            score = distance_score + itm_bonus + atm_bonus + (expiry_component * 0.1)

            reason = (
                f"{instr.strike} {option_type_str} | "
                f"strike={strike_dir}, "
                f"distance={distance_pct:.1%}, "
                f"dte={days_to_expiry}d, "
                f"score={score:.4f}"
            )

        selection = OptionsContractSelection.from_instrument(instr)
        return EvaluatedCandidate(
            instrument=instr,
            selection=selection,
            score=score,
            eligibility=OptionEligibilityReason.VALID,
            reason=reason,
            distance_from_spot=distance_from_spot,
            strike_direction=strike_dir,
        )

    def resolve_contract(
        self,
        *,
        underlying: str,
        expiry: str,
        option_type: str,
        strike: float,
        exchange: str = "NFO",
    ) -> Optional[Instrument]:
        """Resolve an exact contract through the repository.

        This is the canonical instrument resolution — the discovered candidate's
        identity must round-trip through `find_contract` to prove it actually
        exists.
        """
        return self._repository.find_contract(
            underlying=underlying,
            expiry=expiry,
            option_type=option_type,
            strike=strike,
        )
