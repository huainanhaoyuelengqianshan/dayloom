from __future__ import annotations

from conftest import add_activity, add_batch, add_frame, at

from dayloom.analyzer.report import (
    build_report,
    merge_intervals,
    render,
    subtract_intervals,
    total_seconds,
)
from dayloom.llm.mockvlm import MockVLM
from dayloom.llm.provider import VisionClient

DAY = "2026-05-10"


def test_interval_helpers():
    assert merge_intervals([(0, 10), (5, 15)]) == [(0, 15)]
    assert merge_intervals([(30, 40), (0, 10)]) == [(0, 10), (30, 40)]
    assert subtract_intervals([(0, 100)], [(20, 30), (50, 60)]) == [
        (0, 20),
        (30, 50),
        (60, 100),
    ]
    assert total_seconds([(0, 10), (5, 15)]) == 15


def test_idle_time_is_not_counted_as_activity(cfg, repo):
    batch = add_batch(repo, DAY, at(10, 0), at(10, 15))
    add_activity(repo, batch, 1, at(10, 0), at(10, 30), category="coding")
    repo.set_presence("idle", at(10, 10))
    repo.set_presence(None, at(10, 20))

    report = build_report(repo, cfg, DAY)
    assert report.tracked_seconds == 1200.0
    assert report.categories["coding"] == 1200.0
    assert len(report.sessions) == 1


def test_report_counts_sessions_and_apps(cfg, repo):
    batch = add_batch(repo, DAY, at(10, 0), at(10, 15))
    add_activity(repo, batch, 1, at(10, 0), at(10, 10), app="code", category="coding")
    add_activity(repo, batch, 2, at(10, 10), at(10, 20), app="chrome", category="browsing")

    report = build_report(repo, cfg, DAY)
    assert set(report.categories) == {"coding", "browsing"}
    assert set(report.apps) == {"code", "chrome"}
    assert report.tracked_seconds == 1200.0


def test_unanalyzed_frames_are_reported(cfg, repo):
    add_frame(repo, cfg.home, at(10, 0))
    add_frame(repo, cfg.home, at(10, 1))
    report = build_report(repo, cfg, DAY)
    assert report.unanalyzed_frames == 2


def test_existing_narrative_survives_a_numbers_only_run(cfg, repo):
    repo.upsert_report(
        day=DAY,
        tracked_seconds=60,
        categories={"coding": 60},
        apps={"code": 60},
        narrative="这天的旧叙述",
        highlights=["亮点"],
        suggestions=[],
        model="m",
        prompt_version="v1",
    )
    report = build_report(repo, cfg, DAY)
    assert report.narrative == "这天的旧叙述"
    assert report.highlights == ["亮点"]


def test_render_shows_timeline(cfg, repo):
    batch = add_batch(repo, DAY, at(10, 0), at(10, 15))
    add_activity(
        repo, batch, 1, at(10, 0), at(10, 30), app="code", category="coding", title="改去重阈值"
    )
    out = render(build_report(repo, cfg, DAY))
    assert "10:00" in out
    assert "写代码" in out
    assert "改去重阈值" in out


def test_empty_day_renders_a_clear_message(cfg, repo):
    out = render(build_report(repo, cfg, DAY))
    assert "还没有分析结果" in out


def test_narration_uses_the_client(cfg, repo):
    batch = add_batch(repo, DAY, at(10, 0), at(10, 15))
    add_activity(repo, batch, 1, at(10, 0), at(10, 30), category="coding", summary="写代码")
    with MockVLM() as server:
        cfg.llm.base_url = server.base_url
        cfg.llm.model = "mock-vlm"
        client = VisionClient(cfg.llm)
        try:
            report = build_report(repo, cfg, DAY, narrate=True, client=client)
        finally:
            client.close()
    assert "mock 叙述" in report.narrative
    assert report.highlights
    # numbers still come from SQL, not from the model
    assert report.tracked_seconds == 1800.0


def test_narration_failure_keeps_the_numbers(cfg, repo):
    batch = add_batch(repo, DAY, at(10, 0), at(10, 15))
    add_activity(repo, batch, 1, at(10, 0), at(10, 10), category="coding")

    class Boom:
        def complete_json(self, *args, **kwargs):
            raise RuntimeError("boom")

    report = build_report(repo, cfg, DAY, narrate=True, client=Boom())  # type: ignore[arg-type]
    assert report.tracked_seconds == 600.0
    assert report.narrative == ""
