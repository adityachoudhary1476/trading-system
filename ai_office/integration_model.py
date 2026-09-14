"""Integration model for AI Office v0.1.

Design how completed agent work is integrated. Avoids agents directly modifying
the main branch. Uses a dedicated integration workflow with conflict detection,
human approval for high-risk changes, and safe fast-forward merge.

Never silently reset, overwrite, force-push, or rewrite main.
"""

import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone
from .state_transitions import StateMachine
from .path_enforcement import PathEnforcer


class IntegrationModel:
    """Integration model for AI Office v0.1 worktree commits."""

    # Integration base commit — recorded at objective start
    integration_base: Optional[str] = None

    @staticmethod
    def set_integration_base(base_commit: str) -> None:
        """Record the integration base commit at objective start."""
        IntegrationModel.integration_base = base_commit

    @staticmethod
    def get_integration_base() -> Optional[str]:
        """Get the recorded integration base commit."""
        return IntegrationModel.integration_base

    @staticmethod
    def verify_main_not_changed(expected_base: str) -> Tuple[bool, str]:
        """Verify that main HEAD still equals the expected base commit.

        Returns (is_same, message).
        """
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True,
                cwd=".",
            )
            if result.returncode != 0:
                return False, f"Cannot read main HEAD: {result.stderr}"
            actual_head = result.stdout.strip()
            if actual_head == expected_base:
                return True, "main HEAD unchanged — safe to proceed"
            else:
                return False, (
                    f"main HEAD changed: expected {expected_base}, "
                    f"got {actual_head}"
                )
        except Exception as e:
            return False, f"Error checking main HEAD: {e}"

    @staticmethod
    def build_integration_state(
        agent_commits: Dict[str, str],  # task_id -> commit_hash
        base_commit: Optional[str] = None,
        cwd: str = ".",
    ) -> Dict[str, Any]:
        """Build integration state from the base commit and agent commits.

        Returns a dict with the integration state, or raises on error.
        """
        base = base_commit or IntegrationModel.integration_base
        if not base:
            raise ValueError("No integration base commit recorded")

        # Verify base commit exists
        result = subprocess.run(
            ["git", "cat-file", "-e", base],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise ValueError(f"Base commit {base} not found in repository")

        state = {
            "base_commit": base,
            "agent_commits": dict(agent_commits),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "conflicts": [],
        }

        # Conflict detection WITHOUT touching the main working tree.
        # We use `git merge-tree` (plumbing) which computes the merge in
        # memory and reports conflicts — never mutating main. This replaces
        # the previous `git merge --no-commit` in the main tree, which was a
        # safety violation (it could dirty the main checkout mid-run).
        head = IntegrationModel.current_head(cwd)
        for task_id, commit_hash in agent_commits.items():
            conflict_detail = IntegrationModel.detect_merge_conflicts(
                head or base, base, commit_hash, cwd=cwd,
            )
            if conflict_detail is not None:
                state["conflicts"].append({
                    "task_id": task_id,
                    "commit": commit_hash,
                    "conflict": conflict_detail,
                })
                # Do not raise; let the orchestrator handle conflicts.

        return state

    @staticmethod
    def current_head(cwd: str = ".") -> Optional[str]:
        """Return the HEAD commit of the repository at cwd (None if unavailable)."""
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=cwd,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    @staticmethod
    def detect_merge_conflicts(
        onto: str, base: str, commit: str, cwd: str = ".",
    ) -> Optional[str]:
        """Check whether merging `commit` onto `onto` (merge base `base`)
        would produce conflicts, without modifying any working tree.

        Returns a conflict description string when conflicts are detected,
        or None when the merge is clean.
        """
        # Preferred: git >= 2.38 merge-tree --write-tree (in-memory merge).
        result = subprocess.run(
            ["git", "merge-tree", "--write-tree", onto, commit],
            capture_output=True, text=True, cwd=cwd,
        )
        if result.returncode == 0:
            return None
        if result.returncode == 1:
            conflicted = [
                line.strip() for line in result.stdout.split("\n")
                if line.strip() and not line.strip().startswith(("TREE_", "PARENT"))
            ]
            # Output format: first line is the written tree oid (safe to
            # discard), subsequent lines list conflicted file names.
            names = conflicted[1:] if len(conflicted) > 1 else []
            return "merge-tree conflicts: " + ", ".join(names or ["(unnamed files)"])
        # Fallback for older git: three-way merge-tree, read-only.
        legacy = subprocess.run(
            ["git", "merge-tree", base, onto, commit],
            capture_output=True, text=True, cwd=cwd,
        )
        if legacy.returncode == 0 and ("changed in both" not in legacy.stdout
                                       and "<<<<<<<" not in legacy.stdout):
            return None
        return (legacy.stdout[:400] or "merge conflicts detected")

    @staticmethod
    def human_approval_required(risk_level: str, agent_commits: Dict[str, str]) -> bool:
        """Determine if human approval is required for the integration.

        High-risk tasks always require human approval.
        """
        from .high_risk import HighRiskDetector
        controls = HighRiskDetector.required_controls(risk_level)
        return bool(controls.get("human_approval", False))

    @staticmethod
    def fast_forward_merge(integration_base: str) -> Tuple[bool, str]:
        """Fast-forward main to the integration state.

        Returns (success, message).
        """
        # First verify main hasn't changed since integration base
        ok, message = IntegrationModel.verify_main_not_changed(integration_base)
        if not ok:
            return False, message

        # Try fast-forward merge
        result = subprocess.run(
            ["git", "merge", "--fast-forward-only"],
            capture_output=True, text=True,
            cwd=".",
        )
        if result.returncode == 0:
            return True, "Fast-forward merge successful — main updated"
        else:
            # Fast-forward failed; main may have progressed
            # We should rebase the integration state against new main
            return False, (
                "Fast-forward merge failed — main has progressed. "
                "Integration state needs rebasing against new main HEAD."
            )

    @staticmethod
    def rollback_integration() -> str:
        """Rollback the integration if it failed before main was changed.

        Returns a description of the rollback action.
        """
        # Since we never modify main until the final fast-forward,
        # the "rollback" is simply discarding the integration state
        IntegrationModel.integration_base = None
        return "Integration state discarded — main unchanged"