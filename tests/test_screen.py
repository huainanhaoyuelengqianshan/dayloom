from __future__ import annotations

from dayloom.capture.screen import layout_signature, window_covers


def test_layout_signature_missing_metrics():
    assert layout_signature(None) is None
    assert layout_signature({}) is None


def test_layout_signature_changes_when_monitors_change():
    single = {"x": 0, "y": 0, "width": 3072, "height": 1920, "monitors": 1}
    dual = {"x": -2560, "y": 0, "width": 5632, "height": 1920, "monitors": 2}
    assert layout_signature(single) != layout_signature(dual)


def test_layout_signature_is_stable_for_the_same_layout():
    metrics = {"x": -2560, "y": 0, "width": 5632, "height": 1920, "monitors": 2}
    assert layout_signature(metrics) == layout_signature(dict(metrics))


def test_window_covers_detects_a_fullscreen_window():
    monitor = (-2560, 268, 2560, 1440)
    assert window_covers(monitor, monitor)
    # DWM bounds can overshoot by the invisible resize border.
    assert window_covers((-2569, 259, 2578, 1458), monitor)


def test_window_covers_leaves_maximised_windows_alone():
    monitor = (-2560, 268, 2560, 1440)
    # A maximised browser stops short of the taskbar at the bottom.
    assert not window_covers((-2569, 259, 2578, 1398), monitor)
    # A small window is obviously not full screen.
    assert not window_covers((-1720, 700, 1200, 800), monitor)
    assert not window_covers(None, monitor)
