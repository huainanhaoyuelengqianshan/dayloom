"""Analysis pipeline: keyframe selection, segmentation, merging, reporting."""

from __future__ import annotations

from .focus import build_focus_strip, parse_strip
from .merge import MergedSession, build_sessions, merge_activities
from .report import DayReport, build_report, compute_report, render
from .segment import SegmentOutcome, analyze_batch, parse_activities
from .select import Selection, group_states, select_keyframes

__all__ = [
    "select_keyframes",
    "group_states",
    "Selection",
    "analyze_batch",
    "parse_activities",
    "SegmentOutcome",
    "build_focus_strip",
    "parse_strip",
    "merge_activities",
    "build_sessions",
    "MergedSession",
    "build_report",
    "compute_report",
    "render",
    "DayReport",
]
