"""Phase 21 — Checkpoint policy.

A pure, deterministic, caller-driven checkpoint policy. The API never
spawns an autonomous background loop. Instead, the caller (e.g. the
HTTP adapter or a worker) calls :func:`evaluate_checkpoint_policy` after
each bar is processed; if the decision is ``CHECKPOINT``, the caller is
responsible for invoking ``PaperTradingControlCenter.save_session``.

Hard rules:

  * Default policy is **disabled** (no checkpoints are ever written
    unless the caller explicitly enables a policy).
  * ``every_n_bars`` must be a positive integer.
  * ``drawdown_threshold_pct`` must be a fraction in [0, 1].
  * The policy never mutates the runner, broker, or deployment.
  * The policy never places orders, never bypasses the circuit breaker,
    and never alters trading decisions.
  * The policy's decision is a pure function of the supplied state.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CheckpointDecision(str, Enum):
    """The output of :func:`evaluate_checkpoint_policy`."""

    SKIP = "skip"
    CHECKPOINT = "checkpoint"


class CheckpointPolicy(BaseModel):
    """Explicit, caller-configured checkpoint policy.

    All fields are optional; the default (``enabled=False``) is a
    no-op. A policy only triggers a checkpoint when:

      * it is enabled, AND
      * at least one trigger (``every_n_bars`` or
        ``drawdown_threshold_pct``) is set, AND
      * the trigger condition is satisfied.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    every_n_bars: Optional[int] = Field(default=None, ge=1)
    drawdown_threshold_pct: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate(self) -> "CheckpointPolicy":
        if self.enabled:
            if self.every_n_bars is None and self.drawdown_threshold_pct is None:
                raise ValueError(
                    "CheckpointPolicy.enabled=True requires at least one of "
                    "every_n_bars or drawdown_threshold_pct to be set"
                )
        return self


def evaluate_checkpoint_policy(
    *,
    policy: CheckpointPolicy,
    bar_count: int,
    max_drawdown: Optional[float],
    last_checkpoint_bar: Optional[int] = None,
) -> CheckpointDecision:
    """Pure decision function for a single policy evaluation.

    Inputs:

      * ``policy``              — the configured CheckpointPolicy.
      * ``bar_count``           — total processed bars in the live runner.
      * ``max_drawdown``        — current max-drawdown fraction
                                 (negative or zero; ``None`` if unknown).
      * ``last_checkpoint_bar`` — the bar_count at which the last
                                 checkpoint was written, or ``None`` if
                                 none has been written yet.

    Output:

      * :class:`CheckpointDecision.SKIP` (default) or
        :class:`CheckpointDecision.CHECKPOINT`.

    The function never mutates state and never raises. An invalid policy
    (disabled or unset triggers) always returns ``SKIP``.
    """
    if not policy.enabled:
        return CheckpointDecision.SKIP
    if policy.every_n_bars is None and policy.drawdown_threshold_pct is None:
        return CheckpointDecision.SKIP

    # Trigger 1: every-N-bars. Fires when (bar_count - last_checkpoint_bar)
    # reaches the configured interval. If no prior checkpoint exists,
    # treat last_checkpoint_bar as 0 so the policy fires after the first
    # configured bar.
    if policy.every_n_bars is not None:
        base = last_checkpoint_bar if last_checkpoint_bar is not None else 0
        if bar_count - base >= policy.every_n_bars:
            return CheckpointDecision.CHECKPOINT

    # Trigger 2: drawdown threshold. Fires when |max_drawdown| has
    # reached or exceeded the configured percentage.
    if policy.drawdown_threshold_pct is not None and max_drawdown is not None:
        if abs(float(max_drawdown)) >= policy.drawdown_threshold_pct:
            return CheckpointDecision.CHECKPOINT

    return CheckpointDecision.SKIP