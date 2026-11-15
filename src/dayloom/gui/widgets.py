"""Custom-painted widgets.

A day has ~30–60 sessions, so rows are painted directly instead of being built
from dozens of nested widgets: it stays fast, and the visual rhythm (time
column, category stripe, duration) is easier to control than with a box model.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .. import clock
from . import theme
from .model import DayView, SessionView

GAP_LABELS = {"idle": "空闲", "locked": "锁屏", "protected": "黑名单跳过"}


def close_buttons() -> QDialogButtonBox:
    """A Close button with Chinese text — Qt's default strings are untranslated."""
    box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    button = box.button(QDialogButtonBox.StandardButton.Close)
    if button is not None:
        button.setText("关闭")
    return box


class TimelineRow(QWidget):
    """One activity session."""

    clicked = Signal(object)
    HEIGHT = 84

    def __init__(self, view: SessionView, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.view = view
        self._hover = False
        self.setFixedHeight(self.HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pixmap = QPixmap(str(view.thumb)) if view.thumb else None
        if self._pixmap is not None and self._pixmap.isNull():
            self._pixmap = None
        tip = [f"{view.range_label} · {clock.fmt_duration(view.duration)}"]
        if view.title:
            tip.append(view.title)
        if view.summary:
            tip.append(view.summary)
        self.setToolTip("\n".join(tip))

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.view)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)

        painter.setPen(QPen(QColor(theme.BORDER), 1))
        painter.setBrush(QColor(theme.HOVER if self._hover else theme.CARD))
        painter.drawRoundedRect(rect, 10, 10)

        # category stripe
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.color(self.view.category))
        painter.drawRoundedRect(
            QRectF(rect.left(), rect.top() + 12, 4, rect.height() - 24), 2, 2
        )

        left = rect.left() + 20
        painter.setPen(QColor(theme.TEXT))
        painter.setFont(theme.font(11, QFont.Weight.DemiBold))
        time_metrics = QFontMetrics(theme.font(11, QFont.Weight.DemiBold))
        time_w = max(46, time_metrics.horizontalAdvance("00:00") + 6)
        painter.drawText(
            QRectF(left, rect.top() + 16, time_w, 20),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            clock.fmt_hm(self.view.start_ts),
        )
        painter.setPen(QColor(theme.FAINT))
        painter.setFont(theme.font(9))
        painter.drawText(
            QRectF(left, rect.top() + 40, time_w, 18),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            clock.fmt_hm(self.view.end_ts),
        )

        content_left = left + time_w + 16
        right = rect.right() - 18

        # Reserve the thumbnail column even when a frame is missing, so the
        # duration column stays aligned across rows.
        thumb_rect = QRectF(right - 92, rect.top() + 14, 92, 52)
        cursor = right - 92 - 12
        dur_w = max(56, time_metrics.horizontalAdvance("00h00m") + 4)
        content_right = cursor - dur_w - 14

        title = self.view.title or self.view.summary or "（无描述）"
        metrics = QFontMetrics(theme.font(11, QFont.Weight.DemiBold))
        painter.setFont(theme.font(11, QFont.Weight.DemiBold))
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(
            QRectF(content_left, rect.top() + 14, max(10, content_right - content_left), 22),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            metrics.elidedText(title, Qt.TextElideMode.ElideRight, int(content_right - content_left)),
        )

        subtitle = self.view.summary or self.view.app
        if self.view.summary and self.view.app:
            subtitle = f"{self.view.app} · {self.view.summary}"
        metrics_small = QFontMetrics(theme.font(9))
        painter.setFont(theme.font(9))
        painter.setPen(QColor(theme.MUTED))
        painter.drawText(
            QRectF(content_left, rect.top() + 40, max(10, content_right - content_left), 20),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            metrics_small.elidedText(
                subtitle, Qt.TextElideMode.ElideRight, int(content_right - content_left)
            ),
        )

        # duration + category, right aligned
        painter.setFont(theme.font(11, QFont.Weight.DemiBold))
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(
            QRectF(cursor - dur_w, rect.top() + 16, dur_w, 20),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            clock.fmt_duration_short(self.view.duration),
        )
        painter.setFont(theme.font(9))
        painter.setPen(theme.color(self.view.category))
        painter.drawText(
            QRectF(cursor - dur_w, rect.top() + 40, dur_w, 18),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            theme.label(self.view.category),
        )

        if self._pixmap:
            path = QPainterPath()
            path.addRoundedRect(thumb_rect, 6, 6)
            painter.save()
            painter.setClipPath(path)
            painter.drawPixmap(thumb_rect, self._pixmap, QRectF(self._pixmap.rect()))
            painter.restore()


class CategoryBar(QWidget):
    """A single stacked bar of the day's categories."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[tuple[str, float]] = []
        self.setFixedHeight(10)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_data(self, categories: dict[str, float]) -> None:
        total = sum(v for v in categories.values() if v > 0)
        if total <= 0:
            self._items = []
        else:
            self._items = sorted(
                ((k, v / total) for k, v in categories.items() if v > 0),
                key=lambda kv: -kv[1],
            )
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#EEF1F5"))
        painter.drawRoundedRect(rect, 5, 5)
        if not self._items:
            return
        clip = QPainterPath()
        clip.addRoundedRect(rect, 5, 5)
        painter.setClipPath(clip)
        x = rect.left()
        total_width = rect.width()
        for category, fraction in self._items:
            width = total_width * fraction
            gap = 2.0 if width > 6 else 0.0
            painter.setBrush(theme.color(category))
            painter.drawRect(QRectF(x, rect.top(), max(1.5, width - gap), rect.height()))
            x += width


class EmptyState(QWidget):
    def __init__(self, title: str, hint: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 60, 40, 60)
        layout.setSpacing(10)
        layout.addStretch(1)
        big = QLabel(title)
        big.setAlignment(Qt.AlignmentFlag.AlignCenter)
        big.setFont(theme.font(14, QFont.Weight.DemiBold))
        big.setStyleSheet(f"color: {theme.TEXT};")
        layout.addWidget(big)
        small = QLabel(hint)
        small.setAlignment(Qt.AlignmentFlag.AlignCenter)
        small.setWordWrap(True)
        small.setFont(theme.font(10))
        small.setStyleSheet(f"color: {theme.MUTED};")
        layout.addWidget(small)
        layout.addStretch(1)


class DaySidebar(QWidget):
    """Numbers and narrative for the selected day."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(280)
        self.setMaximumWidth(380)
        self.setObjectName("Sidebar")
        self.setStyleSheet(f"QWidget#Sidebar {{ background: {theme.CARD}; }}")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 16)
        outer.setSpacing(12)

        self.day_label = QLabel("—")
        self.day_label.setFont(theme.font(13, QFont.Weight.DemiBold))
        self.day_label.setStyleSheet(f"color: {theme.TEXT};")
        outer.addWidget(self.day_label)

        self.total_label = QLabel("—")
        self.total_label.setFont(theme.font(22, QFont.Weight.Bold))
        self.total_label.setStyleSheet(f"color: {theme.TEXT};")
        outer.addWidget(self.total_label)

        self.sub_label = QLabel("")
        self.sub_label.setFont(theme.font(9))
        self.sub_label.setStyleSheet(f"color: {theme.MUTED};")
        outer.addWidget(self.sub_label)

        # Shown only when something is actually wrong: capture off, analysis
        # paused (balance/budget). Silence means "all good".
        self.notice = QLabel("")
        self.notice.setWordWrap(True)
        self.notice.setFont(theme.font(9))
        self.notice.setStyleSheet(f"color: {theme.WARN};")
        self.notice.hide()
        outer.addWidget(self.notice)

        self.bar = CategoryBar()
        outer.addWidget(self.bar)

        self.cat_box = QVBoxLayout()
        self.cat_box.setSpacing(6)
        outer.addLayout(self.cat_box)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color: {theme.BORDER}; background: {theme.BORDER}; max-height: 1px;")
        outer.addWidget(line)

        caption = QLabel("今天的简报")
        caption.setFont(theme.font(9, QFont.Weight.DemiBold))
        caption.setStyleSheet(f"color: {theme.MUTED};")
        outer.addWidget(caption)

        self.narrative = QTextBrowser()
        self.narrative.setOpenExternalLinks(False)
        self.narrative.setFont(theme.font(10))
        self.narrative.setMinimumHeight(150)
        outer.addWidget(self.narrative, 1)

        self.footnote = QLabel("")
        self.footnote.setWordWrap(True)
        self.footnote.setFont(theme.font(9))
        self.footnote.setStyleSheet(f"color: {theme.FAINT};")
        outer.addWidget(self.footnote)

    def set_notice(self, text: str) -> None:
        if text:
            self.notice.setText(text)
            self.notice.show()
        else:
            self.notice.clear()
            self.notice.hide()

    def _clear_categories(self) -> None:
        while self.cat_box.count():
            item = self.cat_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def set_view(self, view: DayView) -> None:
        self.day_label.setText(f"{view.day} · {clock.weekday_label(view.day)}")
        self.total_label.setText(clock.fmt_duration(view.tracked_seconds) if view.tracked_seconds else "0m")
        parts = []
        if view.sessions:
            parts.append(f"{len(view.sessions)} 段")
        if view.frame_count:
            parts.append(f"{view.frame_count} 帧")
        if view.gaps:
            parts.append(" · ".join(f"{GAP_LABELS.get(k, k)} {clock.fmt_duration(v)}" for k, v in view.gaps.items()))
        self.sub_label.setText(" · ".join(parts))

        self.bar.set_data(view.categories)
        self._clear_categories()
        total = sum(view.categories.values()) or 1.0
        shown = 0
        for category, seconds in sorted(view.categories.items(), key=lambda kv: -kv[1]):
            if seconds < 30 or shown >= 7:
                continue
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            dot = QLabel()
            dot.setFixedSize(8, 8)
            dot.setStyleSheet(f"background: {theme.color(category).name()}; border-radius: 4px;")
            name = QLabel(theme.label(category))
            name.setFont(theme.font(9))
            name.setStyleSheet(f"color: {theme.TEXT};")
            value = QLabel(f"{clock.fmt_duration(seconds)}  {seconds / total * 100:.0f}%")
            value.setFont(theme.font(9))
            value.setStyleSheet(f"color: {theme.MUTED};")
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            value.setFixedWidth(96)
            row_layout.addWidget(dot)
            row_layout.addWidget(name)
            row_layout.addStretch(1)
            row_layout.addWidget(value)
            self.cat_box.addWidget(row)
            shown += 1

        if view.narrative:
            body = view.narrative.replace("\n", "<br>")
            html = f"<div style='line-height:150%;'>{body}</div>"
            if view.highlights:
                items = "".join(f"<li>{h}</li>" for h in view.highlights)
                html += f"<ul style='margin-top:8px; padding-left:16px;'>{items}</ul>"
            self.narrative.setHtml(html)
        elif view.sessions:
            self.narrative.setHtml(
                f"<div style='color:{theme.MUTED}'>还没有简报。运行 <code>dayloom report</code> "
                "或等日终 rollup。</div>"
            )
        else:
            self.narrative.setHtml("")

        notes = []
        if view.unanalyzed:
            notes.append(f"{view.unanalyzed} 帧尚未分析")
        if not view.has_db:
            notes.append("还没有数据库")
        self.footnote.setText(" · ".join(notes))
