"""LLM layer: OpenAI-compatible client, prompt contracts, capability probe."""

from __future__ import annotations

from .probe import Challenge, ProbeResult, make_challenge, parse_colors, read_challenge_colors
from .prompts import CATEGORIES, CATEGORY_ZH, ROLLUP_PROMPT_VERSION, SEGMENT_PROMPT_VERSION
from .provider import (
    Completion,
    LLMError,
    LLMFormatError,
    LLMHTTPError,
    Part,
    ProviderProfile,
    Usage,
    VisionClient,
    extract_json,
)

__all__ = [
    "VisionClient",
    "Part",
    "Usage",
    "Completion",
    "ProviderProfile",
    "LLMError",
    "LLMHTTPError",
    "LLMFormatError",
    "extract_json",
    "make_challenge",
    "Challenge",
    "ProbeResult",
    "parse_colors",
    "read_challenge_colors",
    "CATEGORIES",
    "CATEGORY_ZH",
    "SEGMENT_PROMPT_VERSION",
    "ROLLUP_PROMPT_VERSION",
]
