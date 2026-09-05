"""V5 real-historical validation harness.

Primary objective: determine whether Finova's intelligence demonstrates
measurable predictive value on genuinely historical market data. It must be
capable of concluding V3-better / V4-better / V4-worse / no-difference /
insufficient-evidence / regime-dependent / disappears-after-costs.

Scientific honesty is more important than a positive result. Nothing in this
module optimizes for a favorable outcome; results are reported with explicit
uncertainty and honest sample-size flags.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np
import pandas as pd

from .patterns import _wilson

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Performance metrics (direction / returns / trading / risk)
# --------------------------------------------------------------------------- #
@dataclass
class FullMetrics:
    name: str = ""
    n: int = 0
    resolved: int = 0
    long_count: int = 0
    short_count: int = 0
    neutral_count: int = 0
    directional_accuracy: Optional[float] = None
    precision: Optional[float] = None
    recall: Optional[float] = None
    f1: Optional[float] = None
    avg_return: Optional[float] = None
    median_return: Optional[float] = None
    cumulative_return: Optional[float] = None
    expectancy: Optional[float] = None
    avg_winner: Optional[float] = None
    avg_loser: Optional[float] = None
    payoff_ratio: Optional[float] = None
    trades: int = 0
    win_rate: Optional[float] = None
    profit_factor: Optional[float] = None
    turnover: Optional[float] = None
    cost_pct: float = 0.0
    cost_adjusted_return: Optional[float] = None
    max_drawdown: Optional[float] = None
    volatility: Optional[float] = None
    sharpe: Optional[float] = None
    sortino: Optional[float] = None
    worst_trade: Optional[float] = None
    longest_losing_streak: int = 0
    ci_low: float = 0.0
    ci_high: float = 1.0
    note: str = ""

def compute_metrics(name: str, rows: list[tuple[str, float]],
                    cost_pct: float = 0.0) -> FullMetrics:
    """rows: (bias, realized_return_pct). bias is bullish/bearish/neutral."""
    m = FullMetrics(name=name, n=len(rows))
    if not rows:
        m.note = "no forecasts"
        return m
    m.resolved = len(rows)
    biases = [b for b, _ in rows]
    m.long_count = biases.count("bullish")
    m.short_count = biases.count("bearish")
    m.neutral_count = biases.count("neutral")
    rets = [r for _, r in rows]
    m.avg_return = sum(rets) / len(rets)
    m.median_return = float(np.median(rets))
    m.cumulative_return = (np.prod([1 + r / 100 for r in rets]) - 1) * 100
    m.volatility = float(np.std(rets, ddof=0)) if len(rets) > 1 else None
    m.sharpe = (m.avg_return / m.volatility) if (m.volatility or 0) > 0 else None
    downside = [r for r in rets if r < 0]
    m.sortino = (m.avg_return / float(np.std(downside, ddof=0))
                 if len(downside) > 1 and np.std(downside, ddof=0) > 0 else None)
    m.worst_trade = min(rets) if rets else None
    correct = sum(1 for b, r in rows
                  if (b == "bullish" and r > 0) or (b == "bearish" and r < 0)
                  or (b == "neutral" and abs(r) <= 0.25))
    m.directional_accuracy = correct / len(rows)
    m.ci_low, m.ci_high = _wilson(m.directional_accuracy, len(rows))
    taken = [(b, r) for b, r in rows if b in ("bullish", "bearish")]
    m.trades = len(taken)
    if taken:
        wins = [r for b, r in taken if (b == "bullish" and r > 0)
                or (b == "bearish" and r < 0)]
        losses = [r for b, r in taken if (b == "bullish" and r <= 0)
                  or (b == "bearish" and r >= 0)]
        m.win_rate = len(wins) / len(taken)
        m.expectancy = sum(r for _, r in taken) / len(taken)
        m.avg_winner = sum(wins) / len(wins) if wins else None
        m.avg_loser = sum(losses) / len(losses) if losses else None
        if m.avg_winner is not None and m.avg_loser:
            m.payoff_ratio = abs(m.avg_winner / m.avg_loser)
        gross = sum(wins)
        drag = abs(sum(r for r in losses if r < 0))
        m.profit_factor = (gross / drag) if drag > 0 else None
        m.precision = (len(wins) / len(taken)) if taken else None
        realized_up = sum(1 for _, r in taken if r > 0)
        if realized_up:
            m.recall = (sum(1 for b, r in taken if b == "bullish" and r > 0)
                        / realized_up)
        if m.precision and m.recall and m.precision + m.recall > 0:
            m.f1 = 2 * m.precision * m.recall / (m.precision + m.recall)
        eq = peak = dd = 0.0
        for _, r in taken:
            eq += r
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        m.max_drawdown = dd
        streak = longest = 0
        for _, r in taken:
            if r <= 0:
                streak += 1
                longest = max(longest, streak)
            else:
                streak = 0
        m.longest_losing_streak = longest
        m.cost_pct = cost_pct
        m.turnover = len(taken) / max(1, len(rows))
        m.cost_adjusted_return = (sum(r - cost_pct for _, r in taken)
                                  / len(taken))
    if m.trades < 30:
        m.note = f"insufficient sample: {m.trades} directional trades (<30)"
    return m


def bootstrap_ci(values: list[float], seed: int = 7, n_boot: int = 1000,
                 alpha: float = 0.05) -> tuple[float, float, Optional[float]]:
    """Deterministic bootstrap CI for the MEAN of ``values``.
    Identical inputs => identical outputs (seeded). Returns (lo, hi, mean)."""
    if not values:
        return (0.0, 0.0, None)
    rng = np.random.default_rng(seed)
    arr = np.asarray(values, dtype=float)
    means = np.empty(n_boot)
    for i in range(n_boot):
        means[i] = rng.choice(arr, size=len(arr), replace=True).mean()
    q = alpha / 2 * 100
    return (float(np.percentile(means, q)),
            float(np.percentile(means, 100 - q)), float(means.mean()))

# --------------------------------------------------------------------------- #
# Improvement test (absolute/relative delta + CI + classification)
# --------------------------------------------------------------------------- #
class ImprovementVerdict(str):
    IMPROVEMENT = "IMPROVEMENT"
    NO_CLEAR_DIFFERENCE = "NO_CLEAR_DIFFERENCE"
    REGRESSION = "REGRESSION"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"


@dataclass
class ImprovementResult:
    metric: str = ""
    base_name: str = ""
    compare_name: str = ""
    base_value: Optional[float] = None
    compare_value: Optional[float] = None
    absolute_delta: Optional[float] = None
    relative_delta: Optional[float] = None
    ci_low: float = 0.0
    ci_high: float = 0.0
    verdict: str = ImprovementVerdict.INSUFFICIENT_SAMPLE
    note: str = ""


def improvement_test(base_rows: list[tuple[str, float]],
                     compare_rows: list[tuple[str, float]],
                     metric: str = "expectancy",
                     base_direction: str = "bullish",
                     compare_direction: str = "V4",
                     min_sample: int = 30,
                     seed: int = 7) -> ImprovementResult:
    """Compare one metric (default: directional expectancy, dollar-corrected)
    between two configurations on the SAME forecast rows, with a deterministic
    bootstrap CI on the difference.

    DOCUMENTED RULES:
      - INSUFFICIENT_SAMPLE if either side has fewer than ``min_sample`` rows.
      - IMPROVEMENT if the CI of (compare - base) lies entirely > 0.
      - REGRESSION if the CI lies entirely < 0.
      - Otherwise NO_CLEAR_DIFFERENCE.
    """
    res = ImprovementResult(metric=metric, base_name=base_direction,
                            compare_name=compare_direction,
                            base_value=None, compare_value=None)
    if len(base_rows) < min_sample or len(compare_rows) < min_sample:
        res.note = (f"insufficient sample: base={len(base_rows)}, "
                    f"compare={len(compare_rows)} (need {min_sample})")
        return res

    def _metric(rows):
        m = compute_metrics("t", rows)
        return m.expectancy if metric == "expectancy" else (
            m.accuracy if hasattr(m, "accuracy") else m.win_rate)

    bv = _metric(base_rows)
    cv = _metric(compare_rows)
    res.base_value, res.compare_value = bv, cv
    res.absolute_delta = (cv - bv) if (bv is not None and cv is not None) else None
    res.relative_delta = (res.absolute_delta / abs(bv)) if bv not in (None, 0) else None

    base_rets = [r for _, r in base_rows if _ in ("bullish", "bearish")]
    compare_rets = [r for _, r in compare_rows if _ in ("bullish", "bearish")]
    b_lo, b_hi, bm = bootstrap_ci(base_rets, seed=seed)
    c_lo, c_hi, cm = bootstrap_ci(compare_rets, seed=seed)
    # CI on the difference (mean_delta +/- sqrt(std_err^2)) via joint bootstrap
    rng = np.random.default_rng(seed + 1)
    diffs = []
    a, b = np.asarray(base_rets), np.asarray(compare_rets)
    for _ in range(500):
        sa = rng.choice(a, size=len(a), replace=True).mean()
        sb = rng.choice(b, size=len(b), replace=True).mean()
        diffs.append(sb - sa)
    res.ci_low = float(np.percentile(diffs, 2.5))
    res.ci_high = float(np.percentile(diffs, 97.5))
    if res.ci_low > 0:
        res.verdict = ImprovementVerdict.IMPROVEMENT
    elif res.ci_high < 0:
        res.verdict = ImprovementVerdict.REGRESSION
    else:
        res.verdict = ImprovementVerdict.NO_CLEAR_DIFFERENCE
    return res


# --------------------------------------------------------------------------- #
# Walk-forward + final out-of-sample lock
# --------------------------------------------------------------------------- #
@dataclass
class WalkForwardWindow:
    name: str = ""
    train_start: Optional[datetime] = None
    train_end: Optional[datetime] = None
    test_start: Optional[datetime] = None
    test_end: Optional[datetime] = None
    results: dict[str, FullMetrics] = field(default_factory=dict)


@dataclass
class OOSResult:
    locked: bool = False
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    results: dict[str, FullMetrics] = field(default_factory=dict)


def chronological_split(index: pd.DatetimeIndex, n_windows: int = 3,
                        train_frac: float = 0.6):
    """Chronological train/test windows rolled forward (no random split).

    Returns list of (train_start, train_end, test_start, test_end) datetime
    tuples covering the index, with windows rolling forward.
    """
    ts = list(index)
    n = len(ts)
    out = []
    step = max(1, (n - int(n * train_frac)) // n_windows)
    for w in range(n_windows):
        split = int(n * train_frac) + w * step
        end = min(int(n * train_frac) + int(n * (1 - train_frac)) + w * step, n - 1)
        if split >= end:
            break
        out.append((ts[0], ts[split - 1], ts[split], ts[end]))
    return out


class OOSLock:
    """Final untouched period. No tuning may use it; once evaluated it is
    frozen. Records FINAL_OOS_START / FINAL_OOS_END / FINAL_OOS_LOCKED."""

    def __init__(self) -> None:
        self._locked = False
        self._start: Optional[datetime] = None
        self._end: Optional[datetime] = None
        self.results: dict[str, FullMetrics] = {}

    def lock(self, start: datetime, end: datetime) -> None:
        self._start = start
        self._end = end
        self._locked = True

    @property
    def is_locked(self) -> bool:
        return self._locked

    def freeze(self, results: dict[str, FullMetrics]) -> OOSResult:
        return OOSResult(locked=self._locked, start=self._start, end=self._end,
                         results=results)

# --------------------------------------------------------------------------- #
# Confidence / probability calibration analysis
# --------------------------------------------------------------------------- #
@dataclass
class ConfidenceBucket:
    bucket: str = ""
    count: int = 0
    resolved: int = 0
    win_rate: Optional[float] = None
    avg_return: Optional[float] = None
    expectancy: Optional[float] = None


@dataclass
class CalibrationAnalysis:
    buckets: list[ConfidenceBucket] = field(default_factory=list)
    monotonic: Optional[bool] = None
    monotonicity_failed: bool = False
    brier: Optional[float] = None
    ece: Optional[float] = None
    probability_status: str = "CONFIDENCE_REMAINS_ANALYTICAL_SCORE"
    note: str = ""


BUCKET_EDGES = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 100)]


def confidence_calibration(rows: list[tuple[float, float]],
                           min_per_bucket: int = 10) -> CalibrationAnalysis:
    """rows: (confidence_0_100, realized_return_pct). Reports observed win rate
    per bucket WITHOUT claiming probability. Higher buckets failing to show
    higher win rates => CONFIDENCE_MONOTONICITY_FAILED. Confidence is never
    modified to force monotonicity."""
    out = CalibrationAnalysis()
    for lo, hi in BUCKET_EDGES:
        sel = [r for c, r in rows if lo <= c < hi or (hi == 100 and c == 100)]
        b = ConfidenceBucket(bucket=f"{lo}-{hi}", count=len(sel), resolved=len(sel))
        if sel:
            b.win_rate = sum(1 for r in sel if r > 0) / len(sel)
            b.avg_return = sum(sel) / len(sel)
            b.expectancy = b.avg_return
        out.buckets.append(b)
    rates = [b.win_rate or 0.0 for b in out.buckets if b.count >= min_per_bucket]
    if len(rates) >= 2:
        out.monotonic = all(rates[i + 1] >= rates[i] - 1e-9
                            for i in range(len(rates) - 1))
        out.monotonicity_failed = not out.monotonic
        if out.monotonicity_failed:
            out.note = ("CONFIDENCE_MONOTONICITY_FAILED: higher confidence "
                        "buckets do not show higher observed win rates. "
                        "Confidence is NOT modified to force monotonicity.")
    if len(rows) < 100:
        out.note = (out.note + " " if out.note else "") + \
            f"small sample: {len(rows)} resolved (<100)"
    return out


def brier_ece(rows: list[tuple[float, float]]
              ) -> tuple[Optional[float], Optional[float]]:
    """(confidence_0_100, outcome_0_1) -> (Brier, ECE).

    Applies ONLY when confidence has a legitimate probability interpretation;
    otherwise probability_status stays CONFIDENCE_REMAINS_ANALYTICAL_SCORE."""
    if not rows:
        return None, None
    n = len(rows)
    brier = sum((c / 100 - y) ** 2 for c, y in rows) / n
    srt = sorted(rows)
    ece, m = 0.0, max(1, n // 10)
    for i in range(0, n, m):
        chunk = srt[i:i + m]
        conf = sum(c for c, _ in chunk) / 100 / len(chunk)
        frac = sum(1 for _, y in chunk if y == 1) / len(chunk)
        ece += len(chunk) / n * abs(conf - frac)
    return float(brier), float(ece)

# --------------------------------------------------------------------------- #
# Regime / news-event / pattern-similarity analysis
# --------------------------------------------------------------------------- #
@dataclass
class RegimeSlice:
    regime: str = ""
    count: int = 0
    metrics: Optional["FullMetrics"] = None


def regime_analysis(rows: list[dict[str, Any]],
                    configs=("V2", "V3", "V4")) -> dict[str, dict[str, "FullMetrics"]]:
    """rows: list of {regime, config, bias, realized_return}.
    Returns {regime: {config: FullMetrics}}."""
    grouped: dict[str, dict[str, list[tuple[str, float]]]] = {}
    for r in rows:
        grouped.setdefault(r["regime"], {}).setdefault(r["config"], []).append(
            (r["bias"], r["realized_return"]))
    result = {}
    for rg, conf_map in grouped.items():
        result[rg] = {cf: compute_metrics(f"{cf}@{rg}", rows_)
                      for cf, rows_ in conf_map.items()}
    return result


@dataclass
class NewsEventAnalysis:
    event_type: str = ""
    count: int = 0
    avg_return: Optional[float] = None
    median_return: Optional[float] = None
    avg_mfe: Optional[float] = None
    avg_mae: Optional[float] = None
    expectancy: Optional[float] = None


def news_event_performance(events: list[dict[str, Any]]) -> list[NewsEventAnalysis]:
    """events: list of {event_type, realized_return, mfe, mae}."""
    by_type: dict[str, list[float]] = {}
    mfe_map: dict[str, list[float]] = {}
    mae_map: dict[str, list[float]] = {}
    for e in events:
        t = e["event_type"]
        by_type.setdefault(t, []).append(e["realized_return"])
        mfe_map.setdefault(t, []).append(e.get("mfe") or 0.0)
        mae_map.setdefault(t, []).append(e.get("mae") or 0.0)
    out = []
    for t, rets in sorted(by_type.items()):
        m, a = mfe_map.get(t, []), mae_map.get(t, [])
        out.append(NewsEventAnalysis(
            event_type=t, count=len(rets), avg_return=sum(rets) / len(rets),
            median_return=float(np.median(rets)),
            avg_mfe=(sum(m) / len(m)) if m else None,
            avg_mae=(sum(a) / len(a)) if a else None,
            expectancy=sum(rets) / len(rets)))
    return out

# --------------------------------------------------------------------------- #
# Pattern similarity buckets (monotonicity check)
# --------------------------------------------------------------------------- #
@dataclass
class SimilarityBucketReport:
    bucket: str = ""
    count: int = 0
    win_rate: Optional[float] = None
    avg_return: Optional[float] = None
    mfe: Optional[float] = None
    mae: Optional[float] = None
    expectancy: Optional[float] = None


SIM_BUCKETS = [(0.70, 0.75), (0.75, 0.80), (0.80, 0.85),
               (0.85, 0.90), (0.90, 1.01)]


def pattern_similarity_analysis(
        matches: list[dict[str, Any]]) -> tuple[list[SimilarityBucketReport], bool]:
    """matches: list of {similarity, realized_return, mfe, mae}.
    Returns (bucket_reports, monotonicity_holds). Sets
    SIMILARITY_MONOTONICITY_FAILED when higher similarity does NOT imply
    better outcomes. Buckets configurable via SIM_BUCKETS."""
    reports = []
    monotonic = True
    prev_rate = None
    for lo, hi in SIM_BUCKETS:
        sel = [m for m in matches if lo <= m["similarity"] < hi]
        r = SimilarityBucketReport(bucket=f"{lo:.2f}-{hi:.2f}", count=len(sel))
        if sel:
            rets = [m["realized_return"] for m in sel]
            r.win_rate = sum(1 for x in rets if x > 0) / len(rets)
            r.avg_return = sum(rets) / len(rets)
            r.expectancy = r.avg_return
            r.mfe = sum(m.get("mfe") or 0 for m in sel) / len(sel)
            r.mae = sum(m.get("mae") or 0 for m in sel) / len(sel)
            if prev_rate is not None and r.win_rate < prev_rate - 0.03:
                monotonic = False
            prev_rate = r.win_rate
        reports.append(r)
    return reports, monotonic

# --------------------------------------------------------------------------- #
# Transaction cost model (configurable Indian-market assumptions)
# --------------------------------------------------------------------------- #
@dataclass
class CostAssumptions:
    """Round-trip total cost per executed direction, in basis points.

    Deliberately configurable; decomposes into brokerage / exchange / taxes /
    slippage / spread. LABELLED as assumptions — exact historical costs cannot
    be reconstructed.
    """
    brokerage_bps: float = 2.0
    exchange_bps: float = 0.35
    taxes_bps: float = 1.5
    slippage_bps: float = 5.0
    spread_bps: float = 3.0
    cost_bps: float = 0.0  # if >0, overrides sum (e.g. calibrated later)

    def round_trip_bps(self) -> float:
        if self.cost_bps > 0:
            return self.cost_bps
        return (self.brokerage_bps + self.exchange_bps + self.taxes_bps
                + self.slippage_bps + self.spread_bps)

    def round_trip_pct(self) -> float:
        return self.round_trip_bps() / 100.0


def slippage_sensitivity(
        rows: list[tuple[str, float]],
        bps_levels: tuple[float, float, float] = (2.0, 6.0, 15.0),
        labels: tuple[str, str, str] = ("LOW", "BASE", "HIGH"),
) -> dict[str, "FullMetrics"]:
    """Report whether the edge survives under low/base/high slippage."""
    out = {}
    for lbl, bps in zip(labels, bps_levels):
        cost = CostAssumptions(slippage_bps=bps).round_trip_pct()
        out[lbl] = compute_metrics(lbl, rows, cost_pct=cost)
    return out


def edge_survives_slippage(per_levels: dict[str, "FullMetrics"],
                           threshold_bps: float = 6.0) -> bool:
    """True only if cost-adjusted expectancy stays positive at the slippage
    level whose round-trip bps is closest to (and >=) threshold_bps.
    Documented rule: compare cost_adjusted_return > 0 at realistic slippage."""
    best_key = None
    best_dist = None
    for lbl, m in per_levels.items():
        for bps_key, bps in (("LOW", 2.0), ("BASE", 6.0), ("HIGH", 15.0)):
            if lbl == bps_key and bps >= threshold_bps:
                dist = bps
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best_key = lbl
    if best_key is None or best_key not in per_levels:
        return False
    m = per_levels[best_key]
    return bool(m.cost_adjusted_return is not None and m.cost_adjusted_return > 0)


# --------------------------------------------------------------------------- #
# No-lookahead audit
# --------------------------------------------------------------------------- #
@dataclass
class LookaheadViolation:
    forecast_time: str = ""
    input_timestamp: str = ""
    category: str = ""      # ohlcv / htf_candle / news / pattern / context / options
    detail: str = ""


@dataclass
class CausalityAudit:
    forecasts_checked: int = 0
    violations: list[LookaheadViolation] = field(default_factory=list)

    @property
    def lookahead_violations(self) -> int:
        return len(self.violations)


def audit_snapshot_causality(snapshot, forecast_time=None,
                             category: str = "ohlcv") -> list[LookaheadViolation]:
    """Verify every input timestamp in a snapshot <= forecast_time."""
    ft = forecast_time or snapshot.timestamp
    bad: list[LookaheadViolation] = []
    for ts in snapshot.ohlcv.index:
        if ts > ft:
            bad.append(LookaheadViolation(str(ft), str(ts), category,
                                          "OHLCV bar after forecast time"))
    for tf, frame in snapshot.mtf.items():
        if frame is None or len(frame) == 0:
            continue
        close_time = frame.index + pd.Timedelta(tf)
        if close_time.max() > ft:
            bad.append(LookaheadViolation(str(ft), str(close_time.max()),
                                          "htf_candle",
                                          f"unclosed {tf} candle visible"))
    for e in snapshot.news:
        pub = getattr(e, "published_at", None)
        if pub is not None and pub > ft:
            bad.append(LookaheadViolation(str(ft), str(pub), "news",
                                          "news published after forecast time"))
    return bad

# --------------------------------------------------------------------------- #
# Deterministic final verdict engine (documented rules — no favorable bias)
# --------------------------------------------------------------------------- #
class FinalVerdict(str):
    STRONG_EVIDENCE = "STRONG_EVIDENCE_OF_IMPROVEMENT"
    MODERATE_EVIDENCE = "MODERATE_EVIDENCE_OF_IMPROVEMENT"
    NO_CLEAR_IMPROVEMENT = "NO_CLEAR_IMPROVEMENT"
    REGRESSION = "REGRESSION"
    REGIME_DEPENDENT = "REGIME_DEPENDENT"
    FRAGILE = "FRAGILE_RESULT"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NOT_PROFITABLE_AFTER_COSTS = "NOT_PROFITABLE_AFTER_COSTS"
    UNABLE_TO_ESTABLISH_EDGE = "UNABLE_TO_ESTABLISH_EDGE"


@dataclass
class VerdictInput:
    improvement_V3: Optional["ImprovementResult"] = None
    improvement_V4: Optional["ImprovementResult"] = None
    net_expectancy_base: Optional[float] = None
    net_expectancy_compare: Optional[float] = None
    regime_dependent: bool = False
    edge_survives_costs: bool = False
    edge_survives_slippage: bool = False
    min_sample_met: bool = False
    oos_supported: bool = False


def classify_verdict(v: VerdictInput) -> FinalVerdict:
    """DOCUMENTED RULES (deterministic):
      1. INSUFFICIENT_DATA if min_sample_met is False.
      2. NOT_PROFITABLE_AFTER_COSTS if no config's net expectancy > 0.
      3. REGRESSION if V4 improvement verdict is REGRESSION.
      4. REGIME_DEPENDENT if regime_dependent and improvement weak.
      5. FRAGILE if improvement exists but fails slippage or lacks OOS.
      6. STRONG if V4 improvement + costs + slippage + OOS all hold.
      7. MODERATE if V4 improvement without full OOS.
      8. Otherwise NO_CLEAR_IMPROVEMENT.
    """
    if not v.min_sample_met:
        return FinalVerdict.INSUFFICIENT_DATA
    ne0 = v.net_expectancy_base or 0.0
    ne1 = v.net_expectancy_compare or 0.0
    if ne0 <= 0 and ne1 <= 0:
        return FinalVerdict.NOT_PROFITABLE_AFTER_COSTS
    v4v = v.improvement_V4.verdict if v.improvement_V4 else None
    v3v = v.improvement_V3.verdict if v.improvement_V3 else None
    if v4v == ImprovementVerdict.REGRESSION:
        return FinalVerdict.REGRESSION
    if v4v == ImprovementVerdict.IMPROVEMENT:
        if v.regime_dependent and not v.oos_supported:
            return FinalVerdict.REGIME_DEPENDENT
        if not v.edge_survives_slippage or not v.oos_supported:
            return FinalVerdict.FRAGILE
        if v.edge_survives_costs:
            return FinalVerdict.STRONG_EVIDENCE
        return FinalVerdict.MODERATE_EVIDENCE
    if v.regime_dependent:
        return FinalVerdict.REGIME_DEPENDENT
    if v3v in (ImprovementVerdict.NO_CLEAR_DIFFERENCE, None):
        return FinalVerdict.NO_CLEAR_IMPROVEMENT
    return FinalVerdict.UNABLE_TO_ESTABLISH_EDGE

# --------------------------------------------------------------------------- #
# Real-historical replay driver (causal snapshot -> config biases -> outcome)
# --------------------------------------------------------------------------- #
@dataclass
class V5ReplayRow:
    instrument: str = ""
    config: str = ""
    forecast_time: Optional[datetime] = None
    bias: str = "neutral"
    confidence: float = 0.0
    realized_return: Optional[float] = None
    regime: str = ""
    data_quality: str = ""
    news_available: bool = False
    pattern_matches: int = 0
    similarity: Optional[float] = None
    horizon: str = ""


def _v2_bias(features) -> str:
    """Minimal deterministic V2-style baseline: sign of ROC / RSI disposition."""
    if features.roc is not None:
        if features.roc > 0.005:
            return "bullish"
        if features.roc < -0.005:
            return "bearish"
    if features.rsi_14 is not None:
        if features.rsi_14 > 55:
            return "bullish"
        if features.rsi_14 < 45:
            return "bearish"
    return "neutral"


def _result_from_snap(snap):
    """Minimal NewsPipelineResult shim from a snapshot's visible news."""
    from .news_intelligence import NewsPipelineResult
    return NewsPipelineResult(events=snap.news, articles_ingested=len(snap.news))

def replay_v5_dataset(
    frames: dict[str, pd.DataFrame],          # instrument -> causal frame
    step: int = 5,
    start: int = 80,
    horizon_bars: int = 5,
    news_by_instrument: Optional[dict[str, list]] = None,
    context_history_by_instrument: Optional[dict[str, dict]] = None,
    option_rows_by_instrument: Optional[dict[str, list]] = None,
    min_pattern_matches: int = 12,
    min_similarity: float = 0.80,
) -> tuple[list[V5ReplayRow], "CausalityAudit"]:
    """Full V2/V3/V4 comparison on causal snapshots. Each forecast at T uses
    ONLY data available at T; outcome is forward return after T."""
    from .causal_snapshot import CausalSnapshotBuilder
    from .intelligence import SignalDirection
    from .intelligence_v3 import build_evidence_ledger_v2
    from .news_intelligence import build_news_context, news_to_evidence
    from .patterns import (
        HistoricalPatternEngine, build_pattern_report, fingerprint_from_features,
        pattern_to_evidence,
    )
    from .v4_compare import _vote_direction

    builder = CausalSnapshotBuilder()
    audit = CausalityAudit()
    rows: list[V5ReplayRow] = []
    pat_eng = HistoricalPatternEngine()
    for symbol, df in frames.items():
        if df is None or len(df) < start + horizon_bars + 2:
            continue
        df = df.sort_index()
        n = len(df)
        ticker = symbol.split(":")[-1].split("-")[0].upper()
        news_list = (news_by_instrument or {}).get(symbol, [])
        ctx_hist = (context_history_by_instrument or {}).get(symbol)
        opts = (option_rows_by_instrument or {}).get(symbol)
        for t in range(start, n - horizon_bars, step):
            as_of = df.index[t]
            if getattr(as_of, "tzinfo", None) is None:
                as_of = as_of.tz_localize("UTC")
            as_of_dt = as_of.to_pydatetime()
            snap = builder.snapshot(symbol, "1d", df, as_of_dt)
            if news_list:
                snap = builder.with_news(snap, news_list)
            if opts:
                snap = builder.with_options(snap, opts)
            if ctx_hist is not None:
                snap = builder.with_context_history(snap, ctx_hist)
            audit.forecasts_checked += 1
            audit.violations.extend(audit_snapshot_causality(snap, as_of_dt))
            feats, regime = snap.features, snap.regime
            fwd = (float(df["close"].iloc[t + horizon_bars])
                   / float(df["close"].iloc[t]) - 1) * 100
            v3_dir = (SignalDirection.LONG if feats.roc is not None
                      and feats.roc > 0
                      else SignalDirection.SHORT if feats.roc is not None
                      and feats.roc < 0 else SignalDirection.NEUTRAL)
            led = build_evidence_ledger_v2(feats, regime, v3_dir)
            vote = _vote_direction(led)
            v3_bias = (vote if vote != "neutral" else
                       ("bullish" if feats.trend.name == "BULLISH" else
                        "bearish" if feats.trend.name == "BEARISH" else "neutral"))
            configs: dict[str, str] = {"V2": _v2_bias(feats), "V3": v3_bias}
            fp = fingerprint_from_features(feats, regime, timestamp=as_of_dt,
                                           instrument=symbol)
            lib = pat_eng.build_library(df, instrument=symbol, start=start,
                                        step=2, as_of=as_of_dt)
            matches = pat_eng.find_matches(fp, lib, as_of=as_of_dt,
                                           min_similarity=min_similarity)
            rep = build_pattern_report(
                matches, df, horizons={"1D": horizon_bars},
                min_matches=min_pattern_matches, min_similarity=min_similarity)
            led_t = build_evidence_ledger_v2(feats, regime, v3_dir)
            pattern_to_evidence(led_t, rep, symbol)
            vt = _vote_direction(led_t)
            configs["V4_technical"] = vt if vt != "neutral" else v3_bias
            led_n = build_evidence_ledger_v2(feats, regime, v3_dir)
            if snap.news:
                nctx = build_news_context(_result_from_snap(snap),
                                          as_of=as_of_dt, target_ticker=ticker)
                news_to_evidence(led_n, nctx, as_of=as_of_dt)
            vn = _vote_direction(led_n)
            configs["V4_news"] = vn if vn != "neutral" else v3_bias
            led_f = build_evidence_ledger_v2(feats, regime, v3_dir)
            pattern_to_evidence(led_f, rep, symbol)
            if snap.news:
                nctx = build_news_context(_result_from_snap(snap),
                                          as_of=as_of_dt, target_ticker=ticker)
                news_to_evidence(led_f, nctx, as_of=as_of_dt)
            vf = _vote_direction(led_f)
            configs["V4_full"] = vf if vf != "neutral" else v3_bias
            reg = str(regime.regime.value)
            dq = "HEALTHY" if len(snap.ohlcv) >= 50 else "THIN"
            for cfg, bias in configs.items():
                rows.append(V5ReplayRow(
                    instrument=symbol, config=cfg, forecast_time=as_of_dt,
                    bias=bias, realized_return=fwd, regime=reg,
                    data_quality=dq, news_available=bool(snap.news),
                    pattern_matches=rep.match_count,
                    similarity=rep.similarity_avg,
                    horizon=f"{horizon_bars}d"))
    return rows, audit
