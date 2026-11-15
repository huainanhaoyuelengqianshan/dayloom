"""System tray icon, menu and its generated artwork.

The icon is painted at runtime instead of shipping a .ico: one less binary in
the repo, and the small sizes stay crisp because each one is drawn at its real
pixel size rather than downscaled from 256px.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .. import clock
from . import theme

ICON_SIZES = (16, 20, 24, 32, 48, 64, 128)


def icon_pixmap(size: int) -> QPixmap:
    """A rounded accent tile with three 'threads' — the loom."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(theme.ACCENT))
    radius = size * 0.24
    painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)

    painter.setBrush(QColor("white"))
    bar_w = max(1.0, size * 0.11)
    gap = size * 0.13
    heights = (0.40, 0.60, 0.34)
    total = bar_w * len(heights) + gap * (len(heights) - 1)
    x = (size - total) / 2
    for factor in heights:
        bar_h = size * factor
        painter.drawRoundedRect(
            QRectF(x, (size - bar_h) / 2, bar_w, bar_h), bar_w / 2, bar_w / 2
        )
        x += bar_w + gap
    painter.end()
    return pixmap


def make_icon() -> QIcon:
    """Prefer the packaged .ico (crisper at 16px); fall back to painting."""
    from ..paths import asset

    path = asset("dayloom.ico")
    if path is not None:
        icon = QIcon(str(path))
        if not icon.isNull():
            return icon
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(icon_pixmap(size))
    return icon


class Tray(QSystemTrayIcon):
    """Tray entry point: the app keeps recording when the window is closed."""

    def __init__(self, window) -> None:
        super().__init__(make_icon(), window)
        self.window = window
        self.setToolTip("dayloom")

        menu = QMenu()
        self.headline = QAction("今天还没有记录", menu)
        self.headline.setEnabled(False)
        menu.addAction(self.headline)
        menu.addSeparator()

        open_action = QAction("打开主窗口", menu)
        open_action.triggered.connect(window.show_from_tray)
        menu.addAction(open_action)

        self.start_action = QAction("启动采集", menu)
        self.start_action.triggered.connect(window.start_daemon)
        menu.addAction(self.start_action)

        self.pause_action = QAction("暂停采集", menu)
        self.pause_action.setCheckable(True)
        self.pause_action.triggered.connect(window.toggle_pause)
        menu.addAction(self.pause_action)

        analyze_action = QAction("分析现在", menu)
        analyze_action.triggered.connect(lambda: window.analyze_now(silent=True))
        menu.addAction(analyze_action)

        reveal_action = QAction("打开数据目录", menu)
        reveal_action.triggered.connect(lambda: window.open_path(window.cfg.home))
        menu.addAction(reveal_action)

        menu.addSeparator()
        quit_action = QAction("退出 dayloom", menu)
        quit_action.triggered.connect(window.quit_app)
        menu.addAction(quit_action)

        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)
        self.messageClicked.connect(window.on_tray_message_clicked)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.window.show_from_tray()

    def update_state(
        self,
        *,
        tracked_seconds: float,
        frames_today: int,
        paused: bool,
        gap: str | None,
        daemon_running: bool,
    ) -> None:
        if tracked_seconds >= 60:
            text = f"今天已记录 {clock.fmt_duration_short(tracked_seconds)}"
        elif frames_today:
            text = f"今天已记录 {frames_today} 帧"
        else:
            text = "今天还没有记录"
        if paused:
            text += " · 已暂停"
        elif gap:
            text += " · " + {"idle": "空闲中", "locked": "锁屏中"}.get(gap, "已跳过")
        elif not daemon_running:
            text += " · 守护进程未运行"
        self.headline.setText(text)
        self.pause_action.setChecked(paused)
        self.start_action.setEnabled(not daemon_running)
        self.setToolTip(f"dayloom · {text}")
