"""Test the AI Office task planner."""
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from ai_office import Planner, CoordinationLayer, DAGScheduler


@pytest.fixture
def repo_root():
    return Path(__file__).parent.parent


@pytest.fixture
def temp_db():
    import tempfile
    import os
    fd, path = tempfile.mkstemp(suffix=".db", prefix="ai_office_test_")
    os.close(fd)
    yield path
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        try:
            os.unlink(p)
        except (FileNotFoundError, PermissionError):
            pass


@pytest.fixture
def coord(temp_db):
    c = CoordinationLayer(db_path=temp_db)
    yield c
    c.close()


class TestPlanner:
    """Test the task planner."""

    def test_plan_creates_tasks(self, coord, repo_root):
        objective_id = f"test-obj-{uuid.uuid4().hex[:8]}"
        planner = Planner(coord, str(repo_root))
        tasks = planner.plan(
            objective_id,
            "Implement a simple feature",
            task_type="backend",
            max_attempts=1,
        )
        assert len(tasks) >= 3


class TestDAGScheduler:
    """Test the DAG scheduler."""

    def test_topological_sort_simple(self, repo_root):
        scheduler = DAGScheduler()
        tasks = [
            {"id": "task1", "dependencies": []},
            {"id": "task2", "dependencies": ["task1"]},
        ]
        deps = {"task1": [], "task2": ["task1"]}
        statuses = {"task1": "pending", "task2": "pending"}
        levels = scheduler.topological_sort(tasks, deps, statuses, str(repo_root))
        assert len(levels) >= 1
