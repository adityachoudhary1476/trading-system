"""Test the AI Office agent registry."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from ai_office import all_roles, get_agent_spec


class TestAgentRegistry:
    """Test the agent registry."""

    def test_all_roles_returns_six_roles(self):
        roles = all_roles()
        assert len(roles) == 6
        assert "backend" in roles
        assert "frontend" in roles
        assert "qa" in roles
        assert "reviewer" in roles
        assert "architect" in roles
        assert "researcher" in roles

    def test_get_agent_spec_returns_spec(self):
        spec = get_agent_spec("backend")
        assert spec is not None
        assert spec.role == "backend"

    def test_get_agent_spec_unknown_role(self):
        spec = get_agent_spec("nonexistent")
        assert spec is None
