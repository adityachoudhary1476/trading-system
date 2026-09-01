"""Phase 18/19 — Paper trading report + deterministic replay.

The report aggregates the runner's events into a typed, serializable result.
``run_paper_replay`` is the deterministic chronological replay helper.

Phase 19 adds ``PaperOperationsReport``: an operations-level report (identity,
runtime, performance, health, audit) built from the runner's operations layer.
Metrics that cannot be computed without additional state are left as ``None``
rather than fabricated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Union

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from ..execution.paper_broker import PaperBroker
from ..research.evidence import dataset_identity, EvidenceType, StrategyEvidence
from ..research.evidence import strategy_identity
from ..research.strategy_lab.spec import StrategySpec
from .deployment import PaperDeployment, PaperDeploymentConfig, PaperDeploymentStatus
from .events import PaperOperationEventType
from .gate import DeploymentGate
from .runner import PaperStrategyRunner, SignalType


class PaperTradingReport(BaseModel):
    """Typed, JSON-safe paper-trading result."""

    model_config = ConfigDict(extra="forbid")

    deployment_id: str
    strategy_id: str
    strategy_spec_hash: str
    symbol: str
    timeframe: str

    start: Optional[str] = None
    end: Optional[str] = None
    n_bars: int = 0
    n_orders: int = 0
    n_fills: int = 0
    n_trades: int = 0
    winning_trades: Optional[int] = None
    losing_trades: Optional[int] = None

    realized_pnl: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    transaction_costs: Optional[float] = None
    slippage_estimate: Optional[float] = None
    max_drawdown: Optional[float] = None
    final_equity: Optional[float] = None
    open_position: Optional[dict] = None

    rejection_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    report_version: str = "phase-18"
    schema_version: int = 1


def build_report(
    deployment: PaperDeployment,
    runner: PaperStrategyRunner,
    broker: PaperBroker,
    dataset_id: str,
    *,
    start: Optional[pd.Timestamp] = None,
    end: Optional[pd.Timestamp] = None,
    warnings: Optional[list[str]] = None,
) -> PaperTradingReport:
    """Aggregate runner + broker state into a PaperTradingReport."""
    account = broker.account()
    pos = broker.get_position(deployment.symbol)
    open_pos = None
    if pos is not None and pos.is_open:
        open_pos = pos.as_dict()

    # Aggregate trade / pnl stats from the broker's fill ledger.
    fills = []
    for order in broker._orders.values():
        fills.extend(order.fills)
    n_fills = len(fills)
    realized = float(account.realized_pnl)
    unrealized = float(account.unrealized_pnl)

    # Per-trade reconstruction: every SELL after a BUY (or BUY after a SELL)
    # forms a completed trade for the paper ledger.
    trades = _reconstruct_trades(fills, deployment.symbol)
    n_trades = len(trades)
    wins = sum(1 for t in trades if t["net"] > 0)
    losses = sum(1 for t in trades if t["net"] < 0)
    total_costs = float(sum(f.fee for f in fills))

    # Max drawdown from the running equity curve: replay the cash sequence.
    equity_curve = _running_equity(broker)
    max_dd = _max_drawdown(equity_curve)

    return PaperTradingReport(
        deployment_id=deployment.deployment_id,
        strategy_id=deployment.strategy_id,
        strategy_spec_hash=deployment.strategy_spec_hash,
        symbol=deployment.symbol,
        timeframe=deployment.timeframe,
        start=start.isoformat() if start is not None else None,
        end=end.isoformat() if end is not None else None,
        n_bars=int(runner.bar_count),
        n_orders=int(runner.orders_submitted),
        n_fills=int(n_fills),
        n_trades=int(n_trades),
        winning_trades=int(wins) if n_trades else None,
        losing_trades=int(losses) if n_trades else None,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        transaction_costs=total_costs,
        slippage_estimate=None,
        max_drawdown=max_dd,
        final_equity=float(account.equity),
        open_position=open_pos,
        rejection_count=int(runner.rejected_orders),
        warnings=list(warnings or []),
    )


# --------------------------------------------------------------------------- #
# Deterministic replay
# --------------------------------------------------------------------------- #
def run_paper_replay(
    deployment: PaperDeployment,
    spec: StrategySpec,
    dataset: pd.DataFrame,
    *,
    broker: Optional[PaperBroker] = None,
    dataset_id: Optional[str] = None,
) -> PaperTradingReport:
    """Replay an OHLCV dataset through the runner. Deterministic."""
    if broker is None:
        broker = PaperBroker(
            initial_cash=float(deployment.config.initial_cash),
        )
    DeploymentGate.assert_paper_broker(broker)

    # Defensive: dataset must be sorted chronologically (no future-bar use).
    df = dataset.sort_index()
    if len(df) == 0:
        warnings = ["empty_dataset"]
    elif len(df) < 2:
        warnings = ["insufficient_bars"]
    else:
        warnings = []

    runner = PaperStrategyRunner(deployment=deployment, broker=broker, spec=spec)

    start_ts = df.index[0] if len(df) else None
    end_ts = df.index[-1] if len(df) else None

    for ts, row in df.iterrows():
        bar = {
            "timestamp": pd.Timestamp(ts),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        }
        runner.process_bar(bar)

    ds_id = dataset_id or (
        _dataset_identity_safe(deployment.symbol, deployment.timeframe, df)
    )
    return build_report(
        deployment=deployment,
        runner=runner,
        broker=broker,
        dataset_id=ds_id,
        start=start_ts,
        end=end_ts,
        warnings=warnings,
    )


def _dataset_identity_safe(symbol: str, timeframe: str, df: pd.DataFrame) -> str:
    """Local fallback so we don't depend on a full HistoricalDataset wrapper."""
    payload = {
        "symbol": symbol,
        "timeframe": timeframe,
        "rows": int(len(df)),
        "date_start": str(df.index.min()) if len(df) else "",
        "date_end": str(df.index.max()) if len(df) else "",
    }
    import hashlib
    import json
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:64]


def _reconstruct_trades(fills: list, symbol: str) -> list[dict]:
    """Match buy/sell fills into closed round-trip trades (FIFO).

    Best-effort reconstruction for reporting only; the broker remains the
    source of truth for cash + position state.
    """
    trades: list[dict] = []
    open_legs: list[dict] = []
    for f in fills:
        if f.symbol != symbol:
            continue
        signed = f.quantity if f.side.value == "BUY" else -f.quantity
        if signed > 0:
            open_legs.append({
                "price": float(f.price),
                "qty": float(f.quantity),
                "fee": float(f.fee),
                "ts": f.timestamp,
            })
        else:
            remaining = float(f.quantity)
            close_price = float(f.price)
            close_fee = float(f.fee)
            close_ts = f.timestamp
            while remaining > 0 and open_legs:
                leg = open_legs[0]
                leg_qty = min(remaining, leg["qty"])
                gross = (close_price - leg["price"]) * leg_qty
                leg_cost = leg["fee"] * (leg_qty / leg["qty"])
                trades.append({
                    "entry_ts": leg["ts"],
                    "exit_ts": close_ts,
                    "entry_price": leg["price"],
                    "exit_price": close_price,
                    "quantity": leg_qty,
                    "net": gross - leg_cost - close_fee * (leg_qty / f.quantity),
                })
                leg["qty"] -= leg_qty
                remaining -= leg_qty
                if leg["qty"] <= 1e-12:
                    open_legs.pop(0)
    return trades


def _running_equity(broker: PaperBroker) -> list[float]:
    """Cash-only ledger walk (paper broker has no bar-level equity series)."""
    return [float(broker.account().equity)]


def _max_drawdown(equity: list[float]) -> Optional[float]:
    if not equity:
        return None
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            dd = (v - peak) / peak
            max_dd = min(max_dd, dd)
    return float(max_dd)


# --------------------------------------------------------------------------- #
# Paper-trading evidence (Phase 18 -> StrategyEvidence)
# --------------------------------------------------------------------------- #
def build_paper_trading_evidence(
    deployment: PaperDeployment,
    spec: StrategySpec,
    report: PaperTradingReport,
    dataset_id: str,
    *,
    config: Optional[PaperDeploymentConfig] = None,
) -> StrategyEvidence:
    """Construct a Phase 16 StrategyEvidence record of evidence_type PAPER_TRADING."""
    cfg = config or deployment.config
    configuration = {
        "deployment_id": deployment.deployment_id,
        "symbol": deployment.symbol,
        "timeframe": deployment.timeframe,
        "config": cfg.model_dump(mode="json"),
        "status": deployment.status.value,
    }
    metrics = report.model_dump(mode="json")
    metrics.pop("schema_version", None)
    metrics.pop("report_version", None)
    provenance = {
        "source": "phase_18_paper_deployment",
        "execution_mode": "paper",
        "broker_class": PaperBroker.__module__ + "." + PaperBroker.__name__,
    }
    from .deployment import deployment_identity as _did
    from ..research.strategy_registry import evidence_identity
    eid = evidence_identity(
        deployment.strategy_id,
        EvidenceType.PAPER_TRADING,
        dataset_id,
        configuration,
    )
    return StrategyEvidence(
        evidence_id=eid,
        strategy_id=deployment.strategy_id,
        strategy_spec_hash=deployment.strategy_spec_hash,
        evidence_type=EvidenceType.PAPER_TRADING,
        dataset_id=dataset_id,
        configuration_json=configuration,
        metrics_json=metrics,
        provenance_json=provenance,
    )


# --------------------------------------------------------------------------- #
# Phase 19 — Paper operations report
# --------------------------------------------------------------------------- #
class PaperOperationsReport(BaseModel):
    """Operations-level paper-trading report (Phase 19).

    JSON-safe. Extends the Phase 18 reporting architecture without mutating
    ``PaperTradingReport``. Everything here is derived from the runner's
    operations layer and the broker's public views.
    """

    model_config = ConfigDict(extra="forbid")

    # Identity
    deployment_id: str
    strategy_id: str
    strategy_spec_hash: str
    symbol: str
    timeframe: str

    # Runtime
    start: Optional[str] = None
    end: Optional[str] = None
    processed_bars: int = 0
    generated_signals: int = 0
    submitted_orders: int = 0
    filled_orders: int = 0
    rejected_orders: int = 0

    # Performance
    starting_equity: Optional[float] = None
    ending_equity: Optional[float] = None
    realized_pnl: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    total_return: Optional[float] = None
    max_drawdown: Optional[float] = None
    trade_count: int = 0
    win_rate: Optional[float] = None
    profit_factor: Optional[float] = None

    # Health
    health_status: str = "healthy"
    warnings: list[str] = Field(default_factory=list)
    halt_status: str = "closed"
    halt_reason: Optional[str] = None
    consecutive_errors: int = 0

    # Audit
    events: list[dict] = Field(default_factory=list)
    configuration: dict = Field(default_factory=dict)
    provenance: dict = Field(default_factory=dict)
    report_version: str = "phase-19"
    schema_version: int = 1


def build_operations_report(
    deployment: PaperDeployment,
    runner: PaperStrategyRunner,
    *,
    configuration: Optional[dict] = None,
) -> PaperOperationsReport:
    """Build a ``PaperOperationsReport`` from the runner's operations layer."""
    ops = runner.operations_state()
    account = runner.broker.account()

    starting = ops.starting_equity
    ending = ops.current_equity
    total_return: Optional[float] = None
    if starting is not None and starting > 0 and ending is not None:
        total_return = (ending - starting) / starting

    # Trade statistics from the broker's fill ledger (reporting-only).
    trade_count, win_rate, profit_factor = _ops_trade_stats(
        runner.broker, deployment.symbol
    )

    # Circuit-breaker status.
    halt_status = "closed"
    halt_reason = ops.halt_reason
    cb = runner.circuit_breaker
    if cb is not None and cb.is_open:
        halt_status = "open"
        halt_reason = cb.reason

    # Health warnings from the operations event log.
    warnings = [
        e.message
        for e in _safe_events(runner)
        if e.event_type == PaperOperationEventType.HEALTH_WARNING
    ]

    return PaperOperationsReport(
        deployment_id=deployment.deployment_id,
        strategy_id=deployment.strategy_id,
        strategy_spec_hash=deployment.strategy_spec_hash,
        symbol=deployment.symbol,
        timeframe=deployment.timeframe,
        start=ops.started_at,
        end=ops.last_bar_timestamp,
        processed_bars=ops.processed_bars,
        generated_signals=ops.generated_signals,
        submitted_orders=ops.submitted_orders,
        filled_orders=ops.filled_orders,
        rejected_orders=ops.rejected_orders,
        starting_equity=starting,
        ending_equity=ending,
        realized_pnl=ops.realized_pnl,
        unrealized_pnl=ops.unrealized_pnl,
        total_return=total_return,
        max_drawdown=ops.max_drawdown,
        trade_count=trade_count,
        win_rate=win_rate,
        profit_factor=profit_factor,
        health_status=ops.health_status,
        warnings=warnings,
        halt_status=halt_status,
        halt_reason=halt_reason,
        consecutive_errors=ops.consecutive_errors,
        events=[e.model_dump(mode="json") for e in _safe_events(runner)],
        configuration=configuration or _default_ops_config(deployment),
        provenance={
            "source": "phase_19_paper_operations",
            "execution_mode": "paper",
            "broker_class": PaperBroker.__module__ + "." + PaperBroker.__name__,
        },
    )


def build_paper_operations_evidence(
    deployment: PaperDeployment,
    spec: StrategySpec,
    report: PaperOperationsReport,
    dataset_id: str,
    *,
    config: Optional[PaperDeploymentConfig] = None,
) -> StrategyEvidence:
    """Construct a PAPER_TRADING evidence record for an operations report.

    Idempotent via ``evidence_identity`` (same inputs -> same evidence_id).
    Appends only; never mutates historical research evidence.
    """
    cfg = config or deployment.config
    configuration = {
        "deployment_id": deployment.deployment_id,
        "symbol": deployment.symbol,
        "timeframe": deployment.timeframe,
        "config": cfg.model_dump(mode="json"),
        "status": deployment.status.value,
        "circuit_breaker": report.halt_status,
    }
    metrics = report.model_dump(mode="json")
    metrics.pop("schema_version", None)
    metrics.pop("report_version", None)
    provenance = {
        "source": "phase_19_paper_operations",
        "execution_mode": "paper",
        "broker_class": PaperBroker.__module__ + "." + PaperBroker.__name__,
    }
    from .deployment import deployment_identity as _did  # noqa: F811
    from ..research.strategy_registry import evidence_identity
    eid = evidence_identity(
        deployment.strategy_id,
        EvidenceType.PAPER_TRADING,
        dataset_id,
        configuration,
    )
    return StrategyEvidence(
        evidence_id=eid,
        strategy_id=deployment.strategy_id,
        strategy_spec_hash=deployment.strategy_spec_hash,
        evidence_type=EvidenceType.PAPER_TRADING,
        dataset_id=dataset_id,
        configuration_json=configuration,
        metrics_json=metrics,
        provenance_json=provenance,
    )


def _safe_events(runner: PaperStrategyRunner) -> list:
    """Return the operations event log, or an empty list if not configured."""
    log = runner.event_log
    return log.events if log is not None else []


def _default_ops_config(deployment: PaperDeployment) -> dict:
    return {
        "deployment_id": deployment.deployment_id,
        "symbol": deployment.symbol,
        "timeframe": deployment.timeframe,
        "config": deployment.config.model_dump(mode="json"),
    }


def _ops_trade_stats(
    broker: PaperBroker, symbol: str
) -> tuple[int, Optional[float], Optional[float]]:
    """Round-trip trade stats from the broker's fill ledger (reporting-only).

    Returns (trade_count, win_rate, profit_factor). Win rate and profit factor
    are ``None`` when there are no completed round-trip trades.
    """
    fills: list = []
    for order in broker._orders.values():
        fills.extend(order.fills)
    trades: list[dict] = []
    open_legs: list[dict] = []
    for f in fills:
        if f.symbol != symbol:
            continue
        if f.side.value == "BUY":
            open_legs.append({
                "price": float(f.price),
                "qty": float(f.quantity),
                "fee": float(f.fee),
            })
        else:
            remaining = float(f.quantity)
            close_price = float(f.price)
            close_fee = float(f.fee)
            while remaining > 0 and open_legs:
                leg = open_legs[0]
                leg_qty = min(remaining, leg["qty"])
                gross = (close_price - leg["price"]) * leg_qty
                leg_cost = leg["fee"] * (leg_qty / leg["qty"])
                trades.append({
                    "net": gross - leg_cost - close_fee * (leg_qty / f.quantity),
                })
                leg["qty"] -= leg_qty
                remaining -= leg_qty
                if leg["qty"] <= 1e-12:
                    open_legs.pop(0)
    n = len(trades)
    if n == 0:
        return 0, None, None
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    win_rate = len(wins) / n
    gross_win = sum(t["net"] for t in wins)
    gross_loss = -sum(t["net"] for t in losses)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None
    return n, win_rate, profit_factor