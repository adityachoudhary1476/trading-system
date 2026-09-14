"""Context passing between agents — prevents agents operating like isolated
chat sessions.

Builds per-task context handoff packages in the Agency handoff format
(OBJECTIVE / CONTEXT / RELEVANT FILES / CONSTRAINTS / EXPECTED OUTPUT /
VERIFICATION REQUIRED) and persists them to SQLite, so every agent receives
the accumulated findings of its predecessors rather than a bare prompt.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .coordination import CoordinationLayer


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ContextHandoff:
    """Builds and persists context handoff packages for tasks."""

    def __init__(self, coord: CoordinationLayer):
        self.coord = coord

    def build_handoff(self, task: Dict[str, Any],
                      dependency_tasks: Optional[List[Dict[str, Any]]] = None,
                      retry_evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Build a context handoff package for `task`.

        Combines: task requirements and acceptance criteria, relevant files
        (scope + previous agents' actual changed files), previous agent
        findings, test results, reviewer feedback, errors, and constraints.
        """
        dependency_tasks = dependency_tasks or []
        objective = self.coord.get_objective(task["objective_id"])

        previous_findings: List[Dict[str, Any]] = []
        relevant_files: List[str] = []
        for dep in dependency_tasks:
            entry: Dict[str, Any] = {
                "task_id": dep["id"],
                "agent_type": dep.get("agent_type"),
                "description": dep.get("description"),
                "status": dep.get("status"),
            }
            result = dep.get("result_json")
            if result:
                entry["result"] = {
                    "status": result.get("status"),
                    "changed_files": result.get("changed_files", []),
                    "commits": result.get("commits", []),
                }
                relevant_files.extend(result.get("changed_files", []))
            qa_evidence = dep.get("qa_evidence_json")
            if qa_evidence:
                entry["qa_evidence"] = qa_evidence
            review = dep.get("review_json")
            if review:
                entry["reviewer_feedback"] = {
                    "decision": review.get("decision"),
                    "fixes": review.get("fixes", []),
                }
            previous_findings.append(entry)

        handoff: Dict[str, Any] = {
            "handoff_version": "v1",
            "built_at": _utcnow(),
            "objective": {
                "id": task["objective_id"],
                "description": objective.get("description") if objective else "",
                "status": objective.get("status") if objective else None,
            },
            # Agency handoff format
            "OBJECTIVE": task.get("description", ""),
            "CONTEXT": {
                "scope": task.get("scope"),
                "base_commit": task.get("base_commit"),
                "attempt": (task.get("attempts") or 0) + 1,
                "max_attempts": task.get("max_attempts", 2),
            },
            "RELEVANT_FILES": sorted(set(relevant_files)),
            "CONSTRAINTS": {
                "allowed_paths": task.get("allowed_paths", []),
                "forbidden_paths": task.get("forbidden_paths", []),
                "paper_only": True,
                "high_risk": task.get("high_risk", False),
            },
            "EXPECTED_OUTPUT": {
                "status_values": ["complete", "failed", "blocked"],
                "result_contract": "ai-office-result JSON block",
            },
            "VERIFICATION_REQUIRED": task.get("acceptance_criteria", []),
            "PREVIOUS_AGENT_FINDINGS": previous_findings,
        }
        if retry_evidence:
            handoff["RETRY_EVIDENCE"] = retry_evidence
        return handoff

    def persist_handoff(self, task_id: str, handoff: Dict[str, Any]) -> None:
        """Persist a handoff to the task record and the artifacts table."""
        self.coord.save_task_context(task_id, handoff)
        self.coord.record_artifact(
            task_id, "context_handoff", json.dumps(handoff, indent=2)
        )

    def build_retry_evidence(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Collect failure evidence from a task's previous attempt for the
        fix agent: QA failures, reviewer fixes, test errors."""
        evidence: Dict[str, Any] = {
            "collected_at": _utcnow(),
            "attempt_number": task.get("attempts") or 0,
        }
        if task.get("qa_result"):
            evidence["qa_result"] = task["qa_result"]
        qa_ev = task.get("qa_evidence_json")
        if qa_ev:
            evidence["qa_failures"] = qa_ev
        review = task.get("review_json")
        if review:
            evidence["reviewer_decision"] = review.get("decision")
            evidence["reviewer_fixes"] = review.get("fixes", [])
            evidence["reviewer_comments"] = review.get("comments", {})
        result = task.get("result_json")
        if result:
            evidence["previous_status"] = result.get("status")
            blockers = result.get("blockers") or []
            if blockers:
                evidence["previous_blockers"] = blockers
        tests = task.get("test_results_json")
        if tests:
            evidence["previous_test_results"] = tests
        return evidence

