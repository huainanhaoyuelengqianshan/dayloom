"""Screen grabbing, scaling and encoding.

Capture mode is a privacy decision, not a performance one, so the default is
``active_window``: only the window the user is looking at is ever written to
disk or sent to a model. ``full_monitor`` and ``all_monitors`` exist for people
who want the whole context, and cost more tokens.

Local storage is lossy WebP (small, fast, plenty for a VLM). What goes to the
model is re-encoded as JPEG: WebP data URLs are rejected by a surprising number
of OpenAI-compatible gateways, and a 400 at analysis time is much more annoying
than a slightly larger upload.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from ..capture.winapi import screen_metrics


@dataclass(slots=True)
class Monitor:
    left: int
    top: int
    width: int
    height: int
    index: int

    @property
    def box(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.width, self.height)

    @property
    def label(self) -> str:
        return f"m{self.index}:{self.width}x{self.height}+{self.left}+{self.top}"

    def contains(self, x: int, y: int) -> bool:
        return (
            self.left <= x < self.left + self.width
            and self.top <= y < self.top + self.height
        )


def _monitors(sct) -> list[Monitor]:
    out: list[Monitor] = []
    for idx, mon in enumerate(sct.monitors):
        out.append(
            Monitor(
                left=int(mon["left"]),
                top=int(mon["top"]),
                width=int(mon["width"]),
                height=int(mon["height"]),
                index=idx,
            )
        )
    return out


def _intersect(
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


class Grabber:
    """Owns one mss instance.

    mss objects are bound to the thread that created them and are not safe to
    share, so the capture loop keeps exactly one of these and never hands it
    to the analysis thread pool.

    The monitor list is *not* cached forever: ``mss`` reads it once at
    construction, and a display-layout change (docking, projection mode,
    resolution) would otherwise make every later capture grab the wrong
    rectangle — the capture then shows a screen the user is not on, and the
    model has to guess. The layout signature is cheap, so it is re-checked on
    every grab.
    """

    def __init__(self) -> None:
        self._sct = None
        self._monitors: list[Monitor] = []
        self._layout: tuple[int, ...] | None = None

    def _current_layout(self) -> tuple[int, ...] | None:
        try:
            metrics = screen_metrics()
        except Exception:  # pragma: no cover - non-Windows / API unavailable
            return None
        return layout_signature(metrics)

    def _ensure(self):
        layout = self._current_layout()
        if self._sct is not None and layout is not None and layout != self._layout:
            self.close()
        if self._sct is None:
            import mss

            self._sct = mss.mss()
            self._monitors = _monitors(self._sct)
            self._layout = layout
        return self._sct

    @property
    def monitors(self) -> list[Monitor]:
        self._ensure()
        return self._monitors

    def virtual_box(self) -> tuple[int, int, int, int]:
        return self.monitors[0].box

    def monitor_for(self, rect: tuple[int, int, int, int]) -> Monitor:
        x, y, w, h = rect
        cx, cy = x + w // 2, y + h // 2
        for mon in self.monitors[1:]:
            if mon.contains(cx, cy):
                return mon
        return self.monitors[1] if len(self.monitors) > 1 else self.monitors[0]

    def choose_rect(
        self, mode: str, window_rect: tuple[int, int, int, int] | None
    ) -> tuple[tuple[int, int, int, int], str]:
        """Return (rect, monitor label) for the configured capture mode."""
        virtual = self.virtual_box()
        if mode == "all_monitors":
            return virtual, "all"
        if mode == "full_monitor":
            rect = window_rect or virtual
            mon = self.monitor_for(rect)
            return mon.box, mon.label
        # active_window (default)
        if window_rect:
            clipped = _intersect(window_rect, virtual)
            if clipped and clipped[2] >= 64 and clipped[3] >= 64:
                mon = self.monitor_for(clipped)
                return clipped, mon.label
        mon = self.monitors[1] if len(self.monitors) > 1 else self.monitors[0]
        return mon.box, mon.label

    def grab(self, rect: tuple[int, int, int, int]) -> Image.Image:
        sct = self._ensure()
        x, y, w, h = rect
        shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def close(self) -> None:
        if self._sct is not None:
            try:
                self._sct.close()
            finally:
                self._sct = None
                self._monitors = []
                self._layout = None


def layout_signature(metrics: dict[str, int] | None) -> tuple[int, ...] | None:
    """Virtual-screen signature; changes when monitors are added/removed/moved."""
    if not metrics:
        return None
    return (
        int(metrics.get("x", 0)),
        int(metrics.get("y", 0)),
        int(metrics.get("width", 0)),
        int(metrics.get("height", 0)),
        int(metrics.get("monitors", 0)),
    )


def window_covers(
    window: tuple[int, int, int, int] | None,
    capture: tuple[int, int, int, int],
    tolerance: int = 6,
) -> bool:
    """True when *window* fills *capture* — i.e. a full-screen app hides the taskbar.

    A maximised window is not full screen: its frame stops short of the monitor
    edge by the taskbar's height (and DWM bounds exclude the invisible resize
    border), so it keeps the taskbar visible. Only a window that reaches every
    capture edge can paint over it; cropping the taskbar rectangle in that case
    would label the bottom of the video as "taskbar".
    """
    if not window:
        return False
    wx, wy, ww, wh = window
    cx, cy, cw, ch = capture
    return (
        wx <= cx + tolerance
        and wy <= cy + tolerance
        and wx + ww >= cx + cw - tolerance
        and wy + wh >= cy + ch - tolerance
    )


def scale_to_max_edge(image: Image.Image, max_edge: int) -> Image.Image:
    longest = max(image.size)
    if longest <= max_edge:
        return image
    ratio = max_edge / float(longest)
    size = (max(1, int(image.width * ratio)), max(1, int(image.height * ratio)))
    return image.resize(size, Image.Resampling.LANCZOS)


def encode_webp(image: Image.Image, quality: int = 72) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="WEBP", quality=quality, method=4)
    return buf.getvalue()


def encode_jpeg(image: Image.Image, quality: int = 82) -> bytes:
    buf = io.BytesIO()
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    image.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def jpeg_from_file(path: Path, max_edge: int, quality: int = 82) -> bytes | None:
    """Load a stored frame and re-encode it for the model."""
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            return encode_jpeg(scale_to_max_edge(img, max_edge), quality)
    except (OSError, ValueError):
        return None


def display_report() -> dict[str, object]:
    """Best-effort display diagnostics used by ``dayloom doctor``."""
    metrics = screen_metrics()
    try:
        grabber = Grabber()
        mons = [
            {"label": m.label, "box": m.box}
            for m in grabber.monitors[1:] or grabber.monitors
        ]
        grabber.close()
    except Exception as exc:  # pragma: no cover - hardware dependent
        return {"metrics": metrics, "monitors": [], "error": str(exc)}
    return {"metrics": metrics, "monitors": mons}
