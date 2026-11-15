from __future__ import annotations

from conftest import add_batch, add_frame, at

from dayloom.store.db import open_db
from dayloom.store.repo import NewActivity

DAY = "2026-05-10"


def test_migrations_are_idempotent(cfg):
    db = open_db(cfg.db_path)
    first = db.schema_version()
    db.close()
    db = open_db(cfg.db_path)
    assert db.schema_version() == first == 5
    db.close()


def test_replace_activities_is_idempotent(cfg, repo):
    batch = add_batch(repo, DAY, at(10, 0), at(10, 15))
    activities = [
        NewActivity(at(10, 0), at(10, 5), "code", "coding", "a", "x", 0.9, [1]),
        NewActivity(at(10, 5), at(10, 10), "code", "coding", "b", "y", 0.8, [2]),
    ]
    repo.replace_activities(batch, activities)
    repo.replace_activities(batch, activities)
    count = repo.db.scalar("SELECT COUNT(*) FROM activities WHERE batch_id = ?", (batch,))
    assert count == 2


def test_gap_lifecycle(repo):
    repo.set_presence("idle", 100.0)
    repo.set_presence("idle", 110.0)  # still the same open gap
    gaps = repo.gaps_between(0, 1000)
    assert len(gaps) == 1
    assert gaps[0].end_ts is None

    repo.set_presence(None, 200.0)
    gaps = repo.gaps_between(0, 1000)
    assert gaps[0].end_ts == 200.0
    assert gaps[0].duration(300.0) == 100.0


def test_switching_gap_kind_closes_the_previous_one(repo):
    repo.set_presence("idle", 100.0)
    repo.set_presence("locked", 150.0)
    gaps = {g.kind: g for g in repo.gaps_between(0, 1000)}
    assert gaps["idle"].end_ts == 150.0
    assert gaps["locked"].end_ts is None


def test_ensure_batches_only_creates_closed_windows(cfg, repo):
    for i in range(6):
        add_frame(repo, cfg.home, at(10, 0) + i * 60)

    created = repo.ensure_batches(
        day=DAY, batch_seconds=900, grace_seconds=0, now=at(10, 20), boundary_hour=4
    )
    assert created == 1
    again = repo.ensure_batches(
        day=DAY, batch_seconds=900, grace_seconds=0, now=at(10, 20), boundary_hour=4
    )
    assert again == 0

    pending = repo.next_pending_batch(max_attempts=3)
    assert pending is not None
    assert pending.start_ts == at(10, 0)
    assert len(repo.frames_for_batch(pending.id)) == 6


def test_grace_period_holds_batches_back(cfg, repo):
    add_frame(repo, cfg.home, at(10, 0))
    created = repo.ensure_batches(
        day=DAY, batch_seconds=900, grace_seconds=3600, now=at(10, 10), boundary_hour=4
    )
    assert created == 0


def test_open_window_is_not_batched_early(cfg, repo):
    """Regression: a window that is still collecting frames must wait.

    Comparing only the window *start* to the cutoff batched the window 15
    minutes early; frames arriving later were then never analyzed.
    """
    add_frame(repo, cfg.home, at(10, 0))
    add_frame(repo, cfg.home, at(10, 5))
    # Window [10:00, 10:15) is still open at 10:10.
    created = repo.ensure_batches(
        day=DAY, batch_seconds=900, grace_seconds=0, now=at(10, 10), boundary_hour=4
    )
    assert created == 0
    # Once it closes, both frames land in the same batch.
    created = repo.ensure_batches(
        day=DAY, batch_seconds=900, grace_seconds=0, now=at(10, 16), boundary_hour=4
    )
    assert created == 1
    batch = repo.next_pending_batch(max_attempts=3)
    assert batch is not None
    assert len(repo.frames_for_batch(batch.id)) == 2


def test_ensure_batches_terminates_when_the_day_is_over(cfg, repo):
    """Regression: a fully finished day must not spin forever.

    The window walk used to compare ``end == cutoff`` on the last (clipped)
    window, so it never broke out of the loop — the daemon then hung on every
    tick and never analyzed anything.
    """
    import threading

    add_frame(repo, cfg.home, at(10, 0, day="2026-05-09"))
    result: dict[str, int] = {}

    def run() -> None:
        result["n"] = repo.ensure_batches(
            day="2026-05-09",
            batch_seconds=60,
            grace_seconds=0,
            now=at(10, 0),  # after the day ended -> cutoff == end
            boundary_hour=4,
        )

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive(), "ensure_batches 在整天结束后死循环了"
    assert result["n"] == 1


def test_claim_batch_is_exclusive(repo):
    batch = add_batch(repo, DAY, at(10, 0), at(10, 15), status="pending")
    assert repo.claim_batch(batch) is True
    assert repo.claim_batch(batch) is False


def test_stale_running_batches_are_reclaimed(repo):
    batch = add_batch(repo, DAY, at(10, 0), at(10, 15), status="pending")
    assert repo.claim_batch(batch)
    # A process killed mid-analysis leaves it 'running' with an old timestamp.
    repo.db.execute(
        "UPDATE batches SET updated_at = ? WHERE id = ?", (at(9, 0), batch)
    )
    assert repo.reclaim_stale_running(600) == 1
    status = repo.db.scalar("SELECT status FROM batches WHERE id = ?", (batch,))
    assert status == "pending"


def test_images_used_per_day_tracks_what_was_sent(cfg, repo):
    from dayloom.scheduler import image_budget_left

    batch = add_batch(repo, DAY, at(10, 0), at(10, 15), status="pending")
    assert repo.claim_batch(batch)
    repo.mark_done(
        batch, model="m", prompt_version="v1", tokens_in=100, tokens_out=10, images=6
    )
    assert repo.images_used_on(DAY) == 6
    assert repo.images_used_on("2026-05-11") == 0

    # No budget configured -> None (unlimited), not 0.
    assert image_budget_left(cfg, repo, DAY) is None

    cfg.analysis.max_images_per_day = 10
    assert image_budget_left(cfg, repo, DAY) == 4

    cfg.analysis.max_images_per_day = 6
    assert image_budget_left(cfg, repo, DAY) == 0  # exhausted, not negative


def test_large_dhash_survives_the_database(cfg, repo):
    """Regression: a u64 hash with the top bit set used to raise OverflowError.

    That silently dropped the frame (and spammed the log once per tick).
    """
    from dayloom.capture.dedupe import hamming, to_signed

    top = 2**64 - 1
    add_frame(repo, cfg.home, at(9, 0), dhash_value=top)
    add_frame(repo, cfg.home, at(9, 1), dhash_value=1)

    frames = repo.frames_between(at(9, 0), at(9, 2))
    assert len(frames) == 2
    assert frames[0].dhash == to_signed(top)
    assert hamming(frames[0].dhash, frames[1].dhash) == 63


def test_retention_deletes_old_frames(cfg, repo):
    add_frame(repo, cfg.home, at(9, 0))
    add_frame(repo, cfg.home, at(11, 0))
    removed = repo.delete_frames_before(at(10, 0))
    assert len(removed) == 1
    assert repo.count_frames() == 1


def test_settings_roundtrip(repo):
    repo.set_setting("foo", {"a": 1})
    assert repo.get_setting("foo") == {"a": 1}
    assert repo.get_setting("missing", "fallback") == "fallback"
