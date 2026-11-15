"""Locate the Windows taskbar and express it as a crop box inside a screenshot.

The analyzer needs the taskbar: its icons are the cheapest evidence of which
apps are running. But the bar is a thin strip on a large frame, and asking a
vision model to find it in a 4K image wastes tokens. This module supplies
geometry only (no pixels, no icon parsing) so the analysis layer can crop the
bar and enlarge it before the frame is sent to the model.

Coordinates are physical pixels, the same space ``mss`` reports after
:func:`dayloom.capture.winapi.enable_dpi_awareness`. Windows has no public
"where is the taskbar" API, so bars are found by window class. The main
monitor uses ``Shell_TrayWnd``; every secondary monitor gets its own
``Shell_SecondaryTrayWnd``, and those have to be enumerated because
``FindWindowW`` only returns the first match.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass

from . import winapi

WINDOWS = sys.platform == "win32"
TASKBAR_CLASSES = ("Shell_TrayWnd", "Shell_SecondaryTrayWnd")
MIN_THICKNESS = 8
MIN_VISIBLE_RATIO = 0.6

if WINDOWS:  # pragma: no cover - exercised on Windows only
    # ctypes assumes C int for undeclared functions, which truncates HWNDs on
    # 64-bit Windows and makes the enumeration silently miss windows.
    winapi.user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    winapi.user32.FindWindowW.restype = wintypes.HWND
    winapi.user32.FindWindowExW.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
    ]
    winapi.user32.FindWindowExW.restype = wintypes.HWND


@dataclass(frozen=True, slots=True)
class Taskbar:
    """A taskbar window plus the monitor and screen edge it belongs to."""

    rect: tuple[int, int, int, int]
    monitor: tuple[int, int, int, int]
    edge: str


def _intersection(
    a: tuple[int, int, int, int], b: tuple[int, int, int, int]
) -> tuple[int, int, int, int] | None:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0 = max(ax, bx)
    y0 = max(ay, by)
    x1 = min(ax + aw, bx + bw)
    y1 = min(ay + ah, by + bh)
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def _edge(rect: tuple[int, int, int, int], monitor: tuple[int, int, int, int]) -> str:
    x, y, w, h = rect
    mx, my, mw, mh = monitor
    thin = min(w, h)
    if w >= h:
        # Flush with the monitor's bottom means bottom; the small tolerance
        # absorbs rounding between GetWindowRect and the monitor rectangle.
        return "bottom" if y + h >= my + mh - thin else "top"
    return "right" if x + w >= mx + mw - thin else "left"


def _clamp(value: float) -> float:
    return round(min(1.0, max(0.0, value)), 4)


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    rect = winapi.RECT()
    if not winapi.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        return None
    return (int(rect.left), int(rect.top), width, height)


def taskbar_rects() -> list[tuple[str, tuple[int, int, int, int]]]:
    """Enumerate taskbar windows as ``(class_name, (x, y, w, h))``.

    Best-effort on purpose: the capture loop calls this on a hot path, and a
    shell restart or a locked-down session must not raise into that loop. An
    empty list simply means "no taskbar evidence this time".
    """
    if not WINDOWS:
        return []
    try:
        found: list[tuple[str, tuple[int, int, int, int]]] = []
        primary = winapi.user32.FindWindowW(TASKBAR_CLASSES[0], None)
        if primary:
            rect = _window_rect(primary)
            if rect is not None:
                found.append((TASKBAR_CLASSES[0], rect))
        # FindWindowW only returns the first match. Secondary taskbars form a
        # chain that FindWindowExW walks when handed the previous HWND.
        previous = None
        while True:
            hwnd = winapi.user32.FindWindowExW(None, previous, TASKBAR_CLASSES[1], None)
            if not hwnd:
                break
            rect = _window_rect(hwnd)
            if rect is not None:
                found.append((TASKBAR_CLASSES[1], rect))
            previous = hwnd
        return found
    except Exception:
        # Diagnostics must never take the capture loop down with them.
        return []


def assign_taskbars(
    rects: list[tuple[str, tuple[int, int, int, int]]],
    monitors: list[tuple[int, int, int, int]],
) -> list[Taskbar]:
    """Attach each raw taskbar rectangle to a monitor and an edge.

    Filtering happens here instead of inside :func:`taskbar_rects` so the
    rules stay pure and testable: zero-sized windows, auto-hidden bars (too
    thin, or parked mostly off-screen), and bars with no monitor intersection
    are dropped. Keeping a sliver would cost tokens without giving the model
    anything readable.
    """
    assigned: list[Taskbar] = []
    for _class_name, rect in rects:
        width, height = rect[2], rect[3]
        if width <= 0 or height <= 0 or min(width, height) < MIN_THICKNESS:
            continue
        area = width * height
        monitor: tuple[int, int, int, int] | None = None
        overlap = 0
        for candidate in monitors:
            inter = _intersection(rect, candidate)
            if inter is None:
                continue
            candidate_area = inter[2] * inter[3]
            if candidate_area > overlap:
                monitor, overlap = candidate, candidate_area
        if monitor is None:
            continue
        # More than half the bar off-screen means auto-hide, not a usable bar.
        if overlap * 2 < area:
            continue
        assigned.append(Taskbar(rect=rect, monitor=monitor, edge=_edge(rect, monitor)))
    return assigned


def find_taskbars(monitors: list[tuple[int, int, int, int]]) -> list[Taskbar]:
    """Discover the current taskbars and assign them to *monitors*."""
    return assign_taskbars(taskbar_rects(), monitors)


def strip_box(
    taskbar: Taskbar, capture: tuple[int, int, int, int]
) -> tuple[float, float, float, float] | None:
    """Return the taskbar's box normalized to *capture*, or None if unusable.

    The box is ``(x0, y0, x1, y1)`` in capture-relative 0..1 coordinates, so
    the caller can crop without knowing screen origins. A bar that is mostly
    outside the frame, or only a few pixels tall, is rejected: enlarging that
    sliver would cost tokens without giving the model anything to read.
    """
    tw, th = taskbar.rect[2], taskbar.rect[3]
    cx, cy, cw, ch = capture
    if tw <= 0 or th <= 0 or cw <= 0 or ch <= 0:
        return None
    inter = _intersection(taskbar.rect, capture)
    if inter is None:
        return None
    ix, iy, iw, ih = inter
    if iw < MIN_THICKNESS or ih < MIN_THICKNESS:
        return None
    visible = ih / th if tw >= th else iw / tw
    if visible < MIN_VISIBLE_RATIO:
        return None
    return (
        _clamp((ix - cx) / cw),
        _clamp((iy - cy) / ch),
        _clamp((ix + iw - cx) / cw),
        _clamp((iy + ih - cy) / ch),
    )


_EDGE_ORDER = {"bottom": 0, "top": 1, "right": 2, "left": 3}


def strip_for(
    capture: tuple[int, int, int, int],
    monitors: list[tuple[int, int, int, int]],
) -> tuple[Taskbar, tuple[float, float, float, float]] | None:
    """Pick the most useful visible taskbar for *capture*.

    Bottom bars come first, then top bars: the bottom edge is where Windows
    puts the main taskbar by default, and both horizontal bars carry the icon
    strip we care about, so they outrank vertical ones.
    """
    for taskbar in sorted(find_taskbars(monitors), key=lambda bar: _EDGE_ORDER.get(bar.edge, 9)):
        box = strip_box(taskbar, capture)
        if box is not None:
            return taskbar, box
    return None
