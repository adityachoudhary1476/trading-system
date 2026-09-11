"""Tournament report generation (Phase 24).

Produces deterministic reports with reproducibility metadata.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "TournamentReport",
    "generate_report",
]


@dataclass
class TournamentReport:
    tournament_id: str
    generated_at: str
    config: dict[str, Any]
    universe: dict[str, Any]
    research: dict[str, Any]
    qualification: dict[str, Any]
    diversification: dict[str, Any]
    deployment: dict[str, Any]
    failures: list[dict[str, Any]]
    tests: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, default=str)


def generate_report(
    *,
    tournament_id: str,
    config: dict[str, Any],
    universe_total: int,
    universe_families: list[str],
    backtested_count: int,
    validated_count: int,
    failed_count: int,
    qualified_count: int,
    rejected_count: int,
    top_20: list[dict[str, Any]],
    top_10: list[dict[str, Any]],
    final_n: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
    paper_approved_count: int,
    discovered_count: int,
    deployments_created: int,
    failures: list[dict[str, Any]],
    test_results: dict[str, Any],
) -> TournamentReport:
    """Build a deterministic tournament report."""
    now = datetime.now(UTC).isoformat()

    return TournamentReport(
        tournament_id=tournament_id,
        generated_at=now,
        config=config,
        universe={
            "total_candidates": universe_total,
            "families": sorted(universe_families),
            "valid_count": backtested_count,
            "invalid_count": universe_total - backtested_count,
        },
        research={
            "backtested_count": backtested_count,
            "validated_count": validated_count,
            "failed_count": failed_count,
        },
        qualification={
            "qualified_count": qualified_count,
            "rejected_count": rejected_count,
            "top_20": top_20,
            "top_10": top_10,
            "final_n": final_n,
        },
        diversification={
            "clusters": clusters,
            "selected_families": [s.get("family", "") for s in final_n],
            "correlation_summary": "see clusters",
        },
        deployment={
            "paper_approved_count": paper_approved_count,
            "discovered_count": discovered_count,
            "deployments_created": deployments_created,
        },
        failures=failures,
        tests=test_results,
    )
