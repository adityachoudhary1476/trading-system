"""Structured task/result contract for AI Office v0.1.

Defines the JSON schemas for task assignments and agent results,
with validation logic that the orchestrator enforces.

The contract ensures that agents cannot silently "complete" without
objective evidence, and that the orchestrator can validate completion.
"""

from typing import Any, Dict, List, Optional, Literal, Tuple


class TaskContract:
    """Contract definition for task assignments."""

    @staticmethod
    def validate_task_assignment(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Validate a task assignment dict.

        Returns (is_valid, error_message).
        """
        required_fields = [
            "task_id", "objective", "scope", "agent_type",
            "dependencies", "allowed_paths", "forbidden_paths",
            "base_commit", "worktree_path", "max_attempts"
        ]
        for field in required_fields:
            if field not in data or data[field] is None:
                return False, f"Missing required field: {field}"

        # Validate dependencies
        deps = data["dependencies"]
        if not isinstance(deps, list):
            return False, "dependencies must be a list"
        for dep in deps:
            if not isinstance(dep, str):
                return False, f"dependency must be a string, got {type(dep)}"

        # Validate allowed_paths
        ap = data["allowed_paths"]
        if not isinstance(ap, list):
            return False, "allowed_paths must be a list"
        for p in ap:
            if not isinstance(p, str):
                return False, f"allowed_path must be a string, got {type(p)}"

        # Validate forbidden_paths
        fp = data["forbidden_paths"]
        if not isinstance(fp, list):
            return False, "forbidden_paths must be a list"
        for p in fp:
            if not isinstance(p, str):
                return False, f"forbidden_path must be a string, got {type(p)}"

        # Validate max_attempts
        ma = data["max_attempts"]
        if not isinstance(ma, int) or ma < 1:
            return False, "max_attempts must be a positive integer"

        # Validate base_commit is a non-empty string
        bc = data["base_commit"]
        if not isinstance(bc, str) or not bc.strip():
            return False, "base_commit must be a non-empty string"

        # Validate worktree_path is a non-empty string
        wp = data["worktree_path"]
        if not isinstance(wp, str) or not wp.strip():
            return False, "worktree_path must be a non-empty string"

        return True, None

    @staticmethod
    def make_task_assignment(
        task_id: str,
        objective: str,
        scope: str,
        agent_type: str,
        dependencies: List[str],
        allowed_paths: List[str],
        forbidden_paths: List[str],
        base_commit: str,
        worktree_path: str,
        max_attempts: int = 2,
    ) -> Dict[str, Any]:
        """Construct a validated task assignment dict."""
        return {
            "task_id": task_id,
            "objective": objective,
            "scope": scope,
            "agent_type": agent_type,
            "dependencies": dependencies,
            "allowed_paths": allowed_paths,
            "forbidden_paths": forbidden_paths,
            "base_commit": base_commit,
            "worktree_path": worktree_path,
            "max_attempts": max_attempts,
        }


class AgentResult:
    """Contract definition for agent completion results.

    Every agent result MUST conform to this schema for the orchestrator
    to accept it as a valid task completion.
    """

    @staticmethod
    def validate(result: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Validate an agent result dict.

        Returns (is_valid, error_message).
        """
        # Required top-level fields
        required = ["task_id", "status", "changed_files", "commits", "tests_run"]
        for field in required:
            if field not in result or result[field] is None:
                return False, f"Missing required field: {field}"

        # Validate task_id is a non-empty string
        if not isinstance(result["task_id"], str) or not result["task_id"].strip():
            return False, "task_id must be a non-empty string"

        # Validate status is one of the valid values
        valid_statuses = ["complete", "failed", "blocked"]
        if result["status"] not in valid_statuses:
            return False, f"status must be one of {valid_statuses}, got {result['status']}"

        # Validate changed_files is a list of strings
        cf = result["changed_files"]
        if not isinstance(cf, list):
            return False, "changed_files must be a list"
        for f in cf:
            if not isinstance(f, str):
                return False, f"changed_file entry must be a string, got {type(f)}"

        # Validate commits is a list of strings
        comm = result["commits"]
        if not isinstance(comm, list):
            return False, "commits must be a list"
        for c in comm:
            if not isinstance(c, str):
                return False, f"commit entry must be a string, got {type(c)}"

        # Validate tests_run is a list of strings
        tr = result["tests_run"]
        if not isinstance(tr, list):
            return False, "tests_run must be a list"
        for t in tr:
            if not isinstance(t, str):
                return False, f"test entry must be a string, got {type(t)}"

        # Validate test_results if present
        if "test_results" in result:
            tr = result["test_results"]
            if not isinstance(tr, str) or tr not in ("PASS", "FAIL", "BLOCKED"):
                return False, "test_results must be one of: PASS, FAIL, BLOCKED"

        # Validate objective_evidence if present
        if "objective_evidence" in result:
            oe = result["objective_evidence"]
            if not isinstance(oe, dict):
                return False, "objective_evidence must be a dict"
            if "criteria_met" not in oe:
                return False, "objective_evidence must contain criteria_met"
            if not isinstance(oe["criteria_met"], bool):
                return False, "objective_evidence.criteria_met must be boolean"
            if "details" not in oe or not isinstance(oe["details"], str):
                return False, "objective_evidence must contain details (string)"

        # Validate blockers if present
        if "blockers" in result:
            bl = result["blockers"]
            if not isinstance(bl, list):
                return False, "blockers must be a list"
            for b in bl:
                if not isinstance(b, str):
                    return False, f"blocker entry must be a string, got {type(b)}"

        # Validate risks if present
        if "risks" in result:
            rk = result["risks"]
            if not isinstance(rk, list):
                return False, "risks must be a list"
            for r in rk:
                if not isinstance(r, str):
                    return False, f"risk entry must be a string, got {type(r)}"

        return True, None

    @staticmethod
    def make_agent_result(
        task_id: str,
        status: str,
        changed_files: List[str],
        commits: List[str],
        tests_run: List[str],
        test_results: Optional[str] = None,
        blockers: Optional[List[str]] = None,
        risks: Optional[List[str]] = None,
        objective_evidence: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Construct a validated agent result dict."""
        result: Dict[str, Any] = {
            "task_id": task_id,
            "status": status,
            "changed_files": changed_files,
            "commits": commits,
            "tests_run": tests_run,
        }
        if test_results is not None:
            result["test_results"] = test_results
        if blockers is not None:
            result["blockers"] = blockers
        if risks is not None:
            result["risks"] = risks
        if objective_evidence is not None:
            result["objective_evidence"] = objective_evidence
        return result