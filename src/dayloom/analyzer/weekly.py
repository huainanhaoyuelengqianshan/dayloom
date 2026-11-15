"""Weekly rollup: seven daily reports, folded into one.

Same rule as the daily report — SQL counts, the model narrates. The difference
is that a week is too much detail for a prompt, so each day collapses to a
number and its biggest category, plus the week's aggregate distribution.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from .. import clock
from ..config import Config
from ..llm.prompts import (
    CATEGORY_ZH,
    WEEK_PROMPT_VERSION,
    WEEK_SYSTEM,
    build_week_user,
)
from ..llm.provider import Part, VisionClient
from ..store.repo import Repo
from .report import compute_report

log = logging.getLogger(__name__)


@dataclass(slots=True)
class DaySummary:
    day: str
    tracked_seconds: float
    top_category: str


@dataclass
class WeekReport:
    start_day: str
    end_day: str
    tracked_seconds: float = 0.0
    categories: dict[str, float] = field(default_factory=dict)
    apps: dict[str, float] = field(default_factory=dict)
    daily: list[DaySummary] = field(default_factory=list)
    narrative: str = ""
    highlights: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    model: str = ""

    @property
    def label(self) -> str:
        return f"{self.start_day} – {self.end_day}"

    @property
    def best_day(self) -> DaySummary | None:
        return max(self.daily, key=lambda d: d.tracked_seconds, default=None)


def week_bounds(day: str) -> tuple[str, str]:
    """Monday..Sunday containing ``day``."""
    d = clock.parse_day(day)
    start = d - timedelta(days=d.weekday())
    return start.isoformat(), (start + timedelta(days=6)).isoformat()


def _days(start: str) -> list[str]:
    return [clock.shift_day(start, i) for i in range(7)]


def compute_week(repo: Repo, cfg: Config, any_day: str) -> WeekReport:
    """Read-only: fold the stored daily numbers into a week."""
    start, end = week_bounds(any_day)
    report = WeekReport(start_day=start, end_day=end)
    for day in _days(start):
        daily = compute_report(repo, cfg, day)
        report.tracked_seconds += daily.tracked_seconds
        for category, seconds in daily.categories.items():
            report.categories[category] = report.categories.get(category, 0.0) + seconds
        for app, seconds in daily.apps.items():
            report.apps[app] = report.apps.get(app, 0.0) + seconds
        top = max(daily.categories.items(), key=lambda kv: kv[1], default=None)
        report.daily.append(
            DaySummary(
                day=day,
                tracked_seconds=daily.tracked_seconds,
                top_category=CATEGORY_ZH.get(top[0], top[0]) if top else "",
            )
        )
    stored = repo.get_week(start) or {}
    report.narrative = str(stored.get("narrative") or "")
    report.highlights = list(stored.get("highlights") or [])
    report.suggestions = list(stored.get("suggestions") or [])
    report.model = str(stored.get("model") or "")
    return report


def build_week(
    repo: Repo,
    cfg: Config,
    any_day: str,
    *,
    narrate: bool = False,
    client: VisionClient | None = None,
) -> WeekReport:
    report = compute_week(repo, cfg, any_day)
    if narrate and client is not None and report.tracked_seconds > 0:
        _narrate(report, cfg, client)
    repo.upsert_week(
        start_day=report.start_day,
        end_day=report.end_day,
        tracked_seconds=report.tracked_seconds,
        categories=report.categories,
        apps=report.apps,
        daily={d.day: d.tracked_seconds for d in report.daily},
        narrative=report.narrative,
        highlights=report.highlights,
        suggestions=report.suggestions,
        model=report.model or None,
        prompt_version=WEEK_PROMPT_VERSION,
    )
    return report


def _narrate(report: WeekReport, cfg: Config, client: VisionClient) -> None:
    text = build_week_user(
        start_day=report.start_day,
        end_day=report.end_day,
        tracked_seconds=report.tracked_seconds,
        categories=report.categories,
        apps=report.apps,
        daily=[(d.day, d.tracked_seconds, d.top_category) for d in report.daily],
    )
    try:
        payload, _usage = client.complete_json([Part.of_text(text)], system=WEEK_SYSTEM)
    except Exception as exc:
        log.warning("周报叙述生成失败，只保留统计数字：%s", exc)
        return
    report.narrative = str(payload.get("narrative") or "").strip()
    report.highlights = [str(x) for x in payload.get("highlights") or []][:6]
    report.suggestions = [str(x) for x in payload.get("suggestions") or []][:6]
    report.model = cfg.llm.model


def render_week(report: WeekReport, *, width: int = 62) -> str:
    if report.tracked_seconds <= 0:
        return f"{report.label}：这一周还没有数据。"

    peak = max((d.tracked_seconds for d in report.daily), default=1.0) or 1.0
    lines = [
        f"{report.label} · 记录 {clock.fmt_duration_short(report.tracked_seconds)}",
        "─" * width,
    ]
    for summary in report.daily:
        bar_len = int(round(summary.tracked_seconds / peak * 24))
        bar = "█" * max(0, bar_len)
        label = clock.weekday_label(summary.day)
        lines.append(
            f"{label}  {clock.fmt_duration_short(summary.tracked_seconds):>6}  {bar:<24}  "
            f"{summary.top_category}"
        )
    lines.append("─" * width)
    total = report.tracked_seconds or 1.0
    lines.append(
        " · ".join(
            f"{CATEGORY_ZH.get(k, k)} {clock.fmt_duration_short(v)} {v / total * 100:.0f}%"
            for k, v in sorted(report.categories.items(), key=lambda kv: -kv[1])[:6]
            if v > 0
        )
    )
    if report.narrative:
        lines.extend(["", report.narrative])
        if report.highlights:
            lines.append("")
            lines.extend(f"· {h}" for h in report.highlights)
    else:
        lines.extend(["", "（没有周报叙述：运行 dayloom week，且需要配置视觉模型）"])
    return "\n".join(lines)


def daily_json(report: WeekReport) -> str:
    return json.dumps({d.day: d.tracked_seconds for d in report.daily}, ensure_ascii=False)
