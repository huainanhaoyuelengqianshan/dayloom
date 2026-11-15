"""Vision capability probe.

Why this is not "send a 100x100 test image and see if it errors":

The dangerous failure is not a 400. It is an endpoint that *accepts* the image
and silently drops it — a text-only model behind an OpenAI-compatible proxy, a
llama.cpp build without ``--mmproj``, a gateway that strips ``image_url``. Those
return a fluent, plausible paragraph that describes nothing.

So the probe sends a picture whose answer cannot be guessed: five solid colour
cells in a random order. A model that cannot see them has a 1/4^5 chance of
matching the sequence by luck. The colours are generated locally, never sent
anywhere else, and the whole picture is synthetic — no user data involved.
"""

from __future__ import annotations

import io
import json
import random
import re
from dataclasses import dataclass, field

from PIL import Image, ImageDraw

CELL = 140
GAP = 16
MARGIN = 36
N_CELLS = 5

# name -> (rgb, accepted spellings)
PALETTE: dict[str, tuple[tuple[int, int, int], tuple[str, ...]]] = {
    "red": ((214, 45, 50), ("red", "红", "红色")),
    "green": ((40, 165, 85), ("green", "绿", "绿色")),
    "blue": ((45, 90, 210), ("blue", "蓝", "蓝色", "深蓝")),
    "yellow": ((240, 200, 55), ("yellow", "黄", "黄色", "金黄")),
}

_ALIAS_TO_NAME: dict[str, str] = {
    alias.lower(): name for name, (_, aliases) in PALETTE.items() for alias in aliases
}
_ALIAS_RE = re.compile(
    r"(?<![a-zA-Z])(" + "|".join(sorted(map(re.escape, _ALIAS_TO_NAME), key=len, reverse=True)) + r")(?![a-zA-Z])",
    re.IGNORECASE,
)


@dataclass(slots=True)
class Challenge:
    png: bytes
    colors: list[str]

    @property
    def answer(self) -> list[str]:
        return list(self.colors)


def make_challenge(rng: random.Random | None = None) -> Challenge:
    rnd = rng or random.Random()
    names = list(PALETTE)
    colors = [rnd.choice(names) for _ in range(N_CELLS)]
    if len(set(colors)) == 1:  # a single-colour picture is guessable
        colors[rnd.randrange(N_CELLS)] = rnd.choice([n for n in names if n != colors[0]])

    width = MARGIN * 2 + N_CELLS * CELL + (N_CELLS - 1) * GAP
    height = MARGIN * 2 + CELL
    img = Image.new("RGB", (width, height), (250, 250, 248))
    draw = ImageDraw.Draw(img)
    for idx, name in enumerate(colors):
        x0 = MARGIN + idx * (CELL + GAP)
        draw.rectangle([x0, MARGIN, x0 + CELL, MARGIN + CELL], fill=PALETTE[name][0])

    buf = io.BytesIO()
    img.save(buf, format="PNG")  # lossless: the mock samples exact pixels
    return Challenge(png=buf.getvalue(), colors=colors)


def cell_center(index: int) -> tuple[int, int]:
    x = MARGIN + index * (CELL + GAP) + CELL // 2
    y = MARGIN + CELL // 2
    return x, y


def read_challenge_colors(png: bytes) -> list[str]:
    """Nearest-palette sampling, i.e. what a machine reader would do.

    Used by the mock server so that it *actually looks at* the image instead of
    pretending; it also gives tests a ground-truth reader.
    """
    with Image.open(io.BytesIO(png)) as img:
        rgb = img.convert("RGB")
        out: list[str] = []
        for idx in range(N_CELLS):
            x, y = cell_center(idx)
            if x >= rgb.width or y >= rgb.height:
                break
            pixel = rgb.getpixel((x, y))
            out.append(_nearest(pixel))
        return out


def _nearest(pixel: tuple[int, int, int]) -> str:
    best = None
    best_dist = 1e9
    for name, (ref, _) in PALETTE.items():
        dist = sum((p - r) ** 2 for p, r in zip(pixel[:3], ref, strict=True))
        if dist < best_dist:
            best_dist = dist
            best = name
    return best or "red"


def parse_colors(text: str) -> list[str]:
    """Extract the colour sequence from either JSON or prose."""
    payload = _try_json(text)
    if payload is not None:
        seq: list[str] = []
        for item in payload:
            if isinstance(item, str):
                mapped = _canonical(item)
                if mapped:
                    seq.append(mapped)
        if seq:
            return seq
    return [_ALIAS_TO_NAME[m.group(1).lower()] for m in _ALIAS_RE.finditer(text or "")]


def _try_json(text: str) -> list | None:
    candidate = (text or "").strip()
    candidate = re.sub(r"^```[a-zA-Z]*\s*", "", candidate)
    candidate = re.sub(r"\s*```$", "", candidate)
    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        match = re.search(r"\{.*\}", candidate, re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if isinstance(data, dict):
        for key in ("colors", "colours", "sequence", "answer"):
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, str):
                return [value]
        # fall back to the first list of strings found
        for value in data.values():
            if isinstance(value, list):
                return value
    if isinstance(data, list):
        return data
    return None


def _canonical(word: str) -> str | None:
    w = word.strip().lower()
    if w in _ALIAS_TO_NAME:
        return _ALIAS_TO_NAME[w]
    m = _ALIAS_RE.search(w)
    return _ALIAS_TO_NAME[m.group(1).lower()] if m else None


@dataclass(slots=True)
class ProbeResult:
    ok: bool
    reason: str
    expected: list[str] = field(default_factory=list)
    detected: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)

    def summary(self) -> str:
        mark = "✓" if self.ok else "✗"
        return f"{mark} {self.reason}"
