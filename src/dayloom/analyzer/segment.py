"""One batch in, structured activities out.

The model returns frame indices; this module turns them into timestamps, makes
the result contiguous (no holes, no overlaps), validates the taxonomy, and only
then writes. Everything the model says is treated as untrusted input.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..capture.screen import jpeg_from_file
from ..config import Config, LLMConfig
from ..llm.prompts import (
    CATEGORIES,
    SEGMENT_PROMPT_VERSION,
    SEGMENT_SYSTEM,
    build_segment_user,
    frame_line,
)
from ..llm.provider import LLMFormatError, Part, Usage, VisionClient
from ..store.repo import BatchRow, FrameRow, NewActivity, Repo
from .focus import build_focus_strip
from .media import coarsen_media
from .quality import QualityReport, assess
from .select import select_keyframes

log = logging.getLogger(__name__)

#: Caption for the composite image built by ``analyzer.focus``.
FOCUS_STRIP_NOTE = (
    "\n任务栏条：下面这张图里每行是一张截图底部的任务栏（行首标着 F0、F1… 与截图的先后顺序一致，"
    "后面是该帧的前台应用名；已经放大）。任务栏上图标下方更宽、带高亮背景的那一项，"
    "就是那一刻的前台应用。\n"
)


@dataclass(slots=True)
class SegmentOutcome:
    batch_id: int
    activities: int
    usage: Usage
    frames_sent: int
    state_count: int
    model: str
    prompt_version: str = SEGMENT_PROMPT_VERSION
    quality: QualityReport | None = None


def build_parts(
    frames: list[FrameRow],
    *,
    base: Path,
    max_edge: int,
    max_images: int,
    intro: str = "",
    image_ids: set[int] | None = None,
) -> tuple[list[Part], int]:
    """Interleave frame metadata with the images themselves.

    ``frames`` is every frame of the batch (the model needs the whole
    timeline); ``image_ids`` marks the subset that also gets a screenshot.
    """
    image_ids = image_ids or set()
    parts: list[Part] = []
    sent = 0
    if intro:
        parts.append(Part.of_text(intro))
    for index, frame in enumerate(frames):
        if frame.id not in image_ids:
            continue
        if sent >= max_images or not frame.file:
            continue
        data = jpeg_from_file(base / frame.file, max_edge)
        if not data:
            continue
        parts.append(Part.of_text("\n" + frame_line(index, frame) + "\n"))
        parts.append(Part.of_image(data))
        sent += 1
    return parts, sent


def parse_activities(payload: dict, frames: list[FrameRow]) -> list[NewActivity]:
    """Validate and repair the model's segment list against the frames we sent."""
    raw_items = payload.get("activities")
    if raw_items is None and isinstance(payload.get("segments"), list):
        raw_items = payload["segments"]
    if not isinstance(raw_items, list):
        raise LLMFormatError(f"返回里没有 activities 数组：{str(payload)[:200]}")

    n = len(frames)
    cleaned: list[NewActivity] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        try:
            start_idx = int(item.get("from_frame"))
            end_idx = int(item.get("to_frame", item.get("from_frame")))
        except (TypeError, ValueError):
            continue
        start_idx = max(0, min(start_idx, n - 1))
        end_idx = max(start_idx, min(end_idx, n - 1))

        start_ts = frames[start_idx].ts
        end_ts = frames[end_idx].ts
        if end_ts <= start_ts:
            # A single-frame segment: keep it visible but bounded.
            nxt = frames[end_idx + 1].ts if end_idx + 1 < n else None
            end_ts = (nxt - 0.5) if nxt else start_ts + 60.0
            if end_ts <= start_ts:
                end_ts = start_ts + 60.0

        category = str(item.get("category") or "other").strip().lower()
        if category not in CATEGORIES:
            category = "other"
        confidence = item.get("confidence", 0.5)
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = 0.5

        cleaned.append(
            NewActivity(
                start_ts=start_ts,
                end_ts=end_ts,
                app=str(item.get("app") or frames[start_idx].app or "")[:120],
                category=category,
                title=str(item.get("title") or "")[:200],
                summary=str(item.get("summary") or "")[:500],
                confidence=confidence,
                frame_ids=[f.id for f in frames[start_idx : end_idx + 1]],
            )
        )
        # Video/stream playback is described coarsely and deterministically:
        # the model decided *that* it is media, we decide how much to say.
        coarsen_media(cleaned[-1], frames[start_idx])

    if not cleaned:
        raise LLMFormatError("模型没有返回任何可用的活动段")

    cleaned.sort(key=lambda a: a.start_ts)
    out: list[NewActivity] = []
    for act in cleaned:
        if out:
            prev = out[-1]
            if act.start_ts < prev.end_ts:
                act.start_ts = prev.end_ts
            elif act.start_ts > prev.end_ts:
                # Stretch the previous segment instead of leaving a hole: the
                # screen did not change, the model just started a new段 later.
                prev.end_ts = act.start_ts
        if act.end_ts <= act.start_ts:
            continue
        out.append(act)
    return out


def analyze_batch(
    *,
    cfg: Config,
    repo: Repo,
    client: VisionClient,
    batch: BatchRow,
    base: Path | None = None,
    llm: LLMConfig | None = None,
) -> SegmentOutcome:
    home = base or cfg.home
    tier = llm or cfg.llm
    frames = repo.frames_for_batch(batch.id)
    if not frames:
        repo.replace_activities(batch.id, [])
        repo.mark_done(
            batch.id, model=tier.model, prompt_version=SEGMENT_PROMPT_VERSION,
            tokens_in=0, tokens_out=0,
        )
        return SegmentOutcome(batch.id, 0, Usage(), 0, 0, tier.model)

    selection = select_keyframes(
        frames, max_frames=tier.max_frames, dedupe_distance=cfg.capture.dedupe_distance
    )
    chosen = selection.frames
    notice = ""
    if selection.dropped:
        notice = (
            f"这段时间共有 {len(frames)} 帧，为了让上下文装得下，只给其中 {len(chosen)} 帧配了截图；"
            "其余帧的元数据都在上面的时间线里，请把它们当作连续时间上的证据。"
        )
    intro = build_segment_user(
        day=batch.day,
        start_ts=batch.start_ts,
        end_ts=batch.end_ts,
        frames=frames,
        images=chosen,
        max_frames_notice=notice,
    )
    parts, sent = build_parts(
        frames,
        base=home,
        max_edge=cfg.capture.max_edge,
        max_images=tier.max_frames,
        intro=intro,
        image_ids={frame.id for frame in chosen},
    )
    # The enlarged taskbar strips are the visual "you are here" cue; one small
    # image for the whole batch, skipped when no frame recorded a taskbar.
    focus_image = build_focus_strip(chosen, base=home) if cfg.llm.focus_strip else None
    if focus_image:
        parts.append(Part.of_text(FOCUS_STRIP_NOTE))
        parts.append(Part.of_image(focus_image))
    payload, usage = client.complete_json(parts, system=SEGMENT_SYSTEM)
    activities = parse_activities(payload, frames)
    if activities:
        # The batch window is the observation window, but an idle/locked gap
        # means nobody was there — except for media: watching a video produces
        # no keyboard input at all, so short idle gaps must not cut it short.
        # A gap longer than the media leash still wins, or an abandoned tab
        # would be counted as hours of "影音".
        leash = float(cfg.capture.media_idle_seconds)
        first_is_media = activities[0].category == "media"
        last_is_media = activities[-1].category == "media"
        lower, upper = batch.start_ts, batch.end_ts
        for gap in repo.gaps_between(batch.start_ts, batch.end_ts):
            gap_end = gap.end_ts if gap.end_ts is not None else batch.end_ts
            short_idle = gap.kind == "idle" and (gap_end - gap.start_ts) <= leash
            if gap_end <= activities[0].start_ts and not (short_idle and first_is_media):
                lower = max(lower, gap_end)
            if gap.start_ts >= activities[-1].end_ts and not (short_idle and last_is_media):
                upper = min(upper, gap.start_ts)
        activities[0].start_ts = min(activities[0].start_ts, lower)
        activities[-1].end_ts = max(activities[-1].end_ts, upper)
    quality = assess(activities)
    repo.replace_activities(batch.id, activities)
    repo.mark_done(
        batch.id,
        model=tier.model,
        prompt_version=SEGMENT_PROMPT_VERSION,
        tokens_in=usage.prompt_tokens,
        tokens_out=usage.completion_tokens,
        images=sent,
    )
    log.info(
        "批次 %s 完成：%d 帧 → %d 张图 → %d 段，tokens in=%d out=%d",
        batch.id,
        len(frames),
        sent,
        len(activities),
        usage.prompt_tokens,
        usage.completion_tokens,
    )
    return SegmentOutcome(
        batch_id=batch.id,
        activities=len(activities),
        usage=usage,
        frames_sent=sent,
        state_count=selection.state_count,
        model=tier.model,
        quality=quality,
    )
