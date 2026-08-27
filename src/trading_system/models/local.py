"""Local, offline, deterministic ModelProvider.

This is a REAL implementation of the ModelProvider interface that requires no API
key, no network, and no GPU. It derives a MarketView from the snapshot's
indicators using fixed, auditable rules. It is used as the Day 2 reference
provider and as the guaranteed-testable path.

IMPORTANT: it is a deterministic heuristic for architecture validation, NOT a
trading model. It exists so the full pipeline (snapshot -> view -> signal) runs
end-to-end offline and is unit-testable. A genuine LLM can be dropped in later
via the OpenAICompatibleProvider without changing any downstream code.
"""
from __future__ import annotations

from ..models.base import ModelProvider, ModelProviderError
from ..models.snapshot import MarketSnapshot
from ..models.market_view import MarketView, MarketViewEnum


class LocalRuleModel(ModelProvider):
    name = "local-rule"

    def analyze(self, snapshot: MarketSnapshot) -> MarketView:
        bullish: list[str] = []
        bearish: list[str] = []

        # Trend via price vs SMA20.
        if snapshot.price_vs_sma20 is not None:
            if snapshot.price_vs_sma20 > 0.02:
                bullish.append(f"price {snapshot.price_vs_sma20*100:.1f}% above SMA20 (uptrend)")
            elif snapshot.price_vs_sma20 < -0.02:
                bearish.append(f"price {abs(snapshot.price_vs_sma20)*100:.1f}% below SMA20 (downtrend)")

        # Momentum via RSI.
        if snapshot.rsi_14 is not None:
            if snapshot.rsi_14 >= 70:
                bearish.append(f"RSI {snapshot.rsi_14:.1f} overbought")
            elif snapshot.rsi_14 <= 30:
                bullish.append(f"RSI {snapshot.rsi_14:.1f} oversold")
            elif snapshot.rsi_14 >= 55:
                bullish.append(f"RSI {snapshot.rsi_14:.1f} momentum positive")
            elif snapshot.rsi_14 <= 45:
                bearish.append(f"RSI {snapshot.rsi_14:.1f} momentum weak")

        # MACD.
        if snapshot.macd is not None and snapshot.macd_signal is not None:
            if snapshot.macd > snapshot.macd_signal:
                bullish.append("MACD above signal line")
            else:
                bearish.append("MACD below signal line")

        # Volatility context.
        if snapshot.volatility_annualized is not None:
            if snapshot.volatility_annualized > 0.8:
                bearish.append(f"high volatility {snapshot.volatility_annualized*100:.0f}% annualized")

        # Decision.
        score = len(bullish) - len(bearish)
        if score >= 2:
            view = MarketViewEnum.BULLISH
        elif score <= -2:
            view = MarketViewEnum.BEARISH
        else:
            view = MarketViewEnum.NEUTRAL

        conf = min(1.0, 0.5 + abs(score) * 0.15)
        factors = bullish if view == MarketViewEnum.BULLISH else bearish
        summary = (
            f"Local rule model: {len(bullish)} bullish / {len(bearish)} bearish signals; "
            f"net score {score}."
        )
        try:
            return MarketView(
                symbol=snapshot.symbol,
                timeframe=snapshot.timeframe,
                market_view=view,
                confidence=conf,
                reasoning_summary=summary,
                bullish_factors=bullish,
                bearish_factors=bearish,
                risks=[
                    "Heuristic only; not a predictive model.",
                    "Ignores news, order flow, and regime changes.",
                ],
                invalidating_conditions=[
                    "A break of structure invalidates the current read.",
                ],
                model=self.name,
            )
        except Exception as e:  # pydantic validation failure
            raise ModelProviderError(f"local model produced invalid view: {e}")
