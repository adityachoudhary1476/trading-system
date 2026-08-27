"""Indian market session / trading-calendar abstraction.

Encapsulates NSE/BSE equity session rules so session logic never leaks into the
rest of the app. Key facts (NSE equities):
  * Timezone: Asia/Kolkata (UTC+5:30, no DST).
  * Weekdays Mon-Fri; Saturday/Sunday closed.
  * Regular equity session: 09:15-15:30 IST. (Pre-open 09:00-09:15, but we treat
    the continuous session as 09:15-15:30 for candle completeness.)
  * Index derivatives (NIFTY/BANKNIFTY) trade until 15:30; some expiries to 16:00,
    but we keep the conservative 15:30 equity close for the default calendar.
  * Muhurat trading and other special sessions are NOT modeled (out of scope);
    is_open returns False on such edge cases unless added later.

This module is deterministic and timezone-correct.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from enum import Enum

from zoneinfo import ZoneInfo

KOLKATA = ZoneInfo("Asia/Kolkata")


class MarketState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    UNKNOWN = "unknown"


# Equity continuous session (IST).
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)


def to_kolkata(dt_utc: datetime) -> datetime:
    """Convert a (naive or tz-aware) UTC datetime to Asia/Kolkata."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=__import__("datetime").timezone.utc)
    return dt_utc.astimezone(KOLKATA)


def is_trading_day(dt: datetime) -> bool:
    """True if the given instant falls on an NSE trading weekday."""
    k = to_kolkata(dt)
    return k.weekday() < 5  # Mon=0 .. Fri=4


def market_state(dt_utc: datetime) -> MarketState:
    """Return OPEN/CLOSED for the NSE equity session at the given instant."""
    k = to_kolkata(dt_utc)
    if k.weekday() >= 5:
        return MarketState.CLOSED
    t = k.time()
    if SESSION_OPEN <= t <= SESSION_CLOSE:
        return MarketState.OPEN
    return MarketState.CLOSED


def is_within_session(dt_utc: datetime) -> bool:
    return market_state(dt_utc) == MarketState.OPEN


def session_boundaries(dt_utc: datetime) -> tuple[datetime, datetime]:
    """Return (session_open_kolkata, session_close_kolkata) for dt's trading day.

    If dt is on a weekend, returns the boundaries of the *following* Monday.
    """
    k = to_kolkata(dt_utc)
    if k.weekday() >= 5:
        # shift to Monday
        days = (7 - k.weekday()) % 7 or 7
        k = k + timedelta(days=days)
    open_dt = k.replace(hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute, second=0, microsecond=0)
    close_dt = k.replace(hour=SESSION_CLOSE.hour, minute=SESSION_CLOSE.minute, second=0, microsecond=0)
    return open_dt, close_dt


def next_session_open(dt_utc: datetime) -> datetime:
    open_dt, _ = session_boundaries(dt_utc)
    if to_kolkata(dt_utc) >= open_dt:
        # already past today's open -> next day's open
        return session_boundaries(dt_utc + timedelta(days=1))[0]
    return open_dt
