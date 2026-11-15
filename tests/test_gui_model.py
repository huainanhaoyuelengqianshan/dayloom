from __future__ import annotations

from conftest import add_activity, add_batch, add_frame, at

from dayloom import clock
from dayloom.gui.model import load_day, load_status

DAY = "2026-05-10"


def test_load_day_without_database(home, cfg):
    view = load_day(cfg, DAY)
    assert view.has_db is False
    assert view.sessions == []


def test_load_day_builds_sessions(cfg, repo):
    batch = add_batch(repo, DAY, at(9, 0), at(9, 30))
    add_activity(
        repo, batch, 1, at(9, 0), at(9, 20), app="code", category="coding",
        title="修一个 bug", summary="在 dayloom 里调批次逻辑",
    )
    add_activity(
        repo, batch, 2, at(9, 20), at(9, 30), app="wechat", category="communication",
        title="回消息", summary="回复同事",
    )

    view = load_day(cfg, DAY)
    assert view.has_db is True
    assert len(view.sessions) == 2
    assert view.sessions[0].title == "修一个 bug"
    assert view.sessions[0].summary.startswith("在 dayloom 里")
    assert view.sessions[0].range_label == "09:00 – 09:20"
    assert set(view.categories) == {"coding", "communication"}
    assert abs(view.tracked_seconds - 1800) < 1


def test_load_day_picks_a_thumbnail(cfg, repo):
    batch = add_batch(repo, DAY, at(9, 0), at(9, 30))
    add_activity(repo, batch, 1, at(9, 0), at(9, 30), app="code", category="coding")
    add_frame(repo, cfg.home, at(9, 5), app="code")

    view = load_day(cfg, DAY)
    assert view.sessions[0].thumb is not None
    assert view.sessions[0].thumb.exists()
    assert view.frame_count == 1


def test_load_day_reports_unanalyzed_frames(cfg, repo):
    add_frame(repo, cfg.home, at(9, 5))
    view = load_day(cfg, DAY)
    assert view.unanalyzed == 1


def test_narrative_comes_from_the_stored_report(cfg, repo):
    repo.upsert_report(
        day=DAY,
        tracked_seconds=60,
        categories={"coding": 60},
        apps={"code": 60},
        narrative="昨天的复盘",
        highlights=["亮点一"],
        suggestions=[],
        model="m",
        prompt_version="v1",
    )
    view = load_day(cfg, DAY)
    assert view.narrative == "昨天的复盘"
    assert view.highlights == ["亮点一"]


def test_status_snapshot_has_the_keys_the_window_needs(cfg, repo):
    status = load_status(cfg)
    for key in ("running_pid", "paused", "last_frame_ts", "frames_today", "batches", "current_gap"):
        assert key in status


def test_short_duration_format():
    assert clock.fmt_duration_short(30) == "<1m"
    assert clock.fmt_duration_short(540) == "9m"
    assert clock.fmt_duration_short(4700) == "1h18m"
