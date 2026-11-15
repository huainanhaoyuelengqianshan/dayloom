from __future__ import annotations

from dayloom import clock
from dayloom.llm.prompts import (
    CATEGORIES,
    SEGMENT_PROMPT_VERSION,
    SEGMENT_SYSTEM,
    build_segment_user,
    frame_line,
    screen_of,
    timeline_lines,
)
from dayloom.store.repo import FrameRow


def frame(
    fid: int,
    ts: float,
    *,
    app: str = "code",
    title: str = "main.py",
    monitor: str = "m1:3072x1920+0+0",
) -> FrameRow:
    return FrameRow(
        id=fid,
        ts=ts,
        day="2026-05-10",
        app=app,
        exe="",
        title=title,
        monitor=monitor,
        x=0,
        y=0,
        w=1,
        h=1,
        file=None,
        bytes=0,
        dhash=fid,
        batch_id=None,
    )


def test_screen_of_reads_the_short_label():
    assert screen_of("m2:2560x1440+-2560+236") == "m2"
    assert screen_of("") == "?"
    assert screen_of("garbage") == "garbage"


def test_frame_line_carries_the_display():
    f = frame(0, 1000.0, app="code", title="main.py", monitor="m2:2560x1440+-2560+236")
    assert frame_line(0, f) == f"[帧 0] {clock.fmt_hms(1000.0)} | m2 | code | main.py"


def test_frame_line_keeps_pipes_out_of_the_fields():
    f = frame(0, 1000.0, title="a | b")
    line = frame_line(0, f)
    assert line.count("|") == 3


def test_timeline_merges_consecutive_identical_windows():
    frames = [
        frame(0, 1000.0, app="wps", title="ch1", monitor="m1:3072x1920+0+0"),
        frame(1, 1020.0, app="wps", title="ch1", monitor="m1:3072x1920+0+0"),
        frame(2, 1040.0, app="wps", title="ch1", monitor="m1:3072x1920+0+0"),
        frame(3, 1060.0, app="weixin", title="微信", monitor="m2:2560x1440+-2560+236"),
        frame(4, 1080.0, app="weixin", title="微信", monitor="m2:2560x1440+-2560+236"),
    ]
    lines = timeline_lines(frames).splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("帧 0–2")
    assert "m1" in lines[0] and "wps" in lines[0]
    assert lines[1].startswith("帧 3–4")
    assert "m2" in lines[1] and "weixin" in lines[1]


def test_timeline_includes_frames_that_get_no_screenshot():
    frames = [frame(i, 1000.0 + i * 60, title=f"win{i}") for i in range(30)]
    lines = timeline_lines(frames).splitlines()
    assert len(lines) == 30
    assert any("win7" in line for line in lines)


def test_build_segment_user_counts_frames_and_images():
    frames = [frame(i, 1000.0 + i * 20, title=f"win{i}") for i in range(30)]
    images = frames[::5]
    intro = build_segment_user(
        day="2026-05-10",
        start_ts=1000.0,
        end_ts=1600.0,
        frames=frames,
        images=images,
        max_frames_notice="只给 6 帧配图",
    )
    assert "30 帧" in intro
    assert "其中 6 帧附了截图" in intro
    assert "帧 0–0" not in intro  # single-frame runs are not written as ranges
    assert "帧 0 |" in intro
    assert "win7" in intro
    assert "任务栏" in intro or "帧号" in intro


def test_segment_prompt_mentions_the_badge_and_the_multi_screen_rule():
    assert SEGMENT_PROMPT_VERSION == "segment-v3"
    assert "任务栏" in SEGMENT_SYSTEM
    assert "徽标" in SEGMENT_SYSTEM
    assert "屏" in SEGMENT_SYSTEM
    assert all(category in SEGMENT_SYSTEM for category in CATEGORIES)
