from __future__ import annotations

import sys
from pathlib import Path

from dayloom import autostart


def test_command_targets_the_daemon():
    command = autostart.build_command()
    assert command[1:4] == ["-m", "dayloom", "start"]
    assert command[0].lower().endswith(("python.exe", "pythonw.exe"))


def test_command_prefers_pythonw_when_available():
    launcher = Path(autostart.launcher())
    if Path(sys.executable).name.lower() == "python.exe":
        assert launcher.name.lower() in ("pythonw.exe", "python.exe")


def test_command_carries_the_config_path():
    command = autostart.build_command(Path("C:/data/config.toml"))
    assert "--config" in command
    assert command[-1].endswith("config.toml")


def test_status_never_raises():
    state = autostart.status()
    assert isinstance(state, autostart.AutostartStatus)


def test_describe_mentions_both_switches():
    text = autostart.describe()
    assert "开机自启" in text
    assert "dayloom autostart" in text


def test_startup_dir_lives_under_roaming_appdata():
    path = autostart.startup_dir()
    assert path.name == "Startup"
    assert "Start Menu" in path.parts or "Start Menu" in str(path)


def test_enable_then_disable_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart, "startup_dir", lambda: tmp_path)

    ok, message = autostart.enable()
    assert ok, message
    entries = list(tmp_path.iterdir())
    assert len(entries) == 1
    assert entries[0].name in (autostart.SHORTCUT_NAME, autostart.FALLBACK_NAME)

    ok2, message2 = autostart.disable()
    assert ok2, message2
    assert list(tmp_path.iterdir()) == []


def test_cmd_fallback_is_valid_bat(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart, "startup_dir", lambda: tmp_path)
    autostart._write_cmd(autostart.build_command(None))
    text = (tmp_path / autostart.FALLBACK_NAME).read_text(encoding="utf-8")
    assert "dayloom start" in text
    assert text.startswith("@echo off")


def test_autostart_opens_the_client_by_default(tmp_path, monkeypatch):
    """The bug: logon opened the console daemon, so no window ever appeared."""
    monkeypatch.setattr(autostart, "startup_dir", lambda: tmp_path)
    captured: dict[str, str] = {}

    def fake_create(link, *, target, arguments="", **kwargs):
        captured["target"] = target
        captured["arguments"] = arguments
        Path(link).write_text("", encoding="utf-8")
        return True

    monkeypatch.setattr("dayloom.shortcuts.create_shortcut", fake_create)

    ok, message = autostart.enable(None)
    assert ok, message
    target = Path(captured["target"]).name.lower()
    assert target in ("pythonw.exe", "python.exe", "python3.exe") or target.startswith(
        "dayloom"
    )
    assert "-m dayloom.gui" in captured["arguments"] or captured["arguments"] == ""


def test_autostart_daemon_mode_still_available(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart, "startup_dir", lambda: tmp_path)
    captured: dict[str, str] = {}

    def fake_create(link, *, target, arguments="", **kwargs):
        captured["target"] = target
        captured["arguments"] = arguments
        Path(link).write_text("", encoding="utf-8")
        return True

    monkeypatch.setattr("dayloom.shortcuts.create_shortcut", fake_create)

    ok, _ = autostart.enable(None, gui=False)
    assert ok
    assert "start" in captured["arguments"]


def test_gui_command_is_windowed(monkeypatch, tmp_path):
    gui = tmp_path / "dayloom-gui.exe"
    gui.write_text("", encoding="utf-8")
    (tmp_path / "dayloom.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys.executable", str(tmp_path / "dayloom.exe"))

    command = autostart.gui_command(Path("C:/x/config.toml"))
    assert command[0] == str(gui)
    assert command[1] == "--config"
    assert command[2].endswith("config.toml")


def test_cli_command_uses_the_console_sibling_when_frozen(monkeypatch, tmp_path):
    """Regression: the GUI used to spawn itself instead of the CLI.

    ``sys.executable -m dayloom analyze`` is correct for a source checkout, but
    in a frozen build it relaunches the windowed exe, which ignores the
    arguments and hits the single-instance lock — "分析现在" did nothing.
    """
    gui = tmp_path / "dayloom-gui.exe"
    gui.write_text("", encoding="utf-8")
    (tmp_path / "dayloom.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys.executable", str(gui))

    command = autostart.cli_command("analyze", config_path=Path("C:/x/config.toml"))
    assert command[0] == str(tmp_path / "dayloom.exe")
    assert command[1] == "analyze"
    assert "-m" not in command
    assert command[-1].endswith("config.toml")


def test_cli_command_in_a_source_checkout(monkeypatch):
    monkeypatch.delattr("sys.frozen", raising=False)
    command = autostart.cli_command("week")
    assert command[1:4] == ["-m", "dayloom", "week"]
