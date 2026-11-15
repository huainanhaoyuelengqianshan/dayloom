"""Pick the frames worth sending to a model.

Uniform sampling ("send 12 frames from this 15-minute window") throws money at
near-duplicates: twelve screenshots of the same slide, and none of the 40
seconds the user spent in a chat window. Instead we group the window into
*visual states* — consecutive frames where neither the app/title nor the
picture changed meaningfully — and sample the edges of each state, which is
also exactly where activity boundaries are.

When there are more states than the image budget, the picks have to *cover the
window* rather than describe its most persistent state: a user switching
between two monitors produces dozens of short states, and the old policy
(drop the state with the shortest duration) peeled them off from the front,
so the model received only the tail of the batch and reported a whole quarter
of an hour as one app. Coverage-preserving thinning keeps the first and last
states and spreads the rest over the timeline.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..capture.dedupe import hamming
from ..store.repo import FrameRow

#: A state shorter than this keeps only its first frame (edges add nothing).
MIN_STATE_FOR_EDGE_SECONDS = 30.0
#: A state longer than this also gets a middle frame, so long stretches still
#: show what was happening halfway through.
MIDDLE_FRAME_AFTER_SECONDS = 300.0


@dataclass(slots=True)
class Selection:
    frames: list[FrameRow]
    state_count: int
    dropped: int

    @property
    def kept(self) -> int:
        return len(self.frames)


def group_states(
    frames: list[FrameRow], *, dedupe_distance: int
) -> list[list[FrameRow]]:
    """Split an ordered frame list into contiguous visual states."""
    states: list[list[FrameRow]] = []
    for frame in frames:
        if not states:
            states.append([frame])
            continue
        prev = states[-1][-1]
        same_window = (frame.app, frame.title) == (prev.app, prev.title)
        similar = hamming(frame.dhash, prev.dhash) <= dedupe_distance
        if same_window and similar:
            states[-1].append(frame)
        else:
            states.append([frame])
    return states


def _state_picks(state: list[FrameRow]) -> list[FrameRow]:
    if len(state) == 1:
        return [state[0]]
    picks = [state[0]]
    span = state[-1].ts - state[0].ts
    if span >= MIN_STATE_FOR_EDGE_SECONDS:
        picks.append(state[-1])
        if span >= MIDDLE_FRAME_AFTER_SECONDS:
            picks.append(state[len(state) // 2])
    return picks


def spread_indices(count: int, picks: int) -> list[int]:
    """Evenly spaced indices over ``count`` states, keeping both ends.

    ``count=52, picks=6`` gives ``[0, 10, 20, 31, 41, 51]``: the model sees the
    beginning, the middle and the end of the window instead of six neighbours.
    """
    if picks <= 0 or count <= 0:
        return []
    if picks >= count:
        return list(range(count))
    if picks == 1:
        return [count // 2]
    step = (count - 1) / float(picks - 1)
    return sorted({int(round(i * step)) for i in range(picks)})


def _thin_edges(picks: list[list[FrameRow]], max_frames: int) -> list[list[FrameRow]]:
    """Peel the tail frame of the shortest multi-frame states until it fits."""
    while sum(len(p) for p in picks) > max_frames:
        multi = [(p[-1].ts - p[0].ts, i) for i, p in enumerate(picks) if len(p) > 1]
        if not multi:
            break
        _, idx = min(multi)
        picks[idx] = picks[idx][:-1]
    return picks


def _thin_evenly(kept: list[FrameRow], max_frames: int) -> list[FrameRow]:
    if len(kept) <= max_frames:
        return kept
    step = len(kept) / float(max_frames)
    return [kept[min(len(kept) - 1, int(i * step))] for i in range(max_frames)]


def select_keyframes(
    frames: list[FrameRow], *, max_frames: int = 12, dedupe_distance: int = 3
) -> Selection:
    if not frames:
        return Selection([], 0, 0)
    ordered = sorted(frames, key=lambda f: f.ts)
    states = group_states(ordered, dedupe_distance=dedupe_distance)
    if len(ordered) <= max_frames:
        return Selection(ordered, len(states), 0)

    if len(states) <= max_frames:
        picks = _thin_edges([_state_picks(state) for state in states], max_frames)
        kept = [frame for pick in picks for frame in pick]
        kept.sort(key=lambda f: f.ts)
        kept = _thin_evenly(kept, max_frames)
        return Selection(kept, len(states), len(ordered) - len(kept))

    # More states than images: spread the budget over the whole window. The
    # first and last state always survive, so a batch that starts at 18:30
    # cannot be summarised from its 18:44 tail alone.
    chosen = spread_indices(len(states), max_frames)
    kept: list[FrameRow] = [states[idx][0] for idx in chosen]
    leftover = max_frames - len(kept)
    if leftover > 0:
        # Spend what is left on the *second* edge of the longest selected
        # states; a long state represented by one frame looks shorter than it is.
        ranked = sorted(
            (idx for idx in chosen if len(states[idx]) > 1),
            key=lambda idx: states[idx][-1].ts - states[idx][0].ts,
            reverse=True,
        )
        for idx in ranked[:leftover]:
            tail = states[idx][-1]
            if tail.ts != states[idx][0].ts:
                kept.append(tail)
    kept.sort(key=lambda f: f.ts)
    return Selection(kept, len(states), len(ordered) - len(kept))
