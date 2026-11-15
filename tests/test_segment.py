from __future__ import annotations

import json

import pytest
from conftest import add_frame, at

from dayloom.analyzer.report import build_report
from dayloom.analyzer.segment import analyze_batch, parse_activities
from dayloom.llm.mockvlm import MockVLM
from dayloom.llm.provider import LLMFormatError, Usage, VisionClient
from dayloom.store.repo import FrameRow

DAY = "2026-05-10"
FOCUS = json.dumps({"strip": [0.0, 0.9, 1.0, 1.0], "edge": "bottom"})


def frame(ts: float, fid: int, app: str = "code") -> FrameRow:
    return FrameRow(
        id=fid,
        ts=ts,
        day=DAY,
        app=app,
        exe="",
        title="t",
        monitor="",
        x=0,
        y=0,
        w=1,
        h=1,
        file=None,
        bytes=0,
        dhash=fid,
        batch_id=None,
    )


def test_parse_activities_is_contiguous_and_clamped():
    frames = [frame(1000 + i, i) for i in range(10)]
    payload = {
        "activities": [
            {
                "from_frame": 0,
                "to_frame": 4,
                "app": "code",
                "category": "coding",
                "summary": "a",
                "confidence": 2.0,
            },
            {
                "from_frame": 2,
                "to_frame": 99,
                "app": "code",
                "category": "not-a-category",
                "summary": "b",
            },
        ]
    }
    acts = parse_activities(payload, frames)
    assert len(acts) == 2
    assert acts[0].end_ts == frames[4].ts
    assert acts[1].start_ts == frames[4].ts
    assert acts[1].end_ts == frames[-1].ts
    assert acts[0].confidence == 1.0
    assert acts[1].category == "other"


def test_parse_activities_rejects_an_empty_answer():
    with pytest.raises(LLMFormatError):
        parse_activities({"activities": []}, [frame(0, 0)])


def test_mock_reads_the_frame_metadata_we_actually_send():
    from dayloom.llm.mockvlm import parse_frame_line

    assert parse_frame_line("[帧 3] 10:20:00 | code | main.py") == (3, "code", "main.py")
    assert parse_frame_line("帧 3 | 10:20:00 | code | main.py") == (3, "code", "main.py")
    # segment-v3 adds the display and merges runs into ranges.
    assert parse_frame_line("[帧 3] 10:20:00 | m2 | code | main.py") == (3, "code", "main.py")
    assert parse_frame_line(
        "帧 12–17 | 18:34:26–18:36:25 | m2 | gameviewer | 华硕主机"
    ) == (12, "gameviewer", "华硕主机")
    assert parse_frame_line("这不是帧头") is None


def test_parse_activities_rejects_garbage():
    with pytest.raises(LLMFormatError):
        parse_activities({"nope": 1}, [frame(0, 0)])


def test_batch_window_edges_are_covered(cfg, repo):
    """Keyframes clustered at the start must not shrink a segment to a minute."""
    for i in range(3):
        add_frame(repo, cfg.home, at(10, 1) + i * 10, app="code", title=f"t{i}", dhash_value=i)
    repo.ensure_batches(
        day=DAY, batch_seconds=900, grace_seconds=0, now=at(10, 20), boundary_hour=4
    )
    batch = repo.next_pending_batch(max_attempts=3)
    assert batch is not None
    assert repo.claim_batch(batch.id)

    with MockVLM() as server:
        cfg.llm.base_url = server.base_url
        cfg.llm.model = "mock-vlm"
        client = VisionClient(cfg.llm)
        try:
            analyze_batch(cfg=cfg, repo=repo, client=client, batch=batch)
        finally:
            client.close()

    acts = repo.activities_between(at(10, 0), at(10, 30))
    assert acts
    assert acts[0].start_ts == batch.start_ts
    assert acts[-1].end_ts == batch.end_ts


def test_idle_gap_bounds_the_batch_extension(cfg, repo):
    """A two-minute work burst must not become fifteen minutes of tracked time.

    The window clamp used to stretch the last segment to the batch end; an
    idle/locked gap inside the batch now stops the stretch (media is exempt —
    see the test below).
    """
    add_frame(repo, cfg.home, at(10, 1), app="code", title="main.py", dhash_value=1)
    repo.db.execute(
        "INSERT INTO gaps (kind, start_ts, end_ts) VALUES ('idle', ?, ?)",
        (at(10, 3), at(10, 12)),
    )
    repo.ensure_batches(
        day=DAY, batch_seconds=900, grace_seconds=0, now=at(10, 20), boundary_hour=4
    )
    batch = repo.next_pending_batch(max_attempts=3)
    assert batch is not None
    assert repo.claim_batch(batch.id)

    with MockVLM() as server:
        cfg.llm.base_url = server.base_url
        cfg.llm.model = "mock-vlm"
        client = VisionClient(cfg.llm)
        try:
            analyze_batch(cfg=cfg, repo=repo, client=client, batch=batch)
        finally:
            client.close()

    acts = repo.activities_between(at(10, 0), at(10, 30))
    assert acts
    assert acts[0].start_ts == batch.start_ts          # fills back to the window
    assert acts[-1].end_ts == at(10, 3)                # but stops at the idle gap


def test_media_extends_across_a_short_idle_gap(cfg, repo):
    """Watching a video is silent for the keyboard; that must not cut it off."""
    add_frame(
        repo, cfg.home, at(10, 1), app="potplayer", title="电影", dhash_value=1
    )
    repo.db.execute(
        "INSERT INTO gaps (kind, start_ts, end_ts) VALUES ('idle', ?, ?)",
        (at(10, 3), at(10, 12)),
    )
    repo.ensure_batches(
        day=DAY, batch_seconds=900, grace_seconds=0, now=at(10, 20), boundary_hour=4
    )
    batch = repo.next_pending_batch(max_attempts=3)
    assert batch is not None
    assert repo.claim_batch(batch.id)

    with MockVLM() as server:
        cfg.llm.base_url = server.base_url
        cfg.llm.model = "mock-vlm"
        client = VisionClient(cfg.llm)
        try:
            analyze_batch(cfg=cfg, repo=repo, client=client, batch=batch)
        finally:
            client.close()

    acts = repo.activities_between(at(10, 0), at(10, 30))
    assert acts
    assert acts[-1].category == "media"
    # The idle gap is short enough to be "watching", so the segment reaches the
    # batch end instead of stopping at the gap.
    assert acts[-1].end_ts == batch.end_ts


def test_media_does_not_extend_across_a_long_idle_gap(cfg, repo):
    """An abandoned tab must not become an hour of media."""
    add_frame(
        repo, cfg.home, at(10, 1), app="potplayer", title="电影", dhash_value=1
    )
    # 2 hours of no input, longer than capture.media_idle_seconds.
    repo.db.execute(
        "INSERT INTO gaps (kind, start_ts, end_ts) VALUES ('idle', ?, ?)",
        (at(10, 3), at(10, 3) + 7200),
    )
    repo.ensure_batches(
        day=DAY, batch_seconds=900, grace_seconds=0, now=at(10, 20), boundary_hour=4
    )
    batch = repo.next_pending_batch(max_attempts=3)
    assert batch is not None
    assert repo.claim_batch(batch.id)

    with MockVLM() as server:
        cfg.llm.base_url = server.base_url
        cfg.llm.model = "mock-vlm"
        client = VisionClient(cfg.llm)
        try:
            analyze_batch(cfg=cfg, repo=repo, client=client, batch=batch)
        finally:
            client.close()

    acts = repo.activities_between(at(10, 0), at(10, 30))
    assert acts
    assert acts[-1].end_ts == at(10, 3)


def test_analyze_batch_keeps_dropped_frames_in_the_prompt(cfg, repo, monkeypatch):
    """Frames cut from the image budget must still reach the model as metadata.

    Two monitors make a 15-minute batch alternate between short states; if only
    the surviving keyframes are described, the model reports the whole window
    as whatever the last few frames showed.
    """
    for i in range(40):
        add_frame(
            repo,
            cfg.home,
            at(10, 0) + i * 20,
            app="wps" if i % 2 else "tabbit browser",
            title=f"win{i}",
            dhash_value=i,
            focus=FOCUS,
        )
    repo.ensure_batches(
        day=DAY, batch_seconds=900, grace_seconds=0, now=at(10, 20), boundary_hour=4
    )
    batch = repo.next_pending_batch(max_attempts=3)
    assert batch is not None
    assert repo.claim_batch(batch.id)

    captured: dict = {}

    def fake_complete_json(self, parts, *, system=None, hint=""):
        captured["parts"] = parts
        payload = {
            "activities": [
                {
                    "from_frame": 0,
                    "to_frame": 39,
                    "app": "wps",
                    "category": "writing",
                    "title": "t",
                    "summary": "整理文档",
                    "confidence": 0.8,
                }
            ]
        }
        return payload, Usage()

    monkeypatch.setattr(VisionClient, "complete_json", fake_complete_json)
    client = VisionClient(cfg.llm)
    try:
        outcome = analyze_batch(cfg=cfg, repo=repo, client=client, batch=batch)
    finally:
        client.close()

    parts = captured["parts"]
    text = "".join(part.text for part in parts if part.kind == "text")
    assert "win7" in text  # a frame that did not survive the image budget
    assert "帧 0" in text
    assert "任务栏条" in text  # the badge strip is sent along
    assert outcome.frames_sent == cfg.llm.max_frames
    assert sum(1 for part in parts if part.kind == "image") == cfg.llm.max_frames + 1


def test_analyze_batch_end_to_end(cfg, repo):
    for i in range(6):
        add_frame(
            repo, cfg.home, at(10, 0) + i * 60, app="code", title=f"file{i}.py", dhash_value=i
        )
    repo.ensure_batches(
        day=DAY, batch_seconds=900, grace_seconds=0, now=at(10, 20), boundary_hour=4
    )
    batch = repo.next_pending_batch(max_attempts=3)
    assert batch is not None
    assert repo.claim_batch(batch.id)

    with MockVLM() as server:
        cfg.llm.base_url = server.base_url
        cfg.llm.model = "mock-vlm"
        client = VisionClient(cfg.llm)
        try:
            outcome = analyze_batch(cfg=cfg, repo=repo, client=client, batch=batch)
        finally:
            client.close()

    assert outcome.activities >= 1
    assert 0 < outcome.frames_sent <= cfg.llm.max_frames
    acts = repo.activities_between(at(10, 0), at(10, 30))
    assert acts
    assert all(a.end_ts > a.start_ts for a in acts)
    assert all(a.category for a in acts)
    for prev, cur in zip(acts, acts[1:], strict=False):
        assert cur.start_ts >= prev.end_ts

    status = repo.db.scalar("SELECT status FROM batches WHERE id = ?", (batch.id,))
    assert status == "done"

    report = build_report(repo, cfg, DAY)
    assert report.tracked_seconds > 0
    assert report.unanalyzed_frames == 0
    assert report.categories
