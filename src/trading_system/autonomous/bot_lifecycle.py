"""Autonomous Bot Lifecycle — Phase 1.

Explicit state model with deterministic valid transitions.
Analogous to the existing deployment lifecycle but for the autonomous bot itself.
Invalid state transitions are rejected.
"""

from __future__ import annotations

from enum import Enum
from typing import FrozenSet, Set


class AutonomousBotState(str, Enum):
    """Explicit bot lifecycle states — Phase 1 states.

    CREATED    ->  STARTING
    STARTING   ->  RUNNING | ERROR
    RUNNING    ->  PAUSED | STOPPING
    PAUSED     ->  RUNNING | STOPPING
    STOPPING   ->  STOPPED
    STOPPED    ->  (terminal)
    ERROR      ->  STOPPED
    """
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


# --------------------------------------------------------------------------- #
# Valid transition graph.

# Maps current state -> set of allowed target states.
_VALID_TRANSITIONS: dict[AutonomousBotState, Set[AutonomousBotState]] = {
    AutonomousBotState.CREATED: {AutonomousBotState.STARTING},
    AutonomousBotState.STARTING: {AutonomousBotState.RUNNING, AutonomousBotState.ERROR},
    AutonomousBotState.RUNNING: {AutonomousBotState.PAUSED, AutonomousBotState.STOPPING},
    AutonomousBotState.PAUSED: {AutonomousBotState.RUNNING, AutonomousBotState.STOPPING},
    AutonomousBotState.STOPPING: {AutonomousBotState.STOPPED},
    AutonomousBotState.STOPPED: set(),  # terminal — no outgoing transitions
    AutonomousBotState.ERROR: {AutonomousBotState.STOPPED},
}


def is_valid_transition(
    current: AutonomousBotState, target: AutonomousBotState,
) -> bool:
    """Check whether a state transition is valid according to the Phase 1 graph.

    Returns True if the transition is allowed, False otherwise.
    This is the core safety invariant: INVALID STATE TRANSITIONS MUST BE REJECTED.
    """
    allowed = _VALID_TRANSITIONS.get(current, set())
    return target in allowed


class BotTransitionError(RuntimeError):
    """Raised when an invalid bot state transition is attempted."""

    def __init__(self, current: AutonomousBotState, target: AutonomousBotState) -> None:
        self.current = current
        self.target = target
        super().__init__(
            f"invalid bot state transition {current.value} -> {target.value}"
        )


# --------------------------------------------------------------------------- #
# Bot lifecycle model — manages the bot's state machine.
# The controller uses this to track and transition the bot state
# deterministically and testably.
# --------------------------------------------------------------------------- #


class AutonomousBotLifecycle:
    """Deterministic state machine for an AutonomousBot.

    Guarantees:
      - State transitions are validated against a fixed graph.
      - Invalid transitions raise BotTransitionError.
      - The state is always one of the defined AutonomousBotState values.
      - The state machine is deterministic and testable.

    Usage:
      lifecycle = AutonomousBotLifecycle(initial_state=AutonomousBotState.CREATED)
      lifecycle.transition_to(AutonomousBotState.STARTING)  # OK
      lifecycle.transition_to(AutonomousBotState.CREATED)   # raises BotTransitionError
    """

    def __init__(self, initial_state: AutonomousBotState = AutonomousBotState.CREATED) -> None:
        self._state = initial_state

    @property
    def state(self) -> AutonomousBotState:
        """Current bot lifecycle state."""
        return self._state

    def transition_to(self, target: AutonomousBotState) -> None:
        """Transition the bot to a new state.

        Raises BotTransitionError if the transition is not in the valid graph.
        """
        if not is_valid_transition(self._state, target):
            raise BotTransitionError(self._state, target)
        self._state = target

    def can_transition_to(self, target: AutonomousBotState) -> bool:
        """Check whether a transition to the target state is valid."""
        return is_valid_transition(self._state, target)

    def __str__(self) -> str:
        return f"AutonomousBotLifecycle(state={self._state.value})"

    def __repr__(self) -> str:
        return f"AutonomousBotLifecycle(state={self._state!r})"