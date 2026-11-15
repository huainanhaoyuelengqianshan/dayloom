"""The taskbar strip: per-frame visual evidence of which window was in front.

A full-monitor screenshot is downscaled to 1280px before it goes to a model, so
the Windows taskbar's active-app badge — a few pixels wide at that size — is
invisible. The model then falls back to "whichever window looks busiest" when a
screen contains several windows, which is exactly the multi-monitor misread:
the work happened on one display while the other one was showing a video.

Instead of asking the model to squint, crop the taskbar of every selected frame,
enlarge it and stack the strips into one small image in frame order (rows
labelled F0, F1, …). ``capture.taskbar`` records the normalized crop box with
each frame; this module only renders it. One extra image per batch is cheap and
gives the model the cue the taskbar gives a human: the highlighted icon is the
window that was really in use.

No pixel interpretation happens here on purpose. Guessing which of two wide
indicators is "focused" from colour alone is unreliable across Windows versions
and themes; the enlarged strip plus the deterministic app name in the frame
metadata is enough, and a wrong guess would be worse than no guess.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image, ImageDraw

#: Rows are capped: the batch never sends more images than this, and a taller
#: composite starts competing with the screenshots for attention.
MAX_ROWS = 8
#: Left margin holding ``F0 <app>``. ASCII on purpose: Pillow's default bitmap
#: font has no CJK glyphs, and app names come from the executable stem.
LABEL_WIDTH = 118
GAP = 3
BACKGROUND = (22, 26, 32)
TEXT_COLOR = (232, 236, 240)
MUTED_TEXT_COLOR = (160, 172, 184)
#: Wide enough that taskbar icons (and their indicator bars) survive the model's
#: image tokenization: one extra image of this size costs a few hundred tokens.
MAX_STRIP_WIDTH = 1200


def _ascii_label(text: str, limit: int = 16) -> str:
    """App names are executable stems; anything non-ASCII would render as tofu."""
    cleaned = "".join(char if 32 <= ord(char) < 127 else "?" for char in text or "")
    return cleaned[:limit]


def parse_strip(focus: str) -> tuple[float, float, float, float] | None:
    """Read the normalized taskbar box out of a frame's ``focus`` JSON."""
    if not focus:
        return None
    try:
        payload = json.loads(focus)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    box = payload.get("strip")
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(value) for value in box)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        return None
    return (x0, y0, x1, y1)


def _crop_row(path: Path, box: tuple[float, float, float, float], width: int) -> Image.Image | None:
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            w, h = img.size
            crop = img.crop(
                (int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h))
            )
    except (OSError, ValueError):
        return None
    if crop.width < 8 or crop.height < 4:
        return None
    height = max(6, round(crop.height * width / crop.width))
    return crop.resize((width, height), Image.Resampling.LANCZOS)


def build_focus_strip(
    frames: list, *, base: Path, width: int = MAX_STRIP_WIDTH, quality: int = 80
) -> bytes | None:
    """Stack the taskbars of the selected frames into one JPEG, or None."""
    rows: list[tuple[str, str, Image.Image]] = []
    for frame in frames:
        if len(rows) >= MAX_ROWS:
            break
        box = parse_strip(getattr(frame, "focus", ""))
        if box is None or not getattr(frame, "file", None):
            continue
        row = _crop_row(base / frame.file, box, width)
        if row is not None:
            label = f"F{len(rows)}"
            rows.append((label, _ascii_label(getattr(frame, "app", "")), row))
    if not rows:
        return None

    height = sum(row.height + GAP for _, _, row in rows) + GAP
    canvas = Image.new("RGB", (LABEL_WIDTH + width, height), BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    y = GAP
    for label, app, row in rows:
        canvas.paste(row, (LABEL_WIDTH, y))
        middle = y + max(0, row.height // 2 - 6)
        draw.text((6, middle), label, fill=TEXT_COLOR)
        draw.text((34, middle), app, fill=MUTED_TEXT_COLOR)
        y += row.height + GAP

    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()
