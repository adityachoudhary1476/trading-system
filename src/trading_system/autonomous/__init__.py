"""Autonomous module — Phase 1.

Public API for the autonomous trading architecture layer.

Phase 1 establishes:
  - AutonomousBot configuration/policy model
  - AutonomousBot lifecycle/state model
  - AutonomousController abstraction/service
  - Structured AutonomousDecision model
  - AutonomousDeploymentCoordinator boundary
  - Autonomous vs manual deployment attribution
  - Auditable autonomous decisions/events
  - Failure safety paths

No autonomous trading intelligence, market scanning, or strategy selection
is implemented in this phase.
"""

from __future__ import annotations

from .bot_config import (
    AutonomousBotConfig,
    BotMode,
    BotState,
    CapitalAllocation,
    MaxExposurePct,
    MaxPositionPct,
    UserConstraints,
    AutonomousDecision,
    PolicyValidationResult,
    PolicyValidator,
    Source,
)
from .bot_lifecycle import (
    AutonomousBotState,
    AutonomousBotLifecycle,
    BotTransitionError,
    is_valid_transition,
)
from .controller import AutonomousController
from .coordinator import (
    AutonomousDeploymentCoordinator,
    DeploymentCreationResult,
)
from .events import (
    AutonomousEvent,
    AutonomousEventLog,
    AutonomousEventType,
    make_autonomous_event_id,
)
from .compatibility import (
    CompatibilityConfig,
    CompatibilityResult,
    CompatibilityExclusion,
    CompatibilityExclusionReason,
    CompatibilityFeatures,
    StrategyCompatibility,
    StrategyCompatibilityProfile,
    StrategyCompatibilityEvaluator,
    FeatureDirection,
    get_family_profile,
)
from .ranker import (
    OpportunityRanker,
    OpportunityRankingResult,
    OpportunityFeatures,
    RankedOpportunity,
    RankerConfig,
    RankExclusion,
    RankExclusionReason,
)
from .scanner import (
    MarketScanResult,
    MarketScanner,
    MarketUniverse,
    ScannerConfig,
    ScanCandidate,
    ScanRejection,
    RejectionReason as ScannerRejectionReason,
)
from .decision import (
    DecisionExclusionReason,
    DecisionResult,
    DecisionStatus,
    SelectionConfig,
    SelectionFactor,
    SelectionPolicy,
    SelectedConfiguration,
    StrategyDecisionEngine,
    TradingDecision,
)
from .options_contract import (
    DEFAULT_OPTIONS_CONFIG,
    InMemoryOptionsChainProvider,
    OptionContractResolver,
    OptionLeg,
    OptionsChain,
    OptionsChainProvider,
    OptionsInstrument,
    OptionsStructureBuilder,
    OptionsStrategy,
    OptionsTradeConfig,
    OptionsTradePlan,
    OptionQuote,
    OptionStyle,
    ResolveResult,
    StrategyMapping,
)
from .safety import (
    IdempotencyGuard,
    KillSwitch,
    KillSwitchReason,
    KillSwitchState,
    Phase7Config,
    Phase7SafetyLayer,
    SafetyCheck,
    SafetyResult,
    SafetyValidator,
)

__all__ = [
    # Configuration
    "AutonomousBotConfig",
    "BotMode",
    "BotState",
    "CapitalAllocation",
    "DeploymentCreationResult",
    "MaxExposurePct",
    "MaxPositionPct",
    "UserConstraints",
    "AutonomousDecision",
    "PolicyValidationResult",
    "PolicyValidator",
    "Source",
    # Lifecycle
    "AutonomousBotState",
    "AutonomousBotLifecycle",
    "BotTransitionError",
    "is_valid_transition",
    # Controller
    "AutonomousController",
    # Coordinator
    "AutonomousDeploymentCoordinator",
    # Events
    "AutonomousEvent",
    "AutonomousEventLog",
    "AutonomousEventType",
    "make_autonomous_event_id",
    # Phase 3 — Ranker
    "OpportunityRanker",
    "OpportunityRankingResult",
    "OpportunityFeatures",
    "RankedOpportunity",
    "RankerConfig",
    "RankExclusion",
    "RankExclusionReason",
    # Phase 2 — Scanner
    "MarketScanResult",
    "MarketScanner",
    "MarketUniverse",
    "ScannerConfig",
    "ScanCandidate",
    "ScanRejection",
    "ScannerRejectionReason",
    # Phase 4 — Compatibility
    "CompatibilityConfig",
    "CompatibilityResult",
    "CompatibilityExclusion",
    "CompatibilityExclusionReason",
    "CompatibilityFeatures",
    "StrategyCompatibility",
    "StrategyCompatibilityProfile",
    "StrategyCompatibilityEvaluator",
    "FeatureDirection",
    "get_family_profile",
    # Phase 5 -- Strategy Selection & Signal Generation
    "DecisionExclusionReason",
    "DecisionResult",
    "DecisionStatus",
    "SelectionConfig",
    "SelectionFactor",
    "SelectionPolicy",
    "SelectedConfiguration",
    "StrategyDecisionEngine",
    "TradingDecision",
    # Phase 7 — Safety, Kill-Switch, and Recovery Layer
    "KillSwitch",
    "KillSwitchState",
    "KillSwitchReason",
    "SafetyValidator",
    "SafetyResult",
    "SafetyCheck",
    "IdempotencyGuard",
    "Phase7Config",
    "Phase7SafetyLayer",
    # Phase 8 — Options Contract Selection Layer
    "OptionStyle",
    "OptionsStrategy",
    "OptionQuote",
    "OptionsChain",
    "OptionsInstrument",
    "OptionLeg",
    "OptionsTradePlan",
    "StrategyMapping",
    "OptionsTradeConfig",
    "OptionsChainProvider",
    "InMemoryOptionsChainProvider",
    "OptionContractResolver",
    "ResolveResult",
    "OptionsStructureBuilder",
    "DEFAULT_OPTIONS_CONFIG",
]