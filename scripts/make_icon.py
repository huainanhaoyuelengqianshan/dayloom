"""Generate ``src/dayloom/assets/dayloom.ico`` from the same geometry the tray uses.

Deliberately Pillow-only (no Qt): this runs in CI and inside the build script,
where a display is not available. The shapes must stay in sync with
``dayloom.gui.tray`` — that module is the fallback painter when the packaged
icon is missing.

    python scripts/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ACCENT = (37, 99, 235)
SIZES = [16, 24, 32, 48, 64, 128, 256]
SUPERSAMPLE = 4

BAR_HEIGHTS = (0.40, 0.60, 0.34)


def draw(size: int) -> Image.Image:
    scale = size * SUPERSAMPLE
    image = Image.new("RGBA", (scale, scale), (0, 0, 0, 0))
    draw_ctx = ImageDraw.Draw(image)

    radius = scale * 0.24
    draw_ctx.rounded_rectangle([0, 0, scale - 1, scale - 1], radius=radius, fill=(*ACCENT, 255))

    bar_w = max(1.0, scale * 0.11)
    gap = scale * 0.13
    total = bar_w * len(BAR_HEIGHTS) + gap * (len(BAR_HEIGHTS) - 1)
    x = (scale - total) / 2
    for factor in BAR_HEIGHTS:
        bar_h = scale * factor
        draw_ctx.rounded_rectangle(
            [x, (scale - bar_h) / 2, x + bar_w, (scale + bar_h) / 2],
            radius=bar_w / 2,
            fill=(255, 255, 255, 255),
        )
        x += bar_w + gap

    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    out = Path(__file__).resolve().parents[1] / "src" / "dayloom" / "assets" / "dayloom.ico"
    out.parent.mkdir(parents=True, exist_ok=True)
    frames = [draw(size) for size in SIZES]
    largest = frames[-1]
    largest.save(out, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"wrote {out} ({out.stat().st_size} bytes, {len(SIZES)} sizes)")


if __name__ == "__main__":
    main()
