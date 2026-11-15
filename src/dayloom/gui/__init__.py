"""Qt frontend for dayloom.

Design rule: this package only *reads*. The daemon owns writing (frames,
activities, reports); the GUI opens the same SQLite file read-only and asks the
CLI/daemon to do anything that mutates state. That keeps a crash in the UI from
ever costing a day of data.
"""

from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    from .app import main as _main

    return _main(argv)
