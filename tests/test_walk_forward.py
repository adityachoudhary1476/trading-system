"""Phase 14 tests: chronological walk-forward validation + robustness.

Offline, deterministic. Covers fold generation (rolling/expanding), chronology,
warm-up handling, per-fold and aggregate evaluation, consistency analysis,
overfitting warnings, leakage control (validation data can never influence
same-fold selection), and reproducibility.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_system.research.backtester import BacktestConfig, run_backtest
from trading_system.research.dataset import HistoricalDataset
from trading_system.models.base import ModelProviderError
from trading_system.research.strategy_lab.engine import (
    ResearchConfig,
    StrategyResearchEngine,
)
from trading_system.research.strategy_lab.evaluation import StrategyEvaluation
from trading_system.research.strategy_lab.filters import QualityFilterConfig
from trading_system.research.strategy_lab.interpreter import build_strategy
from trading_system.research.strategy_lab.providers import (
    DeterministicStrategyProvider,
    GenerationContext,
    StrategyProposalProvider,
)
from trading_system.research.strategy_lab.spec import (
    StrategySpec,
    const_operand,
    field_operand,
    indicator_operand,
    make_condition,
)
from trading_system.research.strategy_lab.walk_forward import (
    Fold,
    FoldResult,
    WalkForwardConfig,
    WalkForwardSummary,
    collect_warnings,
    compute_walk_forward_summary,
    generate_folds,
    validate_fold,
    walk_forward_research,
    walk_forward_validate,
)


# --------------------------------------------------------------------------- #
# Deterministic helpers
# --------------------------------------------------------------------------- #
def _ohlc(n, seed=7, drift=0.02, vol=1.0, start_price=100.0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n, freq="1D", tz="UTC")
    close = start_price + np.cumsum(rng.normal(drift, vol, n))
    close = np.maximum(close, 1.0)
    o = close + rng.normal(0, 0.3, n)
    high = np.maximum(o, close) + np.abs(rng.normal(0, 0.5, n))
    low = np.minimum(o, close) - np.abs(rng.normal(0, 0.5, n))
    return pd.DataFrame(
        {
            "open": o, "high": high, "low": low, "close": close,
            "volume": rng.integers(100, 1000, n).astype(float),
        },
        index=idx,
    )


def _dataset(df=None, n=400, **kw):
    return HistoricalDataset(
        symbol="NSE:SBIN", timeframe="1d",
        data=df if df is not None else _ohlc(n, **kw),
    )


def _sma_spec(window=10):
    return StrategySpec(
        name="SMA trend",
        description="test trend",
        symbol="NSE:SBIN",
        timeframe="1d",
        indicators=[{"name": "sma", "params": {"window": window}}],
        entry=make_condition(
            field_operand("close"), ">", indicator_operand(f"sma_{window}")
        ),
        exit=make_condition(
            field_operand("close"), "<", indicator_operand(f"sma_{window}")
        ),
    )


def _always_long_spec():
    return StrategySpec(
        name="Always long",
        description="hold forever",
        symbol="NSE:SBIN",
        timeframe="1d",
        entry=make_condition(field_operand("close"), ">", const_operand(0.0)),
    )


def _wfc(**kw):
    base = dict(
        mode="rolling", n_folds=3, validation_window=60, train_window=150,
        step_size=70, warmup_bars=20, min_validation_trades=2,
    )
    base.update(kw)
    return WalkForwardConfig(**base)


def _btc():
    return BacktestConfig(initial_capital=100_000)


# --------------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kwargs",
    [
        {"validation_window": 0},
        {"train_window": 0},
        {"step_size": 0},
        {"n_folds": 0},
        {"min_train_bars": 1000},
        {"min_validation_bars": 1000},
        {"step_size": 10, "validation_window": 60},  # overlap w/o allow_overlap
        {"min_fold_coverage": 2.0},
        {"mode": "spaghetti"},
    ],
)
def test_invalid_config_rejected(kwargs):
    with pytest.raises(ValueError):
        WalkForwardConfig(**kwargs)


def test_overlap_allowed_explicitly():
    cfg = WalkForwardConfig(step_size=20, validation_window=60, allow_overlap=True)
    assert cfg.allow_overlap is True


# --------------------------------------------------------------------------- #
# Fold generation
# --------------------------------------------------------------------------- #
def test_rolling_windows_boundaries():
    ds = _dataset(n=500)
    cfg = _wfc(n_folds=3, train_window=120, validation_window=50, step_size=60)
    folds = generate_folds(ds, cfg)
    assert [f.fold_id for f in folds] == [0, 1, 2]
    assert [f.train_start for f in folds] == [0, 60, 120]
    assert [f.train_end for f in folds] == [120, 180, 240]
    assert [f.validation_start for f in folds] == [120, 180, 240]
    assert [f.validation_end for f in folds] == [170, 230, 290]
    assert [len(f.train_dataset.data) for f in folds] == [120, 120, 120]
    assert [len(f.validation_run_dataset.data) for f in folds] == [
        70, 70, 70,  # warmup 20 + validation 50
    ]


def test_expanding_windows_boundaries():
    ds = _dataset(n=500)
    cfg = _wfc(mode="expanding", n_folds=3, train_window=120,
               min_train_bars=120, validation_window=50, step_size=70)
    folds = generate_folds(ds, cfg)
    assert [f.train_start for f in folds] == [0, 0, 0]           # always from start
    assert [f.train_end for f in folds] == [120, 190, 260]        # growing
    assert [f.validation_start for f in folds] == [120, 190, 260]
    assert [len(f.train_dataset.data) for f in folds] == [120, 190, 260]


def test_correct_number_of_folds():
    ds = _dataset(n=500)
    cfg = _wfc(n_folds=5)
    assert len(generate_folds(ds, cfg)) == 5


def test_step_size_advances_validation_starts():
    ds = _dataset(n=500)
    cfg = _wfc(n_folds=4, train_window=100, validation_window=40, step_size=80)
    folds = generate_folds(ds, cfg)
    starts = [f.validation_start for f in folds]
    assert starts == [100, 180, 260, 340]
    assert all(starts[i + 1] - starts[i] == 80 for i in range(len(starts) - 1))


def test_insufficient_data_raises():
    ds = _dataset(n=120)
    cfg = _wfc(n_folds=5, train_window=150, validation_window=60, step_size=70)
    with pytest.raises(ValueError, match="insufficient data"):
        generate_folds(ds, cfg)


def test_generate_folds_insufficient_warmup_context_raises():
    ds = _dataset(n=500)
    cfg = _wfc(train_window=60, min_train_bars=60, warmup_bars=200)
    with pytest.raises(ValueError, match="warm-up"):
        generate_folds(ds, cfg)


def test_fold_generation_is_deterministic():
    ds = _dataset(n=500)
    cfg = _wfc(n_folds=4)
    a = generate_folds(ds, cfg)
    b = generate_folds(ds, cfg)
    for fa, fb in zip(a, b):
        assert fa.train_start == fb.train_start
        assert fa.validation_end == fb.validation_end
        assert list(fa.train_dataset.data["close"]) == list(
            fb.train_dataset.data["close"]
        )


def test_validate_fold_passes_for_generated_folds():
    ds = _dataset(n=500)
    cfg = _wfc(n_folds=3)
    for fold in generate_folds(ds, cfg):
        assert validate_fold(fold, cfg) == []


def test_validate_fold_detects_chronology_breach():
    ds = _dataset(n=500)
    cfg = _wfc(n_folds=1)
    fold = generate_folds(ds, cfg)[0]
    # Introduce a leak: the validation window starts BEFORE the train window
    # ends (train_end stays at the original boundary).
    breached = Fold(
        fold_id=fold.fold_id,
        train_start=fold.train_start,
        train_end=fold.validation_start,
        validation_start=fold.validation_start - 10,
        validation_end=fold.validation_end,
        warmup_bars=fold.warmup_bars,
        train_dataset=fold.train_dataset,
        validation_run_dataset=fold.validation_run_dataset,
    )
    problems = validate_fold(breached, cfg)
    assert any("chronology/leakage" in p for p in problems)
    assert any("overlaps training" in p for p in problems)


# --------------------------------------------------------------------------- #
# Chronology
# --------------------------------------------------------------------------- #
def test_train_always_precedes_validation():
    ds = _dataset(n=500)
    cfg = _wfc(n_folds=4)
    for fold in generate_folds(ds, cfg):
        assert fold.train_end == fold.validation_start
        assert fold.train_end < fold.validation_end
        assert (
            fold.train_dataset.data.index.max()
            < fold.validation_run_dataset.data.index[fold.warmup_bars]
        )


def test_folds_are_chronological():
    ds = _dataset(n=500)
    cfg = _wfc(n_folds=4)
    folds = generate_folds(ds, cfg)
    starts = [f.validation_start for f in folds]
    assert starts == sorted(starts)
    # No fold's validation window starts before the previous fold's train end.
    for prev, nxt in zip(folds, folds[1:]):
        assert prev.validation_end <= nxt.validation_start or cfg.allow_overlap


def test_no_random_shuffling():
    ds = _dataset(n=500, seed=3)
    df = ds.data.sort_index()
    cfg = _wfc(n_folds=3, train_window=120, validation_window=50, step_size=60)
    folds = generate_folds(ds, cfg)
    for f in folds:
        # Train dataset must be the exact positional slice of the original.
        assert list(f.train_dataset.data["close"]) == list(
            df["close"].iloc[f.train_start:f.train_end]
        )
        assert list(f.validation_run_dataset.data["close"]) == list(
            df["close"].iloc[
                f.validation_start - f.warmup_bars : f.validation_end
            ]
        )


# --------------------------------------------------------------------------- #
# Warm-up handling
# --------------------------------------------------------------------------- #
def test_insufficient_spec_warmup_raises():
    ds = _dataset(n=500)
    cfg = _wfc(warmup_bars=20)
    spec = _sma_spec(window=100)  # needs 100 warmup bars
    with pytest.raises(ValueError, match="warm-up"):
        walk_forward_validate(ds, spec, _btc(), cfg)


def test_sufficient_spec_warmup_ok():
    ds = _dataset(n=500)
    cfg = _wfc(warmup_bars=30)
    spec = _sma_spec(window=20)
    report = walk_forward_validate(ds, spec, _btc(), cfg)
    assert len(report.folds) == cfg.n_folds


def test_warmup_bars_never_count_as_validation_trades():
    # "Always long" enters during the warm-up context and holds. The fold's
    # validation window must therefore contain ZERO *entered* trades, even
    # though the backtest produced a position.
    ds = _dataset(n=400, seed=11)
    cfgs = _wfc(n_folds=1, warmup_bars=20, validation_window=60,
                min_validation_trades=1)
    fold_obj = generate_folds(ds, cfgs)[0]
    report = walk_forward_validate(ds, _always_long_spec(), _btc(), cfgs)
    fold = report.folds[0]
    assert fold.validation_evaluation is not None
    # Direct run over the validation window alone DOES trade (position opened
    # within it), proving the excluded trade is the context-entered one.
    pure = HistoricalDataset(
        symbol="NSE:SBIN", timeframe="1d",
        data=fold_obj.validation_run_dataset.data.iloc[fold_obj.warmup_bars:],
    )
    direct = run_backtest(pure, build_strategy(_always_long_spec()), _btc())
    assert len(direct.trades) >= 1
    assert fold.validation_trade_count == 0
    assert fold.status == "insufficient_trades"
    assert "win_rate" in fold.unavailable_metrics


def test_validation_return_measured_on_window_only():
    # The always-long equity return must equal the buy-and-hold return over the
    # VALIDATION window only (close[W] -> close[end]), NOT including the
    # warm-up context's price action. The fold's mark-to-market
    # validation_total_return corrects for the context-bridged position.
    ds = _dataset(n=400, drift=0.1, seed=13)
    cfg = _wfc(n_folds=1, warmup_bars=20, validation_window=60,
               min_validation_trades=1)
    fold_obj = generate_folds(ds, cfg)[0]
    report = walk_forward_validate(ds, _always_long_spec(), _btc(), cfg)
    fold = report.folds[0]
    run_df = fold_obj.validation_run_dataset.data
    val_close = run_df["close"].iloc[fold_obj.warmup_bars:]
    window_buyhold = val_close.iloc[-1] / val_close.iloc[0] - 1.0
    assert fold.validation_total_return == pytest.approx(window_buyhold, abs=0.005)
    # (The raw backtest evaluation's realized-equity return is NOT used for
    # walk-forward aggregates — the mark-to-market correction above is.)


def test_short_dataset_fails_clearly():
    ds = _dataset(n=40)
    with pytest.raises(ValueError):
        walk_forward_validate(ds, _sma_spec(10), _btc(), _wfc())


def test_indicator_warmup_never_sees_future_data():
    # Fold validation-run datasets are strict prefixes relative to later folds:
    # the context of fold i is always a chronological prefix of what exists
    # before fold i's validation window. Verify no run dataset contains bars
    # after its own validation_end.
    ds = _dataset(n=500)
    cfg = _wfc(n_folds=4)
    df = ds.data.sort_index()
    for fold in generate_folds(ds, cfg):
        assert fold.validation_run_dataset.data.index[-1] == df.index[fold.validation_end - 1]
        assert fold.validation_run_dataset.data.index[0] == df.index[
            fold.validation_start - fold.warmup_bars
        ]


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
def _wf_engine(**engine_kw):
    filt = QualityFilterConfig(min_trades=2, require_reliable=False, min_bars=30)
    return StrategyResearchEngine(
        DeterministicStrategyProvider(),
        ResearchConfig(max_candidates=4, min_bars=30, quality_filter=filt, **engine_kw),
    )


def test_single_fold():
    ds = _dataset(n=400, drift=0.1)
    cfg = _wfc(n_folds=1)
    report = walk_forward_validate(ds, _sma_spec(10), _btc(), cfg)
    assert len(report.folds) == 1
    fold = report.folds[0]
    assert fold.status == "valid"
    assert fold.validation_trade_count >= cfg.min_validation_trades


def test_multiple_folds():
    ds = _dataset(n=500, drift=0.1)
    cfg = _wfc(n_folds=3)
    report = walk_forward_validate(ds, _sma_spec(10), _btc(), cfg)
    assert len(report.folds) == 3
    assert all(f.status == "valid" for f in report.folds)
    assert report.summary.n_valid == 3


def test_successful_fold_details():
    ds = _dataset(n=400, drift=0.1)
    cfg = _wfc(n_folds=1)
    report = walk_forward_validate(ds, _sma_spec(10), _btc(), cfg)
    fold = report.folds[0]
    assert fold.fold_id == 0
    assert fold.train_evaluation is not None and fold.validation_evaluation is not None
    assert fold.validation_trade_count > 0
    assert fold.validation_win_rate is not None
    assert fold.validation_start < fold.validation_end


def test_failed_fold_when_spec_invalid_on_validation():
    # Spec for another symbol + allowlist → invalid on every fold.
    spec = _sma_spec(10).model_copy(update={"symbol": "NSE:OTHER"})
    ds = _dataset(n=500)
    report = walk_forward_validate(ds, spec, _btc(), _wfc(),
                                   allowed_symbols={"NSE:SBIN"})
    assert all(f.status == "invalid" for f in report.folds)
    assert all(f.validation_evaluation is None for f in report.folds)
    assert report.summary.n_valid == 0
    assert any("No valid validation folds" in w for w in report.warnings)


def test_insufficient_trades_fold():
    cfg = _wfc(n_folds=1, warmup_bars=20, validation_window=60,
               min_validation_trades=100)
    ds = _dataset(n=400, drift=0.0, vol=0.2)  # low-noise: few whipsaws
    report = walk_forward_validate(ds, _sma_spec(10), _btc(), cfg)
    fold = report.folds[0]
    if fold.validation_trade_count < 100:
        assert fold.status == "insufficient_trades"
        assert any("Too few validation trades" in w for w in report.warnings)


def test_unavailable_metrics_on_zero_trade_fold():
    # Always-long enters only in warmup → zero validation trades.
    ds = _dataset(n=400, seed=17)
    cfg = _wfc(n_folds=1, warmup_bars=20, validation_window=60,
               min_validation_trades=1)
    report = walk_forward_validate(ds, _always_long_spec(), _btc(), cfg)
    fold = report.folds[0]
    assert fold.validation_win_rate is None
    assert fold.validation_profit_factor is None
    assert "win_rate" in fold.unavailable_metrics


def test_aggregate_metrics():
    folds = []
    for ret in (0.10, 0.05, -0.03, 0.12):
        ev = StrategyEvaluation(
            spec_name="X", symbol="NSE:SBIN", timeframe="1d",
            net_pnl=ret * 100_000.0, total_return=ret,
            n_trades=8, max_drawdown=-0.02, exposure_pct=0.5, reliable=True,
        )
        folds.append(FoldResult(fold_id=len(folds), status="valid",
                                validation_evaluation=ev,
                                validation_trade_count=8))
    summary = compute_walk_forward_summary(folds, _wfc())
    assert summary.n_valid == 4
    assert summary.positive_folds == 3
    assert summary.positive_fold_ratio == pytest.approx(0.75)
    assert summary.avg_fold_return == pytest.approx(0.06)
    assert summary.median_fold_return == pytest.approx(0.075)
    assert summary.worst_fold_return == pytest.approx(-0.03)
    assert summary.best_fold_return == pytest.approx(0.12)
    assert summary.total_validation_trades == 32
    assert summary.consistency_score is not None


# --------------------------------------------------------------------------- #
# Robustness / consistency
# --------------------------------------------------------------------------- #
def _summary_for_returns(returns):
    folds = [
        FoldResult(
            fold_id=i,
            status="valid",
            validation_evaluation=StrategyEvaluation(
                spec_name="X", symbol="NSE:SBIN", timeframe="1d",
                net_pnl=float(r) * 100_000.0, total_return=float(r),
                n_trades=8, max_drawdown=-0.02, exposure_pct=0.5, reliable=True,
            ),
            validation_trade_count=8,
        )
        for i, r in enumerate(returns)
    ]
    return compute_walk_forward_summary(folds, _wfc())


def test_consistently_profitable_folds_score_high():
    s = _summary_for_returns([0.08, 0.07, 0.09, 0.06])
    assert s.positive_fold_ratio == 1.0
    assert s.return_dispersion < 1.0
    assert s.consistency_score > 0.5


def test_volatile_folds_score_lower_despite_high_aggregate():
    volatile = _summary_for_returns([0.40, 0.35, -0.50, 0.45])
    stable = _summary_for_returns([0.08, 0.07, 0.09, 0.06])
    # The stable (lower aggregate) strategy must score fairly higher for
    # historical consistency, even though its total return may be lower.
    assert stable.consistency_score > volatile.consistency_score
    assert any(
        "Unstable fold performance" in x
        for x in collect_warnings([], volatile, _wfc())
    )


def test_mostly_negative_folds_warn():
    s = _summary_for_returns([-0.05, -0.06, 0.01, -0.04])
    assert s.positive_fold_ratio == pytest.approx(0.25)
    assert any(
        "Negative majority of validation folds" in x
        for x in collect_warnings([], s, _wfc())
    )


def test_one_extreme_positive_fold_flagged_unstable():
    s = _summary_for_returns([-0.04, -0.03, 0.60, -0.05])
    assert s.positive_fold_ratio == pytest.approx(0.25)
    assert s.return_dispersion > 1.0
    assert any(
        "Unstable fold performance" in x
        for x in collect_warnings([], s, _wfc())
    )


def test_one_negative_fold_lowers_consistency():
    s = _summary_for_returns([0.10, 0.12, -0.40, 0.11])
    assert s.consistency_score < 0.5
    assert any(
        "Unstable fold performance" in x for x in collect_warnings([], s, _wfc())
    )


def test_train_validation_divergence_warns():
    folds = [
        FoldResult(
            fold_id=0, status="valid",
            train_evaluation=StrategyEvaluation(
                spec_name="X", symbol="NSE:SBIN", timeframe="1d",
                net_pnl=5_000.0, total_return=0.50, n_trades=20,
                max_drawdown=-0.01, reliable=True),
            validation_evaluation=StrategyEvaluation(
                spec_name="X", symbol="NSE:SBIN", timeframe="1d",
                net_pnl=-1_000.0, total_return=-0.10, n_trades=8,
                max_drawdown=-0.05, reliable=True),
            validation_trade_count=8,
        )
    ]
    summary = compute_walk_forward_summary(folds, _wfc())
    warnings = collect_warnings(folds, summary, _wfc())
    assert any("Very high train return but poor validation return" in x
               for x in warnings)


def test_strategy_works_in_one_period_warns():
    s = _summary_for_returns([-0.03, -0.04, 0.5, -0.05])
    assert any("only works in one period" in x for x in collect_warnings([], s, _wfc()))


def test_coverage_warning():
    folds = []
    for i in range(5):
        if i == 0:
            folds.append(FoldResult(
                fold_id=i, status="valid",
                validation_evaluation=StrategyEvaluation(
                    spec_name="X", symbol="NSE:SBIN", timeframe="1d",
                    net_pnl=100.0, total_return=0.05, n_trades=8,
                    max_drawdown=-0.01, reliable=True),
                validation_trade_count=8))
        else:
            folds.append(FoldResult(fold_id=i, status="invalid", error="boom"))
    cfg = _wfc(n_folds=5, min_fold_coverage=0.6)
    summary = compute_walk_forward_summary(folds, cfg)
    assert summary.coverage == pytest.approx(0.2)
    assert summary.coverage_ok is False
    assert any("Insufficient historical coverage" in x
               for x in collect_warnings(folds, summary, cfg))


# --------------------------------------------------------------------------- #
# Leakage control (mandatory)
# --------------------------------------------------------------------------- #
class _RecordingProvider(DeterministicStrategyProvider):
    """Records every GenerationContext it is shown (leakage spy)."""

    def __init__(self):
        super().__init__()
        self.seen_contexts: list = []

    def generate_strategy(self, context):
        self.seen_contexts.append(context.as_dict())
        return super().generate_strategy(context)


def _shared_prefix_datasets(shared=360, tail=120, up_seed=21, down_seed=22):
    """Two datasets with identical OHLCV in the shared prefix; the tail CLOSES
    differ sharply (one rallies, one declines). Trains lie entirely within the
    prefix, so selection must be identical in both datasets — any difference
    would prove validation data leaked into selection."""
    rng = np.random.default_rng(5)
    n = shared + tail
    # Build the FULL-length opens/highs/lows/volume ONCE so the shared region
    # is byte-identical between the two datasets.
    o = np.empty(n)
    o[0] = 100.0
    closes = np.empty(n)
    closes[0] = 100.0
    for i in range(1, n):
        closes[i] = closes[i - 1] + rng.normal(0.05, 0.8)
        o[i] = closes[i] + rng.normal(0, 0.2)
    high = np.maximum(o, closes) + 0.5
    low = np.minimum(o, closes) - 0.5
    volume = rng.integers(100, 1000, n).astype(float)
    base_idx = pd.date_range("2020-01-01", periods=n, freq="1D", tz="UTC")

    up_close = closes.copy()
    up_close[shared:] = up_close[shared - 1] + np.cumsum(rng.normal(0.4, 0.5, tail))
    down_close = closes.copy()
    down_close[shared:] = down_close[shared - 1] + np.cumsum(rng.normal(-0.4, 0.5, tail))

    def _frame(close):
        return pd.DataFrame(
            {"open": o, "high": high, "low": low, "close": close,
             "volume": volume},
            index=base_idx,
        )

    return (
        HistoricalDataset(symbol="NSE:SBIN", timeframe="1d", data=_frame(up_close)),
        HistoricalDataset(symbol="NSE:SBIN", timeframe="1d", data=_frame(down_close)),
    )


def test_validation_cannot_influence_same_fold_selection():
    # Datasets share identical train prefixes; the validation tails (and the
    # last fold's validation window) differ sharply. Selection must be
    # identical because the engine never sees validation data.
    ds_up, ds_down = _shared_prefix_datasets()
    assert ds_up is not ds_down
    # Prove the validation regions genuinely differ.
    assert list(ds_up.data["close"].iloc[360:]) != list(
        ds_down.data["close"].iloc[360:]
    )

    engine = _wf_engine()
    cfg = _wfc(n_folds=3, train_window=120, validation_window=60, step_size=90,
               warmup_bars=20, min_validation_trades=1)
    rep_up = walk_forward_research(ds_up, engine, 4, _btc(), cfg)
    rep_down = walk_forward_research(ds_down, engine, 4, _btc(), cfg)

    selected_up = [f.selected_spec.name if f.selected_spec else None
                   for f in rep_up.folds]
    selected_down = [f.selected_spec.name if f.selected_spec else None
                     for f in rep_down.folds]
    # Same TRAIN data -> same selection on every fold, despite different
    # validation periods. If validation data had leaked, candidates optimized
    # on the differing profiles would diverge.
    assert selected_up == selected_down
    assert all(name is not None for name in selected_up)


def test_provider_never_sees_validation_data():
    provider = _RecordingProvider()
    engine = StrategyResearchEngine(
        provider,
        ResearchConfig(max_candidates=2, min_bars=30,
                       quality_filter=QualityFilterConfig(
                           min_trades=1, require_reliable=False, min_bars=30)),
    )
    ds = _dataset(n=500, drift=0.1)
    wfc = _wfc(n_folds=3, train_window=120, validation_window=60,
               step_size=90, warmup_bars=20, min_validation_trades=1)
    report = walk_forward_research(ds, engine, 2, _btc(), wfc)
    folds = generate_folds(ds, wfc)

    # provider.generate_strategy is called candidate_count times per fold;
    # contexts are therefore ordered by fold.
    seen = provider.seen_contexts
    assert len(seen) == 3 * 2  # 3 folds x 2 candidates
    for i, fold in enumerate(folds):
        for j in range(2):
            ctx = seen[i * 2 + j]
            # The context is built ONLY from the fold's train window.
            assert ctx["rows"] == len(fold.train_dataset.data)
            assert pd.Timestamp(ctx["date_end"]) == fold.train_dataset.data.index[-1]
            # And the train window ends before the validation window begins.
            assert pd.Timestamp(ctx["date_end"]) <= pd.Timestamp(
                fold.validation_run_dataset.data.index[fold.warmup_bars - 1]
            )
    assert report.summary.n_valid >= 1


def test_research_no_candidate_when_train_selects_nothing():
    class _AlwaysInvalidProvider(StrategyProposalProvider):
        name = "always-invalid"

        def generate_strategy(self, context):
            spec = DeterministicStrategyProvider().generate_strategy(context)
            return spec.model_copy(update={"symbol": "NSE:NOT_ALLOWED"})

    engine = StrategyResearchEngine(
        _AlwaysInvalidProvider(),
        ResearchConfig(max_candidates=2, min_bars=30,
                       allowed_symbols=frozenset({"NSE:SBIN"})),
    )
    ds = _dataset(n=400)
    report = walk_forward_research(ds, engine, 2, _btc(), _wfc(n_folds=2))
    assert all(f.status == "no_candidate" for f in report.folds)
    assert report.summary.n_valid == 0


def test_research_provider_error_becomes_no_candidate():
    class _BrokenProvider(StrategyProposalProvider):
        name = "broken"

        def generate_strategy(self, context):
            raise ModelProviderError("simulated outage")

    engine = StrategyResearchEngine(_BrokenProvider())
    ds = _dataset(n=400)
    report = walk_forward_research(ds, engine, 2, _btc(), _wfc(n_folds=2))
    assert all(f.status == "no_candidate" for f in report.folds)


# --------------------------------------------------------------------------- #
# Determinism + report
# --------------------------------------------------------------------------- #
def test_walk_forward_deterministic():
    ds = _dataset(n=500, drift=0.1)
    cfg = _wfc(n_folds=3)
    a = walk_forward_validate(ds, _sma_spec(10), _btc(), cfg)
    b = walk_forward_validate(ds, _sma_spec(10), _btc(), cfg)
    assert [f.status for f in a.folds] == [f.status for f in b.folds]
    assert [round(f.validation_evaluation.total_return, 12) for f in a.folds] == [
        round(f.validation_evaluation.total_return, 12) for f in b.folds
    ]
    assert a.summary == b.summary
    assert a.warnings == b.warnings


def test_walk_forward_research_deterministic():
    ds = _dataset(n=500, drift=0.1)
    cfg = _wfc(n_folds=3, warmup_bars=30, min_validation_trades=1)
    engine = _wf_engine()
    a = walk_forward_research(ds, engine, 3, _btc(), cfg)
    b = walk_forward_research(ds, engine, 3, _btc(), cfg)
    assert [f.selected_spec.name if f.selected_spec else None for f in a.folds] == [
        f.selected_spec.name if f.selected_spec else None for f in b.folds
    ]
    assert [f.status for f in a.folds] == [f.status for f in b.folds]
    assert a.summary == b.summary
    assert a.warnings == b.warnings


def test_report_disclaimer_and_kind():
    ds = _dataset(n=400, drift=0.1)
    cfg = _wfc(n_folds=2)
    fixed = walk_forward_validate(ds, _sma_spec(10), _btc(), cfg)
    assert fixed.kind == "fixed_spec"
    assert fixed.spec_name == "SMA trend"
    assert any("future profitability" in n and "NOT" in n for n in fixed.notes)
    assert fixed.whole_dataset_evaluation is not None  # in-sample reference
    research = walk_forward_research(ds, _wf_engine(), 3, _btc(), cfg)
    assert research.kind == "research"
    assert any("future profitability" in n and "NOT" in n
               for n in research.notes)
    # In-sample vs validation are never silently mixed: folds carry both, and
    # the report exposes them separately.
    for fold in research.folds:
        if fold.status == "valid":
            assert fold.train_evaluation is not None
            assert fold.validation_evaluation is not None
            assert fold.validation_start != ""