"""Media coarsening: one line per video, never a shot-by-shot commentary."""

from __future__ import annotations

from types import SimpleNamespace

from dayloom.analyzer.media import clean_title, coarsen_media, media_phrase, platform_of
from dayloom.store.repo import NewActivity


def test_platform_detection():
    assert platform_of("轻松一刻_哔哩哔哩_bilibili") == "B站"
    assert platform_of("", "bilibili") == "B站"
    assert platform_of("Some Video - YouTube") == "YouTube"
    assert platform_of("随便一个窗口") == ""


def test_clean_title_strips_decorations():
    assert clean_title("轻松一刻_哔哩哔哩_bilibili") == "轻松一刻"
    assert clean_title("【合集】挖机牧魂人 - YouTube") == "挖机牧魂人"
    assert clean_title("") == ""


def test_media_phrase_is_short_and_specific():
    assert media_phrase("", "哔哩哔哩英雄联盟直播") == "在B站看直播：英雄联盟"
    assert media_phrase("", "挖机牧魂人_哔哩哔哩_bilibili") == "在B站看视频：挖机牧魂人"
    assert media_phrase("", "随便一个窗口") == "看视频：随便一个窗口"


def test_media_phrase_is_capped():
    phrase = media_phrase("", "哔哩哔哩" + "很长的标题" * 20)
    assert len(phrase) <= 42


def _activity(title: str, summary: str, category: str = "media") -> NewActivity:
    return NewActivity(
        start_ts=0.0,
        end_ts=60.0,
        app="tabbit browser",
        category=category,
        title=title,
        summary=summary,
        confidence=0.8,
        frame_ids=[],
    )


def test_detailed_media_summary_is_replaced():
    act = _activity(
        "全屏看英雄联盟解说视频",
        "全屏观看一局英雄联盟的游戏解说视频，画面上飘着弹幕，选手从憋笑到笑场",
    )
    frame = SimpleNamespace(title="英雄联盟解说_哔哩哔哩_bilibili", app="tabbit browser")
    coarsen_media(act, frame)
    assert act.summary == "在B站看视频：英雄联盟解说"
    assert "弹幕" not in act.summary
    assert "笑场" not in act.summary


def test_non_media_is_untouched():
    act = _activity("改代码", "在编辑器里改去重阈值", category="coding")
    coarsen_media(act, SimpleNamespace(title="main.py - Code", app="code"))
    assert act.summary == "在编辑器里改去重阈值"
    assert act.title == "改代码"


def test_media_window_detection_for_capture():
    """Capture uses this to keep the idle threshold sane while a video plays."""
    from dayloom.analyzer.media import is_media_window

    assert is_media_window("tabbit browser", "哔哩哔哩英雄联盟直播")
    assert is_media_window("potplayer", "电影.mkv")
    assert is_media_window("chrome", "Some Video - YouTube")
    assert not is_media_window("code", "main.py - Visual Studio Code")
    assert not is_media_window("explorer", "下载")


def test_media_without_title_still_says_something():
    act = _activity("", "")
    coarsen_media(act, SimpleNamespace(title="", app="potplayer"))
    assert act.summary == "看本地视频"
    assert act.title == act.summary


def test_clean_title_handles_bilibili_title_bar():
    # Real window title seen on this machine: 标题_分区, no site suffix.
    assert clean_title('"他是人机"_英雄联盟') == "他是人机"
    assert clean_title("英雄联盟解说_哔哩哔哩_bilibili") == "英雄联盟解说"


def test_model_wording_supplies_the_platform():
    """The title bar often omits the site; the model's sentence usually has it."""
    act = _activity("看视频", "在 B 站看英雄联盟对局视频，画面里操控盖伦")
    frame = SimpleNamespace(title='"他是人机"_英雄联盟', app="tabbit browser")
    coarsen_media(act, frame)
    assert act.summary == "在B站看视频：他是人机"


def test_media_is_not_flagged_as_shallow():
    """Short is the point for media; the quality gate must not escalate it."""
    from dayloom.analyzer.quality import _is_shallow

    act = _activity("在B站看直播：英雄联盟", "在B站看直播：英雄联盟")
    assert not _is_shallow(act)
    short_code = _activity("x", "y", category="coding")
    assert _is_shallow(short_code)
