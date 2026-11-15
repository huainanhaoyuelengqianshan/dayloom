"""Merge adjacent activity segments into sessions.

``activities`` is the raw, immutable model output (5–15 minute granularity).
``sessions`` is a derived view built by this module and can be rebuilt at any
time — which is the point: change the model or the prompt and you can recompute
the display layer without having lost the original.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..store.repo import ActivityRow

#: Categories that are never merged across, even if adjacent: "unknown" blocks
#: are holes in the timeline, and smoothing them over would be lying.
NEVER_MERGE = {"unknown"}


@dataclass(slots=True)
class MergedSession:
    day: str
    start_ts: float
    end_ts: float
    app: str
    category: str
    title: str
    activity_ids: list[int] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end_ts - self.start_ts)


def merge_activities(
    activities: list[ActivityRow], *, gap_seconds: int = 120
) -> list[MergedSession]:
    """Concatenate adjacent same-app/same-category segments separated by <= gap."""
    if not activities:
        return []
    ordered = sorted(activities, key=lambda a: (a.start_ts, a.id))
    sessions: list[MergedSession] = []
    for act in ordered:
        cat = act.category or "other"
        if sessions:
            last = sessions[-1]
            same_bucket = last.app == act.app and last.category == cat
            close_enough = act.start_ts - last.end_ts <= gap_seconds
            mergeable = cat not in NEVER_MERGE and last.category not in NEVER_MERGE
            if same_bucket and close_enough and mergeable:
                last.end_ts = max(last.end_ts, act.end_ts)
                last.activity_ids.append(act.id)
                if len(act.title or "") > len(last.title or ""):
                    last.title = act.title
                continue
        sessions.append(
            MergedSession(
                day="",
                start_ts=act.start_ts,
                end_ts=act.end_ts,
                app=act.app,
                category=cat,
                title=act.title or act.summary[:80],
                activity_ids=[act.id],
            )
        )
    return sessions


def build_sessions(
    activities: list[ActivityRow], *, day: str, gap_seconds: int = 120
) -> list[MergedSession]:
    sessions = merge_activities(activities, gap_seconds=gap_seconds)
    for session in sessions:
        session.day = day
    return sessions
