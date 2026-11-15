"""A place to actually read the daily rollups.

The rollup existed before but there was nowhere to look at it: the narrative
only showed up as a sidebar paragraph for the day already on screen.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from .. import clock
from ..config import Config
from ..llm.prompts import CATEGORY_ZH
from . import theme
from .model import load_reports
from .widgets import close_buttons


class ReportsDialog(QDialog):
    """List of rolled-up days on the left, the selected one on the right."""

    open_day = Signal(str)

    def __init__(self, cfg: Config, *, days: int = 30, parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._reports = load_reports(cfg, days)
        self._by_day = {r["day"]: r for r in self._reports}

        self.setWindowTitle("日报")
        self.resize(940, 620)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        header = QLabel("每天凌晨自动生成；错过的会在守护进程启动后补上。")
        header.setStyleSheet(f"color: {theme.MUTED};")
        layout.addWidget(header)

        body = QHBoxLayout()
        body.setSpacing(12)
        layout.addLayout(body, 1)

        self.list = QListWidget()
        self.list.setFixedWidth(210)
        self.list.currentItemChanged.connect(self._show_current)
        body.addWidget(self.list)

        self.body = QTextBrowser()
        self.body.setOpenExternalLinks(False)
        body.addWidget(self.body, 1)

        if not self._reports:
            self.body.setHtml(
                f"<p style='color:{theme.MUTED}'>还没有日报。</p>"
                "<p>守护进程每天会自动生成；也可以先 <code>dayloom report</code> 手动生成一天。</p>"
            )
        for report in self._reports:
            self.list.addItem(QListWidgetItem(self._label(report)))
        if self._reports:
            self.list.setCurrentRow(0)

        buttons = QHBoxLayout()
        self.open_button = QPushButton("在主窗口打开这一天")
        self.open_button.setEnabled(bool(self._reports))
        self.open_button.clicked.connect(self._open_selected)
        buttons.addWidget(self.open_button)
        buttons.addStretch(1)
        close = close_buttons()
        close.rejected.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    # -- helpers ---------------------------------------------------------
    def _label(self, report: dict) -> str:
        day = str(report["day"])
        weekday = clock.weekday_label(day)
        tracked = clock.fmt_duration_short(float(report["tracked_seconds"] or 0))
        categories = sorted(
            (report["categories"] or {}).items(), key=lambda kv: kv[1], reverse=True
        )[:1]
        top = " · ".join(CATEGORY_ZH.get(key, key) for key, _ in categories)
        return f"{day} {weekday}\n{tracked} · {top}"

    def _show_current(self, current: QListWidgetItem | None, _previous=None) -> None:
        if current is None:
            return
        index = self.list.row(current)
        if 0 <= index < len(self._reports):
            self.body.setHtml(self._html(self._reports[index]))

    def _html(self, report: dict) -> str:
        day = str(report["day"])
        weekday = clock.weekday_label(day)
        tracked = float(report["tracked_seconds"] or 0)
        parts = [
            f"<h2 style='margin:0 0 2px 0'>{day} {weekday}</h2>",
            f"<p style='color:{theme.MUTED};margin:0 0 12px 0'>"
            f"记录 {clock.fmt_duration_short(tracked)}"
            f" · 模型 {report.get('model') or '（只有统计）'}</p>",
        ]

        categories = sorted(
            (report["categories"] or {}).items(), key=lambda kv: kv[1], reverse=True
        )
        if categories:
            total = sum(value for _, value in categories) or 1.0
            rows = []
            for key, value in categories[:8]:
                share = value / total
                rows.append(
                    f"<tr><td style='padding:2px 10px 2px 0'>{CATEGORY_ZH.get(key, key)}</td>"
                    f"<td style='padding:2px 10px 2px 0'>{clock.fmt_duration_short(value)}</td>"
                    f"<td style='padding:2px 0;width:220px'>"
                    f"<div style='background:{theme.HOVER};border-radius:4px;height:10px'>"
                    f"<div style='background:{theme.color(key).name()};width:{max(2, int(share * 220))}px;"
                    f"border-radius:4px;height:10px'></div></div></td>"
                    f"<td style='padding:2px 0 2px 10px;color:{theme.MUTED}'>{share:.0%}</td></tr>"
                )
            parts.append("<table cellspacing='0' cellpadding='0'>" + "".join(rows) + "</table>")

        narrative = str(report.get("narrative") or "").strip()
        if narrative:
            parts.append(
                f"<p style='margin-top:14px;line-height:1.65'>{_escape(narrative)}</p>"
            )

        highlights = [str(item) for item in (report.get("highlights") or [])]
        if highlights:
            parts.append("<h3 style='margin-bottom:4px'>看点</h3><ul style='margin-top:0'>")
            parts.extend(f"<li>{_escape(item)}</li>" for item in highlights)
            parts.append("</ul>")

        suggestions = [str(item) for item in (report.get("suggestions") or [])]
        if suggestions:
            parts.append("<h3 style='margin-bottom:4px'>可以做得更好</h3><ul style='margin-top:0'>")
            parts.extend(f"<li>{_escape(item)}</li>" for item in suggestions)
            parts.append("</ul>")

        if not narrative and not highlights:
            parts.append(
                f"<p style='color:{theme.MUTED}'>这一天只有统计数字（生成时模型不可用）。"
                "重新跑 <code>dayloom report --date " + day + "</code> 可以补上文字。</p>"
            )
        return "".join(parts)

    def _open_selected(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        day = self._reports[self.list.row(item)]["day"]
        self.open_day.emit(str(day))
        self.accept()


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    )


def show_reports(parent, cfg: Config) -> ReportsDialog:
    dialog = ReportsDialog(cfg, parent=parent)
    dialog.setFont(QFont(theme.app_font().family(), 10))
    dialog.setStyleSheet(theme.STYLESHEET)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.open_day.connect(parent._set_day)
    dialog.exec()
    return dialog
