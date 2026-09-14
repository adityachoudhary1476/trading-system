from typing import Dict, Optional


class AgentSpec:
    def __init__(self, role, agency_agent_file, description, allowed_paths, forbidden_paths, mode="all"):
        self.role = role
        self.agency_agent_file = agency_agent_file
        self.description = description
        self.allowed_paths = allowed_paths
        self.forbidden_paths = forbidden_paths
        self.mode = mode


AGENT_REGISTRY = {
    "backend": AgentSpec(
        "backend",
        ".kilo/agents/backend.md",
        "Backend agent for Python/backend changes",
        ["src/trading_system/", "backend/"],
        ["src/trading_system/execution/"],
    ),
    "frontend": AgentSpec(
        "frontend",
        ".kilo/agents/frontend.md",
        "Frontend agent for React/TypeScript changes",
        ["frontend/src/", "frontend/types/"],
        ["frontend/src/pages/paper/DeploymentPicker.tsx (modify safety nets)"],
    ),
    "qa": AgentSpec(
        "qa",
        ".kilo/agents/qa.md",
        "QA agent for testing invariants",
        [],
        [],
    ),
    "reviewer": AgentSpec(
        "reviewer",
        ".kilo/agents/reviewer.md",
        "Reviewer agent for independent 9-question review",
        [],
        [],
    ),
    "architect": AgentSpec(
        "architect",
        ".kilo/agents/architect.md",
        "Architect agent for design docs",
        [],
        [],
    ),
    "researcher": AgentSpec(
        "researcher",
        ".kilo/agents/researcher.md",
        "Research agent for external investigation",
        [],
        [],
    ),
}


def get_agent_spec(role):
    return AGENT_REGISTRY.get(role)


def has_role(role):
    return role in AGENT_REGISTRY


def all_roles():
    return list(AGENT_REGISTRY.keys())


def validate_role(role):
    if role not in AGENT_REGISTRY:
        raise ValueError("Unknown agent role: " + role)
