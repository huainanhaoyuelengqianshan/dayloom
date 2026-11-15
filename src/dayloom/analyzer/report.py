"""Daily rollup.

Division of labour, stated once: SQL counts, the model narrates. Every number in
a day report (per-category time, per-app time, tracked total) is computed here
from segment boundaries and clipped to the logical day; the model only writes
prose from those numbers. That is the difference between a diary that can be
trusted and a plausible-sounding fiction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .. import clock
from ..config import Config
from ..llm.prompts import (
    CATEGORY_ZH,
    ROLLUP_PROMPT_VERSION,
    ROLLUP_SYSTEM,
    build_rollup_user,
)
from ..llm.provider import Part, VisionClient
from ..store.repo import GapRow, Repo, SessionRow
from .merge import MergedSession, build_sessions

log = logging.getLogger(__name__)

Interval = tuple[float, float]
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


@dataclass
class DayReport:
    day: str
    tracked_seconds: float = 0.0
    categories: dict[str, float] = field(default_factory=dict)
    apps: dict[str, float] = field(default_factory=dict)
    sessions: list[MergedSession] = field(default_factory=list)
    gaps: list[GapRow] = field(default_factory=list)
    unanalyzed_frames: int = 0
    narrative: str = ""
    highlights: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    model: str = ""


# ----------------------------------------------------------------------
# interval algebra
# ----------------------------------------------------------------------
def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    out: list[Interval] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = out[-1]
        if start <= last_end:
            out[-1] = (last_start, max(last_end, end))
        else:
            out.append((start, end))
    return out


def subtract_intervals(intervals: list[Interval], holes: list[Interval]) -> list[Interval]:
    if not intervals or not holes:
        return merge_intervals(intervals)
    holes = merge_intervals(holes)
    result: list[Interval] = []
    for start, end in merge_intervals(intervals):
        cursor = start
        for h_start, h_end in holes:
            if h_end <= cursor:
                continue
            if h_start >= end:
                break
            if h_start > cursor:
                result.append((cursor, min(h_start, end)))
            cursor = max(cursor, h_end)
            if cursor >= end:
                break
        if cursor < end:
            result.append((cursor, end))
    return result


def total_seconds(intervals: list[Interval]) -> float:
    return sum(max(0.0, end - start) for start, end in merge_intervals(intervals))


# ----------------------------------------------------------------------
# report
# ----------------------------------------------------------------------
def compute_report(repo: Repo, cfg: Config, day: str) -> DayReport:
    """Pure read-only aggregation: no writes, safe for the GUI to call often."""
    start, end = clock.day_bounds(day, cfg.general.day_boundary_hour)
    activities = repo.activities_between(start, end)
    sessions = build_sessions(
        activities, day=day, gap_seconds=cfg.analysis.session_gap_seconds
    )

    gaps = repo.gaps_between(start, end)
    hole_intervals: list[Interval] = []
    for gap in gaps:
        g_start = max(start, gap.start_ts)
        g_end = min(end, gap.end_ts if gap.end_ts is not None else clock.now_ts())
        if g_end > g_start:
            hole_intervals.append((g_start, g_end))

    categories: dict[str, float] = {}
    apps: dict[str, float] = {}
    all_intervals: list[Interval] = []
    for session in sessions:
        clipped = (max(start, session.start_ts), min(end, session.end_ts))
        if clipped[1] <= clipped[0]:
            continue
        effective = subtract_intervals([clipped], hole_intervals)
        seconds = total_seconds(effective)
        if seconds <= 0:
            continue
        categories[session.category] = categories.get(session.category, 0.0) + seconds
        apps[session.app or "unknown"] = apps.get(session.app or "unknown", 0.0) + seconds
        all_intervals.extend(effective)

    report = DayReport(
        day=day,
        tracked_seconds=total_seconds(all_intervals),
        categories=categories,
        apps=apps,
        sessions=sessions,
        gaps=gaps,
        unanalyzed_frames=int(
            repo.db.scalar(
                """
                SELECT COUNT(*) FROM frames f
                LEFT JOIN batches b ON f.batch_id = b.id
                WHERE f.day = ? AND (f.batch_id IS NULL OR b.status <> 'done')
                """,
                (day,),
                default=0,
            )
        ),
    )
    # The narrative is not recomputed here (that needs a model call); reuse the
    # stored one so a pure read still shows yesterday's prose.
    stored = repo.get_report(day) or {}
    report.narrative = str(stored.get("narrative") or "")
    report.highlights = list(stored.get("highlights") or [])
    report.suggestions = list(stored.get("suggestions") or [])
    report.model = str(stored.get("model") or "")
    return report


def persist_report(repo: Repo, report: DayReport) -> None:
    """Write the derived session layer and the report row."""
    repo.replace_sessions(
        report.day,
        [
            SessionRow(
                day=s.day,
                start_ts=s.start_ts,
                end_ts=s.end_ts,
                app=s.app,
                category=s.category,
                title=s.title,
                activity_ids=s.activity_ids,
            )
            for s in report.sessions
        ],
    )
    repo.upsert_report(
        day=report.day,
        tracked_seconds=report.tracked_seconds,
        categories=report.categories,
        apps=report.apps,
        narrative=report.narrative,
        highlights=report.highlights,
        suggestions=report.suggestions,
        model=report.model or None,
        prompt_version=ROLLUP_PROMPT_VERSION,
    )


def build_report(
    repo: Repo,
    cfg: Config,
    day: str,
    *,
    narrate: bool = False,
    client: VisionClient | None = None,
) -> DayReport:
    """Compute, optionally narrate, then persist one day."""
    report = compute_report(repo, cfg, day)
    start, end = clock.day_bounds(day, cfg.general.day_boundary_hour)
    activities = repo.activities_between(start, end)

    if narrate and client is not None and activities:
        _narrate(report, cfg, client, activities, start, end)

    if not report.narrative:
        # Don't wipe a good narrative just because this run was numbers-only.
        existing = repo.get_report(day)
        if existing and existing.get("narrative"):
            report.narrative = str(existing["narrative"])
            report.highlights = list(existing.get("highlights") or [])
            report.suggestions = list(existing.get("suggestions") or [])
            report.model = str(existing.get("model") or "")

    persist_report(repo, report)
    return report


def _narrate(
    report: DayReport,
    cfg: Config,
    client: VisionClient,
    activities: list,
    start: float,
    end: float,
) -> None:
    top = sorted(activities, key=lambda a: a.end_ts - a.start_ts, reverse=True)[:40]
    rows = [
        (a.start_ts, a.summary or a.title or a.category, a.end_ts - a.start_ts, a.category)
        for a in sorted(top, key=lambda a: a.start_ts)
    ]
    text = build_rollup_user(
        day=report.day,
        tracked_seconds=report.tracked_seconds,
        categories=report.categories,
        apps=report.apps,
        activities=rows,
    )
    try:
        payload, _usage = client.complete_json([Part.of_text(text)], system=ROLLUP_SYSTEM)
    except Exception as exc:
        # Narration is optional; the numbers must survive a model outage.
        log.warning("日报叙述生成失败，只保留统计数字：%s", exc)
        report.model = ""
        return
    report.narrative = str(payload.get("narrative") or "").strip()
    report.highlights = [str(x) for x in payload.get("highlights") or []][:5]
    report.suggestions = [str(x) for x in payload.get("suggestions") or []][:5]
    report.model = cfg.llm.model


# ----------------------------------------------------------------------
# terminal rendering
# ----------------------------------------------------------------------
def _hm(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def render(report: DayReport, *, width: int = 74) -> str:
    d = clock.parse_day(report.day)
    header = (
        f"{report.day} {WEEKDAYS[d.weekday()]} · 记录 {_hm(report.tracked_seconds)}"
        f" · {len(report.sessions)} 段"
    )
    lines = [header, "─" * width]
    if not report.sessions:
        lines.append("这一天还没有分析结果。")
    for session in report.sessions:
        dur = session.duration
        label = CATEGORY_ZH.get(session.category, session.category)
        title = (session.title or "").strip()
        if len(title) > 40:
            title = title[:39] + "…"
        lines.append(
            f"{clock.fmt_hm(session.start_ts)}  {_hm(dur):>6}  {label:<5} "
            f"{session.app[:14]:<14}  {title}"
        )
    lines.append("─" * width)
    if report.categories:
        total = sum(report.categories.values()) or 1.0
        parts = [
            f"{CATEGORY_ZH.get(cat, cat)} {_hm(sec)} ({sec / total * 100:.0f}%)"
            for cat, sec in sorted(report.categories.items(), key=lambda x: -x[1])
            if sec >= 60
        ]
        lines.append(" · ".join(parts))
    if report.gaps:
        gap_totals: dict[str, float] = {}
        now = clock.now_ts()
        for gap in report.gaps:
            gap_totals[gap.kind] = gap_totals.get(gap.kind, 0.0) + gap.duration(now)
        kinds = {"idle": "空闲", "locked": "锁屏", "protected": "已跳过(黑名单)"}
        lines.append(
            "未记录：" + " · ".join(f"{kinds.get(k, k)} {_hm(v)}" for k, v in gap_totals.items())
        )
    if report.unanalyzed_frames:
        lines.append(
            f"⚠ 还有 {report.unanalyzed_frames} 帧未分析"
            "（可能还在当前批次窗口里，或批次失败）"
        )
    if report.narrative:
        lines.append("")
        lines.append(report.narrative)
        if report.highlights:
            lines.append("")
            lines.extend(f"· {h}" for h in report.highlights)
    elif report.sessions:
        lines.append("")
        lines.append("（没有生成叙述：未配置视觉模型，或叙述调用失败）")
    return "\n".join(lines)
