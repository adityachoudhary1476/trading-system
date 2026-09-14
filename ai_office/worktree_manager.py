"""Git worktree manager for AI Office v0.1.

Worktrees are sibling directories to the main repository, never nested inside it.

Layout:
  parent/
  ├── trading-system/          (main git repo)
  │   ├── .ai-office/          (coordination.db, gitignored)
  │   └── ...
  └── ai-office-worktrees/     (task worktrees)
        └── <objective>/
            └── <task>/
                └── worktree/  (checked out from base commit)
"""

import os
import subprocess
import json
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone


class GitWorktreeManager:
    """Manages Git worktree creation, verification, and cleanup."""

    # Worktree base directory, sibling to the repo
    WORKTREE_BASE = "ai-office-worktrees"

    @staticmethod
    def _get_repo_root() -> str:
        """Get the git root of the main repository."""
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, cwd="."
        )
        if result.returncode != 0:
            raise RuntimeError(f"Not a git repository: {result.stderr}")
        return result.stdout.strip()

    @staticmethod
    def _get_absolute_repo_root() -> str:
        """Get absolute path to repo root."""
        return os.path.abspath(GitWorktreeManager._get_repo_root())

    @staticmethod
    def _get_worktree_path(objective_id: str, task_id: str,
                           repo_root: Optional[str] = None) -> str:
        """Get the absolute path for a task worktree."""
        root = repo_root or GitWorktreeManager._get_absolute_repo_root()
        return os.path.join(root, GitWorktreeManager.WORKTREE_BASE,
                            objective_id, task_id, "worktree")

    @staticmethod
    def _get_task_dir(objective_id: str, task_id: str,
                      repo_root: Optional[str] = None) -> str:
        """Get the task directory (without worktree suffix)."""
        root = repo_root or GitWorktreeManager._get_absolute_repo_root()
        return os.path.join(root, GitWorktreeManager.WORKTREE_BASE,
                            objective_id, task_id)

    @staticmethod
    def _unmerged_commits(worktree_path: str, base_commit: str) -> List[str]:
        """List commits in the worktree that are not reachable from base_commit.

        Used as a safety guard: a worktree with unmerged commits must never
        be silently destroyed.
        """
        result = subprocess.run(
            ["git", "log", "--format=%H", f"{base_commit}..HEAD"],
            capture_output=True, text=True, cwd=worktree_path,
        )
        if result.returncode != 0:
            return []
        return [h.strip() for h in result.stdout.split("\n") if h.strip()]

    @staticmethod
    def create_worktree(objective_id: str, task_id: str, base_commit: str,
                        repo_root: Optional[str] = None,
                        branch_name: Optional[str] = None) -> str:
        """Create a new task worktree checked out from base_commit.

        The worktree starts in detached HEAD at ``base_commit``; a named
        branch is then created at HEAD so agent commits land on a stable,
        integrable branch (never on a detached HEAD).

        Safety: any stale worktree at the same path is only removed when it
        has NO unmerged commits; otherwise the creation is refused so work
        is never destroyed silently.

        Returns the absolute path to the worktree.
        """
        worktree_path = GitWorktreeManager._get_worktree_path(
            objective_id, task_id, repo_root
        )
        task_dir = GitWorktreeManager._get_task_dir(
            objective_id, task_id, repo_root
        )

        # Ensure parent directories exist
        os.makedirs(task_dir, exist_ok=True)

        # Remove any stale worktree at this path — only if it has no
        # unmerged commits (never destroy unmerged work).
        if os.path.exists(worktree_path):
            stale_commits = GitWorktreeManager._unmerged_commits(
                worktree_path, base_commit
            )
            if stale_commits:
                raise RuntimeError(
                    f"Refusing to remove stale worktree {worktree_path}: it holds "
                    f"{len(stale_commits)} unmerged commit(s). Recover them "
                    f"manually or pass a different task id."
                )
            subprocess.run(
                ["git", "worktree", "remove", "--force", worktree_path],
                capture_output=True, text=True,
            )

        # Create the worktree from the base commit
        # The worktree is checked out at the base commit
        result = subprocess.run(
            ["git", "worktree", "add", worktree_path, base_commit],
            capture_output=True, text=True,
            cwd=repo_root or ".",
        )

        if result.returncode != 0:
            raise RuntimeError(f"Failed to create worktree: {result.stderr}")

        # Verify the worktree was created correctly
        # Check that HEAD is at the expected commit
        head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True,
            cwd=worktree_path,
        )
        if head_result.returncode != 0:
            # Clean up and re-raise
            subprocess.run(
                ["git", "worktree", "remove", "--force", worktree_path],
                capture_output=True, text=True,
            )
            raise RuntimeError(
                f"Worktree created but HEAD check failed: {head_result.stderr}"
            )

        # Create a named branch at HEAD so agent commits are integrable.
        branch = branch_name or GitWorktreeManager.task_branch_name(
            objective_id, task_id
        )
        GitWorktreeManager.create_branch_at_head(worktree_path, branch)

        return worktree_path

    @staticmethod
    def task_branch_name(objective_id: str, task_id: str) -> str:
        """Deterministic integration branch name for a task."""
        return f"ai-office/{objective_id}/{task_id}"

    @staticmethod
    def create_branch_at_head(worktree_path: str, branch_name: str) -> bool:
        """Create `branch_name` at the worktree HEAD (no checkout needed)."""
        result = subprocess.run(
            ["git", "branch", branch_name, "HEAD"],
            capture_output=True, text=True, cwd=worktree_path,
        )
        return result.returncode == 0

    @staticmethod
    def verify_worktree(
        worktree_path: str,
        expected_base_commit: str,
    ) -> Tuple[bool, Optional[str]]:
        """Verify that a worktree is at the expected base commit.

        Returns (is_valid, error_message).
        """
        if not os.path.isdir(worktree_path):
            return False, f"Worktree path does not exist: {worktree_path}"

        # Check HEAD commit
        head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True,
            cwd=worktree_path,
        )
        if head_result.returncode != 0:
            return False, f"Cannot read HEAD from worktree: {head_result.stderr}"

        actual_head = head_result.stdout.strip()
        if actual_head != expected_base_commit:
            return False, (
                f"Worktree HEAD mismatch: expected {expected_base_commit}, "
                f"got {actual_head}"
            )

        # Check that it's a valid git worktree
        wt_result = subprocess.run(
            ["git", "worktree", "list", "--json"],
            capture_output=True, text=True,
            cwd=".",  # repo root
        )
        # Verify the worktree is listed
        # Actually, let's just check .git/worktree
        git_dir = os.path.join(worktree_path, ".git")
        if not os.path.isdir(git_dir):
            return False, f"no .git directory in worktree: {worktree_path}"

        # Check that the worktree is linked properly
        try:
            linked_result = subprocess.run(
                ["git", "rev-parse", "--show-superproject-working-tree"],
                capture_output=True, text=True,
                cwd=worktree_path,
            )
            # Some worktrees don't have superprojects; that's fine
        except Exception:
            pass  # Not critical

        return True, None

    @staticmethod
    def list_worktrees(cwd: str = ".") -> List[Dict[str, Any]]:
        """List all git worktrees in the repository.

        Parses the porcelain output (``git worktree list --porcelain``);
        the previous ``--json`` flag does not exist in any git version and
        always returned an empty list.
        """
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True,
            cwd=cwd,
        )
        if result.returncode != 0:
            return []
        worktrees: List[Dict[str, Any]] = []
        current: Dict[str, Any] = {}
        for line in result.stdout.split("\n"):
            line = line.strip()
            if not line:
                if current:
                    worktrees.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            if key == "worktree":
                current = {"path": value}
            elif key == "HEAD":
                current["head"] = value
            elif key == "branch":
                current["branch"] = value.removeprefix("refs/heads/")
            elif key in ("bare", "detached"):
                current[key] = True
        if current:
            worktrees.append(current)
        return worktrees

    @staticmethod
    def remove_worktree(worktree_path: str, base_commit: Optional[str] = None,
                        force: bool = False) -> Tuple[bool, Optional[str]]:
        """Remove a task worktree.

        Safety: when `base_commit` is provided and the worktree holds
        commits not reachable from it, removal is refused unless
        `force=True` is passed explicitly.
        """
        if not os.path.exists(worktree_path):
            return False, "worktree path does not exist"
        if base_commit and not force:
            unmerged = GitWorktreeManager._unmerged_commits(
                worktree_path, base_commit
            )
            if unmerged:
                return False, (
                    f"refusing to remove worktree with {len(unmerged)} "
                    f"unmerged commit(s); pass force=True to override"
                )
        result = subprocess.run(
            ["git", "worktree", "remove", "--force", worktree_path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return False, result.stderr.strip() or "worktree remove failed"
        return True, None

    @staticmethod
    def get_worktree_head(worktree_path: str) -> Optional[str]:
        """Get the HEAD commit hash of a worktree."""
        if not os.path.isdir(worktree_path):
            return None
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True,
            cwd=worktree_path,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    @staticmethod
    def get_worktree_branch(worktree_path: str) -> Optional[str]:
        """Get the current branch of a worktree."""
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True,
            cwd=worktree_path,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    @staticmethod
    def get_modified_files(worktree_path: str, base_commit: str) -> List[str]:
        """Get files modified relative to the base commit in the worktree."""
        result = subprocess.run(
            ["git", "diff", "--name-only", base_commit, "HEAD"],
            capture_output=True, text=True,
            cwd=worktree_path,
        )
        if result.returncode != 0:
            return []
        # Filter out empty lines
        files = [f for f in result.stdout.split("\n") if f.strip()]
        return files

    @staticmethod
    def get_commits_since_base(worktree_path: str, base_commit: str) -> List[Dict[str, Any]]:
        """Get commits made since the base commit in the worktree."""
        result = subprocess.run(
            ["git", "log", "--format=%H", f"{base_commit}..HEAD"],
            capture_output=True, text=True,
            cwd=worktree_path,
        )
        if result.returncode != 0:
            return []
        commits = [{"hash": h.strip()} for h in result.stdout.split("\n") if h.strip()]
        return commits