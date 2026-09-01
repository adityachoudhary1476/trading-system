"""Phase 13 tests: deterministic StrategySpec interpreter.

Every test is offline and deterministic. Key invariants exercised:
  * signals are in {-1, 0, +1} (existing Strategy ABC contract),
  * no look-ahead (appending future bars cannot change past targets),
  * warmup NaNs never trigger entries,
  * long/short permissions are respected,
  * specs integrate with the UNCHANGED backtester (stops/TP via RiskConfig).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_system.research.backtester import BacktestConfig, run_backtest
from trading_system.research.dataset import HistoricalDataset
from trading_system.research.strategy_lab.interpreter import (
    InterpreterError,
    SpecStrategy,
    build_strategy,
    compute_indicators,
)
from trading_system.research.strategy_lab.spec import (
    StrategySpec,
    const_operand,
    field_operand,
    indicator_operand,
    logic,
    make_condition,
    not_,
)
from trading_system.research.strategy_lab.validation import validate_spec


# --------------------------------------------------------------------------- #
# Deterministic data helpers
# --------------------------------------------------------------------------- #
def _df(n=300, seed=1, trend=0.0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    close = 100 + np.cumsum(rng.normal(trend, 1.0, n))
    close = np.maximum(close, 1.0)
    open_ = close + rng.normal(0, 0.3, n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.5, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.5, n))
    vol = rng.integers(100, 1000, n).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def _dataset(df=None, symbol="NSE:SBIN"):
    return HistoricalDataset(
        symbol=symbol, timeframe="1d", data=df if df is not None else _df()
    )


def _spec(**overrides) -> StrategySpec:
    payload = {
        "name": "Spec test",
        "description": "test",
        "symbol": "NSE:SBIN",
        "timeframe": "1d",
        "indicators": [{"name": "sma", "params": {"window": 20}}],
        "entry": make_condition(
            field_operand("close"), ">", indicator_operand("sma_20")
        ),
        "risk": {},
    }
    payload.update(overrides)
    return StrategySpec(**payload)


# --------------------------------------------------------------------------- #
# Basic condition evaluation
# --------------------------------------------------------------------------- #
def test_basic_comparison_threshold():
    df = _df()
    spec = _spec()
    target = build_strategy(spec).generate(df)
    above = (df["close"] > df["close"].rolling(20).mean()).fillna(False)
    assert set(target[above].unique()) <= {0, 1}
    assert (target >= 0).all()  # long-only spec never shorts


def test_entry_then_exit_cycle():
    df = _df()
    spec = _spec(
        exit=make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
    )
    target = build_strategy(spec).generate(df)
    assert (target.iloc[20:] == 1).any()
    assert (target.iloc[20:] == 0).any()
    long_bars = target[target == 1].index
    sma = df["close"].rolling(20).mean()
    assert (df.loc[long_bars, "close"] >= sma.loc[long_bars] * 0.999).all()


def test_crossover_fires_on_cross():
    df = _df()
    spec = _spec(
        indicators=[
            {"name": "ema", "params": {"window": 12}},
            {"name": "ema", "params": {"window": 26}},
        ],
        entry=make_condition(
            indicator_operand("ema_12"), "crosses_above", indicator_operand("ema_26")
        ),
        exit=make_condition(
            indicator_operand("ema_12"), "crosses_below", indicator_operand("ema_26")
        ),
    )
    target = build_strategy(spec).generate(df)
    fe = df["close"].ewm(span=12, adjust=False, min_periods=12).mean()
    se = df["close"].ewm(span=26, adjust=False, min_periods=26).mean()
    cross_up = ((fe > se) & (fe.shift(1) <= se.shift(1))).fillna(False)
    assert cross_up.any()
    for ts in cross_up[cross_up].index[1:]:
        assert target.loc[ts] in (0, 1)


def test_crossunder_symmetry():
    df = _df()
    spec = _spec(
        indicators=[
            {"name": "ema", "params": {"window": 12}},
            {"name": "ema", "params": {"window": 26}},
        ],
        entry=make_condition(
            indicator_operand("ema_12"), "crosses_below", indicator_operand("ema_26")
        ),
    )
    target = build_strategy(spec).generate(df)
    fe = df["close"].ewm(span=12, adjust=False, min_periods=12).mean()
    se = df["close"].ewm(span=26, adjust=False, min_periods=26).mean()
    cross_down = ((fe < se) & (fe.shift(1) >= se.shift(1))).fillna(False)
    assert cross_down.any()
    # Long-only spec: crossunder entries are ignored (entry is for LONGS).
    assert (target == -1).sum() == 0


def test_and_logic():
    df = _df()
    spec = _spec(
        indicators=[
            {"name": "sma", "params": {"window": 20}},
            {"name": "rsi", "params": {"window": 14}},
        ],
        entry=logic(
            "AND",
            make_condition(field_operand("close"), ">", indicator_operand("sma_20")),
            make_condition(indicator_operand("rsi_14"), ">", const_operand(45.0)),
        ),
    )
    target = build_strategy(spec).generate(df)
    assert set(target.unique()) <= {0, 1}


def test_or_logic():
    df = _df()
    spec = _spec(
        entry=logic(
            "OR",
            make_condition(field_operand("close"), ">", const_operand(1e9)),
            make_condition(field_operand("close"), "<", indicator_operand("sma_20")),
        ),
    )
    target = build_strategy(spec).generate(df)
    sma = df["close"].rolling(20).mean()
    below = (df["close"] < sma).fillna(False)
    assert below.any()
    assert (target[below] == 1).all()


def test_not_logic():
    df = _df()
    spec = _spec(
        entry=not_(
            make_condition(field_operand("close"), "<", indicator_operand("sma_20"))
        ),
    )
    target = build_strategy(spec).generate(df)
    sma = df["close"].rolling(20).mean()
    not_below = (~(df["close"] < sma)).fillna(False)
    entries = (target == 1) & (target.shift(1, fill_value=0) == 0)
    assert (entries[~not_below] == False).all()  # noqa: E712 - explicit boolean check


def test_comparison_operators_lte_gte_eq():
    df = _df(120)
    for op in (">=", "<=", "=="):
        spec = _spec(
            entry=make_condition(field_operand("close"), op, const_operand(50.0))
        )
        target = build_strategy(spec).generate(df)
        assert set(target.unique()) <= {-1, 0, 1}


# --------------------------------------------------------------------------- #
# Direction permissions
# --------------------------------------------------------------------------- #
def _cross_specs():
    indicators = [
        {"name": "ema", "params": {"window": 12}},
        {"name": "ema", "params": {"window": 26}},
    ]
    entry = make_condition(
        indicator_operand("ema_12"), "crosses_above", indicator_operand("ema_26")
    )
    entry_short = make_condition(
        indicator_operand("ema_12"), "crosses_below", indicator_operand("ema_26")
    )
    return indicators, entry, entry_short


def test_long_only_never_short():
    target = build_strategy(_spec()).generate(_df())
    assert (target != -1).all()


def test_short_enabled_produces_short_state():
    indicators, entry, entry_short = _cross_specs()
    spec = _spec(
        indicators=indicators,
        entry=entry,
        entry_short=entry_short,
        risk={"allow_short": True},
    )
    target = build_strategy(spec).generate(_df(seed=3))
    assert (target == -1).any()
    assert (target == 1).any()


def test_short_entry_ignored_without_permission():
    indicators, entry, entry_short = _cross_specs()
    df = _df(seed=3)
    spec_short = _spec(
        indicators=indicators, entry=entry, entry_short=entry_short,
        risk={"allow_short": True},
    )
    spec_long = _spec(indicators=indicators, entry=entry)
    assert (build_strategy(spec_long).generate(df) == -1).sum() == 0
    assert (build_strategy(spec_short).generate(df) == -1).sum() > 0


# --------------------------------------------------------------------------- #
# Warmup / data sufficiency / determinism
# --------------------------------------------------------------------------- #
def test_warmup_bars_never_enter():
    df = _df()
    spec = _spec(
        indicators=[{"name": "sma", "params": {"window": 50}}],
        entry=make_condition(field_operand("close"), ">", indicator_operand("sma_50")),
    )
    target = build_strategy(spec).generate(df)
    assert (target.iloc[:49] == 0).all()


def test_insufficient_data_rejected_by_validation():
    ds = _dataset(_df(30))
    spec = _spec(indicators=[{"name": "sma", "params": {"window": 50}}])
    errors = validate_spec(spec, ds, min_bars=50)
    assert any("insufficient" in e for e in errors)


def test_deterministic_output():
    df = _df()
    spec = _spec()
    t1 = build_strategy(spec).generate(df)
    t2 = build_strategy(spec).generate(df)
    pd.testing.assert_series_equal(t1, t2)


def test_no_lookahead_future_bars_do_not_change_past_targets():
    df = _df(200)
    base = build_strategy(_spec()).generate(df)
    extended = pd.concat([df, _df(50, seed=99)], axis=0)
    ext = build_strategy(_spec()).generate(extended)
    pd.testing.assert_series_equal(base, ext.iloc[:200], check_freq=False)


def test_missing_column_raises():
    with pytest.raises(InterpreterError):
        build_strategy(_spec()).generate(_df().drop(columns=["volume"]))


def test_build_strategy_rejects_non_spec():
    with pytest.raises(InterpreterError):
        build_strategy({"entry": "not a spec"})


def test_interpreter_uses_existing_indicator_module():
    df = _df()
    indicators = compute_indicators(_spec(), df)
    expected = df["close"].rolling(20, min_periods=20).mean()
    pd.testing.assert_series_equal(indicators["sma_20"], expected, check_names=False)


# --------------------------------------------------------------------------- #
# Backtester integration (UNCHANGED backtester)
# --------------------------------------------------------------------------- #
def test_integration_with_existing_backtester():
    ds = _dataset()
    spec = _spec(risk={"stop_loss_pct": 0.05})
    result = run_backtest(
        ds, build_strategy(spec), BacktestConfig(initial_capital=100_000)
    )
    assert result.final_capital > 0
    for t in result.trades:
        assert t.direction == 1  # long-only spec


def test_stop_loss_enforced_via_risk_config():
    indicators, entry, _ = _cross_specs()
    ds = _dataset(_df(500, seed=5))
    # Crossover entry with NO exit condition: positions are held until the
    # risk stop fires. Spec risk reaches the backtester the same way the
    # engine does it (merged_backtest_config).
    from trading_system.research.strategy_lab.engine import merged_backtest_config

    spec = _spec(
        indicators=indicators, entry=entry, risk={"stop_loss_pct": 0.01}
    )
    cfg = merged_backtest_config(spec, BacktestConfig(initial_capital=100_000))
    assert cfg.risk.stop_loss_pct == 0.01
    result = run_backtest(ds, build_strategy(spec), cfg)
    assert any(t.exit_reason == "stop_loss" for t in result.trades)


def test_take_profit_enforced_via_risk_config():
    indicators, entry, _ = _cross_specs()
    ds = _dataset(_df(500, seed=5))
    from trading_system.research.strategy_lab.engine import merged_backtest_config

    spec = _spec(
        indicators=indicators, entry=entry, risk={"take_profit_pct": 0.02}
    )
    cfg = merged_backtest_config(spec, BacktestConfig(initial_capital=100_000))
    result = run_backtest(ds, build_strategy(spec), cfg)
    assert any(t.exit_reason == "take_profit" for t in result.trades)


def test_spec_strategy_is_a_strategy_abc_instance():
    from trading_system.research.strategies import Strategy

    strategy = build_strategy(_spec())
    assert isinstance(strategy, Strategy)
    assert isinstance(strategy, SpecStrategy)
    assert strategy.meta.name == "Spec test"
    assert strategy.params["symbol"] == "NSE:SBIN"


def test_existing_strategies_still_work_alongside():
    from trading_system.research.strategies import (
        BreakoutStrategy,
        EMATrendStrategy,
        MomentumStrategy,
    )

    ds = _dataset()
    cfg = BacktestConfig(initial_capital=100_000)
    for strategy in (EMATrendStrategy(), MomentumStrategy(), BreakoutStrategy()):
        result = run_backtest(ds, strategy, cfg)
        assert result.final_capital > 0


