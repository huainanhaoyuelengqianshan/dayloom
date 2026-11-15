from __future__ import annotations

from dayloom.analyzer.merge import build_sessions, merge_activities
from dayloom.store.repo import ActivityRow


def act(fid: int, start: float, end: float, *, app: str = "code", cat: str = "coding",
        title: str = "") -> ActivityRow:
    return ActivityRow(
        id=fid,
        batch_id=1,
        seq=fid,
        start_ts=start,
        end_ts=end,
        app=app,
        category=cat,
        title=title,
        summary="",
        confidence=0.8,
        frame_ids=[],
    )


def test_adjacent_same_activity_merges():
    merged = merge_activities([act(1, 0, 100), act(2, 160, 300)], gap_seconds=120)
    assert len(merged) == 1
    assert merged[0].start_ts == 0
    assert merged[0].end_ts == 300
    assert merged[0].activity_ids == [1, 2]


def test_large_gap_splits():
    merged = merge_activities([act(1, 0, 100), act(2, 500, 600)], gap_seconds=120)
    assert len(merged) == 2


def test_different_category_splits():
    merged = merge_activities(
        [act(1, 0, 100, cat="coding"), act(2, 110, 200, cat="communication")], gap_seconds=120
    )
    assert len(merged) == 2


def test_different_app_splits():
    merged = merge_activities(
        [act(1, 0, 100, app="code"), act(2, 110, 200, app="chrome")], gap_seconds=120
    )
    assert len(merged) == 2


def test_unknown_is_never_smoothed_over():
    merged = merge_activities(
        [act(1, 0, 100, cat="unknown"), act(2, 110, 200, cat="unknown")], gap_seconds=120
    )
    assert len(merged) == 2


def test_build_sessions_stamps_the_day():
    sessions = build_sessions([act(1, 0, 100)], day="2026-05-10")
    assert sessions[0].day == "2026-05-10"
    assert sessions[0].duration == 100


def test_merging_is_order_independent():
    items = [act(2, 160, 300), act(1, 0, 100)]
    merged = merge_activities(items, gap_seconds=120)
    assert len(merged) == 1
    assert merged[0].start_ts == 0
