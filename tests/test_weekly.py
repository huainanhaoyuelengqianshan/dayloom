"""Weekly rollup: bounds, aggregation, persistence and rendering."""

from __future__ import annotations

from conftest import add_activity, add_batch

from dayloom import clock
from dayloom.analyzer.weekly import build_week, compute_week, render_week, week_bounds
from dayloom.llm.mockvlm import MockVLM
from dayloom.llm.provider import VisionClient

# 2026-05-10 is a Sunday -> the week runs 2026-05-04 (Mon) .. 2026-05-10 (Sun)
DAY = "2026-05-10"
MONDAY = "2026-05-04"


def test_week_bounds_start_on_monday():
    assert week_bounds(DAY) == (MONDAY, DAY)
    assert week_bounds(MONDAY) == (MONDAY, DAY)
    assert clock.weekday_label(MONDAY) == "周一"


def _seed_day(repo, day: str, hour: int, minutes: int, category: str = "coding") -> None:
    start = clock.day_bounds(day, 4)[0] + hour * 3600
    batch = add_batch(repo, day, start, start + minutes * 60)
    add_activity(
        repo, batch, 1, start, start + minutes * 60, app="code", category=category
    )


def test_week_aggregates_the_days(cfg, repo):
    _seed_day(repo, "2026-05-04", 9, 60)
    _seed_day(repo, "2026-05-05", 10, 30, category="communication")
    _seed_day(repo, "2026-05-10", 14, 90)

    report = compute_week(repo, cfg, DAY)
    assert report.start_day == MONDAY
    assert report.end_day == DAY
    assert abs(report.tracked_seconds - (3600 + 1800 + 5400)) < 2
    assert abs(report.categories["coding"] - 9000) < 2
    assert len(report.daily) == 7
    assert report.best_day is not None
    assert report.best_day.day == "2026-05-10"  # 90 minutes


def test_week_ignores_other_weeks(cfg, repo):
    _seed_day(repo, MONDAY, 9, 60)
    _seed_day(repo, "2026-04-27", 9, 60)  # previous week
    report = compute_week(repo, cfg, DAY)
    assert abs(report.tracked_seconds - 3600) < 2


def test_week_without_data_is_honest(cfg, repo):
    report = compute_week(repo, cfg, DAY)
    assert report.tracked_seconds == 0
    assert "还没有数据" in render_week(report)


def test_week_render_has_bars_and_categories(cfg, repo):
    _seed_day(repo, MONDAY, 9, 120)
    _seed_day(repo, "2026-05-06", 13, 60)
    out = render_week(compute_week(repo, cfg, DAY))
    assert "周一" in out and "周三" in out
    assert "█" in out
    assert "写代码" in out


def test_week_persists_a_narrative(cfg, repo):
    _seed_day(repo, MONDAY, 9, 90)
    with MockVLM() as server:
        cfg.llm.base_url = server.base_url
        cfg.llm.model = "mock-vlm"
        client = VisionClient(cfg.llm)
        try:
            report = build_week(repo, cfg, DAY, narrate=True, client=client)
        finally:
            client.close()

    assert "mock 叙述" in report.narrative
    stored = repo.get_week(report.start_day)
    assert stored is not None
    assert stored["tracked_seconds"] > 0
    assert "mock 叙述" in stored["narrative"]

    # A later read-only pass reuses the stored prose.
    again = compute_week(repo, cfg, DAY)
    assert "mock 叙述" in again.narrative
