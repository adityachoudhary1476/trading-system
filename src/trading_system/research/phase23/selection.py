"""Diversity selection and correlation clustering (Phase 24).

Prevents selecting N strategies that are effectively the same by clustering
based on return correlation and selecting diversified representatives.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pandas as pd

from .scoring import ScorerInput

__all__ = [
    "ClusterResult",
    "DiversitySelector",
    "select_diversified",
]


@dataclass
class ClusterResult:
    cluster_id: str
    members: list[str]
    representative: str
    avg_correlation: float
    avg_score: float


class DiversitySelector:
    """Cluster strategies by return correlation and select diversified top-N."""

    def __init__(
        self,
        correlation_threshold: float = 0.7,
        min_cluster_size: int = 1,
    ) -> None:
        self.correlation_threshold = correlation_threshold
        self.min_cluster_size = min_cluster_size

    def cluster(
        self,
        candidates: dict[str, Any],
        returns: dict[str, pd.Series],
    ) -> list[ClusterResult]:
        """Cluster candidates by pairwise return correlation.

        Simple greedy clustering:
        1. Sort candidates by score descending
        2. For each unclustered candidate, start a new cluster
        3. Assign any unclustered candidate with correlation > threshold to existing cluster
        """
        if not candidates:
            return []

        # Sort by score descending (candidates is dict[str, float] or dict[str, ScorerInput])
        def get_score(cid: str) -> float:
            c = candidates[cid]
            if hasattr(c, "score"):
                return c.score
            if isinstance(c, dict):
                return c.get("score", 0.0)
            return 0.0

        sorted_ids = sorted(candidates.keys(), key=get_score, reverse=True)

        clusters: list[ClusterResult] = []
        assigned: set[str] = set()

        for cid in sorted_ids:
            if cid in assigned:
                continue
            cluster_members = [cid]
            assigned.add(cid)

            if cid in returns:
                for other_id in sorted_ids:
                    if other_id in assigned or other_id not in returns:
                        continue
                    corr = self._correlation(returns[cid], returns[other_id])
                    if corr is not None and corr >= self.correlation_threshold:
                        cluster_members.append(other_id)
                        assigned.add(other_id)

            avg_corr = self._avg_cluster_correlation(cluster_members, returns)
            avg_score = sum(get_score(m) for m in cluster_members) / len(cluster_members)
            rep = cluster_members[0]

            clusters.append(ClusterResult(
                cluster_id=f"cluster_{len(clusters) + 1}",
                members=cluster_members,
                representative=rep,
                avg_correlation=avg_corr,
                avg_score=avg_score,
            ))

        return clusters

    def select_diversified(
        self,
        candidates: dict[str, Any],
        returns: dict[str, pd.Series],
        top_n: int = 5,
    ) -> tuple[list[str], list[ClusterResult]]:
        """Select top-N diversified candidates from clusters.

        Strategy:
        1. Cluster all candidates
        2. Sort clusters by representative score descending
        3. Pick one representative per cluster until top_n is reached
        """
        clusters = self.cluster(candidates, returns)
        selected: list[str] = []

        def get_score(cid: str) -> float:
            c = candidates[cid]
            if hasattr(c, "score"):
                return c.score
            if isinstance(c, dict):
                return c.get("score", 0.0)
            return 0.0

        for cluster in sorted(clusters, key=lambda c: get_score(c.representative), reverse=True):
            if len(selected) >= top_n:
                break
            selected.append(cluster.representative)

        return selected, clusters

    def _correlation(self, a: pd.Series, b: pd.Series) -> float | None:
        try:
            aligned = pd.concat([a, b], axis=1).dropna()
            if len(aligned) < 5:
                return None
            corr = aligned.iloc[:, 0].corr(aligned.iloc[:, 1])
            if math.isfinite(corr):
                return max(-1.0, min(1.0, corr))
        except Exception:
            pass
        return None

    def _avg_cluster_correlation(self, members: list[str], returns: dict[str, pd.Series]) -> float:
        if len(members) < 2:
            return 0.0
        total = 0.0
        count = 0
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                corr = self._correlation(returns.get(members[i]), returns.get(members[j]))
                if corr is not None:
                    total += corr
                    count += 1
        return total / count if count else 0.0


def select_diversified(
    candidates: dict[str, ScorerInput],
    returns: dict[str, pd.Series],
    top_n: int = 5,
    correlation_threshold: float = 0.7,
) -> tuple[list[str], list[ClusterResult]]:
    """Convenience function for diversity selection."""
    selector = DiversitySelector(correlation_threshold=correlation_threshold)
    return selector.select_diversified(candidates, returns, top_n=top_n)
