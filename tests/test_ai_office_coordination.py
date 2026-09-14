"""Test the AI Office coordination layer."""
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from ai_office import CoordinationLayer


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


class TestCoordinationLayer:
    """Test the coordination layer."""

    def test_create_objective(self, coord):
        objective_id = "test-obj-" + uuid.uuid4().hex[:8]
        result = coord.create_objective(objective_id, "Test objective")
        assert result is not None
        assert result["id"] == objective_id

    def test_get_objective(self, coord):
        objective_id = "test-obj-" + uuid.uuid4().hex[:8]
        coord.create_objective(objective_id, "Test objective")
        obj = coord.get_objective(objective_id)
        assert obj is not None
        assert obj["description"] == "Test objective"

    def test_set_objective_status(self, coord):
        objective_id = "test-obj-" + uuid.uuid4().hex[:8]
        coord.create_objective(objective_id, "Test objective")
        coord.set_objective_status(objective_id, "in_progress")
        obj = coord.get_objective(objective_id)
        assert obj["status"] == "in_progress"

    def test_create_task(self, coord):
        objective_id = "test-obj-" + uuid.uuid4().hex[:8]
        coord.create_objective(objective_id, "Test objective")
        task_id = objective_id + "-task1"
        task = coord.create_task({
            "id": task_id,
            "objective_id": objective_id,
            "description": "Test task",
            "scope": "test",
            "agent_type": "backend",
            "dependencies": [],
            "base_commit": "abc123",
        })
        assert task is not None
        assert task["id"] == task_id

    def test_transition_task(self, coord):
        objective_id = "test-obj-" + uuid.uuid4().hex[:8]
        coord.create_objective(objective_id, "Test objective")
        task_id = objective_id + "-task1"
        coord.create_task({
            "id": task_id,
            "objective_id": objective_id,
            "description": "Test task",
            "scope": "test",
            "agent_type": "backend",
            "dependencies": [],
            "base_commit": "abc123",
        })
        coord.transition_task(task_id, "in_progress")
        task = coord.get_task(task_id)
        assert task["status"] == "in_progress"
