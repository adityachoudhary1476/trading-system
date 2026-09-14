"""State machine for AI Office v0.1 — Validated state transitions.

This module enforces valid state transitions for objectives, tasks, and attempts,
preventing invalid mutations and ensuring deterministic behavior.

All state transitions produce orchestrator_events records.
"""

from datetime import datetime, timezone
from typing import Optional


class StateMachine:
    """Validated state transition machine for AI Office entities."""

    # Valid objective states
    OBJECTIVE_VALID = {"pending", "in_progress", "completed", "blocked", "cancelled"}

    # Valid task states
    TASK_VALID = {"pending", "assigned", "in_progress", "complete", "failed", "blocked", "escalated-to-human"}

    # Valid attempt states
    ATTEMPT_VALID = {"RUNNING", "COMPLETE", "FAILED", "ORPHANED"}

    # Valid transition map for tasks
    TASK_TRANSITIONS = {
        "pending": ["assigned", "in_progress", "blocked", "failed", "escalated-to-human"],
        "assigned": ["in_progress", "blocked", "failed", "escalated-to-human"],
        "in_progress": ["complete", "failed", "blocked", "escalated-to-human", "ORPHANED"],
        "complete": [],  # terminal
        "failed": ["assigned", "in_progress"],  # retry
        "blocked": ["in_progress", "escalated-to-human"],
        "escalated-to-human": [],  # terminal
    }

    # Valid transition map for attempts
    ATTEMPT_TRANSITIONS = {
        "RUNNING": ["COMPLETE", "FAILED", "ORPHANED"],
        "COMPLETE": [],  # terminal
        "FAILED": ["RUNNING"],  # retry (up to max_attempts)
        "ORPHANED": ["RUNNING"],  # retry with fresh worktree
    }

    @staticmethod
    def validate_objective_status(status: str) -> bool:
        """Validate an objective status value."""
        return status in StateMachine.OBJECTIVE_VALID

    @staticmethod
    def validate_task_status(status: str) -> bool:
        """Validate a task status value."""
        return status in StateMachine.TASK_VALID

    @staticmethod
    def validate_attempt_status(status: str) -> bool:
        """Validate an attempt status value."""
        return status in StateMachine.ATTEMPT_VALID

    @staticmethod
    def can_transition_task(from_status: str, to_status: str) -> bool:
        """Check if a task state transition is valid."""
        allowed = StateMachine.TASK_TRANSITIONS.get(from_status, [])
        return to_status in allowed

    @staticmethod
    def can_transition_attempt(from_status: str, to_status: str) -> bool:
        """Check if an attempt state transition is valid."""
        allowed = StateMachine.ATTEMPT_TRANSITIONS.get(from_status, [])
        return to_status in allowed

    @staticmethod
    def transition_objective(current: str, target: str) -> Optional[str]:
        """Transition objective status, returning error if invalid."""
        if current not in StateMachine.OBJECTIVE_VALID:
            return f"Invalid current objective status: {current}"
        if target not in StateMachine.OBJECTIVE_VALID:
            return f"Invalid target objective status: {target}"
        # Business logic transitions
        if current == "pending" and target == "in_progress":
            return None  # valid
        if current == "in_progress" and target == "completed":
            return None  # valid
        if current == "in_progress" and target == "blocked":
            return None  # valid
        if current == "in_progress" and target == "cancelled":
            return None  # valid
        if current == "completed":
            return "Cannot transition from completed objective"
        if current == "cancelled":
            return "Cannot transition from cancelled objective"
        return f"Invalid transition from {current} to {target}"

    @staticmethod
    def transition_task(current: str, target: str) -> Optional[str]:
        """Transition task status, returning error if invalid.

        Returns None when the transition is valid, or an error message.
        """
        if current not in StateMachine.TASK_VALID:
            return f"Invalid current task status: {current}"
        if target not in StateMachine.TASK_VALID:
            return f"Invalid target task status: {target}"
        if not StateMachine.can_transition_task(current, target):
            return f"Transition {current!r} -> {target!r} is not allowed"
        return None

    @staticmethod
    def transition_attempt(current: str, target: str) -> Optional[str]:
        """Transition attempt status, returning error if invalid.

        Returns None when the transition is valid, or an error message.
        """
        if current not in StateMachine.ATTEMPT_VALID:
            return f"Invalid current attempt status: {current}"
        if target not in StateMachine.ATTEMPT_VALID:
            return f"Invalid target attempt status: {target}"
        if not StateMachine.can_transition_attempt(current, target):
            return f"Transition {current!r} -> {target!r} is not allowed"
        return None