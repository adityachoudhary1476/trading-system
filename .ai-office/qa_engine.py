"""QA engine for AI Office v0.1.

Scope-aware test execution and invariant verification.

QA determines the relevant validation suite based on changed files and task
scope, NOT a hard-coded universal command. All results are real command
outcomes — no fabricated evidence. The core test command is configurable per
project (defaults to this repository's Phase 21/22 suite).
"""

import subprocess
import os
from typing import Any, Dict, List, Optional, Tuple

from .high_risk import HIGH_RISK_CATEGORIES
from .path_enforcement import PathEnforcer, _normalize_path


class QAEngine:
    """Scope-aware QA verification for AI Office v0.1."""

    # Repo-relative paths that must never be modified without human approval.
    LIVE_TRADING_PATHS = HIGH_RISK_CATEGORIES["CRITICAL"]["patterns"]

    def __init__(self, repo_root: str,
                 core_test_command: Optional[List[str]] = None,
                 frontend_dir: str = "frontend"):
        self.repo_root = os.path.abspath(repo_root)
        self.enforcer = PathEnforcer(repo_root)
        # Default core suite for THIS repository (Phase 20/21/22 coverage).
        self.core_test_command = core_test_command or [
            "python", "-m", "pytest",
            "tests/test_phase21_api.py", "tests/test_phase22_api.py",
            "tests/test_phase20_control_center.py",
            "-q",
        ]
        self.frontend_dir = frontend_dir

    @staticmethod
    def _changed_files(worktree_path: Optional[str],
                       base_commit: Optional[str]) -> List[str]:
        """Real changed files: committed (base..HEAD) + staged + unstaged."""
        if not worktree_path or not os.path.isdir(worktree_path):
            return []
        files: List[str] = []
        args_sets: List[List[str]] = [["git", "diff", "--name-only", "HEAD"],
                                      ["git", "diff", "--cached", "--name-only"]]
        if base_commit:
            args_sets.insert(0, ["git", "diff", "--name-only", base_commit, "HEAD"])
        for git_args in args_sets:
            result = subprocess.run(
                git_args, capture_output=True, text=True, cwd=worktree_path,
            )
            if result.returncode == 0:
                files.extend(f for f in result.stdout.split("\n") if f.strip())
        return sorted(set(files))

    def _run_pytest(self, cwd: str, args: List[str]) -> Tuple[bool, str]:
        """Run pytest in a directory; return (ok, output_excerpt)."""
        result = subprocess.run(
            ["python", "-m", "pytest"] + args,
            capture_output=True, text=True, cwd=cwd,
        )
        output = (result.stdout or result.stderr or "").strip()
        return result.returncode == 0, output[-400:] if output else ""

    def run_qa(self, task: Dict[str, Any], worktree_path: Optional[str] = None,
               allowed_paths: Optional[List[str]] = None,
               forbidden_paths: Optional[List[str]] = None,
               base_commit: Optional[str] = None) -> Dict[str, Any]:
        """Run QA verification for a task.

        Returns: {"result": "PASS"|"FAIL"|"BLOCKED", "evidence": [...]}
        """
        task_scope = task.get("scope", "unknown")
        allowed = allowed_paths if allowed_paths is not None else task.get("allowed_paths", [])
        forbidden = forbidden_paths if forbidden_paths is not None else task.get("forbidden_paths", [])

        checks: List[Dict[str, Any]] = []

        # Determine the relevant test suites based on scope
        if task_scope == "backend":
            checks.extend(self._run_backend_checks(
                task, worktree_path, allowed, forbidden, base_commit))
        elif task_scope == "frontend":
            checks.extend(self._run_frontend_checks(
                task, worktree_path, allowed, forbidden, base_commit))
        elif task_scope == "full":
            checks.extend(self._run_fullstack_checks(
                task, worktree_path, allowed, forbidden, base_commit))
        else:
            # Default: detect scope from the actual changed files
            checks.extend(self._default_checks(
                task, worktree_path, allowed, forbidden, base_commit))

        # Path scope enforcement against the real git state
        if worktree_path and os.path.isdir(worktree_path):
            is_ok, allowed_violations, forbidden_violations = \
                self.enforcer.check_against_actual_git(
                    worktree_path, allowed, forbidden, base_commit=base_commit,
                )
            if not is_ok:
                checks.append({
                    "check": "path_scope_enforcement",
                    "result": "FAIL",
                    "detail": (
                        f"Path scope violations: forbidden={forbidden_violations} "
                        f"out-of-scope={allowed_violations}"
                    ),
                })

        # Paper-only invariant: changed files must not touch live-trading paths
        changed = self._changed_files(worktree_path, base_commit)
        live_hits = sorted(
            f for f in changed
            if any(
                _normalize_path(f, self.repo_root).startswith(
                    _normalize_path(pattern, self.repo_root).rstrip("/") + "/"
                ) or _normalize_path(f, self.repo_root) == _normalize_path(pattern, self.repo_root)
                for pattern in self.LIVE_TRADING_PATHS
            )
        )
        checks.append({
            "check": "paper_only_invariant",
            "result": "FAIL" if live_hits else "PASS",
            "detail": (
                f"Live-trading path changes require human approval: {live_hits}"
                if live_hits else
                "No live-trading paths touched by changed files"
            ),
        })

        # Determine final result
        passed_checks = [c for c in checks if c.get("result") == "PASS"]
        failed_checks = [c for c in checks if c.get("result") == "FAIL"]
        blocked_checks = [c for c in checks if c.get("result") == "BLOCKED"]

        if blocked_checks:
            result = "BLOCKED"
            evidence = [c["detail"] for c in blocked_checks]
        elif failed_checks:
            result = "FAIL"
            evidence = [c["detail"] for c in failed_checks]
        elif passed_checks:
            result = "PASS"
            evidence = [c["detail"] for c in passed_checks]
        else:
            result = "BLOCKED"
            evidence = ["No checks could be executed"]

        return {
            "result": result,
            "evidence": evidence,
            "checks": checks,
        }


    def _run_backend_checks(self, task: Dict[str, Any],
                            worktree_path: Optional[str],
                            allowed: List[str], forbidden: List[str],
                            base_commit: Optional[str] = None
                            ) -> List[Dict[str, Any]]:
        """Run backend-specific QA checks (real commands, real results)."""
        checks: List[Dict[str, Any]] = []
        changed = self._changed_files(worktree_path, base_commit)

        # 1. Python syntax check on changed files
        py_files = [f for f in changed if f.endswith(".py")]
        for py_file in py_files:
            full_path = os.path.join(worktree_path or "", py_file)
            if not os.path.isfile(full_path):
                checks.append({
                    "check": f"python_syntax:{py_file}",
                    "result": "FAIL",
                    "detail": f"{py_file}: missing from worktree",
                })
                continue
            result = subprocess.run(
                ["python", "-m", "py_compile", full_path],
                capture_output=True, text=True,
            )
            checks.append({
                "check": f"python_syntax:{py_file}",
                "result": "PASS" if result.returncode == 0 else "FAIL",
                "detail": (
                    f"{py_file}: syntax OK" if result.returncode == 0
                    else f"{py_file}: {(result.stderr or '').strip()[:150]}"
                ),
            })

        # 2. pytest on changed test files (worktree-local)
        if worktree_path and os.path.isdir(worktree_path):
            test_files = [f for f in changed if "test" in f.lower()
                          and f.endswith(".py")]
            if test_files:
                ok, output = self._run_pytest(worktree_path, test_files[:5] + ["-q"])
                checks.append({
                    "check": "pytest_changed_tests",
                    "result": "PASS" if ok else "FAIL",
                    "detail": (
                        f"Ran pytest on {len(test_files)} changed test file(s)"
                        if ok else f"pytest failures: {output}"
                    ),
                })
            elif py_files and os.path.isdir(os.path.join(worktree_path, "tests")):
                core_args = [a for a in self.core_test_command[3:] if a != "-q"]
                ok, output = self._run_pytest(worktree_path, core_args + ["-q"])
                checks.append({
                    "check": "pytest_core_suite",
                    "result": "PASS" if ok else "FAIL",
                    "detail": (
                        "Core test suite passes" if ok
                        else f"Core suite failures: {output}"
                    ),
                })
            elif py_files:
                checks.append({
                    "check": "pytest_core_suite",
                    "result": "BLOCKED",
                    "detail": "No tests/ directory in worktree; cannot run core suite",
                })
        return checks

    def _run_frontend_checks(self, task: Dict[str, Any],
                             worktree_path: Optional[str],
                             allowed: List[str], forbidden: List[str],
                             base_commit: Optional[str] = None
                             ) -> List[Dict[str, Any]]:
        """Run frontend-specific QA checks (only when a real setup exists)."""
        checks: List[Dict[str, Any]] = []
        changed = self._changed_files(worktree_path, base_commit)
        ts_files = [f for f in changed
                    if f.endswith((".ts", ".tsx", ".js", ".jsx"))]
        fe_root = os.path.join(worktree_path or ".", self.frontend_dir)
        if not os.path.isdir(fe_root):
            fe_root = worktree_path or "."
        has_pkg = os.path.isfile(os.path.join(fe_root, "package.json"))
        has_modules = os.path.isdir(os.path.join(fe_root, "node_modules"))

        if not (ts_files or has_pkg):
            checks.append({
                "check": "frontend_scope",
                "result": "BLOCKED",
                "detail": "No frontend files changed and no package.json present",
            })
            return checks

        if has_pkg and has_modules:
            result = subprocess.run(
                ["npx", "tsc", "-b", "--noEmit"],
                capture_output=True, text=True, cwd=fe_root,
            )
            checks.append({
                "check": "typescript_typecheck",
                "result": "PASS" if result.returncode == 0 else "FAIL",
                "detail": (
                    "TypeScript type check passed"
                    if result.returncode == 0
                    else "TypeScript errors: "
                         + (result.stdout or result.stderr)[:250]
                ),
            })
        else:
            checks.append({
                "check": "typescript_typecheck",
                "result": "BLOCKED",
                "detail": (
                    "TypeScript check not runnable: package.json/node_modules "
                    "missing in worktree (dependencies not installed there)"
                ),
            })
        return checks


    def _run_fullstack_checks(self, task: Dict[str, Any],
                              worktree_path: Optional[str],
                              allowed: List[str], forbidden: List[str],
                              base_commit: Optional[str] = None
                              ) -> List[Dict[str, Any]]:
        """Run fullstack QA checks."""
        checks = list(self._run_backend_checks(
            task, worktree_path, allowed, forbidden, base_commit))
        checks.extend(self._run_frontend_checks(
            task, worktree_path, allowed, forbidden, base_commit))
        return checks

    def _default_checks(self, task: Dict[str, Any],
                        worktree_path: Optional[str],
                        allowed: List[str], forbidden: List[str],
                        base_commit: Optional[str] = None
                        ) -> List[Dict[str, Any]]:
        """Default QA checks when scope is unknown: detect from real changes."""
        changed = self._changed_files(worktree_path, base_commit)
        py_files = [f for f in changed if f.endswith(".py")]
        ts_files = [f for f in changed
                    if f.endswith((".ts", ".tsx", ".js", ".jsx"))]
        if py_files and not ts_files:
            return self._run_backend_checks(
                task, worktree_path, allowed, forbidden, base_commit)
        if ts_files and not py_files:
            return self._run_frontend_checks(
                task, worktree_path, allowed, forbidden, base_commit)
        if py_files and ts_files:
            return self._run_fullstack_checks(
                task, worktree_path, allowed, forbidden, base_commit)
        # Nothing changed (read-only agent) — nothing to verify locally.
        return [{
            "check": "no_changes_detected",
            "result": "BLOCKED",
            "detail": "No changed files detected in worktree; QA has nothing to verify",
        }]

