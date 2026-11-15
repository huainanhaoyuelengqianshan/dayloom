from __future__ import annotations

from datetime import datetime

from dayloom import clock


def test_day_key_respects_boundary():
    ts = datetime(2026, 5, 10, 3, 30).timestamp()
    assert clock.day_key(ts, 4) == "2026-05-09"
    assert clock.day_key(ts, 0) == "2026-05-10"


def test_day_bounds_are_a_full_day():
    start, end = clock.day_bounds("2026-05-10", 4)
    assert end - start == 86400
    assert clock.day_key(start + 1, 4) == "2026-05-10"
    assert clock.day_key(start - 1, 4) == "2026-05-09"


def test_shift_day_crosses_months():
    assert clock.shift_day("2026-03-01", -1) == "2026-02-28"
    assert clock.shift_day("2026-12-31", 1) == "2027-01-01"


def test_fmt_duration():
    assert clock.fmt_duration(45) == "45s"
    assert clock.fmt_duration(90) == "1m30s"
    assert clock.fmt_duration(3725) == "1h02m"
