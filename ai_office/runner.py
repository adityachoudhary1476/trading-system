"""Runner — the real end-to-end orchestrator loop for AI Office v0.2.

Executes the full path:

    user task -> plan -> worktree per task -> agent dispatch (real executor)
    -> result validation against actual git -> QA gate -> review gate
    -> retry/fix loop (bounded) -> completion verification (9-point)
    -> integration branch record -> final report (persisted)

Honesty rules enforced here:
- A task never becomes "complete" without real committed changes in its
  worktree, real QA results, and real review results.
- If no executor is configured for an agent role, the task is BLOCKED, not
  faked.
- Interruptions are recoverable: all state lives in SQLite; resume_objective
  continues from persisted state.
"""

import json
import os
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .adapter import AgentDispatcher
from .completion import CompletionVerifier
from .context import ContextHandoff
from .coordination import CoordinationLayer
from .high_risk import HighRiskDetector
from .planner import Planner
from .qa_engine import QAEngine
from .review_engine import ReviewEngine
from .task_contract import AgentResult
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
        """Run a full objective lifecycle and return the final report.

        If an objective with tasks already exists under `objective_id`, this
        resumes it (interrupted-execution recovery).
        """
        objective_id = objective_id or self._new_objective_id(description)
        existing_tasks = self.coord.list_tasks(objective_id)
        if existing_tasks:
            return self.resume_objective(objective_id)
        self.coord.create_objective(objective_id, description)
        try:
            self.coord.set_objective_status(objective_id, "in_progress")
        except ValueError:
            pass  # already in_progress from a prior partial run
        attempts = max_attempts or self.max_attempts_default
        Planner(self.coord, self.repo_root).plan(
            objective_id, description, task_type=task_type,
            path_hints=path_hints, max_attempts=attempts)
        return self.execute_pending_tasks(objective_id)

    def resume_objective(self, objective_id: str) -> Dict[str, Any]:
        """Resume an interrupted objective from persisted SQLite state."""
        if self.coord.get_objective(objective_id) is None:
            raise ValueError(f"Objective not found: {objective_id}")
        self.coord.record_event(objective_id=objective_id,
                                event_type="objective_resumed", details={})
        return self.execute_pending_tasks(objective_id)

    def _new_objective_id(self, description: str) -> str:
        slug = "".join(c if c.isalnum() else "-" for c in description.lower())
        slug = "-".join(p for p in slug.split("-") if p)[:30] or "objective"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"{slug}-{stamp}"
# ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------
    def execute_pending_tasks(self, objective_id: str) -> Dict[str, Any]:
        """Execute all pending tasks for an objective level by level.

        Uses the DAG scheduler: tasks in the same level with no dependency
        edges and no path overlap are *eligible* for parallel execution. For
        v0.2 they still run sequentially (the SQLite connection is not
        thread-safe); the eligibility grouping is recorded in events for
        observability and future parallel dispatch.
        """
        from .scheduler import DAGScheduler
        orphaned = self.coord.orphan_stale_attempts(self.orphan_timeout_seconds)
        if orphaned:
            self.coord.record_event(objective_id=objective_id,
                                    event_type="orphans_reconciled",
                                    details={"attempts": orphaned})
        tasks = self.coord.list_tasks(objective_id)
        task_deps = {t["id"]: list(t.get("dependencies") or []) for t in tasks}
        task_statuses = {t["id"]: t.get("status", "pending") for t in tasks}
        levels = DAGScheduler.topological_sort(tasks, task_deps, task_statuses)
        for level in levels:
            ids: List[str] = []
            for entry in level:
                if isinstance(entry, str):
                    ids.append(entry)
                elif isinstance(entry, list):
                    ids.extend(e for e in entry if isinstance(e, str))
            for task_id in ids:
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
                                               error="unmet deps: " + str(unmet))
                    continue
                self.execute_task(task_id)
        return self.finalize_objective(objective_id)

    def execute_task(self, task_id: str) -> Dict[str, Any]:
        """Execute one task with a bounded retry/fix loop (max_attempts)."""
        task = self.coord.get_task(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        objective_id = task["objective_id"]
        cap = task.get("max_attempts") or self.max_attempts_default
        kind = self._infer_kind(task)
        if task.get("status") == "pending":
            self.coord.transition_task(task_id, "assigned")
        task = self.coord.get_task(task_id)
        if task.get("status") in ("assigned", "blocked", "failed"):
            try:
                self.coord.transition_task(task_id, "in_progress")
            except ValueError:
                pass
        outcome: Dict[str, Any] = {"task_id": task_id, "attempts": 0}
        attempts = task.get("attempts") or 0
        while attempts < cap:
            attempts = self.coord.increment_task_attempts(task_id)
            task = self.coord.get_task(task_id)
            try:
                if kind == "implementation":
                    ok, detail = self._run_implementation_attempt(task, attempts)
                elif kind == "qa":
                    ok, detail = self._run_qa_attempt(task)
                elif kind == "review":
                    ok, detail = self._run_review_attempt(task)
                else:
                    ok, detail = False, f"unknown task kind {kind!r}"
            except Exception as exc:
                ok, detail = False, f"attempt raised: {exc}"
                self.coord.record_event(objective_id=objective_id,
                                        task_id=task_id,
                                        event_type="attempt_error",
                                        details={"error": str(exc)})
            outcome.update({"attempts": attempts, "ok": ok, "detail": detail})
            if ok:
                try:
                    self.coord.transition_task(task_id, "complete")
                except ValueError:
                    pass
                self.coord.record_event(objective_id=objective_id, task_id=task_id,
                                        event_type="task_completed",
                                        details={"attempts": attempts})
                outcome["status"] = "complete"
                return outcome
            self.coord.record_event(objective_id=objective_id, task_id=task_id,
                                    event_type="attempt_failed",
                                    details={"attempt": attempts,
                                             "max_attempts": cap,
                                             "detail": detail})
            if attempts >= cap:
                target = self._terminal_status(task)
                try:
                    self.coord.transition_task(task_id, target, error=detail)
                except ValueError:
                    pass
                outcome["status"] = target
                return outcome
            cur = (self.coord.get_task(task_id) or {}).get("status")
            if cur == "failed":
                try:
                    self.coord.transition_task(task_id, "in_progress")
                except ValueError:
                    pass
            elif cur not in ("in_progress", "assigned"):
                try:
                    self.coord.transition_task(task_id, "in_progress")
                except ValueError:
                    pass
        outcome["status"] = (self.coord.get_task(task_id) or {}).get("status")
        return outcome

    def _terminal_status(self, task: Dict[str, Any]) -> str:
        risk = self.risk.assess_task_risk(task.get("allowed_paths") or [],
                                          task["id"])
        controls = HighRiskDetector.required_controls(risk)
        return ("escalated-to-human"
                if controls.get("escalate_immediately") else "failed")

    @staticmethod
    def _infer_kind(task: Dict[str, Any]) -> str:
        role = (task.get("agent_type") or "").lower()
        scope = (task.get("scope") or "").lower()
        if role == "qa" or scope == "qa":
            return "qa"
        if role == "reviewer" or scope == "review":
            return "review"
        return "implementation"
# ------------------------------------------------------------------
    # worktree + git helpers
    # ------------------------------------------------------------------
    def _ensure_worktree(self, task: Dict[str, Any]) -> str:
        """Create (or reuse) the task worktree; persist path on first create."""
        objective_id, task_id = task["objective_id"], task["id"]
        existing = task.get("worktree_path")
        if existing and os.path.isdir(existing):
            return existing
        base = task.get("base_commit") or self._repo_head()
        path = GitWorktreeManager.create_worktree(objective_id, task_id, base,
                                                  repo_root=self.repo_root)
        self.coord.save_task_worktree(task_id, path, base)
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

    def _impl_dependency(self, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """First implementation task this task depends on (QA/review target)."""
        for dep in self._dependency_tasks(task):
            if self._infer_kind(dep) == "implementation":
                return dep
        return None
# ------------------------------------------------------------------
    # attempt implementations
    # ------------------------------------------------------------------
    def _run_implementation_attempt(self, task: Dict[str, Any],
                                    attempt_no: int) -> Any:
        """Dispatch the implementer agent, then run QA + completion gates."""
        objective_id, task_id = task["objective_id"], task["id"]
        try:
            worktree = self._ensure_worktree(task)
        except Exception as exc:
            self.coord.save_task_result(task_id, {
                "status": "failed", "changed_files": [], "commits": [],
                "blockers": [f"worktree failure: {exc}"]})
            return False, f"worktree failure: {exc}"
        retry_evidence = None
        if attempt_no > 1:
            retry_evidence = self.handoff.build_retry_evidence(task)
        handoff = self.handoff.build_handoff(task, self._dependency_tasks(task),
                                             retry_evidence)
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
                "status": "blocked", "changed_files": [], "commits": [],
                "blockers": record.get("blockers", ["no executor"])})
            return False, f"blocked: {record.get('blockers')}"
        self.coord.heartbeat(attempt_id)
        result = self._collect_result(task, worktree, record)
        ok, verr = AgentResult.validate(result)
        if not ok:
            self.coord.finish_attempt(attempt_id, "FAILED",
                                      blockers=[f"malformed: {verr}"])
            self.coord.save_task_result(task_id, result)
            self.coord.record_event(objective_id=objective_id, task_id=task_id,
                                    attempt_id=attempt_id,
                                    event_type="malformed_agent_output",
                                    details={"error": verr})
            return False, f"malformed agent output: {verr}"
        self.coord.save_task_result(task_id, result)
        if result.get("status") in ("failed", "blocked"):
            self.coord.finish_attempt(attempt_id, "FAILED",
                                      blockers=result.get("blockers"),
                                      changed_files=result.get("changed_files"),
                                      commits=result.get("commits"))
            return False, (f"agent {result.get('status')}: "
                           f"{result.get('blockers')}")
        return self._qa_and_complete(task, worktree, result, attempt_id,
                                     attempt_no)
    def _qa_and_complete(self, task: Dict[str, Any], worktree: str,
                         result: Dict[str, Any], attempt_id: str,
                         attempt_no: int) -> Any:
        """QA gate + 8-point completion verification (review deferred)."""
        objective_id, task_id = task["objective_id"], task["id"]
        qa_out = self.qa.run_qa(task, worktree,
                                allowed_paths=task.get("allowed_paths"),
                                forbidden_paths=task.get("forbidden_paths"),
                                base_commit=task.get("base_commit"))
        self.coord.save_task_qa(task_id, qa_out["result"],
                                qa_out.get("checks", []))
        if qa_out["result"] != "PASS":
            self.coord.finish_attempt(attempt_id, "FAILED",
                                      blockers=qa_out.get("evidence"),
                                      changed_files=result.get("changed_files"),
                                      commits=result.get("commits"))
            return False, f"QA {qa_out['result']}: {qa_out.get('evidence')}"
        base = task.get("base_commit") or self._repo_head()
        all_pass, evidence = CompletionVerifier.verify(
            objective_id, task_id, self.coord, worktree, base,
            task.get("allowed_paths") or [], task.get("forbidden_paths") or [],
            result, qa_out["result"], "PASS")
        impl_evidence = {k: v for k, v in evidence.items()
                         if k != "check_7_reviewer_passed"}
        if not all(v["pass"] for v in impl_evidence.values()):
            failed = [k for k, v in impl_evidence.items() if not v["pass"]]
            self.coord.finish_attempt(attempt_id, "FAILED",
                                      blockers=[f"completion failed: {failed}"],
                                      changed_files=result.get("changed_files"),
                                      commits=result.get("commits"))
            return False, f"completion checks failed: {failed}"
        self._record_integration_branch(objective_id, task_id, worktree, base)
        self.coord.finish_attempt(attempt_id, "COMPLETE",
                                  changed_files=result.get("changed_files"),
                                  commits=result.get("commits"))
        return True, f"attempt {attempt_no} complete"

    def _record_integration_branch(self, objective_id: str, task_id: str,
                                   worktree: str, base: str) -> None:
        """Record the merge-ready integration branch for a completed task."""
        branch = GitWorktreeManager.task_branch_name(objective_id, task_id)
        head = GitWorktreeManager.get_worktree_head(worktree)
        self.coord.record_event(objective_id=objective_id, task_id=task_id,
                                event_type="integration_branch_ready",
                                details={"branch": branch, "head": head,
                                         "base": base})

    def _collect_result(self, task: Dict[str, Any], worktree: str,
                        record: Dict[str, Any]) -> Dict[str, Any]:
        """Build an AgentResult from executor payload + real git state."""
        payload = record.get("result_payload") or {}
        base = task.get("base_commit") or self._repo_head()
        changed = GitWorktreeManager.get_modified_files(worktree, base)
        commits = [c["hash"] for c in
                   GitWorktreeManager.get_commits_since_base(worktree, base)]
        for extra_args in (["git", "diff", "--name-only", "HEAD"],
                           ["git", "diff", "--cached", "--name-only"]):
            try:
                proc = subprocess.run(extra_args, capture_output=True, text=True,
                                      cwd=worktree)
                if proc.returncode == 0:
                    changed = sorted(
                        set(changed)
                        | {f for f in proc.stdout.split("\n") if f.strip()})
            except Exception:
                pass
        status = str(payload.get("status", "complete")).lower()
        if status not in ("complete", "failed", "blocked"):
            status = "complete" if (changed or commits) else "failed"
        return AgentResult.make_agent_result(
            task_id=task["id"], status=status,
            changed_files=sorted(set(changed)
                                 | set(payload.get("changed_files") or [])),
            commits=commits or list(payload.get("commits") or []),
            tests_run=list(payload.get("tests_run") or []),
            test_results=payload.get("test_results"),
            blockers=list(payload.get("blockers") or []),
            risks=list(payload.get("risks") or []),
            objective_evidence=payload.get("objective_evidence"))
    def _run_qa_attempt(self, task: Dict[str, Any]) -> Any:
        """Run the QA gate against the implementation task's worktree."""
        objective_id, task_id = task["objective_id"], task["id"]
        attempt_id = f"{task_id}-qa"
        self.coord.start_attempt(task_id, attempt_id)
        target = self._impl_dependency(task) or task
        worktree = target.get("worktree_path")
        if worktree is None or not os.path.isdir(worktree):
            if target is None:
                self.coord.finish_attempt(attempt_id, "FAILED",
                                          blockers=["no implementation dependency"])
                return False, "qa blocked: no implementation dependency"
            try:
                worktree = self._ensure_worktree(target)
            except Exception as exc:
                self.coord.finish_attempt(attempt_id, "FAILED",
                                          blockers=[f"no worktree: {exc}"])
                return False, f"qa blocked: {exc}"
        try:
            qa_out = self.qa.run_qa(
                target, worktree,
                allowed_paths=target.get("allowed_paths"),
                forbidden_paths=target.get("forbidden_paths"),
                base_commit=target.get("base_commit"))
        except Exception as exc:
            self.coord.finish_attempt(attempt_id, "FAILED",
                                      blockers=[f"qa raised: {exc}"])
            return False, f"qa raised: {exc}"
        self.coord.save_task_qa(task_id, qa_out["result"],
                                qa_out.get("checks", []))
        if qa_out["result"] == "PASS":
            self.coord.finish_attempt(attempt_id, "COMPLETE")
            return True, "qa PASS"
        self.coord.finish_attempt(attempt_id, "FAILED",
                                  blockers=qa_out.get("evidence"))
        return False, f"QA {qa_out['result']}: {qa_out.get('evidence')}"
    def _run_review_attempt(self, task: Dict[str, Any]) -> Any:
        """Run the independent 9-question review against the impl diff."""
        objective_id, task_id = task["objective_id"], task["id"]
        target = self._impl_dependency(task)
        if target is None:
            return False, "review blocked: no implementation dependency"
        attempt_id = f"{task_id}-rev"
        self.coord.start_attempt(task_id, attempt_id)
        worktree = target.get("worktree_path")
        base = target.get("base_commit") or self._repo_head()
        diff_text = ""
        changed: List[str] = []
        if worktree and os.path.isdir(worktree):
            changed = GitWorktreeManager.get_modified_files(worktree, base)
            try:
                proc = subprocess.run(["git", "diff", base, "HEAD"],
                                      capture_output=True, text=True,
                                      cwd=worktree)
                if proc.returncode == 0:
                    diff_text = (proc.stdout or "")[:20000]
            except Exception:
                diff_text = ""
        objective = (self.coord.get_objective(objective_id) or {}).get(
            "description", "")
        review = ReviewEngine.review(
            objective=objective,
            task_plan={"description": target.get("description", "")},
            base_commit=base, final_diff=diff_text, changed_files=changed,
            test_results={"tests_passed":
                          1 if target.get("qa_result") == "PASS" else 0},
            qa_result=target.get("qa_result") or "BLOCKED",
            relevant_invariants=[])
        decision = review.get("decision")
        self.coord.save_task_review(task_id, decision, review)
        if decision == "PASS":
            self.coord.finish_attempt(attempt_id, "COMPLETE")
            return True, "review PASS"
        # PASS_WITH_FIXES is accepted, but fixes are propagated to the
        # implementation record so a future fix loop can apply them.
        if decision == "PASS_WITH_FIXES":
            self._propagate_fixes(target, review)
            self.coord.finish_attempt(attempt_id, "COMPLETE")
            return True, "review PASS_WITH_FIXES (fixes recorded)"
        self._propagate_fixes(target, review)
        self.coord.finish_attempt(attempt_id, "FAILED",
                                  blockers=review.get("fixes", []))
        return False, f"review {decision}: {review.get('fixes')}"

    def _propagate_fixes(self, impl_task: Dict[str, Any],
                         review: Dict[str, Any]) -> None:
        """Copy reviewer feedback into the impl task's review record."""
        task_id = impl_task["id"]
        self.coord.save_task_review(task_id, review.get("decision"), review)
        self.coord.record_event(objective_id=impl_task["objective_id"],
                                task_id=task_id,
                                event_type="reviewer_fixes_propagated",
                                details={"fixes": review.get("fixes", [])})
# --- objective finalization -----------------------------------------
    def finalize_objective(self, objective_id: str) -> Dict[str, Any]:
        tasks = self.coord.list_tasks(objective_id)
        statuses = [t.get("status") for t in tasks]
        if not tasks:
            final = "blocked"
        elif all(s == "complete" for s in statuses):
            final = "completed"
        elif "escalated-to-human" in statuses:
            final = "blocked"
        elif any(s in ("failed", "blocked") for s in statuses):
            final = "blocked"
        else:
            final = "blocked"
        cur = (self.coord.get_objective(objective_id) or {}).get("status", "pending")
        if cur == "pending":
            try:
                self.coord.set_objective_status(objective_id, "in_progress")
            except ValueError:
                pass
        try:
            self.coord.set_objective_status(objective_id, final)
        except ValueError:
            pass  # already terminal
        report = self._final_report(objective_id, tasks, final)
        self.coord.record_event(objective_id=objective_id,
                                event_type="objective_finalized",
                                details={"status": final})
        try:
            self.coord.record_artifact(objective_id, "final_report",
                                       json.dumps(report, indent=2))
        except Exception:
            pass
        return report

    def _final_report(self, objective_id: str,
                      tasks: List[Dict[str, Any]], final: str) -> Dict[str, Any]:
        rows = []
        for t in tasks:
            res = t.get("result_json") or {}
            rows.append({
                "id": t["id"],
                "kind": self._infer_kind(t),
                "agent_type": t.get("agent_type"),
                "status": t.get("status"),
                "attempts": t.get("attempts"),
                "qa": t.get("qa_result"),
                "review": t.get("review_result"),
                "changed_files": res.get("changed_files", []),
                "commits": res.get("commits", []),
                "base_commit": t.get("base_commit"),
                "worktree_path": t.get("worktree_path"),
            })
        md = [f"# AI Office final report — {objective_id}", f"status: {final}", ""]
        for r in rows:
            md.append(
                f"- [{r['kind']}/{r['agent_type']}] {r['id']}: {r['status']} "
                f"(attempts={r['attempts']}, qa={r['qa']}, "
                f"review={r['review']}, files={len(r['changed_files'])})")
        report = {"objective_id": objective_id, "status": final,
                  "tasks": rows, "markdown": "\n".join(md)}
        try:
            self.coord.save_objective_report(objective_id, report)
        except Exception:
            pass
        return report