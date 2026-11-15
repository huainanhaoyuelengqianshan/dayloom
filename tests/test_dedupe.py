from __future__ import annotations

from conftest import make_image
from PIL import Image

from dayloom.capture.dedupe import dhash, hamming, is_flat


def test_dhash_is_stable_for_the_same_picture():
    img = make_image(1)
    assert dhash(img) == dhash(img)
    assert hamming(dhash(img), dhash(img)) == 0


def test_dhash_separates_different_pictures():
    a, b = make_image(1), make_image(2)
    assert hamming(dhash(a), dhash(b)) > 3


def test_flat_frame_detection():
    assert is_flat(Image.new("RGB", (200, 120), (0, 0, 0)))
    assert is_flat(Image.new("RGB", (200, 120), (250, 250, 250)))
    assert not is_flat(make_image(3))


def test_dhash_fits_in_64_bits():
    value = dhash(make_image(4))
    assert 0 <= value < 2**64


def test_signed_roundtrip_for_sqlite():
    """SQLite INTEGER is signed; the top half of the u64 range must survive."""
    from dayloom.capture.dedupe import U64_MASK, to_signed

    assert to_signed(0) == 0
    assert to_signed(2**63 - 1) == 2**63 - 1
    assert to_signed(2**63) == -(2**63)
    assert to_signed(U64_MASK) == -1
    assert (to_signed(U64_MASK) & U64_MASK) == U64_MASK


def test_hamming_works_on_stored_negative_hashes():
    from dayloom.capture.dedupe import to_signed

    top = to_signed(2**64 - 1)  # stored as -1
    low = to_signed(0b1)
    assert hamming(top, low) == 63
    assert hamming(top, top) == 0
