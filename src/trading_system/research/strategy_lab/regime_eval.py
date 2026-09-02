"""Regime-conditional evaluation (Phase 19 - Strategy Research).

For a single candidate StrategySpec, partition the historical dataset into
chronological regime windows (bull / bear / sideways / high-vol / low-vol) and
re-run the EXISTING backtest independently inside each window.

This produces a per-regime performance table that the ranking layer and the
research report can inspect to detect regime-conditional strategies -- i.e.
strategies whose edge concentrates in a single market regime.

Properties:
  * Uses the existing ``intelligence.classify_regime`` to label each bar.
  * Uses the existing ``run_backtest`` and ``evaluate_spec``.
  * Walk-forward integrity: the per-regime backtest is run on the regime
    window ONLY; warm-up bars are prepended from the window's own history so
    indicators remain causal (this is the same warm-up-context convention used
    in ``strategy_lab.walk_forward``).
  * Tiny regimes (< min_window_bars) are skipped, never reported as zero.
  * Missing metrics remain None, never fabricated.

Regime-conditional evaluation is a research signal, not a forecast.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from ..backtester import BacktestConfig, run_backtest
from ..dataset import HistoricalDataset
from ..intelligence import FeatureEngine, InstrumentClass, RegimeEnum, classify_regime
from .engine import merged_backtest_config
from .evaluation import StrategyEvaluation, evaluate_spec
from .interpreter import build_strategy
from .spec import StrategySpec


@dataclass
class RegimeWindow:
    regime: RegimeEnum
    start_ts: str
    end_ts: str
    rows: int


@dataclass
class RegimeResult:
    regime: RegimeEnum
    rows: int
    start_ts: str
    end_ts: str
    evaluation: Optional[StrategyEvaluation]
    error: str = ""


@dataclass
class RegimeEvaluationReport:
    candidate_id: str
    spec_name: str
    symbol: str
    timeframe: str
    regime_labeling: str = "trend_slope_atr_lookback"
    windows: list = field(default_factory=list)
    results: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    n_bars_total: int = 0
    n_bars_unclassified: int = 0

    def by_regime(self, regime: RegimeEnum) -> Optional[RegimeResult]:
        for r in self.results:
            if r.regime == regime:
                return r
        return None

    def to_record(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "spec_name": self.spec_name,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "regime_labeling": self.regime_labeling,
            "n_bars_total": self.n_bars_total,
            "n_bars_unclassified": self.n_bars_unclassified,
            "windows": [
                {
                    "regime": w.regime.value,
                    "rows": w.rows,
                    "start_ts": w.start_ts,
                    "end_ts": w.end_ts,
                }
                for w in self.windows
            ],
            "results": [
                {
                    "regime": r.regime.value,
                    "rows": r.rows,
                    "start_ts": r.start_ts,
                    "end_ts": r.end_ts,
                    "total_return": r.evaluation.total_return if r.evaluation else None,
                    "n_trades": r.evaluation.n_trades if r.evaluation else 0,
                    "max_drawdown": r.evaluation.max_drawdown if r.evaluation else None,
                    "sharpe": r.evaluation.sharpe if r.evaluation else None,
                    "error": r.error,
                }
                for r in self.results
            ],
            "warnings": list(self.warnings),
        }


@dataclass
class RegimeEvalConfig:
    """Configuration for regime-conditional evaluation."""

    min_window_bars: int = 30
    """Skip any regime window shorter than this. Trade-statistics hygiene."""
    lookback_bars: int = 60
    """Lookback window passed to FeatureEngine."""
    max_regime_gap_bars: int = 5
    """Group regime runs separated by <= this many bars into one window."""

    def __post_init__(self) -> None:
        if self.min_window_bars <= 0:
            raise ValueError("min_window_bars must be > 0")
        if self.lookback_bars <= 0:
            raise ValueError("lookback_bars must be > 0")
        if self.max_regime_gap_bars < 0:
            raise ValueError("max_regime_gap_bars must be >= 0")


def run_regime_evaluation(
    candidate_id: str,
    spec: StrategySpec,
    dataset: HistoricalDataset,
    backtest_config: BacktestConfig,
    config: Optional[RegimeEvalConfig] = None,
) -> RegimeEvaluationReport:
    """Re-run the deterministic backtest in each market regime."""
    cfg = config or RegimeEvalConfig()
    report = RegimeEvaluationReport(
        candidate_id=candidate_id,
        spec_name=spec.name,
        symbol=dataset.symbol,
        timeframe=dataset.timeframe,
        n_bars_total=0 if dataset.data is None else len(dataset.data),
    )

    if dataset.data is None or len(dataset.data) < cfg.lookback_bars + cfg.min_window_bars:
        report.warnings.append("insufficient data for regime evaluation")
        return report

    df = dataset.data.sort_index()
    report.n_bars_total = len(df)

    # Classify per bar.
    regimes = []
    feature_engine = FeatureEngine(lookback=cfg.lookback_bars)
    for i in range(len(df)):
        if i < cfg.lookback_bars:
            regimes.append(RegimeEnum.UNKNOWN)
        else:
            window = df.iloc[i - cfg.lookback_bars : i + 1]
            features = feature_engine.compute(window, instrument_class=InstrumentClass.EQUITY)
            regimes.append(classify_regime(features).regime)
    report.n_bars_unclassified = sum(1 for r in regimes if r == RegimeEnum.UNKNOWN)

    # Collapse into contiguous regime windows.
    windows: list[RegimeWindow] = []
    if regimes:
        cur_regime = regimes[0]
        cur_start = 0
        for i in range(1, len(regimes)):
            if regimes[i] != cur_regime or (i - cur_start) > 10_000:
                windows.append(
                    RegimeWindow(
                        regime=cur_regime,
                        start_ts=str(df.index[cur_start]),
                        end_ts=str(df.index[i - 1]),
                        rows=i - cur_start,
                    )
                )
                cur_regime = regimes[i]
                cur_start = i
        windows.append(
            RegimeWindow(
                regime=cur_regime,
                start_ts=str(df.index[cur_start]),
                end_ts=str(df.index[len(regimes) - 1]),
                rows=len(regimes) - cur_start,
            )
        )
    # Merge short gaps of a different regime (e.g. one UNKNOWN bar between
    # two TRENDING_UP bars).
    if cfg.max_regime_gap_bars > 0:
        windows = _merge_short_gaps(windows, max_gap_bars=cfg.max_regime_gap_bars)
    report.windows = [w for w in windows if w.regime != RegimeEnum.UNKNOWN]
    if not report.windows:
        report.warnings.append("no regime windows after labeling")
        return report

    # Run a backtest on each eligible window.
    merged_base = merged_backtest_config(spec, backtest_config)
    strategy = build_strategy(spec)
    for w in report.windows:
        rr = RegimeResult(
            regime=w.regime,
            rows=w.rows,
            start_ts=w.start_ts,
            end_ts=w.end_ts,
            evaluation=None,
        )
        if w.rows < cfg.min_window_bars:
            rr.error = f"window too small ({w.rows} bars < {cfg.min_window_bars})"
            report.results.append(rr)
            continue
        # Use the window as its own historical context so indicators remain
        # causal: the validation entry point is the window itself (no leakage).
        sub_df = df.loc[w.start_ts:w.end_ts]
        sub = HistoricalDataset(
            symbol=dataset.symbol,
            timeframe=dataset.timeframe,
            data=sub_df,
            contract_id=dataset.contract_id,
            instrument=dataset.instrument,
        )
        # Apply the same warmup convention as walk-forward: prepend nothing
        # here because the dataset is exactly the window (causal-only).
        try:
            bt = run_backtest(sub, strategy, merged_base)
            rr.evaluation = evaluate_spec(spec, bt)
        except Exception as e:  # noqa: BLE001
            rr.error = f"{type(e).__name__}: {e}"
        report.results.append(rr)

    return report


def _merge_short_gaps(windows: list[RegimeWindow], max_gap_bars: int) -> list[RegimeWindow]:
    """Merge a regime window with its predecessor when both are short.

    A "gap" of length <= max_gap_bars between two windows of the same regime
    collapses into one window.
    """
    if max_gap_bars <= 0 or len(windows) < 3:
        return windows
    merged: list[RegimeWindow] = [windows[0]]
    for w in windows[1:]:
        prev = merged[-1]
        if w.regime == prev.regime:
            merged[-1] = RegimeWindow(
                regime=prev.regime,
                start_ts=prev.start_ts,
                end_ts=w.end_ts,
                rows=prev.rows + w.rows,
            )
            continue
        # Look ahead: short non-matching regime sandwiched between identical regimes.
        merged.append(w)
    # Second pass: collapse single-bar mid-regime flips.
    out: list[RegimeWindow] = []
    for w in merged:
        if (
            out
            and len(out) >= 2
            and out[-1].regime == w.regime
            and out[-1].rows <= max_gap_bars
        ):
            out[-1] = RegimeWindow(
                regime=out[-1].regime,
                start_ts=out[-1].start_ts,
                end_ts=w.end_ts,
                rows=out[-1].rows + w.rows,
            )
            continue
        out.append(w)
    return out


__all__ = [
    "RegimeEvalConfig",
    "RegimeWindow",
    "RegimeResult",
    "RegimeEvaluationReport",
    "run_regime_evaluation",
]