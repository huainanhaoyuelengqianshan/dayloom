"""Week view: seven bars, the category mix, and the week's narrative."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .. import clock
from ..analyzer.weekly import WeekReport
from . import theme
from .widgets import close_buttons


class DailyBars(QWidget):
    """One row per weekday: name, bar, duration."""

    ROW = 30

    def __init__(self, report: WeekReport, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.report = report
        self.setMinimumHeight(self.ROW * 7 + 8)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        report = self.report
        peak = max((d.tracked_seconds for d in report.daily), default=0.0) or 1.0

        label_w = 46
        value_w = 70
        bar_left = label_w + 8
        bar_right = self.width() - value_w - 8
        bar_w = max(40.0, bar_right - bar_left)

        for index, summary in enumerate(report.daily):
            top = index * self.ROW + 4
            painter.setFont(theme.font(9))
            painter.setPen(QColor(theme.MUTED))
            painter.drawText(
                QRectF(0, top, label_w, 20),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                clock.weekday_label(summary.day),
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#EEF1F5"))
            painter.drawRoundedRect(QRectF(bar_left, top + 8, bar_w, 10), 5, 5)
            ratio = summary.tracked_seconds / peak
            if ratio > 0:
                painter.setBrush(QColor(theme.ACCENT))
                painter.drawRoundedRect(
                    QRectF(bar_left, top + 8, max(4.0, bar_w * ratio), 10), 5, 5
                )
            painter.setFont(theme.font(9, QFont.Weight.DemiBold))
            painter.setPen(QColor(theme.TEXT))
            painter.drawText(
                QRectF(bar_right + 8, top, value_w, 26),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                clock.fmt_duration_short(summary.tracked_seconds)
                if summary.tracked_seconds
                else "—",
            )


class WeekDialog(QDialog):
    def __init__(self, report: WeekReport, parent=None) -> None:
        super().__init__(parent)
        self.report = report
        self.setWindowTitle("本周")
        self.resize(560, 620)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(20, 18, 20, 16)

        title = QLabel(report.label)
        title.setFont(theme.font(12, QFont.Weight.DemiBold))
        title.setStyleSheet(f"color: {theme.MUTED};")
        layout.addWidget(title)

        total = QLabel(
            clock.fmt_duration_short(report.tracked_seconds)
            if report.tracked_seconds
            else "没有数据"
        )
        total.setFont(theme.font(24, QFont.Weight.Bold))
        layout.addWidget(total)

        best = report.best_day
        if best is not None and best.tracked_seconds > 0:
            note = QLabel(
                f"最长的一天：{best.day} {clock.weekday_label(best.day)} "
                f"{clock.fmt_duration_short(best.tracked_seconds)}"
            )
            note.setStyleSheet(f"color: {theme.MUTED};")
            layout.addWidget(note)

        layout.addWidget(DailyBars(report))

        if report.categories:
            total_seconds = sum(report.categories.values()) or 1.0
            mix = QLabel(
                " · ".join(
                    f"{theme.label(category)} {seconds / total_seconds * 100:.0f}%"
                    for category, seconds in sorted(
                        report.categories.items(), key=lambda kv: -kv[1]
                    )[:5]
                )
            )
            mix.setWordWrap(True)
            mix.setStyleSheet(f"color: {theme.MUTED};")
            layout.addWidget(mix)

        narrative = QTextBrowser()
        narrative.setOpenExternalLinks(False)
        if report.narrative:
            body = report.narrative.replace("\n", "<br>")
            html = f"<div style='line-height:150%;'>{body}</div>"
            if report.highlights:
                items = "".join(f"<li>{h}</li>" for h in report.highlights)
                html += f"<ul style='margin-top:8px; padding-left:16px;'>{items}</ul>"
            narrative.setHtml(html)
        else:
            narrative.setHtml(
                f"<div style='color:{theme.MUTED}'>还没有周报叙述。"
                "运行 <code>dayloom week</code> 生成一次。</div>"
            )
        layout.addWidget(narrative, 1)

        buttons = close_buttons()
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def show_week(parent, report: WeekReport) -> None:
    WeekDialog(report, parent).exec()


__all__ = ["WeekDialog", "DailyBars", "show_week"]
