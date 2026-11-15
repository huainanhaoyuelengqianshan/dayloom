"""PyInstaller entry point for the windowed build (GUI)."""

from __future__ import annotations

from dayloom.gui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
