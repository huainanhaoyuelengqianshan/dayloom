"""Tray icon and menu, without needing a real notification area."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="桌面客户端依赖 PySide6")

from PySide6.QtWidgets import QWidget  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    from dayloom.gui import theme

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.STYLESHEET)
    return app


class _StubWindow(QWidget):
    def __init__(self, home) -> None:
        super().__init__()
        self.cfg = SimpleNamespace(home=home)
        self.calls: list[str] = []

    def show_from_tray(self) -> None:
        self.calls.append("show")

    def start_daemon(self) -> None:
        self.calls.append("start")

    def toggle_pause(self) -> None:
        self.calls.append("pause")

    def analyze_now(self, silent: bool = False) -> None:
        self.calls.append(f"analyze:{silent}")

    def open_path(self, path) -> None:
        self.calls.append("open")

    def quit_app(self) -> None:
        self.calls.append("quit")

    def on_tray_message_clicked(self) -> None:
        self.calls.append("message")


def test_icon_is_painted_at_every_size(qapp):
    from dayloom.gui.tray import ICON_SIZES, icon_pixmap, make_icon

    for size in ICON_SIZES:
        pixmap = icon_pixmap(size)
        assert pixmap.width() == size and pixmap.height() == size
        assert not pixmap.isNull()
    icon = make_icon()
    assert not icon.isNull()


def test_menu_has_the_expected_actions(qapp, home):
    from dayloom.gui.tray import Tray

    window = _StubWindow(home)
    tray = Tray(window)
    labels = [action.text() for action in tray.contextMenu().actions() if action.text()]
    assert "打开主窗口" in labels
    assert "暂停采集" in labels
    assert "分析现在" in labels
    assert "退出 dayloom" in labels


def test_state_text_reflects_recording_and_pause(qapp, home):
    from dayloom.gui.tray import Tray

    window = _StubWindow(home)
    tray = Tray(window)

    tray.update_state(
        tracked_seconds=12540, frames_today=42, paused=False, gap=None, daemon_running=True
    )
    assert "3h29m" in tray.headline.text()
    assert tray.pause_action.isChecked() is False
    assert tray.start_action.isEnabled() is False

    tray.update_state(
        tracked_seconds=12540, frames_today=42, paused=True, gap=None, daemon_running=True
    )
    assert "已暂停" in tray.headline.text()
    assert tray.pause_action.isChecked() is True

    tray.update_state(
        tracked_seconds=0, frames_today=0, paused=False, gap=None, daemon_running=False
    )
    assert "还没有记录" in tray.headline.text()
    assert "守护进程未运行" in tray.headline.text()
    assert tray.start_action.isEnabled() is True

    tray.update_state(
        tracked_seconds=0, frames_today=3, paused=False, gap="idle", daemon_running=True
    )
    assert "3 帧" in tray.headline.text()
    assert "空闲中" in tray.headline.text()
