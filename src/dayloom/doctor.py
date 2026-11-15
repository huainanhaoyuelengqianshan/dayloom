"""``dayloom doctor`` — the answer to "why is nothing showing up?".

A Windows-first, screen-capturing, local-first tool fails in a handful of
predictable ways: no DPI awareness (wrong rectangle or blurry frames), the
capture API returning black because of a remote session or DRM, an antivirus
quarantine, a locked-down folder, or a model endpoint that silently drops
images. Each of those is checked here and explained in one line.
"""

from __future__ import annotations

import logging
import shutil
import sys
from dataclasses import dataclass

from .config import Config

log = logging.getLogger(__name__)

LEVELS = {"ok": "✓", "warn": "!", "fail": "✗", "info": "·"}


@dataclass(slots=True)
class Check:
    name: str
    status: str
    detail: str
    hint: str = ""


def _python(cfg: Config) -> Check:
    version = ".".join(str(x) for x in sys.version_info[:3])
    ok = sys.version_info >= (3, 11)
    return Check("Python", "ok" if ok else "fail", version, "" if ok else "需要 3.11 或更高版本")


def _deps() -> Check:
    missing: list[str] = []
    for module in ("mss", "PIL", "httpx", "pydantic"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        return Check("依赖", "fail", "缺少 " + ", ".join(missing), "pip install dayloom")
    return Check("依赖", "ok", "mss / pillow / httpx / pydantic")


def _capture(cfg: Config) -> list[Check]:
    from .capture import winapi
    from .capture.dedupe import is_flat
    from .capture.screen import Grabber, display_report

    if not winapi.WINDOWS:
        return [Check("平台", "fail", sys.platform, "采集目前只支持 Windows")]

    checks: list[Check] = []
    dpi = winapi.enable_dpi_awareness()
    checks.append(
        Check(
            "DPI 感知",
            "ok" if dpi.startswith("per-monitor") else "warn",
            dpi,
            "" if dpi.startswith("per-monitor") else "坐标可能被虚拟化，截图会错位",
        )
    )

    report = display_report()
    monitors = report.get("monitors") or []
    metrics = report.get("metrics") or {}
    if report.get("error"):
        checks.append(Check("显示器", "fail", str(report["error"]), "mss 无法枚举显示器"))
        return checks
    checks.append(
        Check(
            "显示器",
            "ok" if monitors else "warn",
            f"{len(monitors)} 块，虚拟桌面 {metrics.get('width')}x{metrics.get('height')}",
            "" if monitors else "没有检测到显示器",
        )
    )

    try:
        from .capture.taskbar import find_taskbars

        bars = find_taskbars([tuple(item["box"]) for item in monitors])
        detail = (
            "、".join(f"{bar.edge}" for bar in bars) if bars else "没找到（自定义任务栏/程序坞？）"
        )
        checks.append(
            Check(
                "任务栏",
                "ok" if bars else "warn",
                f"{len(bars)} 块：{detail}" if bars else detail,
                ""
                if bars
                else "找不到任务栏时拿不到“徽标”证据，分析只能依赖前台窗口元数据",
            )
        )
    except Exception as exc:  # pragma: no cover - depends on the shell
        checks.append(Check("任务栏", "warn", f"{type(exc).__name__}: {exc}"))

    grabber = Grabber()
    try:
        win = winapi.foreground_window()
        rect, label = grabber.choose_rect(cfg.capture.mode, win.rect if win else None)
        image = grabber.grab(rect)
        from PIL import ImageStat

        small = image.convert("L").resize((64, 64))
        stat = ImageStat.Stat(small)
        mean, stddev = stat.mean[0], stat.stddev[0]
        flat = is_flat(image)
        detail = (
            f"{rect[2]}x{rect[3]} @ {label}，亮度 {mean:.0f}，方差 {stddev:.1f}"
            + (f"，前台 {win.app}" if win else "，没有前台窗口")
        )
        checks.append(
            Check(
                "截屏",
                "warn" if flat else "ok",
                detail,
                "画面几乎全黑/全白：可能是远程桌面、受保护内容或采集被拦截"
                if flat
                else "",
            )
        )
    except Exception as exc:
        checks.append(Check("截屏", "fail", f"{type(exc).__name__}: {exc}", "mss 抓屏失败"))
    finally:
        grabber.close()

    try:
        idle = winapi.idle_seconds()
        locked = winapi.is_locked()
        checks.append(
            Check(
                "空闲/锁屏",
                "ok",
                f"空闲 {idle:.0f}s，{'已锁定' if locked else '未锁定'}",
                "空闲判定不对会让截图一直停着" if locked else "",
            )
        )
    except Exception as exc:
        checks.append(Check("空闲/锁屏", "fail", f"{type(exc).__name__}: {exc}"))
    return checks


def _storage(cfg: Config) -> list[Check]:
    checks: list[Check] = []
    probe = cfg.home / ".doctor"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(Check("数据目录", "ok", str(cfg.home)))
    except OSError as exc:
        checks.append(Check("数据目录", "fail", f"{exc}", "目录不可写；换个 data_dir 或修权限"))

    try:
        usage = shutil.disk_usage(cfg.home)
        free_gb = usage.free / (1024**3)
        checks.append(
            Check(
                "磁盘",
                "ok" if free_gb >= 1.0 else "warn",
                f"剩余 {free_gb:.1f} GB",
                "" if free_gb >= 1.0 else "空间不足，截图保留策略会失效",
            )
        )
    except OSError as exc:
        checks.append(Check("磁盘", "warn", str(exc)))

    if cfg.db_path.exists():
        from .store.db import open_db

        db = open_db(cfg.db_path, readonly=True)
        try:
            frames = int(db.scalar("SELECT COUNT(*) FROM frames", default=0) or 0)
            checks.append(
                Check("数据库", "ok", f"schema v{db.schema_version()}，{frames} 帧")
            )
        finally:
            db.close()
    else:
        checks.append(Check("数据库", "info", "还没有数据库（第一次 start 时创建）"))
    return checks


def _llm(cfg: Config) -> Check:
    from .llm.session import VisionUnavailable, open_client
    from .store.db import open_db
    from .store.repo import Repo

    if not cfg.llm.configured:
        return Check("视觉模型", "warn", "未配置", "在 config.toml 的 [llm] 里填 base_url / model")
    db = open_db(cfg.db_path)
    try:
        client = open_client(cfg.llm, Repo(db))
    except VisionUnavailable as exc:
        return Check("视觉模型", "fail", exc.reason, "换一个能看图的模型，或检查 base_url / key")
    except Exception as exc:
        return Check("视觉模型", "fail", f"{type(exc).__name__}: {exc}")
    client.close()
    return Check("视觉模型", "ok", f"{cfg.llm.model} 能读图")


def run_checks(cfg: Config, *, check_llm: bool = False) -> list[Check]:
    checks: list[Check] = [_python(cfg), _deps()]
    checks.extend(_capture(cfg))
    checks.extend(_storage(cfg))
    if check_llm:
        checks.append(_llm(cfg))
    return checks


def render(checks: list[Check]) -> str:
    lines: list[str] = []
    for check in checks:
        mark = LEVELS.get(check.status, "·")
        lines.append(f"{mark} {check.name:<10} {check.detail}")
        if check.hint:
            lines.append(f"  {'':<10} ↳ {check.hint}")
    worst = "ok"
    if any(c.status == "fail" for c in checks):
        worst = "fail"
    elif any(c.status == "warn" for c in checks):
        worst = "warn"
    lines.append("")
    lines.append({"ok": "一切正常。", "warn": "有警告，但能跑。", "fail": "有硬性问题，先修上面标 ✗ 的。"}[worst])
    return "\n".join(lines)


def has_failures(checks: list[Check]) -> bool:
    return any(c.status == "fail" for c in checks)
