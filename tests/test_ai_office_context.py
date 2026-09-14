"""Test the AI Office context handoff."""
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from ai_office import ContextHandoff, CoordinationLayer


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


class TestContextHandoff:
    """Test context handoff."""

    def test_build_handoff(self, coord):
        objective_id = f"test-obj-{uuid.uuid4().hex[:8]}"
        coord.create_objective(objective_id, "Test objective")
        task_id = f"{objective_id}-task1"
        coord.create_task({
            "id": task_id,
            "objective_id": objective_id,
            "description": "Test task",
            "scope": "test",
            "agent_type": "backend",
            "dependencies": [],
            "base_commit": "abc123",
        })
        handoff_builder = ContextHandoff(coord)
        task = coord.get_task(task_id)
        handoff = handoff_builder.build_handoff(task)
        assert handoff["handoff_version"] == "v1"
        assert handoff["OBJECTIVE"] == "Test task"
