"""Deterministic, configurable strategy ranking (Phase 13, Step 10).

Ranking is a weighted composite over NORMALIZED metrics that the evaluation
actually provides — deliberately NOT a single-metric sort by total return.

DEFAULT FORMULA (documented, deterministic):
    score = 0.30 * norm_return
          + 0.20 * norm_drawdown
          + 0.20 * norm_risk_adjusted
          + 0.15 * win_rate
          + 0.10 * norm_profit_factor
          + 0.05 * norm_trade_count

where each component is squashed to [-1, 1] (or [0, 1]) with fixed caps:
    norm_return        = clip(total_return / return_cap, -1, 1)
    norm_drawdown      = 1 - min(1, |max_drawdown| / drawdown_cap)   (1 = no dd)
    norm_risk_adjusted = clip(sharpe / sharpe_cap, -1, 1)  (0 when unavailable)
    win_rate           = as reported (0 when unavailable)
    norm_profit_factor = clip(profit_factor / profit_factor_cap, 0, 1)
    norm_trade_count   = min(1, n_trades / trade_count_target)  (rewards sample size)

Unavailable metrics contribute 0 (they never fabricate signal). Weights are
configurable; unknown metric keys and negative weights are rejected. Ties are
broken by candidate key (alphabetical) so the ordering is fully deterministic.

This ranking is a research triage tool. It does NOT predict future returns and
must not be described as doing so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .evaluation import StrategyEvaluation

__all__ = ["RankingConfig", "CandidateScore", "rank_candidates"]

ALLOWED_METRICS = (
    "total_return",
    "max_drawdown",
    "risk_adjusted",
    "win_rate",
    "profit_factor",
    "trade_count",
)

_DEFAULT_WEIGHTS = {
    "total_return": 0.30,
    "max_drawdown": 0.20,
    "risk_adjusted": 0.20,
    "win_rate": 0.15,
    "profit_factor": 0.10,
    "trade_count": 0.05,
}


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


@dataclass
class RankingConfig:
    weights: dict = field(default_factory=lambda: dict(_DEFAULT_WEIGHTS))
    return_cap: float = 1.0        # +100% return saturates the return component
    drawdown_cap: float = 0.5      # -50% drawdown zeroes the drawdown component
    sharpe_cap: float = 3.0
    profit_factor_cap: float = 3.0
    trade_count_target: int = 20

    def __post_init__(self) -> None:
        unknown = set(self.weights) - set(ALLOWED_METRICS)
        if unknown:
            raise ValueError(
                f"unknown ranking metric(s) {sorted(unknown)}; allowed: {list(ALLOWED_METRICS)}"
            )
        if not self.weights:
            raise ValueError("ranking weights must not be empty")
        negative = {k: v for k, v in self.weights.items() if v < 0}
        if negative:
            raise ValueError(f"ranking weights must be non-negative: {negative}")
        total = sum(self.weights.values())
        if total <= 0:
            raise ValueError("ranking weights must sum to a positive value")
        if self.return_cap <= 0 or self.drawdown_cap <= 0:
            raise ValueError("return_cap and drawdown_cap must be positive")
        if self.trade_count_target <= 0:
            raise ValueError("trade_count_target must be positive")

    def normalized_score(self, evaluation: StrategyEvaluation) -> tuple:
        """(total score, components dict). Pure function of the evaluation."""
        comps: dict = {}
        for metric in ALLOWED_METRICS:
            weight = self.weights.get(metric, 0.0)
            comps[metric] = weight * self._component(metric, evaluation)
        return sum(comps.values()), comps

    def _component(self, metric: str, ev: StrategyEvaluation) -> float:
        if metric == "total_return":
            return _clip(ev.total_return / self.return_cap, -1.0, 1.0)
        if metric == "max_drawdown":
            return 1.0 - min(1.0, abs(ev.max_drawdown) / self.drawdown_cap)
        if metric == "risk_adjusted":
            if ev.sharpe is None:
                return 0.0
            return _clip(ev.sharpe / self.sharpe_cap, -1.0, 1.0)
        if metric == "win_rate":
            return ev.win_rate if ev.win_rate is not None else 0.0
        if metric == "profit_factor":
            if ev.profit_factor is None:
                return 0.0
            return _clip(ev.profit_factor / self.profit_factor_cap, 0.0, 1.0)
        if metric == "trade_count":
            return min(1.0, ev.n_trades / self.trade_count_target)
        raise KeyError(metric)  # pragma: no cover - guarded by __post_init__


@dataclass
class CandidateScore:
    key: str
    score: float
    components: dict


def rank_candidates(
    evaluations: dict,
    config: Optional[RankingConfig] = None,
) -> list:
    """Rank {key: StrategyEvaluation} deterministically (best first).

    Ties break alphabetically by key, so the result is stable across runs.
    """
    cfg = config or RankingConfig()
    scored = []
    for key in sorted(evaluations):
        score, comps = cfg.normalized_score(evaluations[key])
        scored.append(CandidateScore(key=key, score=float(score), components=comps))
    scored.sort(key=lambda c: (-c.score, c.key))
    return scored
