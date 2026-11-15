"""Command line entry point.

Deliberately boring: each command does one thing, prints human text, and exits
non-zero only on a real problem. The daemon is the only long-running command;
everything else is a one-shot that opens the database, does its job and quits.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__, clock, config, paths, runtime
from .analyzer.report import build_report
from .analyzer.report import render as render_report
from .config import Config

log = logging.getLogger("dayloom")


# ----------------------------------------------------------------------
# plumbing
# ----------------------------------------------------------------------
def _cfg(args: argparse.Namespace) -> Config:
    path = getattr(args, "config", None)
    return config.load(Path(path)) if path else config.load()


def _setup_logging(cfg: Config, verbose: bool) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # httpcore's DEBUG stream is thousands of lines per request and hides ours.
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    # The console is for the command's own output; INFO chatter (tier skipped,
    # http request lines) belongs in the log file, or it reads like an error.
    stream.setLevel(logging.DEBUG if verbose else logging.WARNING)
    root.addHandler(stream)
    try:
        file_handler = RotatingFileHandler(
            paths.logs_dir(cfg.home) / "dayloom.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        pass


def _repo(cfg: Config, *, readonly: bool = False):
    from .store.db import open_db
    from .store.repo import Repo

    if readonly and not cfg.db_path.exists():
        return None
    db = open_db(cfg.db_path, readonly=readonly)
    return Repo(db)


def _ago(ts: float | None) -> str:
    if not ts:
        return "从未"
    delta = max(0.0, clock.now_ts() - ts)
    if delta < 60:
        return f"{int(delta)} 秒前"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{delta / 3600:.1f} 小时前"
    return f"{delta / 86400:.1f} 天前"


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------
def cmd_start(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    _setup_logging(cfg, args.verbose)
    pid = runtime.running_pid(cfg.home)
    if pid:
        print(f"已经在运行了（pid {pid}）。要停止：结束该进程，或 Ctrl+C 那个终端。")
        return 1
    print(f"dayloom {__version__}")
    print(f"  数据目录  {cfg.home}")
    print(
        f"  采集      {cfg.capture.mode} · 每 {cfg.capture.probe_interval:g}s 探测"
        f" · 最快每 {cfg.capture.heartbeat:g}s 落一帧"
    )
    print(f"  视觉模型  {cfg.llm.model or '（未配置）'} @ {cfg.llm.base_url}")
    print("  另开终端：dayloom status / dayloom report。Ctrl+C 停止。")
    from .scheduler import run_daemon

    asyncio.run(run_daemon(cfg, Path(args.config) if args.config else None))
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    runtime.set_paused(cfg.home, True)
    print("已暂停采集。分析与已有数据不受影响；恢复：dayloom resume")
    if not runtime.running_pid(cfg.home):
        print("（提示：守护进程当前没有运行，暂停标记会保留到下次启动）")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    runtime.set_paused(cfg.home, False)
    print("已恢复采集。")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from .scheduler import status_snapshot

    cfg = _cfg(args)
    snap = status_snapshot(cfg)
    if args.json:
        print(json.dumps(snap, ensure_ascii=False, indent=2, default=str))
        return 0
    pid = snap.get("running_pid")
    gap = snap.get("current_gap")
    gap_labels = {"idle": "空闲中", "locked": "锁屏中", "protected": "黑名单窗口，已跳过"}
    if snap.get("paused"):
        capture_state = "已暂停"
    elif gap:
        capture_state = f"{gap_labels.get(gap['kind'], gap['kind'])}（{_ago(float(gap['since']))}开始，暂停截屏）"
    else:
        capture_state = "进行中" if pid else "—"
    print(f"数据目录  {snap['home']}")
    print(f"进程      {'运行中 pid ' + str(pid) if pid else '未运行'}")
    print(f"采集      {capture_state}")
    if snap.get("last_frame_ts"):
        print(f"最近一帧  {_ago(float(snap['last_frame_ts']))}（{snap.get('last_frame_app') or '?'}）")
    print(f"今日      {snap.get('today')} · {snap.get('frames_today', 0)} 帧")
    batches = snap.get("batches") or {}
    if batches:
        parts = [f"{k} {v}" for k, v in sorted(batches.items())]
        print(f"批次      {' · '.join(parts)}")
    elif snap.get("db_exists"):
        print("批次      还没有（数据攒够一个批次后才会出现）")
    budget = int(snap.get("max_images_per_day") or 0)
    used = int(snap.get("images_today") or 0)
    if budget:
        note = "（已用完，暂停分析）" if used >= budget else ""
        print(f"图片预算  今日 {used}/{budget} 张{note}")
    else:
        print(f"图片预算  不限（今日已用 {used} 张）")
    last_error = snap.get("last_error")
    if last_error and snap.get("pending"):
        when = clock.fmt_hm(float(snap["last_error_ts"])) if snap.get("last_error_ts") else ""
        print(f"分析      {when} 失败过：{' '.join(str(last_error).split())[:70]}")
    probe = snap.get("probe") or {}
    if probe:
        mark = "通过" if probe.get("ok") else "未通过"
        print(f"视觉校验  {mark}（{probe.get('reason', '')[:60]}）")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from .llm.session import VisionUnavailable, open_client

    cfg = _cfg(args)
    _setup_logging(cfg, args.verbose)
    if not cfg.db_path.exists():
        print("还没有数据库。先跑 `dayloom start` 采集一会儿，或 `dayloom analyze`。")
        return 1
    day = args.date or clock.day_key(clock.now_ts(), cfg.general.day_boundary_hour)
    repo = _repo(cfg)
    if repo is None:
        print("还没有数据库。先跑 `dayloom start` 采集一会儿，或 `dayloom analyze`。")
        return 1
    try:
        client = None
        if not args.no_llm and cfg.llm.enabled and cfg.llm.configured:
            try:
                client = open_client(cfg.llm, repo)
            except VisionUnavailable as exc:
                print(f"! 视觉模型不可用，这次只有统计数字：{exc.reason}", file=sys.stderr)
        report = build_report(repo, cfg, day, narrate=client is not None, client=client)
        if client is not None:
            client.close()
        if args.json:
            payload = {
                "day": report.day,
                "tracked_seconds": report.tracked_seconds,
                "categories": report.categories,
                "apps": report.apps,
                "unanalyzed_frames": report.unanalyzed_frames,
                "narrative": report.narrative,
                "highlights": report.highlights,
                "suggestions": report.suggestions,
                "model": report.model,
                "sessions": [
                    {
                        "start": s.start_ts,
                        "end": s.end_ts,
                        "app": s.app,
                        "category": s.category,
                        "title": s.title,
                        "duration": s.duration,
                    }
                    for s in report.sessions
                ],
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(render_report(report))
        return 0
    finally:
        repo.db.close()


def cmd_analyze(args: argparse.Namespace) -> int:
    from .analyzer.pipeline import analyze_with_escalation, build_clients
    from .llm.provider import LLMError
    from .llm.session import VisionUnavailable

    cfg = _cfg(args)
    _setup_logging(cfg, args.verbose)
    if not (cfg.llm.enabled and cfg.llm.configured):
        print("还没配置视觉模型：在 config.toml 的 [llm] 里填 base_url / model / api_key。")
        return 1
    repo = _repo(cfg)
    if repo is None:
        print("还没有数据库。")
        return 1
    day = args.date or clock.day_key(clock.now_ts(), cfg.general.day_boundary_hour)
    try:
        if args.redo:
            # Prompt or model changed: let the user re-run the day to see the
            # difference. Old activities are replaced only when a batch is
            # re-analyzed, so a crash halfway keeps the rest of the day intact.
            with repo.db.tx() as conn:
                reset = conn.execute(
                    "UPDATE batches SET status='pending', attempts=0 "
                    "WHERE day = ? AND status = 'done'",
                    (day,),
                ).rowcount
            print(f"已重置 {reset} 个已完成批次，将重新分析 {day}")
        if args.force:
            repo.db.execute(
                "UPDATE batches SET status='pending', attempts=0 WHERE status='failed'"
            )
        now = clock.now_ts()
        created = repo.ensure_batches(
            day=day,
            batch_seconds=cfg.batch_seconds,
            grace_seconds=cfg.analysis.grace_seconds,
            now=now,
            boundary_hour=cfg.general.day_boundary_hour,
        )
        if created:
            print(f"新建了 {created} 个批次")
        budget = cfg.analysis.max_images_per_day
        if budget and not args.ignore_budget:
            used = repo.images_used_on(day)
            if used >= budget:
                print(
                    f"今日图片预算已用完（{used}/{budget} 张），暂停分析。\n"
                    "  放宽：config.toml 里的 analysis.max_images_per_day\n"
                    "  强制：dayloom analyze --ignore-budget"
                )
                return 4
        skipped: list[str] = []
        try:
            clients = build_clients(cfg, repo, skipped=skipped)
        except VisionUnavailable as exc:
            print(f"✗ 视觉校验未通过：{exc.reason}")
            return 2
        for note in skipped:
            print(f"! 备用档跳过（不影响主档）：{' '.join(note.split())[:110]}")
        done = failed = 0
        while done + failed < args.limit:
            batch = repo.next_pending_batch(cfg.analysis.max_attempts)
            if batch is None:
                break
            if not repo.claim_batch(batch.id):
                continue
            try:
                result = analyze_with_escalation(
                    cfg=cfg, repo=repo, batch=batch, clients=clients
                )
                outcome = result.outcome
                done += 1
                tier_note = f" · {result.tier}" + ("（升级）" if result.escalated else "")
                print(
                    f"✓ 批次 {batch.id} {clock.fmt_hm(batch.start_ts)}–{clock.fmt_hm(batch.end_ts)}"
                    f" · {outcome.frames_sent} 图 → {outcome.activities} 段"
                    f" · tokens {outcome.usage.prompt_tokens}/{outcome.usage.completion_tokens}"
                    f"{tier_note}"
                )
                if outcome.quality is not None:
                    print(
                        f"   质量：置信度 {outcome.quality.mean_confidence:.2f}"
                        f" · unknown {outcome.quality.unknown_ratio:.0%}"
                        f" · 空泛 {outcome.quality.shallow_ratio:.0%}"
                    )
            except LLMError as exc:
                failed += 1
                repo.mark_failed(batch.id, str(exc))
                print(f"✗ 批次 {batch.id} 失败：{exc}")
        for client in clients.values():
            client.close()
        print(f"完成 {done} 个批次，失败 {failed} 个。")
        if done == 0 and failed == 0:
            print("没有待分析的批次（窗口还没关闭，或已经全部分析完）。")
        return 0
    finally:
        repo.db.close()


def cmd_week(args: argparse.Namespace) -> int:
    from .analyzer.weekly import build_week, render_week
    from .llm.session import VisionUnavailable, open_client

    cfg = _cfg(args)
    _setup_logging(cfg, args.verbose)
    if not cfg.db_path.exists():
        print("还没有数据库。先跑 `dayloom start` 采集几天。")
        return 1
    repo = _repo(cfg)
    if repo is None:
        print("还没有数据库。")
        return 1
    day = args.date or clock.day_key(clock.now_ts(), cfg.general.day_boundary_hour)
    try:
        client = None
        if not args.no_llm and cfg.llm.enabled and cfg.llm.configured:
            try:
                client = open_client(cfg.llm, repo)
            except VisionUnavailable as exc:
                print(f"! 视觉模型不可用，这次只有统计：{exc.reason}", file=sys.stderr)
        report = build_week(repo, cfg, day, narrate=client is not None, client=client)
        if client is not None:
            client.close()
        if args.json:
            payload = {
                "start_day": report.start_day,
                "end_day": report.end_day,
                "tracked_seconds": report.tracked_seconds,
                "categories": report.categories,
                "apps": report.apps,
                "daily": {d.day: d.tracked_seconds for d in report.daily},
                "narrative": report.narrative,
                "highlights": report.highlights,
                "suggestions": report.suggestions,
                "model": report.model,
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(render_week(report))
        return 0
    finally:
        repo.db.close()


def cmd_autostart(args: argparse.Namespace) -> int:
    from . import autostart

    cfg = _cfg(args)
    config_path = Path(args.config) if args.config else (cfg.home / "config.toml")
    if not config_path.exists():
        config_path = None  # let the task run with defaults

    if args.action == "enable":
        ok, message = autostart.enable(
            config_path, prefer_scheduler=args.scheduled, gui=not args.daemon
        )
    elif args.action == "disable":
        ok, message = autostart.disable()
    else:
        print(autostart.describe(config_path))
        return 0
    print(message)
    return 0 if ok else 1


def cmd_shortcut(args: argparse.Namespace) -> int:
    from . import shortcuts

    if args.action == "install":
        ok, message = shortcuts.install(desktop=args.desktop)
    elif args.action == "remove":
        ok, message = shortcuts.remove(desktop=args.desktop)
    else:
        print(shortcuts.describe())
        return 0
    print(message)
    return 0 if ok else 1


def cmd_reports(args: argparse.Namespace) -> int:
    """List (or print) the rolled-up days that actually exist."""
    from .analyzer.report import compute_report
    from .analyzer.report import render as render_report
    from .llm.prompts import CATEGORY_ZH

    cfg = _cfg(args)
    repo = _repo(cfg)
    if repo is None:
        print("还没有数据库。")
        return 1
    try:
        if args.show:
            report = compute_report(repo, cfg, args.show)
            if not report.narrative and not report.sessions:
                print(f"{args.show} 还没有日报。跑 `dayloom report --date {args.show}` 生成。")
                return 1
            print(render_report(report))
            return 0

        reports = repo.list_reports(args.days)
        if not reports:
            print("还没有日报。守护进程每天会自动生成，也可以 dayloom report 手动生成。")
            return 0
        print(f"{'日期':<12}{'记录':>8}  主要内容")
        for report in reports:
            tracked = clock.fmt_duration_short(float(report["tracked_seconds"] or 0))
            categories = sorted(
                (report["categories"] or {}).items(), key=lambda kv: kv[1], reverse=True
            )[:2]
            top = " · ".join(
                f"{CATEGORY_ZH.get(k, k)} {clock.fmt_duration_short(v)}" for k, v in categories
            )
            print(f"{report['day']:<12}{tracked:>8}  {top}")
        print("\n看某一天：dayloom reports --show 2026-10-22")
        return 0
    finally:
        repo.db.close()


def cmd_balance(args: argparse.Namespace) -> int:
    """Ask the endpoint how much credit is left (DeepSeek exposes this)."""
    from .llm.balance import fetch_balance

    cfg = _cfg(args)
    if not (cfg.llm.enabled and cfg.llm.configured):
        print("还没配置视觉模型。")
        return 1
    ok, text = fetch_balance(cfg.llm)
    mark = "✓" if ok else "✗"
    print(f"{mark} {cfg.llm.model} @ {cfg.llm.base_url}")
    print(f"  {text}")
    if not ok:
        print("  充值，或换模型：dayloom use-api / dayloom use-local")
        return 1
    return 0


def cmd_use_local(args: argparse.Namespace) -> int:
    """Point the primary tier at a local OpenAI-compatible server."""
    cfg = _cfg(args)
    model = args.model
    base_url = args.url
    updated = config.with_local_model(cfg, model, base_url)
    path = config.write_config(updated, Path(args.config) if args.config else None)

    print(f"主档  → {model} @ {base_url}")
    if updated.llm.tiers:
        for tier in updated.llm.tiers:
            print(f"升级档 → {tier.model} @ {tier.base_url}")
    print(f"写入  {path}")
    print("\n下一步：")
    print("  dayloom probe-llm      # 验证它真的能看图")
    print("  dayloom analyze        # 把待处理批次跑掉")
    print("\n守护进程会在 2 秒内自动重载配置。")
    return 0


def cmd_use_api(args: argparse.Namespace) -> int:
    """Point the primary tier at a cloud OpenAI-compatible endpoint."""
    cfg = _cfg(args)
    key = (args.key or os.environ.get("DAYLOOM_API_KEY", "")).strip()
    if not key:
        print("缺少 key：用 --key 传，或设置环境变量 DAYLOOM_API_KEY")
        return 1
    updated = config.with_api_model(
        cfg, model=args.model, base_url=args.url, api_key=key
    )
    path = config.write_config(updated, Path(args.config) if args.config else None)

    print(f"主档  → {args.model} @ {updated.llm.base_url}")
    for tier in updated.llm.tiers:
        print(f"升级档 → {tier.model} @ {tier.base_url}")
    print(f"写入  {path}")
    print("\n下一步：")
    print("  dayloom probe-llm    # 确认这个模型真的能看图")
    print("  dayloom analyze      # 把待处理批次跑掉")
    print("\n守护进程会在 2 秒内自动重载配置。")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from .gui.app import main as gui_main
    except ImportError:
        print("桌面客户端需要 PySide6：pip install \"dayloom[qt]\"")
        return 1
    argv: list[str] = []
    if args.config:
        argv += ["--config", str(args.config)]
    if args.day:
        argv += ["--day", args.day]
    return gui_main(argv)


def cmd_cost(args: argparse.Namespace) -> int:
    """Measured token ledger — README cost tables must come from here, not from guesses."""
    cfg = _cfg(args)
    if not cfg.db_path.exists():
        print("还没有数据库。先跑 `dayloom start` 采集一会儿，或 `dayloom analyze`。")
        return 1
    repo = _repo(cfg)
    if repo is None:
        print("还没有数据库。")
        return 1
    try:
        rows = repo.db.query(
            """
            SELECT day, COALESCE(model, '?') AS model, COUNT(*) AS batches,
                   COALESCE(SUM(tokens_in), 0) AS tin,
                   COALESCE(SUM(tokens_out), 0) AS tout,
                   COALESCE(SUM(images), 0) AS imgs
            FROM batches WHERE status = 'done'
            GROUP BY day, model ORDER BY day DESC, model LIMIT ?
            """,
            (args.days * 4,),
        )
        if not rows:
            print("还没有分析完成的批次，所以还没有 token 账本。")
            return 0
        models = {row["model"] for row in rows}
        multi = len(models) > 1
        header = f"{'日期':<12}"
        if multi:
            header += f"{'模型':<18}"
        header += f"{'批次':>5}{'图片':>7}{'输入 token':>14}{'输出 token':>14}"
        print(header)
        total_in = total_out = total_imgs = 0
        for row in rows:
            tin, tout = int(row["tin"]), int(row["tout"])
            imgs = int(row["imgs"])
            total_in += tin
            total_out += tout
            total_imgs += imgs
            line = f"{row['day']:<12}"
            if multi:
                line += f"{str(row['model'])[:17]:<18}"
            line += f"{int(row['batches']):>5}{imgs:>7}{tin:>14,}{tout:>14,}"
            print(line)
        print(
            f"{'合计':<12}"
            + (f"{'':<18}" if multi else "")
            + f"{'':>5}{total_imgs:>7}{total_in:>14,}{total_out:>14,}"
        )
        budget = cfg.analysis.max_images_per_day
        if budget:
            today = clock.day_key(clock.now_ts(), cfg.general.day_boundary_hour)
            used = repo.images_used_on(today)
            print(f"\n今日图片预算：{used}/{budget} 张" + ("（已用完，分析暂停）" if used >= budget else ""))
        else:
            print("\n今日图片预算：未设置（analysis.max_images_per_day = 0 表示不限）")
        if multi:
            print("升级档的批次也在里面：按模型分组的数字就是各档的真实花费。")
        else:
            print("这是实测用量，乘上你那一档的单价就是成本。")
        return 0
    finally:
        repo.db.close()


def cmd_capture_once(args: argparse.Namespace) -> int:
    from .capture import winapi
    from .capture.dedupe import dhash, is_flat
    from .capture.screen import Grabber, encode_webp, scale_to_max_edge

    cfg = _cfg(args)
    winapi.enable_dpi_awareness()
    grabber = Grabber()
    try:
        win = winapi.foreground_window()
        rect, label = grabber.choose_rect(cfg.capture.mode, win.rect if win else None)
        image = grabber.grab(rect)
    finally:
        grabber.close()
    scaled = scale_to_max_edge(image, cfg.capture.max_edge)
    data = encode_webp(scaled, cfg.capture.webp_quality)
    out = Path(args.out) if args.out else cfg.home / "capture-once.webp"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"应用      {win.app if win else '（无前台窗口）'}")
    print(f"标题      {(win.title if win else '')[:100]}")
    print(f"区域      {rect[2]}x{rect[3]} @ {rect[0]},{rect[1]}（{label}）")
    print(f"落盘      {out} · {len(data) / 1024:.1f} KB（缩放后 {scaled.width}x{scaled.height}）")
    print(f"dHash     {dhash(scaled):#018x} · {'画面是纯色/全黑' if is_flat(image) else '有内容'}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import has_failures, render, run_checks

    cfg = _cfg(args)
    checks = run_checks(cfg, check_llm=args.llm)
    print(render(checks))
    return 1 if has_failures(checks) else 0


def cmd_probe_llm(args: argparse.Namespace) -> int:
    from .llm.session import probe_and_cache

    cfg = _cfg(args)
    if not cfg.llm.configured:
        print("还没配置视觉模型。")
        return 1
    repo = _repo(cfg)
    chain = cfg.llm.tier_chain()
    if len(chain) > 1:
        print(f"共 {len(chain)} 档模型，逐档校验：\n")
    ok_all = True
    try:
        for name, llm in chain:
            label = "主档" if name == "primary" else f"升级档 {name}"
            print(f"[{label}] {llm.model} @ {llm.base_url}")
            client, result = probe_and_cache(llm, repo, name=name)
            try:
                print(f"  {result.summary()}")
                print(f"  期望颜色  {','.join(result.expected)}")
                print(f"  模型读出  {','.join(result.detected) or '（空）'}")
                for check, passed in result.checks.items():
                    print(f"  {'✓' if passed else '✗'} {check}")
            finally:
                client.close()
            ok_all = ok_all and result.ok
            print()
    finally:
        if repo is not None:
            repo.db.close()
    return 0 if ok_all else 1


def cmd_mock_vlm(args: argparse.Namespace) -> int:
    from .llm.mockvlm import MockVLM

    server = MockVLM(port=args.port).start()
    print(f"mock VLM 已启动：{server.base_url}")
    print("把这几行放进 config.toml 的 [llm]：")
    print(f'  base_url = "{server.base_url}"')
    print('  api_key  = "mock"')
    print('  model    = "mock-vlm"')
    print("Ctrl+C 退出。")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    if args.example:
        print(config.dump_example())
        return 0
    cfg = _cfg(args)
    print(json.dumps(cfg.model_dump(), ensure_ascii=False, indent=2))
    print(f"\n数据目录：{cfg.home}")
    return 0


# ----------------------------------------------------------------------
# parser
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=None, help="配置文件路径")
    common.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")

    parser = argparse.ArgumentParser(
        prog="dayloom",
        description="本地优先的屏幕活动日记：定时截屏 → 视觉模型理解 → 每日时间线。",
    )
    parser.add_argument("--version", action="version", version=f"dayloom {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start", parents=[common], help="常驻：采集 + 分析 + 日报")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("gui", parents=[common], help="打开桌面客户端（需要 PySide6）")
    p.add_argument("--day", default=None, help="直接打开某一天（YYYY-MM-DD）")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("balance", parents=[common], help="查模型账户余额（DeepSeek 支持）")
    p.set_defaults(func=cmd_balance)

    p = sub.add_parser("reports", parents=[common], help="列出已生成的日报")
    p.add_argument("--days", type=int, default=14, help="看最近几天")
    p.add_argument("--show", default=None, help="直接打印某天的日报（YYYY-MM-DD）")
    p.set_defaults(func=cmd_reports)

    p = sub.add_parser("use-local", parents=[common], help="把主档切成本地模型（旧模型留作升级档）")
    p.add_argument("--model", default="qwen2.5vl:3b", help="本地模型名")
    p.add_argument("--url", default="http://127.0.0.1:11434/v1", help="本地 OpenAI 兼容端点")
    p.set_defaults(func=cmd_use_local)

    p = sub.add_parser("use-api", parents=[common], help="把主档切成云端 OpenAI 兼容端点")
    p.add_argument("--url", required=True, help="例如 https://api.deepseek.com/v1")
    p.add_argument("--model", required=True, help="模型名")
    p.add_argument("--key", default="", help="API key（也可用环境变量 DAYLOOM_API_KEY）")
    p.set_defaults(func=cmd_use_api)

    p = sub.add_parser("autostart", parents=[common], help="登录时自动启动（默认打开界面）")
    p.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=("enable", "disable", "status"),
        help="enable / disable / status（默认 status）",
    )
    p.add_argument(
        "--scheduled",
        action="store_true",
        help="用任务计划程序代替启动文件夹（ONLOGON 需要管理员权限）",
    )
    p.add_argument(
        "--daemon",
        action="store_true",
        help="开机只跑后台采集，不打开界面（默认是打开界面）",
    )
    p.set_defaults(func=cmd_autostart)

    p = sub.add_parser("shortcut", parents=[common], help="开始菜单快捷方式")
    p.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=("install", "remove", "status"),
        help="install / remove / status（默认 status）",
    )
    p.add_argument("--desktop", action="store_true", help="同时在桌面创建（install/remove）")
    p.set_defaults(func=cmd_shortcut)

    p = sub.add_parser("pause", parents=[common], help="暂停采集")
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("resume", parents=[common], help="恢复采集")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("status", parents=[common], help="看当前状态")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("report", parents=[common], help="某一天的时间线 / 日报")
    p.add_argument("--date", default=None, help="YYYY-MM-DD，默认今天")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.add_argument("--no-llm", action="store_true", help="只要统计，不调用模型")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("week", parents=[common], help="本周（周一到周日）汇总")
    p.add_argument("--date", default=None, help="这一周里的任意一天，默认今天")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.add_argument("--no-llm", action="store_true", help="只要统计，不调用模型")
    p.set_defaults(func=cmd_week)

    p = sub.add_parser("analyze", parents=[common], help="立刻分析待处理的批次")
    p.add_argument("--date", default=None, help="YYYY-MM-DD，默认今天")
    p.add_argument("--limit", type=int, default=10, help="本次最多处理几个批次")
    p.add_argument("--force", action="store_true", help="把失败的批次重置为待处理")
    p.add_argument(
        "--redo",
        action="store_true",
        help="把 --date 当天已完成的批次重置为待处理，重新分析（换模型/改提示词后用）",
    )
    p.add_argument(
        "--ignore-budget",
        action="store_true",
        help="即使今日图片预算已用完也继续（临时多花一点）",
    )
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("capture-once", parents=[common], help="立刻截一张，验证采集链路")
    p.add_argument("--out", default=None, help="保存路径")
    p.set_defaults(func=cmd_capture_once)

    p = sub.add_parser("cost", parents=[common], help="Token 实测账本（按天）")
    p.add_argument("--days", type=int, default=14, help="看最近几天")
    p.set_defaults(func=cmd_cost)

    p = sub.add_parser("doctor", parents=[common], help="自检：DPI / 截屏 / 目录 / 数据库 / 模型")
    p.add_argument("--llm", action="store_true", help="顺便校验视觉模型")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("probe-llm", parents=[common], help="用挑战图验证模型能否读图")
    p.set_defaults(func=cmd_probe_llm)

    p = sub.add_parser("mock-vlm", parents=[common], help="启动内置的假视觉模型（无需 key）")
    p.add_argument("--port", type=int, default=8677)
    p.set_defaults(func=cmd_mock_vlm)

    p = sub.add_parser("config", parents=[common], help="打印生效配置或示例配置")
    p.add_argument("--example", action="store_true", help="打印带注释的示例配置")
    p.set_defaults(func=cmd_config)

    return parser


def _welcome() -> str:
    return "\n".join(
        [
            f"dayloom {__version__} — 本地屏幕活动日记",
            "",
            "这是命令行版本。想要图形界面：",
            "  · 双击同目录的 dayloom-gui.exe（开始菜单里的快捷方式也是它）",
            "  · 或者在这里运行：dayloom gui",
            "",
            "常用命令：",
            "  dayloom start          常驻采集 + 分析 + 日报",
            "  dayloom status         看现在跑没跑",
            "  dayloom report         今天的时间线",
            "  dayloom week           本周汇总",
            "  dayloom doctor         自检（DPI / 截屏 / 目录 / 模型）",
            "  dayloom --help         全部命令",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    # Pipes and files get UTF-8; a real console keeps its own code page, because
    # forcing UTF-8 onto a cp936 console produces mojibake for Chinese users.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8")

    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        # Double-clicking the console build used to dump an argparse error and
        # close the window instantly. Explain the two executables instead.
        print(_welcome())
        if sys.stdin.isatty():
            with contextlib.suppress(EOFError, KeyboardInterrupt):
                input("\n按回车关闭…")
        return 0

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
