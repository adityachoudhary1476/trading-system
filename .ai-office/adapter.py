"""Agent dispatch adapter — real execution boundary for AI Office v0.1.

The adapter is the only place where work crosses from the deterministic
orchestration engine into a live agent runtime. It validates the agent role
against the registry, validates the worktree, executes the agent through a
**real executor** (OpenCode CLI or an arbitrary command template), and
returns honest execution records.

Honesty rules:
- If no executor is configured for a role, the dispatch BLOCKS — it never
  fabricates a successful result.
- Malformed agent output is recorded and fails the attempt; it never crashes
  the orchestrator nor silently passes.
"""

import json
import os
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .agent_registry import get_agent_spec, validate_role
from .coordination import CoordinationLayer


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class OpenCodeExecutor:
    """Executes an agent via the OpenCode CLI (``opencode run``).

    This is the production executor: OpenCode hosts the Agency-style agent
    (role definition from ``.kilo/agents/<role>.md``) and performs real work
    in the worktree. The agent is instructed to print a JSON AgentResult
    block in its final message, which we parse.
    """

    def __init__(self, binary: str = "opencode", timeout_seconds: int = 1800):
        self.binary = binary
        self.timeout_seconds = timeout_seconds

    def build_command(self, role: str, prompt: str) -> List[str]:
        return [self.binary, "run", "--agent", role, prompt]

    def __call__(self, role: str, prompt: str, worktree_path: str,
                 context_file: Optional[str] = None) -> Dict[str, Any]:
        cmd = self.build_command(role, prompt)
        proc = subprocess.run(
            cmd, capture_output=True, text=True, cwd=worktree_path,
            timeout=self.timeout_seconds,
        )
        stdout = proc.stdout or ""
        return {
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": proc.stderr or "",
            "result_payload": extract_result_payload(stdout),
        }


class CommandExecutor:
    """Executes an agent via an arbitrary command template.

    The template is a list of argv tokens where the placeholders
    ``{worktree}`` and ``{context}`` are substituted with the worktree path
    and the path of a JSON context-handoff file. The command must be real —
    its actual exit code, stdout and the worktree's actual git state decide
    the outcome.
    """

    def __init__(self, template: List[str], timeout_seconds: int = 1800):
        if not template:
            raise ValueError("CommandExecutor requires a non-empty argv template")
        self.template = template
        self.timeout_seconds = timeout_seconds

    def __call__(self, role: str, prompt: str, worktree_path: str,
                 context_file: Optional[str] = None) -> Dict[str, Any]:
        cmd = [
            token.replace("{worktree}", worktree_path)
                 .replace("{context}", context_file or "")
                 .replace("{role}", role)
            for token in self.template
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, cwd=worktree_path,
            timeout=self.timeout_seconds,
        )
        stdout = proc.stdout or ""
        return {
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": proc.stderr or "",
            "result_payload": extract_result_payload(stdout),
        }


def extract_result_payload(stdout: str) -> Optional[Dict[str, Any]]:
    """Extract the JSON AgentResult payload from agent output.

    Agents are instructed to emit a fenced block like::

        ```ai-office-result
        {"task_id": "...", "status": "complete", ...}
        ```

    A bare JSON object as the entire (stripped) output is also accepted.
    Returns None when no parseable payload exists (malformed output).
    """
    marker = "```ai-office-result"
    payloads: List[str] = []
    if marker in stdout:
        for block in stdout.split(marker)[1:]:
            block = block.split("```", 1)[0].strip()
            if block:
                payloads.append(block)
    stripped = stdout.strip()
    if not payloads and stripped.startswith("{") and stripped.endswith("}"):
        payloads.append(stripped)
    # Take the LAST parseable payload (final message wins).
    for candidate in reversed(payloads):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def build_agent_prompt(task: Dict[str, Any], spec) -> str:
    """Build the Agency-style handoff prompt for an agent.

    Uses the finova-orchestrator handoff format: OBJECTIVE / CONTEXT /
    RELEVANT FILES / CONSTRAINTS / EXPECTED OUTPUT / VERIFICATION REQUIRED.
    """
    lines = [
        f"OBJECTIVE: {task.get('description', '')}",
        "CONTEXT:",
        f"- Scope: {task.get('scope', 'unknown')}",
        f"- Base commit: {task.get('base_commit', '')}",
        f"- Agent definition: {spec.agency_agent_file}",
        "RELEVANT FILES: see handoff-context.json next to this worktree",
        "CONSTRAINTS:",
        "- Modify ONLY files under the allowed paths (repo-relative):",
    ]
    for p in task.get("allowed_paths", []) or []:
        lines.append(f"  - {p}")
    lines.append("- Never touch files under the forbidden paths (repo-relative):")
    for p in task.get("forbidden_paths", []) or []:
        lines.append(f"  - {p}")
    lines += [
        "- Commit your work in this worktree; keep commits reviewable.",
        "- Paper-only: never add live trading, live broker, or credential code.",
        "EXPECTED OUTPUT:",
        "- Real changes committed in this worktree.",
        "- End with a fenced JSON AgentResult block:",
        "  ```ai-office-result",
        '  {"task_id": "...", "status": "complete|failed|blocked", '
        '"changed_files": [...], "commits": [...], "tests_run": [...], '
        '"test_results": "PASS|FAIL|BLOCKED", "objective_evidence": '
        '{"criteria_met": true, "details": "..."}, "blockers": [], "risks": []}',
        "  ```",
        "VERIFICATION REQUIRED: tests you ran and their real results.",
    ]
    return "\n".join(lines)



class AgentDispatcher:
    """Validates and executes agent dispatches, with SQLite-backed auditing."""

    def __init__(self, coord: CoordinationLayer,
                 executors: Optional[Dict[str, Callable[..., Dict[str, Any]]]] = None,
                 default_executor: Optional[Callable[..., Dict[str, Any]]] = None):
        self.coord = coord
        self.executors = executors or {}
        self.default_executor = default_executor

    def executor_for(self, role: str) -> Optional[Callable[..., Dict[str, Any]]]:
        return self.executors.get(role, self.default_executor)

    def validate_worktree(self, worktree_path: str, base_commit: str) -> None:
        if not os.path.isdir(worktree_path):
            raise ValueError(f"Worktree directory does not exist: {worktree_path}")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=worktree_path,
        )
        if head.returncode != 0:
            raise ValueError(f"Cannot read worktree HEAD: {head.stderr.strip()}")
        actual = head.stdout.strip()
        if base_commit and base_commit[:12] not in actual:
            raise ValueError(
                f"Worktree HEAD {actual[:12]} does not match base commit "
                f"{base_commit[:12]}"
            )

    def dispatch(self, task: Dict[str, Any], worktree_path: str,
                 context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Dispatch an agent for `task` inside `worktree`.

        Returns the raw execution record. Raises on validation errors and
        returns honest failure data (never fabricated success) otherwise.
        """
        agent_type = task.get("agent_type") or ""
        if not agent_type or not validate_role(agent_type):
            raise ValueError(f"Unknown agent type: {agent_type!r}")
        spec = get_agent_spec(agent_type)
        if spec is None:
            raise ValueError(f"Agent spec not found for role: {agent_type}")

        base_commit = task.get("base_commit") or ""
        self.validate_worktree(worktree_path, base_commit)

        executor = self.executor_for(agent_type)
        if executor is None:
            blocked = {
                "task_id": task.get("task_id") or task.get("id"),
                "objective_id": task.get("objective_id"),
                "agent_role": agent_type,
                "worktree_path": worktree_path,
                "start_time": _utcnow(),
                "end_time": _utcnow(),
                "exit_status": None,
                "execution_status": "BLOCKED_NO_EXECUTOR",
                "stdout": "",
                "stderr": "",
                "result_payload": None,
                "changed_files": [],
                "commits": [],
                "blockers": [
                    f"No executor configured for agent role: {agent_type}"
                ],
                "risks": [],
            }
            self.coord.record_event(
                objective_id=task.get("objective_id"),
                task_id=task.get("task_id") or task.get("id"),
                event_type="agent_blocked_no_executor",
                details={"agent_type": agent_type},
            )
            return blocked

        # Materialize the context handoff file next to the worktree.
        context_file: Optional[str] = None
        if context is not None:
            context_path = os.path.join(
                os.path.dirname(worktree_path.rstrip("/\\") or "."),
                "handoff-context.json",
            )
            with open(context_path, "w", encoding="utf-8") as fh:
                json.dump(context, fh, indent=2)
            context_file = context_path

        prompt = build_agent_prompt(task, spec)
        attempt_id = f"{task.get('task_id') or task.get('id')}-{_utcnow()}"
        self.coord.record_event(
            objective_id=task.get("objective_id"),
            task_id=task.get("task_id") or task.get("id"),
            attempt_id=attempt_id,
            event_type="agent_dispatched",
            details={"agent_type": agent_type,
                     "worktree_path": worktree_path,
                     "agent_spec": spec.agency_agent_file},
        )

        started = _utcnow()
        execution = executor(agent_type, prompt, worktree_path, context_file)
        return {
            "task_id": task.get("task_id") or task.get("id"),
            "objective_id": task.get("objective_id"),
            "agent_role": agent_type,
            "agency_agent_file": spec.agency_agent_file,
            "worktree_path": worktree_path,
            "attempt_id": attempt_id,
            "start_time": started,
            "end_time": _utcnow(),
            "exit_status": execution.get("exit_code"),
            "execution_status": "EXECUTED",
            "stdout": execution.get("stdout", ""),
            "stderr": execution.get("stderr", ""),
            "result_payload": execution.get("result_payload"),
            "changed_files": [],
            "commits": [],
            "blockers": [],
            "risks": [],
        }

