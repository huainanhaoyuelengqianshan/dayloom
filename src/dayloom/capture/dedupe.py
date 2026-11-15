"""Perceptual hashing and "is this even worth sending" checks.

dHash over a 9x8 grayscale thumbnail: 64 bits, cheap (a few ms at 1280px),
and good enough for the job we give it — deciding whether the screen changed
meaningfully since the last stored frame. We deliberately do not depend on
``imagehash``: it pulls numpy/scipy for something that fits in ten lines, and
keeping the dependency list short matters for a tool that ships to Windows
machines with corporate AV on them.
"""

from __future__ import annotations

from PIL import Image, ImageStat

HASH_SIZE = 8
U64_MASK = (1 << 64) - 1
U64_SIGN = 1 << 63


def to_signed(value: int | None) -> int | None:
    """Map an unsigned 64-bit hash into SQLite's signed INTEGER range.

    dHash naturally produces values in ``[0, 2**64)``; SQLite integers are
    signed 64-bit, so roughly half of all hashes would raise ``OverflowError``
    on insert and silently lose that frame.
    """
    if value is None:
        return None
    value &= U64_MASK
    return value - (1 << 64) if value >= U64_SIGN else value


def dhash(image: Image.Image, hash_size: int = HASH_SIZE) -> int:
    """Difference hash: 1 bit per horizontal neighbour comparison."""
    small = image.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
    # tobytes() instead of getdata(): same bytes, no Pillow deprecation, faster.
    px = small.tobytes()
    bits = 0
    row_len = hash_size + 1
    for row in range(hash_size):
        base = row * row_len
        for col in range(hash_size):
            left = px[base + col]
            right = px[base + col + 1]
            bits = (bits << 1) | (1 if left < right else 0)
    return bits


def hamming(a: int | None, b: int | None) -> int:
    if a is None or b is None:
        return 999
    # Values may come back negative from the database; compare as unsigned.
    return ((a & U64_MASK) ^ (b & U64_MASK)).bit_count()


def is_flat(image: Image.Image, min_stddev: float = 2.0) -> bool:
    """True for uniform frames: black DRM windows, screensavers, capture failures."""
    small = image.convert("L").resize((32, 32), Image.Resampling.BILINEAR)
    stat = ImageStat.Stat(small)
    return stat.stddev[0] < min_stddev
