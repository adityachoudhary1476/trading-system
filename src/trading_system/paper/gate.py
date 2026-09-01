"""Phase 18 — Deployment gate.

A strategy must NOT be deployable merely because it exists in the registry.

The gate enforces, in order:
  1. strategy exists in the registry
  2. spec deserialization is valid (StrategySpec parses + validates)
  3. spec_hash matches the persisted Strategy (identity binding)
  4. lifecycle status is not RETIRED / REJECTED
  5. Phase 17 eligibility (research + walk-forward evidence; freshness)
  6. evidence requirements (research + walk-forward, min trades, recent)
  7. symbol / timeframe match the persisted strategy
  8. risk configuration is valid
  9. paper-only execution target (handled by the runner, but flagged here)

Each failure produces an explicit reason token. Reasons are stable, lowercase
strings (see PAPER_TRADING_GATE_REASONS).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..execution.broker import Broker
from ..execution.paper_broker import PaperBroker
from ..research.evidence import Strategy, StrategyStatus
from ..research.strategy_intelligence import (
    Eligibility,
    EvidenceFreshnessConfig,
    EvidenceRequirement,
    StrategyIntelligence,
)
from ..research.strategy_lab.spec import StrategySpec
from .deployment import (
    PaperDeployment,
    PaperDeploymentConfig,
    PaperDeploymentStatus,
    deployment_identity,
)


PAPER_TRADING_GATE_REASONS = frozenset({
    "unknown_strategy",
    "retired_strategy",
    "rejected_strategy",
    "missing_walk_forward_evidence",
    "missing_validation_metrics",
    "insufficient_validation_trades",
    "stale_evidence",
    "unknown_evidence_freshness",
    "invalid_strategy_spec",
    "strategy_hash_mismatch",
    "symbol_mismatch",
    "timeframe_mismatch",
    "paper_mode_required",
    "invalid_risk_config",
    "no_live_broker_allowed",
})


@dataclass
class GateDecision:
    """Outcome of a gate evaluation.

    ``passed`` is True iff ``reasons`` is empty. Reasons are drawn from
    PAPER_TRADING_GATE_REASONS so they remain a stable, documented contract.
    """

    passed: bool
    reasons: list[str] = field(default_factory=list)
    deployment: Optional[PaperDeployment] = None
    spec: Optional[StrategySpec] = None

    def __bool__(self) -> bool:
        return self.passed


class DeploymentGate:
    """Phase 18 eligibility + identity gate.

    Stateless: take a registry / intelligence / broker, evaluate, return a
    decision. The actual side-effects (persistence, evidence) are the
    caller's responsibility.
    """

    def __init__(
        self,
        intelligence: StrategyIntelligence,
        requirement: Optional[EvidenceRequirement] = None,
        freshness_config: Optional[EvidenceFreshnessConfig] = None,
    ) -> None:
        self.intelligence = intelligence
        self.requirement = requirement or EvidenceRequirement()
        self.freshness_config = freshness_config or EvidenceFreshnessConfig(
            max_age_days=180
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def evaluate(
        self,
        strategy_id: str,
        spec: StrategySpec,
        symbol: str,
        timeframe: str,
        dataset_id: str,
        config: PaperDeploymentConfig,
    ) -> GateDecision:
        reasons: list[str] = []
        strategy = self.intelligence.registry.get_strategy(strategy_id)

        # 1. strategy exists
        if strategy is None:
            reasons.append("unknown_strategy")
            return GateDecision(passed=False, reasons=reasons)

        # 2. spec deserialization is valid (caller passed a StrategySpec;
        #    we re-validate via model_validate(spec.model_dump()) for safety).
        try:
            spec_check = StrategySpec.model_validate(
                spec.model_dump(mode="json")
            )
        except Exception:
            reasons.append("invalid_strategy_spec")
            return GateDecision(passed=False, reasons=reasons)

        # 3. identity binding (exact spec hash)
        if strategy.spec_hash != spec_check.model_dump_json() and \
                strategy.spec_hash != _spec_identity(spec_check):
            # ``strategy.spec_hash`` is the SHA-256 of the canonical JSON
            # produced by ``strategy_identity``. Recompute defensively.
            pass
        from ..research.evidence import strategy_identity as _sid
        if strategy.spec_hash != _sid(spec_check):
            reasons.append("strategy_hash_mismatch")

        # 4. lifecycle status
        if strategy.status == StrategyStatus.RETIRED:
            reasons.append("retired_strategy")
        if strategy.status == StrategyStatus.REJECTED:
            reasons.append("rejected_strategy")

        # 5/6. Phase 17 eligibility (delegates evidence checks)
        eligibility = self.intelligence.assess_eligibility(
            strategy_id=strategy_id,
            requirement=self.requirement,
            freshness_config=self.freshness_config,
        )
        if eligibility.status != Eligibility.ELIGIBLE:
            for r in eligibility.reasons:
                if r in PAPER_TRADING_GATE_REASONS and r not in reasons:
                    reasons.append(r)

        # 7. symbol / timeframe compatibility
        if strategy.symbol != symbol:
            reasons.append("symbol_mismatch")
        if strategy.timeframe != timeframe:
            reasons.append("timeframe_mismatch")

        # 8. risk configuration is valid (already enforced by pydantic, but
        #    double-check that the config explicitly requires paper mode).
        if config.execution_mode != "paper":
            reasons.append("paper_mode_required")

        # reasons list is de-duplicated while preserving order
        seen = set()
        deduped = []
        for r in reasons:
            if r not in seen:
                seen.add(r)
                deduped.append(r)
        reasons = deduped

        if reasons:
            return GateDecision(passed=False, reasons=reasons, spec=spec_check)

        deployment = PaperDeployment(
            deployment_id=deployment_identity(
                strategy_id=strategy_id,
                strategy_spec_hash=strategy.spec_hash,
                symbol=symbol,
                timeframe=timeframe,
                dataset_id=dataset_id,
                config=config,
            ),
            strategy_id=strategy_id,
            strategy_spec_hash=strategy.spec_hash,
            symbol=symbol,
            timeframe=timeframe,
            dataset_id=dataset_id,
            config=config,
            status=PaperDeploymentStatus.CREATED,
            notes="",
        )
        return GateDecision(passed=True, reasons=[], deployment=deployment, spec=spec_check)

    # ------------------------------------------------------------------ #
    # Safety guard: PaperBroker-only
    # ------------------------------------------------------------------ #
    @staticmethod
    def assert_paper_broker(broker: Broker) -> None:
        """Hard reject anything that is not the project's PaperBroker.

        Rejects:
          * any non-Broker object
          * the abstract ``Broker`` itself
          * any concrete Broker subclass that is not PaperBroker
          * objects merely claiming to be PaperBroker via duck-typing
        """
        if broker is None:
            raise TypeError("no_live_broker_allowed: broker is None")
        # Direct identity check first (cheap and exact).
        if isinstance(broker, PaperBroker):
            return
        if broker.__class__ is Broker:
            raise TypeError(
                "no_live_broker_allowed: abstract Broker cannot be used as the "
                "paper deployment execution target"
            )
        # Any other subclass of Broker is rejected (no live brokers in scope).
        raise TypeError(
            "no_live_broker_allowed: deployment requires PaperBroker; got "
            f"{type(broker).__module__}.{type(broker).__name__}"
        )


def _spec_identity(spec: StrategySpec) -> str:
    from ..research.evidence import strategy_identity as _sid
    return _sid(spec)