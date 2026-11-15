"""First-run dialog: three decisions, then the app is actually usable.

Without this, double-clicking the packaged exe gives an empty window and a
daemon that is not running — technically correct, practically broken. The
dialog covers the three things a new user cannot guess:

1. where the data lives,
2. which vision model reads the screenshots (with a real probe, not a ping),
3. whether to start recording now, and at every logon.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import autostart
from .. import config as config_mod
from ..config import Config
from . import theme

PRESETS: list[tuple[str, str, str, str]] = [
    (
        "Ollama（本地，免费）",
        "http://127.0.0.1:11434/v1",
        "qwen2.5vl:7b",
        "先装 Ollama 并执行：ollama pull qwen2.5vl:7b",
    ),
    (
        "llama.cpp（本地，需 --mmproj）",
        "http://127.0.0.1:8080/v1",
        "qwen2.5-vl-7b",
        "启动 llama-server 时必须带 --mmproj，否则模型看不见图片",
    ),
    (
        "LM Studio（本地）",
        "http://127.0.0.1:1234/v1",
        "qwen2.5-vl-7b",
        "在 LM Studio 里加载一个视觉模型并启动本地服务器",
    ),
    (
        "OpenAI 兼容云端（填你自己的）",
        "https://your-endpoint/v1",
        "",
        "任何接受 image_url 的 OpenAI 兼容端点都行；key 只存在本机 config.toml",
    ),
]


class _ProbeSignals(QObject):
    done = Signal(str)


class _ProbeJob(QRunnable):
    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self._cfg = cfg
        self.signals = _ProbeSignals()
        self.setAutoDelete(False)

    def run(self) -> None:
        from ..llm.provider import VisionClient

        try:
            client = VisionClient(self._cfg.llm)
            try:
                probe = client.probe()
            finally:
                client.close()
            self.signals.done.emit(("✓ " if probe.ok else "✗ ") + probe.reason)
        except Exception as exc:  # pragma: no cover - shown in the dialog
            self.signals.done.emit(f"✗ {type(exc).__name__}: {exc}")


def _section(number: str, title: str) -> QLabel:
    label = QLabel(f"{number}　{title}")
    label.setFont(theme.font(10, QFont.Weight.DemiBold))
    label.setStyleSheet(f"color: {theme.TEXT};")
    return label


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {theme.MUTED}; font-size: 12px;")
    return label


class FirstRunDialog(QDialog):
    def __init__(self, cfg: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cfg = cfg.model_copy(deep=True)
        self._result: tuple[Config, Path] | None = None
        self._jobs: list[_ProbeJob] = []

        self.setWindowTitle("开始使用 dayloom")
        self.setModal(True)
        self.resize(620, 660)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(14)

        title = QLabel("开始使用 dayloom")
        title.setFont(theme.font(16, QFont.Weight.Bold))
        layout.addWidget(title)
        layout.addWidget(
            _hint("三步就能用起来。截图、数据库、日报全部留在本机；只有你选的模型会看到画面。")
        )

        # 1. data dir ----------------------------------------------------
        layout.addWidget(_section("1", "数据放在哪"))
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)
        dir_label = QLabel("目录")
        dir_label.setFixedWidth(70)
        dir_label.setStyleSheet(f"color: {theme.MUTED};")
        self.data_dir_edit = QLineEdit(str(self._cfg.home))
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        row_layout.addWidget(dir_label)
        row_layout.addWidget(self.data_dir_edit, 1)
        row_layout.addWidget(browse)
        layout.addWidget(row)
        layout.addWidget(_hint("截图默认保留 14 天，活动记录和日报长期保留。"))

        layout.addWidget(self._divider())

        # 2. model -------------------------------------------------------
        layout.addWidget(_section("2", "用哪个模型看图"))
        self.preset = QComboBox()
        for name, _url, _model, _tip in PRESETS:
            self.preset.addItem(name)
        self.preset.currentIndexChanged.connect(self._apply_preset)
        layout.addWidget(self.preset)

        self.preset_hint = _hint(PRESETS[0][3])
        layout.addWidget(self.preset_hint)

        self.base_url = QLineEdit()
        self.model_name = QLineEdit()
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("本地模型留空")
        for label, widget in (
            ("base_url", self.base_url),
            ("model", self.model_name),
            ("api_key", self.api_key),
        ):
            line = QWidget()
            line_layout = QHBoxLayout(line)
            line_layout.setContentsMargins(0, 0, 0, 0)
            name = QLabel(label)
            name.setFixedWidth(70)
            name.setStyleSheet(f"color: {theme.MUTED};")
            line_layout.addWidget(name)
            line_layout.addWidget(widget, 1)
            layout.addWidget(line)

        test_row = QWidget()
        test_layout = QHBoxLayout(test_row)
        test_layout.setContentsMargins(0, 0, 0, 0)
        self.test_button = QPushButton("测试连接")
        self.test_button.clicked.connect(self._test)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        test_layout.addWidget(self.test_button)
        test_layout.addWidget(self.test_result, 1)
        layout.addWidget(test_row)

        layout.addWidget(
            _hint("测试会生成一张随机色块图，只有真能看图的模型才念得对——读不出来的端点会被判失败。")
        )

        layout.addWidget(self._divider())

        # 3. start -------------------------------------------------------
        layout.addWidget(_section("3", "现在就开始"))
        self.start_now = QCheckBox("启动采集（关掉窗口也会在托盘里继续记录）")
        self.start_now.setChecked(True)
        self.autostart_check = QCheckBox("登录 Windows 时自动打开 dayloom（并开始记录）")
        self.autostart_check.setChecked(True)
        layout.addWidget(self.start_now)
        layout.addWidget(self.autostart_check)

        layout.addStretch(1)

        buttons = QWidget()
        button_layout = QHBoxLayout(buttons)
        button_layout.setContentsMargins(0, 0, 0, 0)
        later = QPushButton("稍后再说")
        later.clicked.connect(self.reject)
        save = QPushButton("保存并开始")
        save.setObjectName("Primary")
        save.clicked.connect(self._save)
        button_layout.addStretch(1)
        button_layout.addWidget(later)
        button_layout.addWidget(save)
        layout.addWidget(buttons)

        self._apply_preset(0)
        self._load_from_config()

    # -- helpers ---------------------------------------------------------
    def _divider(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color: {theme.BORDER}; background: {theme.BORDER}; max-height: 1px;")
        return line

    def _load_from_config(self) -> None:
        llm = self._cfg.llm
        if llm.base_url:
            self.base_url.setText(llm.base_url)
        if llm.model:
            self.model_name.setText(llm.model)
        if llm.api_key:
            self.api_key.setText(llm.api_key)

    def _apply_preset(self, index: int) -> None:
        name, url, model, tip = PRESETS[index]
        self.preset_hint.setText(tip)
        if url:
            self.base_url.setText(url)
        if model:
            self.model_name.setText(model)

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择数据目录", self.data_dir_edit.text())
        if chosen:
            self.data_dir_edit.setText(chosen)

    def _collect(self) -> Config:
        cfg = self._cfg.model_copy(deep=True)
        cfg.general.data_dir = self.data_dir_edit.text().strip()
        cfg.llm.base_url = self.base_url.text().strip() or "http://127.0.0.1:11434/v1"
        cfg.llm.model = self.model_name.text().strip()
        cfg.llm.api_key = self.api_key.text().strip()
        cfg.llm.enabled = bool(cfg.llm.model)
        return Config.model_validate(cfg.model_dump())

    def _test(self) -> None:
        try:
            cfg = self._collect()
        except Exception as exc:
            self.test_result.setText(f"<span style='color:{theme.BAD}'>✗ {exc}</span>")
            return
        if not cfg.llm.model:
            self.test_result.setText(
                f"<span style='color:{theme.WARN}'>先填模型名</span>"
            )
            return
        self.test_button.setEnabled(False)
        self.test_result.setText("测试中…")
        job = _ProbeJob(cfg)
        job.signals.done.connect(self._show_probe)
        self._jobs.append(job)
        QThreadPool.globalInstance().start(job)

    def _show_probe(self, message: str) -> None:
        self.test_button.setEnabled(True)
        colour = theme.OK if message.startswith("✓") else theme.BAD
        self.test_result.setText(f"<span style='color:{colour}'>{message}</span>")

    def _save(self) -> None:
        raw = self.data_dir_edit.text().strip()
        if not raw:
            QMessageBox.warning(self, "缺少目录", "请选择一个存放数据的目录。")
            return
        home = Path(raw).expanduser()
        # Probe the directory before anything else: "无法写入" is a much better
        # message than a raw WinError from deep inside a pydantic property.
        try:
            home.mkdir(parents=True, exist_ok=True)
            probe = home / ".dayloom-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            QMessageBox.warning(self, "无法写入", f"{home}\n{exc}")
            return

        try:
            cfg = self._collect()
        except Exception as exc:
            QMessageBox.warning(self, "配置无效", str(exc))
            return
        try:
            config_path = config_mod.write_config(cfg, home / "config.toml")
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", f"无法写入 {home}：{exc}")
            return

        if self.start_now.isChecked():
            autostart.spawn_daemon(config_path)
        if self.autostart_check.isChecked():
            autostart.enable(config_path)

        self._result = (cfg, config_path)
        if not cfg.llm.model:
            QMessageBox.information(
                self,
                "只采集，不分析",
                "没有填模型，所以现在只会采集截图。\n"
                "之后可以在「工具 → 设置 → 视觉模型」里补上，再点「分析现在」。",
            )
        self.accept()

    def result_config(self) -> tuple[Config, Path] | None:
        return self._result


def run_first_run(cfg: Config, parent=None) -> tuple[Config, Path] | None:
    dialog = FirstRunDialog(cfg, parent)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        return dialog.result_config()
    return None


__all__ = ["FirstRunDialog", "run_first_run", "PRESETS"]
