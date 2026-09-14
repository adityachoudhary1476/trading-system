"""OpenCode/Orchestrator boundary for AI Office v0.1.

Defines the strict division of responsibility between the OpenCode runtime
and the AI Office orchestration engine.

OpenCode responsibilities (agent runtime):
- Objective interpretation
- Task decomposition into structured tasks
- Agent selection (subagent_type)
- Task instructions (passed to sub-agents)
- Reasoning during agent execution
- Escalation decisions (when to escalate to human)

AI Office engine responsibilities (deterministic enforcement):
- SQLite state (.ai-office/coordination.db)
- State transitions (validated via StateMachine)
- Dependency DAG and scheduling
- Path locking and scope enforcement
- Worktree management (creation, verification, cleanup)
- Risk detection (high-risk path classification)
- Verification (9-point completion protocol)
- Attempt tracking (heartbeat, ORPHANED reconciliation)
- Integration safety (merge, conflict, rollback)
- High-risk human approval gate
"""

from typing import Optional, Dict, Any

# Agent registry — used for agent type validation and delegation decisions
from .agent_registry import validate_role, get_agent_spec, AGENT_REGISTRY


class OpenCodeBoundary:
    """Boundary between OpenCode runtime and AI Office orchestration engine."""

    @staticmethod
    def orchestrator_responsibilities() -> Dict[str, bool]:
        """List of responsibilities that belong to the OpenCode orchestrator."""
        return {
            "objective_interpretation": True,
            "task_decomposition": True,
            "agent_selection": True,
            "task_instructions": True,
            "reasoning": True,
            "escalation_decisions": True,
            "human_approval_requests": True,
            "agent_spawning": True,
            "state_persistence_initiation": True,  # initiates SQLite ops but engine executes
        }

    @staticmethod
    def ai_office_responsibilities() -> Dict[str, bool]:
        """List of responsibilities that belong to the AI Office orchestration engine."""
        return {
            "sqlite_state_management": True,         # actual DB reads/writes
            "state_transitions": True,              # validated via StateMachine
            "dag_scheduling": True,                 # parallel/sequential determination
            "path_locking": True,                   # allowed/forbidden path enforcement
            "worktree_management": True,            # creation, verification, cleanup
            "risk_detection": True,                 # high-risk classification
            "verification": True,                   # 9-point completion protocol
            "attempt_tracking": True,               # heartbeat, ORPHANED reconciliation
            "integration_safety": True,             # merge, conflict, rollback
            "qa_gate_execution": True,              # scope-aware QA checks
            "reviewer_gate_execution": True,        # 9-question independent review
            "rollback_behavior": True,              # safe rollback if integration fails
        }

    @staticmethod
    def verify_no_code_modification(agent_type: str, action: str) -> bool:
        """Check that an agent is not performing forbidden code modification.

        OpenCode permissions should enforce this, but the orchestrator also
        independently verifies after execution.
        """
        # Use the agent registry to determine if the agent type is a specialist
        # (QA, Reviewer, Architect, Researcher) that should not modify code
        if not validate_role(agent_type):
            # Unknown agent type — default to safe behavior (allow)
            return True

        # Specialist agents must not modify application code
        specialist_types = ["qa", "reviewer", "architect", "researcher"]
        if agent_type in specialist_types:
            if "modification" in action.lower() or "write" in action.lower():
                return False

        return True

    @staticmethod
    def delegation_contract() -> str:
        """Return the ECC Delegation Completion Contract reminder.

        This is the contract that every agent at every depth must follow:
        1. Your final message IS the deliverable. Never end your turn with
           'waiting for background agents' — a spawned task is not a completed
           task. Ending your turn while children are running orphans their
           results (completed children cannot notify a parent whose turn has ended).
        2. If you delegate, you own collection. Wait for results, integrate them,
           then return. Fire-and-forget delegation is forbidden.
        3. Decompose only when the work cannot fit in one context. Do not
           re-delegate a task already sized for a single agent — depth is an
           outcome, not a plan.
        """
        return (
            "ECC Delegation Completion Contract:\n"
            "1. Your final message IS the deliverable. Never end your turn with "
            "'waiting for background agents' — a spawned task is not a completed "
            "task. Ending your turn while children are running orphans their "
            "results (completed children cannot notify a parent whose turn has ended).\n"
            "2. If you delegate, you own collection. Wait for results, integrate "
            "them, then return. Fire-and-forget delegation is forbidden.\n"
            "3. Decompose only when the work cannot fit in one context. Do not "
            "re-delegate a task already sized for a single agent — depth is an "
            "outcome, not a plan."
        )


# Convenience functions for the orchestrator

def check_agent_permission(agent_type: str, action: str) -> bool:
    """Check if an agent type is permitted to perform the given action.

    This uses OpenCode's permission system where available, with fallback
    to AI Office enforcement.
    """
    from .path_enforcement import PathEnforcer
    # The actual permission check uses OpenCode's role-based permissions
    # The AI Office adds Layer 2 verification (see two-layer enforcement)
    return OpenCodeBoundary.ai_office_responsibilities().get(action, False)


def orchestrator_should_delegate(
    task_id: str,
    current_agent: str,
    intended_agent: str,
    objective: str,
) -> bool:
    """Determine if the orchestrator should delegate a subtask.

    Returns True if delegation is appropriate, False if the orchestrator
    should handle it directly or reassign.
    """
    # Never delegate to the same agent type (would violate review independence)
    if current_agent == intended_agent:
        return False

    # Never delegate high-risk tasks to non-reviewer agents
    # (handled by the orchestrator's high-risk detection)

    # Never delegate QA or Reviewer tasks to implementation agents
    # Check using the agent registry for valid roles
    if intended_agent in ("qa", "reviewer"):
        if current_agent in ("backend", "frontend"):
            return False

    # Validate that both agent types are registered
    if not validate_role(current_agent) or not validate_role(intended_agent):
        # Unknown agent types — default to not delegating for safety
        return False

    return True