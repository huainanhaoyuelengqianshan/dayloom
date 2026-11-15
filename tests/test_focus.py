from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image, ImageDraw

from dayloom.analyzer.focus import (
    LABEL_WIDTH,
    MAX_ROWS,
    build_focus_strip,
    parse_strip,
)
from dayloom.store.repo import FrameRow

STRIP = {"strip": [0.0, 0.9, 1.0, 1.0], "edge": "bottom"}


def make_frame(
    tmp_path: Path,
    name: str,
    *,
    color: tuple[int, int, int] = (200, 40, 40),
    focus: str | None = None,
    fid: int = 0,
) -> FrameRow:
    image = Image.new("RGB", (320, 200), (30, 30, 30))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 180, 320, 200], fill=color)
    rel = Path("frames") / f"{name}.png"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return FrameRow(
        id=fid,
        ts=1000.0 + fid,
        day="2026-05-10",
        app="code",
        exe="",
        title="t",
        monitor="m1:1920x1080+0+0",
        x=0,
        y=0,
        w=320,
        h=200,
        file=str(rel),
        bytes=1,
        dhash=0,
        batch_id=None,
        focus="" if focus is None else focus,
    )


def test_parse_strip_accepts_the_recorded_shape():
    assert parse_strip(json.dumps(STRIP)) == (0.0, 0.9, 1.0, 1.0)


def test_parse_strip_rejects_garbage():
    assert parse_strip("") is None
    assert parse_strip("not json") is None
    assert parse_strip(json.dumps({"nope": 1})) is None
    assert parse_strip(json.dumps({"strip": [0.5, 0.5, 0.5, 1.0]})) is None
    assert parse_strip(json.dumps({"strip": [0.0, 0.5, 1.5, 1.0]})) is None


def test_build_focus_strip_stacks_the_rows(tmp_path):
    frames = [
        make_frame(tmp_path, "a", color=(220, 30, 30), focus=json.dumps(STRIP), fid=1),
        make_frame(tmp_path, "b", color=(30, 220, 30), focus=json.dumps(STRIP), fid=2),
    ]
    data = build_focus_strip(frames, base=tmp_path, width=200)
    assert data
    with Image.open(io.BytesIO(data)) as img:
        assert img.format == "JPEG"
        assert img.width == LABEL_WIDTH + 200
        assert img.height > 20
        raw = img.convert("RGB").tobytes()
        red = sum(1 for i in range(0, len(raw), 3) if raw[i] > 150 and raw[i + 1] < 90)
        green = sum(1 for i in range(0, len(raw), 3) if raw[i + 1] > 150 and raw[i] < 90)
        assert red > 100 and green > 100


def test_build_focus_strip_returns_none_without_evidence(tmp_path):
    frames = [make_frame(tmp_path, "plain", fid=1)]
    assert build_focus_strip(frames, base=tmp_path) is None


def test_build_focus_strip_caps_the_row_count(tmp_path):
    frames = [
        make_frame(tmp_path, f"f{i}", focus=json.dumps(STRIP), fid=i)
        for i in range(MAX_ROWS + 4)
    ]
    data = build_focus_strip(frames, base=tmp_path, width=120)
    assert data
    with Image.open(io.BytesIO(data)) as img:
        single_row = 20 * 120 // 320 + 3  # strip height scaled to 120 then + gap
        assert img.height <= (single_row + 3) * MAX_ROWS + 10
