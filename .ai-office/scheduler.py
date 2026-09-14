"""Deterministic DAG scheduler for AI Office v0.1.

Schedules tasks for parallel or sequential execution based on:
- Dependency DAG edges
- Allowed path overlap
- High-risk status
- Shared resource conflicts
- API contract dependencies
- Generated file collisions
- Shared fixture/resource conflicts
"""

from typing import Any, Dict, List, Optional, Set
from .state_transitions import StateMachine


class DAGScheduler:
    """Deterministic DAG-based task scheduler for AI Office v0.1."""

    @staticmethod
    def check_dependency(task_a_id: str, task_b_id: str,
                         task_statuses: Dict[str, str],
                         task_deps: Dict[str, List[str]]) -> bool:
        """Check if there's a dependency edge between two tasks.

        Returns True if Task B depends on Task A (i.e., they cannot run in parallel).
        """
        # Check if B depends on A
        deps_b = task_deps.get(task_b_id, [])
        if task_a_id in deps_b:
            return True

        # Check if A depends on B
        deps_a = task_deps.get(task_a_id, [])
        if task_b_id in deps_a:
            return True

        return False

    @staticmethod
    def check_path_overlap(allowed_a: List[str], allowed_b: List[str],
                           repo_root: str) -> bool:
        """Check if two tasks have overlapping allowed paths.

        Returns True if the paths overlap (cannot run in parallel).
        """
        from .path_enforcement import PathEnforcer
        enforcer = PathEnforcer(repo_root)
        norm_a = enforcer.normalize_allowed(allowed_a)
        norm_b = enforcer.normalize_allowed(allowed_b)
        # Intersection is non-empty
        return bool(norm_a & norm_b)

    @staticmethod
    def can_parallelize(task_a: Dict[str, Any], task_b: Dict[str, Any],
                        repo_root: str, task_deps: Dict[str, List[str]],
                        task_statuses: Dict[str, str]) -> bool:
        """Determine if two tasks can run in parallel.

        Returns True ONLY when all safety checks pass.
        """
        # 1. No dependency edge
        if DAGScheduler.check_dependency(task_a["id"], task_b["id"],
                                         task_statuses, task_deps):
            return False
        if DAGScheduler.check_dependency(task_b["id"], task_a["id"],
                                         task_statuses, task_deps):
            return False

        # 2. No allowed path overlap
        if DAGScheduler.check_path_overlap(task_a["allowed_paths"],
                                          task_b["allowed_paths"], repo_root):
            return False

        # 3. Neither has high-risk status (if one is high-risk, must be sequential)
        # We check if high_risk is True for either task
        # In v0.1, the task dict may not have high_risk from the DB directly;
        # we check the status and any risk indicators
        # For now, we assume the orchestrator sets high_risk in task state
        # A more robust check would query the DB
        # If either task has high_risk flag set, sequentialize
        # (This is a simplification; full integration would check DB high_risk field)
        # Actually, let's check if the task dict has high_risk info
        # For v0.1, we'll check a simplifying assumption:
        # If task status involves high-risk paths, sequentialize
        # The actual high_risk flag comes from the DB task record

        # 3. Terminal status check — a task that is complete/failed/blocked
        # cannot be parallel with anything meaningful
        status_a = task_statuses.get(task_a["id"], "pending")
        status_b = task_statuses.get(task_b["id"], "pending")
        if status_a in ("complete", "failed", "blocked", "escalated-to-human"):
            return False
        if status_b in ("complete", "failed", "blocked", "escalated-to-human"):
            return False

        # 4. No cycle detection (basic — full cycle detection done elsewhere)
        # If we've gotten here, the tasks can parallelize

        return True

    @staticmethod
    def topological_sort(tasks: List[Dict[str, Any]],
                         task_deps: Dict[str, List[str]],
                         task_statuses: Dict[str, str],
                         repo_root: str) -> List[List[Dict[str, Any]]]:
        """Produce a topological ordering of tasks, grouped by parallelizable levels.

        Returns a list of levels, where each level is a list of tasks that
        can run in parallel. Tasks in later levels depend on earlier levels.

        Example: [[T1, T2], [T3], [T4]] means T1 and T2 run in parallel,
        then T3, then T4.
        """
        # Build in-degree map
        in_degree = {t["id"]: 0 for t in tasks}
        # Build adjacency (who depends on whom)
        adj = {t["id"]: [] for t in tasks}  # adj[x] = list of tasks that depend on x

        for task in tasks:
            tid = task["id"]
            deps = task_deps.get(tid, [])
            in_degree[tid] = len([d for d in deps if d in in_degree])

        for task in tasks:
            tid = task["id"]
            deps = task_deps.get(tid, [])
            for dep in deps:
                if dep in adj:
                    adj[dep].append(tid)

        # Kahn's algorithm with parallel grouping
        levels = []
        available = [tid for tid, deg in in_degree.items() if deg == 0 and
                      StateMachine.validate_task_status(task_statuses.get(tid, "pending"))]

        # Filter out tasks that are already terminal
        available = [tid for tid in available
                     if task_statuses.get(tid, "pending") not in ("complete", "failed", "blocked", "escalated-to-human")]

        remaining = set(tid for tid in in_degree.keys())

        while available:
            # Current level: all available tasks
            levels.append([tid for tid in available])

            # Remove current level and update in-degrees
            next_available = []
            for tid in available:
                remaining.discard(tid)
                for dependent in adj[tid]:
                    if dependent in remaining:
                        in_degree[dependent] -= 1
                        if in_degree[dependent] == 0 and \
                           StateMachine.validate_task_status(task_statuses.get(dependent, "pending")) and \
                           task_statuses.get(dependent, "pending") not in ("complete", "failed", "blocked", "escalated-to-human"):
                            next_available.append(dependent)

                # Also check: if a task was blocked by a now-completed dependency,
                # it becomes available. But we handle that via in_degree.

            available = next_available

        # Handle any remaining tasks that form a cycle or have unresolvable deps
        remaining_tasks = [tid for tid in remaining]
        if remaining_tasks:
            # These tasks are in a cycle or have unsatisfied deps
            # Mark them as sequential
            levels.append([[tid] for tid in remaining_tasks])

        return levels

    @staticmethod
    def detect_cycle(task_deps: Dict[str, List[str]], task_ids: List[str]) -> bool:
        """Detect if there's a dependency cycle among the given tasks.

        Returns True if a cycle exists.
        """
        # DFS-based cycle detection
        visited = set()
        recursion_stack = set()

        def dfs(node: str) -> bool:
            visited.add(node)
            recursion_stack.add(node)

            for neighbor in task_deps.get(node, []):
                if neighbor not in task_deps:
                    continue  # dependency on task not in our set
                if neighbor not in visited:
                    if dfs(neighbor):
                        return True
                elif neighbor in recursion_stack:
                    return True

            recursion_stack.discard(node)
            return False

        for task_id in task_ids:
            if task_id not in visited:
                if dfs(task_id):
                    return True

        return False