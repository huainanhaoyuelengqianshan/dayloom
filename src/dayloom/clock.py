"""Time helpers.

Two rules that the rest of the codebase relies on:

1. Every timestamp stored in SQLite is a POSIX epoch float in UTC.
2. Days are *logical* local days: a configurable boundary hour (default 04:00)
   decides which date a moment belongs to, so working at 02:00 counts as the
   previous day. ``day_key()`` is the only place that decision is made.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

DEFAULT_BOUNDARY_HOUR = 4


def now_ts() -> float:
    import time as _time

    return _time.time()


def local_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts)


def _boundary(boundary_hour: int) -> time:
    return time(hour=max(0, min(23, int(boundary_hour))))


def day_key(ts: float, boundary_hour: int = DEFAULT_BOUNDARY_HOUR) -> str:
    """Logical local day (YYYY-MM-DD) that ``ts`` belongs to."""
    dt = local_dt(ts)
    d = dt.date()
    if dt.time() < _boundary(boundary_hour):
        d = d - timedelta(days=1)
    return d.isoformat()


def parse_day(day: str) -> date:
    return date.fromisoformat(day)


def day_bounds(day: str, boundary_hour: int = DEFAULT_BOUNDARY_HOUR) -> tuple[float, float]:
    """Epoch window [start, end) of a logical day."""
    d = parse_day(day)
    b = _boundary(boundary_hour)
    start = datetime.combine(d, b)
    end = datetime.combine(d + timedelta(days=1), b)
    return start.timestamp(), end.timestamp()


def shift_day(day: str, days: int) -> str:
    return (parse_day(day) + timedelta(days=days)).isoformat()


WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def weekday_label(day: str) -> str:
    return WEEKDAYS[parse_day(day).weekday()]


def fmt_hms(ts: float) -> str:
    return local_dt(ts).strftime("%H:%M:%S")


def fmt_hm(ts: float) -> str:
    return local_dt(ts).strftime("%H:%M")


def fmt_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def fmt_duration_short(seconds: float) -> str:
    """Compact form for dense rows: ``<1m`` / ``9m`` / ``1h18m``."""
    total = int(round(max(0.0, float(seconds))))
    if total < 60:
        return "<1m"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h{rest:02d}m"
