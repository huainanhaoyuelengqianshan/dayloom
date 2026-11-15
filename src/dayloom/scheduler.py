"""The daemon: capture, analysis, daily rollup, retention.

One process, three loops, one writer. Analysis runs in a worker thread because
an HTTP call to a vision model takes seconds and would otherwise stall capture.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, time, timedelta
from pathlib import Path

from . import clock, config, paths, runtime
from .analyzer.pipeline import analyze_with_escalation, build_clients
from .analyzer.report import build_report
from .capture.loop import CaptureLoop
from .config import Config
from .llm.provider import LLMError, VisionClient, is_fatal_error
from .llm.session import VisionUnavailable, probe_summary
from .store.db import open_db
from .store.repo import STATUS_DONE, STATUS_FAILED, Repo

log = logging.getLogger(__name__)

ANALYSIS_TICK_SECONDS = 60.0
HEARTBEAT_SECONDS = 20.0
BATCHES_PER_TICK = 3
CLEANUP_INTERVAL_SECONDS = 6 * 3600
#: A batch claimed but not updated for this long is assumed dead and requeued.
STALE_BATCH_SECONDS = 1200.0
#: Look this far back for days whose rollup never ran.
REPORT_BACKFILL_DAYS = 14


class Daemon:
    def __init__(self, cfg: Config, config_path: Path | None = None) -> None:
        self.cfg = cfg
        self.home = cfg.home
        self.config_path = config_path
        self.stop = asyncio.Event()
        self.db = open_db(cfg.db_path)
        self.repo = Repo(self.db)
        self._client: VisionClient | None = None
        self._clients_cache: dict[str, VisionClient] | None = None
        self._vision_error: str | None = None
        self._capture: CaptureLoop | None = None
        self._capture_task: asyncio.Task | None = None
        self._tasks: list[asyncio.Task] = []
        self._cfg_mtime = self._config_mtime()
        self._budget_warned_at = 0.0
        self._fatal_warned_at = 0.0

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def run(self) -> None:
        runtime.write_pid(self.home)
        runtime.touch_heartbeat(self.home)
        self._recover_state()
        tasks = [
            asyncio.create_task(self._analysis_loop(), name="analysis"),
            asyncio.create_task(self._daily_loop(), name="daily"),
            asyncio.create_task(self._cleanup_loop(), name="cleanup"),
            asyncio.create_task(self._reload_loop(), name="reload"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
        ]
        self._tasks = tasks
        await self._start_capture()

        log.info("dayloom 已启动 pid=%s 数据目录=%s", runtime.read_pid(self.home), self.home)
        try:
            await self.stop.wait()
        finally:
            await self._shutdown(tasks)

    # -- capture task (restartable, because capture settings can change) --
    async def _start_capture(self) -> None:
        if not self.cfg.capture.enabled:
            log.warning("采集已在配置里关闭，只跑分析与日报")
            return
        self._capture = CaptureLoop(self.cfg, self.repo, self.stop)
        self._capture_task = asyncio.create_task(self._capture.run(), name="capture")

    async def _stop_capture(self) -> None:
        task = self._capture_task
        self._capture_task = None
        self._capture = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _restart_capture(self) -> None:
        await self._stop_capture()
        await self._start_capture()

    async def _shutdown(self, tasks: list[asyncio.Task]) -> None:
        log.info("正在停止…")
        self.stop.set()
        await self._stop_capture()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._client is not None:
            self._client.close()
        runtime.clear_pid(self.home)
        with contextlib.suppress(OSError):
            paths.heartbeat_file(self.home).unlink()
        self.db.close()
        log.info("已停止")

    def request_stop(self) -> None:
        self.stop.set()

    # -- hot reload ------------------------------------------------------
    def _config_file(self) -> Path:
        return self.config_path or (paths.home() / "config.toml")

    def _config_mtime(self) -> float:
        try:
            return self._config_file().stat().st_mtime
        except OSError:
            return 0.0

    async def _reload_loop(self) -> None:
        """Pick up edits to config.toml without restarting the daemon."""
        while not self.stop.is_set():
            await self._sleep(2.0)
            if self.stop.is_set():
                return
            mtime = self._config_mtime()
            if mtime <= self._cfg_mtime:
                continue
            try:
                new_cfg = config.load(self.config_path)
            except Exception:
                log.exception("配置文件解析失败，继续使用旧配置")
                continue
            self._cfg_mtime = mtime
            if new_cfg.home != self.home:
                log.warning(
                    "data_dir 改成了 %s：需要重启守护进程才会切过去", new_cfg.home
                )
            self.cfg = new_cfg
            if self._client is not None:
                self._client.close()
                self._client = None
            if self._clients_cache is not None:
                for client in self._clients_cache.values():
                    client.close()
                self._clients_cache = None
            log.info("配置已重载（采集参数立即生效）")
            await self._restart_capture()

    def _recover_state(self) -> None:
        """Called once at startup: make yesterday's leftovers consistent."""
        reclaimed = self.repo.reclaim_stale_running()
        if reclaimed:
            log.info("重新排队了 %d 个中断的批次", reclaimed)
        last = self.repo.last_frame()
        anchor = last.ts if last else clock.now_ts()
        # A daemon that was killed leaves a gap open; close it at the last frame
        # instead of letting it swallow the whole offline period.
        self.repo.set_presence(None, anchor)

    # ------------------------------------------------------------------
    # analysis
    # ------------------------------------------------------------------
    async def _analysis_loop(self) -> None:
        await asyncio.sleep(5.0)
        while not self.stop.is_set():
            try:
                await asyncio.to_thread(self._analysis_once)
            except VisionUnavailable as exc:
                self._vision_error = exc.reason
                log.warning("视觉校验未通过，暂停分析：%s", exc.reason)
                await self._sleep(300.0)
                continue
            except Exception:
                log.exception("分析循环出错")
            await self._sleep(ANALYSIS_TICK_SECONDS)

    def _analysis_once(self) -> None:
        if not (self.cfg.llm.enabled and self.cfg.llm.configured):
            return
        # A killed process (or a crashed analysis) leaves a batch in 'running'
        # forever, because only the claim path can move it. Reclaim periodically
        # so one bad run cannot block the rest of the day.
        self.repo.reclaim_stale_running(STALE_BATCH_SECONDS)
        now = clock.now_ts()
        today = clock.day_key(now, self.cfg.general.day_boundary_hour)
        created = 0
        for day in (today, clock.shift_day(today, -1)):
            created += self.repo.ensure_batches(
                day=day,
                batch_seconds=self.cfg.batch_seconds,
                grace_seconds=self.cfg.analysis.grace_seconds,
                now=now,
                boundary_hour=self.cfg.general.day_boundary_hour,
            )
        log.debug("分析循环：day=%s 新批次=%d 未分配帧=%s", today, created,
                  self.db.scalar("SELECT COUNT(*) FROM frames WHERE batch_id IS NULL"))

        # Images are what the bill is made of, so the cap is on images, not on
        # batches or tokens. Batches stay queued and resume after midnight.
        left = image_budget_left(self.cfg, self.repo, today)
        if left is not None and left <= 0:
            self._warn_budget(self.repo.images_used_on(today), self.cfg.analysis.max_images_per_day)
            return

        for _ in range(BATCHES_PER_TICK):
            batch = self.repo.next_pending_batch(self.cfg.analysis.max_attempts)
            if batch is None:
                return
            clients = self._clients()
            if not self.repo.claim_batch(batch.id):
                continue
            try:
                result = analyze_with_escalation(
                    cfg=self.cfg, repo=self.repo, batch=batch, clients=clients
                )
                log.info(
                    "批次 %s：%d 段（%s%s）",
                    batch.id,
                    result.outcome.activities,
                    result.tier,
                    "，升级" if result.escalated else "",
                )
            except LLMError as exc:
                if is_fatal_error(exc):
                    # No balance / bad key: the user has to act, so keep the
                    # batch queued (and unspent) and stop hammering the endpoint.
                    self.repo.release_batch(batch.id, str(exc))
                    self._warn_fatal(str(exc))
                    return
                log.warning("批次 %s 失败：%s", batch.id, exc)
                self.repo.mark_failed(batch.id, str(exc))
            except Exception as exc:
                log.exception("批次 %s 异常", batch.id)
                self.repo.mark_failed(batch.id, f"{type(exc).__name__}: {exc}")

    def _warn_fatal(self, message: str) -> None:
        now = clock.now_ts()
        if now - self._fatal_warned_at < 1800:
            return
        self._fatal_warned_at = now
        log.warning(
            "分析因额度/鉴权问题暂停（批次保留，不会丢）：%s\n"
            "  处理完会自动恢复：dayloom balance 查看余额，或 dayloom use-local 切本地模型",
            message[:200],
        )

    def _warn_budget(self, used: int, budget: int) -> None:
        """Say it once every half hour, not every sixty seconds."""
        now = clock.now_ts()
        if now - self._budget_warned_at < 1800:
            return
        self._budget_warned_at = now
        log.warning(
            "今日图片预算已用完（%d/%d 张），暂停分析；批次会排队到明天。"
            "要放宽就改 analysis.max_images_per_day",
            used,
            budget,
        )

    def _clients(self) -> dict[str, VisionClient]:
        """One verified client per tier; rebuilt when the config reloads."""
        if self._clients_cache is None:
            self._clients_cache = build_clients(self.cfg, self.repo)
        return self._clients_cache

    # ------------------------------------------------------------------
    # daily rollup
    # ------------------------------------------------------------------
    async def _daily_loop(self) -> None:
        # Catch-up first: a nightly rollup is skipped whenever the machine is
        # asleep at 04:15, and it used to be skipped *forever* — days with
        # activities but no report stayed without one.
        await self._sleep(25)
        while not self.stop.is_set():
            try:
                await asyncio.to_thread(self._fill_missing_reports)
            except Exception:
                log.exception("补生成日报失败")
            delay = self._seconds_until_daily()
            await self._sleep(delay)
            if self.stop.is_set():
                return
            try:
                await asyncio.to_thread(self._daily_once)
            except Exception:
                log.exception("日报生成失败")

    def _fill_missing_reports(self) -> None:
        from .analyzer.report import build_report

        hour = self.cfg.general.day_boundary_hour
        today = clock.day_key(clock.now_ts(), hour)
        existing = self.repo.report_days()

        client: VisionClient | None = None
        if self.cfg.llm.enabled and self.cfg.llm.configured:
            try:
                client = self._clients().get("primary")
            except VisionUnavailable:
                client = None

        made = 0
        # Oldest first: the user is more likely to look at a day that is already
        # a few days old than at yesterday.
        for offset in range(REPORT_BACKFILL_DAYS, 0, -1):
            day = clock.shift_day(today, -offset)
            if day in existing:
                continue
            start, end = clock.day_bounds(day, hour)
            if not self.repo.activities_between(start, end):
                continue
            try:
                build_report(
                    self.repo, self.cfg, day, narrate=client is not None, client=client
                )
            except Exception as exc:
                log.warning("补生成 %s 的日报失败：%s", day, exc)
                continue
            made += 1
        if made:
            log.info("补生成了 %d 天的日报", made)

    def _seconds_until_daily(self) -> float:
        boundary = time(hour=self.cfg.general.day_boundary_hour)
        now = datetime.now()
        today_end = datetime.combine(now.date() + timedelta(days=1), boundary)
        target = today_end + timedelta(minutes=15)
        if target <= now:
            target = today_end + timedelta(days=1)
        return max(30.0, (target - now).total_seconds())

    def _daily_once(self) -> None:
        day = clock.shift_day(
            clock.day_key(clock.now_ts(), self.cfg.general.day_boundary_hour), -1
        )
        client = None
        if self.cfg.llm.enabled and self.cfg.llm.configured:
            try:
                client = self._clients().get("primary")
            except VisionUnavailable as exc:
                log.warning("日报不带叙述：%s", exc.reason)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("日报不带叙述：%s", exc)
        report = build_report(
            self.repo, self.cfg, day, narrate=client is not None, client=client
        )
        log.info(
            "日报 %s 完成：%s，%d 段，%d 帧未分析",
            day,
            clock.fmt_duration(report.tracked_seconds),
            len(report.sessions),
            report.unanalyzed_frames,
        )

    # ------------------------------------------------------------------
    # retention
    # ------------------------------------------------------------------
    async def _heartbeat_loop(self) -> None:
        """Prove liveness to the GUI and to ``dayloom status``.

        Separate from the analysis loop on purpose: a 3B model on CPU can block
        analysis for minutes, and that must not look like a dead process.
        """
        while not self.stop.is_set():
            runtime.touch_heartbeat(self.home)
            await self._sleep(HEARTBEAT_SECONDS)

    async def _cleanup_loop(self) -> None:
        while not self.stop.is_set():
            await self._sleep(CLEANUP_INTERVAL_SECONDS)
            if self.stop.is_set():
                return
            try:
                await asyncio.to_thread(self._cleanup_once)
            except Exception:
                log.exception("清理旧截图失败")

    def _cleanup_once(self) -> None:
        days = self.cfg.storage.keep_frames_days
        if days <= 0:
            return
        cutoff = clock.now_ts() - days * 86400
        rel_paths = self.repo.delete_frames_before(cutoff)
        removed = 0
        for rel in rel_paths:
            path = self.home / rel
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        log.info("清理了 %d 个过期帧文件（保留 %d 天）", removed, days)

    # ------------------------------------------------------------------
    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.stop.wait(), timeout=max(0.1, seconds))


async def run_daemon(cfg: Config, config_path: Path | None = None) -> None:
    daemon = Daemon(cfg, config_path=config_path)
    try:
        await daemon.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        daemon.request_stop()


def image_budget_left(cfg: Config, repo: Repo, day: str) -> int | None:
    """Images still allowed today, or ``None`` when no budget is configured."""
    budget = cfg.analysis.max_images_per_day
    if not budget:
        return None
    return max(0, budget - repo.images_used_on(day))


def status_snapshot(cfg: Config) -> dict[str, object]:
    """Everything ``dayloom status`` shows, without holding a write lock."""
    home: Path = cfg.home
    db_path = cfg.db_path
    info: dict[str, object] = {
        "home": str(home),
        "db": str(db_path),
        "db_exists": db_path.exists(),
        "running_pid": runtime.running_pid(home),
        "paused": runtime.is_paused(home),
        "today": clock.day_key(clock.now_ts(), cfg.general.day_boundary_hour),
    }
    if not db_path.exists():
        return info
    from .store.db import open_db

    db = open_db(db_path, readonly=True)
    try:
        repo = Repo(db)
        # A read-only connection cannot migrate, so an older database may not
        # have the newer columns yet; the next write-open upgrades it.
        try:
            images_today = repo.images_used_on(str(info["today"]))
        except Exception:
            images_today = 0
        newest_error = db.one(
            "SELECT error, updated_at FROM batches WHERE status = 'failed' "
            "ORDER BY updated_at DESC LIMIT 1"
        )
        info["last_error"] = (newest_error["error"] if newest_error else None)
        info["last_error_ts"] = (newest_error["updated_at"] if newest_error else None)
        last = repo.last_frame()
        open_gap = db.one(
            "SELECT kind, start_ts FROM gaps WHERE end_ts IS NULL ORDER BY start_ts DESC LIMIT 1"
        )
        info.update(
            {
                "frames_today": repo.count_frames(str(info["today"])),
                "frames_total": repo.count_frames(),
                "images_today": images_today,
                "max_images_per_day": cfg.analysis.max_images_per_day,
                "last_frame_ts": last.ts if last else None,
                "last_frame_app": last.app if last else None,
                "current_gap": (
                    {"kind": open_gap["kind"], "since": open_gap["start_ts"]}
                    if open_gap
                    else None
                ),
                "batches": repo.batch_counts(),
                "probe": probe_summary(repo, cfg),
                "pending": int(
                    db.scalar(
                        "SELECT COUNT(*) FROM batches WHERE status IN ('pending','failed')",
                        default=0,
                    )
                ),
                "failed": int(
                    db.scalar("SELECT COUNT(*) FROM batches WHERE status = ?", (STATUS_FAILED,), default=0)
                ),
                "done": int(
                    db.scalar("SELECT COUNT(*) FROM batches WHERE status = ?", (STATUS_DONE,), default=0)
                ),
            }
        )
    finally:
        db.close()
    return info
