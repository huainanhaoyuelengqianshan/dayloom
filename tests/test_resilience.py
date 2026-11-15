"""Heartbeat liveness, fatal-error handling, report backfill."""

from __future__ import annotations

import time

from conftest import add_activity, add_batch, at

from dayloom import runtime
from dayloom.store.repo import STATUS_FAILED, STATUS_PENDING

DAY = "2026-05-10"


# -- liveness -----------------------------------------------------------
def test_stale_heartbeat_means_not_running(tmp_path, monkeypatch):
    """A crashed daemon used to look alive because its pid got reused."""
    runtime.write_pid(tmp_path, 99999)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: True)

    runtime.touch_heartbeat(tmp_path)
    assert runtime.running_pid(tmp_path) == 99999

    # Age the heartbeat past the threshold.
    heartbeat = tmp_path / ".heartbeat"
    heartbeat.write_text(str(int(time.time()) - int(runtime.STALE_HEARTBEAT_SECONDS) - 5))
    assert runtime.running_pid(tmp_path) is None


def test_missing_heartbeat_keeps_old_behaviour(tmp_path, monkeypatch):
    """Daemons from before the heartbeat existed must still count as running."""
    runtime.write_pid(tmp_path, 4242)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: True)
    (tmp_path / ".heartbeat").unlink(missing_ok=True)
    assert runtime.running_pid(tmp_path) == 4242


# -- fatal errors -------------------------------------------------------
def test_release_batch_does_not_spend_an_attempt(repo):
    batch = add_batch(repo, DAY, at(10, 0), at(10, 15), status="pending")
    assert repo.claim_batch(batch)
    assert repo.db.scalar("SELECT attempts FROM batches WHERE id = ?", (batch,)) == 1

    repo.release_batch(batch, "余额不足")
    row = repo.db.one("SELECT status, attempts, error FROM batches WHERE id = ?", (batch,))
    assert row["status"] == STATUS_PENDING
    assert row["attempts"] == 0
    assert "余额不足" in row["error"]


def test_fatal_status_detection():
    from dayloom.llm.provider import LLMHTTPError, is_fatal_error

    assert is_fatal_error(LLMHTTPError(402, "no balance"))
    assert is_fatal_error(LLMHTTPError(401, "bad key"))
    assert not is_fatal_error(LLMHTTPError(500, "boom"))
    assert not is_fatal_error(RuntimeError("x"))


# -- reports ------------------------------------------------------------
def test_missing_reports_are_filled_in(cfg, repo):
    """A rollup skipped while the machine slept must be regenerated later."""
    from dayloom.analyzer.report import build_report

    batch = add_batch(repo, DAY, at(9, 0), at(9, 30), status="done")
    add_activity(
        repo, batch, 0, at(9, 0), at(9, 30), app="code", title="写代码", summary="写代码"
    )
    report = build_report(repo, cfg, DAY, narrate=False)
    assert report.tracked_seconds > 0

    listed = repo.list_reports(10)
    assert [r["day"] for r in listed] == [DAY]
    assert DAY in repo.report_days()


def test_report_list_reports_without_narrative(cfg, repo):
    from dayloom.analyzer.report import build_report

    batch = add_batch(repo, DAY, at(14, 0), at(14, 20), status="done")
    add_activity(
        repo, batch, 0, at(14, 0), at(14, 20), app="browser", title="刷网页", summary="刷网页"
    )
    build_report(repo, cfg, DAY, narrate=False)
    row = repo.get_report(DAY)
    assert row is not None
    assert row["narrative"] == ""
    assert row["categories"]


def test_failed_batch_can_be_requeued_for_a_new_endpoint(repo):
    batch = add_batch(repo, DAY, at(10, 0), at(10, 15), status="failed")
    repo.mark_failed(batch, "请求失败：[WinError 10061]")
    assert repo.db.scalar("SELECT status FROM batches WHERE id = ?", (batch,)) == STATUS_FAILED
    repo.db.execute("UPDATE batches SET status='pending', attempts=0 WHERE status='failed'")
    assert repo.next_pending_batch(max_attempts=3) is not None
