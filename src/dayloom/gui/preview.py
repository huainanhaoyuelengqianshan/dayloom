"""Render the window to a PNG without a display.

Used for design iteration and for docs screenshots: it runs the real widget
tree on Qt's ``offscreen`` platform, so what you see in the file is what the app
draws.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dayloom-gui-preview")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--day", default=None)
    parser.add_argument("--out", type=Path, default=Path("dayloom-preview.png"))
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=820)
    args, _unknown = parser.parse_known_args(argv)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication

    from .. import config as config_mod
    from . import theme
    from .app import MainWindow

    cfg = config_mod.load(args.config) if args.config else config_mod.load()

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    app.setPalette(theme.light_palette())
    app.setStyleSheet(theme.STYLESHEET)
    app.setFont(theme.app_font())

    window = MainWindow(cfg, day=args.day, config_path=args.config)
    window.resize(args.width, args.height)
    window.show()
    for _ in range(5):
        app.processEvents()

    pixmap = window.grab()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if not pixmap.save(str(args.out)):
        print(f"保存失败：{args.out}", file=sys.stderr)
        return 1
    print(f"{args.out} ({pixmap.width()}x{pixmap.height()})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
