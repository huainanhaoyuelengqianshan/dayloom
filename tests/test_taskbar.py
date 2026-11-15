from __future__ import annotations

import pytest

from dayloom.capture import taskbar
from dayloom.capture.taskbar import (
    TASKBAR_CLASSES,
    WINDOWS,
    Taskbar,
    assign_taskbars,
    find_taskbars,
    strip_box,
    strip_for,
    taskbar_rects,
)

PRIMARY = (0, 0, 1920, 1080)
SECONDARY = (1920, 0, 2560, 1440)
MONITORS = (PRIMARY, SECONDARY)


def assigned(rect, monitors=MONITORS):
    return assign_taskbars([("Shell_TrayWnd", rect)], monitors)


def test_bottom_taskbar_edge():
    found = assigned((0, 1040, 1920, 40))
    assert len(found) == 1
    assert found[0].edge == "bottom"
    assert found[0].rect == (0, 1040, 1920, 40)
    assert found[0].monitor == PRIMARY


def test_top_taskbar_edge():
    found = assigned((0, 0, 1920, 40))
    assert len(found) == 1
    assert found[0].edge == "top"


def test_left_taskbar_edge():
    found = assigned((0, 0, 40, 1080))
    assert len(found) == 1
    assert found[0].edge == "left"
    assert found[0].monitor == PRIMARY


def test_right_taskbar_edge():
    found = assigned((1880, 0, 40, 1080))
    assert len(found) == 1
    assert found[0].edge == "right"


def test_bottom_edge_on_secondary_uses_absolute_coordinates():
    found = assign_taskbars(
        [("Shell_SecondaryTrayWnd", (1920, 1400, 2560, 40))], [SECONDARY]
    )
    assert len(found) == 1
    assert found[0].edge == "bottom"
    assert found[0].monitor == SECONDARY


def test_monitor_with_the_largest_overlap_wins():
    # 120px on the primary and 80px on the secondary, same height.
    found = assigned((1800, 500, 200, 40))
    assert len(found) == 1
    assert found[0].monitor == PRIMARY


def test_taskbar_fully_on_the_secondary_monitor():
    found = assign_taskbars([("Shell_SecondaryTrayWnd", (2000, 100, 1920, 40))], MONITORS)
    assert len(found) == 1
    assert found[0].monitor == SECONDARY


@pytest.mark.parametrize(
    "rect",
    [
        (0, 1076, 1920, 4),  # a 4px auto-hide sliver
        (0, 0, 4, 1080),  # vertical, 4px wide
        (0, 0, 1920, 7),  # horizontal, 7px tall
    ],
)
def test_too_thin_bars_are_dropped(rect):
    assert assigned(rect) == []


@pytest.mark.parametrize(
    "rect",
    [
        (0, 1040, 0, 40),
        (0, 1040, 1920, 0),
        (0, 1040, -10, 40),
    ],
)
def test_non_positive_sizes_are_dropped(rect):
    assert assigned(rect) == []


def test_bars_without_a_monitor_intersection_are_dropped():
    assert assigned((5000, 5000, 200, 40)) == []


def test_mostly_offscreen_bar_is_treated_as_auto_hidden():
    # Only 10 of 40 pixels remain on-screen.
    assert assigned((0, 1070, 1920, 40)) == []


def test_exactly_half_visible_bar_is_kept():
    found = assigned((0, 1060, 1920, 40))
    assert len(found) == 1
    assert found[0].edge == "bottom"


def test_strip_box_for_a_fully_contained_bottom_bar():
    bar = Taskbar(rect=(0, 960, 1920, 64), monitor=PRIMARY, edge="bottom")
    box = strip_box(bar, (0, 0, 1920, 1024))
    assert box == (0.0, 0.9375, 1.0, 1.0)


def test_strip_box_when_capture_only_covers_part_of_the_bar():
    bar = Taskbar(rect=(0, 960, 1920, 64), monitor=PRIMARY, edge="bottom")
    box = strip_box(bar, (0, 900, 1920, 200))
    assert box == pytest.approx((0.0, 0.3, 1.0, 0.62))


def test_strip_box_is_relative_to_the_capture_origin():
    bar = Taskbar(rect=(100, 100, 200, 40), monitor=PRIMARY, edge="top")
    box = strip_box(bar, (50, 50, 400, 200))
    assert box == pytest.approx((0.125, 0.25, 0.625, 0.45))


def test_strip_box_for_a_partially_captured_vertical_bar():
    bar = Taskbar(rect=(1880, 0, 40, 1080), monitor=PRIMARY, edge="right")
    box = strip_box(bar, (1860, 0, 60, 1080))
    assert box == pytest.approx((0.3333, 0.0, 1.0, 1.0))


def test_strip_box_is_none_when_the_bar_is_outside_the_capture():
    bar = Taskbar(rect=(0, 1040, 1920, 40), monitor=PRIMARY, edge="bottom")
    assert strip_box(bar, (0, 0, 1920, 1000)) is None


def test_strip_box_requires_sixty_percent_visibility():
    bar = Taskbar(rect=(0, 1040, 1920, 40), monitor=PRIMARY, edge="bottom")
    assert strip_box(bar, (0, 0, 1920, 1060)) is None  # 20/40 = 0.5
    box = strip_box(bar, (0, 0, 1920, 1064))  # 24/40 = 0.6, the boundary
    assert box == pytest.approx((0.0, round(1040 / 1064, 4), 1.0, 1.0))


def test_strip_box_rejects_a_seven_pixel_sliver():
    bar = Taskbar(rect=(0, 1070, 1920, 10), monitor=PRIMARY, edge="bottom")
    # 7/10 is visible enough by ratio, but 7px is too small to be worth a crop.
    assert strip_box(bar, (0, 0, 1920, 1077)) is None


def test_strip_for_prefers_the_bottom_bar(monkeypatch):
    def fake_rects():
        return [
            ("Shell_TrayWnd", (0, 0, 1920, 40)),
            ("Shell_TrayWnd", (0, 1040, 1920, 40)),
        ]

    monkeypatch.setattr(taskbar, "taskbar_rects", fake_rects)
    result = strip_for((0, 0, 1920, 1080), [PRIMARY])
    assert result is not None
    picked, box = result
    assert picked.edge == "bottom"
    assert box == pytest.approx((0.0, round(1040 / 1080, 4), 1.0, 1.0))


def test_strip_for_falls_back_to_the_top_bar(monkeypatch):
    def fake_rects():
        return [
            ("Shell_TrayWnd", (0, 0, 1920, 40)),
            ("Shell_TrayWnd", (0, 1040, 1920, 40)),
        ]

    monkeypatch.setattr(taskbar, "taskbar_rects", fake_rects)
    result = strip_for((0, 0, 1920, 500), [PRIMARY])
    assert result is not None
    assert result[0].edge == "top"


def test_strip_for_returns_none_without_taskbars(monkeypatch):
    monkeypatch.setattr(taskbar, "taskbar_rects", lambda: [])
    assert strip_for(PRIMARY, [PRIMARY]) is None


def test_find_taskbars_uses_the_discovered_rectangles(monkeypatch):
    rects = [("Shell_TrayWnd", (0, 1040, 1920, 40))]
    monkeypatch.setattr(taskbar, "taskbar_rects", lambda: rects)
    found = find_taskbars([PRIMARY])
    assert [bar.edge for bar in found] == ["bottom"]


def test_taskbar_rects_returns_a_list_without_raising():
    rects = taskbar_rects()
    assert isinstance(rects, list)
    if not WINDOWS:
        assert rects == []
    for class_name, rect in rects:
        assert class_name in TASKBAR_CLASSES
        assert len(rect) == 4
        assert rect[2] > 0 and rect[3] > 0


@pytest.mark.skipif(not WINDOWS, reason="FindWindowW exists on Windows only")
def test_taskbar_rects_looks_up_the_primary_by_class_name(monkeypatch):
    calls = []

    def fake_find(class_name, window_name):
        calls.append((class_name, window_name))
        return None

    monkeypatch.setattr(taskbar.winapi.user32, "FindWindowW", fake_find)
    monkeypatch.setattr(taskbar.winapi.user32, "FindWindowExW", lambda *args: 0)
    assert taskbar_rects() == []
    assert calls == [("Shell_TrayWnd", None)]

