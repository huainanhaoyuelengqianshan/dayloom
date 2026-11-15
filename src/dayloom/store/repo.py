"""Typed data access.

Every method here is intentionally small and boring: the interesting logic
(which frames to send, how to merge segments) lives in ``analyzer`` where it
can be unit tested without a database.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .. import clock
from ..store.db import Db

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


@dataclass(slots=True)
class FrameRow:
    id: int
    ts: float
    day: str
    app: str
    exe: str
    title: str
    monitor: str
    x: int | None
    y: int | None
    w: int | None
    h: int | None
    file: str | None
    bytes: int | None
    dhash: int | None
    batch_id: int | None
    #: JSON with the taskbar strip crop inside this frame, ``""`` for old rows.
    #: See ``capture.taskbar``; the analyzer turns it into the focus strip image.
    focus: str = ""


@dataclass(slots=True)
class BatchRow:
    id: int
    day: str
    start_ts: float
    end_ts: float
    status: str
    attempts: int
    error: str | None
    model: str | None
    prompt_version: str | None
    tokens_in: int
    tokens_out: int


@dataclass(slots=True)
class NewActivity:
    start_ts: float
    end_ts: float
    app: str
    category: str
    title: str
    summary: str
    confidence: float
    frame_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class ActivityRow:
    id: int
    batch_id: int
    seq: int
    start_ts: float
    end_ts: float
    app: str
    category: str
    title: str
    summary: str
    confidence: float
    frame_ids: list[int]


@dataclass(slots=True)
class SessionRow:
    day: str
    start_ts: float
    end_ts: float
    app: str
    category: str
    title: str
    activity_ids: list[int]


@dataclass(slots=True)
class GapRow:
    kind: str
    start_ts: float
    end_ts: float | None

    def duration(self, now: float) -> float:
        end = self.end_ts if self.end_ts is not None else now
        return max(0.0, end - self.start_ts)


def _report_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "day": row["day"],
        "tracked_seconds": row["tracked_seconds"],
        "categories": json.loads(row["categories"] or "{}"),
        "apps": json.loads(row["apps"] or "{}"),
        "narrative": row["narrative"],
        "highlights": json.loads(row["highlights"] or "[]"),
        "suggestions": json.loads(row["suggestions"] or "[]"),
        "model": row["model"],
        "prompt_version": row["prompt_version"],
        "created_at": row["created_at"],
    }


def _frame(row: sqlite3.Row) -> FrameRow:
    # A read-only client can be looking at a database the daemon has not
    # migrated yet; the column simply is not there until the next start.
    keys = row.keys()
    return FrameRow(
        id=row["id"],
        ts=row["ts"],
        day=row["day"],
        app=row["app"],
        exe=row["exe"],
        title=row["title"],
        monitor=row["monitor"],
        x=row["x"],
        y=row["y"],
        w=row["w"],
        h=row["h"],
        file=row["file"],
        bytes=row["bytes"],
        dhash=row["dhash"],
        batch_id=row["batch_id"],
        focus=row["focus"] if "focus" in keys else "",
    )


def _batch(row: sqlite3.Row) -> BatchRow:
    return BatchRow(
        id=row["id"],
        day=row["day"],
        start_ts=row["start_ts"],
        end_ts=row["end_ts"],
        status=row["status"],
        attempts=row["attempts"],
        error=row["error"],
        model=row["model"],
        prompt_version=row["prompt_version"],
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
    )


def _activity(row: sqlite3.Row) -> ActivityRow:
    return ActivityRow(
        id=row["id"],
        batch_id=row["batch_id"],
        seq=row["seq"],
        start_ts=row["start_ts"],
        end_ts=row["end_ts"],
        app=row["app"],
        category=row["category"],
        title=row["title"],
        summary=row["summary"],
        confidence=row["confidence"],
        frame_ids=list(json.loads(row["frame_ids"] or "[]")),
    )


class Repo:
    def __init__(self, db: Db) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # frames
    # ------------------------------------------------------------------
    def insert_frame(
        self,
        *,
        ts: float,
        day: str,
        app: str,
        exe: str,
        title: str,
        monitor: str,
        rect: tuple[int, int, int, int] | None,
        file: str | None,
        size: int | None,
        dhash: int | None,
        focus: str = "",
    ) -> int:
        x, y, w, h = rect if rect else (None, None, None, None)
        # dHash is an unsigned 64-bit value; SQLite INTEGER is signed, so the
        # top half of the range must be folded into negatives or the insert
        # raises OverflowError and the frame is lost. Imported lazily because
        # capture.* imports this module at package import time.
        from ..capture.dedupe import to_signed

        stored_hash = to_signed(dhash)
        return self.db.execute(
            """
            INSERT INTO frames (ts, day, app, exe, title, monitor, x, y, w, h,
                                file, bytes, dhash, batch_id, created_at, focus)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)
            """,
            (ts, day, app, exe, title, monitor, x, y, w, h, file, size, stored_hash, ts, focus),
        )

    def last_frame(self) -> FrameRow | None:
        row = self.db.one("SELECT * FROM frames ORDER BY ts DESC LIMIT 1")
        return _frame(row) if row else None

    def frames_between(self, start: float, end: float) -> list[FrameRow]:
        rows = self.db.query(
            "SELECT * FROM frames WHERE ts >= ? AND ts < ? ORDER BY ts", (start, end)
        )
        return [_frame(r) for r in rows]

    def unbatched_frames(self, start: float, end: float) -> list[FrameRow]:
        rows = self.db.query(
            "SELECT * FROM frames WHERE ts >= ? AND ts < ? AND batch_id IS NULL ORDER BY ts",
            (start, end),
        )
        return [_frame(r) for r in rows]

    def frames_for_batch(self, batch_id: int) -> list[FrameRow]:
        rows = self.db.query(
            "SELECT * FROM frames WHERE batch_id = ? ORDER BY ts", (batch_id,)
        )
        return [_frame(r) for r in rows]

    def attach_frames(self, frame_ids: list[int], batch_id: int) -> None:
        if not frame_ids:
            return
        with self.db.tx() as conn:
            conn.executemany(
                "UPDATE frames SET batch_id = ? WHERE id = ? AND batch_id IS NULL",
                [(batch_id, fid) for fid in frame_ids],
            )

    def count_frames(self, day: str | None = None) -> int:
        if day is None:
            return int(self.db.scalar("SELECT COUNT(*) FROM frames", default=0))
        return int(self.db.scalar("SELECT COUNT(*) FROM frames WHERE day = ?", (day,), default=0))

    def delete_frames_before(self, cutoff: float, *, drop_files: bool = True) -> list[str]:
        """Delete frame rows older than ``cutoff``; return their relative paths."""
        rows = self.db.query(
            "SELECT file FROM frames WHERE ts < ? AND file IS NOT NULL", (cutoff,)
        )
        paths = [r["file"] for r in rows]
        self.db.execute("DELETE FROM frames WHERE ts < ?", (cutoff,))
        return paths if drop_files else []

    # ------------------------------------------------------------------
    # batches
    # ------------------------------------------------------------------
    def ensure_batches(
        self,
        *,
        day: str,
        batch_seconds: float,
        grace_seconds: float,
        now: float,
        boundary_hour: int,
    ) -> int:
        """Create/park batches for every *closed* window of ``day`` that has frames.

        Windows align to the logical day start, so a batch is always
        ``[day_start + k*step, day_start + (k+1)*step)``. Frames that arrive
        after a batch finished are intentionally left unanalyzed instead of
        silently re-billing the model; they show up as "未分析" in reports.

        The loop walks *frames*, not windows: with a 1-minute batch size a day
        has 1440 windows and a window walk over an empty day used to cost two
        queries per window. Grouping unbatched frames into their windows is one
        query and, unlike the window walk, cannot spin forever on the final
        window (which is clipped to ``end`` and therefore never advances).
        """
        start, end = clock.day_bounds(day, boundary_hour)
        if batch_seconds <= 0:
            return 0
        cutoff = min(now - grace_seconds, end)
        if cutoff <= start:
            return 0

        rows = self.db.query(
            """
            SELECT id, ts FROM frames
            WHERE ts >= ? AND ts < ? AND batch_id IS NULL
            ORDER BY ts
            """,
            (start, cutoff),
        )
        if not rows:
            return 0

        buckets: dict[float, list[int]] = {}
        for row in rows:
            index = int((row["ts"] - start) // batch_seconds)
            w0 = start + index * batch_seconds
            w1 = min(w0 + batch_seconds, end)
            # A window must be closed (and the grace period elapsed) before it
            # is batched; otherwise late frames land in an already-analyzed batch.
            if w1 > cutoff:
                continue
            buckets.setdefault(w0, []).append(int(row["id"]))

        created = 0
        for w0 in sorted(buckets):
            w1 = min(w0 + batch_seconds, end)
            fresh = buckets[w0]
            existing = self.db.one(
                "SELECT id, status FROM batches WHERE day = ? AND start_ts = ?", (day, w0)
            )
            if existing:
                if existing["status"] in (STATUS_PENDING, STATUS_FAILED) and fresh:
                    self.attach_frames(fresh, int(existing["id"]))
                continue
            with self.db.tx() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO batches (day, start_ts, end_ts, status, attempts,
                                         created_at, updated_at)
                    VALUES (?,?,?,?,0,?,?)
                    """,
                    (day, w0, w1, STATUS_PENDING, now, now),
                )
                batch_id = int(cur.lastrowid or 0)
                conn.executemany(
                    "UPDATE frames SET batch_id = ? WHERE id = ?",
                    [(batch_id, fid) for fid in fresh],
                )
            created += 1
        return created

    def next_pending_batch(self, max_attempts: int) -> BatchRow | None:
        row = self.db.one(
            """
            SELECT * FROM batches
            WHERE status IN (?, ?) AND attempts < ?
            ORDER BY start_ts ASC LIMIT 1
            """,
            (STATUS_PENDING, STATUS_FAILED, max_attempts),
        )
        return _batch(row) if row else None

    def batch_counts(self, day: str | None = None) -> dict[str, int]:
        sql = "SELECT status, COUNT(*) AS n FROM batches"
        params: tuple[Any, ...] = ()
        if day:
            sql += " WHERE day = ?"
            params = (day,)
        sql += " GROUP BY status"
        return {r["status"]: int(r["n"]) for r in self.db.query(sql, params)}

    def claim_batch(self, batch_id: int) -> bool:
        """Atomically move pending/failed -> running. False if someone else won."""
        with self.db.tx() as conn:
            cur = conn.execute(
                """
                UPDATE batches SET status=?, attempts=attempts+1, updated_at=?
                WHERE id=? AND status IN (?, ?)
                """,
                (STATUS_RUNNING, clock.now_ts(), batch_id, STATUS_PENDING, STATUS_FAILED),
            )
            return cur.rowcount == 1

    def mark_done(
        self,
        batch_id: int,
        *,
        model: str,
        prompt_version: str,
        tokens_in: int,
        tokens_out: int,
        images: int = 0,
    ) -> None:
        self.db.execute(
            """
            UPDATE batches SET status=?, error=NULL, model=?, prompt_version=?,
                   tokens_in=?, tokens_out=?, images=?, updated_at=?
            WHERE id=?
            """,
            (
                STATUS_DONE,
                model,
                prompt_version,
                tokens_in,
                tokens_out,
                max(0, int(images)),
                clock.now_ts(),
                batch_id,
            ),
        )

    def release_batch(self, batch_id: int, reason: str = "") -> None:
        """Put a claimed batch back in the queue without spending an attempt.

        Used for problems the user has to fix (no balance, bad key): retrying
        three times and marking the batch failed would need a manual reset
        after topping up.
        """
        self.db.execute(
            "UPDATE batches SET status=?, attempts=MAX(0, attempts - 1), error=?, updated_at=? "
            "WHERE id=?",
            (STATUS_PENDING, reason[:500], clock.now_ts(), batch_id),
        )

    def images_used_on(self, day: str) -> int:
        """Images actually sent to a model for ``day`` (the cost driver)."""
        value = self.db.scalar(
            "SELECT COALESCE(SUM(images), 0) FROM batches WHERE day = ? AND status = ?",
            (day, STATUS_DONE),
        )
        return int(value or 0)

    def mark_failed(self, batch_id: int, error: str) -> None:
        self.db.execute(
            "UPDATE batches SET status=?, error=?, updated_at=? WHERE id=?",
            (STATUS_FAILED, error[:2000], clock.now_ts(), batch_id),
        )

    def reclaim_stale_running(self, older_than_seconds: float = 1800.0) -> int:
        cutoff = clock.now_ts() - older_than_seconds
        with self.db.tx() as conn:
            cur = conn.execute(
                "UPDATE batches SET status=?, updated_at=? WHERE status=? AND updated_at < ?",
                (STATUS_PENDING, clock.now_ts(), STATUS_RUNNING, cutoff),
            )
            return cur.rowcount

    # ------------------------------------------------------------------
    # activities
    # ------------------------------------------------------------------
    def replace_activities(self, batch_id: int, activities: list[NewActivity]) -> int:
        now = clock.now_ts()
        with self.db.tx() as conn:
            conn.execute("DELETE FROM activities WHERE batch_id = ?", (batch_id,))
            conn.executemany(
                """
                INSERT INTO activities (batch_id, seq, start_ts, end_ts, app, category,
                                        title, summary, confidence, frame_ids, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        batch_id,
                        seq,
                        a.start_ts,
                        a.end_ts,
                        a.app,
                        a.category,
                        a.title,
                        a.summary,
                        a.confidence,
                        json.dumps(a.frame_ids),
                        now,
                    )
                    for seq, a in enumerate(activities)
                ],
            )
        return len(activities)

    def activities_between(self, start: float, end: float) -> list[ActivityRow]:
        rows = self.db.query(
            """
            SELECT * FROM activities
            WHERE end_ts > ? AND start_ts < ?
            ORDER BY start_ts, id
            """,
            (start, end),
        )
        return [_activity(r) for r in rows]

    # ------------------------------------------------------------------
    # sessions (derived, rebuildable)
    # ------------------------------------------------------------------
    def replace_sessions(self, day: str, sessions: list[SessionRow]) -> int:
        now = clock.now_ts()
        with self.db.tx() as conn:
            conn.execute("DELETE FROM sessions WHERE day = ?", (day,))
            conn.executemany(
                """
                INSERT INTO sessions (day, start_ts, end_ts, app, category, title,
                                      activity_ids, built_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        s.day,
                        s.start_ts,
                        s.end_ts,
                        s.app,
                        s.category,
                        s.title,
                        json.dumps(s.activity_ids),
                        now,
                    )
                    for s in sessions
                ],
            )
        return len(sessions)

    def sessions_for_day(self, day: str) -> list[SessionRow]:
        rows = self.db.query(
            "SELECT * FROM sessions WHERE day = ? ORDER BY start_ts", (day,)
        )
        return [
            SessionRow(
                day=r["day"],
                start_ts=r["start_ts"],
                end_ts=r["end_ts"],
                app=r["app"],
                category=r["category"],
                title=r["title"],
                activity_ids=list(json.loads(r["activity_ids"] or "[]")),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # gaps
    # ------------------------------------------------------------------
    def set_presence(self, kind: str | None, ts: float) -> None:
        """Declare what the user is doing right now.

        ``kind`` is ``None`` (present at the screen), ``idle``, ``locked`` or
        ``protected`` (blacklisted window). Closes gaps of other kinds.
        """
        with self.db.tx() as conn:
            if kind is None:
                conn.execute("UPDATE gaps SET end_ts = ? WHERE end_ts IS NULL", (ts,))
            else:
                conn.execute(
                    "UPDATE gaps SET end_ts = ? WHERE end_ts IS NULL AND kind <> ?", (ts, kind)
                )
                conn.execute(
                    "INSERT OR IGNORE INTO gaps (kind, start_ts, end_ts) VALUES (?,?,NULL)",
                    (kind, ts),
                )
            conn.execute("DELETE FROM gaps WHERE end_ts IS NOT NULL AND end_ts <= start_ts")

    def gaps_between(self, start: float, end: float) -> list[GapRow]:
        rows = self.db.query(
            """
            SELECT kind, start_ts, end_ts FROM gaps
            WHERE (end_ts IS NULL OR end_ts > ?) AND start_ts < ?
            ORDER BY start_ts
            """,
            (start, end),
        )
        return [GapRow(kind=r["kind"], start_ts=r["start_ts"], end_ts=r["end_ts"]) for r in rows]

    # ------------------------------------------------------------------
    # reports
    # ------------------------------------------------------------------
    def upsert_report(
        self,
        *,
        day: str,
        tracked_seconds: float,
        categories: dict[str, float],
        apps: dict[str, float],
        narrative: str,
        highlights: list[str],
        suggestions: list[str],
        model: str | None,
        prompt_version: str | None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO daily_reports (day, tracked_seconds, categories, apps,
                                       narrative, highlights, suggestions,
                                       model, prompt_version, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(day) DO UPDATE SET
                tracked_seconds=excluded.tracked_seconds,
                categories=excluded.categories,
                apps=excluded.apps,
                narrative=excluded.narrative,
                highlights=excluded.highlights,
                suggestions=excluded.suggestions,
                model=excluded.model,
                prompt_version=excluded.prompt_version,
                created_at=excluded.created_at
            """,
            (
                day,
                tracked_seconds,
                json.dumps(categories, ensure_ascii=False),
                json.dumps(apps, ensure_ascii=False),
                narrative,
                json.dumps(highlights, ensure_ascii=False),
                json.dumps(suggestions, ensure_ascii=False),
                model,
                prompt_version,
                clock.now_ts(),
            ),
        )

    def get_report(self, day: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM daily_reports WHERE day = ?", (day,))
        return _report_row(row) if row else None

    def list_reports(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM daily_reports ORDER BY day DESC LIMIT ?", (limit,)
        )
        return [_report_row(row) for row in rows]

    def report_days(self) -> set[str]:
        rows = self.db.query("SELECT day FROM daily_reports")
        return {str(row["day"]) for row in rows}

    # ------------------------------------------------------------------
    # weekly reports
    # ------------------------------------------------------------------
    def upsert_week(
        self,
        *,
        start_day: str,
        end_day: str,
        tracked_seconds: float,
        categories: dict[str, float],
        apps: dict[str, float],
        daily: dict[str, float],
        narrative: str,
        highlights: list[str],
        suggestions: list[str],
        model: str | None,
        prompt_version: str | None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO weekly_reports (start_day, end_day, tracked_seconds, categories,
                                        apps, daily, narrative, highlights, suggestions,
                                        model, prompt_version, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(start_day) DO UPDATE SET
                end_day=excluded.end_day,
                tracked_seconds=excluded.tracked_seconds,
                categories=excluded.categories,
                apps=excluded.apps,
                daily=excluded.daily,
                narrative=excluded.narrative,
                highlights=excluded.highlights,
                suggestions=excluded.suggestions,
                model=excluded.model,
                prompt_version=excluded.prompt_version,
                created_at=excluded.created_at
            """,
            (
                start_day,
                end_day,
                tracked_seconds,
                json.dumps(categories, ensure_ascii=False),
                json.dumps(apps, ensure_ascii=False),
                json.dumps(daily, ensure_ascii=False),
                narrative,
                json.dumps(highlights, ensure_ascii=False),
                json.dumps(suggestions, ensure_ascii=False),
                model,
                prompt_version,
                clock.now_ts(),
            ),
        )

    def get_week(self, start_day: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM weekly_reports WHERE start_day = ?", (start_day,))
        if not row:
            return None
        return {
            "start_day": row["start_day"],
            "end_day": row["end_day"],
            "tracked_seconds": row["tracked_seconds"],
            "categories": json.loads(row["categories"] or "{}"),
            "apps": json.loads(row["apps"] or "{}"),
            "daily": json.loads(row["daily"] or "{}"),
            "narrative": row["narrative"],
            "highlights": json.loads(row["highlights"] or "[]"),
            "suggestions": json.loads(row["suggestions"] or "[]"),
            "model": row["model"],
            "prompt_version": row["prompt_version"],
            "created_at": row["created_at"],
        }

    # ------------------------------------------------------------------
    # settings (runtime overrides, e.g. cached provider capabilities)
    # ------------------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.db.one("SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self.db.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, json.dumps(value, ensure_ascii=False), clock.now_ts()),
        )
