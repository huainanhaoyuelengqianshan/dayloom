"""Main window and application bootstrap."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QDate, QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QFont, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDateEdit,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QSystemTrayIcon,
    QTextBrowser,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, clock, runtime
from .. import config as config_mod
from ..config import Config
from . import theme
from .model import DayView, SessionView, load_day, load_status
from .tray import Tray, make_icon
from .widgets import DaySidebar, EmptyState, TimelineRow, close_buttons

# ----------------------------------------------------------------------
# background jobs (subprocess / probe / doctor never block the UI thread)
# ----------------------------------------------------------------------
_LOCK_HANDLE: object | None = None


class _JobSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Job(QRunnable):
    def __init__(self, fn) -> None:
        super().__init__()
        self._fn = fn
        self.signals = _JobSignals()
        self.setAutoDelete(False)

    def run(self) -> None:  # noqa: D102
        try:
            self.signals.done.emit(self._fn())
        except Exception as exc:  # pragma: no cover - surfaced in a dialog
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}")


class MainWindow(QMainWindow):
    def __init__(
        self,
        cfg: Config,
        *,
        day: str | None = None,
        config_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self._config_path = config_path
        self._today = clock.day_key(clock.now_ts(), cfg.general.day_boundary_hour)
        self._day = day or self._today
        self._signature: tuple | None = None
        self._jobs: list[_Job] = []
        self._suppress_date = False
        self._auto_started = False
        self._last_spawn = 0.0

        self.setWindowTitle("dayloom — 屏幕活动日记")
        self.setWindowIcon(make_icon())
        self._fit_to_screen()
        self._build_ui()
        self._sync_date_edit()
        # Keep the date field from opening with a blue "selected section" pill.
        self.date_edit.setSelectedSection(QDateTimeEdit.Section.NoSection)
        self.scroll.setFocus()

        self._quit_requested = False
        self._tray_hint_shown = False
        self._summary_checked_for: str | None = None
        self._pending_summary_day: str | None = None
        self._state_path = self.cfg.home / ".gui-state.json"
        self.tray: Tray | None = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = Tray(self)
            self.tray.show()

        self.refresh(force=True)

        self._timer = QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

        # Watchdog: the daemon must not be allowed to stay dead because it
        # crashed at logon or was killed after an update.
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(60000)
        self._watchdog.timeout.connect(self.ensure_daemon_running)
        self._watchdog.start()

    # -- construction ----------------------------------------------------
    def _fit_to_screen(self) -> None:
        """Open at a size that actually fits this display.

        A 1280x820 window on a 1229x768 logical desktop (250% scaling) gets
        clamped by the window manager into a cramped box; asking for the
        available geometry up front avoids that.
        """
        from PySide6.QtGui import QGuiApplication

        self.setMinimumSize(860, 540)
        width, height = 1180, 760
        screen = QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        if available is not None:
            width = min(width, max(860, available.width() - 80))
            height = min(height, max(540, available.height() - 80))
        self.resize(width, height)
        if available is not None:
            self.move(
                available.x() + (available.width() - width) // 2,
                available.y() + (available.height() - height) // 2,
            )

    def _build_ui(self) -> None:
        self._build_menu()
        self._build_toolbar()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(1)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(QWidget())

        self.sidebar = DaySidebar()
        splitter.addWidget(self.scroll)
        splitter.addWidget(self.sidebar)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([880, 310])
        self.setCentralWidget(splitter)

        self.status_daemon = QLabel("…")
        self.status_data = QLabel("")
        self.statusBar().addWidget(self.status_daemon)
        self.statusBar().addPermanentWidget(self.status_data)
        self.statusBar().setSizeGripEnabled(False)

    def _build_menu(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu("文件")
        open_dir = QAction("打开数据目录", self)
        open_dir.triggered.connect(lambda: self.open_path(self.cfg.home))
        file_menu.addAction(open_dir)
        open_cfg = QAction("打开配置文件", self)
        open_cfg.triggered.connect(self._open_config)
        file_menu.addAction(open_cfg)
        file_menu.addSeparator()
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.quit_app)
        file_menu.addAction(quit_action)

        tools = bar.addMenu("工具")
        settings_action = QAction("设置…", self)
        settings_action.triggered.connect(self.open_settings_dialog)
        tools.addAction(settings_action)
        doctor = QAction("自检…", self)
        doctor.triggered.connect(self._run_doctor)
        tools.addAction(doctor)
        model = QAction("视觉模型…", self)
        model.triggered.connect(self._show_model_dialog)
        tools.addAction(model)
        analyze = QAction("分析现在", self)
        analyze.triggered.connect(lambda: self.analyze_now(silent=False))
        tools.addAction(analyze)
        week = QAction("本周小结…", self)
        week.triggered.connect(self.show_week_dialog)
        tools.addAction(week)
        reports = QAction("日报…", self)
        reports.triggered.connect(self.show_reports_dialog)
        tools.addAction(reports)
        tools.addSeparator()
        self.autostart_action = QAction("开机自启", self)
        self.autostart_action.setCheckable(True)
        self.autostart_action.triggered.connect(self._toggle_autostart)
        tools.addAction(self.autostart_action)

        shortcut_menu = tools.addMenu("快捷方式")
        create_shortcut = QAction("创建开始菜单快捷方式", self)
        create_shortcut.triggered.connect(self._install_shortcut)
        shortcut_menu.addAction(create_shortcut)
        remove_shortcut = QAction("移除快捷方式", self)
        remove_shortcut.triggered.connect(self._remove_shortcut)
        shortcut_menu.addAction(remove_shortcut)
        # Query the scheduler only when the menu opens, not on every refresh.
        tools.aboutToShow.connect(self._sync_autostart_action)

        help_menu = bar.addMenu("帮助")
        about = QAction("关于 dayloom", self)
        about.triggered.connect(self._show_about)
        help_menu.addAction(about)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("导航")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(16, 16))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(toolbar)

        self.prev_action = QAction("‹ 前一天", self)
        self.prev_action.triggered.connect(lambda: self._shift_day(-1))
        toolbar.addAction(self.prev_action)

        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setToolTip("选择要查看的逻辑日（日界可在 config.toml 里改）")
        self.date_edit.dateChanged.connect(self._on_date_changed)
        toolbar.addWidget(self.date_edit)

        self.next_action = QAction("后一天 ›", self)
        self.next_action.triggered.connect(lambda: self._shift_day(1))
        toolbar.addAction(self.next_action)

        today_action = QAction("今天", self)
        today_action.triggered.connect(self._goto_today)
        toolbar.addAction(today_action)

        toolbar.addSeparator()

        refresh_action = QAction("刷新", self)
        refresh_action.triggered.connect(lambda: self.refresh(force=True))
        toolbar.addAction(refresh_action)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        self.pause_button = QPushButton("暂停采集")
        self.pause_button.clicked.connect(self.toggle_pause)
        toolbar.addWidget(self.pause_button)

        self.analyze_button = QPushButton("分析现在")
        self.analyze_button.setObjectName("Primary")
        self.analyze_button.clicked.connect(lambda: self.analyze_now(silent=False))
        toolbar.addWidget(self.analyze_button)

    # -- data ------------------------------------------------------------
    def refresh(self, force: bool = False) -> None:
        # The window may stay open across the day boundary (04:00 by default).
        today = clock.day_key(clock.now_ts(), self.cfg.general.day_boundary_hour)
        if today != self._today:
            self._today = today
            self._summary_checked_for = None
        try:
            status = load_status(self.cfg)
            view = load_day(self.cfg, self._day)
        except Exception as exc:  # pragma: no cover - defensive
            self.statusBar().showMessage(f"读取失败：{exc}", 6000)
            return
        self._update_status(status)
        self._update_toolbar(status)
        self.sidebar.set_view(view)
        if force or view.signature != self._signature:
            self._signature = view.signature
            self._render_timeline(view)
        self._update_tray(status, view)
        self._maybe_notify_summary()

    def _render_timeline(self, view: DayView) -> None:
        scrollbar = self.scroll.verticalScrollBar()
        position = scrollbar.value()

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(8)

        if not view.has_db:
            layout.addWidget(
                EmptyState(
                    "还没有数据",
                    "先运行 dayloom start 采集一会儿；第一次分析需要先配置一个能看图的模型。",
                )
            )
        elif not view.sessions:
            hint = (
                f"这一天有 {view.frame_count} 帧，但还没有活动段。"
                "点右上角「分析现在」，或等守护进程按批次自动分析。"
                if view.frame_count
                else "这一天没有采集到画面。可能没开机，或者被空闲/锁屏/黑名单跳过了。"
            )
            layout.addWidget(EmptyState("这一天还没有时间线", hint))
        else:
            for session in view.sessions:
                row = TimelineRow(session)
                row.clicked.connect(self._show_detail)
                layout.addWidget(row)
        layout.addStretch(1)

        old = self.scroll.takeWidget()
        self.scroll.setWidget(container)
        if old is not None:
            old.deleteLater()
        scrollbar.setValue(min(position, scrollbar.maximum()))

    def _update_status(self, status: dict) -> None:
        pid = status.get("running_pid")
        paused = status.get("paused")
        gap = status.get("current_gap")
        if paused:
            text, colour = "⏸ 采集已暂停", theme.WARN
        elif pid:
            text, colour = f"● 守护进程运行中 · pid {pid}", theme.OK
        else:
            text, colour = "○ 守护进程未运行", theme.MUTED
        if gap and not paused:
            kind = {"idle": "空闲", "locked": "锁屏", "protected": "黑名单窗口"}.get(gap["kind"], gap["kind"])
            text += f" · {kind}中"
        self.status_daemon.setText(f"<span style='color:{colour}'>{text}</span>")

        bits = []
        if status.get("last_frame_ts"):
            age = clock.now_ts() - float(status["last_frame_ts"])
            if age < 0:
                bits.append("最近一帧 刚刚")
            elif age < 90:
                bits.append(f"最近一帧 {int(age)} 秒前")
            else:
                bits.append(f"最近一帧 {clock.fmt_duration_short(age)}前")
        else:
            bits.append("最近一帧 从未")
        bits.append(f"今日 {status.get('frames_today', 0)} 帧")
        batches = status.get("batches") or {}
        if batches:
            bits.append("批次 " + " · ".join(f"{k} {v}" for k, v in sorted(batches.items())))
        probe = status.get("probe") or {}
        if probe:
            bits.append("模型 ✓" if probe.get("ok") else "模型 ✗")
        self.status_data.setText("   ·   ".join(bits))
        self.sidebar.set_notice(self._notice_for(status))

    def _notice_for(self, status: dict) -> str:
        """One line in the sidebar when something needs the user's attention."""
        if status.get("paused"):
            return ""
        if not status.get("running_pid"):
            return "采集没有在运行 —— 界面会尝试自动拉起，仍然不行就看 logs/dayloom.log"
        budget = int(status.get("max_images_per_day") or 0)
        used = int(status.get("images_today") or 0)
        if budget and used >= budget:
            return f"今日图片预算已用完（{used}/{budget}），分析暂停，明天自动继续"
        pending = int(status.get("pending") or 0)
        last_error = status.get("last_error")
        if pending and last_error:
            text = " ".join(str(last_error).split())[:90]
            return f"有 {pending} 个批次没分析成功：{text}"
        return ""

    def _update_toolbar(self, status: dict) -> None:
        paused = bool(status.get("paused"))
        self.pause_button.setText("恢复采集" if paused else "暂停采集")
        self.pause_button.setToolTip(
            "采集已暂停，点击恢复" if paused else "暂停截屏（守护进程不退出）"
        )

    def _update_tray(self, status: dict, view: DayView) -> None:
        if self.tray is None:
            return
        tracked = view.tracked_seconds
        if self._day != self._today:
            try:
                tracked = load_day(self.cfg, self._today).tracked_seconds
            except Exception:  # pragma: no cover - defensive
                tracked = 0.0
        gap = status.get("current_gap") or {}
        self.tray.update_state(
            tracked_seconds=tracked,
            frames_today=int(status.get("frames_today") or 0),
            paused=bool(status.get("paused")),
            gap=gap.get("kind"),
            daemon_running=bool(status.get("running_pid")),
        )

    def show_week_dialog(self) -> None:
        from .model import load_week
        from .week import show_week

        try:
            report = load_week(self.cfg, self._day)
        except Exception as exc:  # pragma: no cover - defensive
            QMessageBox.warning(self, "读取失败", str(exc))
            return
        if report is None:
            QMessageBox.information(self, "还没有数据", "先跑 `dayloom start` 采集几天。")
            return
        show_week(self, report)

    def open_settings_dialog(self) -> None:
        from .settings import open_settings

        if open_settings(self, self.cfg, self._config_path):
            # Re-read from disk: the dialog writes the whole file, and this keeps
            # the window's copy (idle thresholds, blacklist) identical to it.
            self.cfg = config_mod.load(self._config_path) if self._config_path else config_mod.load()
            self._signature = None
            self.refresh(force=True)

    # -- daily summary notification --------------------------------------
    def _read_state(self) -> dict:
        import json

        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_state(self, state: dict) -> None:
        import contextlib
        import json

        with contextlib.suppress(OSError):
            self._state_path.write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8"
            )

    def _maybe_notify_summary(self) -> None:
        """One balloon per logical day: 'yesterday you recorded 6h12m'."""
        if self.tray is None or self._summary_checked_for == self._today:
            return
        self._summary_checked_for = self._today
        state = self._read_state()
        if state.get("last_summary") == self._today:
            return
        yesterday = clock.shift_day(self._today, -1)
        try:
            view = load_day(self.cfg, yesterday)
        except Exception:  # pragma: no cover - defensive
            return
        if not view.sessions:
            return
        self._write_state({**state, "last_summary": self._today})
        top = sorted(view.categories.items(), key=lambda kv: -kv[1])[:3]
        breakdown = " · ".join(
            f"{theme.label(category)} {seconds / max(1.0, sum(view.categories.values())) * 100:.0f}%"
            for category, seconds in top
        )
        self._pending_summary_day = yesterday
        self.tray.showMessage(
            f"昨天记录了 {clock.fmt_duration_short(view.tracked_seconds)}",
            f"{breakdown}\n点这里看 {yesterday} 的时间线",
            QSystemTrayIcon.MessageIcon.Information,
            8000,
        )

    def on_tray_message_clicked(self) -> None:
        if self._pending_summary_day:
            self._day = self._pending_summary_day
            self._pending_summary_day = None
            self._sync_date_edit()
        self.show_from_tray()

    # -- shortcuts -------------------------------------------------------
    def _install_shortcut(self) -> None:
        from .. import shortcuts

        ok, message = shortcuts.install()
        if not ok:
            QMessageBox.warning(self, "快捷方式", message)
            return
        self.statusBar().showMessage(message, 6000)
        if self.tray is not None:
            self.tray.showMessage(
                "快捷方式", message, QSystemTrayIcon.MessageIcon.Information, 3000
            )

    def _remove_shortcut(self) -> None:
        from .. import shortcuts

        ok, message = shortcuts.remove(desktop=True)
        self.statusBar().showMessage(message, 6000)
        if self.tray is not None:
            self.tray.showMessage(
                "快捷方式", message, QSystemTrayIcon.MessageIcon.Information, 3000
            )

    # -- window lifecycle ------------------------------------------------
    def show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.refresh()

    def quit_app(self) -> None:
        """Really exit — used by the menu and the tray menu."""
        self._quit_requested = True
        self._timer.stop()
        if self.tray is not None:
            self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self.tray is not None and not self._quit_requested:
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage(
                    "dayloom 还在记录",
                    "窗口已收进托盘。要真正退出，请用托盘菜单里的「退出 dayloom」。",
                    QSystemTrayIcon.MessageIcon.Information,
                    5000,
                )
            return
        self._timer.stop()
        super().closeEvent(event)

    # -- autostart -------------------------------------------------------
    def _sync_autostart_action(self) -> None:
        from ..autostart import status as autostart_status

        self.autostart_action.setChecked(autostart_status().registered)

    def _toggle_autostart(self, checked: bool) -> None:
        from ..autostart import disable, enable

        ok, message = enable(self._config_path) if checked else disable()
        if not ok:
            self.autostart_action.setChecked(not checked)
            QMessageBox.warning(self, "开机自启", message)
            return
        self.statusBar().showMessage(message, 6000)
        if self.tray is not None:
            self.tray.showMessage(
                "开机自启", message, QSystemTrayIcon.MessageIcon.Information, 3000
            )

    # -- actions ---------------------------------------------------------
    def _sync_date_edit(self) -> None:
        self._suppress_date = True
        year, month, day = (int(x) for x in self._day.split("-"))
        self.date_edit.setDate(QDate(year, month, day))
        self._suppress_date = False

    def _on_date_changed(self, value: QDate) -> None:
        if self._suppress_date:
            return
        self._day = f"{value.year():04d}-{value.month():02d}-{value.day():02d}"
        self.refresh(force=True)

    def _shift_day(self, delta: int) -> None:
        self._day = clock.shift_day(self._day, delta)
        self._sync_date_edit()
        self.refresh(force=True)

    def _goto_today(self) -> None:
        self._day = clock.day_key(clock.now_ts(), self.cfg.general.day_boundary_hour)
        self._sync_date_edit()
        self.refresh(force=True)

    def _set_day(self, day: str) -> None:
        """Jump to a specific day (used by the reports dialog)."""
        self._day = day
        self._sync_date_edit()
        self.refresh(force=True)

    def show_reports_dialog(self) -> None:
        from .reports import show_reports

        show_reports(self, self.cfg)

    def toggle_pause(self) -> None:
        runtime.set_paused(self.cfg.home, not runtime.is_paused(self.cfg.home))
        self.refresh(force=True)

    def start_daemon(self) -> None:
        """Launch ``dayloom start`` detached (pythonw in dev, console exe when frozen)."""
        from .. import autostart

        if runtime.running_pid(self.cfg.home):
            self.statusBar().showMessage("守护进程已经在运行", 4000)
            return
        config = self._config_path if self._config_path and self._config_path.exists() else None
        if not autostart.spawn_daemon(config):
            QMessageBox.warning(self, "启动失败", "无法启动守护进程，请查看日志。")
            return
        self.statusBar().showMessage("已启动采集，几秒后会自动刷新", 5000)
        QTimer.singleShot(2500, lambda: self.refresh(force=True))

    def ensure_daemon_running(self) -> None:
        """Start capture if the config asks for it and nothing is running.

        Called once shortly after launch and every minute by the watchdog, so a
        daemon that dies (or never started at logon) comes back by itself.
        """
        from .. import autostart

        if not self.cfg.capture.enabled:
            return
        if runtime.is_paused(self.cfg.home) or runtime.running_pid(self.cfg.home):
            return
        # Give a freshly spawned process time to write its pid file.
        if time.monotonic() - self._last_spawn < 45:
            return
        config = self._config_path if self._config_path and self._config_path.exists() else None
        self._last_spawn = time.monotonic()
        if autostart.spawn_daemon(config):
            self._auto_started = True
            self.statusBar().showMessage("采集守护进程未运行，已自动启动", 5000)
            QTimer.singleShot(2500, lambda: self.refresh(force=True))
        else:
            self.statusBar().showMessage("无法启动采集守护进程，详见日志", 8000)

    def open_path(self, path: Path) -> None:
        if not path.exists():
            QMessageBox.information(self, "还没有", f"{path} 还不存在。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_config(self) -> None:
        path = self._config_path or (self.cfg.home / "config.toml")
        if not path.exists():
            example = config_mod.dump_example()
            path.write_text(example, encoding="utf-8")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _run_job(self, fn, on_done) -> None:
        job = _Job(fn)
        job.signals.done.connect(on_done)
        job.signals.failed.connect(self._job_failed)
        self._jobs.append(job)
        QThreadPool.globalInstance().start(job)

    def _job_failed(self, message: str) -> None:
        self.statusBar().showMessage("任务失败", 4000)
        QMessageBox.warning(self, "出错", message)

    def analyze_now(self, silent: bool = False) -> None:
        if not self.analyze_button.isEnabled():
            return
        self.analyze_button.setEnabled(False)
        self.statusBar().showMessage("正在分析待处理批次…")

        def work() -> str:
            from ..autostart import cli_command

            config = (
                self._config_path if self._config_path and self._config_path.exists() else None
            )
            args = cli_command("analyze", config_path=config)
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
                # Console build: without this the child would flash a black window.
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
            return (proc.stdout or "") + (proc.stderr or "")

        def finish(text: str) -> None:
            self.analyze_button.setEnabled(True)
            self.statusBar().showMessage("分析完成", 4000)
            cleaned = text.strip() or "（没有输出）"
            if silent:
                # Triggered from the tray: a modal dialog while the window is
                # hidden would be a trap, so answer with a tray notification.
                if self.tray is not None:
                    last_line = cleaned.splitlines()[-1][:90]
                    self.tray.showMessage(
                        "分析完成",
                        last_line,
                        QSystemTrayIcon.MessageIcon.Information,
                        4000,
                    )
            else:
                self._show_text("分析结果", cleaned)
            self.refresh(force=True)

        self._run_job(work, finish)

    def _run_doctor(self) -> None:
        from ..doctor import render, run_checks

        self._run_job(
            lambda: render(run_checks(self.cfg, check_llm=False)),
            lambda text: self._show_text("自检", text),
        )

    def _show_model_dialog(self) -> None:
        from ..llm.provider import VisionClient

        dialog = QDialog(self)
        dialog.setWindowTitle("视觉模型")
        dialog.resize(520, 260)
        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)

        info = QLabel(
            f"<b>{self.cfg.llm.model or '（未配置）'}</b><br>"
            f"<span style='color:{theme.MUTED}'>{self.cfg.llm.base_url}</span>"
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(info)

        hint = QLabel(
            "校验时会生成一张随机色块图，只有真的能看图的模型才能念对序列。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.MUTED};")
        layout.addWidget(hint)

        result = QLabel("")
        result.setWordWrap(True)
        layout.addWidget(result)

        buttons = close_buttons()
        test = QPushButton("测试连接")
        test.setObjectName("Primary")
        buttons.addButton(test, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def run_probe() -> str:
            client = VisionClient(self.cfg.llm)
            try:
                probe = client.probe()
                return ("✓ " if probe.ok else "✗ ") + probe.reason
            finally:
                client.close()

        def show(message: str) -> None:
            colour = theme.OK if message.startswith("✓") else theme.BAD
            result.setText(f"<span style='color:{colour}'>{message}</span>")

        test.clicked.connect(lambda: (result.setText("测试中…"), self._run_job(run_probe, show)))
        dialog.exec()

    def _show_text(self, title: str, text: str) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(640, 420)
        layout = QVBoxLayout(dialog)
        browser = QTextBrowser()
        browser.setPlainText(text)
        layout.addWidget(browser)
        buttons = close_buttons()
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _show_detail(self, session: SessionView) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("活动详情")
        dialog.resize(720, 560)
        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)

        heading = QLabel(session.title or "（无标题）")
        heading.setFont(theme.font(14, QFont.Weight.DemiBold))
        heading.setWordWrap(True)
        layout.addWidget(heading)

        meta = QLabel(
            f"<span style='color:{theme.MUTED}'>"
            f"{session.range_label} · {clock.fmt_duration(session.duration)} · "
            f"{session.app} · {theme.label(session.category)}</span>"
        )
        meta.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(meta)

        if session.thumb and session.thumb.exists():
            pixmap = QPixmap(str(session.thumb))
            if not pixmap.isNull():
                label = QLabel()
                label.setPixmap(
                    pixmap.scaled(
                        680,
                        380,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                layout.addWidget(label)

        body = QLabel(session.summary or "模型没有给这段写描述。")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(body)
        layout.addStretch(1)

        buttons = close_buttons()
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "关于 dayloom",
            f"<b>dayloom {__version__}</b><br><br>"
            "本地优先的屏幕活动日记。截图、分析、日报全部留在本机。<br><br>"
            f"数据目录：{self.cfg.home}<br>"
            "MIT License",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dayloom gui", description="dayloom 桌面客户端")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--day", default=None, help="直接打开某一天（YYYY-MM-DD）")
    args, _unknown = parser.parse_known_args(argv)

    cfg = config_mod.load(args.config) if args.config else config_mod.load()

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("dayloom")
    app.setApplicationDisplayName("dayloom")
    app.setStyle("Fusion")
    app.setPalette(theme.light_palette())
    app.setStyleSheet(theme.STYLESHEET)
    app.setFont(theme.app_font())
    # With a tray icon the window is a view, not the app: closing it must not quit.
    app.setQuitOnLastWindowClosed(not QSystemTrayIcon.isSystemTrayAvailable())

    config_path = args.config
    # First launch from the Start Menu / double-click: nothing is configured yet.
    # Ask the three questions instead of showing an empty window.
    if args.config is None and not config_mod.resolve_path(None).exists() and not cfg.db_path.exists():
        from .firstrun import run_first_run

        chosen = run_first_run(cfg)
        if chosen is None:
            return 0
        cfg, config_path = chosen

    # One window at a time: the lock is an OS file lock, so a crashed instance
    # releases it automatically (unlike a pid file).
    global _LOCK_HANDLE
    _LOCK_HANDLE = runtime.acquire_instance_lock(cfg.home)
    if _LOCK_HANDLE is None:
        QMessageBox.information(
            None, "dayloom", "dayloom 已经开着一个窗口了。\n要再开一个？先关掉原来的（或从托盘退出）。"
        )
        return 0

    window = MainWindow(cfg, day=args.day, config_path=config_path)
    window.show()
    # Give the window a moment to paint, then honour capture.enabled.
    QTimer.singleShot(1500, window.ensure_daemon_running)
    return int(app.exec())
