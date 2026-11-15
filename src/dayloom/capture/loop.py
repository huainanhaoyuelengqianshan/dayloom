"""The capture loop.

The rule that keeps cost sane is ``decide()``: look often (cheap), write rarely
(expensive). A frame is written when the app changes, when the picture changed
*and* the heartbeat allows it, or when a static screen has gone without an
anchor for ``max_still_minutes``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import clock, paths
from ..analyzer.media import is_media_window
from ..config import Config
from ..store.repo import Repo
from . import taskbar, winapi
from .dedupe import dhash, hamming, is_flat
from .screen import Grabber, encode_webp, scale_to_max_edge, window_covers

log = logging.getLogger(__name__)

MAX_TITLE_CHARS = 512


@dataclass(slots=True)
class Decision:
    store: bool
    reason: str


def decide(
    *,
    now: float,
    last_store_ts: float | None,
    last_app: str,
    last_title: str,
    app: str,
    title: str,
    distance: int,
    dedupe_distance: int,
    heartbeat: float,
    max_still_minutes: int,
    flat: bool,
) -> Decision:
    """Pure storage policy — see tests/test_policy.py."""
    if flat:
        return Decision(False, "flat")
    if last_store_ts is None:
        return Decision(True, "first")
    # A different application is a real context switch: capture it now.
    if app != last_app:
        return Decision(True, "app")
    elapsed = now - last_store_ts
    if title != last_title and elapsed >= heartbeat:
        return Decision(True, "title")
    if distance > dedupe_distance and elapsed >= heartbeat:
        return Decision(True, "change")
    if max_still_minutes > 0 and elapsed >= max_still_minutes * 60:
        return Decision(True, "anchor")
    return Decision(False, "dedupe")


class Blacklist:
    """Window titles / executables that must never be captured."""

    def __init__(self, patterns: list[str], apps: list[str]) -> None:
        self._patterns = [re.compile(p) for p in patterns]
        self._apps = {a.strip().lower() for a in apps if a.strip()}

    def match(self, app: str, exe: str, title: str) -> str | None:
        if app and app.lower() in self._apps:
            return f"app:{app}"
        if exe and Path(exe).name.lower() in self._apps:
            return f"app:{Path(exe).name}"
        for pattern in self._patterns:
            if pattern.search(title or ""):
                return f"title:{pattern.pattern}"
        return None


@dataclass
class CaptureStats:
    stored: int = 0
    skipped_dedupe: int = 0
    skipped_flat: int = 0
    idle_ticks: int = 0
    locked_ticks: int = 0
    protected_ticks: int = 0
    errors: int = 0
    last_store_ts: float | None = None
    last_reason: str = field(default="")

    def as_dict(self) -> dict[str, object]:
        return {
            "stored": self.stored,
            "skipped_dedupe": self.skipped_dedupe,
            "skipped_flat": self.skipped_flat,
            "idle_ticks": self.idle_ticks,
            "locked_ticks": self.locked_ticks,
            "protected_ticks": self.protected_ticks,
            "errors": self.errors,
            "last_store_ts": self.last_store_ts,
            "last_reason": self.last_reason,
        }


class CaptureLoop:
    def __init__(self, cfg: Config, repo: Repo, stop: asyncio.Event) -> None:
        self.cfg = cfg
        self.repo = repo
        self.stop = stop
        self.home: Path = cfg.home
        self.stats = CaptureStats()
        self._grabber = Grabber()
        self._blacklist = Blacklist(cfg.capture.blacklist_titles, cfg.capture.blacklist_apps)
        self._last_hash: int | None = None
        self._last_key: tuple[str, str] = ("", "")
        self._last_presence: str | None = None
        self._paused_flag = paths.paused_flag(self.home)

    # ------------------------------------------------------------------
    async def run(self) -> None:
        winapi.enable_dpi_awareness()
        log.info("采集循环启动：mode=%s interval=%.1fs", self.cfg.capture.mode, self.cfg.capture.probe_interval)
        try:
            while not self.stop.is_set():
                delay = self.cfg.capture.probe_interval
                try:
                    delay = self._tick()
                except Exception:
                    self.stats.errors += 1
                    log.exception("采集失败，2 秒后重试")
                    delay = 2.0
                await self._sleep(delay)
        finally:
            try:
                self.repo.set_presence(None, clock.now_ts())
            except Exception:  # pragma: no cover - shutdown best effort
                log.exception("关闭存在状态时出错")
            self._grabber.close()
            log.info("采集循环结束：%s", self.stats.as_dict())

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.stop.wait(), timeout=max(0.1, seconds))

    # ------------------------------------------------------------------
    def _tick(self) -> float:
        cap = self.cfg.capture
        now = clock.now_ts()

        if self._paused_flag.exists():
            self._last_presence = None
            return 2.0

        if winapi.is_locked():
            self._note_gap("locked", now)
            self.stats.locked_ticks += 1
            return 5.0

        win = winapi.foreground_window()
        if win is None:
            self._last_presence = None
            return cap.probe_interval

        # Watching a video looks like being away: no keys, no mouse. Give media
        # windows a much longer leash, otherwise the "影音" time is cut short.
        idle_limit = cap.idle_seconds
        if is_media_window(win.app, win.title):
            idle_limit = max(idle_limit, cap.media_idle_seconds)
        if winapi.idle_seconds() >= idle_limit:
            self._note_gap("idle", now)
            self.stats.idle_ticks += 1
            return 3.0

        hit = self._blacklist.match(win.app, win.exe, win.title)
        if hit:
            self._note_gap("protected", now)
            self.stats.protected_ticks += 1
            return cap.probe_interval

        self._note_present(now)

        rect, monitor_label = self._grabber.choose_rect(cap.mode, win.rect)
        image = self._grabber.grab(rect)
        scaled = scale_to_max_edge(image, cap.max_edge)
        distance = hamming(dhash(scaled), self._last_hash)

        decision = decide(
            now=now,
            last_store_ts=self.stats.last_store_ts,
            last_app=self._last_key[0],
            last_title=self._last_key[1],
            app=win.app,
            title=win.title,
            distance=distance,
            dedupe_distance=cap.dedupe_distance,
            heartbeat=cap.heartbeat,
            max_still_minutes=cap.max_still_minutes,
            flat=is_flat(image),
        )
        self.stats.last_reason = decision.reason
        if not decision.store:
            if decision.reason == "flat":
                self.stats.skipped_flat += 1
            else:
                self.stats.skipped_dedupe += 1
            return cap.probe_interval

        self._store(
            ts=now,
            image=scaled,
            dhash_value=dhash(scaled),
            win=win,
            rect=rect,
            monitor_label=monitor_label,
        )
        return cap.probe_interval

    # ------------------------------------------------------------------
    def _focus_payload(
        self, rect: tuple[int, int, int, int], window_rect: tuple[int, int, int, int] | None
    ) -> str:
        """Where the taskbar sits inside this capture, if it is visible at all.

        The analyzer crops exactly this strip, enlarges it and sends it with the
        batch: the highlighted icon is the "which window was really in front"
        cue, which the full-screen image loses once it is scaled down for the
        model. ``edge`` is kept for diagnostics only.
        """
        try:
            # A full-screen app paints over the taskbar; the rectangle is still
            # there, but cropping it would label the bottom of the video as a
            # taskbar. Only the captured screen is affected: a full-screen video
            # on the other display must not suppress evidence for this one.
            if window_covers(window_rect, rect):
                return ""
            monitors = [mon.box for mon in self._grabber.monitors[1:]]
            found = taskbar.strip_for(rect, monitors)
            if found is None:
                return ""
            bar, box = found
            return json.dumps(
                {"strip": [round(value, 4) for value in box], "edge": bar.edge},
                ensure_ascii=False,
            )
        except Exception:  # pragma: no cover - diagnostic only, never lose a frame
            log.debug("任务栏条带探测失败", exc_info=True)
            return ""

    def _store(self, *, ts: float, image, dhash_value: int, win, rect, monitor_label: str) -> None:
        day = clock.day_key(ts, self.cfg.general.day_boundary_hour)
        rel = paths.frame_relpath(self.home, day, ts)
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        data = encode_webp(image, self.cfg.capture.webp_quality)
        path.write_bytes(data)
        self.repo.insert_frame(
            ts=ts,
            day=day,
            app=win.app,
            exe=win.exe,
            title=(win.title or "")[:MAX_TITLE_CHARS],
            monitor=monitor_label,
            rect=rect,
            file=rel,
            size=len(data),
            dhash=dhash_value,
            focus=self._focus_payload(rect, win.rect),
        )
        self.stats.stored += 1
        self.stats.last_store_ts = ts
        self._last_hash = dhash_value
        self._last_key = (win.app, win.title or "")
        log.debug("存入一帧 %s (%d bytes, %s)", rel, len(data), self.stats.last_reason)

    def _note_present(self, now: float) -> None:
        if self._last_presence != "present":
            # Coming back from idle/lock/pause: make sure the first frame of the
            # new stretch is stored instead of being deduped against an old one.
            self.stats.last_store_ts = None
            self._last_hash = None
            self._last_presence = "present"
            self.repo.set_presence(None, now)

    def _note_gap(self, kind: str, now: float) -> None:
        self._last_presence = kind
        self.repo.set_presence(kind, now)
