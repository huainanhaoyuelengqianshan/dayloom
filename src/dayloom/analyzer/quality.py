"""How good was that batch, and is the next tier worth paying for?

The model will happily return confident-sounding segments for a screen it could
barely read. Three cheap signals catch most of that, all computed from the
activities we already have:

* **confidence** — the model's own number, duration-weighted;
* **unknown ratio** — how much of the batch it admits it could not read;
* **shallow ratio** — segments whose "summary" is just the app name, the window
  title, or a couple of characters. That is the shape of a model that produced
  structure without understanding.

No new request is needed to compute these, so escalating costs exactly one extra
analysis — and only when the cheap tier actually did badly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ..config import QualityGate
    from ..store.repo import NewActivity


@dataclass(slots=True)
class QualityReport:
    segments: int = 0
    mean_confidence: float = 1.0
    unknown_ratio: float = 0.0
    shallow_ratio: float = 0.0

    @property
    def score(self) -> float:
        """0..1, for logging and for sorting candidates."""
        penalty = (1 - self.unknown_ratio) * (1 - self.shallow_ratio * 0.5)
        return max(0.0, self.mean_confidence * penalty)

    def failure(self, gate: QualityGate) -> str | None:
        """Return a human reason when this result should be escalated."""
        reasons: list[str] = []
        if self.mean_confidence < gate.min_confidence:
            reasons.append(
                f"置信度 {self.mean_confidence:.2f} < {gate.min_confidence:.2f}"
            )
        if self.unknown_ratio > gate.max_unknown_ratio:
            reasons.append(
                f"unknown 占 {self.unknown_ratio:.0%} > {gate.max_unknown_ratio:.0%}"
            )
        if self.shallow_ratio > gate.max_shallow_ratio:
            reasons.append(
                f"空泛描述占 {self.shallow_ratio:.0%} > {gate.max_shallow_ratio:.0%}"
            )
        return "；".join(reasons) if reasons else None


def _is_shallow(activity: NewActivity) -> bool:
    # Media segments are *supposed* to be one short line ("在B站看直播：英雄联盟"),
    # so brevity there is the intended shape, not a model failure.
    if getattr(activity, "category", "") == "media":
        return False
    summary = (activity.summary or "").strip()
    title = (activity.title or "").strip()
    app = (activity.app or "").strip().lower()
    if len(summary) < 6:
        return True
    if summary == title:
        return True
    return bool(app) and summary.lower() == app


def assess(activities: list[NewActivity]) -> QualityReport:
    if not activities:
        return QualityReport(segments=0, mean_confidence=0.0, unknown_ratio=1.0, shallow_ratio=1.0)

    durations = [max(1.0, activity.end_ts - activity.start_ts) for activity in activities]
    total = sum(durations)
    weighted = sum(a.confidence * d for a, d in zip(activities, durations, strict=True))
    unknown = sum(
        d for a, d in zip(activities, durations, strict=True) if a.category == "unknown"
    )
    shallow = sum(d for a, d in zip(activities, durations, strict=True) if _is_shallow(a))
    return QualityReport(
        segments=len(activities),
        mean_confidence=weighted / total,
        unknown_ratio=unknown / total,
        shallow_ratio=shallow / total,
    )
