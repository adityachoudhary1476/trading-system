"""Test the AI Office task type detection."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from ai_office import detect_task_type


class TestDetectTaskType:
    """Test task type detection."""

    def test_detect_backend_task(self):
        task_type = detect_task_type(
            "Implement a new API endpoint in the backend",
            ["src/trading_system/"],
        )
        assert task_type == "backend"

    def test_detect_frontend_task(self):
        task_type = detect_task_type(
            "Create a new React component for the dashboard",
            ["frontend/src/"],
        )
        assert task_type == "frontend"

    def test_detect_fullstack_task(self):
        task_type = detect_task_type(
            "Build a new feature with both backend and frontend",
            ["src/trading_system/", "frontend/src/"],
        )
        assert task_type == "fullstack"

    def test_detect_docs_task(self):
        task_type = detect_task_type(
            "Write documentation for the API",
            ["docs/"],
        )
        assert task_type == "docs"
