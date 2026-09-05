"""Deterministic V4 fixtures: news + historical patterns.

EVERY fixture here is SYNTHETIC_TEST data — invented titles/values for
testing only. None of it represents real news or real market data and none
of it may be used in a production/live path.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

SYNTHETIC_TEST = "SYNTHETIC_TEST"
UTC = timezone.utc

# Fixed reference time so fixture timestamps are fully deterministic.
BASE_TIME = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Raw payloads (RSS 2.0 / Atom) — for parser tests
# --------------------------------------------------------------------------- #
def rss_payload(items: list[tuple[str, str, str]],
                channel: str = "Test Feed") -> bytes:
    rows = "".join(
        f"<item><title>{t}</title><link>{u}</link>"
        f"<pubDate>{p}</pubDate><description>{t} details</description></item>"
        for t, u, p in items)
    xml = (f'<?xml version="1.0"?><rss version="2.0"><channel>'
           f"<title>{channel}</title>{rows}</channel></rss>")
    return xml.encode("utf-8")


def atom_payload(entries: list[tuple[str, str, str]]) -> bytes:
    rows = "".join(
        f'<entry><title>{t}</title><link href="{u}"/>'
        f"<published>{p}</published><summary>{t} summary</summary></entry>"
        for t, u, p in entries)
    xml = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
           f"<title>Atom Test</title>{rows}</feed>")
    return xml.encode("utf-8")


MALFORMED_XML = b"<rss><channel><item><title>broken"
EMPTY_XML = b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'


def make_raw(title: str, published: datetime, source: str = "test",
             url: str = "", publisher: str = "SyntheticWire",
             description: str = "", **meta):
    """SYNTHETIC_TEST raw news item with a deterministic timestamp."""
    from trading_system.research.news_intelligence import RawNewsItem
    src = source if source.startswith(SYNTHETIC_TEST) else f"{SYNTHETIC_TEST}:{source}"
    return RawNewsItem(
        source=src, title=title,
        url=url or f"https://synthetic.test/{abs(hash(title)) % 10**8}",
        description=description or title,
        published_raw=published.isoformat(), publisher=publisher,
        raw_metadata={"reliability": 0.8, **meta})


# --------------------------------------------------------------------------- #
# Scenario fixtures (1-12)
# --------------------------------------------------------------------------- #
def positive_company_news():
    """(1) Strong company-positive event."""
    return make_raw("Reliance Industries wins record order for petchem expansion",
                    BASE_TIME - timedelta(minutes=10),
                    url="https://synthetic.test/ril-win")


def negative_company_news():
    """(2) Strong company-negative event."""
    return make_raw("Reliance Industries faces SEBI probe over disclosures",
                    BASE_TIME - timedelta(minutes=20),
                    url="https://synthetic.test/ril-probe")


def mixed_news():
    """(3) Mixed: record profit but misses analyst expectations."""
    return make_raw("Company reports record profit but misses analyst expectations",
                    BASE_TIME - timedelta(minutes=15),
                    url="https://synthetic.test/mixed")


def duplicate_articles():
    """(4) The SAME event echoed by three publishers."""
    return [
        make_raw("State Bank of India reports strong quarterly results net profit rises",
                 BASE_TIME - timedelta(minutes=30), source="a", publisher="WireA"),
        make_raw("SBI strong quarterly results net profit rises says bank",
                 BASE_TIME - timedelta(minutes=25), source="b", publisher="WireB"),
        make_raw("State Bank of India quarterly results net profit rises strong",
                 BASE_TIME - timedelta(minutes=20), source="c", publisher="WireC"),
    ]


def conflicting_sources():
    """(5) Contract win vs regulatory investigation for the same company."""
    return [
        make_raw("Reliance Industries bagged order for major refinery contract",
                 BASE_TIME - timedelta(minutes=40),
                 url="https://synthetic.test/c1"),
        make_raw("Reliance Industries faces investigation penalty notice from regulator",
                 BASE_TIME - timedelta(minutes=35),
                 url="https://synthetic.test/c2"),
    ]


def unknown_company_news():
    """(6) Entity that must remain UNRESOLVED."""
    return make_raw("Zylotech Industries announces quantum widget breakthrough",
                    BASE_TIME - timedelta(minutes=5))

def missing_timestamp_news():
    """(7) No publication time -> DEGRADED, freshness UNKNOWN."""
    raw = make_raw("Tata Steel commissions new plant", BASE_TIME)
    raw.published_raw = ""
    return raw


def stale_news():
    """(8) Published 3 days ago -> STALE/OLD evidence."""
    return make_raw("Infosys wins large deal", BASE_TIME - timedelta(days=3),
                    url="https://synthetic.test/stale")


def fresh_news():
    """(9) Published 5 minutes ago -> VERY_FRESH."""
    return make_raw("HDFC Bank gets regulatory approval for expansion",
                    BASE_TIME - timedelta(minutes=5),
                    url="https://synthetic.test/fresh")


def macro_event():
    """(10) RBI decision — market-wide, long freshness window."""
    return make_raw("RBI surprises with repo rate cut monetary policy easing",
                    BASE_TIME - timedelta(hours=2),
                    url="https://synthetic.test/rbi")


def sector_event():
    """(11) Banking-sector-wide event."""
    return make_raw("Banking sector NPAs improve as PSU bank credit grows",
                    BASE_TIME - timedelta(minutes=50),
                    url="https://synthetic.test/sector")


def company_event():
    """(12) Plain company event (dividend)."""
    return make_raw("ITC declares interim dividend",
                    BASE_TIME - timedelta(minutes=60),
                    url="https://synthetic.test/itc-div")


def all_news_items() -> list:
    """Everything above in one ingestion batch (pipeline/dedupe tests)."""
    return (duplicate_articles() + conflicting_sources()
            + [positive_company_news(), mixed_news(), unknown_company_news(),
               missing_timestamp_news(), stale_news(), fresh_news(),
               macro_event(), sector_event(), company_event()])


# --------------------------------------------------------------------------- #
# Pattern fixtures (13-20): deterministic OHLCV + combined states
# --------------------------------------------------------------------------- #
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402


def _frame(closes: np.ndarray, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = len(closes)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    idx = pd.date_range("2026-01-01", periods=n, freq="1D", tz="UTC")
    return pd.DataFrame({
        "open": opens,
        "high": np.maximum(closes, opens) + 0.4,
        "low": np.minimum(closes, opens) - 0.4,
        "close": closes,
        "volume": rng.integers(1_000_000, 5_000_000, n).astype(float),
    }, index=idx)


def historical_bullish_pattern(n: int = 150, seed: int = 13) -> pd.DataFrame:
    """(13) Sustained uptrend library state."""
    rng = np.random.default_rng(seed)
    return _frame(100 + np.cumsum(rng.normal(0.4, 0.8, n)), seed=seed)


def historical_bearish_pattern(n: int = 150, seed: int = 14) -> pd.DataFrame:
    """(14) Sustained downtrend library state."""
    rng = np.random.default_rng(seed)
    return _frame(100 - np.cumsum(rng.normal(0.4, 0.8, n)), seed=seed)


def historical_mixed_pattern(n: int = 150, seed: int = 15) -> pd.DataFrame:
    """(15) Trend + range mix — outcomes should be split."""
    rng = np.random.default_rng(seed)
    up = 100 + np.cumsum(rng.normal(0.4, 0.8, n // 2))
    flat = up[-1] + np.cumsum(rng.normal(0.0, 0.6, n - n // 2))
    return _frame(np.concatenate([up, flat]), seed=seed)


def insufficient_history_for_patterns() -> pd.DataFrame:
    """(16) Too few bars to build a library."""
    return historical_bullish_pattern(n=40, seed=16)


def news_agreeing_with_technicals():
    """(19) Bullish trend + positive company news on the same ticker."""
    from trading_system.research.news_intelligence import NewsPipeline
    df = historical_bullish_pattern(seed=19)
    raws = [positive_company_news()]
    return df, NewsPipeline().run(raws, as_of=BASE_TIME)


def news_conflicting_with_technicals():
    """(20) Bullish trend + strongly negative company news."""
    from trading_system.research.news_intelligence import NewsPipeline
    df = historical_bullish_pattern(seed=20)
    raws = [make_raw("Reliance Industries hit with penalty and investigation deepens",
                     BASE_TIME - timedelta(minutes=12),
                     url="https://synthetic.test/neg-conflict")]
    return df, NewsPipeline().run(raws, as_of=BASE_TIME)
