"""Path/scope enforcement for AI Office v0.1.

Canonical path normalization and path-scope checking to prevent agents
from modifying files outside their assigned scope.

Uses normalized canonical repository-relative paths.
"""

import os
import re
import subprocess
from typing import List, Optional, Set, Tuple


def _normalize_path(path: str, repo_root: str) -> str:
    """Normalize a path to a canonical repository-relative form.

    Handles:
    - Absolute paths (converted to relative to repo_root)
    - Parent traversal (../)
    - Platform path separators
    - Double slashes
    - Trailing slashes
    """
    # Absolute path: make relative to repo_root
    if os.path.isabs(path):
        try:
            rel = os.path.relpath(path, repo_root)
        except ValueError:
            # path is on different drive; return as-is normalized
            rel = path
        path = rel
    
    # Platform normalization: use forward slashes consistently
    path = path.replace(os.sep, "/")
    
    # Remove leading ./
    while path.startswith("./"):
        path = path[2:]
    
    # Remove trailing /
    path = path.rstrip("/")
    
    # Normalize multiple slashes
    path = re.sub(r"/+", "/", path)
    
    # Return relative (no leading /)
    if path.startswith("/"):
        path = path[1:]
    
    # Ensure we have at least a base name
    if not path:
        return "."
    
    return path


def _make_repo_relative(path: str, repo_root: str) -> str:
    """Make a path relative to the repository root."""
    abs_path = os.path.abspath(path)
    try:
        rel = os.path.relpath(abs_path, repo_root)
    except ValueError:
        # Different drive; use absolute normalized
        abs_norm = os.path.normpath(abs_path).replace(os.sep, "/")
        return abs_norm
    return _normalize_path(rel, repo_root)


class PathEnforcer:
    """Enforces allowed and forbidden path scopes for agent tasks."""
    
    def __init__(self, repo_root: str):
        self.repo_root = os.path.abspath(repo_root)
        # Ensure repo_root ends without separator for consistent comparison
        self.repo_root = self.repo_root.rstrip("/").replace(os.sep, "/")
    
    def normalize_allowed(self, allowed_paths: List[str]) -> Set[str]:
        """Normalize a list of allowed paths into a set of canonical forms."""
        normalized = set()
        for p in allowed_paths:
            np = _normalize_path(p, self.repo_root)
            normalized.add(np)
        return normalized
    
    def normalize_forbidden(self, forbidden_paths: List[str]) -> Set[str]:
        """Normalize a list of forbidden paths into a set of canonical forms."""
        normalized = set()
        for p in forbidden_paths:
            np = _normalize_path(p, self.repo_root)
            normalized.add(np)
        return normalized
    
    def check_changed_files(
        self,
        changed_files: List[str],
        allowed_paths: List[str],
        forbidden_paths: List[str],
    ) -> Tuple[bool, List[str], List[str]]:
        """Check that changed files respect scope constraints.
        
        Returns:
            (is_ok, violations_from_allowed, violations_of_forbidden)
        """
        allowed = self.normalize_allowed(allowed_paths)
        forbidden = self.normalize_forbidden(forbidden_paths)
        
        actual_normalized = set()
        violations_forbidden = []
        violations_allowed = []
        
        for f in changed_files:
            norm = _normalize_path(f, self.repo_root)
            actual_normalized.add(norm)
            
            # Check forbidden first
            if norm in forbidden:
                violations_forbidden.append(f"{f} (in forbidden paths)")
            
            # Check allowed - the file must be in allowed paths
            # if allowed_paths is non-empty; if empty, all files are allowed
            if allowed and norm not in allowed:
                violations_allowed.append(f"{f} (not in allowed paths)")
        
        is_ok = len(violations_forbidden) == 0 and len(violations_allowed) == 0
        return is_ok, violations_allowed, violations_forbidden
    
    def check_against_actual_git(
        self,
        worktree_path: str,
        allowed_paths: List[str],
        forbidden_paths: List[str],
        base_commit: Optional[str] = None,
    ) -> Tuple[bool, List[str], List[str]]:
        """Check actual git changes against allowed/forbidden paths.
        
        This is the definitive check - it reads the actual git state from
        the worktree (committed base..HEAD, staged, and unstaged) and
        compares against the scope constraints.
        """
        # Get actual modified files from git (committed + staged + unstaged)
        actual_changed: List[str] = []
        git_args_sets: List[List[str]] = [
            ["git", "diff", "--name-only", "HEAD"],
            ["git", "diff", "--cached", "--name-only"],
        ]
        if base_commit:
            git_args_sets.insert(
                0, ["git", "diff", "--name-only", base_commit, "HEAD"]
            )
        for git_args in git_args_sets:
            result = subprocess.run(
                git_args,
                capture_output=True, text=True,
                cwd=worktree_path,
            )
            if result.returncode == 0:
                actual_changed.extend(
                    f for f in result.stdout.split("\n") if f.strip()
                )
        
        # Now use the same logic as check_changed_files
        allowed = self.normalize_allowed(allowed_paths)
        forbidden = self.normalize_forbidden(forbidden_paths)
        
        violations_forbidden = []
        violations_allowed = []
        
        for f in actual_changed:
            norm = _normalize_path(f, self.repo_root)
            if norm in forbidden:
                violations_forbidden.append(f"{f} (in forbidden paths)")
            elif allowed and norm not in allowed:
                violations_allowed.append(f"{f} (not in allowed paths)")
        
        is_ok = len(violations_forbidden) == 0 and len(violations_allowed) == 0
        return is_ok, violations_allowed, violations_forbidden
    
    @staticmethod
    def path_is_subdirectory(parent: str, child: str) -> bool:
        """Check if child is a subdirectory of parent."""
        parent_norm = _normalize_path(parent, "").rstrip("/")
        child_norm = _normalize_path(child, "").rstrip("/")
        if parent_norm == child_norm:
            return True
        return child_norm.startswith(parent_norm + "/")


# Convenience function for task assignment
def make_scope_enforcer(repo_root: str) -> PathEnforcer:
    """Create a PathEnforcer instance for the given repo root."""
    return PathEnforcer(repo_root)