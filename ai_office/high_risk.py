"""High-risk path detection for AI Office v0.1.

Deterministic identification of critical paths that require additional
review, human approval, and stricter integration controls.

The trading-system is a trading system — certain paths are inherently
high-risk and must not be automatically integrated.
"""

import os
from typing import Dict, List, Set, Tuple, Any
from .path_enforcement import _normalize_path


# High-risk path categories — these are deterministic and based on
# the trading-system domain knowledge. Not LLM-dependent.
HIGH_RISK_CATEGORIES = {
    "CRITICAL": {
        # Paths where any change could cause live financial actions
        "patterns": [
            "src/trading_system/execution/",
            "src/trading_system/execution/paper_broker.py",
        ],
        "description": "Critical: Any change could enable live trading or affect order placement",
    },
    "HIGH": {
        # Paths where changes violate paper-only invariants or deployment gates
        "patterns": [
            "src/trading_system/paper_api/router.py",
            "src/trading_system/paper_trading/",
            "backend/routes/paper_api.py",
            "src/trading_system/risk/",
            "src/trading_system/india/",
        ],
        "description": "High: Violates paper-only invariants, deployment gate, or risk controls",
    },
    "MEDIUM": {
        "patterns": [
            "src/trading_system/config/",
            "frontend/src/pages/paper/",
        ],
        "description": "Medium: Standard review required; no credential hardcoding",
    },
}


class HighRiskDetector:
    """Deterministic high-risk path detection for AI Office tasks."""
    
    def __init__(self, repo_root: str):
        self.repo_root = os.path.abspath(repo_root)
        self._cache: Dict[str, str] = {}  # task_id -> risk level
    
    def assess_task_risk(self, allowed_paths: List[str], task_id: str = "") -> str:
        """Assess the risk level of a task based on its allowed paths.
        
        Returns one of: 'CRITICAL', 'HIGH', 'MEDIUM', 'LOW'
        """
        # Normalize allowed paths
        normalized = self.normalize_allowed_paths(allowed_paths)
        
        # Check each category, from most restrictive to least
        for level in ["CRITICAL", "HIGH", "MEDIUM"]:
            patterns = HIGH_RISK_CATEGORIES[level]["patterns"]
            for pattern in patterns:
                norm_pattern = _normalize_path(pattern, self.repo_root)
                # Check if any allowed path matches or is subordinate to the pattern
                for ap in normalized:
                    if ap == norm_pattern or ap.startswith(norm_pattern + "/") or norm_pattern.startswith(ap + "/"):
                        return level
        
        return "LOW"
    
    def normalize_allowed_paths(self, allowed_paths: List[str]) -> Set[str]:
        """Normalize allowed paths for risk assessment."""
        return {_normalize_path(p, self.repo_root) for p in allowed_paths}
    
    def path_matches_high_risk(self, path: str, high_risk_level: str) -> bool:
        """Check if a specific path matches a high-risk category."""
        norm_path = _normalize_path(path, self.repo_root)
        
        if high_risk_level == "CRITICAL":
            patterns = HIGH_RISK_CATEGORIES["CRITICAL"]["patterns"]
        elif high_risk_level == "HIGH":
            patterns = HIGH_RISK_CATEGORIES["HIGH"]["patterns"]
        elif high_risk_level == "MEDIUM":
            patterns = HIGH_RISK_CATEGORIES["MEDIUM"]["patterns"]
        else:
            return False
        
        for pattern in patterns:
            norm_pattern = _normalize_path(pattern, self.repo_root)
            if norm_path == norm_pattern or norm_path.startswith(norm_pattern + "/"):
                return True
        
        return False
    
    def get_risk_level(self) -> str:
        """Get the highest risk level currently detected (for caching/state)."""
        # This is a static classifier; the level is determined by allowed_paths
        return "LOW"  # Will be set per-task assessment
    
    @staticmethod
    def required_controls(risk_level: str) -> Dict[str, Any]:
        """Get the required controls for a given risk level."""
        controls = {
            "CRITICAL": {
                "human_approval": True,
                "prohibit_automatic_integration": True,
                "prohibit_automatic_retry": True,
                "reviewer_required": True,
                "escalate_immediately": True,
            },
            "HIGH": {
                "human_approval": True,
                "prohibit_automatic_integration": True,
                "reviewer_required": True,
                "escalate_immediately": True,
            },
            "MEDIUM": {
                "human_approval": False,
                "prohibit_automatic_integration": False,
                "reviewer_required": True,
                "escalate_immediately": False,
            },
            "LOW": {
                "human_approval": False,
                "prohibit_automatic_integration": False,
                "reviewer_required": False,
                "escalate_immediately": False,
            },
        }
        return controls.get(risk_level, controls["LOW"])