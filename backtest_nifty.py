"""Thin standalone wrapper that runs a bhav strategy file against the bundled
offline NIFTY 1-minute dataset (`bhav/sample_data/nifty_1y_1min.xlsx`).

It reuses the bhav engine and Excel data source directly — no Upstox token, no
live market data, no `bhav.cli` dependency. This is the canonical offline
entry point for the NIFTY paper backtest.

Usage:
    python backtest_nifty.py --strategy examples/orb_v1.py \
        --start 2025-08-01 --end 2025-08-15 --capital 100_000
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import date
from pathlib import Path

from rich.console import Console
from rich.table import Table

from bhav.data.excel_source import (
    ExcelDataReader,
    ExcelDataSource,
    ExcelInstrumentResolver,
)
from bhav.engine.bar_engine import BarEngine, EngineConfig
from bhav.engine.costs import IndianCostModel
from bhav.metrics.report import compute_metrics

console = Console()

DEFAULT_EXCEL_PATH = (
    Path(__file__).resolve().parent / "bhav" / "sample_data" / "nifty_1y_1min.xlsx"
)
UNDERLYING_KEY = "NSE_INDEX|Nifty 50"
DEFAULT_LOT_SIZE = 65


def _load_strategy(path: Path):
    """Load a strategy module from a .py file and return its `strategy` object.

    Mirrors `bhav.cli._load_strategy`: the file must expose a module-level
    `strategy =` variable.
    """
    spec = importlib.util.spec_from_file_location("user_strategy", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"Could not load strategy from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "strategy"):
        raise AttributeError(f"{path} must expose a module-level `strategy` variable")
    return mod.strategy


def _print_summary(m) -> None:
    """Reuses the summary table style from `bhav.cli._print_summary`."""
    t = Table(title="Summary", show_header=False, border_style="dim")
    t.add_column("k", style="dim")
    t.add_column("v")
    t.add_row("Total return", f"{m.total_return_pct:+.2f}%")
    t.add_row("CAGR", f"{m.cagr_pct:+.2f}%")
    t.add_row("Sharpe", f"{m.sharpe:.2f}")
    t.add_row(
        "Sortino", "inf (no losing bars)" if m.sortino is None else f"{m.sortino:.2f}"
    )
    t.add_row("Max drawdown", f"{m.max_drawdown_pct:.2f}%")
    t.add_row("Trades", f"{m.total_trades} ({m.win_rate_pct:.1f}% win rate)")
    t.add_row(
        "Profit factor",
        "inf (no losses)" if m.profit_factor is None else f"{m.profit_factor:.2f}",
    )
    t.add_row("Total costs", f"Rs {m.total_costs:,.0f}")
    console.print(t)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backtest_nifty",
        description=(
            "Run a bhav strategy file against the bundled offline NIFTY 1-minute "
            "dataset (no Upstox token / live market data required)."
        ),
    )
    parser.add_argument(
        "--strategy",
        required=True,
        type=Path,
        help="Path to a .py strategy file exposing a module-level `strategy` variable.",
    )
    parser.add_argument(
        "--start", required=True, help="Backtest start date, YYYY-MM-DD."
    )
    parser.add_argument(
        "--end", required=True, help="Backtest end date, YYYY-MM-DD."
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=100_000,
        help="Starting capital in Rs (default: 100000).",
    )
    parser.add_argument(
        "--warmup-days",
        type=int,
        default=0,
        help="Trading days before --start to feed the strategy (no trades placed).",
    )
    parser.add_argument(
        "--lot-size",
        type=int,
        default=DEFAULT_LOT_SIZE,
        help=f"NIFTY lot size (default: {DEFAULT_LOT_SIZE}).",
    )
    parser.add_argument(
        "--excel-path",
        type=Path,
        default=DEFAULT_EXCEL_PATH,
        help=f"Path to the NIFTY workbook (default: {DEFAULT_EXCEL_PATH}).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Validate the date range up-front (same contract as `bhav run`).
    start_d = date.fromisoformat(args.start)
    end_d = date.fromisoformat(args.end)
    if start_d > end_d:
        parser.error(f"--start {args.start} is after --end {args.end}")

    if not args.strategy.exists():
        console.print(f"[bold red]Strategy file not found:[/bold red] {args.strategy}")
        return 1
    if not args.excel_path.exists():
        console.print(
            f"[bold red]Excel dataset not found:[/bold red] {args.excel_path}"
        )
        return 1

    strategy = _load_strategy(args.strategy)

    source = ExcelDataSource(args.excel_path)
    reader = ExcelDataReader(source)
    resolver = ExcelInstrumentResolver(source)

    cfg = EngineConfig(
        underlying_key=UNDERLYING_KEY,
        start=start_d,
        end=end_d,
        starting_capital=args.capital,
        lot_size=args.lot_size,
        warmup_days=args.warmup_days,
        atm_reference="spot",
        cost_model=IndianCostModel(),
    )

    engine = BarEngine(cfg, reader, resolver)
    console.print(
        f"[bold]Running[/bold] {strategy.name} from {args.start} to {args.end} "
        f"(underlying={UNDERLYING_KEY}, lot={args.lot_size}, source=excel)..."
    )
    portfolio = engine.run(strategy)

    metrics = compute_metrics(portfolio)
    _print_summary(metrics)
    console.print(
        f"\n[dim]Ending equity: Rs {portfolio.equity_curve[-1][1]:,.0f}[/dim]"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
