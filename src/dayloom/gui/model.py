"""Read-only data for the GUI.

No Qt here on purpose: this module is plain Python so it can be unit tested on a
headless CI box without a display plugin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import clock
from ..analyzer.report import compute_report
from ..config import Config
from ..store.db import open_db
from ..store.repo import Repo


@dataclass(slots=True)
class SessionView:
    start_ts: float
    end_ts: float
    app: str
    category: str
    title: str
    summary: str
    thumb: Path | None
    activity_ids: list[int] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end_ts - self.start_ts)

    @property
    def range_label(self) -> str:
        return f"{clock.fmt_hm(self.start_ts)} – {clock.fmt_hm(self.end_ts)}"


@dataclass(slots=True)
class DayView:
    day: str
    has_db: bool = True
    tracked_seconds: float = 0.0
    categories: dict[str, float] = field(default_factory=dict)
    apps: dict[str, float] = field(default_factory=dict)
    sessions: list[SessionView] = field(default_factory=list)
    narrative: str = ""
    highlights: list[str] = field(default_factory=list)
    gaps: dict[str, float] = field(default_factory=dict)
    unanalyzed: int = 0
    frame_count: int = 0

    @property
    def signature(self) -> tuple:
        last = int(self.sessions[-1].end_ts) if self.sessions else 0
        return (len(self.sessions), last, self.unanalyzed, self.narrative[:32], self.frame_count)


def _thumb_for(cfg: Config, frames: list, start_ts: float, end_ts: float) -> Path | None:
    for frame in frames:
        if start_ts <= frame["ts"] < max(end_ts, start_ts + 1):
            if frame["file"]:
                path = cfg.home / frame["file"]
                if path.exists():
                    return path
            return None
    return None


def load_day(cfg: Config, day: str) -> DayView:
    """Everything the day view needs, in one read-only pass."""
    if not cfg.db_path.exists():
        return DayView(day=day, has_db=False)
    db = open_db(cfg.db_path, readonly=True)
    try:
        repo = Repo(db)
        report = compute_report(repo, cfg, day)
        start, end = clock.day_bounds(day, cfg.general.day_boundary_hour)
        frames = db.query(
            "SELECT ts, file FROM frames WHERE ts >= ? AND ts < ? AND file IS NOT NULL ORDER BY ts",
            (start, end),
        )
        frame_count = int(
            db.scalar("SELECT COUNT(*) FROM frames WHERE day = ?", (day,), default=0) or 0
        )
        summaries: dict[int, str] = {}
        for row in db.query(
            "SELECT id, summary FROM activities WHERE end_ts > ? AND start_ts < ?", (start, end)
        ):
            summaries[int(row["id"])] = row["summary"] or ""

        sessions: list[SessionView] = []
        for session in report.sessions:
            summary = ""
            for activity_id in session.activity_ids:
                text = summaries.get(activity_id, "")
                if len(text) > len(summary):
                    summary = text
            sessions.append(
                SessionView(
                    start_ts=session.start_ts,
                    end_ts=session.end_ts,
                    app=session.app,
                    category=session.category,
                    title=session.title,
                    summary=summary,
                    thumb=_thumb_for(cfg, frames, session.start_ts, session.end_ts),
                    activity_ids=list(session.activity_ids),
                )
            )

        gaps: dict[str, float] = {}
        now = clock.now_ts()
        for gap in report.gaps:
            gaps[gap.kind] = gaps.get(gap.kind, 0.0) + gap.duration(now)

        return DayView(
            day=day,
            tracked_seconds=report.tracked_seconds,
            categories=dict(report.categories),
            apps=dict(report.apps),
            sessions=sessions,
            narrative=report.narrative,
            highlights=list(report.highlights),
            gaps=gaps,
            unanalyzed=report.unanalyzed_frames,
            frame_count=frame_count,
        )
    finally:
        db.close()


def load_status(cfg: Config) -> dict:
    """Daemon/DB status, reusing the CLI's snapshot so both agree."""
    from ..scheduler import status_snapshot

    return status_snapshot(cfg)


def load_reports(cfg: Config, days: int = 30) -> list[dict]:
    """Stored daily rollups, newest first (read-only)."""
    if not cfg.db_path.exists():
        return []
    db = open_db(cfg.db_path, readonly=True)
    try:
        return Repo(db).list_reports(days)
    finally:
        db.close()


def load_week(cfg: Config, any_day: str):
    """Read-only week rollup for the GUI (no narration, no writes)."""
    from ..analyzer.weekly import compute_week

    if not cfg.db_path.exists():
        return None
    db = open_db(cfg.db_path, readonly=True)
    try:
        return compute_week(Repo(db), cfg, any_day)
    finally:
        db.close()
