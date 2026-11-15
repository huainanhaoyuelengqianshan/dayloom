"""Screen capture: Windows plumbing, hashing and the capture loop."""

from __future__ import annotations

from .dedupe import dhash, hamming, is_flat
from .loop import Blacklist, CaptureLoop, CaptureStats, decide
from .screen import Grabber, encode_jpeg, encode_webp, jpeg_from_file, scale_to_max_edge

__all__ = [
    "dhash",
    "hamming",
    "is_flat",
    "decide",
    "CaptureLoop",
    "CaptureStats",
    "Blacklist",
    "Grabber",
    "encode_webp",
    "encode_jpeg",
    "jpeg_from_file",
    "scale_to_max_edge",
]
