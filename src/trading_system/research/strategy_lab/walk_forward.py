"""Chronological walk-forward validation for StrategySpec candidates (Phase 14).

Central question this module answers:

    "Does this strategy continue to perform across multiple unseen chronological
     periods, rather than only performing well on the historical data used to
     discover it?"

Guarantees:
  * CHRONOLOGY IS NEVER VIOLATED. Folds are deterministic positional slices of
    the sorted dataset. A fold's VALIDATION window is strictly after its TRAIN
    window; nothing in a validation window can influence candidate generation,
    selection, or ranking for that same fold.
  * NO RANDOM SHUFFLING. Financial time series stay chronological.
  * WARM-UP IS CONTEXT-ONLY. Each fold's validation backtest is run on
    ``[validation_start - warmup_bars : validation_end]`` using the EXISTING
    backtester's ``warmup_bars`` so indicators get historical continuity while
    equity/return/drawdown are measured ONLY from the validation window.
    Per-trade statistics additionally filter the trade ledger to trades entered
    at/after the validation boundary, so warm-up-entered trades never count as
    validation trades. The fold's ``validation_total_return`` marks any
    position bridged from the warm-up context to market at the validation
    boundary (the existing backtester's equity is realized-P&L based while a
    position is held), so a validation-period return never includes warm-up
    context price action.
  * EXISTING BACKTESTER REUSE. No second backtesting engine is introduced.
  * DETERMINISTIC. Identical dataset + spec + configs => identical report.

Walk-forward validation is historical evidence of robustness, NOT a guarantee
of future profitability.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from ..backtester import BacktestConfig, BacktestResult, run_backtest
from ..dataset import HistoricalDataset
from .evaluation import StrategyEvaluation, evaluate_spec
from .engine import StrategyResearchEngine, merged_backtest_config
from .interpreter import build_strategy
from .providers import GenerationContext
from .spec import SpecStatus, StrategySpec
from .validation import required_warmup_bars, validate_spec

__all__ = [
    "WalkForwardConfig",
    "Fold",
    "generate_folds",
    "validate_fold",
    "FoldResult",
    "WalkForwardSummary",
    "compute_walk_forward_summary",
    "collect_warnings",
    "WalkForwardReport",
    "walk_forward_validate",
    "walk_forward_research",
]

# Documented, fixed scale for the return-dispersion component of the
# consistency score. See compute_walk_forward_summary.
DISPERSION_SCALE = 0.5


@dataclass
class WalkForwardConfig:
    """Strongly typed walk-forward configuration (Phase 14, Step 2).

    All window sizes are in BARS of the (chronological) dataset.

    Mode:
      * "rolling"   — the training window is the fixed ``train_window`` bars
                      immediately preceding each validation window.
      * "expanding" — the training window grows from the dataset start; the
                      first fold's training length is ``min_train_bars``.

    Windows:
      * ``n_folds`` validation folds, each of ``validation_window`` bars.
      * Consecutive validation windows start ``step_size`` bars apart.
      * ``allow_overlap=False`` (default) requires ``step_size >=
        validation_window`` so no validation period is evaluated twice.
      * Before each validation window, ``warmup_bars`` of historical context
        are prepended for causal indicator continuity. Only the validation
        window itself contributes to validation metrics.
      * ``min_fold_coverage`` is the minimum fraction of folds that must yield
        a measurable validation result before the run is considered usable.
    """

    mode: str = "rolling"
    n_folds: int = 5
    validation_window: int = 60
    train_window: int = 120          # rolling mode: fixed train length
    step_size: int = 60              # bars between validation starts
    allow_overlap: bool = False
    min_train_bars: int = 60         # expanding mode: initial train length
    min_validation_bars: int = 20
    min_validation_trades: int = 3
    warmup_bars: int = 50            # causal context prepended to validation
    min_fold_coverage: float = 0.6

    def __post_init__(self) -> None:
        problems = self.validate()
        if problems:
            raise ValueError("WalkForwardConfig is invalid: " + "; ".join(problems))

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.mode not in ("rolling", "expanding"):
            problems.append(f"mode must be 'rolling' or 'expanding', got {self.mode!r}")
        if self.n_folds < 1:
            problems.append("n_folds must be >= 1")
        if self.validation_window <= 0:
            problems.append("validation_window must be > 0")
        if self.train_window <= 0:
            problems.append("train_window must be > 0")
        if self.step_size <= 0:
            problems.append("step_size must be > 0")
        if self.warmup_bars < 0:
            problems.append("warmup_bars must be >= 0")
        if not 0 < self.min_train_bars <= self.train_window:
            problems.append(
                "min_train_bars must be in (0, train_window] "
                f"(got {self.min_train_bars} with train_window {self.train_window})"
            )
        if not 0 < self.min_validation_bars <= self.validation_window:
            problems.append(
                "min_validation_bars must be in (0, validation_window] "
                f"(got {self.min_validation_bars} with "
                f"validation_window {self.validation_window})"
            )
        if self.min_validation_trades < 0:
            problems.append("min_validation_trades must be >= 0")
        if not 0.0 < self.min_fold_coverage <= 1.0:
            problems.append("min_fold_coverage must be in (0, 1]")
        if self.step_size < self.validation_window and not self.allow_overlap:
            problems.append(
                "step_size must be >= validation_window unless allow_overlap=True; "
                "overlapping validation periods are disallowed by default"
            )
        return problems


@dataclass
class Fold:
    """One chronological train -> validation fold (Phase 14, Step 3).

    Boundary indices refer to positions in the ORIGINAL sorted dataset
    (0-based, right-exclusive):
        train_start <= train_end == validation_start < validation_end

    ``train_end`` is always == ``validation_start`` (the training window ends
    exactly where the validation window begins, so nothing in the validation
    window exists before selection).

    ``validation_run_dataset`` = ``data[validation_start - warmup_bars :
    validation_end]`` — the causal context + validation window used for the
    validation backtest (with ``warmup_bars`` set on the BacktestConfig so the
    existing backtester measures equity only from the validation boundary).
    ``warmup_bars`` records the number of context bars prepended.
    """

    fold_id: int
    train_start: int
    train_end: int
    validation_start: int
    validation_end: int
    warmup_bars: int
    train_dataset: HistoricalDataset
    validation_run_dataset: HistoricalDataset


def _make_dataset(
    base: HistoricalDataset,
    rows: pd.DataFrame,
) -> HistoricalDataset:
    return HistoricalDataset(
        symbol=base.symbol,
        timeframe=base.timeframe,
        data=rows,
        contract_id=base.contract_id,
        instrument=base.instrument,
    )


def generate_folds(dataset: HistoricalDataset, config: WalkForwardConfig) -> list[Fold]:
    """Deterministically generate chronological folds. Raises on impossible input.

    Raises
    ------
    ValueError
        When the dataset cannot supply the requested number of full folds, or
        when the configured warm-up context cannot be provided before the first
        validation window.
    """
    if dataset is None or dataset.data is None or len(dataset.data) < 2:
        raise ValueError("dataset is too small to generate walk-forward folds")
    df = dataset.data.sort_index()
    n = len(df)
    first_val = config.train_window if config.mode == "rolling" else config.min_train_bars
    if first_val < config.warmup_bars:
        raise ValueError(
            f"the first validation window starts at bar {first_val}, which cannot "
            f"provide the configured {config.warmup_bars} warm-up context bars"
        )
    needed = first_val + (config.n_folds - 1) * config.step_size + config.validation_window
    if needed > n:
        raise ValueError(
            f"insufficient data: {n} bars cannot fit {config.n_folds} folds "
            f"of {config.validation_window} validation bars starting at bar "
            f"{first_val} with step {config.step_size} "
            f"(would need at least {needed} bars)"
        )

    folds: list[Fold] = []
    for i in range(config.n_folds):
        val_start = first_val + i * config.step_size
        val_end = val_start + config.validation_window
        if config.mode == "rolling":
            train_start = val_start - config.train_window
        else:
            train_start = 0
        train_end = val_start
        run_start = val_start - config.warmup_bars
        assert run_start >= 0  # guaranteed by the checks above

        train_rows = df.iloc[train_start:train_end]
        run_rows = df.iloc[run_start:val_end]
        folds.append(
            Fold(
                fold_id=i,
                train_start=train_start,
                train_end=train_end,
                validation_start=val_start,
                validation_end=val_end,
                warmup_bars=config.warmup_bars,
                train_dataset=_make_dataset(dataset, train_rows),
                validation_run_dataset=_make_dataset(dataset, run_rows),
            )
        )
    return folds


def validate_fold(fold: Fold, config: WalkForwardConfig) -> list[str]:
    """Fold-integrity checks (Phase 14, Step 4). Returns a list of problems.

    Called internally by the evaluator before each fold; also useful directly in
    tests. An empty list means the fold is chronologically sound.
    """
    problems: list[str] = []
    if not fold.train_start <= fold.train_end:
        problems.append(f"fold {fold.fold_id}: train_start > train_end")
    if not fold.validation_start <= fold.validation_end:
        problems.append(f"fold {fold.fold_id}: validation_start > validation_end")
    if fold.train_end != fold.validation_start:
        problems.append(
            f"fold {fold.fold_id}: train_end {fold.train_end} != "
            f"validation_start {fold.validation_start} (chronology/leakage breach)"
        )
    if fold.validation_start < fold.train_end:
        problems.append(f"fold {fold.fold_id}: validation overlaps training data")
    exp_train_len = fold.train_end - fold.train_start
    if len(fold.train_dataset.data) != exp_train_len:
        problems.append(
            f"fold {fold.fold_id}: train dataset length "
            f"{len(fold.train_dataset.data)} != expected {exp_train_len}"
        )
    exp_run_len = fold.warmup_bars + (fold.validation_end - fold.validation_start)
    if len(fold.validation_run_dataset.data) != exp_run_len:
        problems.append(
            f"fold {fold.fold_id}: validation-run dataset length "
            f"{len(fold.validation_run_dataset.data)} != expected {exp_run_len}"
        )
    if fold.warmup_bars != config.warmup_bars:
        problems.append(
            f"fold {fold.fold_id}: warmup_bars {fold.warmup_bars} != "
            f"config {config.warmup_bars}"
        )
    return problems


# --------------------------------------------------------------------------- #
# Per-fold evaluation
# --------------------------------------------------------------------------- #
# Fold status values
STATUS_VALID = "valid"
STATUS_NO_CANDIDATE = "no_candidate"
STATUS_INSUFFICIENT_TRADES = "insufficient_trades"
STATUS_INVALID = "invalid"

VALID_STATUSES = {
    STATUS_VALID,
    STATUS_INSUFFICIENT_TRADES,
    STATUS_NO_CANDIDATE,
    STATUS_INVALID,
}


@dataclass
class FoldResult:
    """Result of evaluating one fold (Phase 14, Step 8).

    ``train_evaluation``       = IN-SAMPLE evaluation on the fold's train window.
    ``validation_evaluation``  = WALK-FORWARD evaluation on the fold's validation
                                 window ONLY (equity/return/drawdown measured from
                                 the validation boundary). Never mixed with train.
    ``validation_total_return``= mark-to-market validation-window return (excludes
                                 warm-up context P&L; see _validation_window_return).
    ``validation_trade_count`` = count of trades ENTERED within the validation
                                 window (warm-up-entered trades excluded).
    """

    fold_id: int
    status: str = STATUS_INVALID
    error: str = ""
    selected_spec: Optional[StrategySpec] = None
    train_start: str = ""
    train_end: str = ""
    validation_start: str = ""
    validation_end: str = ""
    train_evaluation: Optional[StrategyEvaluation] = None
    validation_evaluation: Optional[StrategyEvaluation] = None
    validation_trade_count: int = 0
    validation_total_return: Optional[float] = None
    validation_win_rate: Optional[float] = None
    validation_profit_factor: Optional[float] = None
    validation_avg_trade_return: Optional[float] = None
    unavailable_metrics: list = field(default_factory=list)


def _validation_trade_stats(trades) -> tuple:
    """Deterministic win-rate / profit-factor / avg-trade-return over a trade list.

    Follows the same definitions as research/performance.py (profit factor is
    None when there are no losing trades — it is not represented as infinity).
    """
    n = len(trades)
    if n == 0:
        return None, None, None
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    win_rate = len(wins) / n
    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = -sum(t.net_pnl for t in losses)
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    else:
        profit_factor = None
    avg_ret = sum(t.ret for t in trades) / n
    return win_rate, profit_factor, avg_ret


def _first_validation_ts(fold: Fold) -> pd.Timestamp:
    """Timestamp of the first validation-window bar in the run dataset."""
    run = fold.validation_run_dataset.data
    return run.index[fold.warmup_bars]


def _validation_window_return(result, fold: Fold) -> Optional[float]:
    """Mark-to-market validation-window return (excludes warm-up context P&L).

    The existing backtester's equity curve reflects REALIZED P&L (it is flat
    while a position is held). If a position entered during the warm-up context
    is still open at the validation boundary, ``result.total_return`` would
    count the context-portion price action. To measure ONLY the validation
    window, we mark any bridged position to market at the boundary:

        start_value = eval_initial                                (notional at
                     + sum over bridged trades of unrealized P&L    boundary)
        end_value   = result.final_capital                        (mark-out)

    This is deterministic and correct for both positions that close within the
    validation window and positions held to the end.
    """
    boundary_ts = _first_validation_ts(fold)
    run_df = fold.validation_run_dataset.data
    boundary_close = float(run_df["close"].loc[boundary_ts])
    start_value = result.initial_capital
    for t in result.trades:
        if t.entry_ts < boundary_ts:
            start_value += t.direction * t.quantity * (boundary_close - t.entry_price)
    end_value = result.final_capital
    if start_value <= 0:
        return 0.0
    return (end_value - start_value) / start_value


def _assess_fold(
    fold: Fold,
    spec: StrategySpec,
    backtest_config: BacktestConfig,
    wf_config: WalkForwardConfig,
    *,
    allowed_symbols: Optional[set] = None,
    train_evaluation: Optional[StrategyEvaluation] = None,
) -> FoldResult:
    """Deterministically validate + evaluate one fold for a (fixed) spec.

    Uses the EXISTING ``run_backtest``. Warm-up context is provided through the
    validation-run dataset + ``BacktestConfig.warmup_bars``; equity metrics are
    thereby measured only over the validation window. Per-trade statistics are
    computed from trades entered at/after the validation boundary.
    """
    fr = FoldResult(
        fold_id=fold.fold_id,
        train_start=str(fold.train_dataset.data.index[0]),
        train_end=str(fold.train_dataset.data.index[-1]),
        validation_start=str(fold.validation_run_dataset.data.index[fold.warmup_bars]),
        validation_end=str(fold.validation_run_dataset.data.index[-1]),
        selected_spec=spec,
        train_evaluation=train_evaluation,
    )

    # Spec validity for this fold's data.
    errors = validate_spec(
        spec, fold.validation_run_dataset, allowed_symbols=allowed_symbols
    )
    if errors:
        fr.status = STATUS_INVALID
        fr.error = "invalid spec for fold: " + "; ".join(errors)
        fr.unavailable_metrics = ["validation_evaluation"]
        return fr

    # In-sample (train window).
    if train_evaluation is None:
        try:
            train_cfg = merged_backtest_config(spec, backtest_config)
            fr.train_evaluation = evaluate_spec(
                spec,
                run_backtest(fold.train_dataset, build_strategy(spec), train_cfg),
            )
        except ValueError as e:
            fr.error = f"train backtest failed: {e}"
            fr.status = STATUS_INVALID
            fr.unavailable_metrics = ["train_evaluation", "validation_evaluation"]
            return fr

    # Walk-forward (validation window with warm-up context).
    try:
        val_cfg = merged_backtest_config(spec, backtest_config)
        val_cfg.warmup_bars = fold.warmup_bars
        result = run_backtest(
            fold.validation_run_dataset, build_strategy(spec), val_cfg
        )
    except ValueError as e:
        fr.status = STATUS_INVALID
        fr.error = f"validation backtest failed: {e}"
        fr.unavailable_metrics = ["validation_evaluation"]
        return fr

    fr.validation_evaluation = evaluate_spec(spec, result)
    fr.validation_total_return = _validation_window_return(result, fold)

    # Filter the ledger to trades ENTERED within the validation window, so
    # warm-up-entered trades never count as validation trades.
    boundary_ts = _first_validation_ts(fold)
    val_trades = [t for t in result.trades if t.entry_ts >= boundary_ts]
    fr.validation_trade_count = len(val_trades)
    fr.validation_win_rate, fr.validation_profit_factor, fr.validation_avg_trade_return = (
        _validation_trade_stats(val_trades)
    )
    if len(val_trades) < wf_config.min_validation_trades:
        fr.status = STATUS_INSUFFICIENT_TRADES
        if len(val_trades) == 0:
            fr.unavailable_metrics = ["win_rate", "profit_factor", "avg_trade_return"]
    else:
        fr.status = STATUS_VALID
    return fr


# --------------------------------------------------------------------------- #
# Cross-fold summary + warnings
# --------------------------------------------------------------------------- #
@dataclass
class WalkForwardSummary:
    """Aggregate, cross-fold metrics (Phase 14, Step 9).

    All metrics are computed ONLY from folds with ``status == 'valid'``. Folds
    that failed (invalid spec / backtest failure / no selected candidate) or had
    too few validation trades are excluded from the return statistics and are
    surfaced through coverage + warnings instead.

    Consistency score (documented, transparent):
        consistency_score = positive_fold_ratio
                          * (1 - min(1, return_dispersion / DISPERSION_SCALE))

      * positive_fold_ratio = (# folds with validation ret>0) / n_valid
      * return_dispersion   = population std of fold returns / |mean fold return|
                              (population std when mean is ~0).
      * DISPERSION_SCALE = 0.5 is a fixed, documented saturation point.

    A strategy with small, consistently positive fold returns scores high; a
    strategy with one huge positive fold and otherwise negative folds scores
    near zero — regardless of aggregate return. The components
    (``positive_fold_ratio`` / ``return_dispersion``) are always exposed, so the
    score is never opaque.
    """

    n_folds: int = 0
    n_valid: int = 0
    n_failed: int = 0
    coverage: float = 0.0
    coverage_ok: bool = False
    positive_folds: int = 0
    positive_fold_ratio: Optional[float] = None
    avg_fold_return: Optional[float] = None
    median_fold_return: Optional[float] = None
    worst_fold_return: Optional[float] = None
    best_fold_return: Optional[float] = None
    return_std: Optional[float] = None
    return_dispersion: Optional[float] = None
    max_validation_drawdown: Optional[float] = None
    consistency_score: Optional[float] = None
    total_validation_trades: int = 0
    min_validation_trades: int = 0
    valid_fold_ids: list = field(default_factory=list)


def compute_walk_forward_summary(
    fold_results: list, wf_config: WalkForwardConfig
) -> WalkForwardSummary:
    """Deterministic aggregate metrics over valid folds. Never fabricates."""
    n_folds = len(fold_results)
    valid = [f for f in fold_results if f.status == STATUS_VALID]
    returns = [
        f.validation_total_return
        if f.validation_total_return is not None
        else f.validation_evaluation.total_return
        for f in valid
    ]

    summary = WalkForwardSummary(n_folds=n_folds, n_valid=len(valid))
    summary.n_failed = n_folds - len(valid)
    summary.coverage = (len(valid) / n_folds) if n_folds else 0.0
    summary.coverage_ok = summary.coverage >= wf_config.min_fold_coverage
    summary.valid_fold_ids = [f.fold_id for f in valid]

    if valid:
        positive = [r for r in returns if r > 0]
        summary.positive_folds = len(positive)
        summary.positive_fold_ratio = len(positive) / len(valid)
        summary.avg_fold_return = float(statistics.mean(returns))
        summary.median_fold_return = float(statistics.median(returns))
        summary.worst_fold_return = float(min(returns))
        summary.best_fold_return = float(max(returns))
        summary.return_std = float(
            statistics.pstdev(returns) if len(returns) >= 2 else 0.0
        )
        avg = summary.avg_fold_return
        if abs(avg) > 1e-12:
            summary.return_dispersion = summary.return_std / abs(avg)
        else:
            summary.return_dispersion = summary.return_std
        summary.max_validation_drawdown = float(
            min(f.validation_evaluation.max_drawdown for f in valid)
        )
        summary.total_validation_trades = sum(
            f.validation_trade_count for f in valid
        )
        summary.min_validation_trades = min(
            f.validation_trade_count for f in valid
        )
        dispersion = summary.return_dispersion or 0.0
        summary.consistency_score = (
            summary.positive_fold_ratio
            * (1.0 - min(1.0, dispersion / DISPERSION_SCALE))
        )
    return summary


def collect_warnings(
    fold_results: list,
    summary: WalkForwardSummary,
    wf_config: WalkForwardConfig,
    *,
    spec_required_warmup: int = 0,
) -> list[str]:
    """Deterministic overfitting / robustness warnings (Phase 14, Step 11).

    These are research-quality signals, never claims about future performance.
    """
    warnings_list: list[str] = []
    if summary.n_valid == 0:
        warnings_list.append(
            "No valid validation folds; the run produced no measurable "
            "validation performance."
        )
    if not summary.coverage_ok:
        warnings_list.append(
            f"Insufficient historical coverage: {summary.n_valid}/{summary.n_folds} "
            f"folds valid (< {wf_config.min_fold_coverage:.0%})."
        )
    if summary.positive_fold_ratio is not None and summary.positive_fold_ratio < 0.5:
        warnings_list.append(
            f"Negative majority of validation folds "
            f"({summary.positive_folds}/{summary.n_valid} positive)."
        )
    low_trade_folds = sorted(
        f.fold_id
        for f in fold_results
        if f.validation_evaluation is not None
        and f.validation_trade_count < wf_config.min_validation_trades
    )
    if low_trade_folds:
        warnings_list.append(
            f"Too few validation trades in fold(s) {low_trade_folds} "
            f"(< {wf_config.min_validation_trades} trades)."
        )
    if (
        summary.max_validation_drawdown is not None
        and abs(summary.max_validation_drawdown) > 0.5
    ):
        warnings_list.append(
            f"Excessive validation drawdown: "
            f"{summary.max_validation_drawdown:.2%}."
        )
    if summary.return_dispersion is not None and summary.return_dispersion > 1.0:
        warnings_list.append(
            f"Unstable fold performance "
            f"(return dispersion {summary.return_dispersion:.2f})."
        )
    if summary.n_valid >= 2 and summary.positive_folds == 1:
        warnings_list.append(
            "Strategy only works in one period (exactly 1 positive validation fold)."
        )
    paired = [
        (f.train_evaluation, f.validation_evaluation)
        for f in fold_results
        if f.status == STATUS_VALID
        and f.train_evaluation is not None
        and f.validation_evaluation is not None
    ]
    if paired:
        train_returns = [t.total_return for t, _ in paired]
        val_returns = [v.total_return for _, v in paired]
        med_train = float(statistics.median(train_returns))
        med_val = float(statistics.median(val_returns))
        if med_train > 0.05 and med_val <= 0.0:
            warnings_list.append(
                f"Very high train return but poor validation return "
                f"(median train {med_train:.2%}, median validation "
                f"{med_val:.2%})."
            )
        if med_train - med_val > 0.20:
            warnings_list.append(
                f"Large train/validation performance gap "
                f"(median train {med_train:.2%} - median validation "
                f"{med_val:.2%})."
            )
    if spec_required_warmup > wf_config.warmup_bars:
        warnings_list.append(
            f"Spec requires {spec_required_warmup} warm-up bars but the "
            f"walk-forward config provides only {wf_config.warmup_bars}."
        )
    return warnings_list


# --------------------------------------------------------------------------- #
# Report + public entry points
# --------------------------------------------------------------------------- #
DISCLAIMER = (
    "Walk-forward validation is historical evidence of robustness, NOT a "
    "guarantee of future profitability."
)


@dataclass
class WalkForwardReport:
    """Structured walk-forward report (Phase 14, Step 13).

    ``kind`` is ``"fixed_spec"`` or ``"research"``:
      * fixed_spec — a pre-existing StrategySpec was evaluated across every
        fold's validation window (no per-fold selection, no LLM required).
      * research   — per fold, candidates were generated/researched/ranked on
        the TRAIN window ONLY; the single top candidate was then evaluated on
        that fold's validation window.

    ``whole_dataset_evaluation`` (fixed_spec only) is the IN-SAMPLE evaluation
    of the spec on the entire dataset — provided explicitly as a comparison
    reference and never mixed into the walk-forward aggregates.
    """

    kind: str
    spec_name: str
    symbol: str
    timeframe: str
    mode: str
    config: WalkForwardConfig
    folds: list = field(default_factory=list)
    summary: Optional[WalkForwardSummary] = None
    warnings: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    whole_dataset_evaluation: Optional[StrategyEvaluation] = None


def walk_forward_validate(
    dataset: HistoricalDataset,
    spec: StrategySpec,
    backtest_config: BacktestConfig,
    wf_config: WalkForwardConfig,
    *,
    allowed_symbols: Optional[set] = None,
) -> WalkForwardReport:
    """Walk-forward validation of a FIXED StrategySpec (no LLM, no selection).

    Central guarantee (Phase 14, Step 7): the validation windows are only ever
    *measured*, never used for selection — there is no selection in this path at
    all. Every fold's validation backtest is run on warm-up-context + validation
    data with the existing backtester, so indicators are causal and warm-up bars
    never contribute to validation metrics.
    """
    folds = generate_folds(dataset, wf_config)
    required = required_warmup_bars(spec)
    if required > wf_config.warmup_bars:
        raise ValueError(
            f"spec {spec.name!r} requires {required} warm-up bars but the "
            f"walk-forward config provides only {wf_config.warmup_bars}"
        )

    fold_results: list = []
    for fold in folds:
        problems = validate_fold(fold, wf_config)
        if problems:
            raise ValueError(
                "fold generation violated integrity: " + "; ".join(problems)
            )
        fold_results.append(
            _assess_fold(
                fold, spec, backtest_config, wf_config,
                allowed_symbols=allowed_symbols,
            )
        )

    summary = compute_walk_forward_summary(fold_results, wf_config)
    warnings = collect_warnings(
        fold_results, summary, wf_config, spec_required_warmup=required
    )

    whole_eval: Optional[StrategyEvaluation] = None
    try:
        cfg = merged_backtest_config(spec, backtest_config)
        whole_eval = evaluate_spec(
            spec, run_backtest(dataset, build_strategy(spec), cfg)
        )
    except ValueError:
        whole_eval = None

    return WalkForwardReport(
        kind="fixed_spec",
        spec_name=spec.name,
        symbol=dataset.symbol,
        timeframe=dataset.timeframe,
        mode=wf_config.mode,
        config=wf_config,
        folds=fold_results,
        summary=summary,
        warnings=warnings,
        notes=[DISCLAIMER],
        whole_dataset_evaluation=whole_eval,
    )


def walk_forward_research(
    dataset: HistoricalDataset,
    engine: StrategyResearchEngine,
    candidate_count: int,
    backtest_config: BacktestConfig,
    wf_config: WalkForwardConfig,
    *,
    generation_context: Optional[GenerationContext] = None,
    allowed_symbols: Optional[set] = None,
) -> WalkForwardReport:
    """Walk-forward research: per-fold TRAIN-only selection, then validation.

    For each fold:
        1. candidates are generated, validated, backtested, filtered, and ranked
           on the fold's TRAIN window (the existing StrategyResearchEngine);
        2. the top-ranked candidate is selected;
        3. that candidate is evaluated on the fold's VALIDATION window only.

    Leakage control (Phase 14, Step 7): the fold's validation window is NEVER
    presented to the research step of the same fold — the engine only ever sees
    ``fold.train_dataset``, and the per-fold ``GenerationContext`` is built from
    the train window (never the validation window). Validation data can
    therefore never influence which candidate is selected for that fold.
    """
    folds = generate_folds(dataset, wf_config)

    fold_results: list = []
    for fold in folds:
        problems = validate_fold(fold, wf_config)
        if problems:
            raise ValueError(
                "fold generation violated integrity: " + "; ".join(problems)
            )

        # --- 1) TRAIN-ONLY candidate selection --------------------------- #
        ctx = generation_context or GenerationContext.from_dataset(fold.train_dataset)
        train_report = engine.research_candidates(
            fold.train_dataset, ctx, candidate_count, backtest_config
        )
        selected = _select_top_candidate(train_report)

        if selected is None:
            fold_results.append(
                FoldResult(
                    fold_id=fold.fold_id,
                    status=STATUS_NO_CANDIDATE,
                    error="no candidate passed the quality filter on the train window",
                    train_start=str(fold.train_dataset.data.index[0]),
                    train_end=str(fold.train_dataset.data.index[-1]),
                    validation_start=str(
                        fold.validation_run_dataset.data.index[fold.warmup_bars]
                    ),
                    validation_end=str(fold.validation_run_dataset.data.index[-1]),
                    unavailable_metrics=["validation_evaluation"],
                )
            )
            continue

        spec, train_evaluation = selected
        spec_required = required_warmup_bars(spec)
        if spec_required > wf_config.warmup_bars:
            fold_results.append(
                FoldResult(
                    fold_id=fold.fold_id,
                    status=STATUS_INVALID,
                    error=(
                        f"selected spec requires {spec_required} warm-up bars; "
                        f"config provides {wf_config.warmup_bars}"
                    ),
                    selected_spec=spec,
                    train_start=str(fold.train_dataset.data.index[0]),
                    train_end=str(fold.train_dataset.data.index[-1]),
                    validation_start=str(
                        fold.validation_run_dataset.data.index[fold.warmup_bars]
                    ),
                    validation_end=str(fold.validation_run_dataset.data.index[-1]),
                    train_evaluation=train_evaluation,
                    unavailable_metrics=["validation_evaluation"],
                )
            )
            continue

        # --- 2) VALIDATION-ONLY evaluation (warm-up context + window) ----- #
        fold_results.append(
            _assess_fold(
                fold,
                spec,
                backtest_config,
                wf_config,
                allowed_symbols=allowed_symbols,
                train_evaluation=train_evaluation,
            )
        )

    summary = compute_walk_forward_summary(fold_results, wf_config)
    worst_required = max(
        (required_warmup_bars(fr.selected_spec)
         for fr in fold_results if fr.selected_spec is not None),
        default=0,
    )
    warnings = collect_warnings(
        fold_results, summary, wf_config, spec_required_warmup=worst_required
    )

    first_spec = next(
        (fr.selected_spec for fr in fold_results if fr.selected_spec is not None),
        None,
    )
    return WalkForwardReport(
        kind="research",
        spec_name=first_spec.name if first_spec is not None else "no-candidate",
        symbol=dataset.symbol,
        timeframe=dataset.timeframe,
        mode=wf_config.mode,
        config=wf_config,
        folds=fold_results,
        summary=summary,
        warnings=warnings,
        notes=[DISCLAIMER],
    )


def _select_top_candidate(train_report):
    """Return (spec, train_evaluation) of the highest-ranked filtered candidate.

    Returns None when the ranking is empty (no candidate passed the quality
    filters on the train window).
    """
    if not train_report.ranking:
        return None
    by_name = {c.spec_name: c for c in train_report.passed}
    top_name = train_report.ranking[0].key
    candidate = by_name.get(top_name)
    if candidate is None or candidate.spec is None:
        return None
    return candidate.spec, candidate.evaluation
