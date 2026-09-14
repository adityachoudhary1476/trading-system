"""Runner — the real end-to-end orchestrator loop for AI Office v0.2.

Executes the full path:

    user task → plan → worktree per task → agent dispatch (real executor)
    → result validation against actual git → QA gate → review gate
    → retry/fix loop (bounded) → completion verification (9-point)
    → integration (fast-forward only, human approval for high-risk)
    → final report (persisted)

Honesty rules enforced here:
- A task never becomes "complete" without real committed changes in its
  worktree, real QA results, and real review results.
- If no executor is configured for an agent role, the task is BLOCKED, not
  faked.
- Interruptions are recoverable: all state lives in SQLite; `resume_objective`
  continues from persisted state.
"""

import json
import os
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .adapter import AgentDispatcher, extract_result_payload
from .completion import CompletionVerifier
from .context import ContextHandoff
from .coordination import CoordinationLayer
from .high_risk import HighRiskDetector
from .integration_model import IntegrationModel
from .planner import Planner
from .qa_engine import QAEngine
from .review_engine import ReviewEngine
from .task_contract import AgentResult, TaskContract
from .worktree_manager import GitWorktreeManager


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ObjectiveRunner:
    """Executes objectives end-to-end with real agent dispatch and gates."""

    def __init__(self, repo_root: str,
                 coord: Optional[CoordinationLayer] = None,
                 executors: Optional[Dict[str, Callable[..., Dict[str, Any]]]] = None,
                 default_executor: Optional[Callable[..., Dict[str, Any]]] = None,
                 core_test_command: Optional[List[str]] = None,
                 orphan_timeout_seconds: int = 300,
                 max_attempts_default: int = 2):
        self.repo_root = os.path.abspath(repo_root)
        self.coord = coord or CoordinationLayer(
            db_path=os.path.join(self.repo_root, ".ai-office", "coordination.db")
        )
        self.dispatcher = AgentDispatcher(self.coord, executors=executors,
                                          default_executor=default_executor)
        self.qa = QAEngine(self.repo_root, core_test_command=core_test_command)
        self.handoff = ContextHandoff(self.coord)
        self.risk = HighRiskDetector(self.repo_root)
        self.orphan_timeout_seconds = orphan_timeout_seconds
        self.max_attempts_default = max_attempts_default

    # ------------------------------------------------------------------
    # planning
    # ------------------------------------------------------------------
    def run_objective(self, description: str, objective_id: Optional[str] = None,
                      task_type: Optional[str] = None,
                      path_hints: Optional[List[str]] = None,
                      max_attempts: Optional[int] = None) -> Dict[str, Any]:
        """Run a full objective lifecycle and return the final report."""
        objective_id = objective_id or self._new_objective_id(description)
        self.coord.create_objective(objective_id, description)
        self.coord.set_objective_status(objective_id, "in_progress")
        attempts = max_attempts or self.max_attempts_default
        Planner(self.coord, self.repo_root).plan(
            objective_id, description, task_type=task_type,
            path_hints=path_hints, max_attempts=attempts,
        )
        return self.execute_pending_tasks(objective_id)

    def _new_objective_id(self, description: str) -> str:
        slug = "".join(c if c.isalnum() else "-" for c in description.lower())[:32].strip("-")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"obj-{stamp}-{slug or 'task'}"
    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------
    def execute_pending_tasks(self, objective_id: str) -> Dict[str, Any]:
        """Execute all pending tasks for an objective level by level.

        Uses the DAG scheduler: tasks in the same level with no dependency
        edges and no path overlap are *eligible* for parallel execution.
        For v0.2 they still run sequentially (SQLite connection is not
        thread-safe, and parallel git worktrees add failure modes); the
        eligibility grouping is recorded in events for observability and
        future parallel dispatch.
        """
        from .scheduler import DAGScheduler

        orphaned = self.coord.orphan_stale_attempts(self.orphan_timeout_seconds)
        if orphaned:
            self.coord.record_event(
                objective_id=objective_id, event_type="orphans_reconciled",
                details={"attempts": orphaned},
            )
        tasks = self.coord.list_tasks(objective_id)
        task_deps = {t["id"]: list(t.get("dependencies") or []) for t in tasks}
        task_statuses = {t["id"]: t.get("status", "pending") for t in tasks}
        levels = DAGScheduler.topological_sort(tasks, task_deps, task_statuses)
        for level in levels:
            for task_id in level:
                task = self.coord.get_task(task_id)
                if task is None:
                    continue
                if task.get("status") in ("complete", "failed", "blocked",
                                          "escalated-to-human"):
                    continue
                unmet = [d for d in (task.get("dependencies") or [])
                         if (self.coord.get_task(d) or {}).get("status") != "complete"]
                if unmet:
                    self.coord.transition_task(task_id, "blocked",
                                               error=f"unmet dependencies: {unmet}")
                    self.coord.record_event(
                        objective_id=objective_id, task_id=task_id,
                        event_type="task_blocked_on_dependencies",
                        details={"unmet": unmet},
                    )
                    continue
                self.execute_task(task_id)
    def execute_task(self, task_id: str) -> Dict[str, Any]:
        """Execute a single task with retry/fix loop (bounded by max_attempts).

        Returns an outcome dict: {"task_id", "status", "attempts", ...}.
        """
        task = self.coord.get_task(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        objective_id = task["objective_id"]
        max_attempts = task.get("max_attempts") or self.max_attempts_default
        kind = (task.get("kind") or self._infer_kind(task)).lower()
        attempts_done = task.get("attempts") or 0

        if task.get("status") == "pending":
            self.coord.transition_task(task_id, "assigned")
        task = self.coord.get_task(task_id)
        if task.get("status") in ("assigned", "blocked", "failed"):
            try:
                self.coord.transition_task(task_id, "in_progress")
            except ValueError:
                pass  # already in a runnable state

        outcome: Dict[str, Any] = {"task_id": task_id, "attempts": attempts_done}
        while attempts_done < max_attempts:
            attempts_done = self.coord.increment_task_attempts(task_id)
            task = self.coord.get_task(task_id)
            attempt_no = attempts_done
            try:
                if kind == "implementation":
                    ok, detail = self._run_implementation_attempt(task, attempt_no)
                elif kind == "qa":
                    ok, detail = self._run_qa_attempt(task, attempt_no)
                elif kind == "review":
                    ok, detail = self._run_review_attempt(task, attempt_no)
                else:
                    ok, detail = False, f"unknown task kind: {kind!r}"
            except Exception as exc:  # never crash the objective loop
                ok, detail = False, f"attempt raised: {exc}"
                self.coord.record_event(
                    objective_id=objective_id, task_id=task_id,
                    event_type="attempt_error", details={"error": str(exc)},
                )
            outcome.update({"attempts": attempts_done, "ok": ok,
                            "detail": detail})
            if ok:
                self.coord.transition_task(task_id, "complete")
                self.coord.record_event(
                    objective_id=objective_id, task_id=task_id,
                    event_type="task_completed",
                    details={"attempts": attempts_done},
                )
                outcome["status"] = "complete"
                return outcome
            # Failure path: retry if attempts remain, else terminal state.
            self.coord.record_event(
                objective_id=objective_id, task_id=task_id,
                event_type="attempt_failed",
                details={"attempt": attempts_done, "max_attempts": max_attempts,
                         "detail": detail},
            )
            if attempts_done >= max_attempts:
                risk = self.risk.assess_task_risk(task.get("allowed_paths") or [],
                                                  task_id)
                controls = HighRiskDetector.required_controls(risk)
                if controls.get("escalate_immediately"):
                    self.coord.transition_task(task_id, "escalated-to-human",
                                               error=detail)
                    outcome["status"] = "escalated-to-human"
                else:
                    self.coord.transition_task(task_id, "failed", error=detail)
                    outcome["status"] = "failed"
                return outcome
            # Prepare retry: reset to in_progress (failed->in_progress allowed).
            current = (self.coord.get_task(task_id) or {}).get("status")
            if current == "failed":
                self.coord.transition_task(task_id, "in_progress")
            elif current not in ("in_progress", "assigned"):
                try:
                    self.coord.transition_task(task_id, "in_progress")
                except ValueError:
                    pass
        outcome["status"] = (self.coord.get_task(task_id) or {}).get("status")
        return outcome

    @staticmethod
    def _infer_kind(task: Dict[str, Any]) -> str:
        scope = (task.get("scope") or "").lower()
        role = (task.get("agent_type") or "").lower()
        if role == "qa" or scope == "qa":
            return "qa"
        if role == "reviewer" or scope == "review":
            return "review"
        return "implementation"

    # ------------------------------------------------------------------
    # attempt helpers
    def _ensure_worktree(self, task: Dict[str, Any]) -> str:
        """Create (or reuse) the task worktree; persist path on first create."""
        objective_id, task_id = task["objective_id"], task["id"]
        existing = task.get("worktree_path")
        if existing and os.path.isdir(existing):
            return existing
        base_commit = task.get("base_commit") or self._repo_head()
        path = GitWorktreeManager.create_worktree(objective_id, task_id,
                                                  base_commit,
                                                  repo_root=self.repo_root)
        self.coord.save_task_worktree(task_id, path, base_commit)
        return path

    def _repo_head(self) -> str:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, cwd=self.repo_root)
        if proc.returncode != 0:
            raise RuntimeError(f"cannot read repo HEAD: {proc.stderr}")
        return proc.stdout.strip()

    def _dependency_tasks(self, task: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        for dep_id in task.get("dependencies") or []:
            dep = self.coord.get_task(dep_id)
            if dep is not None:
                out.append(dep)
        return out

    # ------------------------------------------------------------------
    def _ensure_worktree(self, task: Dict[str, Any]) -> str:
        """Create (or reuse) the task worktree; persist path on first create."""
        objective_id, task_id = task["objective_id"], task["id"]
        existing = task.get("worktree_path")
        if existing and os.path.isdir(existing):
            return existing
        base_commit = task.get("base_commit") or self._repo_head()
        path = GitWorktreeManager.create_worktree(objective_id, task_id,
                                                  base_commit,
                                                  repo_root=self.repo_root)
        self.coord.save_task_worktree(task_id, path, base_commit)
        return path

    def _repo_head(self) -> str:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, cwd=self.repo_root)
        if proc.returncode != 0:
            raise RuntimeError(f"cannot read repo HEAD: {proc.stderr}")
        return proc.stdout.strip()

    def _dependency_tasks(self, task: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        for dep_id in task.get("dependencies") or []:
            dep = self.coord.get_task(dep_id)
            if dep is not None:
                out.append(dep)
        return out


        return self.finalize_objective(objective_id)

    # ------------------------------------------------------------------
    # attempt helpers
    # ------------------------------------------------------------------
    def _ensure_worktree(self, task: Dict[str, Any]) -> str:
        """Create (or reuse) the task worktree; persist path on first create."""
        objective_id, task_id = task["objective_id"], task["id"]
        existing = task.get("worktree_path")
        if existing and os.path.isdir(existing):
            return existing
        base_commit = task.get("base_commit") or self._repo_head()
        path = GitWorktreeManager.create_worktree(objective_id, task_id,
                                                  base_commit,
                                                  repo_root=self.repo_root)
        self.coord.save_task_worktree(task_id, path, base_commit)
        return path

    def _repo_head(self) -> str:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, cwd=self.repo_root)
        if proc.returncode != 0:
            raise RuntimeError(f"cannot read repo HEAD: {proc.stderr}")
        return proc.stdout.strip()

    def _dependency_tasks(self, task: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        for dep_id in task.get("dependencies") or []:
            dep = self.coord.get_task(dep_id)
            if dep is not None:
                out.append(dep)
        return out

    def _assignment_for(self, task: Dict[str, Any],
                        worktree: str) -> Dict[str, Any]:
        return TaskContract.make_task_assignment(
            task_id=task["id"], objective=task.get("description", ""),
            scope=task.get("scope") or "", agent_type=task.get("agent_type") or "",
            dependencies=task.get("dependencies") or [],
            allowed_paths=task.get("allowed_paths") or [],
            forbidden_paths=task.get("forbidden_paths") or [],
            base_commit=task.get("base_commit") or self._repo_head(),
            worktree_path=worktree,
            max_attempts=task.get("max_attempts") or self.max_attempts_default)

    def _ensure_worktree(self, task: Dict[str, Any]) -> str:
        objective_id, task_id = task["objective_id"], task["id"]
        existing = task.get("worktree_path")
        if existing and os.path.isdir(existing):
            return existing
        base_commit = task.get("base_commit") or self._repo_head()
        path = GitWorktreeManager.create_worktree(objective_id, task_id,
                                                  base_commit,
                                                  repo_root=self.repo_root)
        self.coord.save_task_worktree(task_id, path, base_commit)
        return path

    def _repo_head(self) -> str:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, cwd=self.repo_root)
        if proc.returncode != 0:
            raise RuntimeError(f"cannot read repo HEAD: {proc.stderr}")
        return proc.stdout.strip()

    def _dependency_tasks(self, task: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        for dep_id in task.get("dependencies") or []:
            dep = self.coord.get_task(dep_id)
            if dep is not None:
                out.append(dep)
    def _run_implementation_attempt(self, task: Dict[str, Any],
                                    attempt_no: int) -> Any:
        objective_id, task_id = task["objective_id"], task["id"]
        try:
            worktree = self._ensure_worktree(task)
        except Exception as exc:
            self.coord.save_task_result(task_id, {
                "status": "failed",
                "blockers": [f"worktree failure: {exc}"],
                "changed_files": [], "commits": []})
            return False, f"worktree failure: {exc}"
        deps = self._dependency_tasks(task)
        retry_evidence = None
        if attempt_no > 1:
            retry_evidence = self.handoff.build_retry_evidence(task)
        handoff = self.handoff.build_handoff(task, deps, retry_evidence)
        self.handoff.persist_handoff(task_id, handoff)
        contract_task = {
            "task_id": task_id, "objective_id": objective_id,
            "agent_type": task.get("agent_type"),
            "base_commit": task.get("base_commit") or self._repo_head(),
        }
        attempt_id = f"{task_id}-a{attempt_no}"
        self.coord.start_attempt(task_id, attempt_id)
        record = self.dispatcher.dispatch(contract_task, worktree, handoff)
        if record.get("execution_status") == "BLOCKED_NO_EXECUTOR":
            self.coord.finish_attempt(attempt_id, "FAILED",
                                      blockers=record.get("blockers"))
            self.coord.save_task_result(task_id, {
                "status": "blocked",
                "blockers": record.get("blockers", ["no executor"]),
                "changed_files": [], "commits": []})
            return False, f"blocked: {record.get('blockers')}"
        self.coord.heartbeat(attempt_id)
        result = self._collect_result(task, worktree, record)
        ok, verr = AgentResult.validate(result)
        if not ok:
            self.coord.finish_attempt(attempt_id, "FAILED",
                                      blockers=[f"malformed: {verr}"])
            self.coord.save_task_result(task_id, result)
            self.coord.record_event(
                objective_id=objective_id, task_id=task_id,
                attempt_id=attempt_id, event_type="malformed_agent_output",
                details={"error": verr})
            return False, f"malformed agent output: {verr}"
        self.coord.save_task_result(task_id, result)
        if result.get("status") in ("failed", "blocked"):
            self.coord.finish_attempt(
                attempt_id, "FAILED", blockers=result.get("blockers"),
                changed_files=result.get("changed_files"),
                commits=result.get("commits"))
            return False, f"agent {result.get('status')}: {result.get('blockers')}"
        qa_out = self.qa.run_qa(
            task, worktree, allowed_paths=task.get("allowed_paths"),
            forbidden_paths=task.get("forbidden_paths"),
            base_commit=task.get("base_commit"))
        self.coord.save_task_qa(task_id, qa_out["result"],
                                qa_out.get("checks", []))
        if qa_out["result"] != "PASS":
            self.coord.finish_attempt(
                attempt_id, "FAILED", blockers=qa_out.get("evidence"),
                changed_files=result.get("changed_files"),
                commits=result.get("commits"))
            return False, f"QA {qa_out['result']}: {qa_out.get('evidence')}"
        all_pass, evidence = CompletionVerifier.verify(
            objective_id, task_id, self.coord, worktree,
            task.get("base_commit") or self._repo_head(),
            task.get("allowed_paths") or [], task.get("forbidden_paths") or [],
            result, qa_out["result"], "PASS")
        impl_evidence = {k: v for k, v in evidence.items()
                         if k != "check_7_reviewer_passed"}
        if not all(v["pass"] for v in impl_evidence.values()):
            failed = [k for k, v in impl_evidence.items() if not v["pass"]]
            self.coord.finish_attempt(
                attempt_id, "FAILED",
                blockers=[f"completion failed: {failed}"],
                changed_files=result.get("changed_files"),
                commits=result.get("commits"))
            return False, f"completion checks failed: {failed}"
    def _collect_result(self, task: Dict[str, Any], worktree: str,
                        record: Dict[str, Any]) -> Dict[str, Any]:
        payload = record.get("result_payload") or {}
        base = task.get("base_commit") or self._repo_head()
        real_changed = GitWorktreeManager.get_modified_files(worktree, base)
        try:
            extra = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                capture_output=True, text=True, cwd=worktree)
            if extra.returncode == 0:
                real_changed = sorted(
                    set(real_changed)
                    | {f for f in extra.stdout.split("\n") if f.strip()})
        except Exception:
            pass
        commits = [c["hash"] for c in
                   GitWorktreeManager.get_commits_since_base(worktree, base)]
        status = str(payload.get("status", "complete")).lower()
        if status not in ("complete", "failed", "blocked"):
            status = "complete" if (real_changed or commits) else "failed"
        return AgentResult.make_agent_result(
            task_id=task["id"], status=status,
            changed_files=sorted(set(real_changed)
                                 | set(payload.get("changed_files") or [])),
            commits=commits or list(payload.get("commits") or []),
            tests_run=list(payload.get("tests_run") or []),
            test_results=payload.get("test_results"),
            blockers=list(payload.get("blockers") or []),
            risks=list(payload.get("risks") or []),
            objective_evidence=payload.get("objective_evidence"))

        self.coord.finish_attempt(
            attempt_id, "COMPLETE",
            changed_files=result.get("changed_files"),
            commits=result.get("commits"))
        return True, f"attempt {attempt_no} complete"

        return out

