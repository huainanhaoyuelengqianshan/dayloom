from __future__ import annotations

import random
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from dayloom import clock, paths
from dayloom.config import Config
from dayloom.store.db import open_db
from dayloom.store.repo import Repo


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DAYLOOM_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def cfg(home: Path) -> Config:
    c = Config()
    c.general.data_dir = str(home)
    c.capture.max_edge = 640
    c.analysis.grace_seconds = 0
    c.llm.max_frames = 8
    return c


@pytest.fixture()
def repo(cfg: Config):
    db = open_db(cfg.db_path)
    r = Repo(db)
    try:
        yield r
    finally:
        db.close()


def make_image(seed: int, size: tuple[int, int] = (320, 200)) -> Image.Image:
    rng = random.Random(seed)
    img = Image.new("RGB", size, (245, 245, 245))
    draw = ImageDraw.Draw(img)
    for _ in range(6):
        x0 = rng.randrange(0, size[0] - 60)
        y0 = rng.randrange(0, size[1] - 40)
        draw.rectangle(
            [x0, y0, x0 + rng.randrange(20, 60), y0 + rng.randrange(15, 40)],
            fill=(rng.randrange(0, 255), rng.randrange(0, 255), rng.randrange(0, 255)),
        )
    return img


def add_frame(
    repo: Repo,
    home: Path,
    ts: float,
    *,
    app: str = "code",
    title: str = "dayloom — main.py",
    dhash_value: int = 0,
    seed: int = 0,
    focus: str = "",
) -> int:
    """Insert a frame row *and* the file it points at."""
    day = clock.day_key(ts, 4)
    rel = paths.frame_relpath(home, day, ts)
    path = home / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    make_image(seed, (320, 200)).save(path, format="WEBP")
    return repo.insert_frame(
        ts=ts,
        day=day,
        app=app,
        exe=f"C:/Apps/{app}.exe",
        title=title,
        monitor="m1:1920x1080+0+0",
        rect=(0, 0, 320, 200),
        file=rel,
        size=path.stat().st_size,
        dhash=dhash_value,
        focus=focus,
    )


def add_batch(repo: Repo, day: str, start: float, end: float, status: str = "done") -> int:
    return repo.db.execute(
        """
        INSERT INTO batches (day, start_ts, end_ts, status, attempts, created_at, updated_at)
        VALUES (?,?,?,?,0,?,?)
        """,
        (day, start, end, status, start, start),
    )


def add_activity(
    repo: Repo,
    batch_id: int,
    seq: int,
    start: float,
    end: float,
    *,
    app: str = "code",
    category: str = "coding",
    title: str = "",
    summary: str = "",
) -> int:
    return repo.db.execute(
        """
        INSERT INTO activities (batch_id, seq, start_ts, end_ts, app, category,
                                title, summary, confidence, frame_ids, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (batch_id, seq, start, end, app, category, title, summary, 0.8, "[]", start),
    )


def at(hour: int, minute: int = 0, second: int = 0, day: str = "2026-05-10") -> float:
    d = datetime.fromisoformat(day)
    return d.replace(hour=hour, minute=minute, second=second).timestamp()
