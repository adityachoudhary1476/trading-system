"""AI Office v0.1 — Deterministic multi-agent orchestration layer.

This package provides the deterministic operations for task orchestration,
replacing ad-hoc LLM state management with validated, version-controlled
operations backed by SQLite.
"""

from .coordination import CoordinationLayer
from .state_transitions import StateMachine
from .task_contract import TaskContract, AgentResult
from .worktree_manager import GitWorktreeManager
from .path_enforcement import PathEnforcer
from .high_risk import HighRiskDetector
from .scheduler import DAGScheduler
from .completion import CompletionVerifier
from .qa_engine import QAEngine
from .review_engine import ReviewEngine
from .integration_model import IntegrationModel
from .orchestrator_boundary import OpenCodeBoundary
from .agent_registry import AGENT_REGISTRY, AgentSpec, all_roles, get_agent_spec
from .adapter import (
    AgentDispatcher,
    CommandExecutor,
    OpenCodeExecutor,
    extract_result_payload,
)

__all__ = [
    "CoordinationLayer",
    "StateMachine",
    "TaskContract",
    "AgentResult",
    "GitWorktreeManager",
    "PathEnforcer",
    "HighRiskDetector",
    "DAGScheduler",
    "CompletionVerifier",
    "QAEngine",
    "ReviewEngine",
    "IntegrationModel",
    "OpenCodeBoundary",
    "AGENT_REGISTRY",
    "AgentSpec",
    "all_roles",
    "get_agent_spec",
    "AgentDispatcher",
    "CommandExecutor",
    "OpenCodeExecutor",
    "extract_result_payload",
]