"""First-run wizard: defaults, presets and what it writes."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="桌面客户端依赖 PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from dayloom.gui import theme

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.STYLESHEET)
    app.setFont(theme.app_font())
    return app


def test_presets_are_vision_endpoints():
    from dayloom.gui.firstrun import PRESETS

    labels = [name for name, *_ in PRESETS]
    assert any("Ollama" in name for name in labels)
    assert any("llama.cpp" in name for name in labels)
    for _name, url, _model, tip in PRESETS:
        assert url.startswith("http")
        assert tip


def test_preset_fills_the_fields(cfg, qapp):
    from dayloom.gui.firstrun import PRESETS, FirstRunDialog

    dialog = FirstRunDialog(cfg)
    dialog.preset.setCurrentIndex(1)  # llama.cpp
    _name, url, model, _tip = PRESETS[1]
    assert dialog.base_url.text() == url
    assert dialog.model_name.text() == model


def test_save_writes_a_usable_config(cfg, tmp_path, qapp, monkeypatch):
    from dayloom.gui import firstrun

    target = tmp_path / "chosen-home"
    spawned: list = []
    autostart_calls: list = []
    monkeypatch.setattr(firstrun.autostart, "spawn_daemon", lambda path=None: spawned.append(path) or True)
    monkeypatch.setattr(
        firstrun.autostart, "enable", lambda path=None: autostart_calls.append(path) or (True, "ok")
    )

    dialog = firstrun.FirstRunDialog(cfg)
    dialog.data_dir_edit.setText(str(target))
    dialog.base_url.setText("http://127.0.0.1:11434/v1")
    dialog.model_name.setText("qwen2.5vl:7b")
    dialog.api_key.setText("")
    dialog.start_now.setChecked(True)
    dialog.autostart_check.setChecked(False)
    dialog._save()

    config_file = target / "config.toml"
    assert config_file.exists()
    from dayloom import config as config_mod

    written = config_mod.load(config_file)
    assert written.general.data_dir == str(target)
    assert written.llm.model == "qwen2.5vl:7b"
    assert written.llm.enabled is True
    assert len(spawned) == 1
    assert autostart_calls == []

    result = dialog.result_config()
    assert result is not None
    assert result[1] == config_file


def test_skipping_the_model_disables_analysis(cfg, tmp_path, qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from dayloom.gui import firstrun

    monkeypatch.setattr(firstrun.autostart, "spawn_daemon", lambda path=None: True)
    monkeypatch.setattr(firstrun.autostart, "enable", lambda path=None: (True, "ok"))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))

    dialog = firstrun.FirstRunDialog(cfg)
    dialog.data_dir_edit.setText(str(tmp_path / "home2"))
    dialog.model_name.setText("")
    dialog.start_now.setChecked(False)
    dialog.autostart_check.setChecked(False)
    dialog._save()

    from dayloom import config as config_mod

    written = config_mod.load(tmp_path / "home2" / "config.toml")
    assert written.llm.enabled is False


def test_wizard_rejects_an_unwritable_dir(cfg, qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from dayloom.gui import firstrun

    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append((a[1], a[2])))
    )
    dialog = firstrun.FirstRunDialog(cfg)
    dialog.data_dir_edit.setText("Z:\\definitely\\not\\writable\\here")
    dialog._save()
    assert shown, "应该弹出一个错误框"
    assert shown[0][0] == "无法写入"
    assert "Z:" in shown[0][1]
    assert dialog.result_config() is None
