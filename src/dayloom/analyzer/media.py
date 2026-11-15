"""Media playback gets one coarse line, not a shot-by-shot commentary.

A video produces hundreds of nearly identical frames. Asking a vision model to
describe them yields danmaku transcriptions ("弹幕『对面莫名其妙就输了』") and
scene narration ("从选手憋笑到最终笑场") that nobody wants in a time log.

So for ``category == media`` the text is rebuilt deterministically from the
window title: platform + what is playing, one line, no detail. The model still
decides *that* it is media; we decide how much to say about it.
"""

from __future__ import annotations

import re

#: (title/app needle, display name) — checked in order.
PLATFORMS: tuple[tuple[str, str], ...] = (
    ("哔哩哔哩", "B站"),
    ("bilibili", "B站"),
    ("b站", "B站"),
    ("b 站", "B站"),
    ("youtube", "YouTube"),
    ("腾讯视频", "腾讯视频"),
    ("爱奇艺", "爱奇艺"),
    ("优酷", "优酷"),
    ("芒果tv", "芒果TV"),
    ("netflix", "Netflix"),
    ("抖音", "抖音"),
    ("斗鱼", "斗鱼"),
    ("虎牙", "虎牙"),
    ("网易云音乐", "网易云音乐"),
    ("网易云", "网易云音乐"),
    ("qq音乐", "QQ音乐"),
    ("spotify", "Spotify"),
    ("potplayer", "本地视频"),
    ("vlc", "本地视频"),
    ("mpv", "本地视频"),
)

#: Browser/player decorations that say nothing about the content.
SUFFIXES: tuple[str, ...] = (
    "_哔哩哔哩_bilibili",
    " - 哔哩哔哩",
    " - bilibili",
    " | bilibili",
    " - YouTube",
    " - YouTube Music",
    " - Google Chrome",
    " - Microsoft​Edge",
    " - Microsoft Edge",
    " - Tabbit",
    " - Mozilla Firefox",
    " - VLC media player",
)

MAX_PHRASE = 42
_BRACKETS = re.compile(r"【[^】]*】|\[[^\]]*\]|（[^）]*）|\([^)]*\)")


def platform_of(*texts: str) -> str:
    blob = " ".join(t for t in texts if t).lower()
    for needle, name in PLATFORMS:
        if needle.lower() in blob:
            return name
    return ""


def clean_title(title: str) -> str:
    """Strip decorations and noisy brackets; keep the content name."""
    text = title or ""
    for suffix in SUFFIXES:
        if text.lower().endswith(suffix.lower()):
            text = text[: -len(suffix)]
    text = _BRACKETS.sub(" ", text)
    # B站 puts "标题_分区" in the title bar; keep the title part.
    if "_" in text:
        head = text.split("_")[0].strip()
        if len(head) >= 4:
            text = head
    text = text.strip(" \"'“”‘’")
    text = re.sub(r"\s+", " ", text).strip(" -_|·—")
    # Titles are often truncated by the window manager; drop a dangling cut.
    if text.endswith(("...", "…")):
        text = text.rstrip(".…").strip()
    return text


def strip_platform(text: str) -> str:
    """Remove the platform name and the verb we already print ourselves."""
    out = text or ""
    for needle, name in PLATFORMS:
        out = re.sub(re.escape(needle), " ", out, flags=re.IGNORECASE)
        out = out.replace(name, " ")
    out = re.sub(r"^(观看|在看|全屏观看|全屏看|看)+", "", out.strip())
    out = re.sub(r"(直播|视频|录播)$", "", out.strip())
    return re.sub(r"\s+", " ", out).strip(" -_|·—：:")


def media_phrase(app: str, title: str, *extra: str) -> str:
    """One short line: 在B站看直播：挖机牧魂人。"""
    platform = platform_of(title, app, *extra)
    content = strip_platform(clean_title(title))
    live = any(word in (title or "") for word in ("直播", "live", "LIVE"))
    verb = "看直播" if live else "看视频"
    if platform == "本地视频":
        phrase = "看本地视频"
    elif platform:
        phrase = f"在{platform}{verb}"
    else:
        phrase = verb
    if content and content not in phrase:
        phrase = f"{phrase}：{content}"
    if len(phrase) > MAX_PHRASE:
        phrase = phrase[: MAX_PHRASE - 1] + "…"
    return phrase


def is_detail_noise(text: str) -> bool:
    """True when the model described the picture instead of the activity."""
    markers = ("弹幕", "评论区", "画面", "字幕", "镜头", "表情")
    return any(marker in (text or "") for marker in markers)


def is_media_window(app: str, title: str) -> bool:
    """True when this window is probably playing audio/video.

    Used by capture: watching a video looks exactly like being away (no keyboard
    input), so the idle threshold has to be different or the time is lost.
    """
    if platform_of(title or "", app or ""):
        return True
    blob = f"{title} {app}".lower()
    return any(
        word in blob
        for word in ("视频", "直播", "播放", "番剧", "音乐", "电影", "video", "player")
    )


def coarsen_media(activity, frame) -> None:
    """Replace a media segment's title/summary with the coarse phrase.

    ``activity`` is a ``NewActivity``; ``frame`` is any frame of the segment.
    The frame's window title is preferred over the model's paraphrase because
    it is objective and free.
    """
    if getattr(activity, "category", "") != "media":
        return
    frame_title = getattr(frame, "title", "") if frame is not None else ""
    frame_app = getattr(frame, "app", "") if frame is not None else ""
    # The window title is the objective source for *what* is playing, but it
    # often lacks the platform (a bilibili tab says 标题_分区). The model's own
    # wording usually names the site, so let it vote on the platform only.
    model_hint = f"{activity.title} {activity.summary}"
    title = clean_title(frame_title) or activity.title or ""
    app = activity.app or frame_app
    phrase = media_phrase(app, title, frame_title, frame_app, model_hint)
    activity.title = phrase
    activity.summary = phrase
