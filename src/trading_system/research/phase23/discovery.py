"""Autonomous strategy discovery (Phase 24).

Queries the registry for PAPER_APPROVED strategies without hardcoding names.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "DiscoveryResult",
    "Phase23Discovery",
]


@dataclass
class DiscoveryResult:
    strategy_id: str
    strategy_name: str
    candidate_id: str
    score: float
    symbol: str
    timeframe: str
    spec_hash: str
    is_deployable: bool = True
    discovery_errors: list[str] = None

    def __post_init__(self) -> None:
        if self.discovery_errors is None:
            self.discovery_errors = []


class Phase23Discovery:
    """Discovers PAPER_APPROVED and PAPER_EXPERIMENTAL strategies from the registry."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    def discover(self, max_candidates: int = 10, include_experimental: bool = True) -> list[DiscoveryResult]:
        """Return up to max_candidates paper-approved strategies.
        
        By default, includes both PAPER_APPROVED and PAPER_EXPERIMENTAL strategies.
        Set include_experimental=False to restrict to PAPER_APPROVED only.
        """
        seen: set[str] = set()
        results: list[DiscoveryResult] = []

        def _add(result: DiscoveryResult) -> None:
            if result.strategy_id in seen:
                return
            seen.add(result.strategy_id)
            results.append(result)

        try:
            approved = self._registry.get_paper_approved()
        except Exception:
            approved = []

        for strategy in approved:
            if len(results) >= max_candidates:
                break
            try:
                evidence_list = self._registry.list_evidence(strategy_id=strategy.strategy_id)
                tournament_evidence = None
                is_experimental = False
                for ev in evidence_list:
                    config = ev.configuration_json or {}
                    if config.get("qualification_status") == "PAPER_APPROVED":
                        tournament_evidence = config
                        break
                    if include_experimental and config.get("qualification_status") == "PAPER_EXPERIMENTAL":
                        tournament_evidence = config
                        is_experimental = True
                        break

                if tournament_evidence is None:
                    continue

                _add(DiscoveryResult(
                    strategy_id=strategy.strategy_id,
                    strategy_name=strategy.name or "",
                    candidate_id=tournament_evidence.get("candidate_id", ""),
                    score=float(tournament_evidence.get("score", 0.0)),
                    symbol=strategy.symbol or "",
                    timeframe=strategy.timeframe or "",
                    spec_hash=strategy.spec_hash or "",
                    is_deployable=not is_experimental,
                ))
            except Exception:
                continue

        if include_experimental:
            try:
                experimental = self._registry.get_paper_experimental()
            except Exception:
                experimental = []

            for strategy in experimental:
                if len(results) >= max_candidates:
                    break
                try:
                    evidence_list = self._registry.list_evidence(strategy_id=strategy.strategy_id)
                    tournament_evidence = None
                    for ev in evidence_list:
                        config = ev.configuration_json or {}
                        if config.get("qualification_status") == "PAPER_EXPERIMENTAL":
                            tournament_evidence = config
                            break

                    if tournament_evidence is None:
                        continue

                    _add(DiscoveryResult(
                        strategy_id=strategy.strategy_id,
                        strategy_name=strategy.name or "",
                        candidate_id=tournament_evidence.get("candidate_id", ""),
                        score=float(tournament_evidence.get("score", 0.0)),
                        symbol=strategy.symbol or "",
                        timeframe=strategy.timeframe or "",
                        spec_hash=strategy.spec_hash or "",
                        is_deployable=False,
                    ))
                except Exception:
                    continue

        return results
