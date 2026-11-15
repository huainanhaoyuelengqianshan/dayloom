from __future__ import annotations

from dayloom.capture.loop import decide

BASE = dict(
    last_store_ts=1000.0,
    last_app="code",
    last_title="main.py",
    app="code",
    title="main.py",
    distance=0,
    dedupe_distance=3,
    heartbeat=20.0,
    max_still_minutes=10,
    flat=False,
)


def check(**overrides):
    params = dict(BASE)
    params.update(overrides)
    now = params.pop("now", 1010.0)
    return decide(now=now, **params)


def test_first_frame_always_stored():
    assert decide(now=1000.0, last_store_ts=None, last_app="", last_title="", app="code",
                  title="x", distance=0, dedupe_distance=3, heartbeat=20,
                  max_still_minutes=10, flat=False).store


def test_app_switch_is_captured_immediately():
    assert check(app="chrome").store


def test_title_change_waits_for_heartbeat():
    assert not check(title="main.py — edited", now=1005.0).store
    assert check(title="main.py — edited", now=1030.0).store


def test_visual_change_within_heartbeat_is_deduped():
    assert not check(distance=20, now=1005.0).store
    assert check(distance=20, now=1025.0).store


def test_static_screen_gets_an_anchor_frame():
    assert not check(distance=0, now=1100.0).store
    assert check(distance=0, now=1600.0).reason == "anchor"


def test_flat_frames_are_never_stored():
    decision = check(flat=True, now=5000.0)
    assert not decision.store
    assert decision.reason == "flat"


def test_zero_max_still_disables_anchors():
    assert not check(distance=0, max_still_minutes=0, now=99999.0).store
