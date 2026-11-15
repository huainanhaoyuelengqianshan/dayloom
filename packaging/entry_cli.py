"""PyInstaller entry point for the console build (CLI)."""

from __future__ import annotations

from dayloom.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
