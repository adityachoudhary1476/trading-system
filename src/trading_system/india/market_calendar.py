"""Indian market session / trading-calendar abstraction (Day 4 hardened).

Encapsulates NSE/BSE equity session rules so session logic never leaks into the
rest of the app. Day 4 additions over Day 3:
  * Explicit session phases: PRE_MARKET, REGULAR, POST_MARKET, CLOSED, HOLIDAY.
  * Holiday registry hook: holidays are NOT hard-coded here. A `TradingCalendar`
    can be given a set of holiday dates (e.g. loaded from a provider/file later).
  * Timezone stays Asia/Kolkata (UTC+5:30, no DST).

Sources: NSE public session timings (pre-open 09:00-09:15, regular 09:15-15:30,
post-close 15:30-16:00). These are the standard equity sessions; index derivatives
may differ slightly but we keep the conservative equity boundaries as default.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from dataclasses import dataclass, field
from enum import Enum

from zoneinfo import ZoneInfo

KOLKATA = ZoneInfo("Asia/Kolkata")


class SessionPhase(str, Enum):
    PRE_MARKET = "pre_market"      # 09:00-09:15 IST (order collection/ matching)
    REGULAR = "regular"            # 09:15-15:30 IST (continuous trading)
    POST_MARKET = "post_market"    # 15:30-16:00 IST (post-close)
    CLOSED = "closed"              # outside trading hours / weekend
    HOLIDAY = "holiday"            # exchange holiday (registry-provided)


# Default NSE equity session windows (IST).
PRE_OPEN = (time(9, 0), time(9, 15))
REGULAR = (time(9, 15), time(15, 30))
POST_CLOSE = (time(15, 30), time(16, 0))


@dataclass
class TradingCalendar:
    """Holiday-aware calendar. Holidays are injected, never hard-coded en masse."""

    holidays: set[date] = field(default_factory=set)
    pre_open: tuple[time, time] = PRE_OPEN
    regular: tuple[time, time] = REGULAR
    post_close: tuple[time, time] = POST_CLOSE

    def is_holiday(self, dt: datetime) -> bool:
        return self.to_kolkata(dt).date() in self.holidays

    def add_holiday(self, d: date) -> None:
        self.holidays.add(d)

    def phase(self, dt_utc: datetime) -> SessionPhase:
        k = self.to_kolkata(dt_utc)
        if k.weekday() >= 5:  # Sat/Sun
            return SessionPhase.CLOSED
        if self.is_holiday(dt_utc):
            return SessionPhase.HOLIDAY
        t = k.time()
        if self.pre_open[0] <= t < self.pre_open[1]:
            return SessionPhase.PRE_MARKET
        if self.regular[0] <= t <= self.regular[1]:
            return SessionPhase.REGULAR
        if self.post_close[0] <= t < self.post_close[1]:
            return SessionPhase.POST_MARKET
        return SessionPhase.CLOSED

    # --- convenience predicates ------------------------------------------
    def is_open(self, dt_utc: datetime) -> bool:
        return self.phase(dt_utc) in (SessionPhase.REGULAR, SessionPhase.PRE_MARKET, SessionPhase.POST_MARKET)

    def is_regular_session(self, dt_utc: datetime) -> bool:
        return self.phase(dt_utc) == SessionPhase.REGULAR

    def is_trading_day(self, dt_utc: datetime) -> bool:
        p = self.phase(dt_utc)
        return p not in (SessionPhase.CLOSED, SessionPhase.HOLIDAY)

    # --- timezone helpers -------------------------------------------------
    @staticmethod
    def to_kolkata(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=__import__("datetime").timezone.utc)
        return dt.astimezone(KOLKATA)

    def session_boundaries(self, dt_utc: datetime) -> tuple[datetime, datetime]:
        """Return (open, close) of the nearest regular session for dt's day."""
        k = self.to_kolkata(dt_utc)
        if k.weekday() >= 5 or self.is_holiday(dt_utc):
            # roll forward to next weekday that is not a holiday
            cur = k + timedelta(days=1)
            while cur.weekday() >= 5 or cur.date() in self.holidays:
                cur = cur + timedelta(days=1)
            k = cur
        open_dt = k.replace(hour=REGULAR[0].hour, minute=REGULAR[0].minute, second=0, microsecond=0)
        close_dt = k.replace(hour=REGULAR[1].hour, minute=REGULAR[1].minute, second=0, microsecond=0)
        return open_dt, close_dt

    def next_open(self, dt_utc: datetime) -> datetime:
        open_dt, _ = self.session_boundaries(dt_utc)
        if self.to_kolkata(dt_utc) >= open_dt:
            return self.session_boundaries(dt_utc + timedelta(days=1))[0]
        return open_dt


# Default calendar singleton (no holidays) — preserves Day 3 function signatures.
DEFAULT_CALENDAR = TradingCalendar()


def to_kolkata(dt: datetime) -> datetime:
    return TradingCalendar.to_kolkata(dt)


def is_trading_day(dt: datetime) -> bool:
    return DEFAULT_CALENDAR.is_trading_day(dt)


def market_state(dt: datetime) -> str:
    """Back-compat: OPEN/CLOSED for the regular session."""
    p = DEFAULT_CALENDAR.phase(dt)
    return "open" if p == SessionPhase.REGULAR else "closed"


def is_within_session(dt: datetime) -> bool:
    return DEFAULT_CALENDAR.is_regular_session(dt)


def session_boundaries(dt: datetime) -> tuple[datetime, datetime]:
    return DEFAULT_CALENDAR.session_boundaries(dt)


def next_session_open(dt: datetime) -> datetime:
    return DEFAULT_CALENDAR.next_open(dt)


def session_phase(dt: datetime) -> SessionPhase:
    return DEFAULT_CALENDAR.phase(dt)
