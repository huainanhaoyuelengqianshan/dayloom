"""Settings dialog.

Writes the whole ``config.toml`` (validated through pydantic) and lets the
daemon pick it up by itself: it polls the file's mtime, so no restart is needed
for capture settings. Comments in the file are lost on save, which is why the
dialog says so and keeps a ``.bak``.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import config as config_mod
from ..config import Config
from . import theme

MODES = [
    ("full_monitor", "截前台窗口所在的整块屏（推荐）"),
    ("active_window", "只截前台窗口（最隐私，但窗口小时截图很小）"),
    ("all_monitors", "截所有屏拼合（多屏两边都看得到，最费 token）"),
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
            mark = "✓ " if probe.ok else "✗ "
            self.signals.done.emit(mark + probe.reason)
        except Exception as exc:  # pragma: no cover - shown in the dialog
            self.signals.done.emit(f"✗ {type(exc).__name__}: {exc}")


def _spin(minimum: int, maximum: int, value: int, suffix: str = "") -> QSpinBox:
    box = QSpinBox()
    box.setRange(minimum, maximum)
    box.setValue(value)
    if suffix:
        box.setSuffix(suffix)
    return box


def _dspin(minimum: float, maximum: float, value: float, suffix: str = "") -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(minimum, maximum)
    box.setDecimals(1)
    box.setSingleStep(0.5)
    box.setValue(value)
    if suffix:
        box.setSuffix(suffix)
    return box


class SettingsDialog(QDialog):
    saved = Signal()

    def __init__(self, cfg: Config, config_path: Path | None = None, parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg.model_copy(deep=True)
        self._config_path = config_path
        self._jobs: list[_ProbeJob] = []

        self.setWindowTitle("设置")
        self.resize(640, 620)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        note = QLabel(
            f"保存会重写配置文件（旧文件备份为 .bak），守护进程约 2 秒内自动重载采集参数。"
            f"<br><span style='color:{theme.MUTED}'>data_dir 改动需要重启守护进程。</span>"
        )
        note.setTextFormat(Qt.TextFormat.RichText)
        note.setWordWrap(True)
        layout.addWidget(note)

        tabs = QTabWidget()
        tabs.addTab(self._build_capture_tab(), "采集")
        tabs.addTab(self._build_storage_tab(), "存储与隐私")
        tabs.addTab(self._build_model_tab(), "视觉模型")
        layout.addWidget(tabs, 1)

        buttons = QDialogButtonBox()
        cancel = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        save = buttons.addButton("保存并应用", QDialogButtonBox.ButtonRole.AcceptRole)
        save.setObjectName("Primary")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self._save)
        layout.addWidget(buttons)

    # -- tabs ------------------------------------------------------------
    def _build_capture_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setSpacing(10)
        form.setContentsMargins(16, 16, 16, 16)
        cap = self._cfg.capture

        self.probe_interval = _dspin(0.5, 60.0, cap.probe_interval, " 秒")
        self.heartbeat = _dspin(2.0, 600.0, cap.heartbeat, " 秒")
        self.max_still = _spin(0, 240, cap.max_still_minutes, " 分钟")
        self.idle_seconds = _spin(30, 86400, cap.idle_seconds, " 秒")
        self.mode = QComboBox()
        for value, label in MODES:
            self.mode.addItem(label, value)
        index = max(0, self.mode.findData(cap.mode))
        self.mode.setCurrentIndex(index)
        self.max_edge = _spin(320, 4096, cap.max_edge, " px")
        self.quality = _spin(10, 100, cap.webp_quality)
        self.dedupe = _spin(0, 32, cap.dedupe_distance)

        form.addRow("探测间隔", self.probe_interval)
        form.addRow("最小落帧间隔", self.heartbeat)
        form.addRow("静止补帧", self.max_still)
        form.addRow("空闲判定", self.idle_seconds)
        form.addRow("截屏范围", self.mode)
        form.addRow("图片长边", self.max_edge)
        form.addRow("WebP 质量", self.quality)
        form.addRow("去重阈值", self.dedupe)

        self.titles_edit = QPlainTextEdit("\n".join(cap.blacklist_titles))
        self.titles_edit.setFixedHeight(86)
        self.titles_edit.setPlaceholderText("一行一个正则，例如 (?i)1password")
        form.addRow("标题黑名单", self.titles_edit)

        self.apps_edit = QPlainTextEdit("\n".join(cap.blacklist_apps))
        self.apps_edit.setFixedHeight(64)
        self.apps_edit.setPlaceholderText("一行一个进程名，例如 keepass.exe")
        form.addRow("进程黑名单", self.apps_edit)
        return page

    def _build_storage_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setSpacing(10)
        form.setContentsMargins(16, 16, 16, 16)

        self.keep_days = _spin(0, 3650, self._cfg.storage.keep_frames_days, " 天")
        self.boundary = _spin(0, 23, self._cfg.general.day_boundary_hour, " 点")

        form.addRow("截图保留", self.keep_days)
        hint = QLabel("0 = 永不删除。活动记录和日报永远保留，这里只影响截图文件。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.MUTED};")
        form.addRow("", hint)

        form.addRow("日界", self.boundary)
        hint2 = QLabel("逻辑日从几点开始。设成 4，凌晨 2 点的工作算前一天。")
        hint2.setWordWrap(True)
        hint2.setStyleSheet(f"color: {theme.MUTED};")
        form.addRow("", hint2)

        self.data_dir_label = QLabel(str(self._cfg.home))
        self.data_dir_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.data_dir_label.setStyleSheet(f"color: {theme.MUTED};")
        form.addRow("数据目录", self.data_dir_label)
        return page

    def _build_model_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setSpacing(10)
        form.setContentsMargins(16, 16, 16, 16)
        llm = self._cfg.llm

        self.llm_enabled = QCheckBox("启用视觉分析（关掉就只采集，不花钱）")
        self.llm_enabled.setChecked(llm.enabled)
        form.addRow("", self.llm_enabled)

        self.base_url = QLineEdit(llm.base_url)
        self.base_url.setPlaceholderText("https://your-endpoint/v1 或 http://127.0.0.1:11434/v1")
        form.addRow("base_url", self.base_url)

        self.api_key = QLineEdit(llm.api_key)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("本地模型留空")
        form.addRow("api_key", self.api_key)

        self.model_name = QLineEdit(llm.model)
        form.addRow("model", self.model_name)

        self.max_frames = _spin(1, 64, llm.max_frames, " 帧")
        form.addRow("每次最多送", self.max_frames)
        self.focus_strip = QCheckBox("附一张任务栏徽标条（多屏判断更准，多花一点 token）")
        self.focus_strip.setChecked(llm.focus_strip)
        self.focus_strip.setToolTip(
            "把抽样帧底部的任务栏裁出来、放大、拼成一张小图随批次发送。\n"
            "图标下方更宽/带高亮背景的那一项就是当时的前台应用，"
            "双屏或多窗口时模型靠它判断“用户当前在哪个界面”。\n"
            "上下文很紧的本地模型可以关掉。"
        )
        form.addRow("", self.focus_strip)
        self.max_images = _spin(0, 10000, int(self._cfg.analysis.max_images_per_day), " 张/天")
        self.max_images.setToolTip(
            "每天最多送多少张图给模型，用完就暂停分析（批次留到明天）。0 = 不限。\n"
            "图片几乎占了全部 token，这是控制花费最直接的开关。"
        )
        form.addRow("每日图片预算", self.max_images)
        self.timeout = _spin(5, 1800, int(llm.timeout), " 秒")
        form.addRow("请求超时", self.timeout)

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        self.test_button = QPushButton("测试连接")
        self.test_button.clicked.connect(self._test_connection)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        row_layout.addWidget(self.test_button)
        row_layout.addWidget(self.test_result, 1)
        form.addRow("校验", row)

        hint = QLabel(
            "校验会生成一张随机色块图，只有真的能看图的模型才能念对序列；"
            "静默丢弃图片的端点会被判失败。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.MUTED};")
        form.addRow("", hint)
        return page

    # -- actions ---------------------------------------------------------
    def _collect(self) -> Config:
        cfg = self._cfg.model_copy(deep=True)
        cap = cfg.capture
        cap.probe_interval = float(self.probe_interval.value())
        cap.heartbeat = float(self.heartbeat.value())
        cap.max_still_minutes = int(self.max_still.value())
        cap.idle_seconds = int(self.idle_seconds.value())
        cap.mode = str(self.mode.currentData())
        cap.max_edge = int(self.max_edge.value())
        cap.webp_quality = int(self.quality.value())
        cap.dedupe_distance = int(self.dedupe.value())
        cap.blacklist_titles = [
            line.strip() for line in self.titles_edit.toPlainText().splitlines() if line.strip()
        ]
        cap.blacklist_apps = [
            line.strip() for line in self.apps_edit.toPlainText().splitlines() if line.strip()
        ]

        cfg.storage.keep_frames_days = int(self.keep_days.value())
        cfg.general.day_boundary_hour = int(self.boundary.value())

        cfg.llm.enabled = self.llm_enabled.isChecked()
        cfg.llm.base_url = self.base_url.text().strip()
        cfg.llm.api_key = self.api_key.text().strip()
        cfg.llm.model = self.model_name.text().strip()
        cfg.llm.max_frames = int(self.max_frames.value())
        cfg.llm.focus_strip = self.focus_strip.isChecked()
        cfg.analysis.max_images_per_day = int(self.max_images.value())
        cfg.llm.timeout = float(self.timeout.value())
        return Config.model_validate(cfg.model_dump())

    def _save(self) -> None:
        try:
            cfg = self._collect()
        except Exception as exc:
            QMessageBox.warning(self, "配置无效", str(exc))
            return
        try:
            path = config_mod.write_config(cfg, self._config_path)
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self._cfg = cfg
        self.saved.emit()
        QMessageBox.information(self, "已保存", f"写入 {path}\n守护进程会在 2 秒内重载采集参数。")
        self.accept()

    def _test_connection(self) -> None:
        try:
            cfg = self._collect()
        except Exception as exc:
            self.test_result.setText(f"<span style='color:{theme.BAD}'>✗ {exc}</span>")
            return
        self.test_result.setText("测试中…")
        self.test_button.setEnabled(False)
        job = _ProbeJob(cfg)
        job.signals.done.connect(self._show_probe_result)
        self._jobs.append(job)
        QThreadPool.globalInstance().start(job)

    def _show_probe_result(self, message: str) -> None:
        self.test_button.setEnabled(True)
        colour = theme.OK if message.startswith("✓") else theme.BAD
        self.test_result.setText(f"<span style='color:{colour}'>{message}</span>")


def open_settings(parent, cfg: Config, config_path: Path | None) -> bool:
    """Show the dialog modally; returns True when the config was written."""
    dialog = SettingsDialog(cfg, config_path, parent)
    accepted = [False]
    dialog.saved.connect(lambda: accepted.__setitem__(0, True))
    dialog.exec()
    return accepted[0]


__all__ = ["SettingsDialog", "open_settings"]
