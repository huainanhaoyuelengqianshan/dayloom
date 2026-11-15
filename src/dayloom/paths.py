"""Where dayloom keeps its files.

Everything lives under one directory (``~/dayloom`` by default) so that
backup, deletion and "is this thing phoning home" inspection are one-liners.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_HOME = "DAYLOOM_HOME"


def asset(name: str) -> Path | None:
    """Locate a bundled resource (icon, etc.) in a wheel *or* a frozen build."""
    if getattr(sys, "frozen", False):
        bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidate = bundle / name
    else:
        candidate = Path(__file__).resolve().parent / "assets" / name
    return candidate if candidate.exists() else None


def home() -> Path:
    """Return the data directory, creating it if needed."""
    raw = os.environ.get(ENV_HOME, "").strip()
    base = Path(raw).expanduser() if raw else Path.home() / "dayloom"
    base.mkdir(parents=True, exist_ok=True)
    return base


def config_file(base: Path) -> Path:
    return base / "config.toml"


def db_file(base: Path) -> Path:
    return base / "dayloom.db"


def frames_dir(base: Path) -> Path:
    p = base / "frames"
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir(base: Path) -> Path:
    p = base / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def paused_flag(base: Path) -> Path:
    """A file, not a DB row: pausing must work even if the DB is locked."""
    return base / ".paused"


def pid_file(base: Path) -> Path:
    return base / "dayloom.pid"


def heartbeat_file(base: Path) -> Path:
    """Touched by the daemon every few seconds; see ``runtime.is_running``."""
    return base / ".heartbeat"


def frame_relpath(base: Path, day: str, ts: float) -> str:
    """Relative path (POSIX separators) for a frame stored under the data dir."""
    return f"frames/{day}/{int(ts * 1000)}.webp"


def resolve(base: Path, rel: str) -> Path:
    return base / rel
