from __future__ import annotations

from pathlib import Path

import pytest
from conftest import add_activity, add_batch

from dayloom import config as config_mod
from dayloom import runtime

DAY = "2026-05-10"


# -- config writing -----------------------------------------------------
def test_image_budget_survives_a_write(cfg, tmp_path):
    target = tmp_path / "budget.toml"
    cfg.analysis.max_images_per_day = 480
    config_mod.write_config(cfg, target)
    assert config_mod.load(target).analysis.max_images_per_day == 480


def test_settings_dialog_edits_the_budget(cfg):
    pytest.importorskip("PySide6", reason="桌面客户端依赖 PySide6")
    from PySide6.QtWidgets import QApplication

    from dayloom.gui.settings import SettingsDialog

    QApplication.instance() or QApplication([])
    cfg.analysis.max_images_per_day = 300
    dialog = SettingsDialog(cfg)
    assert int(dialog.max_images.value()) == 300
    dialog.max_images.setValue(120)
    assert dialog._collect().analysis.max_images_per_day == 120


def test_write_config_roundtrips(cfg, tmp_path):
    target = tmp_path / "config.toml"
    cfg.capture.probe_interval = 7.5
    cfg.llm.model = "some-vision-model"
    written = config_mod.write_config(cfg, target)
    assert written == target

    back = config_mod.load(target)
    assert back.capture.probe_interval == 7.5
    assert back.llm.model == "some-vision-model"


def test_write_config_keeps_a_backup(cfg, tmp_path):
    target = tmp_path / "config.toml"
    target.write_text("[general]\nday_boundary_hour = 5\n", encoding="utf-8")
    config_mod.write_config(cfg, target)
    backup = target.with_suffix(".toml.bak")
    assert backup.exists()
    assert "day_boundary_hour = 5" in backup.read_text(encoding="utf-8")


def test_written_config_is_validated(cfg, tmp_path):
    cfg.capture.blacklist_titles = ["(?i)ok", "(unclosed"]
    with pytest.raises(ValueError):
        config_mod.write_config(cfg, tmp_path / "bad.toml")


# -- single instance lock ----------------------------------------------
def test_instance_lock_is_exclusive(home):
    first = runtime.acquire_instance_lock(home)
    assert first is not None
    try:
        assert runtime.acquire_instance_lock(home) is None
    finally:
        first.close()
    second = runtime.acquire_instance_lock(home)
    assert second is not None
    second.close()


# -- settings dialog ----------------------------------------------------
def test_settings_dialog_collects_edits(cfg, qapp):
    from dayloom.gui.settings import SettingsDialog

    dialog = SettingsDialog(cfg)
    dialog.probe_interval.setValue(4.0)
    dialog.heartbeat.setValue(12.0)
    dialog.mode.setCurrentIndex(dialog.mode.findData("full_monitor"))
    dialog.titles_edit.setPlainText("(?i)vault\n(?i)bank")
    dialog.apps_edit.setPlainText("keepass.exe")
    dialog.max_frames.setValue(6)

    result = dialog._collect()
    assert result.capture.probe_interval == 4.0
    assert result.capture.heartbeat == 12.0
    assert result.capture.mode == "full_monitor"
    assert result.capture.blacklist_titles == ["(?i)vault", "(?i)bank"]
    assert result.capture.blacklist_apps == ["keepass.exe"]
    assert result.llm.max_frames == 6


def test_settings_dialog_surfaces_bad_regex(cfg, qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from dayloom.gui.settings import SettingsDialog

    shown: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *args, **kwargs: shown.append(str(args[2])))
    )

    dialog = SettingsDialog(cfg)
    dialog.titles_edit.setPlainText("(unclosed")
    dialog._save()
    assert shown and "正则" in shown[0]


# -- daily summary notification ---------------------------------------
def test_summary_balloon_is_shown_once(cfg, repo, qapp, monkeypatch):
    from PySide6.QtWidgets import QSystemTrayIcon

    from dayloom.gui.app import MainWindow

    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    messages: list[tuple] = []
    monkeypatch.setattr(
        QSystemTrayIcon,
        "showMessage",
        staticmethod(lambda *args: messages.append(args)),
    )

    from dayloom import clock

    today = clock.day_key(clock.now_ts(), cfg.general.day_boundary_hour)
    yesterday = clock.shift_day(today, -1)
    start = clock.day_bounds(yesterday, cfg.general.day_boundary_hour)[0] + 3600
    batch = add_batch(repo, yesterday, start, start + 1800)
    add_activity(repo, batch, 1, start, start + 1800, app="code", category="coding")

    window = MainWindow(cfg, day=today)
    try:
        window._summary_checked_for = None
        window._maybe_notify_summary()
        assert len(messages) == 1
        assert "昨天记录了" in messages[0][0]

        window._summary_checked_for = None
        window._maybe_notify_summary()
        assert len(messages) == 1  # once per day

        assert window._pending_summary_day == yesterday
        window.on_tray_message_clicked()
        assert window._day == yesterday
        assert window._pending_summary_day is None
    finally:
        window.close()


def test_frozen_build_command_uses_the_console_sibling(monkeypatch, tmp_path):
    from dayloom import autostart

    fake_exe = tmp_path / "dayloom-gui.exe"
    fake_exe.write_text("", encoding="utf-8")
    (tmp_path / "dayloom.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys.executable", str(fake_exe))

    command = autostart.build_command(Path("C:/x/config.toml"))
    assert command[0].endswith("dayloom.exe")
    assert command[1] == "start"
    assert command[-1].endswith("config.toml")


@pytest.fixture(scope="module")
def qapp():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from dayloom.gui import theme

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.STYLESHEET)
    return app
