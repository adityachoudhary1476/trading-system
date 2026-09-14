"""Core coordination layer — SQLite-backed task orchestration.

This module provides deterministic operations for:
- Objective and task management
- State transitions with validation (via StateMachine)
- Attempt and heartbeat tracking (ORPHANED reconciliation)
- Event recording (observability)
- Context/handoff artifact persistence

State lives in ``.ai-office/coordination.db`` (gitignored). Everything the
orchestrator needs to survive an interruption is persisted here — the runner
must be able to recover purely from this database.
"""

import sqlite3
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .state_transitions import StateMachine


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class CoordinationLayer:
    """SQLite-backed coordination state for AI Office v0.1."""

    DB_PATH = ".ai-office/coordination.db"

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or self.DB_PATH
        self._ensure_dirs()
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def close(self) -> None:
        """Close the SQLite connection and remove WAL/SHM sidecar files."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None  # type: ignore[assignment]
        # Clean up WAL/SHM sidecar files created by WAL mode.
        for suffix in ("-wal", "-shm"):
            sidecar = self.db_path + suffix
            try:
                os.unlink(sidecar)
            except FileNotFoundError:
                pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _ensure_dirs(self):
        """Ensure the directory for the DB exists."""
        d = os.path.dirname(self.db_path)
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)

    def _init_schema(self):
        """Initialize the database schema with all required tables."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS objectives (
                id TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                human_approval INTEGER NOT NULL DEFAULT 0,
                report_json TEXT
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                objective_id TEXT NOT NULL,
                description TEXT NOT NULL,
                scope TEXT,
                agent_type TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                dependencies TEXT NOT NULL DEFAULT '',
                allowed_paths TEXT,
                forbidden_paths TEXT,
                base_commit TEXT,
                worktree_path TEXT,
                max_attempts INTEGER NOT NULL DEFAULT 2,
                attempts INTEGER NOT NULL DEFAULT 0,
                result_json TEXT,
                test_results_json TEXT,
                qa_result TEXT,
                qa_evidence_json TEXT,
                review_result TEXT,
                review_json TEXT,
                context_json TEXT,
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                high_risk INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS attempt_lifecycle (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                agent_session_id TEXT,
                status TEXT NOT NULL DEFAULT 'RUNNING',
                started_at TEXT NOT NULL,
                heartbeat_at TEXT,
                finished_at TEXT,
                exit_code INTEGER,
                changed_files_json TEXT,
                commits_json TEXT,
                blockers TEXT,
                risks TEXT
            );

            CREATE TABLE IF NOT EXISTS orchestrator_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                objective_id TEXT,
                task_id TEXT,
                attempt_id TEXT,
                event_type TEXT NOT NULL,
                details TEXT,
                occurred_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                type TEXT NOT NULL,
                content_ref TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS schema_migrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
        """)
        self._run_migrations()

    def _run_migrations(self):
        """Apply schema migrations in order (idempotent)."""
        self._conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) "
            "SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM schema_migrations WHERE version = ?)",
            ("0.0.0", _utcnow(), "0.0.0"),
        )
        # v0.1.1: persistence columns for context handoffs and gate evidence.
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        for column in ("context_json", "qa_evidence_json", "review_json"):
            if column not in existing:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT")
        self._conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) "
            "SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM schema_migrations WHERE version = ?)",
            ("0.1.1", _utcnow(), "0.1.1"),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Objectives
    # ------------------------------------------------------------------
    def create_objective(self, objective_id: str, description: str) -> Dict[str, Any]:
        self._conn.execute(
            "INSERT OR IGNORE INTO objectives (id, description, status, created_at) "
            "VALUES (?, ?, 'pending', ?)",
            (objective_id, description, _utcnow()),
        )
        self._conn.commit()
        obj = self.get_objective(objective_id)
        self.record_event(objective_id=objective_id, event_type="objective_created",
                          details={"description": description})
        return obj

    def get_objective(self, objective_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT id, description, status, created_at, human_approval, report_json "
            "FROM objectives WHERE id = ?", (objective_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0], "description": row[1], "status": row[2],
            "created_at": row[3], "human_approval": bool(row[4]),
            "report_json": json.loads(row[5]) if row[5] else None,
        }

    def set_objective_status(self, objective_id: str, status: str) -> None:
        if status not in StateMachine.OBJECTIVE_VALID:
            raise ValueError(f"Invalid objective status: {status}")
        obj = self.get_objective(objective_id)
        if obj is None:
            raise ValueError(f"Objective not found: {objective_id}")
        err = StateMachine.transition_objective(obj["status"], status)
        if err:
            raise ValueError(err)
        self._conn.execute(
            "UPDATE objectives SET status = ? WHERE id = ?", (status, objective_id)
        )
        self._conn.commit()
        self.record_event(objective_id=objective_id,
                          event_type="objective_status_changed",
                          details={"from": obj["status"], "to": status})

    def set_objective_human_approval(self, objective_id: str, approved: bool) -> None:
        self._conn.execute(
            "UPDATE objectives SET human_approval = ? WHERE id = ?",
            (1 if approved else 0, objective_id),
        )
        self._conn.commit()
        self.record_event(objective_id=objective_id, event_type="human_approval",
                          details={"approved": approved})

    def save_objective_report(self, objective_id: str, report: Dict[str, Any]) -> None:
        self._conn.execute(
            "UPDATE objectives SET report_json = ? WHERE id = ?",
            (json.dumps(report), objective_id),
        )

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------
    def create_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO tasks
                (id, objective_id, description, scope, agent_type, status,
                 dependencies, allowed_paths, forbidden_paths, base_commit,
                 worktree_path, max_attempts, attempts, created_at, high_risk)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                task["id"], task["objective_id"], task["description"],
                task.get("scope"), task.get("agent_type"),
                json.dumps(task.get("dependencies", [])),
                json.dumps(task.get("allowed_paths", [])),
                json.dumps(task.get("forbidden_paths", [])),
                task.get("base_commit"), task.get("worktree_path"),
                task.get("max_attempts", 2), _utcnow(),
                int(task.get("high_risk", 0)),
            ),
        )
        self._conn.commit()
        self.record_event(objective_id=task["objective_id"], task_id=task["id"],
                          event_type="task_created",
                          details={"agent_type": task.get("agent_type")})
        return self.get_task(task["id"])

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        col_names = [
            d[0] for d in self._conn.execute(
                "SELECT * FROM tasks LIMIT 1"
            ).description
        ]
        task = dict(zip(col_names, row))
        for field in ("dependencies", "allowed_paths", "forbidden_paths"):
            task[field] = json.loads(task[field] or "[]")
        for field in ("result_json", "test_results_json", "qa_evidence_json",
                      "review_json", "context_json"):
            task[field] = json.loads(task[field]) if task[field] else None
        task["high_risk"] = bool(task.get("high_risk"))
        return task

    def list_tasks(self, objective_id: str) -> List[Dict[str, Any]]:
        ids = [r[0] for r in self._conn.execute(
            "SELECT id FROM tasks WHERE objective_id = ? ORDER BY created_at, id",
            (objective_id,),
        ).fetchall()]
        return [self.get_task(tid) for tid in ids]

    def transition_task(self, task_id: str, new_status: str,
                        error: Optional[str] = None) -> None:
        task = self.get_task(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        err = StateMachine.transition_task(task["status"], new_status)
        if err:
            raise ValueError(
                f"Invalid task transition {task['status']!r} -> {new_status!r}: {err}"
                + (f" ({error})" if error else "")
            )
        updates: Dict[str, Any] = {"status": new_status}
        if new_status == "in_progress" and not task.get("started_at"):
            updates["started_at"] = _utcnow()
        if new_status in ("complete", "failed", "escalated-to-human"):
            updates["finished_at"] = _utcnow()
        if new_status == "complete":
            updates["completed_at"] = _utcnow()
        sets = ", ".join(f"{k} = ?" for k in updates)
        self._conn.execute(
            f"UPDATE tasks SET {sets} WHERE id = ?",
            (*updates.values(), task_id),
        )
        self._conn.commit()
        self.record_event(objective_id=task["objective_id"], task_id=task_id,
                          event_type="task_status_changed",
                          details={"from": task["status"], "to": new_status,
                                   "error": error})

    def increment_task_attempts(self, task_id: str) -> int:
        task = self.get_task(task_id)
        attempts = (task["attempts"] or 0) + 1
        self._conn.execute(
            "UPDATE tasks SET attempts = ? WHERE id = ?", (attempts, task_id)
        )
        self._conn.commit()
        return attempts

    def save_task_worktree(self, task_id: str, worktree_path: str,
                           base_commit: str) -> None:
        self._conn.execute(
            "UPDATE tasks SET worktree_path = ?, base_commit = ? WHERE id = ?",
            (worktree_path, base_commit, task_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Attempts (heartbeat / orphan reconciliation)
    # ------------------------------------------------------------------
    def start_attempt(self, task_id: str, attempt_id: str,
                      agent_session_id: Optional[str] = None) -> None:
        self._conn.execute(
            "INSERT INTO attempt_lifecycle "
            "(task_id, attempt_id, agent_session_id, status, started_at, heartbeat_at) "
            "VALUES (?, ?, ?, 'RUNNING', ?, ?)",
            (task_id, attempt_id, agent_session_id, _utcnow(), _utcnow()),
        )
        self._conn.commit()
        self.record_event(task_id=task_id, attempt_id=attempt_id,
                          event_type="attempt_started", details={})

    def heartbeat(self, attempt_id: str) -> None:
        self._conn.execute(
            "UPDATE attempt_lifecycle SET heartbeat_at = ? WHERE attempt_id = ? "
            "AND status = 'RUNNING'",
            (_utcnow(), attempt_id),
        )
        self._conn.commit()

    def finish_attempt(self, attempt_id: str, status: str,
                       exit_code: Optional[int] = None,
                       changed_files: Optional[List[str]] = None,
                       commits: Optional[List[str]] = None,
                       blockers: Optional[List[str]] = None,
                       risks: Optional[List[str]] = None) -> None:
        if status not in StateMachine.ATTEMPT_VALID:
            raise ValueError(f"Invalid attempt status: {status}")
        self._conn.execute(
            "UPDATE attempt_lifecycle SET status = ?, finished_at = ?, exit_code = ?, "
            "changed_files_json = ?, commits_json = ?, blockers = ?, risks = ? "
            "WHERE attempt_id = ? AND status = 'RUNNING'",
            (status, _utcnow(), exit_code,
             json.dumps(changed_files or []), json.dumps(commits or []),
             json.dumps(blockers or []), json.dumps(risks or []), attempt_id),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT task_id FROM attempt_lifecycle WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        self.record_event(task_id=row[0] if row else None, attempt_id=attempt_id,
                          event_type="attempt_finished",
                          details={"status": status, "exit_code": exit_code})

    def orphan_stale_attempts(self, timeout_seconds: int = 300) -> List[str]:
        """Mark RUNNING attempts with stale heartbeats as ORPHANED."""
        cutoff = datetime.now(timezone.utc).timestamp() - timeout_seconds
        rows = self._conn.execute(
            "SELECT attempt_id, heartbeat_at, started_at FROM attempt_lifecycle "
            "WHERE status = 'RUNNING'"
        ).fetchall()
        orphaned: List[str] = []
        for attempt_id, heartbeat_at, started_at in rows:
            ref = heartbeat_at or started_at
            try:
                ref_ts = datetime.fromisoformat(ref).timestamp()
            except (TypeError, ValueError):
                continue
            if ref_ts < cutoff:
                self.finish_attempt(attempt_id, "ORPHANED",
                                    blockers=["stale heartbeat"])
                orphaned.append(attempt_id)
        return orphaned


    # ------------------------------------------------------------------
    # Events & artifacts (observability)
    # ------------------------------------------------------------------
    def record_event(self, event_type: str, objective_id: Optional[str] = None,
                     task_id: Optional[str] = None, attempt_id: Optional[str] = None,
                     details: Optional[Dict[str, Any]] = None) -> None:
        self._conn.execute(
            "INSERT INTO orchestrator_events "
            "(objective_id, task_id, attempt_id, event_type, details, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (objective_id, task_id, attempt_id, event_type,
             json.dumps(details or {}), _utcnow()),
        )
        self._conn.commit()

    def list_events(self, objective_id: Optional[str] = None,
                    limit: int = 200) -> List[Dict[str, Any]]:
        query = ("SELECT id, objective_id, task_id, attempt_id, event_type, details, "
                 "occurred_at FROM orchestrator_events")
        params: List[Any] = []
        if objective_id:
            query += " WHERE objective_id = ?"
            params.append(objective_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(query, params).fetchall()
        events = []
        for r in rows:
            try:
                details = json.loads(r[5]) if r[5] else {}
            except (TypeError, ValueError):
                details = {"raw": r[5]}
            events.append({
                "id": r[0], "objective_id": r[1], "task_id": r[2],
                "attempt_id": r[3], "event_type": r[4], "details": details,
                "occurred_at": r[6],
            })
        return events

    def record_artifact(self, task_id: str, artifact_type: str,
                        content: str) -> None:
        self._conn.execute(
            "INSERT INTO artifacts (task_id, type, content_ref, created_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, artifact_type, content, _utcnow()),
        )
        self._conn.commit()

    def list_artifacts(self, task_id: Optional[str] = None,
                       artifact_type: Optional[str] = None) -> List[Dict[str, Any]]:
        query = "SELECT id, task_id, type, content_ref, created_at FROM artifacts"
        clauses, params = [], []
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if artifact_type:
            clauses.append("type = ?")
            params.append(artifact_type)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"
        rows = self._conn.execute(query, params).fetchall()
        return [{
            "id": r[0], "task_id": r[1], "type": r[2],
            "content": r[3], "created_at": r[4],
        } for r in rows]

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.ProgrammingError:
            pass


    def save_task_result(self, task_id: str, result: Dict[str, Any]) -> None:
        self._conn.execute(
            "UPDATE tasks SET result_json = ? WHERE id = ?",
            (json.dumps(result), task_id),
        )
        self._conn.commit()

    def save_task_qa(self, task_id: str, qa_result: str, evidence: Any) -> None:
        self._conn.execute(
            "UPDATE tasks SET qa_result = ?, qa_evidence_json = ? WHERE id = ?",
            (qa_result, json.dumps(evidence), task_id),
        )
        self._conn.commit()

    def save_task_review(self, task_id: str, review_result: str,
                         review: Dict[str, Any]) -> None:
        self._conn.execute(
            "UPDATE tasks SET review_result = ?, review_json = ? WHERE id = ?",
            (review_result, json.dumps(review), task_id),
        )
        self._conn.commit()

    def save_task_context(self, task_id: str, context: Dict[str, Any]) -> None:
        """Persist the context handoff package for a task."""
        self._conn.execute(
            "UPDATE tasks SET context_json = ? WHERE id = ?",
            (json.dumps(context), task_id),
        )
        self._conn.commit()

