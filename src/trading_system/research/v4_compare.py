"""V4 walk-forward comparison: V3 vs V4 variants (research-only).

Measures — never assumes — whether news and historical patterns help.
"Historical pattern statistics describe past observations and do not
guarantee future outcomes."
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from .intelligence import MarketIntelligenceEngine, _build_instrument_context
from .intelligence_v3 import build_evidence_ledger_v2
from .news_intelligence import (
    NewsPipelineResult, build_news_context, news_to_evidence,
)
from .patterns import (
    FeatureWeights, HistoricalPatternEngine, build_pattern_report,
    fingerprint_from_features, pattern_to_evidence, _wilson,
)

CONFIGS = ("V3", "V4_technical", "V4_technical_news", "V4_full")


def _bias_of(direction_value: str) -> str:
    return {"long": "bullish", "short": "bearish"}.get(direction_value, "neutral")


def _vote_direction(ledger) -> str:
    """Deterministic direction vote from supported evidence weights."""
    bull = sum(i.effective_weight for i in ledger.supported
               if i.direction == "bullish")
    bear = sum(i.effective_weight for i in ledger.supported
               if i.direction == "bearish")
    if bull > bear * 1.1:
        return "bullish"
    if bear > bull * 1.1:
        return "bearish"
    return "neutral"


@dataclass
class StrategyMetrics:
    name: str = ""
    n: int = 0
    trades: int = 0
    accuracy: Optional[float] = None
    precision: Optional[float] = None
    recall: Optional[float] = None
    win_rate: Optional[float] = None
    expectancy: Optional[float] = None
    profit_factor: Optional[float] = None
    avg_return: Optional[float] = None
    max_drawdown: Optional[float] = None
    sharpe: Optional[float] = None
    ci_low: float = 0.0
    ci_high: float = 1.0
    note: str = ""


def _metrics(name: str, rows: list[tuple[str, float]]) -> StrategyMetrics:
    """rows: (bias, realized_return_pct). Honest small-sample notes."""
    m = StrategyMetrics(name=name, n=len(rows))
    if not rows:
        m.note = "no forecasts"
        return m
    taken = [(b, r) for b, r in rows if b in ("bullish", "bearish")]
    m.trades = len(taken)
    correct = sum(1 for b, r in rows
                  if (b == "bullish" and r > 0)
                  or (b == "bearish" and r < 0)
                  or (b == "neutral" and abs(r) <= 0.25))
    m.accuracy = correct / len(rows)
    if taken:
        wins = [r for b, r in taken
                if (b == "bullish" and r > 0) or (b == "bearish" and r < 0)]
        losses = [r for b, r in taken if (b == "bullish" and r <= 0)
                  or (b == "bearish" and r >= 0)]
        m.win_rate = len(wins) / len(taken)
        m.ci_low, m.ci_high = _wilson(m.win_rate, len(taken))
        m.expectancy = sum(r for _b, r in taken) / len(taken)
        m.avg_return = m.expectancy
        gross = sum(wins)
        drag = abs(sum(r for r in losses if r < 0))
        m.profit_factor = (gross / drag) if drag > 0 else None
        rets = [r for _b, r in taken]
        mean = sum(rets) / len(rets)
        var = (sum((x - mean) ** 2 for x in rets) / len(rets)) ** 0.5
        m.sharpe = (mean / var) if var > 0 else None
        equity = peak = 0.0
        dd = 0.0
        for _b, r in taken:
            equity += r
            peak = max(peak, equity)
            dd = min(dd, equity - peak)
        m.max_drawdown = dd
        ups = [r for _b, r in taken if r > 0]
        if ups:
            m.precision = (sum(1 for b, r in taken if b == "bullish" and r > 0)
                           / len(ups))
        realized_up = sum(1 for _b, r in taken if r > 0)
        if realized_up:
            m.recall = (sum(1 for b, r in taken if b == "bullish" and r > 0)
                        / realized_up)
    if m.trades < 30:
        m.note = f"insufficient sample: {m.trades} trades (<30)"
    return m

def compare_strategies(
    df: pd.DataFrame,
    news_result: Optional[NewsPipelineResult] = None,
    symbol: str = "NSE:DEMO-EQ",
    horizons: Optional[dict[str, int]] = None,
    step: int = 5,
    start: int = 80,
    min_similarity: float = 0.80,
    min_pattern_matches: int = 12,
) -> dict[str, StrategyMetrics]:
    """Walk-forward V3 vs V4 comparison over identical steps.

    At each timestamp T:
      - fingerprints/features use ONLY data <= T
      - the pattern library holds only pre-T states whose outcome window
        has closed by T (enforced by find_matches(horizon=...))
      - news considered only if published_at <= T
    """
    horizons = horizons or {"1D": 5}
    h_name, h_bars = next(iter(horizons.items()))
    n = len(df)
    spacing = (df.index[-1] - df.index[0]) / max(1, n - 1)
    horizon_delta = spacing * h_bars
    engine = MarketIntelligenceEngine(lookback=60)
    ticker = symbol.split(":")[-1].split("-")[0].upper()
    tech_groups = {"price", "trend", "momentum", "volatility", "volume"}
    pat_tech = HistoricalPatternEngine(weights=FeatureWeights().masked(tech_groups))
    pat_full = HistoricalPatternEngine(weights=FeatureWeights())
    rows: dict[str, list[tuple[str, float]]] = {c: [] for c in CONFIGS}

    for t in range(start, n - h_bars, step):
        idx_ts = df.index[t]
        as_of = (idx_ts.to_pydatetime() if hasattr(idx_ts, "to_pydatetime")
                 else datetime.fromisoformat(str(idx_ts)))
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        window = df.iloc[:t + 1]
        r3 = engine.analyze(symbol, "1d", window)
        if r3["status"] != "OK":
            continue
        cand = r3["signal_candidate"]
        feats, regime = r3["features"], r3["regime"]
        v3_bias = _bias_of(cand.direction.value)
        fwd_ret = (float(df["close"].iloc[t + h_bars])
                   / float(df["close"].iloc[t]) - 1) * 100

        lib = pat_tech.build_library(df, instrument=symbol, start=start,
                                     step=2, as_of=as_of - horizon_delta)
        fp = fingerprint_from_features(feats, regime, timestamp=as_of,
                                       instrument=symbol)
        news_ctx = None
        if news_result is not None:
            news_ctx = build_news_context(news_result, as_of=as_of,
                                          target_ticker=ticker)

        def run_config(config_name: str, weights_groups, with_news: bool) -> None:
            pengine = (pat_full if config_name == "V4_full" else pat_tech)
            matches = pengine.find_matches(
                fp, lib, as_of=as_of, min_similarity=min_similarity,
                horizon=horizon_delta)
            report = build_pattern_report(
                matches, df, horizons={h_name: h_bars},
                min_matches=min_pattern_matches,
                min_similarity=min_similarity)
            led = build_evidence_ledger_v2(feats, regime, cand.direction)
            pattern_to_evidence(led, report, symbol)
            if with_news and news_ctx is not None:
                news_to_evidence(led, news_ctx, as_of=as_of)
            vote = _vote_direction(led)
            bias = vote if vote != "neutral" else v3_bias
            rows[config_name].append((bias, fwd_ret))

        rows["V3"].append((v3_bias, fwd_ret))
        run_config("V4_technical", tech_groups, with_news=False)
        run_config("V4_technical_news", tech_groups, with_news=True)
        run_config("V4_full", None, with_news=True)

    return {c: _metrics(c, rows[c]) for c in CONFIGS}
