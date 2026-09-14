"""Planner — task decomposition for AI Office v0.2.

Deterministic, rule-based decomposition of a user task into a dependency DAG
of tasks with agent roles, path scopes, and acceptance criteria.

The orchestrator boundary assigns *objective interpretation* to the live
orchestrator runtime (OpenCode); the planner provides the deterministic
spine: given the interpreted task type and path hints, it produces the
smallest appropriate team of agents (Agency routing rule: never force a task
through every agent). Every decomposition ends with independent QA and
review gates (the reviewer is always a different agent_type than the
implementer — review independence).
"""

import os
import subprocess
from typing import Any, Dict, List, Optional

from .agent_registry import validate_role
from .coordination import CoordinationLayer
from .high_risk import HighRiskDetector
from .path_enforcement import PathEnforcer


def detect_task_type(task_description: str,
                     path_hints: Optional[List[str]] = None) -> str:
    """Classify a task description into a decomposition pattern.

    Deterministic keyword analysis, optionally biased by path hints.
    """
    text = task_description.lower()
    hints = " ".join(path_hints or []).lower()
    backend_kw = ("backend", "python", "api", "pytest", "src/", "trading_system",
                  "database", "sqlite", "server", "cli", "endpoint")
    frontend_kw = ("frontend", "react", "typescript", "tsx", "component",
                   "frontend/src", "vite", "ui", "page", "dashboard")
    docs_kw = ("docs", "documentation", "readme", "research", "design doc",
               "audit", "report", "investigate")
    is_backend = any(k in text or k in hints for k in backend_kw)
    is_frontend = any(k in text or k in hints for k in frontend_kw)
    is_docs = any(k in text or k in hints for k in docs_kw)
    if is_docs and not is_backend and not is_frontend:
        return "docs"
    if is_backend and is_frontend:
        return "fullstack"
    if is_frontend:
        return "frontend"
    return "backend"


def derive_scope_paths(task_type: str,
                       path_hints: Optional[List[str]] = None) -> List[str]:
    """Derive allowed path scopes for a task type."""
    default_scopes = {
        "backend": ["src/trading_system/", "backend/", "tests/"],
        "frontend": ["frontend/src/", "frontend/types/", "frontend/"],
        "fullstack": ["src/trading_system/", "backend/", "tests/",
                      "frontend/src/", "frontend/types/", "frontend/"],
        "docs": ["docs/", "*.md"],
    }
    hints = [h for h in (path_hints or []) if h]
    return hints if hints else default_scopes.get(task_type,
                                                  default_scopes["backend"])


def derive_forbidden_paths(task_type: str) -> List[str]:
    """Forbidden paths: never allow agents to touch critical live paths."""
    return [
        "src/trading_system/execution/",
        ".env",
        ".env.local",
        "data/market_data.db",
    ]


class Planner:
    """Deterministic task decomposition with SQLite persistence."""

    def __init__(self, coord: CoordinationLayer, repo_root: str,
                 base_commit: Optional[str] = None):
        self.coord = coord
        self.repo_root = os.path.abspath(repo_root)
        self.enforcer = PathEnforcer(repo_root)
        self.risk = HighRiskDetector(repo_root)
        self.base_commit = base_commit or self._current_head()

    def _current_head(self) -> Optional[str]:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=self.repo_root,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def plan(self, objective_id: str, task_description: str,
             task_type: Optional[str] = None,
             path_hints: Optional[List[str]] = None,
             max_attempts: int = 2) -> List[Dict[str, Any]]:
        """Decompose `task_description` into tasks and persist them.

        Returns the ordered list of task dicts (DAG order: implementation
        first, then QA, then review; dependencies impl → qa → review).
        """
        if task_type is None:
            task_type = detect_task_type(task_description, path_hints)
        scope = derive_scope_paths(task_type, path_hints)
        forbidden = derive_forbidden_paths(task_type)
        risk_level = self.risk.assess_task_risk(scope, objective_id)

        specs = self._decompose(task_type, task_description, max_attempts)
        created: List[Dict[str, Any]] = []
        impl_ids: List[str] = []
        qa_ids: List[str] = []
        for spec in specs:
            task_id = f"{objective_id}-{spec['id']}"
            deps: List[str] = []
            if spec["kind"] == "implementation":
                deps = []
            elif spec["kind"] == "qa":
                deps = list(impl_ids)
            else:
                deps = list(impl_ids) + list(qa_ids)
            task = self.coord.create_task({
                "id": task_id,
                "objective_id": objective_id,
                "description": spec["description"],
                "scope": spec["scope"],
                "agent_type": spec["agent_type"],
                "dependencies": deps,
                "allowed_paths": spec.get("allowed_paths", scope),
                "forbidden_paths": spec.get("forbidden_paths", forbidden),
                "base_commit": self.base_commit,
                "max_attempts": spec.get("max_attempts", max_attempts),
                "high_risk": risk_level in ("CRITICAL", "HIGH"),
            })
            if spec["kind"] == "implementation":
                impl_ids.append(task_id)
            elif spec["kind"] == "qa":
                qa_ids.append(task_id)
            created.append(task)
        self.coord.record_event(
            objective_id=objective_id, event_type="objective_planned",
            details={"task_type": task_type, "risk_level": risk_level,
                     "tasks": [t["id"] for t in created]},
        )
        return created


    def _decompose(self, task_type: str, task_description: str,
                   max_attempts: int) -> List[Dict[str, Any]]:
        """Smallest appropriate team per task type (Agency routing rule)."""
        specs: List[Dict[str, Any]] = []
        if task_type == "docs":
            specs.append({
                "id": "research",
                "kind": "implementation",
                "agent_type": "researcher",
                "scope": "docs",
                "description": f"Research/investigate: {task_description}",
                "max_attempts": max_attempts,
                "allowed_paths": ["docs/", "*.md"],
            })
        elif task_type == "fullstack":
            for tid, role in (("impl-backend", "backend"),
                              ("impl-frontend", "frontend")):
                specs.append({
                    "id": tid,
                    "kind": "implementation",
                    "agent_type": role,
                    "scope": role,
                    "description": (
                        f"Implement ({'Python/backend' if role == 'backend' else 'React/TS'}): "
                        f"{task_description}"
                    ),
                    "max_attempts": max_attempts,
                    "allowed_paths": derive_scope_paths(role),
                })
        else:
            role = task_type  # backend | frontend
            specs.append({
                "id": "impl",
                "kind": "implementation",
                "agent_type": role,
                "scope": role,
                "description": f"Implement: {task_description}",
                "max_attempts": max_attempts,
                "allowed_paths": derive_scope_paths(role),
            })
        specs.append({
            "id": "qa",
            "kind": "qa",
            "agent_type": "qa",
            "scope": "qa",
            "description": f"QA verification: {task_description}",
            "max_attempts": max_attempts,
            "allowed_paths": [],
        })
        specs.append({
            "id": "review",
            "kind": "review",
            "agent_type": "reviewer",
            "scope": "review",
            "description": f"Independent 9-question review: {task_description}",
            "max_attempts": max_attempts,
            "allowed_paths": [],
        })
        for spec in specs:
            if spec["agent_type"] and not validate_role(spec["agent_type"]):
                raise ValueError(f"Unknown agent role: {spec['agent_type']}")
        return specs

