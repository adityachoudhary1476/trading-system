"""Provider-independent risk layer (Day 7 research).

Risk config is applied CONSISTENTLY by the backtester. Default values are
conservative (long-only, no leverage, no position-level stops unless explicitly
set). No risk parameter is silently ignored — if a value is set, the engine uses it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RiskConfig:
    # --- position sizing ---
    max_position_size: Optional[float] = None  # max units per position
    max_allocation_pct: float = 1.0           # max fraction of equity in one position
    allow_short: bool = False                 # must be True to take SHORT signals
    leverage: float = 1.0                     # 1.0 = no leverage; applied only if >1 set

    # --- per-trade exits ---
    stop_loss_pct: Optional[float] = None     # exit if adverse move >= this (frac)
    take_profit_pct: Optional[float] = None   # exit if favorable move >= this (frac)

    # --- portfolio limits ---
    max_loss_per_trade_pct: Optional[float] = None  # cap notional loss per trade
    max_positions: int = 1                    # simultaneous positions (1 = one symbol)

    def effective_leverage(self) -> float:
        return self.leverage if self.leverage and self.leverage > 0 else 1.0
