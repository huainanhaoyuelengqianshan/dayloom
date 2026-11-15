"""Prompts and the shared activity taxonomy.

Two rules encoded here:

1. The model never returns wall-clock times. It returns *frame indices*.
   Models are bad at clocks and good at "this frame to that frame"; mapping
   indices to timestamps is deterministic Python.
2. The model never returns durations either. Durations are computed from the
   segment boundaries, so "每件事花了多久" cannot be hallucinated.
"""

from __future__ import annotations

from .. import clock

SEGMENT_PROMPT_VERSION = "segment-v3"
ROLLUP_PROMPT_VERSION = "rollup-v1"
WEEK_PROMPT_VERSION = "week-v1"

#: Fixed taxonomy. Order matters only for display colour assignment.
CATEGORIES: list[str] = [
    "coding",
    "writing",
    "reading",
    "browsing",
    "communication",
    "meeting",
    "design",
    "media",
    "gaming",
    "admin",
    "learning",
    "other",
    "unknown",
]

CATEGORY_ZH: dict[str, str] = {
    "coding": "写代码",
    "writing": "写作",
    "reading": "阅读",
    "browsing": "浏览",
    "communication": "沟通",
    "meeting": "会议",
    "design": "设计",
    "media": "影音",
    "gaming": "游戏",
    "admin": "事务",
    "learning": "学习",
    "other": "其他",
    "unknown": "无法辨认",
}

SEGMENT_SYSTEM = """\
你是屏幕活动分析器。你会收到同一个时间段内、按时间顺序排列的若干张屏幕截图。
每帧都有一行元数据：帧号 | 时间 | 屏 | 应用 | 窗口标题；只有一部分帧附了截图。

这批帧可能来自两块（或多块）显示器：同一批里会出现不同的「屏」。同一时刻只有一帧代表当时的
前台窗口——它就是采集时 Windows 报告的你正在交互的那个窗口。

你的任务：把这段时间切分成若干个「活动段」，说明每个段里用户在做什么事。

规则：
1. 用帧号描述范围（from_frame / to_frame，含两端），绝对不要输出任何时间或时长。
2. 每一帧都必须被某个活动段覆盖，区间按顺序、不重叠、不留空。
3. category 只能取以下值之一：coding, writing, reading, browsing, communication,
   meeting, design, media, gaming, admin, learning, other, unknown
4. summary 写一句中文，说明「在做什么事」，不要只复述应用名或窗口标题。
   反例：「在 VS Code 中」。正例：「改截图去重阈值，反复跑单测」。
5. 画面没有变化的相邻帧合并成一个段，不要切碎。
6. 只在画面里确实能看到时才下结论；看不清就把 category 设为 unknown，
   summary 写「画面无法辨认」，并降低 confidence。不要编造画面里没有的信息。
7. app 填你在元数据里看到的那个应用名，不要翻译、不要改写。
8. category 是 media（看视频、直播、听音乐）时，只说明「在哪个平台看什么内容」，
   一句话，不超过 30 字。**不要**描述画面细节、人物表情、镜头切换，
   **不要**转录弹幕、评论、字幕、聊天内容——这些对时间日志没有意义。
   正例：「在看B站直播：英雄联盟」。反例：「画面从选手憋笑到最终笑场，弹幕密集」。
9. 多屏时：判断某一帧是什么活动，以该帧的「应用」+ 该帧画面为准。另一块屏上的窗口、
   或同一块屏里没有被交互的窗口（例如背景里在放视频、游戏、聊天列表），都是**背景活动**，
   不要为它单独开段，也不要因为它就判断这一段时间在干什么。
10. 如果随附了「任务栏条」图片：每行对应一张截图（行首 F0、F1… 与截图顺序一致，后面标了该帧的
    应用名），是那一刻屏幕底部任务栏的放大图。任务栏上图标下方更宽、带高亮背景的那一项（徽标），
    就是那一刻 Windows 认为正在使用的应用。当一张截图里有多个窗口、你难以判断用户
    在哪个界面时，以「任务栏徽标 + 该帧的应用」为准；两者都指向同一个应用时置信度更高。
11. 不是每一帧都有截图：时间线里没配图的帧同样要被活动段覆盖，段边界可以落在任意一帧上。

只输出 JSON，不要 markdown 代码块，不要解释：
{"activities":[{"from_frame":0,"to_frame":3,"app":"code","category":"coding",
"title":"改去重阈值","summary":"在 dayloom 里调 pHash 阈值并跑单测","confidence":0.8}]}
"""

ROLLUP_SYSTEM = """\
你是日程复盘助手。你会收到某一天的活动段汇总（数字已经由程序统计好了）。

规则：
1. 不要重算或改写任何数字；你只负责叙述和归纳。
2. 用中文写 150~250 字，说清这一天的主线：先做什么、后做什么、被打断了几次、
   哪段时间是连续的深度工作。
3. 指出 1~3 个值得注意的点（例如大块时间被切碎、开会占比过高）。
4. 不要编造汇总里没有的活动。

只输出 JSON：
{"narrative":"...","highlights":["...","..."],"suggestions":["..."]}
"""


WEEK_SYSTEM = """\
你是周复盘助手。你会收到一周每天的活动时长与分类统计（数字已经由程序算好）。

规则：
1. 不要重算或改写任何数字；你只负责叙述和归纳。
2. 用中文写 200~300 字：这一周的主线是什么、哪天最专注、哪天最碎、
   有没有重复出现的中断模式。
3. 指出 2~4 个值得注意的点或可执行的建议，要具体（"把会议压到周二下午"
   比"减少会议"好）。
4. 不要编造统计里没有的活动。

只输出 JSON：
{"narrative":"...","highlights":["...","..."],"suggestions":["..."]}
"""


def build_week_user(
    *,
    start_day: str,
    end_day: str,
    tracked_seconds: float,
    categories: dict[str, float],
    apps: dict[str, float],
    daily: list[tuple[str, float, str]],
) -> str:
    def _hm(seconds: float) -> str:
        seconds = max(0, int(seconds))
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"

    lines = [
        f"周期：{start_day} 至 {end_day}",
        f"总记录时长：{_hm(tracked_seconds)}",
        "",
        "每天（日期 | 时长 | 占比最高的事）：",
    ]
    for day, seconds, top in daily:
        lines.append(f"- {day} | {_hm(seconds)} | {top or '无'}")
    lines.extend(
        [
            "",
            "分类合计：",
            "\n".join(
                f"- {CATEGORY_ZH.get(k, k)}: {_hm(v)}"
                for k, v in sorted(categories.items(), key=lambda kv: -kv[1])
            )
            or "- （无）",
            "",
            "应用合计：",
            "\n".join(
                f"- {k}: {_hm(v)}" for k, v in sorted(apps.items(), key=lambda kv: -kv[1])[:8]
            )
            or "- （无）",
            "",
            "请只输出 JSON。",
        ]
    )
    return "\n".join(lines)


#: Timeline lines sent at most; a 15-minute window rarely needs more, but a
#: pathological batch must not turn into a novel.
MAX_TIMELINE_LINES = 120


def screen_of(monitor: str) -> str:
    """``m2:2560x1440+-2560+236`` -> ``m2``; unknown -> ``?``."""
    label = (monitor or "").split(":", 1)[0].strip()
    return label or "?"


def _clip(text: str, limit: int = 90) -> str:
    """One line, no pipes (they separate the metadata fields)."""
    flat = " ".join(str(text or "").replace("|", "｜").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def frame_line(index: int, frame) -> str:
    """``[帧 3] 10:20:00 | m2 | code | main.py`` — the format the model parses."""
    return (
        f"[帧 {index}] {clock.fmt_hms(frame.ts)} | {screen_of(frame.monitor)} | "
        f"{frame.app or 'unknown'} | {_clip(frame.title)}"
    )


def timeline_lines(frames: list) -> str:
    """Per-frame metadata, with consecutive identical windows merged into runs.

    The model must see *all* frames, not only the ones with a screenshot: with
    two monitors, the frames that were dropped from the image budget are
    exactly the ones that prove the user was somewhere else for ten minutes.
    """
    runs: list[dict] = []
    for index, frame in enumerate(frames):
        key = (frame.app or "unknown", _clip(frame.title), screen_of(frame.monitor))
        if runs and runs[-1]["key"] == key:
            runs[-1]["end"] = index
        else:
            runs.append({"key": key, "start": index, "end": index})
    if len(runs) > MAX_TIMELINE_LINES:
        head = len(runs) // 2
        tail = MAX_TIMELINE_LINES - head
        omitted = len(runs) - MAX_TIMELINE_LINES
        runs = runs[:head] + [{"omitted": omitted}] + runs[-tail:]

    lines: list[str] = []
    for run in runs:
        if "omitted" in run:
            lines.append(f"…（中间省略 {run['omitted']} 段窗口切换）…")
            continue
        first = frames[run["start"]]
        last = frames[run["end"]]
        if run["start"] == run["end"]:
            span = f"帧 {run['start']}"
            when = clock.fmt_hms(first.ts)
        else:
            span = f"帧 {run['start']}–{run['end']}"
            when = f"{clock.fmt_hms(first.ts)}–{clock.fmt_hms(last.ts)}"
        app, title, screen = run["key"]
        lines.append(f"{span} | {when} | {screen} | {app} | {title}")
    return "\n".join(lines)


def frame_meta_lines(frames: list, *, tz_offset: str = "") -> str:
    """Render one metadata line per frame (no images here)."""
    return "\n".join(frame_line(index, frame) for index, frame in enumerate(frames))


def build_segment_user(
    *,
    day: str,
    start_ts: float,
    end_ts: float,
    frames: list,
    images: list | None = None,
    max_frames_notice: str = "",
    include_meta: bool = True,
) -> str:
    """The batch header: time window, the full timeline, then image placement.

    ``frames`` is *every* frame of the batch (all of them get metadata);
    ``images`` are the subset that will also be attached as screenshots.
    """
    span = max(0.0, end_ts - start_ts)
    images = images or []
    head = (
        f"日期 {day}，时间段 {clock.fmt_hms(start_ts)}–{clock.fmt_hms(end_ts)}"
        f"（共 {int(span // 60)} 分钟，{len(frames)} 帧，其中 {len(images)} 帧附了截图）。\n"
    )
    if include_meta:
        head += (
            "时间线（行首是帧号范围；连续相同的前台窗口已合并）：\n"
            + timeline_lines(frames)
            + "\n"
        )
    else:
        head += "下面按顺序给出每一帧的元数据与截图。\n"
    if max_frames_notice:
        head += f"\n注意：{max_frames_notice}\n"
    tail = (
        "\n下面按顺序给出附截图的帧，每张前面标了对应帧号（帧号指向上面的时间线）："
        "\n\n请只输出 JSON。from_frame / to_frame 必须是时间线里出现过的帧号。"
    )
    return head + tail


def build_rollup_user(
    *,
    day: str,
    tracked_seconds: float,
    categories: dict[str, float],
    apps: dict[str, float],
    activities: list[tuple[str, str, float]],
) -> str:
    def _hm(seconds: float) -> str:
        seconds = max(0, int(seconds))
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"

    cats = "\n".join(
        f"- {CATEGORY_ZH.get(k, k)}: {_hm(v)}"
        for k, v in sorted(categories.items(), key=lambda x: -x[1])
    )
    tops = "\n".join(f"- {k}: {_hm(v)}" for k, v in sorted(apps.items(), key=lambda x: -x[1])[:8])

    lines = [
        f"日期：{day}",
        f"总记录时长：{_hm(tracked_seconds)}",
        "",
        "各类别时长：",
        cats or "- （无）",
        "",
        "应用时长前几名：",
        tops or "- （无）",
    ]
    if activities:
        lines.extend(["", "活动段（时间 | 类别 | 描述 | 时长）："])
        for start_ts, summary, duration, category in activities:
            lines.append(
                f"- {clock.fmt_hm(start_ts)} | {CATEGORY_ZH.get(category, category)}"
                f" | {summary}（{_hm(duration)}）"
            )
    lines.append("\n请只输出 JSON。")
    return "\n".join(lines)
