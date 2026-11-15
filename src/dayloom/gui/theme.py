"""Visual language: one place for colours, fonts and the stylesheet."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette

from ..llm.prompts import CATEGORY_ZH

BG = "#F5F6F8"
CARD = "#FFFFFF"
BORDER = "#E4E7EC"
HOVER = "#F2F5F9"
TEXT = "#101828"
MUTED = "#667085"
FAINT = "#98A2B3"
ACCENT = "#2563EB"
ACCENT_SOFT = "#DBEAFE"
OK = "#079455"
WARN = "#DC6803"
BAD = "#D92D20"

CATEGORY_COLORS: dict[str, str] = {
    "coding": "#3B82F6",
    "writing": "#8B5CF6",
    "reading": "#06B6D4",
    "browsing": "#10B981",
    "communication": "#F59E0B",
    "meeting": "#EF4444",
    "design": "#EC4899",
    "media": "#A855F7",
    "gaming": "#14B8A6",
    "admin": "#64748B",
    "learning": "#0EA5E9",
    "other": "#94A3B8",
    "unknown": "#CBD5E1",
}

FONT_FAMILY = "Microsoft YaHei UI"


def color(category: str) -> QColor:
    return QColor(CATEGORY_COLORS.get(category, CATEGORY_COLORS["other"]))


def label(category: str) -> str:
    return CATEGORY_ZH.get(category, category)


def font(size: float, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    f = QFont(FONT_FAMILY, int(size))
    f.setWeight(weight)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return f


def app_font() -> QFont:
    return font(10)


def light_palette() -> QPalette:
    """Pin a light palette.

    The stylesheet paints most surfaces light, but unstyled widgets (menus,
    message boxes, tooltips) take their colours from the palette. On a Windows
    machine in dark mode that produced light-grey text on our white cards —
    exactly the "tray menu is unreadable" bug. Design is light; say so.
    """
    palette = QPalette()
    roles = QPalette.ColorRole
    palette.setColor(roles.Window, QColor(BG))
    palette.setColor(roles.WindowText, QColor(TEXT))
    palette.setColor(roles.Base, QColor(CARD))
    palette.setColor(roles.AlternateBase, QColor(HOVER))
    palette.setColor(roles.Text, QColor(TEXT))
    palette.setColor(roles.PlaceholderText, QColor(FAINT))
    palette.setColor(roles.Button, QColor(CARD))
    palette.setColor(roles.ButtonText, QColor(TEXT))
    palette.setColor(roles.BrightText, QColor("#FFFFFF"))
    palette.setColor(roles.Highlight, QColor(ACCENT))
    palette.setColor(roles.HighlightedText, QColor("#FFFFFF"))
    palette.setColor(roles.ToolTipBase, QColor(TEXT))
    palette.setColor(roles.ToolTipText, QColor("#FFFFFF"))
    palette.setColor(roles.Link, QColor(ACCENT))
    palette.setColor(roles.LinkVisited, QColor(ACCENT))
    disabled = QPalette.ColorGroup.Disabled
    palette.setColor(disabled, roles.Text, QColor(FAINT))
    palette.setColor(disabled, roles.WindowText, QColor(FAINT))
    palette.setColor(disabled, roles.ButtonText, QColor(FAINT))
    return palette


STYLESHEET = f"""
QMainWindow, QWidget#Root {{
    background: {BG};
}}
QToolBar {{
    background: {CARD};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 10px;
    spacing: 6px;
}}
QToolBar QToolButton, QPushButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 7px;
    padding: 6px 12px;
    color: {TEXT};
    font-size: 13px;
}}
QToolBar QToolButton:hover, QPushButton:hover {{
    background: {HOVER};
    border-color: {BORDER};
}}
QToolBar QToolButton:pressed, QPushButton:pressed {{
    background: {ACCENT_SOFT};
}}
QPushButton#Primary {{
    background: {ACCENT};
    color: white;
    border: none;
    font-weight: 600;
}}
QPushButton#Primary:hover {{
    background: #1D4ED8;
}}
QDateEdit {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 5px 8px;
    min-width: 120px;
    color: {TEXT};
}}
QDateEdit::drop-down {{ border: none; width: 18px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 4px 2px 4px 2px;
}}
QScrollBar::handle:vertical {{
    background: #D0D5DD; border-radius: 5px; min-height: 40px;
}}
QScrollBar::handle:vertical:hover {{ background: #B4BCC8; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0px; }}
QStatusBar {{
    background: {CARD};
    border-top: 1px solid {BORDER};
    color: {MUTED};
    font-size: 12px;
}}
QStatusBar::item {{ border: none; }}
QTextBrowser {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 10px;
    color: {TEXT};
}}
QMenuBar {{ background: {CARD}; border-bottom: 1px solid {BORDER}; }}
QMenuBar::item {{ padding: 6px 10px; background: transparent; }}
QMenuBar::item:selected {{ background: {HOVER}; border-radius: 6px; }}
QMenu {{ background: {CARD}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px; }}
QMenu::item {{ color: {TEXT}; padding: 6px 22px 6px 16px; border-radius: 6px; }}
QMenu::item:selected {{ background: {HOVER}; color: {TEXT}; }}
QMenu::item:disabled {{ color: {FAINT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 5px 10px; }}
QToolTip {{
    background: {TEXT}; color: white; border: none; padding: 6px 8px; border-radius: 6px;
}}
"""
