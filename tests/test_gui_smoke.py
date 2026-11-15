"""Smoke tests for the Qt layer.

They run on Qt's ``offscreen`` platform, so CI needs no display. The window is
built from the same seeded database the model tests use, which catches the
"works in isolation, explodes on construction" class of bug.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="桌面客户端依赖 PySide6（pip install dayloom[qt]）")

from conftest import add_activity, add_batch, at  # noqa: E402

DAY = "2026-05-10"


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    from dayloom.gui import theme

    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(theme.STYLESHEET)
    app.setFont(theme.app_font())
    yield app


def test_window_builds_and_renders(cfg, repo, qapp):
    from dayloom.gui.app import MainWindow

    batch = add_batch(repo, DAY, at(9, 0), at(9, 30))
    add_activity(
        repo, batch, 1, at(9, 0), at(9, 30), app="code", category="coding",
        title="写时间线视图", summary="自绘行控件",
    )

    window = MainWindow(cfg, day=DAY)
    try:
        window.resize(1100, 700)
        window.show()
        qapp.processEvents()

        assert window.sidebar.total_label.text() != "—"
        assert DAY in window.sidebar.day_label.text()

        rows = window.findChildren(object)
        from dayloom.gui.widgets import TimelineRow

        timeline_rows = [w for w in rows if isinstance(w, TimelineRow)]
        assert len(timeline_rows) == 1
        assert timeline_rows[0].view.title == "写时间线视图"

        pixmap = window.grab()
        assert pixmap.width() > 100 and pixmap.height() > 100
    finally:
        window.close()


def test_empty_window_shows_an_empty_state(cfg, qapp):
    from dayloom.gui.app import MainWindow
    from dayloom.gui.widgets import EmptyState, TimelineRow

    window = MainWindow(cfg, day=DAY)
    try:
        window.show()
        qapp.processEvents()
        widgets = window.findChildren(object)
        assert any(isinstance(w, EmptyState) for w in widgets)
        assert not any(isinstance(w, TimelineRow) for w in widgets)
    finally:
        window.close()


def test_day_navigation_moves_one_day(cfg, qapp):
    from dayloom.gui.app import MainWindow

    window = MainWindow(cfg, day=DAY)
    try:
        window._shift_day(-1)
        assert window._day == "2026-05-09"
        window._shift_day(1)
        assert window._day == DAY
    finally:
        window.close()


def test_close_hides_to_tray_instead_of_quitting(cfg, qapp, monkeypatch):
    from PySide6.QtWidgets import QSystemTrayIcon

    from dayloom.gui.app import MainWindow

    monkeypatch.setattr(
        QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True)
    )
    window = MainWindow(cfg, day=DAY)
    try:
        window.show()
        qapp.processEvents()
        assert window.tray is not None
        window.close()
        qapp.processEvents()
        assert window.isVisible() is False
        assert window._quit_requested is False
        window.quit_app()
        assert window._quit_requested is True
    finally:
        window.close()
