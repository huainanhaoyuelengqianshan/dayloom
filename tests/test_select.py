from __future__ import annotations

from dayloom.analyzer.select import group_states, select_keyframes, spread_indices
from dayloom.store.repo import FrameRow


def frame(ts: float, *, app: str = "code", title: str = "main.py", dhash_value: int = 0,
          fid: int = 0, monitor: str = "") -> FrameRow:
    return FrameRow(
        id=fid,
        ts=ts,
        day="2026-05-10",
        app=app,
        exe="",
        title=title,
        monitor=monitor,
        x=0,
        y=0,
        w=None,
        h=None,
        file=None,
        bytes=None,
        dhash=dhash_value,
        batch_id=None,
    )


def test_empty_input():
    selection = select_keyframes([])
    assert selection.frames == []
    assert selection.state_count == 0


def test_small_batch_is_kept_whole():
    frames = [frame(1000.0 + i) for i in range(5)]
    assert len(select_keyframes(frames, max_frames=12).frames) == 5


def test_static_screen_is_one_state():
    frames = [frame(1000.0 + i * 10, dhash_value=42) for i in range(50)]
    states = group_states(frames, dedupe_distance=3)
    assert len(states) == 1
    selection = select_keyframes(frames, max_frames=12)
    assert selection.state_count == 1
    assert selection.frames[0].ts == frames[0].ts
    assert selection.frames[-1].ts == frames[-1].ts


def test_window_switch_starts_a_new_state():
    frames = [frame(1000.0, app="code"), frame(1010.0, app="chrome"), frame(1020.0, app="code")]
    states = group_states(frames, dedupe_distance=3)
    assert len(states) == 3


def test_selection_respects_the_budget():
    frames = []
    for i in range(200):
        frames.append(frame(1000.0 + i, app=f"app{i % 20}", dhash_value=i, fid=i))
    selection = select_keyframes(frames, max_frames=8)
    assert len(selection.frames) <= 8
    assert selection.dropped >= 100
    timestamps = [f.ts for f in selection.frames]
    assert timestamps == sorted(timestamps)


def test_selection_keeps_distinct_apps_when_budget_allows():
    frames = [frame(1000.0 + i * 30, app=f"app{i}") for i in range(5)]
    selection = select_keyframes(frames, max_frames=12)
    apps = {f.app for f in selection.frames}
    assert len(apps) == 5


def test_spread_indices_keeps_both_ends():
    assert spread_indices(52, 6) == [0, 10, 20, 31, 41, 51]
    assert spread_indices(3, 10) == [0, 1, 2]
    assert spread_indices(0, 6) == []


def test_switch_fest_covers_the_whole_window():
    """Two monitors mean dozens of one-frame states; the picks must span them.

    The old reduction dropped the shortest states from the front, so batch
    236 (52 states, budget 6) sent the model only 18:43:50–18:44:54 and the
    first 14 minutes were reported as one app.
    """
    frames = [
        frame(
            1000.0 + i * 20,
            app="wps" if i % 2 == 0 else "tabbit browser",
            title=f"t{i}",
            dhash_value=i,
            fid=i,
            monitor="m1:2560x1440+0+0" if i % 2 == 0 else "m2:2560x1440+-2560+236",
        )
        for i in range(52)
    ]
    selection = select_keyframes(frames, max_frames=6)
    assert len(selection.frames) <= 6
    stamps = [f.ts for f in selection.frames]
    assert stamps == sorted(stamps)
    # Both ends of the window survive.
    assert stamps[0] == frames[0].ts
    assert stamps[-1] == frames[-1].ts
    # And both monitors are represented, not just whichever one ended the run.
    monitors = {f.monitor.split(":")[0] for f in selection.frames}
    assert monitors == {"m1", "m2"}


def test_switch_fest_picks_are_spread_over_time():
    frames = [
        frame(1000.0 + i, app=f"app{i % 7}", title=str(i), dhash_value=i, fid=i)
        for i in range(70)
    ]
    selection = select_keyframes(frames, max_frames=6)
    gaps = [b.ts - a.ts for a, b in zip(selection.frames, selection.frames[1:], strict=False)]
    assert len(selection.frames) <= 6
    # Uniform-ish coverage: no gap is dramatically larger than the average.
    assert max(gaps) <= 2.5 * (sum(gaps) / len(gaps))
